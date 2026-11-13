"""Text to Speech (wb_tts) — Faster Qwen3-TTS integration for WorldBox.

Connects WorldBox to a local or remote Faster Qwen3-TTS provider server
(`modules/wb_tts/server.py`, launched via `tts_server.bat` / `tts_server.sh`)
and proxies streaming 24 kHz 16-bit mono PCM to desktop and mobile browsers,
with optional sentence-level vocal emotion routing via Jev.

Why proxy through WorldBox's backend?
- Mobile browsers playing over LAN, Tailscale, or HTTPS only ever connect to
  the single WorldBox port — no CORS preflights, mixed-content blocks, or extra
  firewall rules on phones.
- Client abort (`AbortController.abort()` on stop, swipe, skip, or regenerate)
  immediately closes the upstream `httpx` stream, which trips
  `until_disconnected` on the GPU server and frees CUDA right away.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from backend.engine.jev import Uncertain, choice

MODULE_ID = "wb_tts"
DEFAULT_SERVER_URL = "http://127.0.0.1:7864"
ACCEPTED_SERVICES = {"worldbox-faster-qwen-tts", "pngtuber-faster-qwen-tts"}
KEY_MASK_PREFIX = "****"
SAMPLE_RATE = 24000
MIN_EMOTION_PROB = 0.25
RECENT_SCENE_MESSAGES = 6

_HIDDEN_BLOCK_RE = re.compile(
    r"<!--[\s\S]*?(?:-->|$)|^ {0,3}<(details|internal_states)\b[^>]*>[\s\S]*?(?:</\1\s*>|$)",
    re.IGNORECASE | re.MULTILINE,
)
_TAG_RE = re.compile(r"</?\s*(?:color\s*:\s*)?[a-z0-9_:-]+(?:\s+[^>]*)?>", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_DECOR_RE = re.compile(r"[*_~`#>]+")
_SPEAKABLE_RE = re.compile(r"[A-Za-z0-9\u00C0-\u024F\u0400-\u04FF\u3040-\u30FF\u4E00-\u9FFF]")

_EMOTIONS_MANIFEST = Path(__file__).resolve().parent / "voices" / "emotions" / "manifest.json"
_DEFAULT_EMOTION_DESCRIPTIONS: dict[str, str] = {
    "neutral": "Calm, balanced, matter-of-fact narration or ordinary conversation without strong emotion.",
    "warm": "Gentle affection, comfort, kindness, tenderness, or welcoming warmth.",
    "awed": "Breathless wonder, amazement, reverence, or spellbound discovery.",
    "grim": "Dark, solemn, ominous, foreboding, or grave weight and dread.",
    "somber": "Sorrow, grief, heartbreak, mourning, regret, or quiet sadness.",
    "hushed": "Secretive whisper, stealth, hiding, or speaking very quietly so others do not overhear.",
    "tense": "Urgent alarm, immediate danger, combat pressure, panic-action, or high-stakes haste.",
    "angry": "Fierce anger, fury, outrage, sharp accusation, or heated confrontation.",
    "cold": "Icy, emotionless menace, detached hostility, disdain, or dangerous calm.",
    "fearful": "Trembling fear, terror, dread, pleading, or frightened hesitation.",
    "weary": "Deep exhaustion, physical fatigue, burnout, injury strain, or heavy tired sighs.",
    "excited": "Bright joy, enthusiasm, triumph, delight, or high-spirited energy.",
    "amused": "Lighthearted laughter, chuckling, humor, cheerful mirth, or finding something funny.",
    "teasing": "Playful banter, smug smirk, witty provocation, sly drawl, or cheeky flirting.",
    "flustered": "Embarrassed, shy, caught off guard, blushing, stammering, or flabbergasted.",
    "horny": "Erotic arousal, breathless desire, sensual intimacy, seduction, or heavy lustful sighs.",
}

_services: dict = {}
_data_dir: Path | None = None
_config_cache: dict | None = None
_emotion_catalog_cache: dict[str, dict] | None = None
_health_cache: dict = {
    "checked_at": 0.0,
    "reachable": False,
    "ready": False,
    "service": "",
    "model": "",
    "sample_rate": SAMPLE_RATE,
    "voices": [],
    "emotions": [],
    "resolved_voice": "default",
    "error": "",
}


def _default_config() -> dict:
    return {
        "enabled": True,
        "emotion_enabled": True,
        "server_url": DEFAULT_SERVER_URL,
        "token": "",
        "temperature": 0.9,
        "chunk_size": 8,
    }


def load_emotion_catalog() -> dict[str, dict]:
    """Return `{emotion_id: {id, label, description}}` from `voices/emotions/manifest.json`."""
    global _emotion_catalog_cache
    if _emotion_catalog_cache is not None:
        return _emotion_catalog_cache

    catalog: dict[str, dict] = {}
    if _EMOTIONS_MANIFEST.is_file():
        try:
            raw = json.loads(_EMOTIONS_MANIFEST.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for emo_id, entry in raw.items():
                    if not isinstance(emo_id, str) or not isinstance(entry, dict):
                        continue
                    desc = str(
                        entry.get("description")
                        or _DEFAULT_EMOTION_DESCRIPTIONS.get(emo_id)
                        or emo_id
                    ).strip()
                    label = str(entry.get("label") or emo_id.replace("_", " ").title()).strip()
                    catalog[emo_id] = {"id": emo_id, "label": label, "description": desc}
        except (OSError, ValueError):
            catalog = {}

    if not catalog:
        catalog = {
            emo_id: {
                "id": emo_id,
                "label": emo_id.replace("_", " ").title(),
                "description": desc,
            }
            for emo_id, desc in _DEFAULT_EMOTION_DESCRIPTIONS.items()
        }

    _emotion_catalog_cache = catalog
    return catalog


def normalize_server_url(raw: str) -> str:
    val = str(raw or "").strip().rstrip("/")
    if not val:
        return DEFAULT_SERVER_URL
    if "://" not in val:
        val = f"http://{val}"
    parsed = urlparse(val)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return DEFAULT_SERVER_URL
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def strip_hidden_blocks(raw: str) -> str:
    """Remove hidden state/details blocks while preserving inline styling cues for Jev."""
    if not raw:
        return ""
    text = _HIDDEN_BLOCK_RE.sub(" ", str(raw))
    return re.sub(r"\s+", " ", text).strip()


def clean_spoken_text(raw: str) -> str:
    """Strip hidden state blocks, <color> tags, and markdown into natural speech text."""
    if not raw:
        return ""
    text = _HIDDEN_BLOCK_RE.sub(" ", str(raw))
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text)
    text = _MD_DECOR_RE.sub("", text)
    # Normalize curly quotes and collapse whitespace
    text = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    text = re.sub(r"\s+", " ", text).strip()
    if not _SPEAKABLE_RE.search(text):
        return ""
    return text


def _jev_status() -> dict:
    engine = _services.get("engine")
    llm = getattr(engine, "llm", None)
    decisions = getattr(llm, "decisions", None)
    if decisions is None:
        return {"available": False, "enabled": False, "tts_enabled": False, "has_credentials": False}
    try:
        pub = decisions.public_config()
        has_creds = bool(decisions.has_credentials())
    except Exception:
        return {"available": False, "enabled": False, "tts_enabled": False, "has_credentials": False}
    return {
        "available": bool(pub.get("enabled") and pub.get("tts_enabled", True) and has_creds),
        "enabled": bool(pub.get("enabled")),
        "tts_enabled": bool(pub.get("tts_enabled", True)),
        "has_credentials": has_creds,
    }


def _collect_recent_scene_messages() -> list[dict[str, str]]:
    """Gather recent scene messages from the active session without truncating message text."""
    session_manager = _services.get("session_manager")
    state = getattr(session_manager, "state", None)
    if not isinstance(state, dict):
        return []
    raw_messages = state.get("chat_messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        return []
    selected = []
    for msg in raw_messages[-RECENT_SCENE_MESSAGES:]:
        if not isinstance(msg, dict) or msg.get("error"):
            continue
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant", "system"):
            continue
        content = strip_hidden_blocks(str(msg.get("content") or ""))
        if content:
            selected.append({"role": role, "content": content})
    return selected


async def classify_sentence_emotion(
    raw_text: str,
    spoken_text: str,
    preceding_sentences: list[str] | None = None,
) -> str:
    """Use Jev to pick one of the 16 vocal emotions for `spoken_text`, falling back to `neutral`."""
    engine = _services.get("engine")
    llm = getattr(engine, "llm", None)
    decisions = getattr(llm, "decisions", None)
    if decisions is None:
        return "neutral"

    catalog = load_emotion_catalog()
    inspector = getattr(llm, "inspector", None)

    turn_prev = [
        strip_hidden_blocks(str(s))
        for s in (preceding_sentences or [])
        if isinstance(s, str) and strip_hidden_blocks(s)
    ]
    state = {
        "recent_messages": _collect_recent_scene_messages(),
        "turn_preceding_sentences": turn_prev,
        "target_sentence": strip_hidden_blocks(raw_text) or spoken_text,
        "spoken_text": spoken_text,
    }
    questions = {
        "emotion": choice(
            "Choose the single vocal delivery emotion that best fits how `target_sentence` should be spoken aloud given `recent_messages` and `turn_preceding_sentences`. Choose `neutral` for ordinary narration or calm matter-of-fact statements without clear emotional charge.",
            {emo_id: spec["description"] for emo_id, spec in catalog.items()},
        )
    }

    try:
        decision = await decisions.decide(
            state,
            questions,
            module=MODULE_ID,
            step="tts:emotion",
            inspector=inspector,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return "neutral"

    try:
        selected, probs, _conf = decision.choice_distribution("emotion")
        if selected not in catalog or probs.get(selected, 0.0) < MIN_EMOTION_PROB:
            raise Uncertain(decision.reason or "low emotion probability")
        await decision.finish("accepted")
        return selected
    except asyncio.CancelledError:
        await decision.finish("cancelled")
        raise
    except Uncertain as exc:
        await decision.finish("fallback", str(exc))
        return "neutral"
    except Exception as exc:
        await decision.finish("fallback", type(exc).__name__)
        return "neutral"


def _config_path() -> Path:
    base = _data_dir
    if base is None:
        base = Path(__file__).resolve().parent.parent.parent / "data" / MODULE_ID
    return base / "config.json"


def _load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return dict(_config_cache)
    cfg = _default_config()
    path = _config_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(_sanitize_config(loaded, existing=cfg))
        except (OSError, ValueError, TypeError):
            pass
    _config_cache = cfg
    return dict(cfg)


def _sanitize_config(updates: dict, existing: dict | None = None) -> dict:
    cur = existing if existing is not None else _load_config()
    out = dict(cur)
    if "enabled" in updates:
        out["enabled"] = bool(updates["enabled"])
    if "emotion_enabled" in updates:
        out["emotion_enabled"] = bool(updates["emotion_enabled"])
    if "server_url" in updates:
        out["server_url"] = normalize_server_url(updates["server_url"])
    if "token" in updates:
        tok = str(updates.get("token") or "").strip()
        if not tok.startswith(KEY_MASK_PREFIX):
            out["token"] = tok
    if "temperature" in updates:
        try:
            out["temperature"] = round(min(1.5, max(0.1, float(updates["temperature"]))), 2)
        except (TypeError, ValueError):
            pass
    if "chunk_size" in updates:
        try:
            out["chunk_size"] = min(24, max(2, int(updates["chunk_size"])))
        except (TypeError, ValueError):
            pass
    return out


def _save_config(updates: dict) -> dict:
    global _config_cache
    prev = _load_config()
    merged = _sanitize_config(updates, existing=prev)
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _config_cache = merged
    if merged["server_url"] != prev["server_url"] or merged["token"] != prev["token"]:
        _health_cache["checked_at"] = 0.0
    return dict(merged)


def _public_config(cfg: dict | None = None) -> dict:
    c = cfg if cfg is not None else _load_config()
    pub = dict(c)
    tok = pub.get("token") or ""
    pub["has_token"] = bool(tok)
    pub["token"] = (KEY_MASK_PREFIX + tok[-4:]) if len(tok) > 4 else (KEY_MASK_PREFIX if tok else "")
    return pub


def _auth_headers(cfg: dict) -> dict[str, str]:
    tok = (cfg.get("token") or "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _resolve_voice(voices: list[str], default_voice: str = "") -> str:
    if default_voice and default_voice in voices:
        return default_voice
    if "default" in voices:
        return "default"
    if "current_cleaned" in voices:
        return "current_cleaned"
    return voices[0] if voices else "default"


async def probe_server(force: bool = False) -> dict:
    """Check `/health` on the configured Faster Qwen3-TTS server."""
    now = time.monotonic()
    if not force and _health_cache["checked_at"] and (now - _health_cache["checked_at"] < 4.0):
        return dict(_health_cache)

    cfg = _load_config()
    url = f"{cfg['server_url']}/health"
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(url, headers=_auth_headers(cfg))
        if resp.status_code == 401:
            _health_cache.update({
                "checked_at": now,
                "reachable": True,
                "ready": False,
                "error": "Unauthorized (check bearer token)",
            })
            return dict(_health_cache)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        service = str(payload.get("service") or "")
        voices = [str(v) for v in (payload.get("voices") or []) if isinstance(v, str)]
        emotions = [str(e) for e in (payload.get("emotions") or []) if isinstance(e, str)]
        ready = bool(payload.get("ready")) and (not service or service in ACCEPTED_SERVICES)
        resolved_voice = _resolve_voice(voices, str(payload.get("default_voice") or ""))
        _health_cache.update({
            "checked_at": now,
            "reachable": True,
            "ready": ready,
            "service": service,
            "model": str(payload.get("model") or ""),
            "sample_rate": int(payload.get("sample_rate") or SAMPLE_RATE),
            "voices": voices,
            "emotions": emotions,
            "resolved_voice": resolved_voice,
            "error": "" if ready else (f"Unexpected service: {service}" if service else "Server warming up…"),
        })
    except Exception as exc:
        _health_cache.update({
            "checked_at": now,
            "reachable": False,
            "ready": False,
            "error": str(exc),
        })
    return dict(_health_cache)


def set_services(services: dict) -> None:
    global _services, _data_dir, _config_cache
    _services = services or {}
    base = _services.get("global_data_dir") or _services.get("data_dir")
    _data_dir = (Path(base) / MODULE_ID) if base else None
    _config_cache = None
    _health_cache["checked_at"] = 0.0


async def on_command_tts(args: list, state: dict, sdk) -> dict:
    sub = args[0].lower() if args else "status"
    if sub in ("on", "off"):
        cfg = _save_config({"enabled": sub == "on"})
        return {
            "message": (
                f"🔊 Text-to-Speech enabled ({cfg['server_url']})."
                if cfg["enabled"]
                else "🔇 Text-to-Speech muted."
            )
        }
    if sub in ("emotion", "emotions"):
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            return {"message": "Usage: /tts emotions on | off", "error": True}
        cfg = _save_config({"emotion_enabled": args[1].lower() == "on"})
        return {
            "message": (
                "🎭 TTS emotional delivery enabled."
                if cfg["emotion_enabled"]
                else "🎭 TTS emotional delivery disabled (using neutral voice)."
            )
        }
    if sub == "url":
        if len(args) < 2:
            return {"message": "Usage: /tts url <http://host:7864>", "error": True}
        cfg = _save_config({"server_url": args[1]})
        health = await probe_server(force=True)
        state_label = "ready" if health["ready"] else f"unreachable ({health['error'] or 'offline'})"
        return {"message": f"🔊 TTS server set to {cfg['server_url']} — {state_label}."}
    if sub == "status":
        cfg = _load_config()
        health = await probe_server(force=True)
        enabled_str = "enabled" if cfg["enabled"] else "muted"
        emo_str = "emotions on" if cfg.get("emotion_enabled", True) else "emotions off"
        server_str = (
            f"ready ({health['model'] or 'Qwen3-TTS'})"
            if health["ready"]
            else f"offline ({health['error'] or 'not running'})"
        )
        return {"message": f"🔊 TTS is {enabled_str} ({emo_str}) · Server {cfg['server_url']}: {server_str}"}
    return {
        "message": "Usage: /tts on | off | emotions on|off | url <http://host:7864> | status",
        "error": True,
    }


def get_router():
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import Response, StreamingResponse

    router = APIRouter()

    @router.get("/status")
    async def get_status(probe: bool = False):
        cfg = _load_config()
        health = await probe_server(force=probe)
        return {
            "module": MODULE_ID,
            "config": _public_config(cfg),
            "health": health,
            "emotions": list(load_emotion_catalog().values()),
            "jev": _jev_status(),
        }

    @router.post("/probe")
    async def post_probe(body: dict | None = None):
        if isinstance(body, dict) and body:
            _save_config(body)
        cfg = _load_config()
        health = await probe_server(force=True)
        return {
            "ok": bool(health["ready"]),
            "config": _public_config(cfg),
            "health": health,
            "emotions": list(load_emotion_catalog().values()),
            "jev": _jev_status(),
        }

    @router.get("/config")
    async def get_config():
        return _public_config()

    @router.put("/config")
    async def put_config(body: dict):
        saved = _save_config(body or {})
        health = await probe_server(force=True)
        return {
            "config": _public_config(saved),
            "health": health,
            "jev": _jev_status(),
        }

    @router.post("/stream")
    async def stream_tts(request: Request, body: dict | None = None):
        payload = body or {}
        raw_text = str(payload.get("text") or "")
        text = clean_spoken_text(raw_text)
        if not text:
            return Response(status_code=204)

        cfg = _load_config()
        if not cfg.get("enabled", True) and not payload.get("force"):
            raise HTTPException(status_code=409, detail="TTS is currently muted")

        # Ensure we know which voice identifier and emotion bank the target server exposes
        health = await probe_server(force=False)
        voice = str(payload.get("voice") or "").strip() or health.get("resolved_voice") or "default"

        catalog = load_emotion_catalog()
        requested_emotion = str(payload.get("emotion") or "").strip().lower()
        preceding = payload.get("preceding_sentences")
        preceding_list = preceding if isinstance(preceding, list) else None

        if requested_emotion and requested_emotion != "auto" and requested_emotion in catalog:
            resolved_emotion = requested_emotion
        elif cfg.get("emotion_enabled", True):
            resolved_emotion = await classify_sentence_emotion(
                raw_text=raw_text,
                spoken_text=text,
                preceding_sentences=preceding_list,
            )
        else:
            resolved_emotion = ""

        upstream_url = f"{cfg['server_url']}/v1/tts/stream"
        upstream_body = {
            "text": text,
            "voice": voice,
            "emotion": resolved_emotion,
            "chunk_size": cfg.get("chunk_size", 8),
            "temperature": cfg.get("temperature", 0.9),
            "instruction": str(payload.get("instruction") or ""),
        }

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=4.0, read=120.0, write=10.0, pool=5.0)
        )
        try:
            req = client.build_request(
                "POST",
                upstream_url,
                json=upstream_body,
                headers=_auth_headers(cfg),
            )
            upstream = await client.send(req, stream=True)
        except httpx.RequestError as exc:
            await client.aclose()
            _health_cache["checked_at"] = 0.0
            _health_cache["ready"] = False
            _health_cache["reachable"] = False
            _health_cache["error"] = str(exc)
            raise HTTPException(status_code=502, detail=f"TTS server unreachable: {exc}") from exc

        if upstream.status_code >= 400:
            err_bytes = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            detail = err_bytes.decode("utf-8", errors="replace")[:300] or f"HTTP {upstream.status_code}"
            raise HTTPException(status_code=upstream.status_code, detail=detail)

        sample_rate = upstream.headers.get("x-sample-rate") or str(SAMPLE_RATE)
        channels = upstream.headers.get("x-channels") or "1"
        audio_format = upstream.headers.get("x-audio-format") or "s16le"
        active_emotion = upstream.headers.get("x-tts-emotion") or resolved_emotion

        async def forward_pcm():
            try:
                async for chunk in upstream.aiter_bytes(chunk_size=8192):
                    if await request.is_disconnected():
                        break
                    if chunk:
                        yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        headers = {
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Audio-Format": audio_format,
            "X-Sample-Rate": str(sample_rate),
            "X-Channels": str(channels),
        }
        if active_emotion:
            headers["X-TTS-Emotion"] = active_emotion

        return StreamingResponse(
            forward_pcm(),
            media_type="application/octet-stream",
            headers=headers,
        )

    return router
