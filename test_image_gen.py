import asyncio
import importlib.util
import json
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
