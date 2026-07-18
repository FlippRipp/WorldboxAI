"""Lazy enrichment tests: the importance floor (majors-only upfront detail),
targeted node runs, and the play-time background backfill (queueing, session
sync, await-on-arrival, idle trickle).

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_lazy_enrichment.py
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

# The module file is named backend.py, which collides with the core `backend`
# package — load it explicitly by path under a private name.
_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
_spec = importlib.util.spec_from_file_location(
    "wb_worldgen_backend_lazy_test", os.path.join(_MOD_DIR, "backend.py")
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
    d = tempfile.mkdtemp(prefix="wb_lazy_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    wb._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot")
    wb._enrichment_batch_size = 1
    return wb


def _mixed_world(builder, world_id="lazy_world"):
    """Two majors (importance 8, 6) and three minors (3, 2, 1)."""
    importances = {"n0": 8, "n1": 6, "n2": 3, "n3": 2, "n4": 1}
    nodes = [
        {"id": nid, "type": "settlement" if imp >= 6 else "waypoint",
         "importance": imp, "x": float(i), "y": 0.0,
         "name": "", "description": "", "region": ""}
        for i, (nid, imp) in enumerate(importances.items())
    ]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}", "distance": 10} for i in range(4)]
    builder.save_world(world_id, {
        "seed_prompt": "test",
        "steps": {"map_generation": {"data": {"nodes": nodes, "edges": edges}, "approved": True}},
    })
    return world_id


def _fake_enrichment(builder):
    async def fake_label(node, context, used_names=None):
        return f"Name {node['id']}", f"snippet {node['id']}"

    async def fake_desc(node, context, existing_description=""):
        return f"Flavor text for {node['id']}"

    builder._enrichment._live_label = fake_label
    builder._enrichment._live_description = fake_desc


# ---------------------------------------------------------------------------
# Engine: importance floor + targeted node runs
# ---------------------------------------------------------------------------

def test_run_importance_floor_details_only_majors(builder):
    wid = _mixed_world(builder)
    _fake_enrichment(builder)
    events = []

    async def on_event(evt):
        events.append(evt)

    summary = asyncio.run(builder.enrich_run(
        wid, phase="all", importance_floor=6, on_event=on_event))

    assert summary["labeled"] == 2
    assert summary["described"] == 2
    nodes = {n["id"]: n for n in
             builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]}
    assert nodes["n0"]["name"] and nodes["n0"]["description"]
    assert nodes["n1"]["name"] and nodes["n1"]["description"]
    for nid in ("n2", "n3", "n4"):
        assert not nodes[nid]["name"] and not nodes[nid]["description"]
    # Progress totals are scoped to the targeted majors, so the run reads
    # as complete when the targeted work is done.
    phases = [e for e in events if e["type"] == "phase"]
    assert phases[0]["total_nodes"] == 2
    last_node_evt = [e for e in events if e["type"] == "node"][-1]
    assert last_node_evt["total_nodes"] == 2
    assert last_node_evt["per_layer"]["root"]["total"] == 2


def test_run_node_ids_targets_specific_nodes(builder):
    wid = _mixed_world(builder)
    _fake_enrichment(builder)

    summary = asyncio.run(builder.enrich_run(wid, phase="all", node_ids=["n3"]))

    assert summary["labeled"] == 1
    assert summary["described"] == 1
    nodes = {n["id"]: n for n in
             builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]}
    assert nodes["n3"]["name"] == "Name n3"
    assert nodes["n3"]["description"] == "Flavor text for n3"
    assert not nodes["n0"]["name"]  # floor-qualified but not targeted


def test_detail_nodes_and_get_map_node(builder):
    wid = _mixed_world(builder)
    _fake_enrichment(builder)

    asyncio.run(builder.detail_nodes(wid, ["n2"]))
    node = builder.get_map_node(wid, "n2")
    assert node["name"] == "Name n2"
    assert node["description"] == "Flavor text for n2"
    assert builder.get_map_node(wid, "does_not_exist") is None


def test_default_importance_floor_follows_setting(builder):
    assert builder.default_importance_floor() == 6  # no settings: lazy default
    builder.set_settings(FakeSettings({"world.upfront_detail": "full"}))
    assert builder.default_importance_floor() is None
    builder.set_settings(FakeSettings({"world.upfront_detail": "major_locations"}))
    assert builder.default_importance_floor() == 6


# ---------------------------------------------------------------------------
# Play-time backfill (module backend machinery)
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
    """Wire the module backend to a fake live session for world `wid`."""
    compiled = builder.compile_world(builder.load_world(wid))
    data_dir = Path(tmpdir) / "data"
    save_world_dir = data_dir / "saves" / "save1" / "World"
    save_world_dir.mkdir(parents=True)
    with open(save_world_dir / "world_data.json", "w", encoding="utf-8") as f:
        json.dump(compiled, f)

    sm = types.SimpleNamespace(
        data_dir=data_dir,
        state={
            "world_id": wid,
            "active_save_id": "save1",
            "world_data": compiled,
        },
    )
    engine = types.SimpleNamespace(
        llm=types.SimpleNamespace(mode="live"), memory=FakeMemory())
    wbg.world_builder = builder
    wbg._services = {
        "engine": engine,
        "session_manager": sm,
        "settings": FakeSettings({
            "world.travel_minutes_per_edge": 20,
            "world.backfill_per_turn": 2,
        }),
    }
    state = {
        "world_id": wid,
        "world_data": compiled,
        "player_location_node_id": "n0",
        "player_location_region": "",
        "player_location_layer_id": None,
        "revealed_node_ids": ["n0", "n1"],
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


def test_arrival_at_undetailed_node_waits_and_syncs(builder, tmpdir):
    wid = _mixed_world(builder)
    _fake_enrichment(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    state["player_location_node_id"] = "n2"  # minor node: no name/description
    wbg._services["settings"].values["world.backfill_per_turn"] = 0  # no trickle noise

    result = asyncio.run(wbg.on_gather_context(state, None))

    # The turn waited for the node's detail and narrates from it.
    assert "Name n2" in result["context_string"]
    assert "Flavor text for n2" in result["context_string"]
    # Synced into the live session world_data and the save's world_data.json.
    node = next(n for n in sm.state["world_data"]["maps"]["root"]["nodes"] if n["id"] == "n2")
    assert node["name"] == "Name n2"
    with open(sm.data_dir / "saves" / "save1" / "World" / "world_data.json", encoding="utf-8") as f:
        on_disk = json.load(f)
    disk_node = next(n for n in on_disk["maps"]["root"]["nodes"] if n["id"] == "n2")
    assert disk_node["description"] == "Flavor text for n2"
    # And embedded into the RAG world index.
    assert any(e["source_id"] == "n2" and "Name n2" in e["text"]
               for e in engine.memory.entries)


def test_idle_trickle_details_pending_nodes(builder, tmpdir):
    wid = _mixed_world(builder)
    _fake_enrichment(builder)
    # Majors upfront: n0/n1 already detailed, three minors pending.
    asyncio.run(builder.enrich_run(wid, phase="all", importance_floor=6))
    sm, engine, state = _play_session(builder, tmpdir, wid)

    async def main():
        await wbg.on_gather_context(state, None)
        assert wbg._backfill["task"] is not None
        await wbg._backfill["task"]

    asyncio.run(main())

    detailed = [n["id"] for n in sm.state["world_data"]["maps"]["root"]["nodes"] if n["description"]]
    # Two more nodes per turn (backfill_per_turn=2) on top of the majors.
    assert len(detailed) == 4


def test_reveal_queues_backfill_on_teleport(builder, tmpdir):
    wid = _mixed_world(builder)
    _fake_enrichment(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    wbg._services["settings"].values["world.travel_minutes_per_edge"] = 0  # instant moves
    wbg._services["settings"].values["world.backfill_per_turn"] = 0

    async def main():
        result = await wbg.on_mutate_state({"player_location_node_id": "n3"}, state, None)
        assert result["player_location_node_id"] == "n3"
        task = wbg._backfill["task"]
        if task is not None:
            await task
        return result

    asyncio.run(main())

    # The teleport revealed n3; its neighbors n2/n4 became the name-only
    # fringe and were queued for naming so the map can show them.
    described = {n["id"] for n in sm.state["world_data"]["maps"]["root"]["nodes"] if n["description"]}
    assert {"n2", "n4"} <= described


def test_backfill_inert_without_live_llm(builder, tmpdir):
    wid = _mixed_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    engine.llm.mode = "mock"
    state["player_location_node_id"] = "n2"

    result = asyncio.run(wbg.on_gather_context(state, None))

    assert wbg._backfill["task"] is None
    # Sparse-context fallback: honest improvisation line for the unnamed node.
    assert "unexplored" in result["context_string"]


# ---------------------------------------------------------------------------
# Context + mutation schema fallbacks for undetailed nodes
# ---------------------------------------------------------------------------

def test_location_context_falls_back_to_label_description():
    world = {
        "map": {"nodes": [{"id": "n1", "name": "Emberhold", "type": "town",
                           "description": "", "label_description": "A soot-stained mining town."}],
                "edges": []},
        "regions": {"regions": []},
    }
    state = {"player_location_node_id": "n1", "module_data": {}}
    ctx = wbg._build_location_context(state, world)
    assert "Emberhold" in ctx and "soot-stained" in ctx


def test_mutation_schema_offers_revealed_unexplored_nodes():
    world = {
        "map": {"nodes": [
            {"id": "n1", "name": "Emberhold", "type": "town"},
            {"id": "n2", "name": "", "type": "waypoint"},
            {"id": "n3", "name": "", "type": "waypoint"},
        ], "edges": []},
        "regions": {"regions": []},
        "layers": [],
    }
    state = {"revealed_node_ids": ["n1", "n2"]}
    schema = wbg._build_location_mutation_schema(world, state)
    options = schema["player_location_node_id"]["options"]
    assert "n1 (Emberhold)" in options
    assert "n2 (unexplored waypoint)" in options
    assert not any(o.startswith("n3") for o in options)  # unrevealed stays hidden


def test_mutation_schema_offers_unexplored_fringe_nodes():
    # A node one edge beyond the revealed set (the map's name-only fringe)
    # is a valid destination even before it's revealed.
    world = {
        "map": {"nodes": [
            {"id": "n1", "name": "Emberhold", "type": "town"},
            {"id": "n2", "name": "", "type": "waypoint"},
            {"id": "n3", "name": "", "type": "waypoint"},
        ], "edges": [{"from": "n1", "to": "n2"}]},
        "regions": {"regions": []},
        "layers": [],
    }
    state = {"revealed_node_ids": ["n1"]}
    schema = wbg._build_location_mutation_schema(world, state)
    options = schema["player_location_node_id"]["options"]
    assert "n2 (unexplored waypoint)" in options  # fringe of n1
    assert not any(o.startswith("n3") for o in options)  # beyond the fringe
