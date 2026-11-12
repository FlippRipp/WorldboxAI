import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.api.server as server
import backend.engine.llm as llm_mod
from backend.engine.jev import JevService, noul
from backend.engine.llm import LLMService
from backend.engine.llm_inspector import LLMInspector
from backend.engine.provider_manager import ProviderManager
from backend.engine.usage_tracker import (
    NANOGPT_SEARCH_FEE_USD,
    OPENROUTER_SEARCH_FEE_USD,
    CostCalculator,
    UsageLedger,
    normalize_module_source,
)
from backend.sdk.llm_bridge import LLMBridge


def test_cost_calculator_prefers_provider_reported_cost():
    calc = CostCalculator()
    cost, source = calc.resolve_cost(
        model="openrouter/anthropic/claude-sonnet-4",
        provider="openrouter",
        tokens_in=1000,
        tokens_out=500,
        usage={"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.012345},
    )
    assert cost == pytest.approx(0.012345)
    assert source == "reported"


def test_router_catalog_pricing_normalizes_openrouter_and_nanogpt_units(tmp_path):
    cache_file = tmp_path / "pricing_cache.json"
    calc = CostCalculator(cache_file)

    # OpenRouter: USD per single token
    calc.update_openrouter_models([
        {"id": "custom/or-model", "pricing": {"prompt": "0.000002", "completion": "0.000008"}},
    ])
    # NanoGPT: USD per million tokens
    calc.update_nanogpt_models([
        {
            "id": "google/gemini-4-argon",
            "pricing": {"prompt": 1.5, "completion": 6.0, "unit": "per_million_tokens"},
        },
    ])

    # Reload from disk cache to verify persistence
    reloaded = CostCalculator(cache_file)
    or_cost, or_src = reloaded.resolve_cost(
        model="openrouter/custom/or-model",
        provider="openrouter",
        tokens_in=1000,
        tokens_out=250,
    )
    assert or_src == "priced"
    assert or_cost == pytest.approx(1000 * 0.000002 + 250 * 0.000008)

    ng_cost, ng_src = reloaded.resolve_cost(
        model="nano-gpt/google/gemini-4-argon",
        provider="nanogpt",
        tokens_in=2000,
        tokens_out=500,
    )
    assert ng_src == "priced"
    assert ng_cost == pytest.approx((2000 * 1.5 + 500 * 6.0) / 1_000_000.0)


def test_litellm_fallback_prices_direct_and_router_prefixed_models():
    calc = CostCalculator()
    cost, source = calc.resolve_cost(
        model="openai/gpt-4o-mini",
        provider="openai",
        tokens_in=10_000,
        tokens_out=2_000,
    )
    assert source == "priced"
    assert cost is not None and cost > 0

    # Unknown custom model without router cache returns (None, "unknown"), never bogus $0.
    unk_cost, unk_src = calc.resolve_cost(
        model="custom/totally-nonexistent-model-xyz",
        provider="openai",
        tokens_in=500,
        tokens_out=100,
    )
    assert unk_cost is None
    assert unk_src == "unknown"


def test_provider_manager_fetch_models_caches_router_pricing(tmp_path, monkeypatch):
    def handler(request):
        if "openrouter.ai" in str(request.url):
            return httpx.Response(200, json={"data": [
                {
                    "id": "author/test-model",
                    "architecture": {"output_modalities": ["text"]},
                    "pricing": {"prompt": "0.000001", "completion": "0.000004"},
                },
            ]})
        if request.url.path == "/api/v1/models":
            assert request.url.params.get("detailed") == "true"
            return httpx.Response(200, json={"data": [
                {
                    "id": "author/nano-model",
                    "pricing": {"prompt": 2.0, "completion": 8.0, "unit": "per_million_tokens"},
                },
            ]})
        if request.url.path == "/api/v1/embedding-models":
            return httpx.Response(200, json={"data": [
                {
                    "id": "text-embedding-3-small",
                    "pricing": {"prompt": 0.02, "completion": 0.0, "unit": "per_million_tokens"},
                },
            ]})
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    pm = ProviderManager(str(tmp_path))
    pm.save_config("openrouter", {"api_key": "or-key"})
    pm.save_config("nanogpt", {"api_key": "ng-key"})

    asyncio.run(pm.fetch_models("openrouter"))
    asyncio.run(pm.fetch_models("nanogpt"))

    c1, _ = pm.cost_calculator.resolve_cost(
        model="openrouter/author/test-model", tokens_in=1000, tokens_out=500
    )
    assert c1 == pytest.approx(0.003)

    c2, _ = pm.cost_calculator.resolve_cost(
        model="nano-gpt/text-embedding-3-small", call_type="embedding", tokens_in=50_000, tokens_out=0
    )
    assert c2 == pytest.approx(50_000 * 0.02 / 1_000_000.0)


def test_llm_service_tracks_embeddings_streaming_and_search_costs(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path)
    inspector = LLMInspector()
    inspector.set_usage_ledger(ledger)

    svc = LLMService(mode="live")
    svc.set_inspector(inspector)
    svc.set_cost_calculator(ledger.cost_calculator)
    svc.reconfigure("openrouter", {
        "api_key": "or-key",
        "storyteller_model": "openrouter/openai/gpt-4o-mini",
        "reader_model": "openrouter/openai/gpt-4o-mini",
        "module_fast_model": "openrouter/openai/gpt-4o-mini",
        "embedding_model": "openrouter/openai/text-embedding-3-small",
        "search_enabled": True,
    })

    seen_kwargs = []

    async def fake_acompletion(**kwargs):
        seen_kwargs.append(kwargs)
        if kwargs.get("stream"):
            async def gen():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello ", reasoning_content=""))],
                    usage=None,
                )
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="world", reasoning_content=""))],
                    usage=SimpleNamespace(to_dict=lambda: {
                        "prompt_tokens": 120,
                        "completion_tokens": 30,
                        "cost": 0.00045,
                    }),
                )
            return gen()
        # Web search fallback without provider-reported cost -> adds OPENROUTER_SEARCH_FEE_USD
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Search answer", annotations=[]),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(to_dict=lambda: {"prompt_tokens": 200, "completion_tokens": 50}),
        )

    async def fake_aembedding(**kwargs):
        return SimpleNamespace(
            data=[{"index": 0, "embedding": [0.1, 0.2]}],
            usage=SimpleNamespace(to_dict=lambda: {"prompt_tokens": 80, "total_tokens": 80, "cost": 0.000016}),
        )

    monkeypatch.setattr(llm_mod, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_mod, "aembedding", fake_aembedding)

    async def run_all():
        chunks = []
        await svc.generate_story_from_messages(
            [{"role": "user", "content": "Go north"}],
            streaming_callback=lambda tok: chunks.append(tok) or asyncio.sleep(0),
            inspector_ctx={"call_type": "storyteller", "step": "storyteller_node"},
        )
        await svc.get_embedding("ancient sword", inspector_ctx={"call_type": "embedding", "step": "rag"})
        await svc.web_search("arrakis spice", inspector_ctx={"module_source": "wb_worldgen"})

    asyncio.run(run_all())

    assert seen_kwargs[0]["stream_options"] == {"include_usage": True}

    summary = ledger.query_summary(range_key="all")
    assert summary["totals"]["calls"] == 3
    assert summary["totals"]["tokens_in"] == 120 + 80 + 200
    assert summary["totals"]["tokens_out"] == 30 + 0 + 50
    # Search call includes at least OPENROUTER_SEARCH_FEE_USD + token cost
    assert summary["totals"]["cost_usd"] > (0.00045 + 0.000016 + OPENROUTER_SEARCH_FEE_USD)

    by_mod = {m["module"]: m for m in summary["by_module"]}
    assert set(by_mod.keys()) == {"core", "wb_worldgen"}
    assert by_mod["core"]["calls"] == 2
    assert by_mod["wb_worldgen"]["calls"] == 1


def test_nanogpt_search_adds_web_endpoint_fee(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path)
    inspector = LLMInspector()
    inspector.set_usage_ledger(ledger)

    svc = LLMService(mode="live")
    svc.set_inspector(inspector)
    svc.set_cost_calculator(ledger.cost_calculator)
    svc.reconfigure("nanogpt", {
        "api_key": "ng-key",
        "module_fast_model": "nano-gpt/openai/gpt-4o-mini",
        "search_enabled": True,
    })

    def web_handler(request):
        return httpx.Response(200, json={
            "data": [{"title": "Doc", "url": "https://example.com", "content": "Facts"}],
        })

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(web_handler), **kw),
    )

    async def fake_acompletion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Digest"), finish_reason="stop")],
            usage=SimpleNamespace(to_dict=lambda: {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0002}),
        )

    monkeypatch.setattr(llm_mod, "acompletion", fake_acompletion)
    asyncio.run(svc.web_search("query"))

    summary = ledger.query_summary(range_key="all")
    assert summary["totals"]["cost_usd"] == pytest.approx(0.0002 + NANOGPT_SEARCH_FEE_USD)


def test_jev_and_novita_image_gen_record_into_usage_ledger(tmp_path, monkeypatch):
    import modules.wb_image_gen.backend as img_backend

    ledger = UsageLedger(tmp_path)
    inspector = LLMInspector()
    inspector.set_usage_ledger(ledger)

    # 1. Jev decision call
    def jev_handler(request):
        return httpx.Response(200, json={
            "model": "jev-latest",
            "answers": {"q": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 1000, "output_tokens": 5, "cost": 0.000042},
        })

    jev_cfg = tmp_path / "jev" / "config.json"
    jev_svc = JevService(jev_cfg, transport=httpx.MockTransport(jev_handler))
    jev_svc.save({"enabled": True, "api_key": "jev-secret"})

    async def run_jev():
        dec = await jev_svc.decide(
            "Player swings sword.",
            {"q": noul("Is this combat?")},
            module="wb_core_rpg",
            step="rpg:combat",
            inspector=inspector,
        )
        await dec.finish("accepted")

    asyncio.run(run_jev())

    # 2. Novita cloud image render
    old_services = dict(img_backend._services)
    old_submit = img_backend._novita_submit
    old_poll = img_backend._novita_poll
    old_download = img_backend._download
    try:
        img_backend._services["engine"] = SimpleNamespace(llm=SimpleNamespace(inspector=inspector))
        async def fake_submit(cfg, prompt):
            return "task_1"
        async def fake_poll(cfg, tid):
            return "https://example.com/out.jpg"
        async def fake_download(url):
            return b"jpegbytes", "jpg"
        img_backend._novita_submit = fake_submit
        img_backend._novita_poll = fake_poll
        img_backend._download = fake_download

        asyncio.run(img_backend._generate_image(
            {"provider": "novita", "model_name": "flux-2-dev"},
            "a sunlit citadel",
        ))
    finally:
        img_backend._services.clear()
        img_backend._services.update(old_services)
        img_backend._novita_submit = old_submit
        img_backend._novita_poll = old_poll
        img_backend._download = old_download

    summary = ledger.query_summary(range_key="all")
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["cost_usd"] == pytest.approx(0.000042 + 0.03)
    by_type = {r["call_type"]: r for r in summary["by_call_type"]}
    assert by_type["decision"]["cost_usd"] == pytest.approx(0.000042)
    assert by_type["image_gen"]["cost_usd"] == pytest.approx(0.03)


def test_backfill_time_buckets_and_filtering_endpoints(tmp_path, monkeypatch):
    jsonl_path = tmp_path / "llm_calls.jsonl"
    base_ts = 1_760_000_000.0
    # Day 1: core storyteller ($0.10) + unattributed world_build ($0.25)
    # Day 2: wb_npc_system ($0.05)
    sample_records = [
        {
            "id": "c1",
            "timestamp": base_ts - 2 * 86400,
            "call_type": "storyteller",
            "model": "openrouter/anthropic/claude-sonnet-4",
            "step": "storyteller_node",
            "module_source": "",
            "status": "complete",
            "tokens_in": 2000,
            "tokens_out": 400,
            "estimated_cost_usd": 0.10,
        },
        {
            "id": "c2",
            "timestamp": base_ts - 2 * 86400 + 600,
            "call_type": "world_build",
            "model": "openrouter/anthropic/claude-sonnet-4",
            "step": "world_build:lore",
            "module_source": "",
            "status": "complete",
            "tokens_in": 4000,
            "tokens_out": 1000,
            "estimated_cost_usd": 0.25,
        },
        {
            "id": "c3",
            "timestamp": base_ts - 1 * 86400,
            "call_type": "module_fast",
            "model": "openrouter/openai/gpt-4o-mini",
            "step": "module:generate",
            "module_source": "wb_npc_system",
            "status": "complete",
            "tokens_in": 500,
            "tokens_out": 100,
            "estimated_cost_usd": 0.05,
        },
    ]
    jsonl_path.write_text("\n".join(json.dumps(r) for r in sample_records) + "\n", encoding="utf-8")

    ledger = UsageLedger(tmp_path, backfill_jsonl_path=jsonl_path)
    monkeypatch.setattr(server, "usage_ledger", ledger)

    # Verify direct time-series bucketing (base + accumulated)
    res_7d = ledger.query_summary(range_key="7d", now_ts=base_ts)
    assert res_7d["totals"]["cost_usd"] == pytest.approx(0.40)
    non_zero_buckets = [b for b in res_7d["buckets"] if b["base_cost_usd"] > 0]
    assert len(non_zero_buckets) == 2
    assert non_zero_buckets[0]["base_cost_usd"] == pytest.approx(0.35)
    assert non_zero_buckets[0]["accumulated_cost_usd"] == pytest.approx(0.35)
    assert non_zero_buckets[1]["base_cost_usd"] == pytest.approx(0.05)
    assert non_zero_buckets[1]["accumulated_cost_usd"] == pytest.approx(0.40)
    assert res_7d["buckets"][-1]["accumulated_cost_usd"] == pytest.approx(0.40)

    # Verify REST API filtering by module & model keeps available filter lists intact
    client = TestClient(server.app)
    resp = client.get("/api/usage/summary", params={"range": "all", "module": ["wb_worldgen"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["cost_usd"] == pytest.approx(0.25)
    assert body["totals"]["calls"] == 1
    # All 3 modules remain selectable in filter UI even while filtered to wb_worldgen
    avail_mod_ids = {m["id"] for m in body["available_modules"]}
    assert avail_mod_ids == {"core", "wb_worldgen", "wb_npc_system"}
    assert set(body["available_models"]) == {
        "openrouter/anthropic/claude-sonnet-4",
        "openrouter/openai/gpt-4o-mini",
    }

    # Filter by model
    resp_model = client.get(
        "/api/usage/summary",
        params={"range": "all", "model": ["openrouter/openai/gpt-4o-mini"]},
    )
    assert resp_model.status_code == 200
    assert resp_model.json()["totals"]["cost_usd"] == pytest.approx(0.05)

    # Clear endpoint resets ledger
    del_resp = client.delete("/api/usage")
    assert del_resp.status_code == 200
    after = client.get("/api/usage/summary", params={"range": "all"}).json()
    assert after["totals"]["calls"] == 0
    assert after["totals"]["cost_usd"] == 0.0


def test_llm_bridge_current_module_is_isolated_across_concurrent_tasks(monkeypatch):
    inspector = LLMInspector()
    svc = LLMService(mode="live")
    svc.set_inspector(inspector)
    monkeypatch.setattr(svc, "_reasoning_kwargs", lambda _: {})

    async def slow_completion(**kwargs):
        await asyncio.sleep(0.01)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=SimpleNamespace(to_dict=lambda: {"prompt_tokens": 5, "completion_tokens": 2}),
        )

    monkeypatch.setattr(llm_mod, "acompletion", slow_completion)
    bridge = LLMBridge()
    bridge._set_service(svc)

    async def worker(mod_id: str):
        bridge._current_module = mod_id
        try:
            await asyncio.sleep(0.005)
            await bridge.generate(f"hello from {mod_id}", model_preference="fastest")
        finally:
            bridge._current_module = ""

    async def run_both():
        await asyncio.gather(worker("wb_core_rpg"), worker("wb_plot_director"))

    asyncio.run(run_both())
    recorded_modules = {c["module_source"] for c in inspector.get_calls()}
    assert recorded_modules == {"wb_core_rpg", "wb_plot_director"}


def test_normalize_module_source():
    assert normalize_module_source("wb_npc_system", "module_fast", "module:generate") == "wb_npc_system"
    assert normalize_module_source("", "world_build", "map:expand") == "wb_worldgen"
    assert normalize_module_source("", "reader", "intro:known_locations") == "wb_worldgen"
    assert normalize_module_source("", "storyteller", "storyteller_node") == "core"
    assert normalize_module_source("", "image_gen", "novita:image") == "wb_image_gen"
