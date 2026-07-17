"""Lazy site expansion tests: id namespacing, write-once caching, compile
fold-in, play-time prefetch + session sync, and location-context rendering.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_site_expansion.py
"""

import asyncio
import importlib.util
import json
import os
import shutil
import tempfile
import types
from pathlib import Path

import pytest

from wbworldgen.worldgen import WorldBuilder
from wbworldgen.worldgen.enrichment.sites import is_expandable, site_world_entries

_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
_spec = importlib.util.spec_from_file_location(
    "wb_worldgen_backend_sites_test", os.path.join(_MOD_DIR, "backend.py")
)
wbg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wbg)


class FakeSettings:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_sites_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    wb._llm_service = types.SimpleNamespace(
        mode="mock", module_fast_model="fast-slot", reader_model="reader-slot")
    return wb


def _site_world(builder, world_id="site_world"):
    nodes = [
        {"id": "c1", "name": "Vessencia", "type": "city", "importance": 8,
         "x": 0.0, "y": 0.0, "description": "A terraced harbor city.", "region": "Coast"},
        {"id": "w1", "name": "Old Mill", "type": "landmark", "importance": 6,
         "x": 10.0, "y": 0.0, "description": "", "region": "Coast"},
        {"id": "w2", "name": "", "type": "waypoint", "importance": 1,
         "x": 20.0, "y": 0.0, "description": "", "region": "Coast"},
    ]
    edges = [{"from": "c1", "to": "w1", "distance": 10},
             {"from": "w1", "to": "w2", "distance": 10}]
    builder.save_world(world_id, {
        "seed_prompt": "test",
        "steps": {
            "lore": {"data": {"world_name": "Testia", "premise": "A test world."}, "approved": True},
            "map_generation": {"data": {"nodes": nodes, "edges": edges}, "approved": True},
        },
    })
    return world_id


def test_is_expandable_rules():
    assert is_expandable({"name": "V", "type": "city", "importance": 8})
    assert is_expandable({"name": "V", "type": "settlement", "importance": 6})
    assert not is_expandable({"name": "", "type": "city", "importance": 8})  # unnamed
    assert not is_expandable({"name": "V", "type": "landmark", "importance": 8})  # wrong type
    assert not is_expandable({"name": "V", "type": "city", "importance": 3})  # minor


def test_expand_site_mock_mode_and_write_once_cache(builder):
    wid = _site_world(builder)

    site = asyncio.run(builder.expand_site(wid, "c1"))
    assert site["parent_node_id"] == "c1"
    assert site["name"] == "Vessencia"
    assert site["sub_locations"]
    assert all(s["id"].startswith("c1:s") for s in site["sub_locations"])
    # Persisted write-once under the world's sites/ dir.
    on_disk = builder._persistence.load_site(wid, "c1")
    assert on_disk == site
    # Second call returns the cache without regenerating.
    calls = []
    orig = builder._sites.expand

    async def counting(*a, **kw):
        calls.append(1)
        return await orig(*a, **kw)

    builder._sites.expand = counting
    again = asyncio.run(builder.expand_site(wid, "c1"))
    assert again == site
    assert calls == []
    # force=True regenerates.
    asyncio.run(builder.expand_site(wid, "c1", force=True))
    assert calls == [1]


def test_expand_site_validates_and_namespaces_llm_output(builder):
    wid = _site_world(builder)
    builder._llm_service.mode = "live"

    async def fake_live(node, context, max_subs, template_vocab=None):
        return {
            "layout_summary": "Three rings around the harbor.",
            "sub_locations": [
                {"id": "evil-id", "name": "Saltmarket Row", "type": "market",
                 "description": "Fish and rope.", "adjacent": ["The Terraces"]},
                {"name": "The Terraces", "type": "district",
                 "description": "Stacked houses.", "adjacent": ["Saltmarket Row", "Nowhere"]},
                {"name": "saltmarket row", "type": "dup"},  # duplicate name dropped
                {"name": "", "type": "empty"},              # empty name dropped
            ],
        }

    builder._sites._live_expand = fake_live
    site = asyncio.run(builder.expand_site(wid, "c1"))

    subs = site["sub_locations"]
    assert [s["name"] for s in subs] == ["Saltmarket Row", "The Terraces"]
    assert subs[0]["id"] == "c1:s1" and subs[1]["id"] == "c1:s2"  # server-side ids
    # Adjacency resolved from names to assigned ids; unresolvable dropped.
    assert subs[0]["adjacent"] == ["c1:s2"]
    assert subs[1]["adjacent"] == ["c1:s1"]
    assert site["layout_summary"] == "Three rings around the harbor."


def test_sites_fold_into_compiled_world(builder):
    wid = _site_world(builder)
    asyncio.run(builder.expand_site(wid, "c1"))

    world_state = builder.load_world(wid)
    assert "c1" in world_state["sites"]
    compiled = builder.compile_world(world_state)
    assert compiled["site_maps"]["c1"]["name"] == "Vessencia"
    # Worlds without sites simply lack the key.
    other = _site_world(builder, world_id="bare_world")
    bare = builder.compile_world(builder.load_world(other))
    assert "site_maps" not in bare


def test_site_world_entries_format():
    site = {
        "parent_node_id": "c1",
        "name": "Vessencia",
        "layout_summary": "Rings around the harbor.",
        "sub_locations": [
            {"id": "c1:s1", "name": "Saltmarket Row", "type": "market", "description": "Fish."},
            {"id": "c1:s2", "name": "", "type": "x"},  # unnamed skipped
        ],
    }
    entries = site_world_entries("c1", site)
    assert entries[0]["source_type"] == "site" and entries[0]["source_id"] == "c1"
    assert "Layout of Vessencia" in entries[0]["text"]
    assert entries[1]["source_type"] == "site_node" and entries[1]["source_id"] == "c1:s1"
    assert entries[1]["text"] == "Place in Vessencia: Saltmarket Row (market). Fish."
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# Play-time prefetch + session sync (module backend)
# ---------------------------------------------------------------------------

class FakeMemory:
    def __init__(self):
        self.entries = []

    def has_world_index(self):
        return True

    async def embed_world_entries(self, entries, llm):
        self.entries.extend(entries)
        return len(entries)


def _play_session(builder, tmpdir, wid):
    compiled = builder.compile_world(builder.load_world(wid))
    data_dir = Path(tmpdir) / "data"
    save_world_dir = data_dir / "saves" / "save1" / "World"
    save_world_dir.mkdir(parents=True)
    with open(save_world_dir / "world_data.json", "w", encoding="utf-8") as f:
        json.dump(compiled, f)
    sm = types.SimpleNamespace(
        data_dir=data_dir,
        state={"world_id": wid, "active_save_id": "save1", "world_data": compiled},
    )
    engine = types.SimpleNamespace(
        llm=types.SimpleNamespace(mode="live"), memory=FakeMemory())
    wbg.world_builder = builder
    wbg._services = {
        "engine": engine,
        "session_manager": sm,
        "settings": FakeSettings({
            "world.travel_turns_per_edge": 2,
            "world.backfill_per_turn": 0,
            "world.site_expansion_mode": "prefetch",
        }),
    }
    state = {
        "world_id": wid,
        "world_data": compiled,
        "player_location_node_id": "w1",
        "player_location_region": "Coast",
        "player_location_layer_id": None,
        "revealed_node_ids": ["c1", "w1", "w2"],
        "module_data": {},
    }
    return sm, engine, state


@pytest.fixture(autouse=True)
def clean_backfill():
    wbg._backfill_reset()
    yield
    wbg._backfill_reset()
    wbg.world_builder = None
    wbg._services = None


def test_travel_start_prefetches_destination_site(builder, tmpdir):
    wid = _site_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)

    async def main():
        # Reader declares the city as destination — journey starts and the
        # site expansion kicks off in the background.
        result = await wbg.on_mutate_state({"player_location_node_id": "c1"}, state, None)
        assert result["module_data"]["wb_worldgen"]["travel"]["destination_node_id"] == "c1"
        task = wbg._site_tasks.get("c1")
        assert task is not None
        await task

    asyncio.run(main())

    # Synced into the live session and the save's world_data.json.
    assert "c1" in sm.state["world_data"]["site_maps"]
    with open(sm.data_dir / "saves" / "save1" / "World" / "world_data.json", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["site_maps"]["c1"]["name"] == "Vessencia"
    # Embedded into the RAG world index (layout + sub-locations).
    assert any(e["source_type"] == "site" for e in engine.memory.entries)
    assert any(e["source_type"] == "site_node" for e in engine.memory.entries)
    # Cached in the world dir: a fresh compile carries it too.
    compiled = builder.compile_world(builder.load_world(wid))
    assert "c1" in compiled["site_maps"]


def test_arrival_triggers_expansion_when_prefetch_missed(builder, tmpdir):
    wid = _site_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    state["player_location_node_id"] = "c1"

    async def main():
        await wbg.on_gather_context(state, None)
        task = wbg._site_tasks.get("c1")
        assert task is not None
        await task

    asyncio.run(main())
    assert "c1" in sm.state["world_data"]["site_maps"]


def test_site_mode_off_never_expands(builder, tmpdir):
    wid = _site_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    wbg._services["settings"].values["world.site_expansion_mode"] = "off"
    state["player_location_node_id"] = "c1"

    asyncio.run(wbg.on_gather_context(state, None))
    assert not wbg._site_tasks
    assert "site_maps" not in sm.state["world_data"]


def test_non_expandable_nodes_are_ignored(builder, tmpdir):
    wid = _site_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    state["player_location_node_id"] = "w1"  # landmark: not expandable

    asyncio.run(wbg.on_gather_context(state, None))
    assert not wbg._site_tasks


def test_location_context_includes_site_interior(builder, tmpdir):
    wid = _site_world(builder)
    site = asyncio.run(builder.expand_site(wid, "c1"))
    compiled = builder.compile_world(builder.load_world(wid))
    state = {
        "player_location_node_id": "c1",
        "player_location_region": "Coast",
        "module_data": {},
    }
    ctx = wbg._build_location_context(state, compiled)
    assert "<location_interior>" in ctx
    assert site["sub_locations"][0]["name"] in ctx
    assert "Layout:" in ctx
