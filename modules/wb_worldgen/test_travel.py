"""Time-based travel tests.

A declared destination becomes a cross-map itinerary with a duration in
in-world minutes; each turn the Reader-extracted ``travel_minutes_covered``
advances it (``travel_completed`` finishes it early, ``travel_interrupted``
pauses it). Run by path with the venv python:

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
    """Default pace: 20 in-world minutes per average map leg."""
    wbg._services = {"settings": FakeSettings({"world.travel_minutes_per_edge": 20})}
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
    assert travel["destination_node_id"] == "n_d"
    assert travel["eta_minutes"] == 60  # 3 average legs x 20 min
    assert travel["minutes_traveled"] == 0
    (segment,) = travel["itinerary"]["segments"]
    assert segment["kind"] == "route"
    assert segment["nodes"] == ["n_a", "n_b", "n_c", "n_d"]
    assert travel["itinerary"]["ee_total"] == pytest.approx(3.0)
    # No narrated travel time yet — the player has not moved.
    assert "player_location_node_id" not in result
    assert state["player_location_node_id"] == "n_a"


def test_waypoints_are_reached_and_revealed_along_the_way():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    # 25 of 60 minutes: past Bryn (1.0 ee), a quarter into the next leg.
    result = run_turn(state, {"travel_minutes_covered": 25})

    assert result["player_location_node_id"] == "n_b"
    assert state["player_location_node_id"] == "n_b"
    assert state["player_location_region"] == "West"  # Bryn's region
    assert "n_b" in state["revealed_node_ids"]  # the reached waypoint is revealed
    # Neighbors are NOT revealed anymore — they form the name-only fringe
    # the map renders faded (details stay hidden until the player goes there).
    assert "n_c" not in state["revealed_node_ids"]
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["minutes_traveled"] == 25
    assert travel["waypoint_cursor"] == 1


def test_minutes_covered_accepts_string_values():
    # The mutation schema types the field as string; the reader may return "25".
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    run_turn(state, {"travel_minutes_covered": "25"})
    assert state["module_data"]["wb_worldgen"]["travel"]["minutes_traveled"] == 25


def test_full_journey_arrives_and_clears_travel():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d", "player_location_region": "East"})
    run_turn(state, {"travel_minutes_covered": 60})

    assert state["player_location_node_id"] == "n_d"
    assert state["player_location_region"] == "East"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def test_travel_completed_finishes_the_journey_early():
    # The storyteller narrated the whole uneventful trip in one scene; the
    # reader reports travel_completed and the player arrives now.
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    result = run_turn(state, {"travel_completed": True})

    assert result["player_location_node_id"] == "n_d"
    assert state["module_data"]["wb_worldgen"]["travel"] is None
    # Waypoints along the skipped stretch are still revealed.
    assert "n_c" in state["revealed_node_ids"]


def test_arrival_reveals_only_the_visited_node():
    # Landing somewhere opens fog on that node alone; its neighbors become
    # the map's name-only fringe instead of being revealed outright.
    wbg._services = {"settings": FakeSettings({"world.travel_minutes_per_edge": 0})}
    state = make_state(make_world())
    state["revealed_node_ids"] = ["n_a"]
    run_turn(state, {"player_location_node_id": "n_b"})
    assert set(state["revealed_node_ids"]) == {"n_a", "n_b"}


def test_fringe_is_one_edge_beyond_the_revealed_set():
    from wbruntime.worldspace import ensure_v2, fringe_node_ids
    state = make_state(make_world())
    ensure_v2(state)
    world = state["world_data"]
    assert fringe_node_ids(world, {"n_a", "n_b"}) == {"n_c"}
    assert fringe_node_ids(world, {"n_b"}) == {"n_a", "n_c"}
    # The unreachable island has no edges — never part of any fringe.
    assert fringe_node_ids(world, {"n_a", "n_b", "n_c", "n_d"}) == set()


def test_intent_roster_offers_the_name_only_fringe():
    # Fringe names are visible on the map, so the player can head for them;
    # nodes beyond the fringe stay unlisted.
    from wbruntime.intent import _destination_roster
    state = make_state(make_world())  # revealed: n_a, n_b
    roster = "\n".join(_destination_roster(state["world_data"], state))
    assert "n_b: Bryn" in roster
    assert "n_c: Cael" in roster  # fringe of n_b
    assert "n_d" not in roster  # two edges out — still unknown


def test_instant_mode_keeps_classic_teleport():
    wbg._services = {"settings": FakeSettings({"world.travel_minutes_per_edge": 0})}
    state = make_state(make_world())
    result = run_turn(state, {"player_location_node_id": "n_d"})

    assert result["player_location_node_id"] == "n_d"
    assert state["player_location_node_id"] == "n_d"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def test_interrupted_journey_does_not_advance():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    run_turn(state, {"travel_minutes_covered": 25})
    before = state["module_data"]["wb_worldgen"]["travel"]["minutes_traveled"]

    run_turn(state, {"travel_interrupted": True, "travel_minutes_covered": 30})
    after = state["module_data"]["wb_worldgen"]["travel"]
    assert after["minutes_traveled"] == before


def test_new_destination_reroutes_from_last_reached_node():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    run_turn(state, {"travel_minutes_covered": 20})  # exactly reaches Bryn
    assert state["player_location_node_id"] == "n_b"

    # The player turns back toward Aldern mid-journey.
    run_turn(state, {"player_location_node_id": "n_a"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    (segment,) = travel["itinerary"]["segments"]
    assert segment["nodes"] == ["n_b", "n_a"]
    assert travel["destination_node_id"] == "n_a"
    assert travel["minutes_traveled"] == 0  # a fresh journey, fresh clock


def test_unreachable_destination_falls_back_to_teleport():
    state = make_state(make_world())
    result = run_turn(state, {"player_location_node_id": "n_x"})

    assert result["player_location_node_id"] == "n_x"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def test_legacy_turn_based_record_is_replanned_in_time():
    # A save from before time-based travel carries a route/leg record; the
    # next mutation converts it into a minutes-based journey to the same
    # destination.
    state = make_state(make_world())
    state["module_data"]["wb_worldgen"] = {"travel": {
        "route": ["n_a", "n_b", "n_c", "n_d"], "leg_index": 0,
        "leg_progress": 5.0, "leg_distance": 10.0,
        "destination_node_id": "n_d", "map_id": "root", "phase": "approach",
    }}
    run_turn(state, {})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert "itinerary" in travel
    assert travel["destination_node_id"] == "n_d"

    result = run_turn(state, {"travel_completed": True})
    assert result["player_location_node_id"] == "n_d"


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

    # The player is at n_a; the passage is at n_b — one journey covers the
    # approach plus the instant hop (1 ee total = 20 minutes).
    run_turn(state, {"player_passage": "c_cave (cave_mouth: The Sinkhole -> The Undercroft: Cave Landing)"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["destination_node_id"] == "u_1"
    assert travel["destination_map_id"] == "undercroft"
    kinds = [s["kind"] for s in travel["itinerary"]["segments"]]
    assert kinds == ["route", "connection"]
    assert travel["eta_minutes"] == 20

    result = run_turn(state, {"travel_minutes_covered": 20})
    assert result["player_location_node_id"] == "u_1"
    assert result["player_location_map_id"] == "undercroft"
    assert state["module_data"]["wb_worldgen"]["travel"] is None
    assert "u_1" in state["revealed_node_ids"]


def test_journey_passage_transits_in_time():
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

    # Standing at the shuttle: a 3-ee crossing = 60 minutes at default pace.
    run_turn(state, {"player_passage": "c_shuttle"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["eta_minutes"] == 60
    context = wbg._build_location_context(state, wd)
    assert "EN ROUTE" in context and "Dawnrunner" in context

    run_turn(state, {"travel_minutes_covered": 30})  # mid-crossing
    assert state["player_location_node_id"] == "n_a"  # still aboard
    result = run_turn(state, {"travel_minutes_covered": 30})  # arrival
    assert result["player_location_node_id"] == "k_port"
    assert result["player_location_map_id"] == "kepler"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def test_select_option_labels_are_stripped():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d (Dunmore)"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["destination_node_id"] == "n_d"


def test_en_route_context_describes_the_journey_in_minutes():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    run_turn(state, {"travel_minutes_covered": 25})

    context = wbg._build_location_context(state, state["world_data"])
    assert "EN ROUTE" in context
    assert "Dunmore" in context
    assert "25" in context and "60" in context  # minutes elapsed / eta
    assert "Bryn" in context and "Cael" in context  # current stretch
    assert "whatever pace feels natural" in context


def test_disabling_travel_mid_journey_finishes_the_trip():
    state = make_state(make_world())
    run_turn(state, {"player_location_node_id": "n_d"})
    wbg._services = {"settings": FakeSettings({"world.travel_minutes_per_edge": 0})}

    result = run_turn(state, {})
    assert result["player_location_node_id"] == "n_d"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


def test_schema_offers_travel_fields():
    state = make_state(make_world())
    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    assert "travel_minutes_covered" in schema
    assert "travel_completed" not in schema  # no journey yet

    run_turn(state, {"player_location_node_id": "n_d"})
    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    assert "travel_completed" in schema


def make_hierarchy_world():
    """An instant-travel house interior anchored on a small town map, joined
    by a front door: Bedroom - Hallway |door| The House - Old Square - School."""
    return {
        "world_format": 2,
        "root_map_id": "root",
        "maps": {
            "root": {
                "map_id": "root", "label": "Kirkstad", "level_type": "region",
                "description": "", "parent_map_id": None, "anchor_node_id": None,
                "generator_id": "world_map", "schema": 2, "config": {},
                "nodes": [
                    {"id": "n_home", "name": "The House", "type": "building", "x": 0, "y": 0, "region": "Town"},
                    {"id": "n_square", "name": "Old Square", "type": "plaza", "x": 10, "y": 0, "region": "Town"},
                    {"id": "n_school", "name": "Ekdal School", "type": "school", "x": 20, "y": 0, "region": "Town"},
                ],
                "edges": [
                    {"from": "n_home", "to": "n_square", "distance": 10},
                    {"from": "n_square", "to": "n_school", "distance": 10},
                ],
            },
            "house": {
                "map_id": "house", "label": "The House", "level_type": "interior",
                "description": "", "parent_map_id": "root", "anchor_node_id": "n_home",
                "generator_id": "interior", "schema": 2,
                "config": {"instant_travel": True},
                "nodes": [
                    {"id": "h_bed", "name": "Bedroom", "type": "room", "x": 0, "y": 0},
                    {"id": "h_hall", "name": "Hallway", "type": "room", "x": 1, "y": 0},
                ],
                "edges": [{"from": "h_bed", "to": "h_hall", "distance": 1}],
            },
        },
        "connections": [{
            "id": "c_door", "from": {"map_id": "house", "node_id": "h_hall"},
            "to": {"map_id": "root", "node_id": "n_home"},
            "kind": "door", "name": "Front Door", "description": "",
            "travel": {"mode": "instant"}, "bidirectional": True,
            "requirements": "", "hidden": False, "origin": "generated",
        }],
        "regions": {"regions": [{"name": "Town", "terrain": "streets", "climate": "mild"}]},
    }


def test_cross_map_itinerary_spans_interior_door_and_overworld():
    world = make_hierarchy_world()
    itinerary = wbg._plan_itinerary(world, "house", "h_bed", "n_school")
    kinds = [s["kind"] for s in itinerary["segments"]]
    assert kinds == ["route", "connection", "route"]
    assert itinerary["segments"][0]["map_id"] == "house"
    assert itinerary["segments"][0]["leg_ee"] == [0.0]  # instant interior
    assert itinerary["segments"][1]["connection_id"] == "c_door"
    assert itinerary["segments"][2]["nodes"] == ["n_home", "n_square", "n_school"]
    assert itinerary["ee_total"] == pytest.approx(2.0)
    assert itinerary["destination_map_id"] == "root"


def test_cross_map_journey_travels_and_arrives():
    state = {
        "player_location_node_id": "h_bed",
        "player_location_map_id": "house",
        "player_location_region": "Town",
        "revealed_node_ids": ["h_bed", "h_hall", "n_home"],
        "world_data": make_hierarchy_world(),
        "module_data": {},
    }
    run_turn(state, {"player_location_node_id": "n_school"})
    travel = state["module_data"]["wb_worldgen"]["travel"]
    assert travel["eta_minutes"] == 40  # 2 paced legs x 20 min

    # 25 of 40 minutes: through the house and door (free), past Old Square.
    run_turn(state, {"travel_minutes_covered": 25})
    assert state["player_location_node_id"] == "n_square"
    assert state["player_location_map_id"] == "root"

    result = run_turn(state, {"travel_completed": True})
    assert result["player_location_node_id"] == "n_school"
    assert result["player_location_map_id"] == "root"
    assert state["module_data"]["wb_worldgen"]["travel"] is None


# --- Travel intent (pre-storyteller destination detection) -----------------

class FakeIntentLLM:
    mode = "live"
    reader_model = "fake-reader"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def simple_completion(self, messages, model=None, response_format=None,
                                inspector_ctx=None, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return self.responses.pop(0)

    async def get_embedding(self, text, inspector_ctx=None):
        return [1.0, 0.0, 0.0]


class FakeWorldMemory:
    def __init__(self, entries):
        self.entries = entries

    def has_world_index(self):
        return True

    def search_world(self, vector, limit=3, with_scores=False):
        return self.entries[:limit]


class FakeEngine:
    def __init__(self, llm, memory=None):
        self.llm = llm
        self.memory = memory


def _intent_services(llm, memory=None, resolution="roster"):
    return {
        "settings": FakeSettings({
            "world.travel_minutes_per_edge": 20,
            "world.destination_resolution": resolution,
            "world.site_expansion_mode": "off",
        }),
        "engine": FakeEngine(llm, memory),
    }


def _intent_state():
    state = make_state(make_world())
    state["revealed_node_ids"] = ["n_a", "n_b", "n_c", "n_d"]
    state["input_text"] = "I set out along the road to Dunmore"
    return state


def test_intent_roster_mode_starts_journey_with_llm_eta():
    llm = FakeIntentLLM(['{"traveling": true, "destination_node_id": "n_d", '
                         '"destination": "Dunmore", "transport": "walking", "eta_minutes": 45}'])
    wbg._services = _intent_services(llm, resolution="roster")
    state = _intent_state()

    result = asyncio.run(wbg._evaluate_travel_intent(state))
    travel = result["module_data"]["wb_worldgen"]["travel"]
    assert travel["destination_node_id"] == "n_d"
    assert travel["eta_minutes"] == 45  # the LLM estimate, not ee x pace
    assert travel["transport"] == "walking"
    assert "<travel_plan>" in result["context_string"]
    assert "45 in-world minutes" in result["context_string"]
    # The roster listed the revealed named nodes for direct selection.
    assert "n_b: Bryn" in llm.prompts[0]
    assert "n_d: Dunmore" in llm.prompts[0]


def test_intent_semantic_mode_resolves_via_world_search():
    llm = FakeIntentLLM([
        '{"traveling": true, "destination": "the town of Dunmore", "transport": "walking"}',
        '{"destination_node_id": "n_d", "eta_minutes": 30}',
    ])
    memory = FakeWorldMemory([{"source_type": "node", "source_id": "n_d"},
                              {"source_type": "lore", "source_id": "x"}])
    wbg._services = _intent_services(llm, memory, resolution="semantic")
    state = _intent_state()

    result = asyncio.run(wbg._evaluate_travel_intent(state))
    travel = result["module_data"]["wb_worldgen"]["travel"]
    assert travel["destination_node_id"] == "n_d"
    assert travel["eta_minutes"] == 30
    # Stage 1 saw no roster; stage 2 offered the searched candidates.
    assert "n_d" not in llm.prompts[0]
    assert "Dunmore" in llm.prompts[1]


def test_intent_not_traveling_is_a_no_op():
    llm = FakeIntentLLM(['{"traveling": false}'])
    wbg._services = _intent_services(llm)
    state = _intent_state()
    state["input_text"] = "I ask the innkeeper about the harvest"

    assert asyncio.run(wbg._evaluate_travel_intent(state)) == {}
    assert len(llm.prompts) == 1


def test_intent_skips_mock_and_empty_input():
    llm = FakeIntentLLM([])
    llm.mode = "mock"
    wbg._services = _intent_services(llm)
    state = _intent_state()
    assert asyncio.run(wbg._evaluate_travel_intent(state)) == {}

    llm2 = FakeIntentLLM([])
    wbg._services = _intent_services(llm2)
    state["input_text"] = ""
    assert asyncio.run(wbg._evaluate_travel_intent(state)) == {}
    assert llm2.prompts == []


def test_intent_same_destination_does_not_restart_journey():
    state = _intent_state()
    run_turn(state, {"player_location_node_id": "n_d"})
    llm = FakeIntentLLM(['{"traveling": true, "destination_node_id": "n_d", '
                         '"destination": "Dunmore", "transport": "walking", "eta_minutes": 45}'])
    wbg._services = _intent_services(llm)

    assert asyncio.run(wbg._evaluate_travel_intent(state)) == {}


# --- Improvised transitions and secrets (mechanics unchanged) --------------

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


def test_new_location_authoring_is_anchored_at_player(monkeypatch):
    # A destination that doesn't exist yet is authored with the player's
    # current node as placement anchor, so "the school's storage building"
    # lands next to where the story is, not across the map.
    state = make_state(make_world())
    captured = {}

    class FakeBuilder:
        async def author_location(self, world_id, description, anchor_node_id=None):
            captured["description"] = description
            captured["anchor_node_id"] = anchor_node_id
            return None  # authoring outcome doesn't matter here — the anchor does

    monkeypatch.setattr(wbg, "world_builder", FakeBuilder())
    run_turn(state, {
        "custom_transition": "walked over to the storage building",
        "custom_transition_new_location": "the school's storage building",
    })
    assert captured["description"] == "the school's storage building"
    assert captured["anchor_node_id"] == "n_a"


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


def test_hidden_connection_far_node_not_a_transition_target():
    """The far side of an undiscovered way must not be offered as an
    improvised-transition target ('beyond a known way') — that would
    pre-leak the secret. Discovery makes it a normal candidate."""
    state = _two_map_state()
    schema = asyncio.run(wbg.on_mutation_schema(state, None))
    targets = schema.get("custom_transition_target", {}).get("options", [])
    assert not any(t.startswith("u_1") for t in targets)
    run_turn(state, {"discover_passage": schema["discover_passage"]["options"][0]})
    schema2 = asyncio.run(wbg.on_mutation_schema(state, None))
    targets2 = schema2.get("custom_transition_target", {}).get("options", [])
    assert any(t.startswith("u_1") for t in targets2)
