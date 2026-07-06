import asyncio
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

MID = "wb_image_gen"


def _load_backend(tmp_path=None):
    """Fresh module instance per test so global locks/tasks never leak across
    event loops (each test drives its own asyncio.run)."""
    path = Path(__file__).parent / "modules" / MID / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_image_gen_backend", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if tmp_path is not None:
        mod.set_services({"data_dir": str(tmp_path)})
    return mod


def _make_sdk(reply="a knight rides through mist", captured=None):
    captured = captured if captured is not None else {}

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        captured.setdefault("prompts", []).append(prompt)
        captured.setdefault("preferences", []).append(model_preference)
        return reply

    return SimpleNamespace(llm=SimpleNamespace(generate=generate, _current_module=""))


def _state(turn=3, data=None, history=None, **extra):
    state = {
        "turn": turn,
        "active_save_id": "mystory",
        "history": history if history is not None else ["The market square bustles."],
        "chat_messages": [
            {"role": "user", "content": "I browse the stalls."},
            {"role": "ai", "content": "A merchant waves you over."},
        ],
        "module_configs": {},
        "module_data": {MID: data} if data is not None else {},
    }
    state.update(extra)
    return state


def _enable(backend, **overrides):
    cfg = backend._default_config()
    cfg.update({"enabled": True, "api_key": "secret123", "interval": 2,
                "model_name": "dreamshaper_8.safetensors"})
    cfg.update(overrides)
    backend._save_config(cfg)
    return cfg


def _fake_novita(backend, image_bytes=b"fakepng"):
    """Monkeypatch the Novita client with instant fakes."""
    async def submit(cfg, prompt):
        return "task-1"

    async def poll(cfg, task_id):
        return "https://signed.example/sample.jpeg"

    async def download(url):
        return image_bytes, "jpg"

    backend._novita_submit = submit
    backend._novita_poll = poll
    backend._download = download


# ---------------------------------------------------------------------------
# Template / prompt writing
# ---------------------------------------------------------------------------

def test_render_template_survives_braces_and_appends_suffix(tmp_path):
    backend = _load_backend(tmp_path)

    rendered = backend._render_template(
        "SCENE: {narration}\nPAST: {history}",
        'He said {"never"} and {left}', "old {stuff}")
    assert 'He said {"never"} and {left}' in rendered
    assert "old {stuff}" in rendered

    captured = {}
    sdk = _make_sdk(reply='```\n"a foggy alley"\n```', captured=captured)
    cfg = {**backend._default_config(), "style_suffix": "oil painting"}
    prompt = asyncio.run(backend._write_image_prompt(cfg, "narration text", "earlier", sdk))
    assert prompt == "a foggy alley, oil painting"
    assert captured["preferences"] == ["smartest"]
    assert "narration text" in captured["prompts"][0]


# ---------------------------------------------------------------------------
# Librarian counter / trigger
# ---------------------------------------------------------------------------

def test_librarian_noop_when_disabled_keyless_or_modelless(tmp_path):
    backend = _load_backend(tmp_path)

    async def run():
        # Default config: disabled, no key, no model.
        assert await backend.on_librarian(_state(), _make_sdk()) is None
        _enable(backend, enabled=False)
        assert await backend.on_librarian(_state(), _make_sdk()) is None
        _enable(backend, api_key="")
        assert await backend.on_librarian(_state(), _make_sdk()) is None
        _enable(backend, model_name="")
        assert await backend.on_librarian(_state(), _make_sdk()) is None

    asyncio.run(run())


def test_librarian_counts_triggers_and_resets(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend, interval=2)
    _fake_novita(backend)

    async def run():
        r1 = await backend.on_librarian(_state(turn=1, data={"turns_since_image": 0}), _make_sdk())
        assert r1["module_data"][MID]["turns_since_image"] == 1

        r2 = await backend.on_librarian(_state(turn=2, data={"turns_since_image": 1}), _make_sdk())
        update = r2["module_data"][MID]
        assert update["turns_since_image"] == 0
        assert update["last_trigger"]
        await asyncio.gather(*backend._tasks)
        return update["last_trigger"]

    record_id = asyncio.run(run())
    records = backend._read_index()
    assert len(records) == 1
    record = records[0]
    assert record["id"] == record_id
    assert record["status"] == "done"
    assert record["save_id"] == "mystory"
    assert record["turn"] == 2
    assert (tmp_path / MID / "images" / record["filename"]).read_bytes() == b"fakepng"


def test_librarian_keeps_ripe_counter_when_busy(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend, interval=2)

    async def run():
        async with backend._get_gen_lock():
            result = await backend.on_librarian(
                _state(turn=5, data={"turns_since_image": 1}), _make_sdk())
        update = result["module_data"][MID]
        assert update["turns_since_image"] == 2
        assert "last_trigger" not in update

    asyncio.run(run())
    assert backend._read_index() == []


# ---------------------------------------------------------------------------
# Pipeline failure isolation
# ---------------------------------------------------------------------------

def test_pipeline_failure_marks_record_and_never_raises(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)

    async def boom(cfg, prompt):
        raise RuntimeError("Novita rejected the request (403): invalid API key")

    backend._novita_submit = boom

    async def run():
        record_id = backend._spawn_generation(
            save_id="mystory", turn=4, narration="a scene", history="",
            sdk=_make_sdk(), trigger="auto")
        assert record_id
        await asyncio.gather(*backend._tasks)  # must not raise
        return record_id

    record_id = asyncio.run(run())
    record = backend._read_index()[0]
    assert record["id"] == record_id
    assert record["status"] == "error"
    assert "403" in record["error"]


def test_restart_flips_stale_pending_records(tmp_path):
    backend = _load_backend(tmp_path)
    asyncio.run(backend._append_record({"id": "x1", "status": "generating"}))
    asyncio.run(backend._append_record({"id": "x2", "status": "done"}))

    backend.set_services({"data_dir": str(tmp_path)})  # simulated restart
    records = {r["id"]: r for r in backend._read_index()}
    assert records["x1"]["status"] == "error"
    assert records["x1"]["error"] == "interrupted by restart"
    assert records["x2"]["status"] == "done"


# ---------------------------------------------------------------------------
# /image command
# ---------------------------------------------------------------------------

def test_command_image_requires_key_and_spawns(tmp_path):
    backend = _load_backend(tmp_path)

    async def run_no_key():
        return await backend.on_command_image([], _state(), _make_sdk())

    result = asyncio.run(run_no_key())
    assert "No API key" in result["message"]

    # Key but no model picked yet: friendly pointer to the studio.
    _enable(backend, model_name="")
    result = asyncio.run(backend.on_command_image([], _state(), _make_sdk()))
    assert "No model selected" in result["message"]

    _enable(backend)
    _fake_novita(backend)

    async def run():
        result = await backend.on_command_image([], _state(turn=7), _make_sdk())
        await asyncio.gather(*backend._tasks)
        return result

    result = asyncio.run(run())
    assert "Illustrating" in result["message"]
    record_id = result["module_data"][MID]["last_trigger"]
    record = backend._read_index()[0]
    assert record["id"] == record_id
    assert record["trigger"] == "manual"
    assert record["status"] == "done"
    assert record["turn"] == 7


def test_command_image_reports_busy(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)

    async def run():
        async with backend._get_gen_lock():
            return await backend.on_command_image([], _state(), _make_sdk())

    result = asyncio.run(run())
    assert "already being generated" in result["message"]


# ---------------------------------------------------------------------------
# Router: config masking + file safety
# ---------------------------------------------------------------------------

def _client(backend):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(backend.get_router())
    return TestClient(app)


def test_config_get_masks_key_and_put_keeps_masked_key(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    resp = client.put("/config", json={"api_key": "secret123", "interval": 60,
                                       "steps": 500, "guidance_scale": 99, "width": 4000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key"] == "****t123"
    assert body["has_key"] is True
    assert body["interval"] == 50       # clamped
    assert body["steps"] == 100         # clamped
    assert body["guidance_scale"] == 30 # clamped
    assert body["width"] == 2048        # clamped

    # Round-tripping the masked value must not clobber the stored key.
    resp = client.put("/config", json={"api_key": "****t123", "interval": 5})
    assert resp.status_code == 200
    assert backend._load_config()["api_key"] == "secret123"

    # The raw key never appears in any GET payload.
    resp = client.get("/config")
    assert "secret123" not in resp.text

    assert client.put("/config", json={"sampler_name": "Fake Sampler"}).status_code == 400


def test_image_file_serving_blocks_traversal(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    images_dir = tmp_path / MID / "images"
    (images_dir / "good_1_abc.jpg").write_bytes(b"imgdata")
    (tmp_path / MID / "config.json").write_text("{}", encoding="utf-8")

    assert client.get("/images/file/good_1_abc.jpg").content == b"imgdata"
    for bad in ("..%2fconfig.json", "..%5cconfig.json", "good_1_abc.exe",
                "%2e%2e%2fconfig.json", "a" * 200 + ".jpg"):
        assert client.get(f"/images/file/{bad}").status_code == 404


def test_images_index_filters_by_save_and_counts_pending(tmp_path):
    backend = _load_backend(tmp_path)
    asyncio.run(backend._append_record({"id": "a", "save_id": "s1", "status": "done"}))
    asyncio.run(backend._append_record({"id": "b", "save_id": "s2", "status": "generating"}))
    asyncio.run(backend._append_record({"id": "c", "save_id": "s1", "status": "pending"}))
    client = _client(backend)

    body = client.get("/images?save_id=s1").json()
    assert [r["id"] for r in body["records"]] == ["c", "a"]  # newest first
    assert body["pending"] == 1

    body = client.get("/images").json()
    assert len(body["records"]) == 3
    assert body["pending"] == 2


def test_prompt_style_detection(tmp_path):
    backend = _load_backend(tmp_path)

    def cfg(base="", name="model.safetensors"):
        return {**backend._default_config(), "model_base": base, "model_name": name}

    assert backend._prompt_style(cfg("FLUX.1")) == "natural"
    assert backend._prompt_style(cfg("SDXL 1.0")) == "natural"
    assert backend._prompt_style(cfg("")) == "natural"
    assert backend._prompt_style(cfg("Pony")) == "tags"
    assert backend._prompt_style(cfg("Illustrious XL")) == "tags"
    # sd_name fallback for configs saved before model_base existed
    assert backend._prompt_style(cfg("", "ponyDiffusionV6XL.safetensors")) == "tags"
    assert backend._is_pony(cfg("Pony"))
    assert not backend._is_pony(cfg("Illustrious XL"))


def test_prompt_writer_picks_template_and_pony_tags(tmp_path):
    backend = _load_backend(tmp_path)

    # Pony base: tag template used, quality tags prepended before the suffix.
    captured = {}
    sdk = _make_sdk(reply="1girl, market square, smiling", captured=captured)
    cfg = {**backend._default_config(), "model_base": "Pony",
           "model_name": "m.safetensors", "style_suffix": "anime style"}
    prompt = asyncio.run(backend._write_image_prompt(cfg, "scene", "", sdk))
    assert "DANBOORU" in captured["prompts"][0]
    assert prompt == "score_9, score_8_up, score_7_up, 1girl, market square, smiling, anime style"

    # Illustrious: tag template, but no pony score tags.
    sdk = _make_sdk(reply="1girl, market square", captured={})
    cfg = {**backend._default_config(), "model_base": "Illustrious XL", "model_name": "m.safetensors"}
    prompt = asyncio.run(backend._write_image_prompt(cfg, "scene", "", sdk))
    assert prompt == "1girl, market square"

    # Flux: natural-language template, no tags anywhere.
    captured = {}
    sdk = _make_sdk(reply="a bustling market at dawn", captured=captured)
    cfg = {**backend._default_config(), "model_base": "FLUX.1", "model_name": "m.safetensors"}
    prompt = asyncio.run(backend._write_image_prompt(cfg, "scene", "", sdk))
    assert "DANBOORU" not in captured["prompts"][0]
    assert "vivid image-generation prompt" in captured["prompts"][0]
    assert prompt == "a bustling market at dawn"

    # Pony prefix + suffix survive the 1024 cap; scene text is what gets trimmed.
    sdk = _make_sdk(reply="x" * 3000, captured={})
    cfg = {**backend._default_config(), "model_base": "Pony",
           "model_name": "m.safetensors", "style_suffix": "anime style"}
    prompt = asyncio.run(backend._write_image_prompt(cfg, "scene", "", sdk))
    assert len(prompt) <= backend.MAX_PROMPT_CHARS
    assert prompt.startswith("score_9, score_8_up, score_7_up, ")
    assert prompt.endswith(", anime style")


def test_prompt_cap_respects_novita_limit(tmp_path):
    backend = _load_backend(tmp_path)
    captured = {}
    sdk = _make_sdk(reply="x" * 3000, captured=captured)
    cfg = {**backend._default_config(), "style_suffix": "oil painting, dramatic light"}

    prompt = asyncio.run(backend._write_image_prompt(cfg, "narration", "", sdk))
    assert len(prompt) <= backend.MAX_PROMPT_CHARS
    assert prompt.endswith("oil painting, dramatic light")  # suffix survives the trim


def test_novita_payload_shape(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = {**backend._default_config(), "model_name": "dreamshaper_8.safetensors",
           "width": 512, "height": 768, "steps": 30, "guidance_scale": 8.0,
           "sampler_name": "Euler a", "negative_prompt": ""}

    payload = backend._novita_payload(cfg, "a red door")
    req = payload["request"]
    assert req["model_name"] == "dreamshaper_8.safetensors"
    assert req["prompt"] == "a red door"
    assert (req["width"], req["height"]) == (512, 768)
    assert req["image_num"] == 1
    assert req["steps"] == 30
    assert req["guidance_scale"] == 8.0
    assert req["sampler_name"] == "Euler a"
    assert req["seed"] == -1
    assert "negative_prompt" not in req  # omitted when empty
    assert payload["extra"]["response_image_type"] == "jpeg"

    cfg["negative_prompt"] = "blurry"
    assert backend._novita_payload(cfg, "p")["request"]["negative_prompt"] == "blurry"


def test_models_endpoint_proxies_and_requires_key(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    assert client.get("/models?query=real").status_code == 400  # no key yet

    _enable(backend)
    captured = {}

    async def fake_list(cfg, query, cursor, limit):
        captured.update({"query": query, "cursor": cursor, "limit": limit})
        return {
            "models": [
                {"sd_name": "goodModel_v1.safetensors", "sd_name_in_api": "goodModel_v1.safetensors",
                 "name": "Good Model", "status": 1, "is_sdxl": True,
                 "base_model": "SDXL 1.0", "cover_url": "https://img.example/c.jpg"},
                {"sd_name": "brokenModel.safetensors", "name": "Broken", "status": 0},
            ],
            "pagination": {"next_cursor": "abc123"},
        }

    backend._novita_list_models = fake_list
    body = client.get("/models?query=real&limit=10").json()
    assert captured == {"query": "real", "cursor": "", "limit": 10}
    assert body["next_cursor"] == "abc123"
    assert len(body["models"]) == 1  # unavailable model filtered out
    model = body["models"][0]
    assert model["sd_name"] == "goodModel_v1.safetensors"
    assert model["is_sdxl"] is True

    async def bad_key(cfg, query, cursor, limit):
        raise RuntimeError("Novita rejected the model search: invalid API key")

    backend._novita_list_models = bad_key
    resp = client.get("/models")
    assert resp.status_code == 502
    assert "invalid API key" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# LoRA library: families, payloads, trigger words, Civitai flattening
# ---------------------------------------------------------------------------

# Civitai stores the full SHA256; Novita's catalog truncates it to 10 chars.
FULL_HASH = "a1b2c3d4e5" + "f" * 54
NOVITA_HASH = "A1B2C3D4E5"


def _lora(**overrides):
    entry = {
        "id": "123456",
        "name": "Detail Tweaker",
        "base_model": "SDXL 1.0",
        "sha256": "abc123",
        "download_url": "https://civitai.com/api/download/models/123456",
        "trained_words": ["detailed", "sharp focus"],
        "active": True,
        "strength": 0.7,
        "sd_name_override": "",
        "novita": {"sd_name_in_api": "detail_tweaker_123456.safetensors"},
    }
    entry.update(overrides)
    return entry


def _lora_cfg(backend, **overrides):
    cfg = {**backend._default_config(),
           "model_name": "sd_xl_base_1.0.safetensors", "model_base": "SDXL 1.0"}
    cfg.update(overrides)
    return cfg


def test_base_family_buckets(tmp_path):
    backend = _load_backend(tmp_path)
    assert backend._base_family("SDXL 1.0") == "sdxl"
    assert backend._base_family("Pony") == "sdxl"
    assert backend._base_family("Illustrious") == "sdxl"
    assert backend._base_family("NoobAI") == "sdxl"
    assert backend._base_family("SD 1.5") == "sd15"
    assert backend._base_family("Flux.1 D") == "flux"
    assert backend._base_family("Flux.2 D") == "flux"
    assert backend._base_family("") == ""
    # The built-in FLUX.2 model has no base metadata but must resolve to flux.
    cfg = _lora_cfg(backend, model_name=backend.FLUX2_MODEL_NAME, model_base="")
    assert backend._checkpoint_family(cfg) == "flux"


def test_sd_payload_includes_matched_compatible_loras(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, lora_library=[_lora()])
    payload = backend._novita_payload(cfg, "a castle")
    assert payload["request"]["loras"] == [
        {"model_name": "detail_tweaker_123456.safetensors", "strength": 0.7}]


def test_sd_payload_skips_inactive_unmatched_and_incompatible(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, lora_library=[
        _lora(id="1", active=False),                          # inactive
        _lora(id="2", novita=None),                           # not on Novita
        _lora(id="3", base_model="SD 1.5"),                   # wrong family
        _lora(id="4", base_model="Flux.1 D"),                 # flux lora on SD checkpoint
    ])
    assert "loras" not in backend._novita_payload(cfg, "a castle")["request"]


def test_sd_payload_override_beats_missing_match_and_clamps(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, lora_library=[
        _lora(novita=None, sd_name_override="my_upload.safetensors", strength=5.0)])
    loras = backend._novita_payload(cfg, "x")["request"]["loras"]
    assert loras == [{"model_name": "my_upload.safetensors", "strength": 1.0}]

    many = [_lora(id=str(i)) for i in range(8)]
    cfg = _lora_cfg(backend, lora_library=many)
    assert len(backend._novita_payload(cfg, "x")["request"]["loras"]) == backend.SD_LORAS_MAX


def test_flux2_payload_uses_download_links_with_token(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, model_name=backend.FLUX2_MODEL_NAME, model_base="",
                    civitai_api_key="civkey",
                    lora_library=[_lora(base_model="Flux.2 D", novita=None)])
    payload = backend._flux2_payload(cfg, "a castle")
    assert payload["loras"] == [
        "https://civitai.com/api/download/models/123456?token=civkey"]
    assert payload["size"] == "1024*1024"
    assert payload["seed"] == -1

    # Token joins an existing query string with '&'.
    cfg["lora_library"] = [_lora(
        base_model="Flux.1 D", novita=None,
        download_url="https://civitai.com/api/download/models/1?type=Model")]
    assert backend._flux2_payload(cfg, "x")["loras"][0].endswith("?type=Model&token=civkey")


def test_flux2_payload_skips_sd_loras_clamps_size_caps_count(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, model_name=backend.FLUX2_MODEL_NAME,
                    width=2048, height=100, lora_library=[_lora()])  # SDXL lora
    payload = backend._flux2_payload(cfg, "x")
    assert "loras" not in payload
    assert payload["size"] == "1536*256"

    cfg["lora_library"] = [_lora(id=str(i), base_model="Flux.2 D", novita=None)
                           for i in range(5)]
    assert len(backend._flux2_payload(cfg, "x")["loras"]) == backend.FLUX_LORAS_MAX


def test_submit_routes_flux2_to_its_endpoint(tmp_path):
    backend = _load_backend(tmp_path)
    seen = {}

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"task_id": "t1"}
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            seen.update({"url": url, "payload": json})
            return FakeResponse()

    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        cfg = _lora_cfg(backend, model_name=backend.FLUX2_MODEL_NAME,
                        api_key="k", civitai_api_key="c",
                        lora_library=[_lora(base_model="Flux.2 D", novita=None)])
        task_id = asyncio.run(backend._novita_submit(cfg, "a castle"))
    finally:
        httpx.AsyncClient = original
    assert task_id == "t1"
    assert seen["url"].endswith("/v3/async/flux-2-dev")
    assert seen["payload"]["loras"] == [
        "https://civitai.com/api/download/models/123456?token=c"]


def test_trigger_words_injected_for_usable_loras_only(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, lora_library=[
        _lora(trained_words=["glowing runes"]),
        _lora(id="2", base_model="Flux.2 D", novita=None, trained_words=["skipme"]),
        _lora(id="3", active=False, trained_words=["also skipped"]),
        _lora(id="4", trained_words=["Glowing Runes", "bokeh"]),  # dedupe, case-insensitive
    ])
    assert backend._active_trigger_words(cfg) == ["glowing runes", "bokeh"]

    captured = {}
    sdk = _make_sdk(reply="a scene with glowing runes", captured=captured)
    asyncio.run(backend._write_image_prompt(cfg, "narration", "", sdk))
    assert "glowing runes, bokeh" in captured["prompts"][0]

    # No active loras: the instruction never appears.
    captured = {}
    sdk = _make_sdk(captured=captured)
    asyncio.run(backend._write_image_prompt(
        _lora_cfg(backend), "narration", "", sdk))
    assert "trigger words" not in captured["prompts"][0]


def test_flatten_civitai_model(tmp_path):
    backend = _load_backend(tmp_path)
    raw = {
        "id": 42, "name": "Cool LoRA", "type": "LORA", "nsfw": False,
        "creator": {"username": "artist"},
        "stats": {"downloadCount": 1000, "thumbsUpCount": 55},
        "modelVersions": [{
            "id": 987, "name": "v2", "baseModel": "Pony",
            "trainedWords": ["cool style"],
            "downloadUrl": "https://civitai.com/api/download/models/987",
            "files": [
                {"primary": False, "downloadUrl": "https://x/other", "hashes": {}},
                {"primary": True, "sizeKB": 100.5,
                 "downloadUrl": "https://civitai.com/api/download/models/987",
                 "hashes": {"SHA256": "ABCDEF"}},
            ],
            "images": [
                {"url": "https://img/clip.mp4", "type": "video"},
                {"url": "https://img/pic.jpg", "type": "image"},
            ],
        }],
    }
    flat = backend._flatten_civitai_model(raw)
    assert flat["id"] == "987"
    assert flat["sha256"] == "abcdef"                 # primary file's hash, lowercased
    assert flat["thumb_url"] == "https://img/pic.jpg"  # first non-video image
    assert flat["stats"] == {"downloads": 1000, "likes": 55}
    assert flat["civitai_url"] == "https://civitai.com/models/42"
    assert flat["trained_words"] == ["cool style"]
    assert flat["published_at"] == ""  # absent in fixture, defaults empty
    assert flat["tags"] == []

    assert backend._flatten_civitai_model({"id": 1, "modelVersions": []}) is None


def test_flatten_collects_all_version_hashes(tmp_path):
    backend = _load_backend(tmp_path)
    raw = {
        "id": 7, "name": "Multi", "type": "LORA",
        "stats": {}, "creator": {},
        "modelVersions": [
            {"id": 30, "name": "v3", "files": [
                {"primary": True, "hashes": {"SHA256": "CCC"}}], "images": []},
            {"id": 20, "name": "v2", "files": [
                {"primary": True, "hashes": {"SHA256": "BBB"}}], "images": []},
            {"id": 10, "name": "v1", "files": [
                {"primary": True, "hashes": {"SHA256": "ccc"}}], "images": []},  # dupe of v3
        ],
    }
    flat = backend._flatten_civitai_model(raw)
    assert flat["sha256"] == "ccc"                 # latest version stays primary
    assert flat["all_hashes"] == ["ccc", "bbb"]    # every version, deduped/lowercased


def test_novita_match_lora_via_older_version_hash(tmp_path):
    backend = _load_backend(tmp_path)
    calls = []
    old_hash = "b1b2b3b4b5" + "0" * 54

    async def fake_list(cfg, query, cursor, limit, types="checkpoint", visibility=""):
        calls.append((query, cursor, types))
        return {"models": [
            {"hash_sha256": "B1B2B3B4B5", "sd_name_in_api": "old_ver.safetensors",
             "status": 1}], "pagination": {}}

    backend._novita_list_models = fake_list
    entry = _lora(novita=None, sha256="c1" * 32, all_hashes=["c1" * 32, old_hash])
    match = asyncio.run(backend._novita_match_lora(_lora_cfg(backend, api_key="k"), entry))
    assert match == {"sd_name_in_api": "old_ver.safetensors"}
    # One whole-catalog sync, no per-key guess queries (Novita can't find
    # Civitai ids via filter.query).
    assert calls == [("", "", "lora")]

    # When several versions are mirrored, the newest one wins (all_hashes is
    # ordered latest-first).
    index = {"C1C1C1C1C1": "new_ver.safetensors",
             "B1B2B3B4B5": "old_ver.safetensors"}
    match = asyncio.run(backend._novita_match_lora(
        _lora_cfg(backend, api_key="k"), entry, index))
    assert match == {"sd_name_in_api": "new_ver.safetensors"}


def test_match_all_endpoint_rechecks_unmatched_only(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    cfg = backend._load_config()
    cfg["lora_library"] = [
        _lora(id="1", novita=None, sha256=FULL_HASH),                 # should recheck → match
        _lora(id="2"),                                                # already matched
        _lora(id="3", novita=None, sd_name_override="manual.st"),     # override set
        _lora(id="4", novita=None, base_model="Flux.2 D"),            # flux: link-based
        _lora(id="5", novita=None, sha256="unknown", all_hashes=[]),  # rechecks, stays None
    ]
    backend._save_config(cfg)

    async def fake_list(cfg, query, cursor, limit, types="checkpoint", visibility=""):
        return {"models": [
            {"hash_sha256": NOVITA_HASH, "sd_name_in_api": "found.safetensors",
             "status": 1}], "pagination": {}}

    backend._novita_list_models = fake_list
    client = _client(backend)
    body = client.post("/loras/match_all").json()
    assert body["checked"] == 2
    assert body["matched"] == 1

    entries = {e["id"]: e for e in backend._load_config()["lora_library"]}
    assert entries["1"]["novita"] == {"sd_name_in_api": "found.safetensors"}
    assert entries["1"]["novita_checked_at"]
    assert entries["5"]["novita"] is None
    assert entries["5"]["novita_checked_at"]
    assert entries["3"]["novita"] is None and not entries["3"].get("novita_checked_at")
    assert entries["4"]["novita"] is None and not entries["4"].get("novita_checked_at")

    # Keyless: refused.
    backend2 = _load_backend(tmp_path / "nokey")
    assert _client(backend2).post("/loras/match_all").status_code == 400


def test_my_novita_loras_endpoint(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)
    assert client.get("/novita/my-loras").status_code == 400  # no key

    _enable(backend)
    captured = {}

    async def fake_list(cfg, query, cursor, limit, types="checkpoint", visibility=""):
        captured.update({"types": types, "visibility": visibility})
        return {"models": [
            {"sd_name_in_api": "mine_1.safetensors", "name": "My Style",
             "base_model": "SDXL 1.0", "status": 1},
            {"sd_name": "processing.safetensors", "name": "Uploading", "status": 2},
            {"name": "nameless", "status": 1},  # unusable, dropped
        ]}

    backend._novita_list_models = fake_list
    body = client.get("/novita/my-loras").json()
    assert captured == {"types": "lora", "visibility": "private"}
    assert body["loras"] == [
        {"sd_name": "mine_1.safetensors", "name": "My Style",
         "base_model": "SDXL 1.0", "ready": True},
        {"sd_name": "processing.safetensors", "name": "Uploading",
         "base_model": "", "ready": False},
    ]


def test_novita_match_lora_by_truncated_hash_prefix(tmp_path):
    """Novita returns only the first 10 uppercase chars of each SHA256; the
    full lowercase Civitai hash must still match (the original bug: full-hash
    equality never matched anything)."""
    backend = _load_backend(tmp_path)
    calls = []

    async def fake_list(cfg, query, cursor, limit, types="checkpoint", visibility=""):
        calls.append(cursor)
        if not cursor:
            return {"models": [
                {"hash_sha256": "WRONGWRONG", "sd_name_in_api": "no.safetensors",
                 "status": 1},
                {"hash_sha256": "DEADBEEF00", "sd_name_in_api": "broken.safetensors",
                 "status": 0},
            ], "pagination": {"next_cursor": "c_100"}}
        return {"models": [
            {"hash_sha256": NOVITA_HASH, "sd_name_in_api": "yes.safetensors",
             "status": 1}], "pagination": {}}

    backend._novita_list_models = fake_list
    cfg = _lora_cfg(backend, api_key="k")
    match = asyncio.run(backend._novita_match_lora(cfg, _lora(sha256=FULL_HASH)))
    assert match == {"sd_name_in_api": "yes.safetensors"}
    assert calls == ["", "c_100"]  # paged through the whole catalog once

    # Broken (status != 1) mirrors never match, and the index now serves from
    # the disk cache — no further network calls.
    calls.clear()
    dead = "deadbeef00" + "0" * 54
    assert asyncio.run(backend._novita_match_lora(cfg, _lora(sha256=dead))) is None
    assert calls == []

    # No hash at all: no lookup.
    assert asyncio.run(backend._novita_match_lora(
        cfg, _lora(sha256="", all_hashes=[]))) is None


def test_novita_lora_index_ttl_and_force_refresh(tmp_path):
    backend = _load_backend(tmp_path)
    calls = []

    async def fake_list(cfg, query, cursor, limit, types="checkpoint", visibility=""):
        calls.append(cursor)
        return {"models": [
            {"hash_sha256": NOVITA_HASH, "sd_name_in_api": "x.safetensors",
             "status": 1}], "pagination": {}}

    backend._novita_list_models = fake_list
    cfg = _lora_cfg(backend, api_key="k")
    assert asyncio.run(backend._novita_lora_index(cfg)) == {
        NOVITA_HASH: "x.safetensors"}
    assert len(calls) == 1

    # Within the TTL the disk cache answers.
    asyncio.run(backend._novita_lora_index(cfg))
    assert len(calls) == 1

    # force=True rebuilds regardless.
    asyncio.run(backend._novita_lora_index(cfg, force=True))
    assert len(calls) == 2

    # An expired cache rebuilds too.
    path = backend._lora_index_path()
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["fetched_at"] = time.time() - backend.NOVITA_LORA_INDEX_TTL_S - 1
    path.write_text(json.dumps(stale), encoding="utf-8")
    asyncio.run(backend._novita_lora_index(cfg))
    assert len(calls) == 3

    # Training-data archives mirrored as "lora" entries are excluded (a .zip
    # sd_name is not a loadable weight).
    async def zip_list(cfg, query, cursor, limit, types="checkpoint", visibility=""):
        return {"models": [
            {"hash_sha256": "0000000000", "sd_name_in_api": "123_training_data.zip",
             "status": 1}], "pagination": {}}

    backend._novita_list_models = zip_list
    assert asyncio.run(backend._novita_lora_index(cfg, force=True)) == {}


def test_lora_library_endpoints_roundtrip(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    client = _client(backend)

    async def fake_list(cfg, query, cursor, limit, types="checkpoint", visibility=""):
        return {"models": [
            {"hash_sha256": NOVITA_HASH, "sd_name_in_api": "found_123456.safetensors",
             "status": 1}], "pagination": {}}

    backend._novita_list_models = fake_list

    item = {"id": "123456", "name": "Detail Tweaker", "base_model": "SDXL 1.0",
            "sha256": FULL_HASH, "download_url": "https://civitai.com/api/download/models/123456",
            "trained_words": ["detailed"], "stats": {"downloads": 5, "likes": 2}}
    resp = client.post("/loras", json=item)
    assert resp.status_code == 200
    entry = resp.json()["entry"]
    assert entry["novita"] == {"sd_name_in_api": "found_123456.safetensors"}
    assert entry["active"] is False

    assert client.post("/loras", json=item).status_code == 409  # dedupe

    resp = client.patch("/loras/123456", json={"active": True, "strength": 1.7})
    assert resp.status_code == 200
    assert resp.json()["entry"]["strength"] == 1.0  # clamped

    # Persisted: a fresh config load feeds the payload builder.
    cfg = backend._load_config()
    cfg.update({"model_name": "sd_xl_base_1.0.safetensors", "model_base": "SDXL 1.0"})
    assert backend._novita_payload(cfg, "x")["request"]["loras"] == [
        {"model_name": "found_123456.safetensors", "strength": 1.0}]

    assert client.delete("/loras/123456").status_code == 200
    assert backend._load_config()["lora_library"] == []
    assert client.delete("/loras/123456").status_code == 404
    assert client.patch("/loras/zzz", json={"active": True}).status_code == 404


def test_civitai_loras_endpoint_gates_nsfw(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    for mode in ("include", "only"):
        resp = client.get(f"/civitai/loras?nsfw={mode}")
        assert resp.status_code == 400
        assert "Civitai API key" in resp.json()["detail"]

    captured = {}

    async def fake_search(cfg, **kwargs):
        captured.update(kwargs)
        return {"items": [], "next_cursor": ""}

    backend._civitai_search_loras = fake_search
    resp = client.get("/civitai/loras?query=style&base_model=Pony&sort=Newest&lora_type=LoCon")
    assert resp.status_code == 200
    assert captured == {"query": "style", "base_model": "Pony", "lora_type": "LoCon",
                        "sort": "Newest", "nsfw_mode": "off", "category": "",
                        "cursor": "", "limit": 24}

    # Unknown mode values degrade to off instead of erroring (or bypassing the gate).
    assert client.get("/civitai/loras?nsfw=true").status_code == 200
    assert captured["nsfw_mode"] == "off"


def test_civitai_search_nsfw_modes(tmp_path):
    backend = _load_backend(tmp_path)
    seen = {}

    def _mixed_body():
        version = {"id": 1, "name": "v1", "files": [], "images": []}
        return {"items": [
            {"id": 1, "nsfw": False, "name": "safe", "modelVersions": [dict(version, id=1)]},
            {"id": 2, "nsfw": True, "name": "spicy", "modelVersions": [dict(version, id=2)]},
        ], "metadata": {"nextCursor": ""}}

    class FakeResponse:
        status_code = 200
        def json(self):
            return _mixed_body()

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            seen["params"] = dict(params)
            return FakeResponse()

    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        def run(mode):
            return asyncio.run(backend._civitai_search_loras(
                backend._default_config(), query="", base_model="", lora_type="LORA",
                sort="Most Downloaded", nsfw_mode=mode, cursor="", limit=24))

        assert seen == {}
        result = run("off")
        assert seen["params"]["nsfw"] == "false"
        assert [i["nsfw"] for i in result["items"]] == [False, True]  # no post-filter

        result = run("include")
        assert seen["params"]["nsfw"] == "true"
        assert len(result["items"]) == 2

        result = run("only")
        assert seen["params"]["nsfw"] == "true"
        assert [i["name"] for i in result["items"]] == ["spicy"]
    finally:
        httpx.AsyncClient = original


def test_civitai_search_with_query_sorts_proxy_side(tmp_path):
    # Civitai's Meilisearch path (query set) ignores `sort` and returns
    # relevance order; the proxy must re-sort and fetch a full page.
    backend = _load_backend(tmp_path)
    seen = {}

    def _version(vid, published):
        return {"id": vid, "name": "v1", "files": [], "images": [],
                "publishedAt": published}

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"items": [
                {"id": 1, "name": "mid", "nsfw": False,
                 "stats": {"downloadCount": 50, "thumbsUpCount": 500},
                 "modelVersions": [_version(1, "2024-01-01T00:00:00Z")]},
                {"id": 2, "name": "big", "nsfw": False,
                 "stats": {"downloadCount": 900, "thumbsUpCount": 10},
                 "modelVersions": [_version(2, "2023-01-01T00:00:00Z")]},
                {"id": 3, "name": "new", "nsfw": False,
                 "stats": {"downloadCount": 1, "thumbsUpCount": 2},
                 "modelVersions": [_version(3, "2025-06-01T00:00:00Z")]},
            ], "metadata": {}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            seen["params"] = dict(params)
            return FakeResponse()

    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        def run(sort, query="style"):
            result = asyncio.run(backend._civitai_search_loras(
                backend._default_config(), query=query, base_model="",
                lora_type="LORA", sort=sort, nsfw_mode="off", cursor="", limit=24))
            return [i["name"] for i in result["items"]]

        assert run("Most Downloaded") == ["big", "mid", "new"]
        assert seen["params"]["limit"] == "100"  # full page fetched for re-sort
        assert run("Highest Rated") == ["mid", "big", "new"]
        assert run("Newest") == ["new", "mid", "big"]
        # Without a query Civitai's own order is kept (and correct).
        assert run("Most Downloaded", query="") == ["mid", "big", "new"]
        assert seen["params"]["limit"] == "24"
    finally:
        httpx.AsyncClient = original


def test_civitai_category_filter(tmp_path):
    # Without a query the category rides the API's tag= param; with a query
    # (where Civitai ignores tag=) items are post-filtered on their tags.
    backend = _load_backend(tmp_path)
    seen = {}

    def _model(mid, tags):
        return {"id": mid, "name": f"m{mid}", "nsfw": False, "tags": tags,
                "stats": {"downloadCount": mid, "thumbsUpCount": 0},
                "modelVersions": [{"id": mid, "name": "v", "files": [], "images": []}]}

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"items": [
                _model(1, ["Character", "anime"]),
                _model(2, ["style"]),
            ], "metadata": {}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            seen["params"] = dict(params)
            return FakeResponse()

    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        def run(query, category):
            return asyncio.run(backend._civitai_search_loras(
                backend._default_config(), query=query, base_model="",
                lora_type="LORA", sort="Most Downloaded", nsfw_mode="off",
                cursor="", limit=24, category=category))

        result = run("", "character")
        assert seen["params"]["tag"] == "character"
        assert len(result["items"]) == 2  # API already filtered; no post-filter

        result = run("frieren", "character")
        assert "tag" not in seen["params"]
        assert [i["id"] for i in result["items"]] == ["1"]  # tag post-filter (case-folded)

        result = run("", "not-a-category")
        assert "tag" not in seen["params"]  # unknown category ignored
    finally:
        httpx.AsyncClient = original


def test_civitai_search_with_query_chains_pages_and_dedupes(tmp_path):
    backend = _load_backend(tmp_path)
    requested = []

    def _model(mid, downloads):
        return {"id": mid, "name": f"m{mid}", "nsfw": False,
                "stats": {"downloadCount": downloads, "thumbsUpCount": 0},
                "modelVersions": [{"id": mid, "name": "v", "files": [], "images": []}]}

    pages = {
        "": {"items": [_model(1, 10), _model(2, 500)], "metadata": {"nextCursor": "c2"}},
        "c2": {"items": [_model(2, 500), _model(3, 90)], "metadata": {"nextCursor": "c3"}},
        "c3": {"items": [_model(4, 999)], "metadata": {"nextCursor": "c4"}},
    }

    class FakeResponse:
        status_code = 200
        def __init__(self, body):
            self._body = body
        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            cursor = dict(params).get("cursor", "")
            requested.append(cursor)
            return FakeResponse(pages[cursor])

    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        result = asyncio.run(backend._civitai_search_loras(
            backend._default_config(), query="style", base_model="", lora_type="LORA",
            sort="Most Downloaded", nsfw_mode="off", cursor="", limit=24))
    finally:
        httpx.AsyncClient = original

    assert requested == ["", "c2", "c3"]                # CIVITAI_SEARCH_PAGES chained
    assert result["next_cursor"] == "c4"                # load-more continues from there
    ids = [i["id"] for i in result["items"]]
    assert ids == ["4", "2", "3", "1"]                  # merged, deduped, sorted by downloads


def test_civitai_nsfw_bool_config_migrates(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = backend._default_config()
    cfg["civitai_nsfw"] = True  # pre-dropdown config file
    backend._save_config(cfg)
    assert backend._load_config()["civitai_nsfw"] == "include"

    cfg["civitai_nsfw"] = False
    backend._save_config(cfg)
    assert backend._load_config()["civitai_nsfw"] == "off"


def test_models_endpoint_pins_flux2(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)

    async def fake_list(cfg, query, cursor, limit):
        return {"models": [], "pagination": {}}

    backend._novita_list_models = fake_list
    client = _client(backend)

    first = client.get("/models").json()["models"]
    assert first[0]["sd_name"] == backend.FLUX2_MODEL_NAME

    assert client.get("/models?query=flux").json()["models"][0]["sd_name"] == backend.FLUX2_MODEL_NAME
    assert client.get("/models?query=anime").json()["models"] == []      # no pin
    assert client.get("/models?cursor=abc").json()["models"] == []        # not on later pages


def test_submit_key_endpoint_validates_before_saving(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    async def ok(key):
        return True

    async def bad(key):
        return False

    backend._validate_novita_key = ok
    backend._validate_civitai_key = bad

    resp = client.post("/keys/novita", json={"api_key": " goodkey123 "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_key"] is True
    assert "goodkey123" not in resp.text  # masked in the response
    assert backend._load_config()["api_key"] == "goodkey123"

    resp = client.post("/keys/civitai", json={"api_key": "wrongkey"})
    assert resp.status_code == 400
    assert "rejected" in resp.json()["detail"]
    assert backend._load_config()["civitai_api_key"] == ""  # not stored

    assert client.post("/keys/novita", json={"api_key": "   "}).status_code == 400
    assert client.post("/keys/novita", json={"api_key": "****t123"}).status_code == 400
    assert client.post("/keys/other", json={"api_key": "x"}).status_code == 404

    async def down(key):
        raise RuntimeError("Could not reach Novita: timeout")

    backend._validate_novita_key = down
    resp = client.post("/keys/novita", json={"api_key": "whatever"})
    assert resp.status_code == 502
    assert "Could not reach" in resp.json()["detail"]
    assert backend._load_config()["api_key"] == "goodkey123"  # unchanged


def test_civitai_key_masked_in_config(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    resp = client.put("/config", json={"civitai_api_key": "civsecret99", "civitai_nsfw": "only"})
    body = resp.json()
    assert body["civitai_api_key"] == "****t99" or body["civitai_api_key"].endswith("t99")
    assert body["has_civitai_key"] is True
    assert body["civitai_nsfw"] == "only"
    assert "civsecret99" not in resp.text
    assert client.put("/config", json={"civitai_nsfw": "everything"}).status_code == 400

    # Masked round-trip keeps the stored key.
    client.put("/config", json={"civitai_api_key": body["civitai_api_key"]})
    assert backend._load_config()["civitai_api_key"] == "civsecret99"


def test_generate_endpoint_studio_override_and_busy(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    _fake_novita(backend)
    client = _client(backend)

    resp = client.post("/generate", json={"prompt_override": "a red door"})
    assert resp.status_code == 200
    record_id = resp.json()["record_id"]

    # TestClient runs its own loop per request; wait for the task from here.
    for _ in range(100):
        record = next((r for r in backend._read_index() if r["id"] == record_id), None)
        if record and record["status"] in ("done", "error"):
            break
        import time as _t
        _t.sleep(0.02)
    assert record["status"] == "done"
    assert record["save_id"] == "__studio__"
    assert record["image_prompt"] == "a red door"
