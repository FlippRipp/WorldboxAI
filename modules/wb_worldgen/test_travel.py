"""Gradual travel tests.

A Reader-declared destination starts a journey along the edge graph; progress
advances each turn at the pace set by `world.travel_turns_per_edge` instead of
teleporting the player. Run by path with the venv python:

    .venv/Scripts/python -m pytest modules/wb_worldgen/test_travel.py
"""
import asyncio
import importlib.util
import os

import pytest

# The module file is named backend.py, which collides with the core `backend`
# package — load it explicitly by path under a private name.
_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
_spec = importlib.util.spec_from_file_location(
    "wb_worldgen_backend_under_test", os.path.join(_MOD_DIR, "backend.py")
)
wbg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wbg)


class FakeSettings:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key, 2)


def make_world():
    """Four nodes in a line, 10 map-units apart: Aldern-Bryn-Cael-Dunmore."""
    nodes = [
        {"id": "n_a", "name": "Aldern", "type": "settlement", "x": 0, "y": 0, "region": "West"},
        {"id": "n_b", "name": "Bryn", "type": "crossroads", "x": 10, "y": 0, "region": "West"},
        {"id": "n_c", "name": "Cael", "type": "wilderness", "x": 20, "y": 0, "region": "East"},
        {"id": "n_d", "name": "Dunmore", "type": "settlement", "x": 30, "y": 0, "region": "East"},
        # An unreachable island node (no edges).
        {"id": "n_x", "name": "Lost Isle", "type": "landmark", "x": 99, "y": 99, "region": "East"},
    ]
    edges = [
        {"from": "n_a", "to": "n_b", "distance": 10},
        {"from": "n_b", "to": "n_c", "distance": 10},
        {"from": "n_c", "to": "n_d", "distance": 10},
    ]
    return {
        "map": {"nodes": nodes, "edges": edges},
        "regions": {"regions": [
            {"name": "West", "terrain": "hills", "climate": "mild"},
            {"name": "East", "terrain": "plains", "climate": "dry"},
        ]},
    }


def make_state(world):
    return {
        "player_location_node_id": "n_a",
        "player_location_region": "West",
        "revealed_node_ids": ["n_a", "n_b"],
        "world_data": world,
        "module_data": {},
    }


@pytest.fixture(autouse=True)
def travel_pace():
    """Default pace: 2 turns per average edge (speed = 5 units/turn)."""
    wbg._services = {"settings": FakeSettings({"world.travel_turns_per_edge": 2})}
    yield
    wbg._services = None


def run_turn(state, mutation):
    """Call on_mutate_state and merge the result like graph.py's reader does."""
    result = asyncio.run(wbg.on_mutate_state(mutation, state, None))
    md = result.get("module_data")
    if md and "wb_worldgen" in md:
        own = state.setdefault("module_data", {}).setdefault("wb_worldgen", {})
        own.update(md["wb_worldgen"])
    if result.get("player_location_node_id"):
        for key in ("player_location_node_id", "player_location_region",
                    "player_location_map_id", "revealed_node_ids"):
            if key in result:
                state[key] = result[key]
    return result


def test_set_out_starts_travel_without_teleporting():
    state = make_state(make_world())
    result = run_turn(state, {"player_location_node_id": "n_d"})

    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["route"] == ["n_a", "n_b", "n_c", "n_d"]
    assert travel["destination_node_id"] == "n_d"
    # One turn of progress (speed 5) on a 10-unit leg — still short of Bryn.
    assert travel["leg_index"] == 0
    assert travel["leg_progress"] == pytest.approx(5.0)
    assert "player_location_node_id" not in result
    assert state["player_location_node_id"] == "n_a"


def test_waypoints_are_reached_and_revealed_along_the_way():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    result = run_turn(state, {})  # second turn completes the first leg

    assert result["player_location_node_id"] == "n_b"
    assert state["player_location_node_id"] == "n_b"
    assert state["player_location_region"] == "West"  # Bryn's region
    assert "n_c" in state["revealed_node_ids"]  # fog opens around Bryn
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["leg_index"] == 1
    assert travel["leg_progress"] == pytest.approx(0.0)


def test_full_journey_arrives_and_clears_travel():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d", "player_location_region": "East"})
    for _ in range(10):
        if not state["module_data"]["wb_worldgen"]["travel"]:
            break
        run_turn(state, {})

    assert state["player_location_node_id"] == "n_d"
    assert state["player_location_region"] == "East"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def test_instant_mode_keeps_classic_teleport():
    wbg._services = {"settings": FakeSettings({"world.travel_turns_per_edge": 0})}
    state = make_state(make_world())
    result = run_turn(state, {"player_location_node_id": "n_d"})

    assert result["player_location_node_id"] == "n_d"
    assert state["player_location_node_id"] == "n_d"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def test_interrupted_journey_does_not_advance():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    before = dict(state["module_data"]["wb_worldgen"]["travel"])

    run_turn(state, {"travel_interrupted": True})
    after = state["module_data"]["wb_worldgen"]["travel"]
    assert after["leg_index"] == before["leg_index"]
    assert after["leg_progress"] == pytest.approx(before["leg_progress"])


def test_new_destination_reroutes_from_last_reached_node():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    run_turn(state, {})  # reach Bryn
    assert state["player_location_node_id"] == "n_b"

    # The player turns back toward Aldern mid-journey.
    run_turn(state, {"player_location_node_id": "n_a"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["route"] == ["n_b", "n_a"]
    assert travel["destination_node_id"] == "n_a"


def test_unreachable_destination_falls_back_to_teleport():
    state = make_state(make_world())
    result = run_turn(state, {"player_location_node_id": "n_x"})

    assert result["player_location_node_id"] == "n_x"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def make_two_map_world():
    """The line world plus a parallel 'undercroft' map joined by a cave mouth
    at Bryn (world_format 2 comes from the in-place migration on first turn)."""
    world = make_world()
    world["maps"] = None  # force migration path from legacy 'map' key
    world.pop("maps")
    return world


def test_instant_passage_lands_on_the_far_map():
    state = make_state(make_world())
    # Migrate by running a no-op turn, then bolt on a second map + connection.
    run_turn(state, {})
    wd = state["world_data"]
    wd["maps"]["undercroft"] = {
        "map_id": "undercroft", "label": "The Undercroft", "level_type": "underground",
        "description": "", "parent_map_id": "root", "anchor_node_id": None,
        "generator_id": "world_map", "schema": 2,
        "nodes": [{"id": "u_1", "name": "Cave Landing", "type": "cavern", "x": 0, "y": 0}],
        "edges": [], "config": {},
    }
    wd["connections"] = [{
        "id": "c_cave", "from": {"map_id": "root", "node_id": "n_b"},
        "to": {"map_id": "undercroft", "node_id": "u_1"},
        "kind": "cave_mouth", "name": "The Sinkhole", "description": "",
        "travel": {"mode": "instant"}, "bidirectional": True,
        "requirements": "", "hidden": False, "origin": "generated",
    }]

    # The player is at n_a; the passage is at n_b — approach starts first.
    result = run_turn(state, {"player_passage": "c_cave (cave_mouth: The Sinkhole -> The Undercroft: Cave Landing)"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["pending_connection_id"] == "c_cave"
    assert travel["destination_node_id"] == "n_b"

    # Second turn completes the leg to n_b and rolls straight into the hop.
    result = run_turn(state, {})
    assert result["player_location_node_id"] == "u_1"
    assert result["player_location_map_id"] == "undercroft"
    assert state["module_data"]["wb_worldgen"]["travel"] is None
    assert "u_1" in state["revealed_node_ids"]


def test_journey_passage_transits_over_turns():
    state = make_state(make_world())
    run_turn(state, {})
    wd = state["world_data"]
    wd["maps"]["kepler"] = {
        "map_id": "kepler", "label": "Kepler-3", "level_type": "planet",
        "description": "", "parent_map_id": "root", "anchor_node_id": None,
        "generator_id": "world_map", "schema": 2,
        "nodes": [{"id": "k_port", "name": "Landing Field", "type": "port", "x": 0, "y": 0}],
        "edges": [], "config": {},
    }
    wd["connections"] = [{
        "id": "c_shuttle", "from": {"map_id": "root", "node_id": "n_a"},
        "to": {"map_id": "kepler", "node_id": "k_port"},
        "kind": "shuttle", "name": "Dawnrunner", "description": "",
        "travel": {"mode": "journey", "turns": 3}, "bidirectional": True,
        "requirements": "", "hidden": False, "origin": "generated",
    }]

    # Standing at the shuttle: the transit starts (turn 1 of 3).
    run_turn(state, {"player_passage": "c_shuttle"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["phase"] == "transit"
    assert travel["transit_turns_left"] == 2
    context = wbg._build_location_context(state, wd)
    assert "IN TRANSIT" in context and "Dawnrunner" in context

    run_turn(state, {})  # turn 2
    assert state["module_data"]["wb_worldgen"]["travel"]["transit_turns_left"] == 1
    result = run_turn(state, {})  # turn 3 — arrival
    assert result["player_location_node_id"] == "k_port"
    assert result["player_location_map_id"] == "kepler"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def test_select_option_labels_are_stripped():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d (Dunmore)"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["destination_node_id"] == "n_d"


def test_en_route_context_describes_the_journey():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})

    context = wbg._build_location_context(state, state["world_data"])
    assert "EN ROUTE" in context
    assert "Aldern" in context and "Bryn" in context
    assert "Dunmore" in context
    assert "NOT yet arrived" in context


def test_disabling_travel_mid_journey_drops_the_record():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    wbg._services = {"settings": FakeSettings({"world.travel_turns_per_edge": 0})}

    result = run_turn(state, {})
    assert state["module_data"]["wb_worldgen"]["travel"] is None
    assert "player_location_node_id" not in result


def _two_map_state():
    """Migrated line world + an undercroft map joined by one hidden and one
    open connection, player at n_a."""
    state = make_state(make_world())
    run_turn(state, {})  # migrate in place
    wd = state["world_data"]
    wd["maps"]["undercroft"] = {
        "map_id": "undercroft", "label": "The Undercroft", "level_type": "underground",
        "description": "", "parent_map_id": "root", "anchor_node_id": None,
        "generator_id": "world_map", "schema": 2,
        "nodes": [{"id": "u_1", "name": "Cave Landing", "type": "cavern", "x": 0, "y": 0}],
        "edges": [], "config": {},
    }
    wd["connections"] = [{
        "id": "c_secret", "from": {"map_id": "root", "node_id": "n_a"},
        "to": {"map_id": "undercroft", "node_id": "u_1"},
        "kind": "trapdoor", "name": "Hidden Trapdoor", "description": "",
        "travel": {"mode": "instant"}, "bidirectional": True,
        "requirements": "", "hidden": True, "origin": "generated",
    }]
    return state


def test_improvised_transition_one_time_leaves_no_connection():
    state = make_state(make_world())
    run_turn(state, {})
    result = run_turn(state, {
        "custom_transition": "lockpicked the mill's rear window",
        "custom_transition_target": "n_b (Bryn — visited, on Aldern)",
        "custom_transition_becomes": "one_time (leaves no usable way behind)",
    })
    assert result["player_location_node_id"] == "n_b"
    assert state["world_data"].get("connections", []) == []


def test_improvised_transition_open_passage_persists_and_dedupes():
    state = make_state(make_world())
    run_turn(state, {})
    mutation = {
        "custom_transition": "blew a hole in the western wall",
        "custom_transition_target": "n_b (Bryn)",
        "custom_transition_becomes": "open_passage (a permanent open way)",
    }
    run_turn(state, mutation)
    conns = state["world_data"]["connections"]
    assert len(conns) == 1
    assert conns[0]["origin"] == "improvised" and conns[0]["bidirectional"]
    assert conns[0]["requirements"] == ""
    # Same endpoints again: reuse, never duplicate.
    state["player_location_node_id"] = "n_a"
    run_turn(state, mutation)
    assert len(state["world_data"]["connections"]) == 1


def test_improvised_conditional_passage_carries_requirements():
    state = make_state(make_world())
    run_turn(state, {})
    run_turn(state, {
        "custom_transition": "pried open the sewer grate",
        "custom_transition_target": "n_c (Cael)",
        "custom_transition_becomes": "conditional_passage (permanent but gated)",
    })
    (c,) = state["world_data"]["connections"]
    assert c["requirements"] == "pried open the sewer grate"


def test_teleport_to_visited_node_on_another_map():
    state = _two_map_state()
    state["revealed_node_ids"].append("u_1")
    result = run_turn(state, {
        "custom_transition": "spoke the word of recall",
        "custom_transition_target": "u_1 (Cave Landing — visited, on The Undercroft)",
        "custom_transition_becomes": "one_time (leaves no usable way behind)",
    })
    assert result["player_location_map_id"] == "undercroft"
    assert result["player_location_node_id"] == "u_1"
    # A teleport is not a doorway: only the pre-existing hidden connection
    # remains, and matching endpoints just unhide it rather than duplicate.
    conns = state["world_data"]["connections"]
    assert len(conns) == 1 and conns[0]["id"] == "c_secret"
    assert conns[0]["hidden"] is False  # matching endpoints revealed it


def test_hidden_connection_is_not_offered_until_discovered():
    state = _two_map_state()
    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    passages = schema.get("player_passage", {}).get("options", [])
    assert not any(p.startswith("c_secret") for p in passages)
    # The storyteller sees it marked SECRET in context.
    context = wbg._build_location_context(state, state["world_data"])
    assert "SECRET" in context and "Hidden Trapdoor" in context
    # Discovery is offered, and unhides it for future turns.
    discover = schema["discover_passage"]["options"]
    assert any(o.startswith("c_secret") for o in discover)
    run_turn(state, {"discover_passage": discover[0]})
    assert state["world_data"]["connections"][0]["hidden"] is False
    schema2 = asyncio.run(wbg.on_mutation_schema(state, None))
    assert any(p.startswith("c_secret") for p in schema2["player_passage"]["options"])
