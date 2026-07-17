"""world_format 2 migration tests: flat maps, layered worlds and session
state migrate idempotently with every node id preserved.

Run by explicit path (module tests are outside the root pytest whitelist):
    python -m pytest modules/wb_worldgen/test_migration.py
"""

import copy
import importlib.util
import os

_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
_spec = importlib.util.spec_from_file_location(
    "wb_worldgen_backend_migration_test", os.path.join(_MOD_DIR, "backend.py")
)
wbg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wbg)

from wbworldgen.worldgen import mapspace  # noqa: E402
from wbworldgen.worldgen.migrate import migrate_session_state, migrate_world_data  # noqa: E402


def flat_world():
    return {
        "rules": {"genre": "fantasy"},
        "lore": {"world_name": "Aerathis"},
        "map": {
            "nodes": [
                {"id": "n1", "name": "Thornhold", "type": "settlement", "x": 1, "y": 2, "importance": 8},
                {"id": "n2", "name": "", "type": "waypoint", "x": 3, "y": 4},
            ],
            "edges": [{"from": "n1", "to": "n2", "distance": 5}],
            "regions": [{"region_name": "West", "node_ids": ["n1", "n2"]}],
            "config": {"map_width": 1000, "map_height": 1000},
            "layer_id": "main",
        },
        "regions": {"regions": [{"name": "West"}]},
    }


def layered_world():
    return {
        "lore": {"world_name": "Duallands"},
        "layers": [
            {"layer_id": "surface", "name": "The Surface", "layer_type": "surface",
             "description": "Overworld.", "index": 0},
            {"layer_id": "underground", "name": "The Deep Roads", "layer_type": "underground",
             "description": "Endless tunnels.", "index": 1},
        ],
        "layer_rules": [{"layer_id": "underground", "name": "The Deep Roads",
                         "rules": ["No natural light."]}],
        "map_layers": [
            {"layer_id": "surface", "name": "The Surface", "layer_type": "surface", "index": 0,
             "map": {"nodes": [{"id": "surface_n1", "name": "Gate Town", "type": "settlement", "x": 0, "y": 0}],
                     "edges": [], "config": {}}},
            {"layer_id": "underground", "name": "The Deep Roads", "layer_type": "underground", "index": 1,
             "map": {"nodes": [{"id": "underground_n1", "name": "Black Stair", "type": "dungeon_entrance", "x": 0, "y": 0}],
                     "edges": [], "config": {}}},
        ],
        "map_connections": [
            {"id": "lc_0001", "from_layer_id": "surface", "from_node_id": "surface_n1",
             "to_layer_id": "underground", "to_node_id": "underground_n1",
             "connection_type": "cave_entrance", "name": "The Sinkhole",
             "description": "A yawning pit.", "bidirectional": True},
        ],
    }


def test_flat_world_migrates_to_root_map():
    wd = migrate_world_data(flat_world())
    assert wd["world_format"] == 2
    assert set(wd["maps"]) == {"root"}
    root = wd["maps"]["root"]
    assert root["label"] == "Aerathis"
    assert root["parent_map_id"] is None and root["anchor_node_id"] is None
    assert root["legacy_layer_id"] == "main"
    assert [n["id"] for n in root["nodes"]] == ["n1", "n2"]
    assert root["regions"][0]["region_name"] == "West"
    assert "map" not in wd and "map_layers" not in wd
    assert wd["connections"] == []
    assert wd["hierarchy"]["levels"][0]["level_type"] == "world"


def test_layered_world_migrates_to_parallel_maps():
    wd = migrate_world_data(layered_world())
    assert set(wd["maps"]) == {"root", "underground"}
    root, deep = wd["maps"]["root"], wd["maps"]["underground"]
    assert root["legacy_layer_id"] == "surface" and root["label"] == "The Surface"
    assert deep["parent_map_id"] == "root" and deep["anchor_node_id"] is None
    assert deep["level_type"] == "underground"
    assert deep["rules"] == ["No natural light."]
    # Node ids preserved verbatim.
    assert root["nodes"][0]["id"] == "surface_n1"
    assert deep["nodes"][0]["id"] == "underground_n1"
    # The inter-layer connection became a first-class connection.
    (c,) = wd["connections"]
    assert c["from"] == {"map_id": "root", "node_id": "surface_n1"}
    assert c["to"] == {"map_id": "underground", "node_id": "underground_n1"}
    assert c["kind"] == "cave_entrance" and c["origin"] == "migrated"
    assert c["travel"] == {"mode": "instant"}
    for legacy in ("map_layers", "map_connections", "layers", "layer_rules"):
        assert legacy not in wd


def test_migration_is_idempotent():
    wd = migrate_world_data(layered_world())
    snapshot = copy.deepcopy(wd)
    assert migrate_world_data(wd) is wd
    assert wd == snapshot


def test_session_state_migrates_layer_to_map():
    wd = migrate_world_data(layered_world())
    state = {"world_data": wd, "player_location_node_id": "underground_n1",
             "player_location_layer_id": "underground"}
    assert migrate_session_state(state)
    assert state["player_location_map_id"] == "underground"
    assert "player_location_layer_id" not in state
    # Node placement wins over the stale layer key.
    state2 = {"world_data": wd, "player_location_node_id": "surface_n1",
              "player_location_layer_id": "underground"}
    migrate_session_state(state2)
    assert state2["player_location_map_id"] == "root"


def test_mapspace_accessors_on_migrated_world():
    wd = migrate_world_data(layered_world())
    assert {n["id"] for n in mapspace.all_nodes(wd)} == {"surface_n1", "underground_n1"}
    assert mapspace.map_of_node(wd, "underground_n1") == "underground"
    views = mapspace.connections_from(wd, "underground")
    assert views and views[0]["far"]["map_id"] == "root"
    trail = mapspace.breadcrumb(wd, "underground")
    assert [m["map_id"] for m in trail] == ["root", "underground"]


def test_hooks_migrate_session_worlds_in_place():
    import asyncio
    state = {"world_data": layered_world(), "player_location_node_id": "surface_n1",
             "player_location_layer_id": "surface", "revealed_node_ids": ["surface_n1"],
             "module_data": {}}
    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    assert state["world_data"]["world_format"] == 2
    assert state["player_location_map_id"] == "root"
    # The movement select only offers the player's current map...
    options = schema["player_location_node_id"]["options"]
    assert any(o.startswith("surface_n1") for o in options)
    assert not any(o.startswith("underground_n1") for o in options)
    # ...and the cave mouth is offered as a passage.
    passages = schema["player_passage"]["options"]
    assert any(p.startswith("lc_0001") for p in passages)


def test_hierarchy_parallel_maps_flow_into_v2(tmp_path):
    """hierarchy_design parallel maps generate as sibling maps with scoped
    landmark/faction attachment (the Phase-3 pipeline rework end to end)."""
    from wbworldgen.worldgen import WorldBuilder, register_default_steps

    wb = WorldBuilder(worlds_dir=str(tmp_path))
    register_default_steps(wb)
    state = {
        "seed_prompt": "duallands",
        "steps": {
            "lore": {"data": {"world_name": "Duallands"}, "approved": True},
            "hierarchy_design": {"data": {
                "notes": "A surface world over an endless underworld.",
                "parallel_maps": [{
                    "label": "The Deep Roads", "level_type": "underground",
                    "description": "Endless tunnels.",
                    "connection_kind": "cave_mouth", "connection_count": 2,
                }],
                "pregenerate": [],
            }, "approved": True},
            "natural_landmarks": {"data": {"landmarks": [
                {"scope": "", "name": "The Shard", "type": "monolith",
                 "environment": "rocky_summit", "description": "A black spike."},
                {"scope": "The Deep Roads", "name": "The Sunless Sea", "type": "lake",
                 "environment": "lake_shore", "description": "Still black water."},
            ]}, "approved": True},
            "society_factions": {"data": {"factions": [
                {"scope": "", "name": "The Wardens", "type": "order",
                 "description": "Keepers of the gates.", "settlements": ["Gatewatch"],
                 "significant_landmarks": []},
            ]}, "approved": True},
        },
    }
    map_data = wb._map_gen.generate(state, config={"total_nodes": 40})
    assert "layers" in map_data  # legacy multilayer shape, migrated at compile
    state["steps"]["map_generation"] = {"data": map_data, "approved": True}

    compiled = wb.compile_world(state)
    assert set(compiled["maps"]) == {"root", "the_deep_roads"}
    deep = compiled["maps"]["the_deep_roads"]
    assert deep["level_type"] == "underground"
    assert deep["parent_map_id"] == "root" and deep["anchor_node_id"] is None
    # The cave mouths became connections.
    cave = [c for c in compiled["connections"] if c["kind"] == "cave_mouth"]
    assert cave and all(c["to"]["map_id"] == "the_deep_roads" or
                        c["from"]["map_id"] == "the_deep_roads" for c in cave)
    # Authored content bound onto the right maps.
    root_names = {n.get("name") for n in compiled["maps"]["root"]["nodes"]}
    deep_names = {n.get("name") for n in deep["nodes"]}
    assert {"The Shard", "Gatewatch"} <= root_names
    assert "The Sunless Sea" in deep_names
    # Scope landmarks/factions attached to their MapRecords.
    assert any(l["name"] == "The Shard" for l in compiled["maps"]["root"]["landmarks"])
    assert any(f["name"] == "The Wardens" for f in compiled["maps"]["root"]["factions"])
    assert any(l["name"] == "The Sunless Sea" for l in deep["landmarks"])
    # Template levels ride into the hierarchy block.
    assert compiled["hierarchy"]["levels"][0]["level_type"] == "world"
