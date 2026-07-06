"""Image Generation -- illustrates the story with Novita AI text-to-image.

Every N storyteller generations (or on demand via /image) the latest narration
is condensed into an image prompt by the smartest LLM slot, then submitted to
Novita's async txt2img API. The whole pipeline runs as a fire-and-forget
background task so the player keeps playing while the image renders; the
chat-feed footer widget polls the module's index and shows the image under the
turn it illustrates. Novita hosts thousands of community checkpoints, so the
model is picked via a searchable dropdown backed by the /models proxy below.

Config is global (one Novita key for all stories), owned by this module and
edited in the Image Studio main-menu screen -- not in per-save settings.
"""
import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MODULE_ID = "wb_image_gen"

NOVITA_BASE = "https://api.novita.ai"
CIVITAI_BASE = "https://civitai.com/api/v1"

# Built-in first-party model routed to /v3/async/flux-2-dev (not in /v3/model).
# Its LoRAs are passed as download URLs, so any Civitai Flux LoRA works directly.
FLUX2_MODEL_NAME = "flux-2-dev"
FLUX2_SIZE_MIN, FLUX2_SIZE_MAX = 256, 1536

CIVITAI_SORTS = ["Most Downloaded", "Newest", "Highest Rated"]
CIVITAI_LORA_TYPES = ["LORA", "LoCon", "DoRA"]
# Civitai's nsfw param: false = SFW only, true = mixed. "only" post-filters.
CIVITAI_NSFW_MODES = ["off", "include", "only"]
CIVITAI_BASE_MODELS = [
    "SD 1.5", "SDXL 1.0", "Pony", "Illustrious", "NoobAI", "Flux.1 D", "Flux.2 D",
]

LORA_LIBRARY_MAX = 200
SD_LORAS_MAX = 5        # per txt2img request
FLUX_LORAS_MAX = 3      # flux-2-dev accepts up to 3 URL loras

SAMPLERS = [
    "DPM++ 2M Karras",
    "DPM++ SDE Karras",
    "DPM++ 2S a Karras",
    "Euler a",
    "Euler",
    "DDIM",
    "UniPC",
    "LMS",
]
KEY_MASK_PREFIX = "****"

# Novita rejects prompts over 1024 characters.
MAX_PROMPT_CHARS = 1024

POLL_INTERVAL_S = 2.0
POLL_MAX_ITERATIONS = 240          # ~8 minutes
POLL_MAX_TRANSIENT_FAILURES = 5
SUBMIT_RETRIES = 2
INDEX_MAX_RECORDS = 500

DEFAULT_PROMPT_TEMPLATE = """You write prompts for an AI image generator. Turn the scene below into ONE vivid image-generation prompt.

Rules:
- Describe a single striking moment from the LATEST SCENE: subjects, action, setting, lighting, mood, camera framing.
- Concrete visual language only. No story summary, no character inner thoughts, no proper-noun lore the image model cannot know -- describe what things LOOK like instead.
- Output ONLY the prompt text, no quotes, no preamble, under 150 words.

EARLIER CONTEXT (for continuity only):
{history}

LATEST SCENE (illustrate this):
{narration}"""

DEFAULT_PROMPT_TEMPLATE_TAGS = """You write prompts for an AI image generator that expects DANBOORU-STYLE TAGS. Turn the scene below into ONE comma-separated tag list depicting a single striking moment from the latest scene.

Rules:
- Output comma-separated booru tags, most important first: subject count (1girl, 1boy, 2girls, no humans...), then appearance (hair, eyes, clothing, species), action/pose, expression, setting, lighting, mood, composition (close-up, from above, wide shot...).
- Lowercase danbooru conventions. Concrete visual tags only -- no story summary, no proper-noun lore the image model cannot know; describe what things LOOK like instead.
- Output ONLY the tag list, no quotes, no preamble, 20-40 tags.

EARLIER CONTEXT (for continuity only):
{history}

LATEST SCENE (illustrate this):
{narration}"""

DEFAULT_PONY_QUALITY_TAGS = "score_9, score_8_up, score_7_up"

_services: dict = {}
_tasks: set = set()
_gen_lock: asyncio.Lock | None = None
_index_lock: asyncio.Lock | None = None


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _data_dir() -> Path:
    base = _services.get("data_dir")
    if base:
        root = Path(base) / MODULE_ID
    else:
        root = Path(__file__).resolve().parent.parent.parent / "data" / MODULE_ID
    (root / "images").mkdir(parents=True, exist_ok=True)
    return root


def _default_config() -> dict:
    return {
        "enabled": False,
        "api_key": "",
        "model_name": "",               # a Novita checkpoint sd_name, picked via search
        "model_base": "",               # the picked model's base_model metadata (drives prompt style)
        "width": 1024,
        "height": 1024,
        "steps": 28,
        "guidance_scale": 7.0,
        "sampler_name": "DPM++ 2M Karras",
        "negative_prompt": "blurry, low quality, watermark, text, deformed",
        "interval": 3,
        "prompt_model_preference": "smartest",
        "prompt_template": DEFAULT_PROMPT_TEMPLATE,
        "prompt_template_tags": DEFAULT_PROMPT_TEMPLATE_TAGS,
        "pony_quality_tags": DEFAULT_PONY_QUALITY_TAGS,
        "style_suffix": "",
        "civitai_api_key": "",
        "civitai_nsfw": "off",          # one of CIVITAI_NSFW_MODES
        "lora_library": [],             # saved Civitai LoRAs; see _normalize_lora_entry
    }


def _atomic_write_json(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_config() -> dict:
    cfg = _default_config()
    path = _data_dir() / "config.json"
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in cfg})
        except (json.JSONDecodeError, OSError) as e:
            print(f"[Image Gen] Failed to read config.json: {e}")
    # civitai_nsfw was a bool before it became a mode string.
    if cfg.get("civitai_nsfw") not in CIVITAI_NSFW_MODES:
        cfg["civitai_nsfw"] = "include" if cfg.get("civitai_nsfw") is True else "off"
    return cfg


def _save_config(cfg: dict) -> None:
    _atomic_write_json(_data_dir() / "config.json", cfg)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return KEY_MASK_PREFIX + key[-4:]


def _get_index_lock() -> asyncio.Lock:
    global _index_lock
    if _index_lock is None:
        _index_lock = asyncio.Lock()
    return _index_lock


def _get_gen_lock() -> asyncio.Lock:
    global _gen_lock
    if _gen_lock is None:
        _gen_lock = asyncio.Lock()
    return _gen_lock


def _read_index() -> list[dict]:
    path = _data_dir() / "index.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
        return records if isinstance(records, list) else []
    except (json.JSONDecodeError, OSError) as e:
        print(f"[Image Gen] Failed to read index.json: {e}")
        return []


def _write_index(records: list[dict]) -> None:
    _atomic_write_json(_data_dir() / "index.json", records[-INDEX_MAX_RECORDS:])


async def _append_record(record: dict) -> None:
    async with _get_index_lock():
        records = _read_index()
        records.append(record)
        _write_index(records)


async def _patch_record(record_id: str, **fields) -> None:
    async with _get_index_lock():
        records = _read_index()
        for record in records:
            if record.get("id") == record_id:
                record.update(fields)
                break
        _write_index(records)


def set_services(services: dict) -> None:
    """Capture shared engine services and clean up records that were mid-flight
    when the server last stopped, so the frontend never polls them forever."""
    global _services
    _services = services or {}
    try:
        records = _read_index()
        dirty = False
        for record in records:
            if record.get("status") in ("pending", "prompting", "generating"):
                record["status"] = "error"
                record["error"] = "interrupted by restart"
                dirty = True
        if dirty:
            _write_index(records)
    except Exception as e:
        print(f"[Image Gen] Startup index cleanup failed: {e}")


# --------------------------------------------------------------------------
# Prompt writing (LLM slot, never a concrete model)
# --------------------------------------------------------------------------

def _model_ident(cfg: dict) -> str:
    """Base-model metadata plus sd_name, for prompt-style detection. The name is
    included as a fallback for configs saved before model_base was stored."""
    return f"{cfg.get('model_base', '')} {cfg.get('model_name', '')}".lower()


def _prompt_style(cfg: dict) -> str:
    """"tags" (danbooru) for Pony/Illustrious bases, "natural" for Flux and
    everything else."""
    ident = _model_ident(cfg)
    if "pony" in ident or "illustrious" in ident:
        return "tags"
    return "natural"


def _is_pony(cfg: dict) -> bool:
    return "pony" in _model_ident(cfg)


# --------------------------------------------------------------------------
# LoRA library
# --------------------------------------------------------------------------

def _base_family(base: str) -> str:
    """Coarse base-model family for LoRA/checkpoint compatibility. SDXL-class
    covers everything trained on SDXL (Pony, Illustrious, NoobAI...)."""
    ident = str(base or "").lower()
    if "flux" in ident:
        return "flux"
    if "xl" in ident or "pony" in ident or "illustrious" in ident or "noob" in ident:
        return "sdxl"
    if "1.5" in ident or "sd 1" in ident or "sd1" in ident:
        return "sd15"
    return ""


def _checkpoint_family(cfg: dict) -> str:
    if cfg.get("model_name") == FLUX2_MODEL_NAME:
        return "flux"
    return _base_family(_model_ident(cfg))


def _entry_sd_name(entry: dict) -> str:
    """Novita library name for an SD-family LoRA: the hash-match result, or the
    user's manual override (for LoRAs console-uploaded to their account)."""
    override = str(entry.get("sd_name_override") or "").strip()
    if override:
        return override
    return str((entry.get("novita") or {}).get("sd_name_in_api") or "")


def _active_loras(cfg: dict) -> list[dict]:
    library = cfg.get("lora_library")
    if not isinstance(library, list):
        return []
    return [e for e in library if isinstance(e, dict) and e.get("active")]


def _entry_usable(entry: dict, family: str) -> bool:
    """Active entry is usable when it matches the checkpoint family and has a
    way to reach Novita (library name for SD, download URL for Flux)."""
    if _base_family(entry.get("base_model")) != family or not family:
        return False
    if family == "flux":
        return bool(str(entry.get("download_url") or "").strip())
    return bool(_entry_sd_name(entry))


def _sd_payload_loras(cfg: dict) -> list[dict]:
    family = _checkpoint_family(cfg)
    if family == "flux":
        return []
    out = []
    for entry in _active_loras(cfg):
        if not _entry_usable(entry, family):
            continue
        try:
            strength = max(0.0, min(1.0, float(entry.get("strength", 0.7))))
        except (TypeError, ValueError):
            strength = 0.7
        out.append({"model_name": _entry_sd_name(entry), "strength": round(strength, 2)})
    return out[:SD_LORAS_MAX]


def _civitai_download_link(entry: dict, cfg: dict) -> str:
    """Civitai download URL with the user's token appended — Civitai requires
    auth on downloads, and Novita fetches the file server-side."""
    url = str(entry.get("download_url") or "").strip()
    key = str(cfg.get("civitai_api_key") or "").strip()
    if url and key:
        url += ("&" if "?" in url else "?") + "token=" + key
    return url


def _flux_payload_loras(cfg: dict) -> list[str]:
    if _checkpoint_family(cfg) != "flux":
        return []
    urls = [
        _civitai_download_link(entry, cfg)
        for entry in _active_loras(cfg)
        if _entry_usable(entry, "flux")
    ]
    return [u for u in urls if u][:FLUX_LORAS_MAX]


def _applied_lora_names(cfg: dict) -> list[str]:
    family = _checkpoint_family(cfg)
    return [
        str(entry.get("name") or entry.get("id") or "?")
        for entry in _active_loras(cfg)
        if _entry_usable(entry, family)
    ][:FLUX_LORAS_MAX if family == "flux" else SD_LORAS_MAX]


def _active_trigger_words(cfg: dict) -> list[str]:
    """Trained trigger words of the LoRAs that will actually be applied, so the
    prompt-writer LLM can work them in and the LoRAs fire."""
    family = _checkpoint_family(cfg)
    words: list[str] = []
    seen: set[str] = set()
    for entry in _active_loras(cfg):
        if not _entry_usable(entry, family):
            continue
        for word in (entry.get("trained_words") or [])[:4]:
            word = str(word).strip().strip(",")
            if word and word.lower() not in seen:
                seen.add(word.lower())
                words.append(word)
    return words[:12]


def _render_template(template: str, narration: str, history: str) -> str:
    # Sequential replace instead of str.format: narration prose routinely
    # contains braces that would blow up format().
    out = template.replace("{narration}", narration)
    out = out.replace("{history}", history or "(story just began)")
    return out


def _clean_image_prompt(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        first_line, _, rest = text.partition("\n")
        if first_line.strip().lower() in ("text", "prompt", "markdown"):
            text = rest
    text = text.strip().strip('"').strip()
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_PROMPT_CHARS]


async def _write_image_prompt(cfg: dict, narration: str, history: str, sdk) -> str:
    style = _prompt_style(cfg)
    if style == "tags":
        template = cfg.get("prompt_template_tags") or DEFAULT_PROMPT_TEMPLATE_TAGS
    else:
        template = cfg.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE
    prompt = _render_template(template, narration[-4000:], history[-3000:])
    triggers = _active_trigger_words(cfg)
    if triggers:
        prompt += ("\n\nMANDATORY: weave these trigger words into the output verbatim "
                   "(they activate style adapters): " + ", ".join(triggers))
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(
            prompt, model_preference=cfg.get("prompt_model_preference", "smartest"))
    finally:
        sdk.llm._current_module = ""
    image_prompt = _clean_image_prompt(raw)
    if not image_prompt:
        raise RuntimeError("prompt writer returned an empty prompt")

    # Pony checkpoints are trained to expect score_* quality tags up front.
    prefix = str(cfg.get("pony_quality_tags") or "").strip() if _is_pony(cfg) else ""
    suffix = str(cfg.get("style_suffix") or "").strip()

    # Trim the scene text, never the prefix/suffix, to fit Novita's cap.
    reserved = (len(prefix) + 2 if prefix else 0) + (len(suffix) + 2 if suffix else 0)
    image_prompt = image_prompt[:max(0, MAX_PROMPT_CHARS - reserved)].rstrip(", ")
    pieces = [p for p in (prefix, image_prompt, suffix) if p]
    return ", ".join(pieces)[:MAX_PROMPT_CHARS]


# --------------------------------------------------------------------------
# Novita client
# --------------------------------------------------------------------------

def _novita_headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_key']}", "accept": "application/json"}


def _novita_payload(cfg: dict, image_prompt: str) -> dict:
    payload = {
        "extra": {"response_image_type": "jpeg"},
        "request": {
            "model_name": str(cfg.get("model_name", "")),
            "prompt": image_prompt[:MAX_PROMPT_CHARS],
            "width": int(cfg.get("width", 1024)),
            "height": int(cfg.get("height", 1024)),
            "image_num": 1,
            "steps": int(cfg.get("steps", 28)),
            "guidance_scale": float(cfg.get("guidance_scale", 7.0)),
            "sampler_name": str(cfg.get("sampler_name", "DPM++ 2M Karras")),
            "seed": -1,
        },
    }
    negative = str(cfg.get("negative_prompt") or "").strip()
    if negative:
        payload["request"]["negative_prompt"] = negative[:MAX_PROMPT_CHARS]
    loras = _sd_payload_loras(cfg)
    if loras:
        payload["request"]["loras"] = loras
    return payload


def _flux2_payload(cfg: dict, image_prompt: str) -> dict:
    clamp = lambda v: max(FLUX2_SIZE_MIN, min(FLUX2_SIZE_MAX, int(v or 1024)))
    payload = {
        "prompt": image_prompt[:MAX_PROMPT_CHARS],
        "size": f"{clamp(cfg.get('width'))}*{clamp(cfg.get('height'))}",
        "seed": -1,
    }
    loras = _flux_payload_loras(cfg)
    if loras:
        payload["loras"] = loras
    return payload


def _novita_error_detail(resp) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body.get("reason") or body)[:300]
    except Exception:
        return resp.text[:300]


async def _novita_submit(cfg: dict, image_prompt: str) -> str:
    """Submit an async generation task; return the task id. FLUX.2 is a
    first-party model on its own endpoint; everything else is SD txt2img."""
    import httpx
    if cfg.get("model_name") == FLUX2_MODEL_NAME:
        url = f"{NOVITA_BASE}/v3/async/flux-2-dev"
        payload = _flux2_payload(cfg, image_prompt)
    else:
        url = f"{NOVITA_BASE}/v3/async/txt2img"
        payload = _novita_payload(cfg, image_prompt)
    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        for attempt in range(SUBMIT_RETRIES + 1):
            try:
                resp = await client.post(url, headers=_novita_headers(cfg), json=payload)
                if resp.status_code in (401, 403):
                    raise RuntimeError(
                        f"Novita rejected the request ({resp.status_code}): invalid API key")
                if resp.status_code in (400, 402, 422, 429):
                    raise RuntimeError(
                        f"Novita rejected the request ({resp.status_code}): {_novita_error_detail(resp)}")
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Novita server error {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                task_id = (resp.json() or {}).get("task_id")
                if not task_id:
                    raise RuntimeError(f"Novita response missing task_id: {resp.text[:300]}")
                return task_id
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_error = e
                if attempt < SUBMIT_RETRIES:
                    await asyncio.sleep(2 + attempt * 3)
    raise RuntimeError(f"Novita submit failed after {SUBMIT_RETRIES + 1} attempts: {last_error}")


async def _novita_poll(cfg: dict, task_id: str) -> str:
    """Poll the task until it succeeds; return the presigned image URL."""
    import httpx
    url = f"{NOVITA_BASE}/v3/async/task-result"
    transient_failures = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        for _ in range(POLL_MAX_ITERATIONS):
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                resp = await client.get(url, headers=_novita_headers(cfg),
                                        params={"task_id": task_id})
                resp.raise_for_status()
                body = resp.json()
                transient_failures = 0
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                transient_failures += 1
                if transient_failures > POLL_MAX_TRANSIENT_FAILURES:
                    raise RuntimeError(f"Novita polling kept failing: {e}")
                continue

            task = body.get("task") or {}
            status = str(task.get("status", ""))
            if status == "TASK_STATUS_SUCCEED":
                images = body.get("images") or []
                image_url = images[0].get("image_url") if images else None
                if not image_url:
                    raise RuntimeError("Novita task succeeded but returned no image URL")
                return image_url
            if status == "TASK_STATUS_FAILED":
                reason = str(task.get("reason") or "no reason given")[:300]
                raise RuntimeError(f"Novita generation failed: {reason}")
            # TASK_STATUS_QUEUED / TASK_STATUS_PROCESSING: keep polling.
    raise RuntimeError("Novita generation timed out")


async def _download(image_url: str) -> tuple[bytes, str]:
    """Download the presigned result immediately (the URL expires)."""
    import httpx
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for attempt in range(2):
            try:
                resp = await client.get(image_url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                ext = {"image/png": "png", "image/webp": "webp"}.get(
                    content_type.split(";")[0].strip(), "jpg")
                return resp.content, ext
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_error = e
                if attempt == 0:
                    await asyncio.sleep(2)
    raise RuntimeError(f"Image download failed: {last_error}")


async def _novita_list_models(cfg: dict, query: str, cursor: str, limit: int,
                              types: str = "checkpoint") -> dict:
    """Search Novita's model catalog (thousands of Civitai-mirrored models)."""
    import httpx
    params = {
        "filter.types": types,
        "pagination.limit": max(1, min(100, limit)),
    }
    if query:
        params["filter.query"] = query
    if cursor:
        params["pagination.cursor"] = cursor

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        resp = await client.get(f"{NOVITA_BASE}/v3/model",
                                headers=_novita_headers(cfg), params=params)
        if resp.status_code in (401, 403):
            raise RuntimeError("Novita rejected the model search: invalid API key")
        if resp.status_code != 200:
            raise RuntimeError(f"Novita model search failed ({resp.status_code}): "
                               f"{_novita_error_detail(resp)}")
        return resp.json()


# --------------------------------------------------------------------------
# Civitai client (LoRA browsing) + Novita availability matching
# --------------------------------------------------------------------------

def _civitai_headers(cfg: dict) -> dict:
    headers = {"accept": "application/json"}
    key = str(cfg.get("civitai_api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _flatten_civitai_model(model: dict) -> dict | None:
    """Reduce a Civitai /models hit to the library-entry shape (latest version,
    primary file). Returns None for hits without a downloadable version."""
    versions = model.get("modelVersions") or []
    version = versions[0] if versions else {}
    if not version.get("id"):
        return None
    files = version.get("files") or []
    file = next((f for f in files if f.get("primary")), files[0] if files else {})
    thumb = next(
        (i.get("url") for i in (version.get("images") or [])
         if i.get("url") and i.get("type") != "video"),
        "")
    stats = model.get("stats") or {}
    return {
        "id": str(version["id"]),
        "model_id": model.get("id"),
        "name": str(model.get("name") or ""),
        "version_name": str(version.get("name") or ""),
        "creator": str((model.get("creator") or {}).get("username") or ""),
        "type": str(model.get("type") or "LORA"),
        "base_model": str(version.get("baseModel") or ""),
        "sha256": str((file.get("hashes") or {}).get("SHA256") or "").lower(),
        "download_url": str(file.get("downloadUrl") or version.get("downloadUrl") or ""),
        "size_kb": file.get("sizeKB"),
        "trained_words": [str(w) for w in (version.get("trainedWords") or [])],
        "thumb_url": str(thumb or ""),
        "civitai_url": f"https://civitai.com/models/{model.get('id')}",
        "nsfw": bool(model.get("nsfw")),
        "stats": {
            "downloads": int(stats.get("downloadCount") or 0),
            "likes": int(stats.get("thumbsUpCount") or 0),
        },
    }


async def _civitai_search_loras(cfg: dict, *, query: str, base_model: str,
                                lora_type: str, sort: str, nsfw_mode: str,
                                cursor: str, limit: int) -> dict:
    import httpx
    if nsfw_mode not in CIVITAI_NSFW_MODES:
        nsfw_mode = "off"
    params = [
        ("types", lora_type if lora_type in CIVITAI_LORA_TYPES else "LORA"),
        ("sort", sort if sort in CIVITAI_SORTS else CIVITAI_SORTS[0]),
        ("limit", str(max(1, min(100, limit)))),
        ("nsfw", "false" if nsfw_mode == "off" else "true"),
    ]
    if query:
        params.append(("query", query))
    if base_model:
        params.append(("baseModels", base_model))
    if cursor:
        params.append(("cursor", cursor))

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        resp = await client.get(f"{CIVITAI_BASE}/models",
                                headers=_civitai_headers(cfg), params=params)
        if resp.status_code == 401:
            raise RuntimeError("Civitai rejected the request: invalid API key")
        if resp.status_code != 200:
            raise RuntimeError(f"Civitai search failed ({resp.status_code}): "
                               f"{resp.text[:300]}")
        body = resp.json()

    items = [
        flat for flat in (_flatten_civitai_model(m) for m in (body.get("items") or []))
        if flat is not None
    ]
    if nsfw_mode == "only":
        items = [i for i in items if i["nsfw"]]
    next_cursor = str((body.get("metadata") or {}).get("nextCursor") or "")
    return {"items": items, "next_cursor": next_cursor}


async def _novita_match_lora(cfg: dict, entry: dict) -> dict | None:
    """Find the saved Civitai LoRA in Novita's Civitai-mirrored catalog by
    SHA256. Novita sd_names usually embed the Civitai version id, so that is
    the first search key; the model name is the fallback."""
    sha = str(entry.get("sha256") or "").lower()
    if not sha:
        return None
    queries = [str(entry.get("id") or ""), str(entry.get("name") or "")[:60]]
    for query in filter(None, queries):
        body = await _novita_list_models(cfg, query, "", 100, types="lora")
        for model in body.get("models") or []:
            if str(model.get("hash_sha256") or "").lower() != sha:
                continue
            sd_name = model.get("sd_name_in_api") or model.get("sd_name")
            if sd_name and model.get("status") == 1:
                return {"sd_name_in_api": sd_name}
    return None


def _normalize_lora_entry(item: dict) -> dict:
    """Library entry from a browser selection, with activation defaults."""
    entry = {k: item.get(k) for k in (
        "id", "model_id", "name", "version_name", "creator", "type", "base_model",
        "sha256", "download_url", "size_kb", "trained_words", "thumb_url",
        "civitai_url", "nsfw", "stats")}
    entry["id"] = str(entry.get("id") or "")
    entry["trained_words"] = [str(w) for w in (entry.get("trained_words") or [])][:20]
    entry.update({
        "saved_at": _now(),
        "active": False,
        "strength": 0.7,
        "sd_name_override": "",
        "novita": None,
        "novita_checked_at": None,
    })
    return entry


# --------------------------------------------------------------------------
# Background pipeline
# --------------------------------------------------------------------------

def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "story"))[:40].strip("-") or "story"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hook_sdk(sdk):
    """Router-initiated runs have no hook sdk; fall back to the engine's."""
    if sdk is not None:
        return sdk
    engine = _services.get("engine")
    return getattr(engine, "sdk", None)


def _spawn_generation(*, save_id: str, turn: int, narration: str, history: str,
                      sdk, trigger: str = "auto", prompt_override: str | None = None) -> str | None:
    """Create a pending record and fire the pipeline task. Returns the record id,
    or None when a generation is already running (caller decides what that means)."""
    lock = _get_gen_lock()
    if lock.locked():
        return None

    cfg = _load_config()
    record_id = f"{_slug(save_id)}_{int(turn or 0)}_{uuid.uuid4().hex[:8]}"
    record = {
        "id": record_id,
        "save_id": save_id,
        "turn": int(turn or 0),
        "status": "pending",
        "trigger": trigger,
        "filename": None,
        "image_prompt": prompt_override or "",
        "narration_excerpt": (narration or "")[:200],
        "model_name": cfg.get("model_name", ""),
        "loras": _applied_lora_names(cfg),
        "width": cfg.get("width"),
        "height": cfg.get("height"),
        "error": None,
        "created_at": _now(),
        "completed_at": None,
        "duration_s": None,
    }

    async def _run():
        await _append_record(record)
        await _generation_pipeline(record_id, cfg, narration, history,
                                   _hook_sdk(sdk), prompt_override)

    task = asyncio.get_running_loop().create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return record_id


async def _generation_pipeline(record_id: str, cfg: dict, narration: str,
                               history: str, sdk, prompt_override: str | None) -> None:
    started = time.monotonic()
    lock = _get_gen_lock()
    try:
        async with lock:
            if prompt_override:
                image_prompt = _clean_image_prompt(prompt_override)
            else:
                if sdk is None:
                    raise RuntimeError("no LLM available to write the image prompt")
                await _patch_record(record_id, status="prompting")
                image_prompt = await _write_image_prompt(cfg, narration, history, sdk)

            await _patch_record(record_id, status="generating", image_prompt=image_prompt)
            task_id = await _novita_submit(cfg, image_prompt)
            image_url = await _novita_poll(cfg, task_id)
            data, ext = await _download(image_url)

            filename = f"{record_id}.{ext}"
            path = _data_dir() / "images" / filename
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)

            await _patch_record(
                record_id, status="done", filename=filename,
                completed_at=_now(), duration_s=round(time.monotonic() - started, 1))
            print(f"[Image Gen] {record_id} done in {round(time.monotonic() - started, 1)}s")
    except Exception as e:
        # A failed illustration must never surface as an exception anywhere —
        # it only marks its own record.
        print(f"[Image Gen] {record_id} failed: {e}")
        try:
            await _patch_record(
                record_id, status="error", error=str(e)[:500],
                completed_at=_now(), duration_s=round(time.monotonic() - started, 1))
        except Exception as patch_err:
            print(f"[Image Gen] Could not record failure for {record_id}: {patch_err}")


# --------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------

def _own_data(state: dict) -> dict:
    return state.get("module_data", {}).get(MODULE_ID, {})


async def on_gather_context(state: dict, sdk) -> dict:
    if not _own_data(state):
        return {"module_data": {MODULE_ID: {"turns_since_image": 0}}}
    return {}


async def on_librarian(state: dict, sdk) -> dict | None:
    cfg = _load_config()
    if not cfg.get("enabled") or not cfg.get("api_key") or not cfg.get("model_name"):
        return None
    history = state.get("history", [])
    if not history:
        return None

    count = int(_own_data(state).get("turns_since_image", 0) or 0) + 1
    interval = max(1, int(cfg.get("interval", 3) or 3))

    if count >= interval:
        record_id = _spawn_generation(
            save_id=state.get("active_save_id") or "unknown",
            turn=state.get("turn", 0),
            narration=str(history[-1]),
            history="\n".join(str(h) for h in history[-6:-1]),
            sdk=sdk,
        )
        if record_id:
            print(f"[Image Gen] Turn {state.get('turn')}: auto illustration started ({record_id}).")
            return {"module_data": {MODULE_ID: {"turns_since_image": 0, "last_trigger": record_id}}}
        # Busy: keep the ripe counter so the next turn retries.

    return {"module_data": {MODULE_ID: {"turns_since_image": count}}}


def _latest_narration(state: dict) -> str:
    for message in reversed(state.get("chat_messages", [])):
        if message.get("role") in ("ai", "assistant") and str(message.get("content", "")).strip():
            return str(message["content"])
    history = state.get("history", [])
    return str(history[-1]) if history else ""


async def on_command_image(args: list[str], state: dict, sdk) -> dict:
    cfg = _load_config()
    if not cfg.get("api_key"):
        return {"message": "[Image Gen] No API key configured. Add one in Image Studio (main menu).",
                "signal": "end_turn"}
    if not cfg.get("model_name"):
        return {"message": "[Image Gen] No model selected. Pick one in Image Studio (main menu).",
                "signal": "end_turn"}

    narration = _latest_narration(state)
    if not narration:
        return {"message": "[Image Gen] Nothing to illustrate yet — play a turn first.",
                "signal": "end_turn"}

    history = state.get("history", [])
    record_id = _spawn_generation(
        save_id=state.get("active_save_id") or "unknown",
        turn=state.get("turn", 0),
        narration=narration,
        history="\n".join(str(h) for h in history[-6:-1]),
        sdk=sdk,
        trigger="manual",
    )
    if record_id is None:
        return {"message": "[Image Gen] An image is already being generated — give it a moment.",
                "signal": "end_turn"}
    return {
        "message": "[Image Gen] Illustrating the current scene — the image will appear under the latest message shortly.",
        "signal": "end_turn",
        "module_data": {MODULE_ID: {"last_trigger": record_id}},
    }


# --------------------------------------------------------------------------
# Router (mounted at /api/modules/wb_image_gen)
# --------------------------------------------------------------------------

_FILENAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,120}\.(jpg|jpeg|png|webp)")


def get_router():
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse
    from pydantic import BaseModel

    router = APIRouter()

    class ConfigUpdate(BaseModel):
        enabled: bool | None = None
        api_key: str | None = None
        model_name: str | None = None
        model_base: str | None = None
        width: int | None = None
        height: int | None = None
        steps: int | None = None
        guidance_scale: float | None = None
        sampler_name: str | None = None
        negative_prompt: str | None = None
        interval: int | None = None
        prompt_model_preference: str | None = None
        prompt_template: str | None = None
        prompt_template_tags: str | None = None
        pony_quality_tags: str | None = None
        style_suffix: str | None = None
        civitai_api_key: str | None = None
        civitai_nsfw: str | None = None

    class GenerateRequest(BaseModel):
        prompt_override: str | None = None
        save_id: str | None = None
        retry_record_id: str | None = None

    class LoraSave(BaseModel):
        id: str
        model_id: int | None = None
        name: str
        version_name: str = ""
        creator: str = ""
        type: str = "LORA"
        base_model: str = ""
        sha256: str = ""
        download_url: str = ""
        size_kb: float | None = None
        trained_words: list[str] = []
        thumb_url: str = ""
        civitai_url: str = ""
        nsfw: bool = False
        stats: dict = {}

    class LoraPatch(BaseModel):
        active: bool | None = None
        strength: float | None = None
        sd_name_override: str | None = None

    def _public_config(cfg: dict) -> dict:
        out = dict(cfg)
        out["api_key"] = _mask_key(cfg.get("api_key", ""))
        out["has_key"] = bool(cfg.get("api_key"))
        out["civitai_api_key"] = _mask_key(cfg.get("civitai_api_key", ""))
        out["has_civitai_key"] = bool(cfg.get("civitai_api_key"))
        out["samplers"] = SAMPLERS
        out["default_prompt_template"] = DEFAULT_PROMPT_TEMPLATE
        out["default_prompt_template_tags"] = DEFAULT_PROMPT_TEMPLATE_TAGS
        out["default_pony_quality_tags"] = DEFAULT_PONY_QUALITY_TAGS
        out["prompt_style"] = _prompt_style(cfg)
        out["civitai_sorts"] = CIVITAI_SORTS
        out["civitai_lora_types"] = CIVITAI_LORA_TYPES
        out["civitai_nsfw_modes"] = CIVITAI_NSFW_MODES
        out["civitai_base_models"] = CIVITAI_BASE_MODELS
        out["flux2_model_name"] = FLUX2_MODEL_NAME
        out["checkpoint_family"] = _checkpoint_family(cfg)
        return out

    @router.get("/config")
    async def get_config():
        return _public_config(_load_config())

    @router.put("/config")
    async def put_config(update: ConfigUpdate):
        cfg = _load_config()
        incoming = update.model_dump(exclude_none=True)

        key = incoming.pop("api_key", None)
        if key is not None and not key.startswith(KEY_MASK_PREFIX):
            cfg["api_key"] = key.strip()
        civitai_key = incoming.pop("civitai_api_key", None)
        if civitai_key is not None and not civitai_key.startswith(KEY_MASK_PREFIX):
            cfg["civitai_api_key"] = civitai_key.strip()

        if "sampler_name" in incoming and incoming["sampler_name"] not in SAMPLERS:
            raise HTTPException(status_code=400, detail=f"Unknown sampler. Allowed: {SAMPLERS}")
        for side in ("width", "height"):
            if side in incoming:
                incoming[side] = max(128, min(2048, (int(incoming[side]) // 8) * 8))
        if "steps" in incoming:
            incoming["steps"] = max(1, min(100, int(incoming["steps"])))
        if "guidance_scale" in incoming:
            incoming["guidance_scale"] = max(1.0, min(30.0, float(incoming["guidance_scale"])))
        if "interval" in incoming:
            incoming["interval"] = max(1, min(50, int(incoming["interval"])))
        if ("prompt_model_preference" in incoming
                and incoming["prompt_model_preference"] not in ("fastest", "balanced", "smartest")):
            raise HTTPException(status_code=400, detail="prompt_model_preference must be a model slot")
        if "civitai_nsfw" in incoming and incoming["civitai_nsfw"] not in CIVITAI_NSFW_MODES:
            raise HTTPException(status_code=400,
                                detail=f"civitai_nsfw must be one of {CIVITAI_NSFW_MODES}")

        cfg.update(incoming)
        _save_config(cfg)
        return _public_config(cfg)

    @router.get("/images")
    async def list_images(save_id: str | None = None, limit: int = 200):
        records = _read_index()
        if save_id:
            records = [r for r in records if r.get("save_id") == save_id]
        records = records[-max(1, min(500, limit)):]
        records.reverse()
        pending = sum(1 for r in records if r.get("status") in ("pending", "prompting", "generating"))
        return {"records": records, "pending": pending}

    @router.get("/images/file/{filename}")
    async def get_image(filename: str):
        if not _FILENAME_RE.fullmatch(filename):
            raise HTTPException(status_code=404, detail="Not found")
        images_dir = (_data_dir() / "images").resolve()
        path = (images_dir / filename).resolve()
        if images_dir not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        # Filenames are immutable (uuid-suffixed), so long caching is safe.
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @router.get("/models")
    async def search_models(query: str = "", cursor: str = "", limit: int = 48):
        cfg = _load_config()
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No API key configured")
        try:
            body = await _novita_list_models(cfg, query.strip(), cursor.strip(), limit)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        models = [
            {
                "sd_name": m.get("sd_name_in_api") or m.get("sd_name"),
                "name": m.get("name"),
                "is_sdxl": bool(m.get("is_sdxl")),
                "base_model": m.get("base_model"),
                "cover_url": m.get("cover_url"),
            }
            for m in (body.get("models") or [])
            if m.get("status") == 1 and (m.get("sd_name_in_api") or m.get("sd_name"))
        ]
        next_cursor = (body.get("pagination") or {}).get("next_cursor") or ""
        # First-party FLUX.2 rides its own endpoint, so it is not in /v3/model;
        # pin it to the top of matching first pages.
        if not cursor.strip() and (not query.strip() or "flux" in query.lower()):
            models.insert(0, {
                "sd_name": FLUX2_MODEL_NAME,
                "name": "FLUX.2 [dev] — Novita first-party (LoRAs via Civitai link)",
                "is_sdxl": False,
                "base_model": "Flux.2",
                "cover_url": None,
            })
        return {"models": models, "next_cursor": next_cursor}

    @router.get("/civitai/loras")
    async def civitai_loras(query: str = "", base_model: str = "", lora_type: str = "LORA",
                            sort: str = "Most Downloaded", nsfw: str = "off",
                            cursor: str = "", limit: int = 24):
        cfg = _load_config()
        if nsfw not in CIVITAI_NSFW_MODES:
            nsfw = "off"
        if nsfw != "off" and not cfg.get("civitai_api_key"):
            raise HTTPException(status_code=400,
                                detail="NSFW browsing needs a Civitai API key")
        try:
            return await _civitai_search_loras(
                cfg, query=query.strip(), base_model=base_model.strip(),
                lora_type=lora_type, sort=sort, nsfw_mode=nsfw,
                cursor=cursor.strip(), limit=limit)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))

    def _find_lora(cfg: dict, lora_id: str) -> dict:
        entry = next((e for e in cfg.get("lora_library") or []
                      if isinstance(e, dict) and e.get("id") == lora_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="LoRA not in library")
        return entry

    @router.post("/loras")
    async def save_lora(item: LoraSave):
        cfg = _load_config()
        library = cfg.get("lora_library") or []
        if any(isinstance(e, dict) and e.get("id") == item.id for e in library):
            raise HTTPException(status_code=409, detail="Already in library")
        if len(library) >= LORA_LIBRARY_MAX:
            raise HTTPException(status_code=400,
                                detail=f"Library is full ({LORA_LIBRARY_MAX} LoRAs)")

        entry = _normalize_lora_entry(item.model_dump())
        # Flux LoRAs go to Novita as download links; only SD ones need to exist
        # in Novita's mirrored catalog.
        if _base_family(entry.get("base_model")) != "flux" and cfg.get("api_key"):
            try:
                entry["novita"] = await _novita_match_lora(cfg, entry)
            except RuntimeError as e:
                print(f"[Image Gen] Novita match failed for {entry['id']}: {e}")
            else:
                entry["novita_checked_at"] = _now()

        cfg = _load_config()  # re-load: the match awaited, config may have moved
        library = cfg.get("lora_library") or []
        if not any(isinstance(e, dict) and e.get("id") == entry["id"] for e in library):
            library.append(entry)
        cfg["lora_library"] = library
        _save_config(cfg)
        return {"entry": entry, "lora_library": library}

    @router.post("/loras/{lora_id}/match")
    async def rematch_lora(lora_id: str):
        cfg = _load_config()
        entry = _find_lora(cfg, lora_id)
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No Novita API key configured")
        try:
            match = await _novita_match_lora(cfg, entry)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        cfg = _load_config()
        entry = _find_lora(cfg, lora_id)
        entry["novita"] = match
        entry["novita_checked_at"] = _now()
        _save_config(cfg)
        return {"entry": entry, "lora_library": cfg["lora_library"]}

    @router.patch("/loras/{lora_id}")
    async def patch_lora(lora_id: str, patch: LoraPatch):
        cfg = _load_config()
        entry = _find_lora(cfg, lora_id)
        if patch.active is not None:
            entry["active"] = bool(patch.active)
        if patch.strength is not None:
            entry["strength"] = max(0.0, min(1.0, float(patch.strength)))
        if patch.sd_name_override is not None:
            entry["sd_name_override"] = patch.sd_name_override.strip()
        _save_config(cfg)
        return {"entry": entry, "lora_library": cfg["lora_library"]}

    @router.delete("/loras/{lora_id}")
    async def delete_lora(lora_id: str):
        cfg = _load_config()
        _find_lora(cfg, lora_id)
        cfg["lora_library"] = [e for e in cfg["lora_library"]
                               if not (isinstance(e, dict) and e.get("id") == lora_id)]
        _save_config(cfg)
        return {"lora_library": cfg["lora_library"]}

    @router.post("/generate")
    async def generate(req: GenerateRequest):
        cfg = _load_config()
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No API key configured")
        if not cfg.get("model_name"):
            raise HTTPException(status_code=400, detail="No model selected — search and pick one first")

        save_id = req.save_id
        turn = 0
        narration = ""
        history_text = ""
        prompt_override = (req.prompt_override or "").strip() or None

        if req.retry_record_id:
            record = next((r for r in _read_index() if r.get("id") == req.retry_record_id), None)
            if record is None:
                raise HTTPException(status_code=404, detail="Record not found")
            save_id = record.get("save_id")
            turn = record.get("turn", 0)
            narration = record.get("narration_excerpt", "")
            prompt_override = prompt_override or record.get("image_prompt") or None
            if not prompt_override and not narration:
                raise HTTPException(status_code=400, detail="Record has nothing to retry from")
        elif prompt_override is None:
            session_manager = _services.get("session_manager")
            state = getattr(session_manager, "state", None) or {}
            history = state.get("history", [])
            if not history:
                raise HTTPException(status_code=400, detail="No story to illustrate — provide a prompt")
            narration = str(history[-1])
            history_text = "\n".join(str(h) for h in history[-6:-1])
            turn = state.get("turn", 0)
            save_id = save_id or getattr(session_manager, "active_save_id", None) or "unknown"
        else:
            save_id = save_id or "__studio__"

        record_id = _spawn_generation(
            save_id=save_id, turn=turn, narration=narration, history=history_text,
            sdk=None, trigger="studio" if save_id == "__studio__" else "manual",
            prompt_override=prompt_override,
        )
        if record_id is None:
            raise HTTPException(status_code=409, detail="A generation is already running")
        return {"record_id": record_id}

    @router.delete("/images/{record_id}")
    async def delete_image(record_id: str):
        async with _get_index_lock():
            records = _read_index()
            record = next((r for r in records if r.get("id") == record_id), None)
            if record is None:
                raise HTTPException(status_code=404, detail="Record not found")
            records = [r for r in records if r.get("id") != record_id]
            _write_index(records)
        filename = record.get("filename")
        if filename and _FILENAME_RE.fullmatch(filename):
            path = _data_dir() / "images" / filename
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                print(f"[Image Gen] Could not delete {filename}: {e}")
        return {"ok": True}

    return router
