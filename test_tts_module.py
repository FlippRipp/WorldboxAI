import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.engine.jev import JevService
from backend.engine.llm_call_log import LLMCallLog
from backend.engine.llm_inspector import LLMInspector
from modules.wb_tts import backend as tts_backend
from modules.wb_tts import server as tts_server


class FakeStreamingModel:
    def __init__(self):
        self.calls = []
        self.prewarmed = []
        self.closed_generators = 0

    def _resolve_voice_clone_prompt(
        self,
        *,
        input_ids,
        ref_audio,
        ref_text,
        xvec_only=False,
        append_silence=True,
        voice_clone_prompt=None,
    ):
        self.prewarmed.append((ref_audio, ref_text, xvec_only, append_silence))
        return {}, [], True

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
        non_streaming_mode=None,
    ):
        self.calls.append({
            "text": text,
            "language": language,
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "chunk_size": chunk_size,
            "temperature": temperature,
            "instruct": instruct,
            "non_streaming_mode": non_streaming_mode,
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
    # strip_hidden_blocks keeps inline color/markdown cues for Jev while removing hidden blocks
    stripped = tts_backend.strip_hidden_blocks(raw)
    assert "internal_states" not in stripped
    assert "HP 10" not in stripped
    assert "<color:pink:whisper>" in stripped


def test_inject_clause_breath_cue_for_horny():
    assert (
        tts_server.inject_clause_breath_cue(
            "Don't pull away yet... stay right here with me."
        )
        == "Don't pull away yet... haah... stay right here with me."
    )
    assert (
        tts_server.inject_clause_breath_cue(
            "Keep your hands right where they are. Don't make me wait."
        )
        == "Keep your hands right where they are... haah... don't make me wait."
    )
    assert (
        tts_server.inject_clause_breath_cue(
            "Wait right here by the gate. I need to tell you what I saw."
        )
        == "Wait right here by the gate... haah... I need to tell you what I saw."
    )
    # Explicit sigh/moan already written -> untouched
    already = "Ahh... don't stop now, stay right here."
    assert tts_server.inject_clause_breath_cue(already) == already


def test_pcm16_soft_knee_prevents_hard_clipping():
    loud = np.array([0.5, 0.95, -1.4], dtype=np.float32)
    raw = tts_server.pcm16(loud, gain=1.35)
    decoded = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    assert decoded[0] > 0.65
    assert 0.88 < decoded[1] <= 1.0
    assert -1.0 <= decoded[2] < -0.88


def test_standalone_tts_server_health_auth_emotions_and_pcm_stream():
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
    assert len(body["emotions"]) == 16
    assert {"neutral", "tense", "hushed", "teasing", "horny"}.issubset(set(body["emotions"]))

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

    # Emotion-routed stream selects emotion ref_audio, instruct, nsm, and horny breath cue
    horny_res = client.post(
        "/v1/tts/stream",
        json={
            "text": "Close the door behind you, and come sit right here next to me.",
            "voice": "default",
            "emotion": "horny",
        },
        headers=headers,
    )
    assert horny_res.status_code == 200
    assert horny_res.headers["X-TTS-Emotion"] == "horny"
    last_call = fake_model.calls[-1]
    assert last_call["ref_audio"].endswith("horny.wav")
    assert "... haah..." in last_call["text"]
    assert last_call["non_streaming_mode"] is False
    assert "breathless" in (last_call["instruct"] or "").lower()

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

    # Slash commands toggle enabled and emotion states
    off_msg = asyncio.run(tts_backend.on_command_tts(["off"], {}, None))
    assert "muted" in off_msg["message"].lower()
    assert tts_backend._load_config()["enabled"] is False

    emo_off = asyncio.run(tts_backend.on_command_tts(["emotions", "off"], {}, None))
    assert "disabled" in emo_off["message"].lower()
    assert tts_backend._load_config()["emotion_enabled"] is False
    asyncio.run(tts_backend.on_command_tts(["emotions", "on"], {}, None))
    assert tts_backend._load_config()["emotion_enabled"] is True

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
    assert len(probe_res.json()["emotions"]) == 16

    # Non-speakable punctuation returns 204 without hitting GPU
    empty_res = client.post("/api/modules/wb_tts/stream", json={"text": "***"})
    assert empty_res.status_code == 204
    assert len(fake_model.calls) == 0

    # Real prose proxies PCM bytes, strips <color> tags for TTS, and falls back to neutral when Jev is unconfigured
    pcm_res = client.post(
        "/api/modules/wb_tts/stream",
        json={"text": '<pink>"Good evening!"</pink>'},
    )
    assert pcm_res.status_code == 200
    assert pcm_res.headers["X-Sample-Rate"] == "24000"
    assert pcm_res.headers["X-TTS-Emotion"] == "neutral"
    assert len(pcm_res.content) == 3 * 240 * 2
    assert fake_model.calls[-1]["text"] == '"Good evening!"'
    assert fake_model.calls[-1]["ref_audio"].endswith("neutral.wav")


def test_wb_tts_jev_emotion_routing_and_fallback(tmp_path, monkeypatch):
    jev_requests = []
    chosen = {"emotion": "teasing", "prob": 0.62}

    def jev_handler(request):
        payload = json.loads(request.content)
        jev_requests.append(payload)
        options = list(payload["questions"]["emotion"]["criteria"].keys())
        sel = chosen["emotion"]
        p_sel = chosen["prob"]
        rem = round((1.0 - p_sel) / (len(options) - 1), 6)
        probs = {k: rem for k in options}
        probs[sel] = round(1.0 - sum(v for k, v in probs.items() if k != sel), 6)
        return httpx.Response(
            200,
            json={
                "model": "jev-test-1",
                "answers": {
                    "emotion": {
                        "type": "choice",
                        "choice": sel,
                        "confidence": p_sel,
                        "probabilities": probs,
                    }
                },
                "usage": {"input_tokens": 1200, "output_tokens": 30},
            },
        )

    jev_service = JevService(
        tmp_path / "providers" / "jev" / "config.json",
        transport=httpx.MockTransport(jev_handler),
    )
    jev_service.save({"enabled": True, "tts_enabled": True, "api_key": "jev-key"})
    inspector = LLMInspector()
    inspector.set_call_logger(LLMCallLog(str(tmp_path / "logs")))

    long_scene_msg = "The tavern candles burned low. " * 120
    session_manager = SimpleNamespace(
        state={
            "chat_messages": [
                {"role": "user", "content": long_scene_msg},
                {"role": "assistant", "content": "<internal_states>secret</internal_states>She leaned across the oak counter."},
            ]
        }
    )
    engine = SimpleNamespace(llm=SimpleNamespace(decisions=jev_service, inspector=inspector))
    tts_backend.set_services({
        "global_data_dir": tmp_path,
        "engine": engine,
        "session_manager": session_manager,
    })

    fake_model = FakeStreamingModel()
    voices_dir = Path(tts_server.__file__).resolve().parent / "voices"
    upstream_app = tts_server.build_app(
        model_dir=Path("Qwen3-TTS-12Hz-1.7B-Base"),
        voices_dir=voices_dir,
        model=fake_model,
    )
    upstream_transport = httpx.ASGITransport(app=upstream_app)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        if "transport" not in kwargs or kwargs["transport"] is None:
            kwargs["transport"] = upstream_transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(tts_backend.httpx, "AsyncClient", patched_async_client)

    host_app = FastAPI()
    host_app.include_router(tts_backend.get_router(), prefix="/api/modules/wb_tts")
    client = TestClient(host_app)

    # 1. Jev picks `teasing` (even at 0.62 < 0.90 RPG confidence) and preserves full untruncated context
    res = client.post(
        "/api/modules/wb_tts/stream",
        json={
            "text": '<color:gold:smirk>"Oh really? And who rescued whom?"</color>',
            "preceding_sentences": ["She tilted her head with a slow grin."],
        },
    )
    assert res.status_code == 200
    assert res.headers["X-TTS-Emotion"] == "teasing"
    assert fake_model.calls[-1]["ref_audio"].endswith("teasing.wav")
    assert fake_model.calls[-1]["text"] == '"Oh really? And who rescued whom?"'

    sent_state = jev_requests[-1]["state"]
    assert sent_state["recent_messages"][0]["content"] == long_scene_msg.strip()
    assert "secret" not in sent_state["recent_messages"][1]["content"]
    assert "<color:gold:smirk>" in sent_state["target_sentence"]
    assert sent_state["turn_preceding_sentences"] == ["She tilted her head with a slow grin."]
    assert inspector.get_calls()[-1]["decision_outcome"] == "accepted"

    # 2. Low probability (< 0.25) falls back cleanly to `neutral`
    chosen["emotion"] = "awed"
    chosen["prob"] = 0.15
    low_res = client.post(
        "/api/modules/wb_tts/stream",
        json={"text": "He walked across the courtyard."},
    )
    assert low_res.status_code == 200
    assert low_res.headers["X-TTS-Emotion"] == "neutral"
    assert inspector.get_calls()[-1]["decision_outcome"] == "fallback"

    # 3. Explicit manual emotion (e.g. from VoiceServer test picker) bypasses Jev
    prev_req_count = len(jev_requests)
    manual_res = client.post(
        "/api/modules/wb_tts/stream",
        json={"text": "Get down behind the pillars!", "emotion": "tense", "force": True},
    )
    assert manual_res.status_code == 200
    assert manual_res.headers["X-TTS-Emotion"] == "tense"
    assert len(jev_requests) == prev_req_count
    assert fake_model.calls[-1]["ref_audio"].endswith("tense.wav")
