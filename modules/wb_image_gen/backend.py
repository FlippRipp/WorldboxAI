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
    return payload


def _novita_error_detail(resp) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body.get("reason") or body)[:300]
    except Exception:
        return resp.text[:300]


async def _novita_submit(cfg: dict, image_prompt: str) -> str:
    """Submit an async txt2img task; return the task id."""
    import httpx
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


async def _novita_list_models(cfg: dict, query: str, cursor: str, limit: int) -> dict:
    """Search Novita's checkpoint catalog (thousands of models)."""
    import httpx
    params = {
        "filter.types": "checkpoint",
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

    class GenerateRequest(BaseModel):
        prompt_override: str | None = None
        save_id: str | None = None
        retry_record_id: str | None = None

    def _public_config(cfg: dict) -> dict:
        out = dict(cfg)
        out["api_key"] = _mask_key(cfg.get("api_key", ""))
        out["has_key"] = bool(cfg.get("api_key"))
        out["samplers"] = SAMPLERS
        out["default_prompt_template"] = DEFAULT_PROMPT_TEMPLATE
        out["default_prompt_template_tags"] = DEFAULT_PROMPT_TEMPLATE_TAGS
        out["default_pony_quality_tags"] = DEFAULT_PONY_QUALITY_TAGS
        out["prompt_style"] = _prompt_style(cfg)
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
        return {"models": models, "next_cursor": next_cursor}

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
