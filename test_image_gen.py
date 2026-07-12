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
        raise backend.NonRetryableError(
            "Novita rejected the request (403): invalid API key")

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


# ---------------------------------------------------------------------------
# Step retries
# ---------------------------------------------------------------------------

def _run_pipeline(backend):
    async def run():
        record_id = backend._spawn_generation(
            save_id="mystory", turn=1, narration="a scene", history="",
            sdk=_make_sdk(), trigger="auto")
        assert record_id
        await asyncio.gather(*backend._tasks)
        return record_id

    record_id = asyncio.run(run())
    record = backend._read_index()[0]
    assert record["id"] == record_id
    return record


def test_step_retry_recovers_from_transient_failures(tmp_path):
    backend = _load_backend(tmp_path)
    backend.STEP_RETRY_BASE_DELAY_S = 0
    _enable(backend, step_retries=2)
    _fake_novita(backend)
    calls = {"submit": 0}

    async def flaky_submit(cfg, prompt):
        calls["submit"] += 1
        if calls["submit"] < 3:
            raise RuntimeError("Novita server error 502")
        return "task-1"

    backend._novita_submit = flaky_submit

    record = _run_pipeline(backend)
    assert record["status"] == "done"
    assert calls["submit"] == 3


def test_step_retry_exhausts_configured_attempts(tmp_path):
    backend = _load_backend(tmp_path)
    backend.STEP_RETRY_BASE_DELAY_S = 0
    _enable(backend, step_retries=1)
    calls = {"submit": 0}

    async def always_boom(cfg, prompt):
        calls["submit"] += 1
        raise RuntimeError("Novita server error 503")

    backend._novita_submit = always_boom

    record = _run_pipeline(backend)
    assert record["status"] == "error"
    assert calls["submit"] == 2
    assert "after 2 attempts" in record["error"]


def test_step_retry_zero_means_single_attempt(tmp_path):
    backend = _load_backend(tmp_path)
    backend.STEP_RETRY_BASE_DELAY_S = 0
    _enable(backend, step_retries=0)
    calls = {"submit": 0}

    async def always_boom(cfg, prompt):
        calls["submit"] += 1
        raise RuntimeError("Novita server error 503")

    backend._novita_submit = always_boom

    record = _run_pipeline(backend)
    assert record["status"] == "error"
    assert calls["submit"] == 1
    # A single attempt keeps the original error, no attempt-count wrapper.
    assert "attempts" not in record["error"]


def test_nonretryable_failure_skips_step_retries(tmp_path):
    backend = _load_backend(tmp_path)
    backend.STEP_RETRY_BASE_DELAY_S = 0
    _enable(backend, step_retries=3)
    calls = {"submit": 0}

    async def refuse(cfg, prompt):
        calls["submit"] += 1
        raise backend.NonRetryableError(
            "The image provider refused this prompt (content policy): nsfw")

    backend._novita_submit = refuse

    record = _run_pipeline(backend)
    assert record["status"] == "error"
    assert calls["submit"] == 1
    assert "content policy" in record["error"]


def test_prompt_writing_step_retries(tmp_path):
    backend = _load_backend(tmp_path)
    backend.STEP_RETRY_BASE_DELAY_S = 0
    _enable(backend, step_retries=1)
    _fake_novita(backend)
    calls = {"llm": 0}

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls["llm"] += 1
        if calls["llm"] == 1:
            raise RuntimeError("LLM hiccup")
        return "a knight rides through mist"

    sdk = SimpleNamespace(llm=SimpleNamespace(generate=generate, _current_module=""))

    async def run():
        record_id = backend._spawn_generation(
            save_id="mystory", turn=1, narration="a scene", history="",
            sdk=sdk, trigger="auto")
        await asyncio.gather(*backend._tasks)
        return record_id

    asyncio.run(run())
    record = backend._read_index()[0]
    assert record["status"] == "done"
    assert calls["llm"] == 2
    assert record["image_prompt"] == "a knight rides through mist"


# ---------------------------------------------------------------------------
# Parallel images (image_num)
# ---------------------------------------------------------------------------

def test_parallel_image_num_writes_all_files(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend, image_num=3)
    counter = {"n": 0}

    async def submit(cfg, prompt):
        counter["n"] += 1
        return f"task-{counter['n']}"

    async def poll(cfg, task_id):
        return f"https://signed.example/{task_id}.jpeg"

    async def download(url):
        return url.encode(), "jpg"

    backend._novita_submit = submit
    backend._novita_poll = poll
    backend._download = download

    record = _run_pipeline(backend)
    assert record["status"] == "done"
    assert counter["n"] == 3
    assert len(record["filenames"]) == 3
    assert record["filename"] == record["filenames"][0]
    contents = {(tmp_path / MID / "images" / f).read_bytes() for f in record["filenames"]}
    assert len(contents) == 3   # three distinct tasks, three distinct files


def test_parallel_partial_failure_keeps_survivors(tmp_path):
    backend = _load_backend(tmp_path)
    backend.STEP_RETRY_BASE_DELAY_S = 0
    _enable(backend, image_num=3, step_retries=0)
    _fake_novita(backend)
    calls = {"n": 0}

    async def flaky_submit(cfg, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise backend.NonRetryableError("refused: content policy")
        return "task-ok"

    backend._novita_submit = flaky_submit

    record = _run_pipeline(backend)
    assert record["status"] == "done"
    assert len(record["filenames"]) == 2
    assert record["filename"] == record["filenames"][0]
    for f in record["filenames"]:
        assert (tmp_path / MID / "images" / f).read_bytes() == b"fakepng"


def test_parallel_all_fail_marks_error(tmp_path):
    backend = _load_backend(tmp_path)
    backend.STEP_RETRY_BASE_DELAY_S = 0
    _enable(backend, image_num=2, step_retries=0)

    async def always_boom(cfg, prompt):
        raise backend.NonRetryableError("refused: content policy")

    backend._novita_submit = always_boom

    record = _run_pipeline(backend)
    assert record["status"] == "error"
    assert "content policy" in record["error"]
    assert list((tmp_path / MID / "images").iterdir()) == []


def test_image_num_config_clamped(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    body = client.get("/config").json()
    assert body["image_num"] == 1
    assert body["image_num_max"] == backend.IMAGE_NUM_MAX

    assert client.put("/config", json={"image_num": 99}).json()["image_num"] == backend.IMAGE_NUM_MAX
    assert client.put("/config", json={"image_num": 0}).json()["image_num"] == 1

    # A hand-edited config normalizes on load.
    cfg = backend._load_config()
    cfg["image_num"] = "bogus"
    backend._save_config(cfg)
    assert backend._load_config()["image_num"] == 1


def test_delete_and_retry_remove_all_parallel_files(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    _fake_novita(backend)
    client = _client(backend)
    images = backend._data_dir() / "images"

    def _make_batch_record(rid):
        files = [f"{rid}.jpg", f"{rid}_1.jpg", f"{rid}_2.jpg"]
        for f in files:
            (images / f).write_bytes(b"oldimage")
        asyncio.run(backend._append_record({
            "id": rid, "save_id": "mystory", "turn": 3, "status": "done",
            "narration_excerpt": "A merchant waves you over.",
            "image_prompt": "a smiling merchant",
            "filename": files[0], "filenames": files,
        }))
        return files

    files = _make_batch_record("mystory_3_00000001")
    assert client.delete("/images/mystory_3_00000001").status_code == 200
    assert all(not (images / f).exists() for f in files)

    # Regenerating a batch record also clears every file it owned.
    files = _make_batch_record("mystory_3_00000002")
    resp = client.post("/generate", json={"retry_record_id": "mystory_3_00000002"})
    assert resp.status_code == 200
    record_id = resp.json()["record_id"]
    for _ in range(100):
        record = next((r for r in backend._read_index() if r["id"] == record_id), None)
        if record and record["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert record["status"] == "done"
    assert all(not (images / f).exists() for f in files)


def test_poll_task_failure_retryability_split(tmp_path):
    """A refused prompt must not be resubmitted; an ordinary task failure may."""
    backend = _load_backend(tmp_path)

    class _Resp:
        def __init__(self, body):
            self._body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self._body

    def _poll_with(reason):
        failed = {"task": {"status": "TASK_STATUS_FAILED", "reason": reason}}

        class _Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, *a, **k):
                return _Resp(failed)

        import httpx
        original = httpx.AsyncClient
        httpx.AsyncClient = lambda **kw: _Client()
        backend.POLL_INTERVAL_S = 0
        try:
            asyncio.run(backend._novita_poll({"api_key": "k"}, "task-1"))
        except Exception as e:
            return e
        finally:
            httpx.AsyncClient = original

    assert isinstance(_poll_with("flagged by moderation"), backend.NonRetryableError)
    err = _poll_with("internal worker error")
    assert isinstance(err, RuntimeError)
    assert not isinstance(err, backend.NonRetryableError)


def test_step_retries_config_clamped(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    assert backend._load_config()["step_retries"] == 1  # default

    assert client.put("/config", json={"step_retries": 99}).json()["step_retries"] == 5
    assert client.put("/config", json={"step_retries": -3}).json()["step_retries"] == 0

    # Garbage on disk falls back to the default instead of crashing.
    cfg = backend._default_config()
    cfg["step_retries"] = "lots"
    backend._save_config(cfg)
    assert backend._load_config()["step_retries"] == 1


class _ErrResponse:
    """Minimal stand-in for an httpx error response."""
    def __init__(self, body=None, text="", status_code=400):
        self._body = body
        self.text = text
        self.status_code = status_code

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


def test_novita_error_detail_combines_message_reason_and_metadata(tmp_path):
    backend = _load_backend(tmp_path)
    resp = _ErrResponse({
        "code": 40001,
        "message": "request rejected",
        "reason": "SENSITIVE_CONTENT",
        "metadata": {"field": "prompt"},
    })
    detail = backend._novita_error_detail(resp)
    # All three signal-bearing fields survive, not just the first one found.
    assert "request rejected" in detail
    assert "SENSITIVE_CONTENT" in detail
    assert "prompt" in detail


def test_novita_error_detail_falls_back_to_text_without_json(tmp_path):
    backend = _load_backend(tmp_path)
    assert backend._novita_error_detail(_ErrResponse(text="Bad Gateway")) == "Bad Gateway"
    assert "HTTP 400" in backend._novita_error_detail(_ErrResponse(text=""))


def test_describe_novita_failure_flags_content_refusals(tmp_path):
    backend = _load_backend(tmp_path)
    # A refusal reads as a content-policy message regardless of which path
    # (submit HTTP error or task failure) produced it.
    submit = backend._describe_novita_failure("nsfw content detected", 400)
    poll = backend._describe_novita_failure("prompt flagged by moderation")
    assert "content policy" in submit.lower()
    assert "content policy" in poll.lower()
    # A genuine request error keeps its HTTP context and no refusal wording.
    other = backend._describe_novita_failure("width must be a multiple of 8", 400)
    assert "HTTP 400" in other
    assert "content policy" not in other.lower()


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


def test_chat_image_conceal_config_and_index_exposure(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    # Defaults to off; the chat widget reads the mode off the images index.
    assert client.get("/config").json()["chat_image_conceal"] == "off"
    assert client.get("/images").json()["chat_image_conceal"] == "off"

    assert client.put("/config", json={"chat_image_conceal": "sepia"}).status_code == 400

    resp = client.put("/config", json={"chat_image_conceal": "blackout"})
    assert resp.status_code == 200
    assert resp.json()["chat_image_conceal"] == "blackout"
    assert client.get("/images").json()["chat_image_conceal"] == "blackout"

    # An unknown stored value (hand-edited config) normalizes back to off.
    cfg = backend._load_config()
    cfg["chat_image_conceal"] = "bogus"
    backend._save_config(cfg)
    assert backend._load_config()["chat_image_conceal"] == "off"


def test_prompt_style_detection(tmp_path):
    backend = _load_backend(tmp_path)

    def cfg(base="", name="model.safetensors"):
        return {**backend._default_config(), "model_base": base, "model_name": name}

    assert backend._prompt_style(cfg("FLUX.1")) == "natural"
    assert backend._prompt_style(cfg("SDXL 1.0")) == "natural"
    assert backend._prompt_style(cfg("")) == "natural"
    assert backend._prompt_style(cfg("Pony")) == "tags"
    assert backend._prompt_style(cfg("Illustrious XL")) == "tags"
    assert backend._prompt_style(cfg("NoobAI XL")) == "tags"
    assert backend._prompt_style(cfg("Animagine XL 3.1")) == "tags"
    # sd_name fallback for configs saved before model_base existed
    assert backend._prompt_style(cfg("", "ponyDiffusionV6XL.safetensors")) == "tags"
    assert backend._prompt_style(cfg("", "noobaiXLNAIXL_epsilon.safetensors")) == "tags"
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
    assert "BOORU-STYLE TAGS" in captured["prompts"][0]
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
    assert "BOORU-STYLE TAGS" not in captured["prompts"][0]
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


def test_booru_subject_mode_default_and_rules(tmp_path):
    backend = _load_backend(tmp_path)
    assert backend._default_config()["booru_subject_mode"] == "auto"
    assert backend._default_config()["booru_break_separator"] is False

    # Tag models with no roster data: auto resolves to single.
    captured = {}
    cfg = {**backend._default_config(), "model_base": "Pony", "model_name": "m.safetensors"}
    asyncio.run(backend._write_image_prompt(
        cfg, "scene", "", _make_sdk(reply="1girl", captured=captured)))
    assert "SINGLE SUBJECT RULE" in captured["prompts"][0]
    assert "most relevant subject" in captured["prompts"][0]
    assert "MULTI-SUBJECT STRUCTURE" not in captured["prompts"][0]

    # Multi: the structured multi rule replaces the single rule.
    captured = {}
    asyncio.run(backend._write_image_prompt(
        {**cfg, "booru_subject_mode": "multi"}, "scene", "",
        _make_sdk(reply="2girls", captured=captured)))
    assert "SINGLE SUBJECT RULE" not in captured["prompts"][0]
    assert "MULTI-SUBJECT STRUCTURE" in captured["prompts"][0]

    # Natural-language models get neither rule, whatever the mode says.
    captured = {}
    flux = {**backend._default_config(), "model_base": "FLUX.1",
            "model_name": "m.safetensors", "booru_subject_mode": "multi"}
    asyncio.run(backend._write_image_prompt(flux, "scene", "", _make_sdk(captured=captured)))
    assert "SINGLE SUBJECT RULE" not in captured["prompts"][0]
    assert "MULTI-SUBJECT STRUCTURE" not in captured["prompts"][0]


def test_booru_multi_subject_rule_structure(tmp_path):
    backend = _load_backend(tmp_path)
    captured = {}
    cfg = {**backend._default_config(), "model_base": "Illustrious XL",
           "model_name": "m.safetensors", "booru_subject_mode": "multi"}
    asyncio.run(backend._write_image_prompt(
        cfg, "scene", "", _make_sdk(reply="2girls", captured=captured)))
    prompt = captured["prompts"][0]
    assert "MULTI-SUBJECT STRUCTURE" in prompt
    assert "ONE CONTIGUOUS tag group" in prompt
    assert "2girls, 1boy 1girl" in prompt          # count-combo examples
    assert "side-by-side" in prompt                # interaction tags
    assert "BREAK" not in prompt                   # separator is opt-in


def test_booru_break_separator_opt_in(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = {**backend._default_config(), "model_base": "Illustrious XL",
           "model_name": "m.safetensors", "booru_subject_mode": "multi",
           "booru_break_separator": True}

    captured = {}
    asyncio.run(backend._write_image_prompt(
        cfg, "scene", "", _make_sdk(reply="2girls", captured=captured)))
    assert "uppercase word BREAK" in captured["prompts"][0]

    # Single mode: no character groups to separate.
    captured = {}
    asyncio.run(backend._write_image_prompt(
        {**cfg, "booru_subject_mode": "single"}, "scene", "",
        _make_sdk(reply="1girl", captured=captured)))
    assert "BREAK" not in captured["prompts"][0]

    # Natural-language models never see it.
    captured = {}
    flux = {**cfg, "model_base": "FLUX.1"}
    asyncio.run(backend._write_image_prompt(flux, "scene", "", _make_sdk(captured=captured)))
    assert "BREAK" not in captured["prompts"][0]


def test_booru_subject_mode_reshapes_character_block(tmp_path):
    backend = _load_backend(tmp_path)
    characters = {"player": {"name": "Ash", "descriptor": "female elf; silver hair"},
                  "npcs": [{"name": "Borin", "descriptor": "male human; tall and scarred"}]}
    cfg = {**backend._default_config(), "model_base": "Pony", "model_name": "m.safetensors"}

    # Single: the roster stays (any character could be the pick) but conversion
    # is scoped to the single chosen subject.
    captured = {}
    asyncio.run(backend._write_image_prompt(
        {**cfg, "booru_subject_mode": "single"}, "scene", "",
        _make_sdk(reply="1girl", captured=captured), characters))
    prompt = captured["prompts"][0]
    assert "if the ONE subject you depict is listed below" in prompt
    assert "booru appearance tags" in prompt
    assert "- Ash (player character): female elf; silver hair" in prompt
    assert "- Borin: male human; tall and scarred" in prompt

    # Multi: the per-character tag-group contract, roster lines intact.
    captured = {}
    asyncio.run(backend._write_image_prompt(
        {**cfg, "booru_subject_mode": "multi"}, "scene", "",
        _make_sdk(reply="2boys", captured=captured), characters))
    prompt = captured["prompts"][0]
    assert "their own contiguous tag group" in prompt
    assert "Never merge two characters' traits" in prompt
    assert "- Ash (player character): female elf; silver hair" in prompt
    assert "- Borin: male human; tall and scarred" in prompt

    # Natural style keeps its own header regardless of the mode.
    captured = {}
    asyncio.run(backend._write_image_prompt(
        backend._default_config(), "scene", "", _make_sdk(captured=captured), characters))
    assert "depict them EXACTLY as described" in captured["prompts"][0]
    assert "ONE subject" not in captured["prompts"][0]


def test_booru_auto_mode_resolves_by_roster(tmp_path):
    backend = _load_backend(tmp_path)
    player = {"name": "Ash", "descriptor": "female elf; silver hair"}
    npc = {"name": "Borin", "descriptor": "male human; tall and scarred"}
    cfg = {**backend._default_config(), "model_base": "Illustrious XL",
           "model_name": "m.safetensors", "booru_subject_mode": "auto"}

    # Two tracked characters in frame: multi.
    captured = {}
    asyncio.run(backend._write_image_prompt(
        cfg, "scene", "", _make_sdk(reply="2girls", captured=captured),
        {"player": player, "npcs": [npc]}))
    assert "MULTI-SUBJECT STRUCTURE" in captured["prompts"][0]
    assert "SINGLE SUBJECT RULE" not in captured["prompts"][0]

    # One character, or no roster at all: single.
    for chars in ({"player": player, "npcs": []}, None):
        captured = {}
        asyncio.run(backend._write_image_prompt(
            cfg, "scene", "", _make_sdk(reply="1girl", captured=captured), chars))
        assert "SINGLE SUBJECT RULE" in captured["prompts"][0]

    # POV hides the player, so a player + one NPC roster counts as one.
    captured = {}
    asyncio.run(backend._write_image_prompt(
        {**cfg, "player_in_images": "pov"}, "scene", "",
        _make_sdk(reply="1boy", captured=captured), {"player": player, "npcs": [npc]}))
    assert "SINGLE SUBJECT RULE" in captured["prompts"][0]

    # Direct resolution: natural models opt out entirely; bogus stored values
    # fall back to single.
    flux = {**backend._default_config(), "model_base": "FLUX.1", "model_name": "m.safetensors"}
    assert backend._subject_mode(flux) == ""
    assert backend._subject_mode({**cfg, "booru_subject_mode": "crowd"}) == "single"


def test_booru_subject_mode_config_roundtrip(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    body = client.get("/config").json()
    assert body["booru_subject_mode"] == "auto"
    assert body["booru_break_separator"] is False
    assert body["booru_subject_modes"] == list(backend.BOORU_SUBJECT_MODES)

    resp = client.put("/config", json={"booru_subject_mode": "multi",
                                       "booru_break_separator": True})
    assert resp.status_code == 200
    assert resp.json()["booru_subject_mode"] == "multi"
    assert resp.json()["booru_break_separator"] is True
    assert backend._load_config()["booru_subject_mode"] == "multi"

    assert client.put("/config", json={"booru_subject_mode": "crowd"}).status_code == 400

    # The pre-mode boolean still works as a deprecated alias...
    assert client.put("/config", json={"booru_single_subject": True}).status_code == 200
    assert backend._load_config()["booru_subject_mode"] == "single"
    assert client.put("/config", json={"booru_single_subject": False}).status_code == 200
    assert backend._load_config()["booru_subject_mode"] == "multi"
    # ...but an explicit mode wins over it.
    client.put("/config", json={"booru_single_subject": True, "booru_subject_mode": "auto"})
    assert backend._load_config()["booru_subject_mode"] == "auto"


def test_booru_subject_mode_migration(tmp_path):
    backend = _load_backend(tmp_path)
    path = backend._data_dir() / "config.json"
    legacy = {k: v for k, v in backend._default_config().items()
              if k not in ("booru_subject_mode", "booru_break_separator")}

    # A stored pre-mode config keeps the choice the user made.
    backend._atomic_write_json(path, {**legacy, "booru_single_subject": True})
    assert backend._load_config()["booru_subject_mode"] == "single"
    backend._atomic_write_json(path, {**legacy, "booru_single_subject": False})
    assert backend._load_config()["booru_subject_mode"] == "multi"

    # An explicit stored mode wins over a lingering legacy bool.
    backend._atomic_write_json(
        path, {**legacy, "booru_single_subject": False, "booru_subject_mode": "auto"})
    assert backend._load_config()["booru_subject_mode"] == "auto"

    # An unknown stored value (hand-edited config) normalizes to single.
    backend._atomic_write_json(path, {**legacy, "booru_subject_mode": "bogus"})
    assert backend._load_config()["booru_subject_mode"] == "single"


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
    assert loras == [{"model_name": "my_upload.safetensors", "strength": 5.0}]

    # Weights beyond the unlocked -10..10 range (or garbage) clamp/default.
    cfg = _lora_cfg(backend, lora_library=[
        _lora(id="1", strength=50.0), _lora(id="2", strength=-50.0),
        _lora(id="3", strength=-1.2), _lora(id="4", strength="junk")])
    loras = backend._novita_payload(cfg, "x")["request"]["loras"]
    assert [entry["strength"] for entry in loras] == [10.0, -10.0, -1.2, 0.7]

    many = [_lora(id=str(i)) for i in range(8)]
    cfg = _lora_cfg(backend, lora_library=many)
    assert len(backend._novita_payload(cfg, "x")["request"]["loras"]) == backend.SD_LORAS_MAX


def test_parse_condition_reply(tmp_path):
    backend = _load_backend(tmp_path)
    # New object form: number -> weight (clamped to the allowed range).
    assert backend._parse_condition_reply('{"1": 0.7, "3": 1.2}') == {1: 0.7, 3: 1.2}
    assert backend._parse_condition_reply('Sure: {"2": -50}') == {2: -10.0}
    assert backend._parse_condition_reply('{"1": "high"}') == {1: None}
    assert backend._parse_condition_reply("{}") == {}
    # Legacy bare-array form still parses (weight unspecified).
    assert backend._parse_condition_reply("[1, 3]") == {1: None, 3: None}
    assert backend._parse_condition_reply("The answer is [2].") == {2: None}
    assert backend._parse_condition_reply("[]") == {}
    assert backend._parse_condition_reply("none of them") is None
    assert backend._parse_condition_reply("") is None
    assert backend._parse_condition_reply('{"one": 0.7}') is None


def test_lora_condition_gate(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, lora_library=[
        _lora(id="1", condition="a battle is happening"),
        _lora(id="2", condition="the scene is set at night"),
        _lora(id="3"),                                    # no condition: untouched
        _lora(id="4", active=False, condition="ignored"), # inactive: not asked
    ])

    captured = {}
    sdk = _make_sdk(reply="[2]", captured=captured)
    gated = asyncio.run(backend._apply_lora_conditions(cfg, "Moonlit rooftops.", sdk))
    assert [e["id"] for e in gated["lora_library"]] == ["2", "3", "4"]
    assert cfg["lora_library"] != gated["lora_library"]  # original untouched
    assert captured["preferences"] == ["fastest"]
    prompt = captured["prompts"][0]
    assert "Moonlit rooftops." in prompt
    assert "1. [GATED, weight 0.7] condition: a battle is happening" in prompt
    assert "2. [GATED, weight 0.7] condition: the scene is set at night" in prompt
    assert "ignored" not in prompt

    # Fail open: LLM error, unparseable reply, or no sdk keep everything.
    async def boom(prompt, model_preference="balanced", max_tokens=None):
        raise RuntimeError("llm down")
    broken = SimpleNamespace(llm=SimpleNamespace(generate=boom, _current_module=""))
    assert asyncio.run(backend._apply_lora_conditions(cfg, "x", broken)) is cfg
    assert asyncio.run(backend._apply_lora_conditions(
        cfg, "x", _make_sdk(reply="both feel right"))) is cfg
    assert asyncio.run(backend._apply_lora_conditions(cfg, "x", None)) is cfg

    # No conditional loras: the LLM is never called.
    captured2 = {}
    plain = _lora_cfg(backend, lora_library=[_lora(id="3")])
    assert asyncio.run(backend._apply_lora_conditions(
        plain, "x", _make_sdk(captured=captured2))) is plain
    assert "prompts" not in captured2


def test_lora_condition_gets_character_sheets(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, lora_library=[
        _lora(id="1", condition="Elara is in the scene")])
    characters = {
        "player": {"name": "Rin", "descriptor": "female human; red cloak"},
        "npcs": [{"name": "Elara", "descriptor": "female elf; silver hair"}],
    }

    captured = {}
    asyncio.run(backend._apply_lora_conditions(
        cfg, "The sorceress smiles.", _make_sdk(reply="[1]", captured=captured),
        characters))
    prompt = captured["prompts"][0]
    assert "CHARACTERS PRESENT" in prompt
    assert "- Rin (player character): female human; red cloak" in prompt
    assert "- Elara: female elf; silver hair" in prompt
    # The sheets sit between the scene and the adapter list.
    assert prompt.index("The sorceress") < prompt.index("CHARACTERS PRESENT")
    assert prompt.index("CHARACTERS PRESENT") < prompt.index("condition: Elara is in the scene")

    # No snapshot (or an empty one): the block disappears entirely.
    for chars in (None, {"player": None, "npcs": []}):
        captured = {}
        asyncio.run(backend._apply_lora_conditions(
            cfg, "x", _make_sdk(reply="[1]", captured=captured), chars))
        assert "CHARACTERS PRESENT" not in captured["prompts"][0]
        assert "{characters}" not in captured["prompts"][0]


def test_lora_llm_mode_derivation(tmp_path):
    backend = _load_backend(tmp_path)
    # Explicit modes win; gating without text degrades (nothing to decide).
    assert backend._entry_llm_mode(_lora(llm_mode="weight", condition="x")) == "weight"
    assert backend._entry_llm_mode(_lora(llm_mode="gate", condition="x")) == "gate"
    assert backend._entry_llm_mode(_lora(llm_mode="gate")) == "off"
    assert backend._entry_llm_mode(_lora(llm_mode="both")) == "weight"
    # Legacy entries (no llm_mode): condition meant gate, llm_weight meant weight.
    assert backend._entry_llm_mode(_lora(condition="x")) == "gate"
    assert backend._entry_llm_mode(_lora(llm_weight=True)) == "weight"
    assert backend._entry_llm_mode(_lora(llm_weight=True, condition="x")) == "both"
    assert backend._entry_llm_mode(_lora()) == "off"
    assert backend._entry_llm_mode(_lora(llm_mode="sometimes")) == "off"


def test_lora_llm_mode_weight_is_not_gated(tmp_path):
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, lora_library=[
        _lora(id="1", llm_mode="weight", condition="0.2 calm, 1.5 battle"),
        _lora(id="2", llm_mode="both", condition="only during storms"),
        _lora(id="3", llm_mode="gate", condition="at night"),
        _lora(id="4", llm_mode="gate"),  # no text: degrades to off, not asked
    ])

    captured = {}
    sdk = _make_sdk(reply='{"1": 1.3}', captured=captured)
    gated = asyncio.run(backend._apply_lora_conditions(cfg, "A calm morning.", sdk))
    by_id = {e["id"]: e for e in gated["lora_library"]}
    # Weight mode's text is instructions, NOT a gate: entry 1 gets its weight;
    # entries 2 and 3 were omitted from the reply, so their gates drop them.
    assert sorted(by_id) == ["1", "4"]
    assert by_id["1"]["strength"] == 1.3

    # Every adapter line opens with the mode label the prompt header defines.
    prompt = captured["prompts"][0]
    assert ("1. [ALWAYS APPLIES, pick the weight, default 0.7] "
            "instructions: 0.2 calm, 1.5 battle") in prompt
    assert ("2. [GATED, pick the weight if it applies, default 0.7] "
            "condition: only during storms") in prompt
    assert "3. [GATED, weight 0.7] condition: at night" in prompt
    assert "4." not in prompt
    # The header explains each label to the gate LLM.
    assert "[GATED, weight W]" in prompt
    assert "[ALWAYS APPLIES, pick the weight]" in prompt
    assert "[GATED, pick the weight if it applies]" in prompt

    # Omitted weight-only entries fail open to the slider value.
    gated = asyncio.run(backend._apply_lora_conditions(
        cfg, "x", _make_sdk(reply="{}")))
    assert [e["id"] for e in gated["lora_library"]] == ["1", "4"]
    assert all(e["strength"] == 0.7 for e in gated["lora_library"])


def test_lora_llm_weight_legacy_fields(tmp_path):
    # Entries saved before explicit llm_mode keep working via derivation.
    backend = _load_backend(tmp_path)
    cfg = _lora_cfg(backend, lora_library=[
        _lora(id="1", llm_weight=True, condition="stronger in battles"),
        _lora(id="2", llm_weight=True),                    # no condition: always applies
        _lora(id="3", condition="at night", strength=0.5), # gate only: weight stays put
        _lora(id="4"),                                     # neither: not asked
    ])

    captured = {}
    sdk = _make_sdk(reply='{"1": 1.4, "2": -60, "3": 9.9}', captured=captured)
    gated = asyncio.run(backend._apply_lora_conditions(cfg, "A duel at midnight.", sdk))
    by_id = {e["id"]: e for e in gated["lora_library"]}
    assert by_id["1"]["strength"] == 1.4
    assert by_id["2"]["strength"] == -10.0  # clamped to the allowed range
    assert by_id["3"]["strength"] == 0.5    # gate-only: LLM weight ignored
    assert by_id["4"]["strength"] == 0.7
    assert cfg["lora_library"][0]["strength"] == 0.7  # original untouched

    # The prompt marks weight-picking adapters and lists defaults.
    prompt = captured["prompts"][0]
    assert ("1. [GATED, pick the weight if it applies, default 0.7] "
            "condition: stronger in battles") in prompt
    assert "2. [ALWAYS APPLIES, pick the weight, default 0.7]" in prompt
    assert "3. [GATED, weight 0.5] condition: at night" in prompt
    assert "4." not in prompt

    # A weight of 0 drops the lora for this image; conditional loras omitted
    # from the reply are dropped, weight-only ones fail open to their slider.
    gated = asyncio.run(backend._apply_lora_conditions(
        cfg, "x", _make_sdk(reply='{"1": 0}')))
    ids = [e["id"] for e in gated["lora_library"]]
    assert ids == ["2", "4"]
    assert {e["id"]: e["strength"] for e in gated["lora_library"]} == {"2": 0.7, "4": 0.7}

    # Legacy array reply: gates still work, weights stay at the slider value.
    gated = asyncio.run(backend._apply_lora_conditions(
        cfg, "x", _make_sdk(reply="[1, 2]")))
    assert [e["id"] for e in gated["lora_library"]] == ["1", "2", "4"]
    assert all(e["strength"] == 0.7 for e in gated["lora_library"])


def test_lora_condition_patch_roundtrip(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    cfg = backend._load_config()
    cfg["lora_library"] = [_lora(id="1")]
    backend._save_config(cfg)

    client = _client(backend)
    body = client.patch("/loras/1", json={"condition": "  during storms  "}).json()
    assert body["entry"]["condition"] == "during storms"
    assert backend._load_config()["lora_library"][0]["condition"] == "during storms"

    # Long conditions are stored intact (LLM input is never capped), and
    # clearing works.
    long = "x" * 1000
    body = client.patch("/loras/1", json={"condition": long}).json()
    assert body["entry"]["condition"] == long
    body = client.patch("/loras/1", json={"condition": ""}).json()
    assert body["entry"]["condition"] == ""


def test_lora_trained_words_patch_roundtrip(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    cfg = backend._load_config()
    cfg["lora_library"] = [_lora(id="1")]
    backend._save_config(cfg)

    client = _client(backend)
    # Edited words are trimmed, blanks dropped, and persisted.
    body = client.patch(
        "/loras/1", json={"trained_words": ["  glowing runes ", "", "ornate armor"]}).json()
    assert body["entry"]["trained_words"] == ["glowing runes", "ornate armor"]
    assert backend._load_config()["lora_library"][0]["trained_words"] == [
        "glowing runes", "ornate armor"]

    # Clearing works, and the list is capped.
    assert client.patch("/loras/1", json={"trained_words": []}).json()["entry"]["trained_words"] == []
    body = client.patch("/loras/1", json={"trained_words": [str(i) for i in range(40)]}).json()
    assert len(body["entry"]["trained_words"]) == 20


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
    assert body["max_slots"] == backend.NOVITA_UPLOAD_SLOTS


def test_lora_download_redirects_with_token(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend, civitai_api_key="civkey123")
    cfg = backend._load_config()
    cfg["lora_library"] = [
        _lora(id="1"),
        _lora(id="2", download_url=""),
    ]
    backend._save_config(cfg)

    client = _client(backend)
    resp = client.get("/loras/1/download", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == (
        "https://civitai.com/api/download/models/123456?token=civkey123")

    assert client.get("/loras/2/download", follow_redirects=False).status_code == 404
    assert client.get("/loras/nope/download", follow_redirects=False).status_code == 404


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
    assert entry["llm_mode"] == "off"

    assert client.post("/loras", json=item).status_code == 409  # dedupe

    resp = client.patch("/loras/123456", json={"active": True, "strength": 1.7})
    assert resp.status_code == 200
    assert resp.json()["entry"]["strength"] == 1.7  # beyond 1 is fine now

    resp = client.patch("/loras/123456", json={"strength": -12})
    assert resp.json()["entry"]["strength"] == -10.0  # clamped to the range

    for mode in ("gate", "weight", "both", "off"):
        resp = client.patch("/loras/123456", json={"llm_mode": mode})
        assert resp.json()["entry"]["llm_mode"] == mode
    assert client.patch("/loras/123456", json={"llm_mode": "sometimes"}).status_code == 400
    resp = client.patch("/loras/123456", json={"strength": 1.7})

    # Persisted: a fresh config load feeds the payload builder.
    cfg = backend._load_config()
    cfg.update({"model_name": "sd_xl_base_1.0.safetensors", "model_base": "SDXL 1.0"})
    assert backend._novita_payload(cfg, "x")["request"]["loras"] == [
        {"model_name": "found_123456.safetensors", "strength": 1.7}]

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


def test_generate_retry_replaces_failed_record(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    _fake_novita(backend)
    client = _client(backend)

    asyncio.run(backend._append_record({
        "id": "mystory_3_deadbeef", "save_id": "mystory", "turn": 3,
        "status": "error", "error": "Novita generation failed",
        "narration_excerpt": "A merchant waves you over.",
        "image_prompt": "a smiling merchant", "filename": None,
    }))

    resp = client.post("/generate", json={"retry_record_id": "mystory_3_deadbeef"})
    assert resp.status_code == 200
    record_id = resp.json()["record_id"]

    for _ in range(100):
        record = next((r for r in backend._read_index() if r["id"] == record_id), None)
        if record and record["status"] in ("done", "error"):
            break
        import time as _t
        _t.sleep(0.02)
    assert record["status"] == "done"
    # The replacement keeps the failed record's message anchor and prompt...
    assert record["save_id"] == "mystory"
    assert record["turn"] == 3
    assert record["narration_excerpt"] == "A merchant waves you over."
    assert record["image_prompt"] == "a smiling merchant"
    # ...and the failed record is gone, so the footer never shows both.
    assert all(r["id"] != "mystory_3_deadbeef" for r in backend._read_index())


def test_generate_retry_regenerates_done_record_and_deletes_file(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    _fake_novita(backend, image_bytes=b"newimage")
    client = _client(backend)

    old_file = backend._data_dir() / "images" / "mystory_3_00000000.jpg"
    old_file.write_bytes(b"oldimage")
    asyncio.run(backend._append_record({
        "id": "mystory_3_00000000", "save_id": "mystory", "turn": 3,
        "status": "done", "error": None,
        "narration_excerpt": "A merchant waves you over.",
        "image_prompt": "a smiling merchant",
        "filename": "mystory_3_00000000.jpg",
    }))

    resp = client.post("/generate", json={"retry_record_id": "mystory_3_00000000"})
    assert resp.status_code == 200
    record_id = resp.json()["record_id"]

    for _ in range(100):
        record = next((r for r in backend._read_index() if r["id"] == record_id), None)
        if record and record["status"] in ("done", "error"):
            break
        import time as _t
        _t.sleep(0.02)
    # The regenerated image keeps the anchor and prompt; the replaced record
    # and its file are gone.
    assert record["status"] == "done"
    assert record["narration_excerpt"] == "A merchant waves you over."
    assert record["image_prompt"] == "a smiling merchant"
    assert all(r["id"] != "mystory_3_00000000" for r in backend._read_index())
    assert not old_file.exists()
    assert (backend._data_dir() / "images" / record["filename"]).read_bytes() == b"newimage"


def test_generate_endpoint_refine_runs_prompt_writer(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    _fake_novita(backend)
    captured = {}
    sdk = _make_sdk(reply="a refined moonlit castle", captured=captured)
    backend.set_services({"data_dir": str(tmp_path),
                          "engine": SimpleNamespace(sdk=sdk)})
    client = _client(backend)

    resp = client.post("/generate", json={"prompt_override": "castle by the sea",
                                          "refine": True})
    assert resp.status_code == 200
    record_id = resp.json()["record_id"]

    for _ in range(100):
        record = next((r for r in backend._read_index() if r["id"] == record_id), None)
        if record and record["status"] in ("done", "error"):
            break
        import time as _t
        _t.sleep(0.02)
    assert record["status"] == "done"
    # The typed text became the scene; the LLM's output became the prompt.
    assert record["narration_excerpt"] == "castle by the sea"
    assert record["image_prompt"] == "a refined moonlit castle"
    assert "castle by the sea" in captured["prompts"][0]
    assert captured["preferences"] == ["smartest"]


# ---------------------------------------------------------------------------
# Hugging Face LoRA source + Novita-availability browse badges
# ---------------------------------------------------------------------------

def test_flatten_hf_model(tmp_path):
    backend = _load_backend(tmp_path)
    raw = {
        "id": "XLabs-AI/flux-RealismLora",
        "downloads": 1200, "likes": 34,
        "lastModified": "2026-01-01T00:00:00.000Z",
        "tags": ["lora", "base_model:black-forest-labs/FLUX.1-dev",
                 "not-for-all-audiences"],
        "cardData": {"instance_prompt": "realism"},
        "siblings": [
            {"rfilename": "preview.png"},
            {"rfilename": "lora.safetensors",
             "lfs": {"sha256": "ABCDEF123", "size": 2048000}},
        ],
        "gated": False,
    }
    flat = backend._flatten_hf_model(raw)
    assert flat["id"] == "hf:XLabs-AI__flux-RealismLora"
    assert flat["source"] == "hf"
    assert flat["repo_id"] == "XLabs-AI/flux-RealismLora"
    assert flat["name"] == "flux-RealismLora"
    assert flat["creator"] == "XLabs-AI"
    assert flat["base_model"] == "Flux.1 D"
    assert flat["sha256"] == "abcdef123"       # lowercased for the hash index
    assert flat["all_hashes"] == ["abcdef123"]
    assert flat["download_url"] == (
        "https://huggingface.co/XLabs-AI/flux-RealismLora/resolve/main/lora.safetensors")
    assert flat["page_url"] == "https://huggingface.co/XLabs-AI/flux-RealismLora"
    assert flat["civitai_url"] == ""
    assert flat["thumb_url"].endswith("/resolve/main/preview.png")
    assert flat["nsfw"] is True
    assert flat["gated"] is False
    assert flat["trained_words"] == ["realism"]
    assert flat["size_kb"] == 2000.0
    assert flat["stats"] == {"downloads": 1200, "likes": 34}

    # Repos without a .safetensors file are not LoRAs we can use.
    assert backend._flatten_hf_model(
        {"id": "a/b", "siblings": [{"rfilename": "weights.bin"}]}) is None
    # HF's gated field is False or "auto"/"manual"; private counts too.
    assert backend._flatten_hf_model({**raw, "gated": "auto"})["gated"] is True
    assert backend._flatten_hf_model({**raw, "private": True})["gated"] is True


def test_hf_pick_safetensors_multiple_files(tmp_path):
    backend = _load_backend(tmp_path)
    primary, hashes, count = backend._hf_pick_safetensors([
        {"rfilename": "small.safetensors", "lfs": {"sha256": "AAA", "size": 10}},
        {"rfilename": "big.safetensors", "lfs": {"sha256": "BBB", "size": 100}},
        {"rfilename": "readme.md"},
    ])
    assert primary["rfilename"] == "big.safetensors"   # largest wins
    assert hashes == ["aaa", "bbb"]                    # but every hash is kept
    assert count == 2

    # Listing responses carry no lfs info: first file, no hashes yet.
    primary, hashes, count = backend._hf_pick_safetensors(
        [{"rfilename": "a.safetensors"}, {"rfilename": "b.safetensors"}])
    assert primary["rfilename"] == "a.safetensors"
    assert hashes == []
    assert count == 2

    assert backend._hf_pick_safetensors([]) == (None, [], 0)


def test_hf_base_model_mapping(tmp_path):
    backend = _load_backend(tmp_path)
    cases = {
        "base_model:black-forest-labs/FLUX.1-dev": "Flux.1 D",
        "base_model:black-forest-labs/FLUX.2-dev": "Flux.2 D",
        "base_model:stabilityai/stable-diffusion-xl-base-1.0": "SDXL 1.0",
        "base_model:runwayml/stable-diffusion-v1-5": "SD 1.5",
        "base_model:AstraliteHeart/pony-diffusion-v6": "Pony",
        "base_model:OnomaAIResearch/Illustrious-xl-early-release-v0": "Illustrious",
        "base_model:Laxhar/noobai-XL-1.0": "NoobAI",
    }
    for tag, expected in cases.items():
        assert backend._hf_base_model_name([tag]) == expected
        # Every mapped name must land in a family the downstream logic knows.
        assert backend._base_family(expected) != ""
    # Every family offered as a search filter must map back to itself.
    for name in backend.HF_BASE_MODELS:
        assert backend._hf_base_model_name([backend.HF_BASE_MODELS[name]]) == name
    assert backend._hf_base_model_name(["lora", "text-to-image"]) == ""
    # Unknown bases pass through raw (family "" -> never usable, same as
    # unknown Civitai bases).
    assert backend._hf_base_model_name(["base_model:foo/bar"]) == "foo/bar"


class _FakeHfResponse:
    def __init__(self, body, links=None, status_code=200):
        self.status_code = status_code
        self._body = body
        self.links = links or {}
        self.text = ""

    def json(self):
        return self._body


def test_hf_loras_endpoint(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)
    seen = {}

    listing = [
        {"id": "artist/style-lora", "downloads": 10, "likes": 2,
         "tags": ["lora", "base_model:stabilityai/stable-diffusion-xl-base-1.0"],
         "siblings": [{"rfilename": "style.safetensors"}]},
        {"id": "artist/spicy-lora", "downloads": 5, "likes": 1,
         "tags": ["lora", "not-for-all-audiences",
                  "base_model:stabilityai/stable-diffusion-xl-base-1.0"],
         "siblings": [{"rfilename": "spicy.safetensors"}]},
        {"id": "artist/no-files", "tags": ["lora"],
         "siblings": [{"rfilename": "README.md"}]},
    ]

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            if url.endswith("/api/models"):
                seen["params"] = list(params)
                return _FakeHfResponse(
                    listing,
                    links={"next": {"url": backend.HF_API_BASE + "/models?cursor=abc"}})
            repo = url.split("/api/models/")[1]
            seen.setdefault("details", []).append(repo)
            return _FakeHfResponse({
                "id": repo, "downloads": 10, "likes": 2,
                "tags": ["lora", "base_model:stabilityai/stable-diffusion-xl-base-1.0"],
                "siblings": [{"rfilename": "style.safetensors",
                              "lfs": {"sha256": FULL_HASH, "size": 4096}}],
            })

    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        resp = client.get("/hf/loras", params={
            "query": "style", "base_model": "SDXL 1.0", "sort": "Most Liked"})
        assert resp.status_code == 200
        body = resp.json()
        params = seen["params"]
        assert ("filter", "lora") in params
        assert ("filter", backend.HF_BASE_MODELS["SDXL 1.0"]) in params
        assert ("sort", "likes") in params and ("direction", "-1") in params
        assert ("full", "true") in params and ("search", "style") in params
        assert body["next_cursor"] == backend.HF_API_BASE + "/models?cursor=abc"

        # nsfw defaults to off (dropping the tagged repo, no key needed) and
        # the fileless repo never shows; the survivor is hash-enriched.
        assert [i["id"] for i in body["items"]] == ["hf:artist__style-lora"]
        item = body["items"][0]
        assert item["sha256"] == FULL_HASH
        assert seen["details"] == ["artist/style-lora"]
        # No Novita index on disk: availability is unknown, not wrong.
        assert item["novita_available"] is None

        resp = client.get("/hf/loras", params={"nsfw": "only"})
        assert [i["id"] for i in resp.json()["items"]] == ["hf:artist__spicy-lora"]

        # Pagination cursors are full Hub URLs; anything else is refused.
        resp = client.get("/hf/loras", params={"cursor": "https://evil.example/x"})
        assert resp.status_code == 502
        assert "cursor" in resp.json()["detail"].lower()
    finally:
        httpx.AsyncClient = original


def test_hf_save_roundtrip_and_novita_match(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    client = _client(backend)

    async def fake_list(cfg, query, cursor, limit, types="checkpoint", visibility=""):
        return {"models": [
            {"hash_sha256": NOVITA_HASH, "sd_name_in_api": "mirrored_style.safetensors",
             "status": 1}], "pagination": {}}

    backend._novita_list_models = fake_list

    item = {"id": "hf:artist__style-lora", "source": "hf",
            "repo_id": "artist/style-lora", "name": "style-lora",
            "base_model": "SDXL 1.0", "sha256": FULL_HASH,
            "download_url": "https://huggingface.co/artist/style-lora/resolve/main/style.safetensors",
            "page_url": "https://huggingface.co/artist/style-lora"}
    resp = client.post("/loras", json=item)
    assert resp.status_code == 200
    entry = resp.json()["entry"]
    assert entry["source"] == "hf"
    assert entry["page_url"] == "https://huggingface.co/artist/style-lora"
    assert entry["novita"] == {"sd_name_in_api": "mirrored_style.safetensors"}

    # The hf: id survives the path routes, and the entry feeds the SD payload.
    assert client.patch(f"/loras/{item['id']}",
                        json={"active": True}).status_code == 200
    cfg = backend._load_config()
    cfg.update({"model_name": "sd_xl_base_1.0.safetensors", "model_base": "SDXL 1.0"})
    assert backend._novita_payload(cfg, "x")["request"]["loras"] == [
        {"model_name": "mirrored_style.safetensors", "strength": 0.7}]
    assert client.delete(f"/loras/{item['id']}").status_code == 200


def test_hf_flux_download_link_gets_no_civitai_token(tmp_path):
    backend = _load_backend(tmp_path)
    hf_url = "https://huggingface.co/a/b/resolve/main/l.safetensors"
    cfg = _lora_cfg(backend, model_name=backend.FLUX2_MODEL_NAME, model_base="",
                    civitai_api_key="civkey",
                    lora_library=[
                        _lora(id="hf1", source="hf", base_model="Flux.1 D",
                              novita=None, download_url=hf_url),
                        # Legacy entry without a source field: still Civitai.
                        _lora(id="civ", base_model="Flux.2 D", novita=None),
                    ])
    urls = backend._flux2_payload(cfg, "x")["loras"]
    assert hf_url in urls  # untouched — no Civitai token leaked to HF
    assert "https://civitai.com/api/download/models/123456?token=civkey" in urls


def test_save_rejects_gated_hf(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)
    resp = client.post("/loras", json={
        "id": "hf:a__b", "source": "hf", "repo_id": "a/b", "name": "b",
        "base_model": "Flux.1 D", "gated": True,
        "download_url": "https://huggingface.co/a/b/resolve/main/l.safetensors"})
    assert resp.status_code == 400
    assert "ated" in resp.json()["detail"]
    assert backend._load_config()["lora_library"] == []


def test_browse_annotates_novita_availability(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    async def fake_search(cfg, **kwargs):
        return {"items": [
            {"id": "1", "base_model": "SDXL 1.0", "sha256": FULL_HASH, "all_hashes": []},
            {"id": "2", "base_model": "SDXL 1.0", "sha256": "9" * 64, "all_hashes": []},
            {"id": "3", "base_model": "Flux.1 D", "sha256": "", "all_hashes": []},
            {"id": "4", "base_model": "SDXL 1.0", "sha256": "", "all_hashes": []},
        ], "next_cursor": ""}

    backend._civitai_search_loras = fake_search

    # Fresh index on disk: definite yes/no per hash, straight from the cache.
    backend._atomic_write_json(backend._lora_index_path(), {
        "fetched_at": time.time(),
        "hashes": {NOVITA_HASH: "mirrored.safetensors"}})
    by_id = {i["id"]: i for i in client.get("/civitai/loras").json()["items"]}
    assert by_id["1"]["novita_available"] is True
    assert by_id["1"]["novita_sd_name"] == "mirrored.safetensors"
    assert by_id["2"]["novita_available"] is False
    assert "novita_available" not in by_id["3"]      # flux: UI shows "via link"
    assert by_id["4"]["novita_available"] is None    # no hashes to match

    # An expired index still answers badges (allow_stale).
    stale = json.loads(backend._lora_index_path().read_text(encoding="utf-8"))
    stale["fetched_at"] = time.time() - backend.NOVITA_LORA_INDEX_TTL_S - 1
    backend._lora_index_path().write_text(json.dumps(stale), encoding="utf-8")
    by_id = {i["id"]: i for i in client.get("/civitai/loras").json()["items"]}
    assert by_id["1"]["novita_available"] is True

    # No index at all (and no Novita key -> no background build): unknown,
    # and the browse response never waits on a catalog sync.
    backend._lora_index_path().unlink()
    by_id = {i["id"]: i for i in client.get("/civitai/loras").json()["items"]}
    assert by_id["1"]["novita_available"] is None
    assert by_id["2"]["novita_available"] is None


def test_keys_hf_provider(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    async def ok(key):
        return True

    backend._validate_hf_key = ok
    resp = client.post("/keys/hf", json={"api_key": " hftok99 "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_hf_key"] is True
    assert "hftok99" not in resp.text  # masked in the response
    assert backend._load_config()["hf_api_key"] == "hftok99"

    async def bad(key):
        return False

    backend._validate_hf_key = bad
    resp = client.post("/keys/hf", json={"api_key": "nope"})
    assert resp.status_code == 400
    assert "Hugging Face" in resp.json()["detail"]
    assert backend._load_config()["hf_api_key"] == "hftok99"  # unchanged

    async def down(key):
        raise RuntimeError("Could not reach Hugging Face: timeout")

    backend._validate_hf_key = down
    assert client.post("/keys/hf", json={"api_key": "x"}).status_code == 502

    # Masked round-trip through PUT /config keeps the stored key.
    masked = client.get("/config").json()["hf_api_key"]
    assert masked.startswith(backend.KEY_MASK_PREFIX)
    client.put("/config", json={"hf_api_key": masked})
    assert backend._load_config()["hf_api_key"] == "hftok99"


def test_download_endpoint_redirects_hf_without_token(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend, civitai_api_key="civkey")
    hf_url = "https://huggingface.co/a/b/resolve/main/l.safetensors"
    cfg = backend._load_config()
    cfg["lora_library"] = [
        _lora(id="hf1", source="hf", download_url=hf_url),
        _lora(id="123456"),  # legacy civitai entry
    ]
    backend._save_config(cfg)

    client = _client(backend)
    resp = client.get("/loras/hf1/download", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == hf_url  # no token appended

    resp = client.get("/loras/123456/download", follow_redirects=False)
    assert resp.headers["location"].endswith("?token=civkey")


# ---------------------------------------------------------------------------
# Character reference roster (consistent appearances across images)
# ---------------------------------------------------------------------------

def _npc(name, appearance="tall and scarred", introduced=True, status="active",
         traveling=False, last_turn=0, **extra):
    npc = {"name": name, "race": "human", "gender": "male",
           "appearance": appearance, "introduced": introduced, "status": status,
           "traveling_with_player": traveling, "last_interaction_turn": last_turn}
    npc.update(extra)
    return npc


def _char_state(player=True, npcs=None, **extra):
    state = _state(**extra)
    if player:
        state["characters"] = {"default_player": {
            "name": "Ash", "race": "elf", "gender": "female",
            "short_appearance": "silver hair, green cloak"}}
    if npcs is not None:
        state["module_data"] = {**state.get("module_data", {}),
                                "wb_npc_system": {"characters": npcs}}
    return state


def test_character_snapshot_selects_sorts_and_caps(tmp_path):
    backend = _load_backend(tmp_path)

    # Nothing to say without either source module.
    assert backend._character_snapshot(_state()) is None
    assert backend._character_snapshot(
        _char_state(player=False, npcs={})) is None

    # Player without any appearance text is skipped.
    state = _state(characters={"default_player": {"name": "Ash", "race": "elf"}})
    assert backend._character_snapshot(state) is None

    snap = backend._character_snapshot(_char_state(npcs={
        "n1": _npc("Borin", last_turn=5),
        "n2": _npc("Kira", traveling=True, last_turn=1),
        "n3": _npc("Ghost", status="dead"),
        "n4": _npc("Stranger", introduced=False, status="unintroduced"),
        "n5": _npc("Blank", appearance="  "),
        "n6": _npc("Recent", last_turn=9),
    }))
    assert snap["player"] == {"key": "player", "name": "Ash",
                              "descriptor": "female elf; silver hair, green cloak"}
    # Companions first, then most recently seen; dead/unmet/undescribed dropped.
    assert [n["name"] for n in snap["npcs"]] == ["Kira", "Recent", "Borin"]

    # full_appearance is the fallback when short_appearance is missing.
    state = _state(characters={"default_player": {
        "name": "Ash", "full_appearance": "a full paragraph of looks"}})
    snap = backend._character_snapshot(state)
    assert snap["player"]["descriptor"] == "a full paragraph of looks"

    # No token caps on LLM input context (see CLAUDE.md): the snapshot keeps
    # every present character with their full appearance text, and both the
    # prompt writer and the LoRA gate list them all.
    many = {f"n{i}": _npc(f"NPC{i}", appearance="x" * 500, last_turn=i)
            for i in range(10)}
    snap = backend._character_snapshot(_char_state(player=False, npcs=many))
    assert len(snap["npcs"]) == 10
    assert all("x" * 500 in n["descriptor"] for n in snap["npcs"])

    block = backend._character_block(backend._default_config(), snap)
    gate = backend._condition_character_block(snap)
    for i in range(10):
        assert f"- NPC{i}:" in block
        assert f"- NPC{i}:" in gate


def test_character_snapshot_uses_npc_scene_presence(tmp_path):
    backend = _load_backend(tmp_path)
    npcs = {
        "n1": _npc("Borin", id="n1", last_turn=9),  # recent but absent: dropped
        "n2": _npc("Kira", id="n2", last_turn=1),   # present per the roster
        "n3": _npc("Vex", id="n3", last_turn=2),    # named in the latest narration
    }

    state = _char_state(npcs=npcs, history=["Earlier.", "Vex steps from the shadows."])
    state["module_data"]["wb_npc_system"]["scene_presence"] = {"turn": 3, "npc_ids": ["n2"]}
    snap = backend._character_snapshot(state)
    assert [n["name"] for n in snap["npcs"]] == ["Vex", "Kira"]

    # An empty roster means nobody is present: only the player remains.
    state = _char_state(npcs=npcs, history=["The alley is empty."])
    state["module_data"]["wb_npc_system"]["scene_presence"] = {"turn": 3, "npc_ids": []}
    snap = backend._character_snapshot(state)
    assert snap["player"] and snap["npcs"] == []

    # A stale or malformed roster falls back to the recency heuristic.
    for presence in ({"turn": 99, "npc_ids": ["n2"]}, {"npc_ids": ["n2"]}, "junk"):
        state = _char_state(npcs=npcs)
        state["module_data"]["wb_npc_system"]["scene_presence"] = presence
        snap = backend._character_snapshot(state)
        assert [n["name"] for n in snap["npcs"]] == ["Borin", "Vex", "Kira"]

    # Name matching is whole-word: "Vexation" must not resurrect Vex.
    state = _char_state(npcs=npcs, history=["Pure vexation grips the crowd."])
    state["module_data"]["wb_npc_system"]["scene_presence"] = {"turn": 3, "npc_ids": []}
    assert backend._character_snapshot(state)["npcs"] == []


def test_character_snapshot_honors_manual_pin_and_npc_statuses(tmp_path):
    backend = _load_backend(tmp_path)
    npcs = {
        # Just placed by the player: the roster (computed before they existed)
        # cannot know them, but the fresh pin keeps them in.
        "n1": _npc("Sela", id="n1", presence_pinned_turn=3),
        "n2": _npc("Stale", id="n2", presence_pinned_turn=1),   # pin expired
        # Real NPC-system statuses, not the fictional "dead": both dropped
        # even with a fresh pin.
        "n3": _npc("Gone", id="n3", status="departed", presence_pinned_turn=3),
        "n4": _npc("Han", id="n4", status="deceased", presence_pinned_turn=3),
    }
    state = _char_state(npcs=npcs, history=["The pier is empty."])
    state["module_data"]["wb_npc_system"]["scene_presence"] = {"turn": 3, "npc_ids": []}
    snap = backend._character_snapshot(state)
    assert [n["name"] for n in snap["npcs"]] == ["Sela"]


def test_lora_gate_sees_all_known_characters(tmp_path):
    backend = _load_backend(tmp_path)
    npcs = {
        # In the scene per the roster.
        "n1": _npc("Kira", id="n1"),
        # Known but absent: kept for the LoRA gate, not the image prompt.
        "n2": _npc("Borin", id="n2"),
        # Active but never flagged introduced (activated from the browser
        # before the flags were synced): still known.
        "n3": _npc("Sela", id="n3", introduced=False),
    }
    state = _char_state(npcs=npcs, history=["The pier is empty."])
    state["module_data"]["wb_npc_system"]["scene_presence"] = {"turn": 3, "npc_ids": ["n1"]}

    snap = backend._character_snapshot(state)
    assert [n["name"] for n in snap["npcs"]] == ["Kira"]
    assert sorted(n["name"] for n in snap["all_npcs"]) == ["Borin", "Kira", "Sela"]

    # The LoRA gate lists every known character; the prompt writer's block
    # only those present in the scene.
    gate = backend._condition_character_block(snap)
    assert "- Kira:" in gate and "- Borin:" in gate and "- Sela:" in gate
    block = backend._character_block(backend._default_config(), snap)
    assert "- Kira:" in block
    assert "Borin" not in block and "Sela" not in block


def test_character_block_injected_in_both_styles(tmp_path):
    backend = _load_backend(tmp_path)
    characters = {"player": {"name": "Ash", "descriptor": "female elf; silver hair"},
                  "npcs": [{"name": "Borin", "descriptor": "male human; tall and scarred"}]}

    # Natural template.
    captured = {}
    sdk = _make_sdk(captured=captured)
    cfg = backend._default_config()
    asyncio.run(backend._write_image_prompt(cfg, "scene", "", sdk, characters))
    prompt = captured["prompts"][0]
    assert "KNOWN CHARACTERS" in prompt
    assert "- Ash (player character): female elf; silver hair" in prompt
    assert "- Borin: male human; tall and scarred" in prompt
    assert "POV RULE" not in prompt

    # Tags template converts descriptions into booru tags. The default auto
    # mode sees two characters in frame, so the multi contract applies and the
    # full roster still rides along.
    captured = {}
    sdk = _make_sdk(reply="1girl", captured=captured)
    cfg = {**backend._default_config(), "model_base": "Pony", "model_name": "m.safetensors"}
    asyncio.run(backend._write_image_prompt(cfg, "scene", "", sdk, characters))
    assert "booru appearance tags" in captured["prompts"][0]
    assert "their own contiguous tag group" in captured["prompts"][0]
    assert "- Ash (player character): female elf; silver hair" in captured["prompts"][0]
    assert "- Borin: male human; tall and scarred" in captured["prompts"][0]

    # Disabled or absent roster: no block.
    for cfg, chars in ((backend._default_config(), None),
                       ({**backend._default_config(),
                         "character_reference_enabled": False}, characters)):
        captured = {}
        asyncio.run(backend._write_image_prompt(
            cfg, "scene", "", _make_sdk(captured=captured), chars))
        assert "KNOWN CHARACTERS" not in captured["prompts"][0]


def test_pov_mode_hides_player_and_adds_conditional_rule(tmp_path):
    backend = _load_backend(tmp_path)
    characters = {"player": {"name": "Ash", "descriptor": "female elf; silver hair"},
                  "npcs": [{"name": "Borin", "descriptor": "tall and scarred"}]}
    cfg = {**backend._default_config(), "player_in_images": "pov"}

    captured = {}
    asyncio.run(backend._write_image_prompt(
        cfg, "scene", "", _make_sdk(captured=captured), characters))
    prompt = captured["prompts"][0]
    assert "POV RULE" in prompt
    # The player is never depicted in POV mode, so they stay out of the roster.
    assert "Ash" not in prompt
    assert "- Borin: tall and scarred" in prompt
    # First person is reserved for direct interaction; ordinary scenes just
    # leave the player out of frame with no forced viewpoint.
    assert "first person" in prompt
    assert "no forced first-person viewpoint" in prompt

    # POV with a player-only roster: no character list, just the rule.
    captured = {}
    asyncio.run(backend._write_image_prompt(
        cfg, "scene", "", _make_sdk(captured=captured),
        {"player": characters["player"], "npcs": []}))
    assert "KNOWN CHARACTERS" not in captured["prompts"][0]
    assert "POV RULE" in captured["prompts"][0]

    # Tags style asks for pov framing tags.
    captured = {}
    tag_cfg = {**cfg, "model_base": "Pony", "model_name": "m.safetensors"}
    asyncio.run(backend._write_image_prompt(
        tag_cfg, "scene", "", _make_sdk(reply="1boy", captured=captured), characters))
    assert "framing tags such as pov" in captured["prompts"][0]


def test_librarian_feeds_roster_end_to_end(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend, interval=1)
    _fake_novita(backend)
    captured = {}

    async def run():
        state = _char_state(npcs={"n1": _npc("Borin")}, turn=2,
                            data={"turns_since_image": 0})
        result = await backend.on_librarian(state, _make_sdk(captured=captured))
        await asyncio.gather(*backend._tasks)
        return result

    result = asyncio.run(run())
    assert result["module_data"][MID]["last_trigger"]
    prompt = captured["prompts"][0]
    assert "- Ash (player character): female elf; silver hair, green cloak" in prompt
    assert "- Borin:" in prompt
    record = backend._read_index()[0]
    assert record["characters"] == ["Ash", "Borin"]
    assert record["status"] == "done"


def test_studio_generation_carries_no_roster(tmp_path):
    backend = _load_backend(tmp_path)
    _enable(backend)
    _fake_novita(backend)
    captured = {}
    sdk = _make_sdk(reply="a castle", captured=captured)
    backend.set_services({"data_dir": str(tmp_path),
                          "engine": SimpleNamespace(sdk=sdk)})
    client = _client(backend)

    resp = client.post("/generate", json={"prompt_override": "castle by the sea",
                                          "refine": True})
    assert resp.status_code == 200
    record_id = resp.json()["record_id"]
    for _ in range(100):
        record = next((r for r in backend._read_index() if r["id"] == record_id), None)
        if record and record["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert record["status"] == "done"
    assert record["characters"] == []
    assert "KNOWN CHARACTERS" not in captured["prompts"][0]


# ---------------------------------------------------------------------------
# Precomputed character appearance tags (booru models)
# ---------------------------------------------------------------------------

def _tag_state(**extra):
    return _char_state(npcs={"n1": _npc("Borin", id="n1", last_turn=5),
                             "n2": _npc("Kira", id="n2", last_turn=1)}, **extra)


def test_tag_backfill_generates_caches_and_invalidates(tmp_path):
    backend = _load_backend(tmp_path)
    # interval high enough that no image generation fires alongside.
    _enable(backend, model_base="Pony", model_name="m.safetensors", interval=99)
    captured = {}
    sdk = _make_sdk(reply="silver hair, green eyes", captured=captured)

    async def run():
        await backend.on_librarian(_tag_state(), sdk)
        await asyncio.gather(*backend._tasks)

        cache = backend._read_tag_cache()["mystory"]
        assert sorted(cache) == ["n1", "n2", "player"]
        for entry in cache.values():
            assert entry["tags"] == "silver hair, green eyes"
            assert entry["hash"] and entry["updated_at"] and entry["name"]
        assert len(captured["prompts"]) == 3
        assert captured["preferences"] == ["smartest"] * 3
        for prompt in captured["prompts"]:
            assert "hair color" in prompt and "eye color" in prompt
            assert "STRICTLY EXCLUDE: clothing" in prompt
        assert any("female elf; silver hair, green cloak" in p
                   for p in captured["prompts"])

        # Warm cache: a second pass regenerates nothing.
        await backend.on_librarian(_tag_state(), sdk)
        await asyncio.gather(*backend._tasks)
        assert len(captured["prompts"]) == 3

        # The snapshot serves cached tags -- and drops them the moment the
        # appearance no longer matches the cached hash.
        state = _tag_state()
        state["module_data"]["wb_npc_system"]["characters"]["n1"]["appearance"] = "now bald"
        snap = backend._character_snapshot(state)
        borin = next(n for n in snap["all_npcs"] if n["name"] == "Borin")
        kira = next(n for n in snap["all_npcs"] if n["name"] == "Kira")
        assert "tags" not in borin
        assert kira["tags"] == "silver hair, green eyes"
        assert snap["player"]["tags"] == "silver hair, green eyes"

        # Only the changed character regenerates.
        await backend.on_librarian(state, sdk)
        await asyncio.gather(*backend._tasks)
        assert len(captured["prompts"]) == 4
        assert "now bald" in captured["prompts"][3]
        assert backend._read_tag_cache()["mystory"]["n1"]["hash"] == \
            backend._appearance_hash(borin["descriptor"])

        # Roster pruning: a dead NPC's entry is evicted on the next write.
        state = _tag_state()
        bank = state["module_data"]["wb_npc_system"]["characters"]
        bank["n2"]["status"] = "deceased"
        bank["n1"]["appearance"] = "shaved head"  # forces a cache write
        await backend.on_librarian(state, sdk)
        await asyncio.gather(*backend._tasks)
        assert "n2" not in backend._read_tag_cache()["mystory"]

    asyncio.run(run())


def test_tag_backfill_gating(tmp_path):
    backend = _load_backend(tmp_path)
    captured = {}
    sdk = _make_sdk(captured=captured)

    async def run():
        snapshot = backend._character_snapshot(_tag_state())
        # Natural-language checkpoint: no tag precomputation.
        _enable(backend)
        backend._spawn_tag_backfill("mystory", snapshot, sdk)
        # Booru checkpoint but character reference disabled.
        _enable(backend, model_base="Pony", character_reference_enabled=False)
        backend._spawn_tag_backfill("mystory", snapshot, sdk)
        # No API key / no model.
        _enable(backend, model_base="Pony", api_key="")
        backend._spawn_tag_backfill("mystory", snapshot, sdk)
        # No characters.
        _enable(backend, model_base="Pony")
        backend._spawn_tag_backfill("mystory", None, sdk)
        # A backfill already in flight: skipped, no double spawn.
        async with backend._get_tag_lock():
            backend._spawn_tag_backfill("mystory", snapshot, sdk)
        await asyncio.gather(*backend._tasks)

    asyncio.run(run())
    assert "prompts" not in captured
    assert backend._read_tag_cache() == {}


def test_character_block_serves_ready_tags(tmp_path):
    backend = _load_backend(tmp_path)
    tag_cfg = {**backend._default_config(),
               "model_base": "Pony", "model_name": "m.safetensors"}
    characters = {
        "player": {"key": "player", "name": "Ash",
                   "descriptor": "female elf; silver hair",
                   "tags": "silver hair, long hair, green eyes, pointy ears"},
        "npcs": [{"key": "n1", "name": "Borin",
                  "descriptor": "male human; tall and scarred"}],
    }

    block = backend._character_block(tag_cfg, characters)
    assert ("- Ash (player character): appearance tags (canonical, use verbatim): "
            "silver hair, long hair, green eyes, pointy ears | "
            "description: female elf; silver hair") in block
    # Untagged characters keep the plain descriptor line and contract.
    assert "- Borin: male human; tall and scarred" in block
    assert "include those tags VERBATIM" in block
    assert "clothing, pose, and expression" in block

    # With no tagged character in frame, the verbatim contract is absent.
    untagged = {**characters,
                "player": {k: v for k, v in characters["player"].items() if k != "tags"}}
    assert "VERBATIM" not in backend._character_block(tag_cfg, untagged)

    # Natural-language models never see tags.
    natural = backend._character_block(backend._default_config(), characters)
    assert "appearance tags" not in natural
    assert "- Ash (player character): female elf; silver hair" in natural

    # The LoRA gate keeps plain descriptors regardless.
    gate = backend._condition_character_block(
        {**characters, "all_npcs": characters["npcs"]})
    assert "appearance tags" not in gate
    assert "- Ash (player character): female elf; silver hair" in gate


def test_clean_character_tags(tmp_path):
    backend = _load_backend(tmp_path)
    raw = "```\n1girl, Masterpiece, score_9, Red Hair, red hair, scar across nose\n```"
    assert backend._clean_character_tags(raw) == "red hair, scar across nose"
    assert backend._clean_character_tags("") == ""
    # A reply of nothing but scene/quality tags is unusable.
    assert backend._clean_character_tags("solo, highres, score_8_up") == ""


def test_tag_cache_prunes_oldest_saves(tmp_path):
    backend = _load_backend(tmp_path)
    total = backend.TAG_CACHE_MAX_SAVES + 3
    cache = {f"s{i}": {"player": {"hash": "h", "tags": "t",
                                  "updated_at": f"2026-01-{i + 1:02d}"}}
             for i in range(total)}
    backend._write_tag_cache(cache)
    stored = backend._read_tag_cache()
    assert len(stored) == backend.TAG_CACHE_MAX_SAVES
    assert all(f"s{i}" not in stored for i in range(3))
    assert f"s{total - 1}" in stored


def test_player_in_images_config_validation(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    resp = client.put("/config", json={"player_in_images": "pov",
                                       "character_reference_enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["player_in_images"] == "pov"
    assert body["character_reference_enabled"] is False

    assert client.put("/config", json={"player_in_images": "invisible"}).status_code == 400
    assert backend._load_config()["player_in_images"] == "pov"

    # Junk values in a hand-edited config file degrade to "show".
    cfg = backend._load_config()
    cfg["player_in_images"] = "nonsense"
    backend._save_config(cfg)
    assert backend._load_config()["player_in_images"] == "show"


# ---------------------------------------------------------------------------
# Danbooru tag usage filter
# ---------------------------------------------------------------------------

_TAG_DICT = {
    "long_hair": 500000,
    "red_hair": 400000,
    "1boy": 300000,
    "forest": 200000,
    "sword_(weapon)": 20000,
    "boundary_tag": 100,
    "obscure_tag": 5,
}


def _filter_cfg(backend, mode="soft", min_count=100):
    return {**backend._default_config(),
            "tag_usage_filter": mode, "tag_usage_min_count": min_count}


def test_tag_usage_filter_modes(tmp_path):
    backend = _load_backend(tmp_path)
    backend._tag_dict_cache = dict(_TAG_DICT)
    text = "long hair, obscure_tag, made_up_tag, boundary_tag"

    off = backend._filter_tags_by_usage(text, _filter_cfg(backend, "off"))
    assert off == text

    soft = backend._filter_tags_by_usage(text, _filter_cfg(backend, "soft"))
    assert soft == "long hair, made_up_tag, boundary_tag"

    hard = backend._filter_tags_by_usage(text, _filter_cfg(backend, "hard"))
    # A tag exactly at the threshold is kept.
    assert hard == "long hair, boundary_tag"


def test_tag_usage_filter_normalization(tmp_path):
    backend = _load_backend(tmp_path)
    backend._tag_dict_cache = {**_TAG_DICT, "old_alias": 400000}

    # Spaces/case match underscore dictionary keys; escaped parens unescape;
    # attention-weight syntax is stripped for lookup but the original token
    # text survives in the output.
    text = "Long Hair, sword \\(weapon\\), (red hair:1.2), forest:0.8, obscure_tag"
    out = backend._filter_tags_by_usage(text, _filter_cfg(backend, "hard"))
    assert out == "Long Hair, sword \\(weapon\\), (red hair:1.2), forest:0.8"

    # An alias resolves to the canonical tag's count.
    assert backend._filter_tags_by_usage(
        "old_alias", _filter_cfg(backend, "hard")) == "old_alias"


def test_tag_usage_filter_preserves_specials(tmp_path):
    backend = _load_backend(tmp_path)
    backend._tag_dict_cache = dict(_TAG_DICT)
    cfg = _filter_cfg(backend, "hard", min_count=10_000_000)

    # BREAK standalone and embedded in a comma token both survive; the tags
    # around an embedded BREAK are still filtered individually.
    out = backend._filter_tags_by_usage(
        "long hair, BREAK, red hair BREAK 1boy, obscure_tag, score_9", cfg,
        whitelist=("long hair",))
    assert out == "long hair, BREAK, BREAK, score_9"

    # Trigger-word whitelist beats the dictionary, including phrases that
    # contain commas (split by the comma tokenizer).
    out = backend._filter_tags_by_usage(
        "sparklestyle, neon glow, forest", cfg,
        whitelist=("sparklestyle, neon glow",))
    assert out == "sparklestyle, neon glow"


def test_tag_usage_filter_fails_open(tmp_path):
    backend = _load_backend(tmp_path)

    # All dictionary files missing: prompt passes through unchanged.
    backend.TAG_DICT_FILES = (tmp_path / "missing.csv", tmp_path / "missing2.csv")
    text = "obscure_tag, made_up_tag"
    assert backend._filter_tags_by_usage(text, _filter_cfg(backend, "hard")) == text

    # Filtering that would drop every real tag keeps the prompt unfiltered.
    backend._tag_dict_cache = dict(_TAG_DICT)
    assert backend._filter_tags_by_usage(text, _filter_cfg(backend, "hard")) == text
    assert backend._filter_tags_by_usage(
        "obscure_tag BREAK made_up_tag", _filter_cfg(backend, "hard")) \
        == "obscure_tag BREAK made_up_tag"


def test_tag_dict_loader(tmp_path):
    backend = _load_backend(tmp_path)
    danbooru = tmp_path / "danbooru.csv"
    danbooru.write_text(
        "long_hair,0,500000,\"/lh,longhair\"\n"
        "malformed row without count\n"
        "longhair,0,7\n"          # canonical row spelled like the alias above
        "solo,0,5000954\n"
        "anthro,0,40\n",          # rare on danbooru, common on e621
        encoding="utf-8")
    e621 = tmp_path / "e621.csv"
    e621.write_text(
        "anthro,0,3381927,\"anthromorph\"\n"
        "long_hair,0,90000\n",    # lower count than danbooru: higher wins
        encoding="utf-8")
    backend.TAG_DICT_FILES = (danbooru, e621)

    usage = backend._tag_usage_dict()
    assert usage["long_hair"] == 500000      # max across sites
    assert usage["/lh"] == 500000            # alias -> canonical count
    assert usage["longhair"] == 7            # canonical row wins over alias
    assert usage["solo"] == 5000954
    assert usage["anthro"] == 3381927        # e621 count wins over danbooru's
    assert usage["anthromorph"] == 3381927
    assert "malformed" not in " ".join(usage)

    # Cached for the process lifetime: deleting the files changes nothing.
    danbooru.unlink()
    e621.unlink()
    assert backend._tag_usage_dict()["long_hair"] == 500000

    # One missing file still loads the others.
    backend2 = _load_backend(tmp_path)
    only = tmp_path / "only.csv"
    only.write_text("solo,0,5000954\n", encoding="utf-8")
    backend2.TAG_DICT_FILES = (tmp_path / "gone.csv", only)
    assert backend2._tag_usage_dict()["solo"] == 5000954


def test_prompt_writer_applies_tag_usage_filter(tmp_path):
    backend = _load_backend(tmp_path)
    backend._tag_dict_cache = dict(_TAG_DICT)
    reply = "long hair, obscure_tag, made_up_tag, forest"

    cfg = {**backend._default_config(), "model_base": "Pony",
           "tag_usage_filter": "hard", "tag_usage_min_count": 100,
           "pony_quality_tags": "score_9, score_8_up",
           "style_suffix": "sparklestyle glow"}   # dictionary-unknown, must survive
    prompt = asyncio.run(backend._write_image_prompt(
        cfg, "narration", "earlier", _make_sdk(reply=reply)))
    assert prompt == "score_9, score_8_up, long hair, forest, sparklestyle glow"

    # Natural-language models are never tag-filtered, even with the filter on.
    cfg = {**backend._default_config(),
           "tag_usage_filter": "hard", "tag_usage_min_count": 100}
    prompt = asyncio.run(backend._write_image_prompt(
        cfg, "narration", "earlier", _make_sdk(reply=reply)))
    assert prompt == reply


def test_legacy_tags_template_upgrades_on_load(tmp_path):
    backend = _load_backend(tmp_path)

    # A stored template equal to an old default follows the default.
    cfg = backend._default_config()
    cfg["prompt_template_tags"] = backend.LEGACY_PROMPT_TEMPLATES_TAGS[0]
    backend._save_config(cfg)
    assert backend._load_config()["prompt_template_tags"] \
        == backend.DEFAULT_PROMPT_TEMPLATE_TAGS

    # A customized template is left alone.
    cfg["prompt_template_tags"] = "my custom template {narration} {history}"
    backend._save_config(cfg)
    assert backend._load_config()["prompt_template_tags"] \
        == "my custom template {narration} {history}"


def test_clean_character_tags_usage_filter(tmp_path):
    backend = _load_backend(tmp_path)
    backend._tag_dict_cache = dict(_TAG_DICT)
    raw = "red hair, obscure_tag, scar across nose"

    # Without cfg: unchanged behavior (dedupe/blacklist only).
    assert backend._clean_character_tags(raw) == raw

    cfg = _filter_cfg(backend, "soft")
    assert backend._clean_character_tags(raw, cfg) == "red hair, scar across nose"
    # "hard" would also drop the unknown scar tag.
    cfg = _filter_cfg(backend, "hard")
    assert backend._clean_character_tags(raw, cfg) == "red hair"
    # A reply whose every tag would drop falls back to unfiltered rather
    # than "" (which would send the backfill into a retry loop).
    assert backend._clean_character_tags("obscure_tag", cfg) == "obscure_tag"


def test_tag_usage_filter_config_validation(tmp_path):
    backend = _load_backend(tmp_path)
    client = _client(backend)

    body = client.get("/config").json()
    assert body["tag_usage_filter"] == "off"
    assert body["tag_usage_min_count"] == 100
    assert body["tag_usage_filter_modes"] == ["off", "soft", "hard"]

    resp = client.put("/config", json={"tag_usage_filter": "hard",
                                       "tag_usage_min_count": -5})
    assert resp.status_code == 200
    assert resp.json()["tag_usage_filter"] == "hard"
    assert resp.json()["tag_usage_min_count"] == 0    # clamped
    assert backend._load_config()["tag_usage_filter"] == "hard"

    assert client.put("/config", json={"tag_usage_filter": "medium"}).status_code == 400
    assert backend._load_config()["tag_usage_filter"] == "hard"

    # Junk values in a hand-edited config file degrade to defaults.
    cfg = backend._load_config()
    cfg["tag_usage_filter"] = "nonsense"
    cfg["tag_usage_min_count"] = "many"
    backend._save_config(cfg)
    cfg = backend._load_config()
    assert cfg["tag_usage_filter"] == "off"
    assert cfg["tag_usage_min_count"] == 100
