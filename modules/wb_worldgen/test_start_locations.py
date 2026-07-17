"""Request Start tests: candidate discovery is unaffected by lazy world detail,
and when the player's requested start has no good existing match a brand-new
start location is authored on-demand onto an unnamed map position.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_start_locations.py
"""

import asyncio
import json
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder
from wbworldgen.worldgen import start_locations as start_locs


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_start_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    wb._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot")
    return wb


def _start_world(builder, world_id="start_world"):
    """One named settlement + unnamed waypoints (a lazy-mode map)."""
    nodes = [
        {"id": "s1", "name": "Havenport", "type": "settlement", "importance": 8,
         "x": 0.0, "y": 0.0, "description": "A bustling harbor town.", "region": "Coast"},
        {"id": "w1", "name": "", "type": "wilderness", "importance": 3,
         "x": 10.0, "y": 0.0, "description": "", "region": "Highlands"},
        {"id": "w2", "name": "", "type": "waypoint", "importance": 1,
         "x": 20.0, "y": 0.0, "description": "", "region": "Coast"},
    ]
    edges = [{"from": "s1", "to": "w1", "distance": 10},
             {"from": "w1", "to": "w2", "distance": 10}]
    builder.save_world(world_id, {
        "seed_prompt": "test",
        "steps": {
            "lore": {"data": {"world_name": "Testia", "premise": "A test world."}, "approved": True},
            "terrain_regions": {"data": {"regions": [
                {"name": "Coast", "terrain": "cliffs", "climate": "wet", "description": ""},
                {"name": "Highlands", "terrain": "craggy mountains riddled with caves",
                 "climate": "cold", "description": ""},
            ]}, "approved": True},
            "map_generation": {"data": {"nodes": nodes, "edges": edges}, "approved": True},
        },
    })
    return world_id


class ScriptedLLM:
    """simple_completion returns the queued payloads in order."""

    def __init__(self, payloads):
        self.mode = "live"
        self.reader_model = "reader-slot"
        self.payloads = list(payloads)
        self.calls = []

    async def simple_completion(self, messages=None, **kwargs):
        self.calls.append(messages)
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload)


def test_candidate_pool_ignores_unnamed_waypoints(builder):
    wid = _start_world(builder)
    candidates = builder.get_start_locations(wid)
    assert [c["node_id"] for c in candidates] == ["s1"]
    assert candidates[0]["name"] == "Havenport"


def test_good_match_uses_existing_candidate_without_generation(builder):
    wid = _start_world(builder)
    llm = ScriptedLLM([{"node_id": "s1", "name": "Havenport", "reason": "harbor fits"}])

    location = asyncio.run(builder.llm_pick_start_location(wid, "a port town", llm))

    assert location["node_id"] == "s1"
    assert not location.get("generated")
    assert len(llm.calls) == 1  # pick only, no generation call


def test_no_match_generates_start_on_unnamed_slot(builder):
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "NONE", "wanted": "a cave hideout"},
        {"node_id": "w1", "name": "Gloamdeep Cave", "type": "cave",
         "label_description": "A hidden cave in the crags.",
         "description": "A deep cave carved into the highland crags, littered with old campfires.",
         "reason": "the Highlands are riddled with caves"},
    ])

    location = asyncio.run(builder.llm_pick_start_location(wid, "start in a cave", llm))

    assert location["node_id"] == "w1"
    assert location["name"] == "Gloamdeep Cave"
    assert location["type"] == "landmark"  # unknown type coerced
    assert location["generated"] is True
    # Persisted onto the world: the node now exists as a named start candidate.
    nodes = {n["id"]: n for n in
             builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]}
    assert nodes["w1"]["name"] == "Gloamdeep Cave"
    assert nodes["w1"]["type"] == "landmark"
    assert nodes["w1"]["importance"] >= 6
    assert "deep cave" in nodes["w1"]["description"]
    candidates = builder.get_start_locations(wid)
    assert {c["node_id"] for c in candidates} == {"s1", "w1"}
    # The generation prompt offered only unnamed slots.
    gen_user_msg = llm.calls[1][1]["content"]
    assert "w1" in gen_user_msg and "w2" in gen_user_msg
    assert "s1:" not in gen_user_msg


def test_generation_failure_falls_back_to_best_existing(builder):
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "NONE", "wanted": "a cave"},
        ValueError("provider exploded"),                       # generation call fails
        {"node_id": "s1", "name": "Havenport", "reason": "best available"},
    ])

    location = asyncio.run(builder.llm_pick_start_location(wid, "start in a cave", llm))

    assert location["node_id"] == "s1"
    assert not location.get("generated")
    # Nothing was written onto the unnamed nodes.
    nodes = {n["id"]: n for n in
             builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]}
    assert not nodes["w1"]["name"] and not nodes["w2"]["name"]


def test_invalid_generated_node_id_falls_back(builder):
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "NONE", "wanted": "a cave"},
        {"node_id": "s1", "name": "Bad Slot", "type": "landmark"},  # named node: not a slot
        {"node_id": "s1", "name": "Havenport", "reason": "fallback"},
    ])

    location = asyncio.run(builder.llm_pick_start_location(wid, "start in a cave", llm))

    assert location["node_id"] == "s1"
    assert not location.get("generated")


def test_mock_llm_never_asks_for_no_match(builder):
    wid = _start_world(builder)
    llm = types.SimpleNamespace(mode="mock")

    location = asyncio.run(builder.llm_pick_start_location(wid, "anything", llm))

    # Single candidate, mock mode: picked directly without any LLM call.
    assert location["node_id"] == "s1"


def test_unnamed_slots_prefers_important_nodes():
    compiled = {
        "map": {"nodes": [
            {"id": "a", "name": "", "importance": 1},
            {"id": "b", "name": "", "importance": 5},
            {"id": "c", "name": "Named", "importance": 9},
        ]},
    }
    slots = start_locs._unnamed_slots(compiled)
    assert [s["id"] for s in slots] == ["b", "a"]  # named nodes never offered
