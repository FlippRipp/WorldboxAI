#!/usr/bin/env python3
"""Standalone Faster Qwen3-TTS streaming provider server for WorldBox.

Extracted from PNGTuber's real-time speech runtime so it can run either on the
same machine as WorldBox or on a dedicated GPU host over LAN / Tailscale (to
keep mobile gameplay fast and battery-friendly).

Endpoints:
  GET  /health         -> readiness, active model, sample rate, voice ids, and emotions
  POST /v1/tts/stream  -> raw 24 kHz s16le mono PCM stream with disconnect abort
  POST /shutdown       -> graceful uvicorn shutdown

Security:
  When --token (or WB_TTS_TOKEN) is set, every request must include
  `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import inspect
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent
DEFAULT_INSTALL_DIR = REPO_ROOT / "tts_server"
DEFAULT_MODEL_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
DEFAULT_MODEL_DIR = DEFAULT_INSTALL_DIR / "models" / "Qwen3-TTS-12Hz-1.7B-Base"
DEFAULT_VOICES_DIR = MODULE_DIR / "voices"

SAMPLE_RATE = 24000
CHANNELS = 1
SERVICE_ID = "worldbox-faster-qwen-tts"

_EXPLICIT_BREATH_RE = re.compile(r"\b(ha+h|ah+|mm+|uh+)\b", flags=re.IGNORECASE)


@dataclass(frozen=True)
class VoiceDefinition:
    id: str
    label: str
    reference_audio: Path
    reference_text: str
    language: str = "English"


@dataclass(frozen=True)
class EmotionDefinition:
    id: str
    label: str
    description: str
    reference_audio: Path
    reference_text: str
    instruct: str
    temperature: float = 0.88
    non_streaming_mode: bool = True
    gain: float = 1.0
    clause_breath_cue: str = ""
    language: str = "English"


class SpeechRequest(BaseModel):
    text: str
    voice: str = "default"
    emotion: str = ""
    chunk_size: int = 8
    temperature: float | None = None
    instruction: str = ""
    non_streaming_mode: bool | None = None


def _read_reference_transcript(path: Path) -> str:
    try:
        paragraphs = [
            part.strip()
            for part in path.read_text(encoding="utf-8").split("\n\n")
        ]
    except OSError:
        return ""
    return next((part for part in reversed(paragraphs) if part), "")


def discover_voices(voices_dir: Path) -> dict[str, VoiceDefinition]:
    """Load every `<id>.wav` + `<id>.txt` pair in `voices_dir`."""
    voices: dict[str, VoiceDefinition] = {}
    if not voices_dir.is_dir():
        return voices
    for wav_path in sorted(voices_dir.glob("*.wav")):
        voice_id = wav_path.stem
        txt_path = wav_path.with_suffix(".txt")
        transcript = _read_reference_transcript(txt_path)
        if not transcript:
            continue
        label = "Default Narrator" if voice_id == "default" else voice_id.replace("_", " ").title()
        voices[voice_id] = VoiceDefinition(
            id=voice_id,
            label=label,
            reference_audio=wav_path.resolve(),
            reference_text=transcript,
        )
    # Alias `current_cleaned` -> `default` for PNGTuber client compatibility
    if "default" in voices and "current_cleaned" not in voices:
        d = voices["default"]
        voices["current_cleaned"] = VoiceDefinition(
            id="current_cleaned",
            label=d.label,
            reference_audio=d.reference_audio,
            reference_text=d.reference_text,
            language=d.language,
        )
    return voices


def discover_emotions(voices_dir: Path) -> dict[str, EmotionDefinition]:
    """Load the 16-emotion reference bank from `voices_dir / 'emotions' / 'manifest.json'`."""
    emotions_dir = voices_dir / "emotions"
    manifest_path = emotions_dir / "manifest.json"
    emotions: dict[str, EmotionDefinition] = {}
    if not manifest_path.is_file():
        return emotions
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return emotions
    if not isinstance(raw, dict):
        return emotions
    for emo_id, entry in raw.items():
        if not isinstance(emo_id, str) or not isinstance(entry, dict):
            continue
        wav_name = str(entry.get("wav") or f"{emo_id}.wav")
        wav_path = emotions_dir / wav_name
        if not wav_path.is_file():
            continue
        ref_text = str(entry.get("ref_text") or "").strip()
        if not ref_text:
            ref_text = _read_reference_transcript(emotions_dir / str(entry.get("txt") or f"{emo_id}.txt"))
        if not ref_text:
            continue
        emotions[emo_id] = EmotionDefinition(
            id=emo_id,
            label=str(entry.get("label") or emo_id.replace("_", " ").title()),
            description=str(entry.get("description") or ""),
            reference_audio=wav_path.resolve(),
            reference_text=ref_text,
            instruct=str(entry.get("instruct") or "").strip(),
            temperature=float(entry.get("temperature", 0.88)),
            non_streaming_mode=bool(entry.get("non_streaming_mode", True)),
            gain=float(entry.get("gain", 1.0)),
            clause_breath_cue=str(entry.get("clause_breath_cue") or ""),
        )
    return emotions


def inject_clause_breath_cue(text: str, cue: str = "... haah... ") -> str:
    """Insert an open-mouth breath cue at the main mid-sentence clause break when none is already written."""
    if not cue or not text or _EXPLICIT_BREATH_RE.search(text):
        return text

    def _cont(rest: str) -> str:
        rest = rest.lstrip()
        if len(rest) >= 2 and rest[0].isupper() and not (rest[0] == "I" and not rest[1].isalpha()):
            return rest[0].lower() + rest[1:]
        return rest

    m = re.search(r"\.\.\.\s+(?=\S)", text)
    if m:
        return text[: m.start()] + cue + _cont(text[m.end() :])

    m = re.search(r"([.!?])\s+(?=[A-Z\"'])", text)
    if m:
        return text[: m.start()] + cue + _cont(text[m.end() :])

    m = re.search(r"(?:,\s+|\s*—\s*)(?=\S)", text[12:])
    if m:
        s = 12 + m.start()
        e = 12 + m.end()
        return text[:s] + cue + _cont(text[e:])

    leading = cue.strip().lstrip(".").strip()
    if leading:
        leading = leading[0].upper() + leading[1:]
        return f"{leading} {_cont(text)}"
    return text


def pcm16(audio, gain: float = 1.0) -> bytes:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if gain != 1.0:
        samples = samples * float(gain)
        abs_s = np.abs(samples)
        knee = 0.88
        over = abs_s > knee
        if np.any(over):
            headroom = 1.0 - knee
            soft = knee + headroom * np.tanh((abs_s[over] - knee) / headroom)
            samples = samples.copy()
            samples[over] = np.sign(samples[over]) * soft
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


async def until_disconnected(chunks, disconnected):
    """Stop requesting chunks after client disconnect; release the model generator."""
    iterator = iter(chunks)
    try:
        while not await disconnected():
            try:
                chunk = next(iterator)
            except StopIteration:
                return
            if await disconnected():
                return
            yield chunk
            # Yield briefly to the event loop so uvicorn flushes each PCM chunk
            # immediately rather than coalescing the synchronous generator.
            await asyncio.sleep(0)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def ensure_model_downloaded(model_dir: Path, repo_id: str = DEFAULT_MODEL_REPO) -> Path:
    """Download Qwen3-TTS weights from Hugging Face on first launch if missing."""
    config_file = model_dir / "config.json"
    if config_file.is_file():
        return model_dir
    from huggingface_hub import snapshot_download

    print(f"[wb-tts] Downloading {repo_id} -> {model_dir} ...", flush=True)
    model_dir.parent.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(model_dir),
    )
    return model_dir


def _prewarm_emotion_prompts(model, emotions: dict[str, EmotionDefinition]) -> None:
    """Pre-encode all 16 emotion reference WAVs into `model._voice_prompt_cache` (< 0.3 MB VRAM)."""
    resolver = getattr(model, "_resolve_voice_clone_prompt", None)
    if not callable(resolver) or not emotions:
        return
    warmed = 0
    for emo in emotions.values():
        try:
            resolver(
                input_ids=[None],
                ref_audio=str(emo.reference_audio),
                ref_text=emo.reference_text,
                xvec_only=False,
                append_silence=True,
                voice_clone_prompt=None,
            )
            warmed += 1
        except Exception as exc:
            print(f"[wb-tts] Warning: failed to prewarm emotion '{emo.id}': {exc}", flush=True)
    if warmed:
        print(f"[wb-tts] Pre-warmed {warmed} emotion voice prompts in VRAM cache.", flush=True)


def build_app(
    *,
    model_dir: Path,
    voices_dir: Path,
    token: str = "",
    device: str = "cuda",
    model=None,
) -> FastAPI:
    """Build the FastAPI service. Pass `model` in tests to skip GPU loading."""
    voices = discover_voices(voices_dir)
    if not voices:
        raise RuntimeError(
            f"No complete voice references (<name>.wav + <name>.txt) found in {voices_dir}"
        )
    emotions = discover_emotions(voices_dir)
    default_voice = voices.get("default") or next(iter(voices.values()))

    if model is None:
        try:
            import shutil
            if shutil.which("ffmpeg") is None:
                import static_ffmpeg
                static_ffmpeg.add_paths()
        except Exception:
            pass
        import torch
        from faster_qwen3_tts import FasterQwen3TTS

        resolved_model_dir = ensure_model_downloaded(model_dir)
        print(f"[wb-tts] Loading faster-qwen3-tts from {resolved_model_dir} ...", flush=True)
        model = FasterQwen3TTS.from_pretrained(
            str(resolved_model_dir.resolve()),
            device=device,
            dtype=torch.bfloat16,
            attn_implementation="eager",
            max_seq_len=2048,
            local_files_only=True,
        )
        print("[wb-tts] Capturing CUDA graphs and warming up default voice...", flush=True)
        model.generate_voice_clone(
            text="Hello from the warmup run.",
            language=default_voice.language,
            ref_audio=str(default_voice.reference_audio),
            ref_text=default_voice.reference_text,
            max_new_tokens=20,
        )
        _prewarm_emotion_prompts(model, emotions)

    stream_params = set()
    try:
        sig = inspect.signature(model.generate_voice_clone_streaming)
        supports_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        stream_params = {"*"} if supports_var_kw else set(sig.parameters.keys())
    except (TypeError, ValueError):
        stream_params = {"*"}
    accepts_nsm = "*" in stream_params or "non_streaming_mode" in stream_params

    expected_token = (token or "").strip()
    gen_lock = asyncio.Lock()

    async def verify_auth(authorization: str | None = Header(default=None)):
        if not expected_token:
            return
        scheme, _, creds = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(creds.strip(), expected_token):
            raise HTTPException(status_code=401, detail="Unauthorized")

    app = FastAPI(title="WorldBox Faster Qwen3-TTS")

    @app.get("/health", dependencies=[Depends(verify_auth)])
    async def health():
        public_voices = [vid for vid in voices if vid != "current_cleaned"]
        return {
            "ready": True,
            "service": SERVICE_ID,
            "pid": os.getpid(),
            "model": model_dir.name,
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "voices": public_voices,
            "default_voice": default_voice.id,
            "emotions": list(emotions.keys()),
        }

    @app.post("/shutdown", dependencies=[Depends(verify_auth)])
    async def shutdown(request: Request):
        server = getattr(request.app.state, "uvicorn_server", None)
        if server is not None:
            server.should_exit = True
        return {"stopping": True, "service": SERVICE_ID}

    @app.post("/v1/tts/stream", dependencies=[Depends(verify_auth)])
    async def stream_speech(request: SpeechRequest, connection: Request):
        text = request.text.strip()
        if not text:
            raise HTTPException(400, "Text is required")
        voice = voices.get(request.voice) or (
            default_voice if request.voice in ("", "default") else None
        )
        if voice is None:
            raise HTTPException(404, f"Unknown voice: {request.voice}")

        chunk_size = max(2, min(24, int(request.chunk_size)))
        emo_key = (request.emotion or "").strip().lower()
        use_emotion = (
            emotions.get(emo_key)
            if emo_key and voice.id in ("default", "current_cleaned")
            else None
        )

        if use_emotion is not None:
            resolved_emotion = use_emotion.id
            ref_audio = str(use_emotion.reference_audio)
            ref_text = use_emotion.reference_text
            language = use_emotion.language
            base_temp = use_emotion.temperature
            if request.temperature is not None:
                # Scale emotion's calibrated temperature proportionally around default 0.9
                scaled = base_temp * (float(request.temperature) / 0.9)
                temperature = round(max(0.1, min(1.5, scaled)), 3)
            else:
                temperature = max(0.1, min(1.5, base_temp))
            instruction = request.instruction.strip() or use_emotion.instruct
            nsm = (
                request.non_streaming_mode
                if request.non_streaming_mode is not None
                else use_emotion.non_streaming_mode
            )
            gain = use_emotion.gain
            if use_emotion.clause_breath_cue:
                text = inject_clause_breath_cue(text, use_emotion.clause_breath_cue)
        else:
            resolved_emotion = ""
            ref_audio = str(voice.reference_audio)
            ref_text = voice.reference_text
            language = voice.language
            raw_temp = 0.9 if request.temperature is None else float(request.temperature)
            temperature = max(0.1, min(1.5, raw_temp))
            instruction = request.instruction.strip()
            nsm = request.non_streaming_mode
            gain = 1.0

        async def generate():
            # Serialize GPU streaming across concurrent sentence prefetches so
            # static CUDA graph replay buffers never collide.
            async with gen_lock:
                if await connection.is_disconnected():
                    return
                stream_kwargs = {
                    "text": text,
                    "language": language,
                    "ref_audio": ref_audio,
                    "ref_text": ref_text,
                    "chunk_size": chunk_size,
                    "temperature": temperature,
                    "instruct": instruction or None,
                }
                if accepts_nsm and nsm is not None:
                    stream_kwargs["non_streaming_mode"] = bool(nsm)
                chunks = model.generate_voice_clone_streaming(**stream_kwargs)
                stream = until_disconnected(chunks, connection.is_disconnected)
                try:
                    async for audio, _sample_rate, _timing in stream:
                        yield pcm16(audio, gain=gain)
                finally:
                    await stream.aclose()

        headers = {
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Audio-Format": "s16le",
            "X-Sample-Rate": str(SAMPLE_RATE),
            "X-Channels": str(CHANNELS),
        }
        if resolved_emotion:
            headers["X-TTS-Emotion"] = resolved_emotion

        return StreamingResponse(
            generate(),
            media_type="application/octet-stream",
            headers=headers,
        )

    print(
        f"[wb-tts] Faster Qwen3-TTS ready (default voice: {default_voice.id}, emotions: {len(emotions)}).",
        flush=True,
    )
    return app


def main() -> None:
    default_ip = "0.0.0.0" if os.environ.get("WB_TTS_LISTEN", "1") != "0" else "127.0.0.1"
    parser = argparse.ArgumentParser(description="WorldBox Faster Qwen3-TTS Provider Server")
    parser.add_argument("--ip", default=os.environ.get("WB_TTS_IP", default_ip))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WB_TTS_PORT", "7864")))
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(os.environ.get("WB_TTS_MODEL_DIR", str(DEFAULT_MODEL_DIR))),
    )
    parser.add_argument(
        "--voices-dir",
        type=Path,
        default=Path(os.environ.get("WB_TTS_VOICES_DIR", str(DEFAULT_VOICES_DIR))),
    )
    parser.add_argument("--token", default=os.environ.get("WB_TTS_TOKEN", ""))
    parser.add_argument("--device", default=os.environ.get("WB_TTS_DEVICE", "cuda"))
    args = parser.parse_args()

    app = build_app(
        model_dir=args.model_dir,
        voices_dir=args.voices_dir,
        token=args.token,
        device=args.device,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.ip, port=args.port, log_level="info")
    )
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()
