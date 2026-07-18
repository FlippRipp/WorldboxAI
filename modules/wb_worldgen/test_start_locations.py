"""Request Start tests: candidate discovery is unaffected by lazy world detail,
and when the player's requested start has no good existing match a brand-new
start location is authored on-demand onto an unnamed map position.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_start_locations.py
"""

import asyncio
import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder
from wbworldgen.worldgen import start_locations as start_locs

# The module file is named backend.py, which collides with the core `backend`
# package — load it explicitly by path under a private name.
_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
_spec = importlib.util.spec_from_file_location(
    "wb_worldgen_backend_start_tests", os.path.join(_MOD_DIR, "backend.py")
)
wbg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wbg)


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

    # The pick prompt teaches that a request for a PART of a listed location
    # (its rooftop, storage...) matches the location itself, never a no-match.
    assert "NOT a no-match" in llm.calls[0][1]["content"]
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
    # The generation prompt offers only unnamed slots as claimable positions
    # (named nodes appear separately, as founding anchors for NEW).
    gen_user_msg = llm.calls[1][1]["content"]
    positions = gen_user_msg.split("Available unnamed map positions:")[1]
    assert "w1" in positions and "w2" in positions
    assert "s1" not in positions


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


def _fake_session(tmpdir):
    data_dir = pathlib.Path(tmpdir) / "session_data"
    return types.SimpleNamespace(
        data_dir=data_dir,
        state={},
        create_save=lambda save_id, **kw: {"save_id": save_id, **kw},
        get_memory_path=lambda: str(data_dir / "memory"),
    )


def _fake_engine(llm):
    async def _noop_async(*args, **kwargs):
        return 0

    return types.SimpleNamespace(
        llm=llm,
        set_memory_path=lambda p: None,
        ensure_memory=_noop_async,
        memory=types.SimpleNamespace(init_world_index=lambda p: None, embed_world=_noop_async),
    )


def test_story_source_derives_start_from_scenario(builder, tmpdir, monkeypatch):
    # World + scenario with no explicit pick or typed preference: the start
    # location is chosen from the scenario itself — the pick prompt carries
    # the opening scene and the player's change request (highest priority).
    wid = _start_world(builder)
    llm = ScriptedLLM([{"node_id": "s1", "name": "Havenport", "reason": "the opening is set in a harbor"}])
    monkeypatch.setattr(wbg, "world_builder", builder)

    scenario = {
        "name": "Ambush",
        "scenario_description": "Bandits stalk the harbor road.",
        "starting_prompt": "The wagon wheel snaps at dusk by the docks.",
        "pending_modification_request": "make the harbor freeze over",
    }
    result = asyncio.run(wbg.create_world_story_source(
        save_id="scenario_start", source_id=wid, start_preference=None,
        session_manager=_fake_session(tmpdir),
        engine=_fake_engine(llm), scenario=scenario))

    assert result["start_location"]["node_id"] == "s1"
    pick_msg = llm.calls[0][1]["content"]
    assert "The wagon wheel snaps at dusk by the docks." in pick_msg
    assert "make the harbor freeze over" in pick_msg


def test_story_source_prefers_typed_preference_over_scenario(builder, tmpdir, monkeypatch):
    # An explicit start preference (API callers) still wins over the scenario.
    wid = _start_world(builder)
    llm = ScriptedLLM([{"node_id": "s1", "name": "Havenport", "reason": "a port town"}])
    monkeypatch.setattr(wbg, "world_builder", builder)

    scenario = {"name": "Ambush", "starting_prompt": "The wagon wheel snaps at dusk."}
    result = asyncio.run(wbg.create_world_story_source(
        save_id="pref_start", source_id=wid, start_preference="a port town",
        session_manager=_fake_session(tmpdir),
        engine=_fake_engine(llm), scenario=scenario))

    assert result["start_location"]["node_id"] == "s1"
    pick_msg = llm.calls[0][1]["content"]
    assert "a port town" in pick_msg
    assert "wagon wheel" not in pick_msg


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


def test_unnamed_slots_anchor_ordering_and_near_names():
    compiled = {
        "map": {"nodes": [
            {"id": "s1", "name": "Havenport", "x": 0.0, "y": 0.0, "importance": 8},
            {"id": "w_far", "name": "", "x": 100.0, "y": 0.0, "importance": 9},
            {"id": "w_near", "name": "", "x": 10.0, "y": 0.0, "importance": 1},
        ]},
    }
    # Without an anchor: importance order, but every slot still knows its
    # nearest named places.
    slots = start_locs._unnamed_slots(compiled)
    assert [s["id"] for s in slots] == ["w_far", "w_near"]
    assert slots[0]["near_named"][0]["name"] == "Havenport"
    # With an anchor: closest to the anchor first, tagged with the distance.
    anchored = start_locs._unnamed_slots(compiled, anchor_node_id="s1")
    assert [s["id"] for s in anchored] == ["w_near", "w_far"]
    assert anchored[0]["anchor_distance"] == pytest.approx(10.0)


def test_generate_prompt_carries_distance_tiers_and_named_places(builder):
    # The authoring prompt states the map's typical route leg, the tiered
    # distance limits, the NEW escape hatch, and the named places (with ids)
    # a NEW node could be founded beside.
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "w1", "name": "Somewhere", "type": "landmark",
         "label_description": "l", "description": "d", "reason": "r"},
    ])
    compiled = builder.compile_world(builder.load_world(wid))

    asyncio.run(start_locs.generate_start_location(compiled, "a cave", "a cave", llm))

    user = llm.calls[0][1]["content"]
    assert "Existing named places:" in user
    assert "- s1: Havenport (settlement)" in user
    assert "about 10 map units" in user  # mean of the two 10-unit edges
    assert "ONE route leg" in user and "TWO route legs" in user
    assert '"node_id": "NEW"' in user and "near_node_id" in user
    # And the cross-boundary redirect for places inside an existing site.
    assert '"belongs_inside"' in user and "without leaving" in user


def test_no_slot_fits_founds_new_node_beside_anchor(builder):
    # A NEW answer founds a brand-new node one route leg beside the named
    # anchor: persisted into the map step data with a real edge, region
    # membership inherited, and offered as a start candidate.
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "NONE", "wanted": "a lighthouse"},
        {"node_id": "NEW", "near_node_id": "s1", "name": "The Harbor Light",
         "type": "landmark", "label_description": "A lighthouse on the point.",
         "description": "A salt-bleached lighthouse guarding Havenport's approach.",
         "reason": "no free position sits close enough to Havenport"},
    ])

    location = asyncio.run(builder.llm_pick_start_location(wid, "a lighthouse by Havenport", llm))

    assert location["generated"] is True
    assert location["name"] == "The Harbor Light"
    assert location["map_id"] == "root"
    new_id = location["node_id"]
    assert new_id.startswith("root:g")
    assert location["new_node"]["id"] == new_id
    assert location["new_edges"][0]["from"] == "s1"

    # Persisted: the step data gained the node and its link edge to s1.
    data = builder.load_world(wid)["steps"]["map_generation"]["data"]
    node = next(n for n in data["nodes"] if n["id"] == new_id)
    assert node["name"] == "The Harbor Light"
    assert node["importance"] >= 6
    assert node["region"] == "Coast"  # inherited from the anchor
    edge = next(e for e in data["edges"] if new_id in (e["from"], e["to"]))
    assert {edge["from"], edge["to"]} == {"s1", new_id}
    # Placed about one typical route leg (10 units) from the anchor.
    assert 5.0 <= edge["distance"] <= 20.0
    assert {c["node_id"] for c in builder.get_start_locations(wid)} == {"s1", new_id}


def test_new_node_with_unknown_anchor_falls_back(builder):
    # A NEW answer naming no usable anchor is invalid output: nothing is
    # created and the pick falls back to the best existing candidate.
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "NONE", "wanted": "a lighthouse"},
        {"node_id": "NEW", "near_node_id": "nope", "name": "The Harbor Light",
         "type": "landmark"},
        {"node_id": "s1", "name": "Havenport", "reason": "fallback"},
    ])

    location = asyncio.run(builder.llm_pick_start_location(wid, "a lighthouse", llm))

    assert location["node_id"] == "s1"
    assert not location.get("generated")
    data = builder.load_world(wid)["steps"]["map_generation"]["data"]
    assert not any(n["id"].startswith("root:g") for n in data["nodes"])


def test_author_location_returns_founded_node_mirror_data(builder):
    # Play-time authoring: a NEW answer without near_node_id falls back to
    # the anchor (the player's node), and the returned candidate carries the
    # node + edges for the caller to mirror into the session.
    wid = _start_world(builder)
    builder._llm_service = ScriptedLLM([
        {"node_id": "NEW", "name": "The Harbor Light", "type": "landmark",
         "label_description": "A lighthouse.", "description": "A lighthouse on the point.",
         "reason": "belongs right by Havenport"},
    ])

    result = asyncio.run(builder.author_location(
        wid, "the lighthouse by Havenport", anchor_node_id="s1"))

    assert result["generated"] is True
    assert result["map_id"] == "root"
    assert result["new_node"]["name"] == "The Harbor Light"
    assert result["new_edges"][0]["from"] == "s1"


def test_belongs_inside_answer_starts_at_the_parent_place(builder):
    # A start request for a place inside an existing site starts AT that site
    # (the scene plays out in its part of it) — nothing is authored.
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "NONE", "wanted": "the harbor master's office"},
        {"belongs_inside": "s1"},
    ])

    location = asyncio.run(builder.llm_pick_start_location(
        wid, "the harbor master's office in Havenport", llm))

    assert location["node_id"] == "s1"
    assert not location.get("generated")
    data = builder.load_world(wid)["steps"]["map_generation"]["data"]
    assert not any(n["id"].startswith("root:g") for n in data["nodes"])
    nodes = {n["id"]: n for n in data["nodes"]}
    assert not nodes["w1"]["name"] and not nodes["w2"]["name"]


def test_invalid_belongs_inside_falls_back(builder):
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "NONE", "wanted": "an office"},
        {"belongs_inside": "nope"},
        {"node_id": "s1", "name": "Havenport", "reason": "fallback"},
    ])

    location = asyncio.run(builder.llm_pick_start_location(wid, "an office", llm))

    assert location["node_id"] == "s1"
    assert not location.get("generated")


def test_author_location_passes_belongs_inside_through(builder):
    # Play-time authoring hands the redirect to the caller (travel grows the
    # site's interior) instead of claiming a map position.
    wid = _start_world(builder)
    builder._llm_service = ScriptedLLM([{"belongs_inside": "s1"}])

    result = asyncio.run(builder.author_location(
        wid, "the harbor master's office", anchor_node_id="s1"))

    assert result == {"belongs_inside": "s1"}


def test_append_map_node_dispatches_to_layer_and_child_bundle(builder):
    # The persistence append helper writes into the right store: the matching
    # layer of the map_generation step data (with region membership), or a
    # persisted child-map bundle.
    wid = "layered_world"
    builder.save_world(wid, {
        "seed_prompt": "test",
        "steps": {
            "lore": {"data": {"world_name": "Layered", "premise": "p"}, "approved": True},
            "map_generation": {"data": {"layers": [
                {"layer_id": "root", "name": "Surface",
                 "map": {"nodes": [{"id": "r1", "name": "Topside", "x": 0, "y": 0}],
                         "edges": []}},
                {"layer_id": "underdark", "name": "Underdark",
                 "map": {"nodes": [{"id": "u1", "name": "Deepgate", "x": 0, "y": 0}],
                         "edges": [],
                         "regions": [{"region_name": "The Deep", "node_ids": ["u1"]}]}},
            ]}, "approved": True},
        },
    })
    node = {"id": "underdark:g2", "name": "Fungal Grove", "type": "landmark",
            "x": 5.0, "y": 5.0}
    edges = [{"from": "u1", "to": "underdark:g2", "distance": 7.0}]
    assert builder._persistence.append_map_node(wid, "underdark", node, edges)

    layers = builder.load_world(wid)["steps"]["map_generation"]["data"]["layers"]
    under = layers[1]["map"]
    assert any(n["id"] == "underdark:g2" for n in under["nodes"])
    assert edges[0] in under["edges"]
    assert "underdark:g2" in under["regions"][0]["node_ids"]
    assert all(n["id"] != "underdark:g2" for n in layers[0]["map"]["nodes"])

    # Child bundle dispatch: a map_id persisted under maps/ goes to its bundle.
    bundle = {"map": {"map_id": "root:c1", "nodes": [{"id": "root:c1:n1", "name": "Hall"}],
                      "edges": []}, "connections": []}
    builder._persistence.save_child_map(wid, bundle)
    child_node = {"id": "root:c1:g2", "name": "Cellar", "type": "place"}
    assert builder._persistence.append_map_node(
        wid, "root:c1", child_node, [{"from": "root:c1:n1", "to": "root:c1:g2", "distance": 1.0}])
    on_disk = builder._persistence.load_child_map(wid, "root:c1")
    assert any(n["id"] == "root:c1:g2" for n in on_disk["map"]["nodes"])

    # Unknown map: refused, nothing written.
    assert not builder._persistence.append_map_node(wid, "nowhere", dict(node), [])


def test_generate_start_location_prompt_is_anchor_aware(builder):
    # Authoring "the school's storage building" from the player's position:
    # slots come annotated with nearest named places and player distance, and
    # the prompt carries the place-it-close placement rules.
    wid = _start_world(builder)
    llm = ScriptedLLM([
        {"node_id": "w1", "name": "Harbor Storage", "type": "landmark",
         "label_description": "l", "description": "A cramped storage outbuilding.",
         "reason": "right by the harbor"},
    ])
    compiled = builder.compile_world(builder.load_world(wid))

    location = asyncio.run(start_locs.generate_start_location(
        compiled, "the storage building by Havenport", "a storage building", llm,
        anchor_node_id="s1"))

    assert location["node_id"] == "w1"
    user = llm.calls[0][1]["content"]
    assert "currently at: Havenport" in user
    assert "near Havenport" in user
    assert "distance from player" in user
    assert "CLOSEST" in user
