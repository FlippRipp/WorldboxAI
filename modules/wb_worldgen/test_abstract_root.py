"""Authored abstract root maps: worlds with map_style "abstract" get a
conceptual node graph authored from the hierarchy guidance and the authored
places — not the procedural Poisson scatter (the "Lustra System" failure).

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_abstract_root.py
"""

import asyncio
import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.generation.abstract_graph import (
    ensure_crossing_nodes, layout_abstract_graph, mock_abstract_parsed,
    normalize_abstract_graph)


@pytest.fixture
def builder():
    d = tempfile.mkdtemp(prefix="wb_abstract_")
    wb = WorldBuilder(worlds_dir=d)
    register_default_steps(wb)
    yield wb
    shutil.rmtree(d, ignore_errors=True)


AREAS = [
    {"name": "The Glimmering Core", "terrain": "orbital habitats", "description": "inner system"},
    {"name": "The Tattered Belt", "terrain": "drifting asteroids", "description": "lawless"},
]

NAMED = [
    {"name": "Fleshport", "category": "landmark", "region": "The Tattered Belt",
     "description": "A lawless asteroid den."},
    {"name": "The Halo Ring", "category": "landmark", "region": "The Glimmering Core",
     "description": "A ring habitat."},
    {"name": "The Auction Blocks", "category": "settlement",
     "region": "The Tattered Belt", "part_of": "Fleshport", "relation": "adjacent",
     "description": "Where the cartel trades."},
]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_resolves_regions_and_recovers_descriptions():
    parsed = {"description": "A twin-zone system.", "nodes": [
        {"name": "Cinder", "kind": "planet", "region": "glimmering core",
         "importance": 9, "description": "A scorched world.",
         "adjacent": ["Fleshport"]},
        {"name": "Fleshport", "kind": "station", "region": "The Tattered Belt",
         "importance": 8, "description": "", "adjacent": ["Cinder"],
         "contains": ["The Auction Blocks"]},
        {"name": "The Halo Ring", "kind": "station", "region": "Nowhere",
         "importance": 7, "description": "Glittering ring.", "adjacent": []},
    ]}
    graph = normalize_abstract_graph(parsed, NAMED, AREAS)
    by_name = {n["name"]: n for n in graph["nodes"]}
    assert by_name["Cinder"]["region"] == "The Glimmering Core"
    # Empty authored description recovered from the named-location list.
    assert by_name["Fleshport"]["description"] == "A lawless asteroid den."
    # Contains folded with description; unknown region blanked.
    contained = by_name["Fleshport"]["contained_locations"]
    assert contained == [{"name": "The Auction Blocks",
                          "description": "Where the cartel trades."}]
    assert by_name["The Halo Ring"]["region"] == ""
    # Adjacency deduped to one symmetric edge.
    assert len(graph["edges"]) == 1
    assert graph["description"] == "A twin-zone system."


def test_normalize_never_drops_authored_places():
    # The author forgot The Halo Ring and The Auction Blocks entirely.
    parsed = {"nodes": [
        {"name": "Fleshport", "kind": "station", "region": "The Tattered Belt",
         "importance": 8, "description": "d", "adjacent": []},
        {"name": "Lustra", "kind": "star", "region": "The Glimmering Core",
         "importance": 10, "description": "The sun.", "adjacent": ["Fleshport"]},
    ]}
    graph = normalize_abstract_graph(parsed, NAMED, AREAS)
    by_name = {n["name"]: n for n in graph["nodes"]}
    # Anchored place folds into its anchor node.
    assert {c["name"] for c in by_name["Fleshport"]["contained_locations"]} == \
        {"The Auction Blocks"}
    # Region-matched place folds into its region's most important node.
    assert {c["name"] for c in by_name["Lustra"]["contained_locations"]} == \
        {"The Halo Ring"}


def test_normalize_dedups_article_variants_and_assigns_types():
    parsed = {"nodes": [
        {"name": "The Halo Ring", "importance": 9, "adjacent": []},
        {"name": "Halo Ring", "importance": 5, "adjacent": []},  # duplicate
        {"name": "Relay Gate", "importance": 3, "adjacent": []},
    ]}
    graph = normalize_abstract_graph(parsed, [], AREAS)
    names = [n["name"] for n in graph["nodes"]]
    assert names == ["The Halo Ring", "Relay Gate"]
    assert graph["nodes"][0]["type"] == "settlement"  # importance >= 8
    assert graph["nodes"][1]["type"] == "waypoint"    # importance < 5


def test_ensure_crossing_nodes_uses_authored_then_synthesizes():
    parsed = {"nodes": [
        {"name": "Neural Plaza", "kind": "plaza", "importance": 6,
         "crossing": "The Datasphere", "adjacent": []},
        {"name": "Hub", "importance": 9, "adjacent": ["Neural Plaza"]},
    ]}
    graph = normalize_abstract_graph(parsed, [], [], id_prefix="root_")
    crossings = ensure_crossing_nodes(graph, "The Datasphere", "neural_jack", 2,
                                      "root_")
    assert len(crossings) == 2
    assert crossings[0]["name"] == "Neural Plaza"
    assert all(c["type"] == "neural_jack" for c in crossings)
    # The synthesized crossing is wired to the most important node.
    synth = crossings[1]
    assert any({e["from"], e["to"]} == {synth["id"], graph["nodes"][1]["id"]}
               for e in graph["edges"])


def _components(nodes, edges):
    parent = {n["id"]: n["id"] for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        parent[find(e["from"])] = find(e["to"])
    return len({find(n["id"]) for n in nodes})


def test_layout_positions_regions_and_repairs_connectivity():
    parsed = {"nodes": [
        {"name": f"Node {i}", "importance": 5 + (i % 5),
         "region": AREAS[i % 2]["name"], "adjacent": []}
        for i in range(10)
    ]}
    graph = normalize_abstract_graph(parsed, [], AREAS)
    assert not graph["edges"]  # nothing authored
    result = layout_abstract_graph(graph, AREAS, generated_from="seed")
    assert _components(result["nodes"], result["edges"]) == 1
    for n in result["nodes"]:
        assert 0 <= n["x"] <= 1000 and 0 <= n["y"] <= 1000
    assert {r["region_name"] for r in result["regions"]} == \
        {a["name"] for a in AREAS}
    # Nodes of one region cluster nearer their own centroid than the other's.
    import math
    cents = {}
    for r in result["regions"]:
        members = [n for n in result["nodes"] if n["id"] in r["node_ids"]]
        cents[r["region_name"]] = (sum(n["x"] for n in members) / len(members),
                                   sum(n["y"] for n in members) / len(members))
    for n in result["nodes"]:
        own = cents[n["region"]]
        other = cents[[a["name"] for a in AREAS if a["name"] != n["region"]][0]]
        assert math.hypot(n["x"] - own[0], n["y"] - own[1]) <= \
            math.hypot(n["x"] - other[0], n["y"] - other[1])
    assert result["config"]["generated_from"] == "seed"


def test_normalize_carries_orbital_structure_and_links_satellites():
    parsed = {"nodes": [
        {"name": "Lustra", "kind": "star", "importance": 10, "center": True,
         "adjacent": []},
        {"name": "Cinder", "kind": "planet", "importance": 7, "orbit": 1,
         "adjacent": ["Lustra"]},
        {"name": "Mirage", "kind": "planet", "importance": 9, "orbit": 3,
         "adjacent": []},
        {"name": "The Slick", "kind": "moon", "importance": 6,
         "parent": "mirage", "adjacent": []},
        {"name": "Ghost Moon", "kind": "moon", "importance": 3,
         "parent": "Nowhere", "adjacent": []},
    ]}
    graph = normalize_abstract_graph(parsed, [], [])
    by = {n["name"]: n for n in graph["nodes"]}
    assert by["Lustra"]["center"] is True
    assert by["Cinder"]["orbit"] == 1 and by["Mirage"]["orbit"] == 3
    # parent resolves tolerantly; unknown parents drop.
    assert by["The Slick"]["parent_id"] == by["Mirage"]["id"]
    assert "parent_id" not in by["Ghost Moon"]
    # A satellite always has a travel route to its parent.
    assert any({e["from"], e["to"]} == {by["The Slick"]["id"], by["Mirage"]["id"]}
               for e in graph["edges"])


def test_normalize_keeps_only_the_most_important_center():
    parsed = {"nodes": [
        {"name": "False Sun", "importance": 6, "center": True, "adjacent": []},
        {"name": "True Sun", "importance": 10, "center": True, "adjacent": []},
    ]}
    graph = normalize_abstract_graph(parsed, [], [])
    by = {n["name"]: n for n in graph["nodes"]}
    assert by["True Sun"].get("center") is True
    assert "center" not in by["False Sun"]
    assert by["False Sun"]["orbit"] == 1  # demoted to the innermost ring


def test_orbital_layout_rings_center_and_satellites():
    import math
    parsed = {"nodes": [
        {"name": "Lustra", "kind": "star", "importance": 10, "center": True,
         "adjacent": []},
        {"name": "Cinder", "kind": "planet", "importance": 7, "orbit": 1,
         "adjacent": ["Lustra"]},
        {"name": "Verdantia", "kind": "planet", "importance": 8, "orbit": 2,
         "adjacent": []},
        {"name": "Mirage", "kind": "planet", "importance": 9, "orbit": 5,
         "adjacent": []},
        {"name": "The Slick", "kind": "moon", "importance": 6,
         "parent": "Mirage", "adjacent": []},
        {"name": "Neon Docks", "kind": "station", "importance": 8,
         "parent": "Mirage", "adjacent": []},
        {"name": "Drifter Relay", "kind": "gate", "importance": 4,
         "adjacent": []},  # no orbit: lands on an added outermost ring
    ]}
    graph = normalize_abstract_graph(parsed, [], [])
    result = layout_abstract_graph(graph, [], generated_from="seed")
    by = {n["name"]: n for n in result["nodes"]}

    def dist_from_center(n):
        return math.hypot(n["x"] - 500.0, n["y"] - 500.0)

    # Hub in the middle; ring order follows orbit order, not absolute values.
    assert dist_from_center(by["Lustra"]) < 1.0
    assert dist_from_center(by["Cinder"]) < dist_from_center(by["Verdantia"]) \
        < dist_from_center(by["Mirage"])
    assert dist_from_center(by["Drifter Relay"]) > dist_from_center(by["Mirage"]) - 25
    # Satellites hug their parent.
    for moon in ("The Slick", "Neon Docks"):
        d = math.hypot(by[moon]["x"] - by["Mirage"]["x"],
                       by[moon]["y"] - by["Mirage"]["y"])
        assert d <= 110
    # Ring metadata for renderers; graph fully connected.
    orbits = result["config"]["orbits"]
    assert orbits["center_node_id"] == by["Lustra"]["id"]
    assert [r["orbit"] for r in orbits["rings"]] == [1, 2, 5, 6]
    radii = [r["radius"] for r in orbits["rings"]]
    assert radii == sorted(radii)
    assert _components(result["nodes"], result["edges"]) == 1


def test_layout_without_structure_hints_stays_in_cluster_mode():
    parsed = {"nodes": [
        {"name": f"Node {i}", "importance": 6, "region": AREAS[i % 2]["name"],
         "adjacent": []} for i in range(6)
    ]}
    graph = normalize_abstract_graph(parsed, [], AREAS)
    result = layout_abstract_graph(graph, AREAS)
    assert "orbits" not in result["config"]


def test_cluster_mode_satellites_hug_their_parent_too():
    import math
    parsed = {"nodes": [
        {"name": "Fleshport", "importance": 9, "region": AREAS[1]["name"],
         "adjacent": []},
        {"name": "Chop Dock", "importance": 5, "parent": "Fleshport",
         "adjacent": []},
        {"name": "Halo Ring", "importance": 8, "region": AREAS[0]["name"],
         "adjacent": ["Fleshport"]},
    ]}
    graph = normalize_abstract_graph(parsed, [], AREAS)
    result = layout_abstract_graph(graph, AREAS)
    by = {n["name"]: n for n in result["nodes"]}
    d = math.hypot(by["Chop Dock"]["x"] - by["Fleshport"]["x"],
                   by["Chop Dock"]["y"] - by["Fleshport"]["y"])
    assert d <= 110
    assert "orbits" not in result["config"]
    # Satellites still count as region members.
    belt = next(r for r in result["regions"]
                if r["region_name"] == AREAS[1]["name"])
    assert by["Chop Dock"]["id"] in belt["node_ids"] or \
        by["Chop Dock"]["region"] == ""


def test_mock_parsed_covers_named_locations_and_areas():
    parsed = mock_abstract_parsed("Testworld", AREAS, NAMED)
    names = [n["name"] for n in parsed["nodes"]]
    assert "Fleshport" in names and "The Halo Ring" in names
    graph = normalize_abstract_graph(parsed, NAMED, AREAS)
    result = layout_abstract_graph(graph, AREAS)
    assert _components(result["nodes"], result["edges"]) == 1


# ---------------------------------------------------------------------------
# Facade integration (mock mode)
# ---------------------------------------------------------------------------

def _abstract_state(parallel=True):
    hierarchy = {
        "levels": [
            {"level_type": "system_graph", "label": "Lustra System",
             "generator_id": "world_map",
             "guidance": "Nodes are celestial bodies and major stations."},
            {"level_type": "interior", "label": "Interior",
             "generator_id": "interior", "nestable": True, "guidance": ""},
        ],
    }
    if parallel:
        hierarchy["parallel_maps"] = [
            {"label": "The Datasphere", "level_type": "datasphere",
             "description": "A neon VR plane.", "connection_kind": "neural_jack",
             "connection_count": 2}]
    return {
        "seed_prompt": "a lewd solar system",
        "steps": {
            "world_form": {"data": {
                "world_kind": "A single solar system of indulgence.",
                "map_style": "abstract", "skip_steps": [],
                "step_directives": [{"step_id": "map_generation",
                                     "directive": "Star, planets, stations."}],
            }, "approved": True},
            "hierarchy_design": {"data": hierarchy, "approved": True},
            "natural_landmarks": {"data": {
                "areas": AREAS,
                "landmarks": [
                    {"scope": "", "region": "The Tattered Belt",
                     "name": "Fleshport", "type": "asteroid den",
                     "description": "A lawless asteroid den."},
                    {"scope": "", "region": "The Glimmering Core",
                     "name": "The Halo Ring", "type": "orbital",
                     "description": "A ring habitat."},
                    {"scope": "The Datasphere", "region": "", "name": "The Nexus",
                     "type": "virtual nexus", "description": "Data heart."},
                ]}, "approved": True},
            "society_factions": {"data": {"factions": [
                {"scope": "", "region": "Fleshport", "name": "The Velvet Chain",
                 "type": "cartel", "description": "",
                 "settlements": ["The Flesh Markets"],
                 "significant_landmarks": []},
            ]}, "approved": True},
        },
    }


def test_abstract_world_gets_authored_multilayer_map(builder):
    state = _abstract_state(parallel=True)
    data = asyncio.run(builder.generate_step(
        "map_generation", state, state["seed_prompt"]))

    assert "layers" in data
    root_layer = data["layers"][0]
    # The authored level_type survives (was hardcoded "world" before).
    assert root_layer["layer_type"] == "system_graph"
    root_map = root_layer["map"]
    names = {n["name"] for n in root_map["nodes"] if n["name"]}
    assert "Fleshport" in names and "The Halo Ring" in names
    # The cartel settlement follows its landmark-named region reference.
    market = next((n for n in root_map["nodes"]
                   if n["name"] == "The Flesh Markets"), None)
    assert market is not None and market["region"] == "The Tattered Belt"
    # Every non-crossing node is named — no anonymous filler on abstract maps.
    for n in root_map["nodes"]:
        assert n["name"] or n["type"] == "neural_jack"
    assert {r["region_name"] for r in root_map["regions"]} <= \
        {a["name"] for a in AREAS}

    # The parallel plane is authored too, with paired crossings.
    plane_map = data["layers"][1]["map"]
    assert "The Nexus" in {n["name"] for n in plane_map["nodes"]}
    assert len(data["connections"]) == 2
    root_ids = {n["id"] for n in root_map["nodes"]}
    plane_ids = {n["id"] for n in plane_map["nodes"]}
    for c in data["connections"]:
        assert c["connection_type"] == "neural_jack"
        assert c["from_node_id"] in root_ids
        assert c["to_node_id"] in plane_ids


def test_abstract_world_without_planes_gets_flat_authored_map(builder):
    state = _abstract_state(parallel=False)
    data = asyncio.run(builder.generate_step(
        "map_generation", state, state["seed_prompt"]))
    assert "nodes" in data and "layers" not in data
    assert all(n["name"] for n in data["nodes"])
    assert _components(data["nodes"], data["edges"]) == 1
    # Flat root nodes carry no layer_id, exactly like the procedural path.
    assert all("layer_id" not in n for n in data["nodes"])


def test_terrain_worlds_keep_the_procedural_path(builder):
    state = _abstract_state(parallel=False)
    state["steps"]["world_form"]["data"]["map_style"] = "terrain"
    state["steps"]["hierarchy_design"]["data"]["levels"][0]["level_type"] = "world"
    data = asyncio.run(builder.generate_step(
        "map_generation", state, state["seed_prompt"],
        config={"total_nodes": 40}))
    # Procedural scatter: full node budget, unnamed filler nodes exist.
    assert len(data["nodes"]) == 40
    assert any(not n["name"] for n in data["nodes"])


def test_worlds_predating_the_design_step_stay_procedural(builder):
    state = _abstract_state(parallel=False)
    del state["steps"]["world_form"]
    data = asyncio.run(builder.generate_step(
        "map_generation", state, state["seed_prompt"],
        config={"total_nodes": 40}))
    assert len(data["nodes"]) == 40
