"""Child-map expansion tests: entrance contract, write-once caching, legacy
site migration into interior maps, play-time prefetch + session sync, and
the enter: passage flow.

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
from wbworldgen.worldgen.enrichment.maps_expand import (
    child_map_id,
    is_expandable,
    map_world_entries,
)
from wbworldgen.worldgen.migrate import migrate_world_data

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


class FakeMemory:
    def __init__(self):
        self.entries = []

    def has_world_index(self):
        return True

    async def embed_world_entries(self, entries, llm):
        self.entries.extend(entries)
        return len(entries)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_maps_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    wb._llm_service = types.SimpleNamespace(
        mode="mock", module_fast_model="fast-slot", reader_model="reader-slot")
    return wb


def _map_world(builder, world_id="map_world"):
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
        state={"world_id": wid, "active_save_id": "save1", "world_data": compiled},
    )
    engine = types.SimpleNamespace(
        llm=types.SimpleNamespace(mode="live"), memory=FakeMemory())
    wbg.world_builder = builder
    wbg._backfill_reset()
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
        "player_location_node_id": "c1",
        "player_location_map_id": "root",
        "player_location_region": "Coast",
        "revealed_node_ids": ["c1", "w1"],
        "module_data": {},
    }
    return sm, engine, state


# --- expandability ---------------------------------------------------------

def test_is_expandable_rules(builder):
    wid = _map_world(builder)
    compiled = builder.compile_world(builder.load_world(wid))
    nodes = {n["id"]: n for n in compiled["maps"]["root"]["nodes"]}
    # Named nodes qualify regardless of importance (the AI decides depth).
    assert is_expandable(compiled, "root", nodes["c1"])
    assert is_expandable(compiled, "root", nodes["w1"])
    # Unnamed nodes never do.
    assert not is_expandable(compiled, "root", nodes["w2"])


# --- expansion + cache -----------------------------------------------------

def test_expand_node_mock_mode_and_write_once_cache(builder):
    wid = _map_world(builder)

    bundle = asyncio.run(builder.expand_node(wid, "root", "c1"))
    record = bundle["map"]
    assert record["map_id"] == child_map_id("root", "c1")
    assert record["parent_map_id"] == "root"
    assert record["anchor_node_id"] == "c1"
    assert record["level_type"] == "interior"
    assert record["nodes"] and record["config"]["instant_travel"]
    assert all(n["id"].startswith(record["map_id"] + ":") for n in record["nodes"])
    # Entrance contract: at least one connection anchors the child to c1.
    assert any(c["from"]["node_id"] == "c1" and c["to"]["map_id"] == record["map_id"]
               for c in bundle["connections"])
    # Persisted write-once under the world's maps/ dir.
    on_disk = builder._persistence.load_child_map(wid, record["map_id"])
    assert on_disk == bundle
    # Second call returns the cache without regenerating.
    calls = []
    orig = builder._maps_expand.expand

    async def counting(*a, **kw):
        calls.append(1)
        return await orig(*a, **kw)

    builder._maps_expand.expand = counting
    again = asyncio.run(builder.expand_node(wid, "root", "c1"))
    assert again == bundle
    assert calls == []
    asyncio.run(builder.expand_node(wid, "root", "c1", force=True))
    assert calls == [1]


def test_expand_node_validates_llm_output_and_entrance(builder):
    wid = _map_world(builder)
    builder._llm_service.mode = "live"

    async def fake_live(node, context, parent_map, levels, max_locations, template_vocab=None, must_include=None):
        return {
            "label": "The Harbor Rings",
            "level_type": "bogus-level",  # falls back to the first allowed level
            "description": "Three rings around the harbor.",
            "locations": [
                {"id": "evil-id", "name": "Saltmarket Row", "type": "market",
                 "description": "Fish and rope.", "adjacent": ["The Terraces"]},
                {"name": "The Terraces", "type": "district",
                 "description": "Stacked houses.", "adjacent": ["Saltmarket Row", "Nowhere"]},
                {"name": "saltmarket row", "type": "dup"},  # duplicate dropped
                {"name": "", "type": "empty"},              # empty dropped
            ],
            "entrance_kind": "harbor gate",
            "connections": [],
        }

    builder._maps_expand._live_expand = fake_live
    bundle = asyncio.run(builder.expand_node(wid, "root", "c1"))
    record = bundle["map"]
    assert record["label"] == "The Harbor Rings"
    assert record["level_type"] == "interior"
    names = [n["name"] for n in record["nodes"]]
    assert names == ["Saltmarket Row", "The Terraces"]
    # Server-assigned ids; LLM ids ignored.
    assert all(n["id"].startswith(record["map_id"] + ":n") for n in record["nodes"])
    # Adjacency resolved by name into one edge (unresolvable refs dropped).
    assert len(record["edges"]) == 1
    # No entrance flag in the payload -> first location became the entrance,
    # and the mandatory entrance connection uses the parsed kind.
    (entry,) = bundle["connections"]
    assert entry["kind"] == "harbor gate"
    assert entry["from"] == {"map_id": "root", "node_id": "c1"}


def test_expansion_without_locations_raises(builder):
    wid = _map_world(builder)
    builder._llm_service.mode = "live"

    async def fake_live(*a, **kw):
        return {"label": "Empty", "locations": []}

    builder._maps_expand._live_expand = fake_live
    with pytest.raises(ValueError):
        asyncio.run(builder.expand_node(wid, "root", "c1"))


def test_child_maps_fold_into_compiled_world(builder):
    wid = _map_world(builder)
    bundle = asyncio.run(builder.expand_node(wid, "root", "c1"))
    builder.invalidate_compiled(wid) if hasattr(builder, "invalidate_compiled") else None
    compiled = builder.compile_world(builder.load_world(wid))
    record = bundle["map"]
    assert record["map_id"] in compiled["maps"]
    assert any(c["to"]["map_id"] == record["map_id"] for c in compiled["connections"])


# --- legacy site migration -------------------------------------------------

def _legacy_site_world():
    return {
        "map": {"nodes": [{"id": "c1", "name": "Vessencia", "type": "city",
                           "importance": 8, "x": 0, "y": 0}],
                "edges": [], "config": {}},
        "site_maps": {"c1": {
            "parent_node_id": "c1", "name": "Vessencia",
            "layout_summary": "Rings around the harbor.",
            "sub_locations": [
                {"id": "c1:s1", "name": "Saltmarket Row", "type": "market",
                 "description": "Fish and rope.", "adjacent": ["c1:s2"]},
                {"id": "c1:s2", "name": "The Terraces", "type": "district",
                 "description": "Stacked houses.", "adjacent": ["c1:s1"]},
            ],
            "schema": 1,
        }},
    }


def test_legacy_sites_migrate_to_interior_maps():
    wd = migrate_world_data(_legacy_site_world())
    assert "site_maps" not in wd
    interior = wd["maps"]["site_c1"]
    assert interior["anchor_node_id"] == "c1" and interior["parent_map_id"] == "root"
    assert interior["generator_id"] == "interior"
    # Sub-location ids preserved verbatim (saves + RAG source_ids stay valid).
    assert {n["id"] for n in interior["nodes"]} == {"c1:s1", "c1:s2"}
    assert interior["edges"] and interior["config"]["instant_travel"]
    entry = next(c for c in wd["connections"] if c["to"]["map_id"] == "site_c1")
    assert entry["from"]["node_id"] == "c1" and entry["origin"] == "migrated"


def test_legacy_site_position_migrates_to_interior_map():
    from wbworldgen.worldgen.migrate import migrate_session_state
    wd = migrate_world_data(_legacy_site_world())
    state = {"world_data": wd, "player_location_node_id": "c1",
             "module_data": {"wb_worldgen": {"site_position": {
                 "parent_node_id": "c1", "sub_location_id": "c1:s2"}}}}
    migrate_session_state(state)
    assert state["player_location_map_id"] == "site_c1"
    assert state["player_location_node_id"] == "c1:s2"
    assert state["module_data"]["wb_worldgen"]["site_position"] is None


# --- play-time triggers ----------------------------------------------------

# --- growing a child map on demand -----------------------------------------

def test_grow_child_map_mock_adds_node_next_to_player(builder):
    wid = _map_world(builder)
    bundle = asyncio.run(builder.expand_node(wid, "root", "c1"))
    map_id = bundle["map"]["map_id"]
    start = bundle["map"]["nodes"][0]

    grown = asyncio.run(builder.grow_child_map(
        wid, map_id, "the storage building behind the gate",
        near_node_id=start["id"]))

    assert grown["created"] is True
    node = grown["node"]
    assert node["name"] == "The storage building behind the gate"
    assert node["id"].startswith(map_id + ":g")
    # Persisted onto the child-map bundle, wired to the player's node.
    on_disk = builder._persistence.load_child_map(wid, map_id)
    assert any(n["id"] == node["id"] for n in on_disk["map"]["nodes"])
    assert any({e["from"], e["to"]} == {start["id"], node["id"]}
               for e in on_disk["map"]["edges"])
    # Positioned right beside its anchor: about one typical edge length away.
    edge_dists = [e["distance"] for e in bundle["map"]["edges"] if e.get("distance")]
    spacing = sum(edge_dists) / len(edge_dists)
    dist = ((node["x"] - start["x"]) ** 2 + (node["y"] - start["y"]) ** 2) ** 0.5
    assert dist <= spacing * 1.5


def test_grow_child_map_live_prompt_and_existing_match(builder):
    wid = _map_world(builder)
    bundle = asyncio.run(builder.expand_node(wid, "root", "c1"))
    map_id = bundle["map"]["map_id"]
    nodes_by_name = {n["name"]: n for n in bundle["map"]["nodes"]}
    start = bundle["map"]["nodes"][0]
    hall = "Vessencia Hall 2"

    payloads = [
        {"name": "Storage Shed", "type": "outbuilding",
         "description": "Crates and dust.", "adjacent": [hall]},
        {"existing": "Storage Shed"},
    ]
    captured = []

    async def fake_completion(messages=None, **kwargs):
        captured.append(messages)
        return json.dumps(payloads.pop(0))

    builder._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot",
        simple_completion=fake_completion)

    grown = asyncio.run(builder.grow_child_map(
        wid, map_id, "a shed for the harvest tools", near_node_id=start["id"]))

    assert grown["created"] is True
    user = captured[0][1]["content"]
    assert hall in user                                # existing locations listed
    assert f"currently at: {start['name']}" in user    # player anchor present
    assert "a shed for the harvest tools" in user
    assert '"existing"' in user                        # duplicate guard offered
    assert "belongs_outside" in user                   # boundary veto offered
    assert "without leaving" in user
    # The authored adjacency won: the shed adjoins the hall it named.
    assert any({e["from"], e["to"]} == {nodes_by_name[hall]["id"], grown["node"]["id"]}
               for e in grown["edges"])

    # Asking again matches the now-existing shed instead of duplicating it.
    count_before = len(builder._persistence.load_child_map(wid, map_id)["map"]["nodes"])
    again = asyncio.run(builder.grow_child_map(
        wid, map_id, "the tool shed", near_node_id=start["id"]))
    assert again["created"] is False
    assert again["node"]["id"] == grown["node"]["id"]
    count_after = len(builder._persistence.load_child_map(wid, map_id)["map"]["nodes"])
    assert count_after == count_before


def test_grow_child_map_belongs_outside_veto(builder):
    # The interior author may veto: the request is its own destination in the
    # wider world — nothing is created and the marker flows to the caller.
    wid = _map_world(builder)
    bundle = asyncio.run(builder.expand_node(wid, "root", "c1"))
    map_id = bundle["map"]["map_id"]
    count_before = len(bundle["map"]["nodes"])

    async def fake_completion(messages=None, **kwargs):
        return json.dumps({"belongs_outside": True})

    builder._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot",
        simple_completion=fake_completion)
    grown = asyncio.run(builder.grow_child_map(
        wid, map_id, "the lonely lighthouse across the bay"))

    assert grown == {"belongs_outside": True}
    on_disk = builder._persistence.load_child_map(wid, map_id)
    assert len(on_disk["map"]["nodes"]) == count_before


def test_expand_must_include_reaches_the_authoring_prompt(builder):
    # Folding a pending sub-location request into a site's first expansion:
    # the interior author is told the map must include that place.
    wid = _map_world(builder)
    captured = []

    async def fake_completion(messages=None, **kwargs):
        captured.append(messages)
        return json.dumps({
            "label": "Inside Vessencia", "level_type": "interior",
            "entrance_kind": "gate", "entrance_name": "Harbor Gate",
            "entrance_description": "",
            "locations": [{"name": "Saltmarket Row", "type": "market",
                           "description": "Fish and rope.", "adjacent": [],
                           "is_entrance": True}],
            "connections": []})

    builder._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot",
        simple_completion=fake_completion)
    asyncio.run(builder.expand_node(
        wid, "root", "c1", must_include="the storage building behind the gate"))

    user = captured[0][1]["content"]
    assert 'MUST include a location for it: "the storage building behind the gate"' in user


def test_ancestry_anchor_resolves_through_nested_maps():
    from wbruntime.travel import _ancestry_anchor
    wd = {"maps": {
        "root": {"map_id": "root", "nodes": [{"id": "c1", "name": "School"}]},
        "m_int": {"map_id": "m_int", "parent_map_id": "root",
                  "anchor_node_id": "c1", "nodes": [{"id": "m_int:n1", "name": "Gym"}]},
        "m_deep": {"map_id": "m_deep", "parent_map_id": "m_int",
                   "anchor_node_id": "m_int:n1", "nodes": []},
    }}
    assert _ancestry_anchor(wd, "m_deep") == "c1"
    assert _ancestry_anchor(wd, "m_int") == "c1"
    assert _ancestry_anchor(wd, "root") is None


def test_schema_offers_new_sub_location_inside_and_at_sites(builder, tmpdir):
    wid = _map_world(builder)
    asyncio.run(builder.expand_node(wid, "root", "c1"))
    sm, engine, state = _play_session(builder, tmpdir, wid)

    # On the root map at a site: the grow field targets the site's interior,
    # and both boundary fields carry the discriminating rule + cross-hints.
    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    assert "inside Vessencia" in schema["new_sub_location"]["label"]
    assert "without leaving" in schema["new_sub_location"]["description"]
    assert "custom_transition_new_location" in schema["new_sub_location"]["description"]
    assert "new_sub_location" in schema["custom_transition_new_location"]["description"]

    # At an unnamed node: nothing to grow into.
    state["player_location_node_id"] = "w2"
    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    assert "new_sub_location" not in schema

    # Inside the child map: the grow field targets the current map.
    interior_id = child_map_id("root", "c1")
    entrance = state["world_data"]["maps"][interior_id]["nodes"][0]
    state["player_location_map_id"] = interior_id
    state["player_location_node_id"] = entrance["id"]
    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    assert "new_sub_location" in schema
    assert "without leaving" in schema["new_sub_location"]["description"]


def test_travel_new_sub_location_grows_map_and_moves_player(builder, tmpdir):
    wid = _map_world(builder)
    asyncio.run(builder.expand_node(wid, "root", "c1"))
    sm, engine, state = _play_session(builder, tmpdir, wid)
    interior_id = child_map_id("root", "c1")
    entrance = state["world_data"]["maps"][interior_id]["nodes"][0]
    state["player_location_map_id"] = interior_id
    state["player_location_node_id"] = entrance["id"]
    state["revealed_node_ids"].append(entrance["id"])

    result = asyncio.run(wbg.on_mutate_state(
        {"new_sub_location": "the storage building behind the gate"}, state, None))

    # The player moved to the freshly grown node (interior travel is instant).
    new_id = result["player_location_node_id"]
    assert new_id.startswith(interior_id + ":g")
    # The session's world_data gained the node + its edge to the player.
    session_map = state["world_data"]["maps"][interior_id]
    assert any(n["id"] == new_id for n in session_map["nodes"])
    assert any({e["from"], e["to"]} == {entrance["id"], new_id}
               for e in session_map["edges"])
    # Synced to the save file and embedded in the RAG index.
    with open(sm.data_dir / "saves" / "save1" / "World" / "world_data.json",
              encoding="utf-8") as f:
        on_disk = json.load(f)
    assert any(n["id"] == new_id for n in on_disk["maps"][interior_id]["nodes"])
    assert any("storage building" in e["text"].lower() for e in engine.memory.entries)


def test_travel_new_sub_location_at_site_creates_interior_and_enters(builder, tmpdir):
    # On the overworld, standing AT a site with no interior yet: the request
    # creates the interior (folding the place into its first expansion),
    # grows the requested spot onto it, and the player steps inside to it.
    wid = _map_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)

    result = asyncio.run(wbg.on_mutate_state(
        {"new_sub_location": "the storage building behind the gate"}, state, None))

    interior_id = child_map_id("root", "c1")
    new_id = result["player_location_node_id"]
    assert result["player_location_map_id"] == interior_id
    assert new_id.startswith(interior_id + ":g")
    session_map = state["world_data"]["maps"][interior_id]
    assert any(n["id"] == new_id for n in session_map["nodes"])
    # Save file carries the fresh interior and the grown node.
    with open(sm.data_dir / "saves" / "save1" / "World" / "world_data.json",
              encoding="utf-8") as f:
        on_disk = json.load(f)
    assert any(n["id"] == new_id for n in on_disk["maps"][interior_id]["nodes"])


def test_travel_grow_belongs_outside_lands_on_the_overworld(builder, tmpdir, monkeypatch):
    # Inside a site, the Reader asks for a "sub-location" that is really its
    # own destination out in the world: grow vetoes with belongs_outside and
    # the place is authored outside, anchored at the site's overworld node.
    wid = _map_world(builder)
    asyncio.run(builder.expand_node(wid, "root", "c1"))
    sm, engine, state = _play_session(builder, tmpdir, wid)
    interior_id = child_map_id("root", "c1")
    entrance = state["world_data"]["maps"][interior_id]["nodes"][0]
    state["player_location_map_id"] = interior_id
    state["player_location_node_id"] = entrance["id"]

    captured = {}

    async def fake_grow(world_id, map_id, desc, near_node_id=None):
        return {"belongs_outside": True}

    async def fake_author(world_id, description, anchor_node_id=None):
        captured["anchor"] = anchor_node_id
        return {"node_id": "w1", "name": "Old Mill", "type": "landmark",
                "map_id": "root", "generated": True}

    monkeypatch.setattr(builder, "grow_child_map", fake_grow)
    monkeypatch.setattr(builder, "author_location", fake_author)
    result = asyncio.run(wbg.on_mutate_state(
        {"new_sub_location": "the lonely lighthouse across the bay"}, state, None))

    assert captured["anchor"] == "c1"  # resolved through map ancestry
    assert result["player_location_map_id"] == "root"
    assert result["player_location_node_id"] == "w1"


def test_travel_authored_belongs_inside_grows_the_site_interior(builder, tmpdir, monkeypatch):
    # The wider-world authoring path may redirect the other way: the named
    # destination is really a spot inside an existing site — its interior is
    # created/grown and the player lands in there.
    wid = _map_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)

    async def fake_author(world_id, description, anchor_node_id=None):
        return {"belongs_inside": "c1"}

    monkeypatch.setattr(builder, "author_location", fake_author)
    result = asyncio.run(wbg.on_mutate_state({
        "custom_transition": "slipped through the harbor gate",
        "custom_transition_new_location": "the fish-gutting hall of Vessencia",
    }, state, None))

    interior_id = child_map_id("root", "c1")
    assert result["player_location_map_id"] == interior_id
    new_id = result["player_location_node_id"]
    session_map = state["world_data"]["maps"][interior_id]
    assert any(n["id"] == new_id for n in session_map["nodes"])


def test_travel_authors_new_root_node_when_no_slot_fits(builder, tmpdir):
    # An improvised destination that fits no free map position: authoring
    # answers NEW and founds a brand-new node beside its named anchor. Travel
    # mirrors the node + link edge into the session's world_data, the save
    # file, and the RAG index, then lands the player there.
    wid = _map_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    # Landing on the new node must not prefetch its interior mid-test (the
    # scripted LLM below only answers the authoring call).
    wbg._services["settings"].values["world.site_expansion_mode"] = "manual"

    class ScriptedLLM:
        mode = "live"
        reader_model = "reader-slot"

        async def simple_completion(self, messages=None, **kwargs):
            return json.dumps({
                "node_id": "NEW", "near_node_id": "c1", "name": "The Salt Shrine",
                "type": "landmark", "label_description": "A shrine on the point.",
                "description": "A brine-crusted shrine at the city's edge.",
                "reason": "no free position sits close enough to Vessencia"})

    builder._llm_service = ScriptedLLM()
    result = asyncio.run(wbg.on_mutate_state({
        "custom_transition": "followed the pilgrim path",
        "custom_transition_new_location": "the salt shrine by Vessencia",
    }, state, None))

    new_id = result["player_location_node_id"]
    assert new_id.startswith("root:g")
    session_map = state["world_data"]["maps"]["root"]
    node = next(n for n in session_map["nodes"] if n["id"] == new_id)
    assert node["name"] == "The Salt Shrine"
    assert any({e["from"], e["to"]} == {"c1", new_id} for e in session_map["edges"])
    # Persisted into the world itself...
    data = builder.load_world(wid)["steps"]["map_generation"]["data"]
    assert any(n["id"] == new_id for n in data["nodes"])
    # ...the save file, and the RAG index.
    with open(sm.data_dir / "saves" / "save1" / "World" / "world_data.json",
              encoding="utf-8") as f:
        on_disk = json.load(f)
    assert any(n["id"] == new_id for n in on_disk["maps"]["root"]["nodes"])
    assert any("salt shrine" in e["text"].lower() for e in engine.memory.entries)


def test_travel_start_prefetches_destination_child_map(builder, tmpdir):
    wid = _map_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)

    async def main():
        # Setting out toward the landmark starts its expansion in the background.
        await wbg.on_mutate_state({"player_location_node_id": "w1"}, state, None)
        task = wbg._site_tasks.get("w1")
        assert task is not None
        await task

    asyncio.run(main())
    interior_id = child_map_id("root", "w1")
    assert interior_id in sm.state["world_data"]["maps"]
    # Synced to the save file too.
    with open(sm.data_dir / "saves" / "save1" / "World" / "world_data.json", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert interior_id in on_disk["maps"]
    # And embedded in the expected lockstep format.
    assert any(e["source_type"] == "map" and e["source_id"] == interior_id
               for e in engine.memory.entries)


def test_enter_passage_creates_and_enters_child_map(builder, tmpdir):
    wid = _map_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)

    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    enter_options = [o for o in schema["player_passage"]["options"] if o.startswith("enter:c1")]
    assert enter_options, "expandable current node should offer an enter: token"

    result = asyncio.run(wbg.on_mutate_state({"player_passage": enter_options[0]}, state, None))
    interior_id = child_map_id("root", "c1")
    assert result["player_location_map_id"] == interior_id
    assert result["player_location_node_id"].startswith(interior_id + ":")
    assert result["player_location_node_id"] in set(result["revealed_node_ids"])


def test_arrival_triggers_expansion_when_prefetch_missed(builder, tmpdir):
    wid = _map_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    wbg._services["settings"].values["world.site_expansion_mode"] = "on_arrival"

    async def main():
        await wbg.on_gather_context(state, None)
        task = wbg._site_tasks.get("c1")
        assert task is not None
        await task

    asyncio.run(main())
    assert child_map_id("root", "c1") in sm.state["world_data"]["maps"]


def test_expansion_modes_off_and_manual_do_not_autofire(builder, tmpdir):
    wid = _map_world(builder)
    for mode in ("off", "manual"):
        sm, engine, state = _play_session(builder, tmpdir + mode, wid)
        wbg._services["settings"].values["world.site_expansion_mode"] = mode
        asyncio.run(wbg.on_gather_context(state, None))
        assert wbg._site_tasks.get("c1") is None


def test_prefetch_skips_minor_nodes_but_enter_never_refuses(builder, tmpdir):
    wid = _map_world(builder)
    sm, engine, state = _play_session(builder, tmpdir, wid)
    # w1 has importance 6 -> prefetch fires; drop it below the floor.
    node = next(n for n in sm.state["world_data"]["maps"]["root"]["nodes"] if n["id"] == "w1")
    node["importance"] = 2

    async def main():
        wbg._maybe_expand_node(state, "w1")
        assert wbg._site_tasks.get("w1") is None  # below the prefetch floor
        wbg._maybe_expand_node(state, "w1", on_request=True)
        task = wbg._site_tasks.get("w1")
        assert task is not None  # explicit requests always run
        await task

    asyncio.run(main())


# --- RAG entry format ------------------------------------------------------

def test_map_world_entries_format():
    record = {
        "map_id": "m_ab", "label": "The Keep", "level_type": "interior",
        "description": "A drum keep.",
        "nodes": [
            {"id": "m_ab:n1", "name": "Gate", "type": "gate", "description": "Iron-bound."},
            {"id": "m_ab:n2", "name": "Hall", "type": "hall", "description": ""},  # skipped
        ],
    }
    connections = [
        {"id": "c_1", "kind": "door", "name": "The Gate",
         "from": {"map_id": "root", "node_id": "n1"},
         "to": {"map_id": "m_ab", "node_id": "m_ab:n1"},
         "description": "Oak.", "hidden": False},
        {"id": "c_2", "kind": "tunnel", "name": "Secret",
         "from": {"map_id": "root", "node_id": "n1"},
         "to": {"map_id": "m_ab", "node_id": "m_ab:n1"},
         "description": "", "hidden": True},  # hidden -> skipped
    ]
    entries = map_world_entries(record, connections,
                                maps_by_id={"root": {"label": "World"}, "m_ab": record})
    texts = [e["text"] for e in entries]
    assert any(t.startswith("Map: The Keep (interior).") for t in texts)
    assert any(t.startswith("Location [The Keep]: Gate (gate).") for t in texts)
    assert any(t.startswith("Connection: door 'The Gate' linking World and The Keep.") for t in texts)
    assert not any("Secret" in t for t in texts)
