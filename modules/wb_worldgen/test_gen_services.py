"""Tests for the explicit engine contract: GenServices, CompiledWorldCache
and the shared RateLimitBackoff (Arc A items A1/A2 of the worldgen
architecture plan).

The point under test: the engines depend on nothing but the named fields of
a GenServices object — a hand-built fake is enough to run one — and the
facade keeps its legacy private attributes (``_llm_service`` & co.) as live
views over the one services instance the engines share.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_gen_services.py
"""

import asyncio
import shutil
import tempfile
import time
import types

import pytest

from wbworldgen.worldgen import WorldBuilder
from wbworldgen.worldgen.compiled_cache import CompiledWorldCache
from wbworldgen.worldgen.enrichment import EnrichmentEngine
from wbworldgen.worldgen.enrichment.passes import label as label_pass
from wbworldgen.worldgen.services import GenServices, RateLimitBackoff


def _world_state(n_nodes=3):
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": n_nodes - i,
         "x": float(i), "y": 0.0, "name": "", "description": "", "region": ""}
        for i in range(n_nodes)
    ]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(n_nodes - 1)]
    return {
        "seed_prompt": "test",
        "steps": {"map_generation": {"data": {"nodes": nodes, "edges": edges},
                                     "approved": True}},
    }


class FakeStore:
    """The enrichment_store contract: two methods, nothing else."""

    def __init__(self):
        self.writes = []
        self.flushes = []

    def save_node_enrichment(self, world_id, node_id, field, value):
        self.writes.append((world_id, node_id, field, value))

    def flush_enrichment_cache(self, world_id=None):
        self.flushes.append(world_id)


# ---------------------------------------------------------------------------
# CompiledWorldCache
# ---------------------------------------------------------------------------

def test_compiled_cache_caches_and_invalidates():
    loads = []

    def loader(world_id):
        loads.append(world_id)
        return _world_state()

    cache = CompiledWorldCache(load_world=loader)
    c1 = cache.load("w1")
    c2 = cache.load("w1")
    assert c1 is c2
    assert loads == ["w1"]  # served from cache on the second call
    assert "w1" in cache

    cache.invalidate("w1")
    assert "w1" not in cache
    cache.load("w1")
    assert loads == ["w1", "w1"]  # re-read after invalidation


def test_compiled_cache_holds_one_world():
    cache = CompiledWorldCache(load_world=lambda wid: _world_state())
    cache.load("w1")
    cache.load("w2")
    assert "w2" in cache
    assert "w1" not in cache  # size 1: the actively-worked world


def test_compiled_cache_update_node_mirrors_writes():
    cache = CompiledWorldCache(load_world=lambda wid: _world_state())
    compiled = cache.load("w1")
    cache.update_node(compiled, "n0", "name", "Alpha")
    assert cache.get_node("w1", "n0")["name"] == "Alpha"
    # Unknown ids are a silent no-op, exactly like the engine-internal
    # version this was extracted from.
    cache.update_node(compiled, "missing", "name", "X")
    assert cache.get_node("w1", "missing") is None


def test_compiled_cache_release_terrain_keeps_compiled():
    cache = CompiledWorldCache(load_world=lambda wid: _world_state())
    compiled = cache.load("w1")
    compiled["_terrain_layers"] = {"main": object()}
    cache.release_terrain("w1")
    assert "w1" in cache  # compiled JSON stays cached
    assert "_terrain_layers" not in compiled  # rasters dropped


# ---------------------------------------------------------------------------
# RateLimitBackoff
# ---------------------------------------------------------------------------

def test_backoff_arms_only_on_rate_limit_markers():
    backoff = RateLimitBackoff()
    assert backoff.note_rate_limit(Exception("connection reset")) is False
    assert backoff._until == 0.0
    assert backoff.note_rate_limit(Exception("HTTP 429 Too Many Requests")) is True
    assert backoff._until > time.monotonic()


def test_backoff_wait_returns_once_cooldown_passed():
    backoff = RateLimitBackoff()
    backoff._until = time.monotonic() - 1  # already expired
    asyncio.run(backoff.wait())  # must not hang


# ---------------------------------------------------------------------------
# Engines run on a hand-built GenServices (no facade involved)
# ---------------------------------------------------------------------------

def test_enrichment_engine_runs_on_fake_services(monkeypatch):
    store = FakeStore()
    cache = CompiledWorldCache(load_world=lambda wid: _world_state(2))
    services = GenServices(
        llm=types.SimpleNamespace(mode="live", module_fast_model="fast",
                                  reader_model="reader"),
        prompts=lambda prompt_id, fallback, **kwargs: fallback,
        enrichment_store=store,
        compiled=cache,
        load_world=lambda wid: _world_state(2),
    )
    engine = EnrichmentEngine(services)

    async def fake_label(services_, node, context, used_names=None, problem_note=None):
        return f"Name {node['id']}", f"snippet {node['id']}"

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    result = asyncio.run(engine.run("w1", phase="label", count=1, batch_size=1))

    assert result["labeled"] == 1
    assert ("w1", "n0", "name", "Name n0") in store.writes
    assert ("w1", "n0", "label_description", "snippet n0") in store.writes
    assert store.flushes == ["w1"]
    # The write was mirrored onto the shared compiled cache.
    assert cache.get_node("w1", "n0")["name"] == "Name n0"


# ---------------------------------------------------------------------------
# Facade wiring: one services instance, legacy privates as live views
# ---------------------------------------------------------------------------

@pytest.fixture
def builder():
    d = tempfile.mkdtemp(prefix="wb_services_")
    yield WorldBuilder(worlds_dir=d)
    shutil.rmtree(d, ignore_errors=True)


def test_engines_share_one_services_instance(builder):
    assert builder._enrichment._services is builder._services
    assert builder._sites._services is builder._services
    assert builder._maps_expand._services is builder._services
    assert builder._services.compiled is builder._compiled


def test_legacy_privates_are_live_views(builder):
    fake_llm = types.SimpleNamespace(mode="live")
    builder._llm_service = fake_llm  # direct assignment, as tests do
    assert builder._services.llm is fake_llm
    assert builder._enrichment._llm is fake_llm  # engines see the write

    builder.set_world_builder_temperature(0.42)
    assert builder._services.temperature == 0.42
    assert builder._world_builder_temperature == 0.42

    resized = asyncio.Semaphore(5)
    builder._enrichment_semaphore = resized  # enrich_run's resize path
    assert builder._services.semaphore is resized


def test_save_paths_invalidate_shared_cache(builder):
    wid = builder.save_world("w_inval", _world_state())
    builder._compiled.load(wid)
    assert wid in builder._compiled
    builder.save_step(wid, "map_generation",
                      builder.load_world(wid)["steps"]["map_generation"])
    assert wid not in builder._compiled
