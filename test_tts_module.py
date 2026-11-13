import asyncio
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.wb_tts import backend as tts_backend
from modules.wb_tts import server as tts_server


class FakeStreamingModel:
    def __init__(self):
        self.calls = []
        self.closed_generators = 0

    def generate_voice_clone_streaming(
        self,
        *,
        text,
        language,
        ref_audio,
        ref_text,
        chunk_size=8,
        temperature=0.9,
        instruct=None,
    ):
        self.calls.append({
            "text": text,
            "language": language,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "chunk_size": chunk_size,
            "temperature": temperature,
            "instruct": instruct,
        })

        def _gen():
            try:
                for val in (0.25, -0.25, 0.5):
                    yield np.full((240,), val, dtype=np.float32), 24000, {}
            finally:
                self.closed_generators += 1

        return _gen()


def test_clean_spoken_text_strips_markup_and_hidden_blocks():
    raw = (
        "<internal_states>mood: tense</internal_states>\n\n"
        '<color:pink:whisper>"Stay *close* to [me](https://example.com)!"</color> '
        "<details><summary>Stats</summary>HP 10</details>"
    )
    assert tts_backend.clean_spoken_text(raw) == '"Stay close to me!"'
    assert tts_backend.clean_spoken_text("   --- ***   ") == ""


def test_standalone_tts_server_health_auth_and_pcm_stream():
    fake_model = FakeStreamingModel()
    voices_dir = Path(tts_server.__file__).resolve().parent / "voices"
    app = tts_server.build_app(
        model_dir=Path("Qwen3-TTS-12Hz-1.7B-Base"),
        voices_dir=voices_dir,
        token="secret-token",
        model=fake_model,
    )
    client = TestClient(app)

    # Auth required when token is configured
    unauth = client.get("/health")
    assert unauth.status_code == 401

    headers = {"Authorization": "Bearer secret-token"}
    health = client.get("/health", headers=headers)
    assert health.status_code == 200
    body = health.json()
    assert body["ready"] is True
    assert body["service"] == "worldbox-faster-qwen-tts"
    assert "default" in body["voices"]

    # Stream PCM16 audio (3 chunks * 240 samples * 2 bytes = 1440 bytes)
    stream_res = client.post(
        "/v1/tts/stream",
        json={"text": "Hello from WorldBox.", "voice": "default"},
        headers=headers,
    )
    assert stream_res.status_code == 200
    assert stream_res.headers["X-Sample-Rate"] == "24000"
    assert stream_res.headers["X-Audio-Format"] == "s16le"
    assert len(stream_res.content) == 3 * 240 * 2
    assert fake_model.closed_generators == 1

    # PNGTuber legacy voice alias also resolves cleanly
    alias_res = client.post(
        "/v1/tts/stream",
        json={"text": "Legacy voice alias.", "voice": "current_cleaned"},
        headers=headers,
    )
    assert alias_res.status_code == 200
    assert len(alias_res.content) == 3 * 240 * 2


def test_until_disconnected_closes_generator_immediately():
    closed = {"count": 0}

    def producer():
        try:
            yield b"chunk-1"
            yield b"chunk-2"
            yield b"chunk-3"
        finally:
            closed["count"] += 1

    seen = []

    async def run():
        checks = iter([False, False, True])

        async def is_disconnected():
            return next(checks)

        stream = tts_server.until_disconnected(producer(), is_disconnected)
        try:
            async for item in stream:
                seen.append(item)
        finally:
            await stream.aclose()

    asyncio.run(run())
    assert seen == [b"chunk-1"]
    assert closed["count"] == 1


def test_wb_tts_backend_config_masking_commands_and_proxy(tmp_path, monkeypatch):
    tts_backend.set_services({"global_data_dir": tmp_path})

    host_app = FastAPI()
    host_app.include_router(tts_backend.get_router(), prefix="/api/modules/wb_tts")
    client = TestClient(host_app)

    # Save config with token -> returned config masks token
    put_res = client.put(
        "/api/modules/wb_tts/config",
        json={
            "enabled": True,
            "server_url": "192.168.1.42:7864",
            "token": "my-super-secret",
            "temperature": 0.85,
        },
    )
    assert put_res.status_code == 200
    saved_cfg = put_res.json()["config"]
    assert saved_cfg["server_url"] == "http://192.168.1.42:7864"
    assert saved_cfg["has_token"] is True
    assert saved_cfg["token"].startswith("****")

    # Updating another field while echoing masked token preserves real token
    client.put(
        "/api/modules/wb_tts/config",
        json={"token": saved_cfg["token"], "chunk_size": 10},
    )
    assert tts_backend._load_config()["token"] == "my-super-secret"

    # Slash commands toggle enabled state
    off_msg = asyncio.run(tts_backend.on_command_tts(["off"], {}, None))
    assert "muted" in off_msg["message"].lower()
    assert tts_backend._load_config()["enabled"] is False

    # Muted /stream returns 409 unless force=True
    muted_res = client.post("/api/modules/wb_tts/stream", json={"text": "Test speech"})
    assert muted_res.status_code == 409

    # Mock upstream Faster Qwen3-TTS server via httpx MockTransport
    fake_model = FakeStreamingModel()
    voices_dir = Path(tts_server.__file__).resolve().parent / "voices"
    upstream_app = tts_server.build_app(
        model_dir=Path("Qwen3-TTS-12Hz-1.7B-Base"),
        voices_dir=voices_dir,
        token="my-super-secret",
        model=fake_model,
    )
    transport = httpx.ASGITransport(app=upstream_app)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(tts_backend.httpx, "AsyncClient", patched_async_client)

    asyncio.run(tts_backend.on_command_tts(["on"], {}, None))
    probe_res = client.post("/api/modules/wb_tts/probe")
    assert probe_res.status_code == 200
    assert probe_res.json()["ok"] is True

    # Non-speakable punctuation returns 204 without hitting GPU
    empty_res = client.post("/api/modules/wb_tts/stream", json={"text": "***"})
    assert empty_res.status_code == 204
    assert len(fake_model.calls) == 0

    # Real prose proxies PCM bytes and strips <color> tags before reaching TTS model
    pcm_res = client.post(
        "/api/modules/wb_tts/stream",
        json={"text": '<pink>"Good evening!"</pink>'},
    )
    assert pcm_res.status_code == 200
    assert pcm_res.headers["X-Sample-Rate"] == "24000"
    assert len(pcm_res.content) == 3 * 240 * 2
    assert fake_model.calls[-1]["text"] == '"Good evening!"'
