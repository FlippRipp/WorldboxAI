"""Procedural child-map expansion (plan M3a): a location opens into a map
built by any implemented generator, not just an authored interior.

The expansion call still decides the level (or honors a pinned one); interior
levels author locations exactly as before, procedural levels (world_map,
city_roadnet) build offline with a deterministic seed and get named lazily by
the play-time enrichment engine — whose results must persist into the child
map's bundle.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_procedural_expansion.py
"""

import asyncio
import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.expansion.maps_expand import (
    allowed_child_levels,
    child_map_id,
    is_expandable,
)

LEVELS = [
    {"level_type": "star_system", "label": "Star System", "generator_id": "world_map",
     "guidance": "Planets, stations and belts around the star."},
    {"level_type": "planet", "label": "Planet", "generator_id": "world_map",
     "guidance": "One planet's surface."},
    {"level_type": "city", "label": "City", "generator_id": "city_roadnet",
     "guidance": "One settlement's streets."},
    {"level_type": "interior", "label": "Interior", "generator_id": "interior",
     "nestable": True, "guidance": "Rooms of one structure."},
]


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_pexp_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    register_default_steps(wb)
    return wb


@pytest.fixture
def solar(builder):
    """A saved solar-system world with named anchors on the root map."""
    state = {"seed_prompt": "a solar system space opera", "steps": {
        "hierarchy_design": {"data": {"levels": [dict(l) for l in LEVELS]},
                             "approved": True}}}
    map_data = asyncio.run(builder.generate_step(
        "map_generation", state, state["seed_prompt"], config={"total_nodes": 40}))
    map_data["nodes"][0]["name"] = "Planet Kestrel"
    map_data["nodes"][1]["name"] = "Station Argo"
    state["steps"]["map_generation"] = {"data": map_data, "approved": True}
    builder.save_world("solar", state)
    return builder


def _root(builder, world_id="solar"):
    compiled = builder.compile_world(builder.load_world(world_id))
    return compiled, compiled["root_map_id"], compiled["maps"][compiled["root_map_id"]]


def _node_named(map_record, name):
    return next(n for n in map_record["nodes"] if n.get("name") == name)


# ---------------------------------------------------------------------------
# Allowed levels
# ---------------------------------------------------------------------------

def test_allowed_child_levels_offer_all_implemented_generators(solar):
    compiled, _, root = _root(solar)
    assert [l["level_type"] for l in allowed_child_levels(compiled, root)] == \
        ["planet", "city", "interior"]
    # Old worlds (default levels) still offer interiors only below the root.
    old = {"seed_prompt": "s", "steps": {}, "map": {"nodes": [], "edges": []}}
    old_compiled = solar.compile_world(old)
    old_root = {"level_type": "world"}
    assert [l["level_type"] for l in allowed_child_levels(old_compiled, old_root)] == \
        ["interior"]


# ---------------------------------------------------------------------------
# Procedural children
# ---------------------------------------------------------------------------

def test_planet_expansion_builds_procedural_world_map(solar):
    compiled, root_id, root = _root(solar)
    anchor = _node_named(root, "Planet Kestrel")
    bundle = asyncio.run(solar.expand_node("solar", root_id, anchor["id"],
                                           level_type="planet"))
    record = bundle["map"]
    assert record["level_type"] == "planet"
    assert record["generator_id"] == "world_map"
    assert record["parent_map_id"] == root_id
    assert record["anchor_node_id"] == anchor["id"]
    assert record["label"] == "Planet Kestrel"
    assert len(record["nodes"]) >= 20
    # Born unnamed: the play-time enrichment engine names them lazily.
    assert all(not n.get("name") for n in record["nodes"])
    # Node ids are namespaced by the child map id.
    assert all(n["id"].startswith(record["map_id"] + ":") for n in record["nodes"])
    # One entrance connection, anchored to the map's most important node.
    assert len(bundle["connections"]) == 1
    conn = bundle["connections"][0]
    assert conn["from"] == {"map_id": root_id, "node_id": anchor["id"]}
    arrival = next(n for n in record["nodes"] if n["id"] == conn["to"]["node_id"])
    assert arrival["importance"] == max(n.get("importance", 0) for n in record["nodes"])


def test_planet_expansion_is_deterministic(solar):
    compiled, root_id, root = _root(solar)
    anchor = _node_named(root, "Planet Kestrel")
    b1 = asyncio.run(solar.expand_node("solar", root_id, anchor["id"], level_type="planet"))
    b2 = asyncio.run(solar.expand_node("solar", root_id, anchor["id"],
                                       level_type="planet", force=True))
    assert [n["id"] for n in b1["map"]["nodes"]] == [n["id"] for n in b2["map"]["nodes"]]
    assert [(n["x"], n["y"]) for n in b1["map"]["nodes"]] == \
        [(n["x"], n["y"]) for n in b2["map"]["nodes"]]


def test_city_of_a_planet_and_interior_below(solar):
    compiled, root_id, root = _root(solar)
    anchor = _node_named(root, "Planet Kestrel")
    planet = asyncio.run(solar.expand_node("solar", root_id, anchor["id"],
                                           level_type="planet"))["map"]
    # Name one planet node (as play-time enrichment would), open it as a city.
    target = planet["nodes"][0]
    solar._save_node_enrichment("solar", target["id"], "name", "Port Vesta")
    city = asyncio.run(solar.expand_node("solar", planet["map_id"], target["id"],
                                         level_type="city"))["map"]
    assert city["level_type"] == "city"
    assert city["generator_id"] == "city_roadnet"
    assert city["parent_map_id"] == planet["map_id"]
    assert len(city["nodes"]) >= 20
    # A named city node opens further (interior is still available below).
    compiled = solar.compile_world(solar.load_world("solar"))
    named_city_node = dict(city["nodes"][0], name="The Custom House")
    assert is_expandable(compiled, city["map_id"], named_city_node)


def test_unpinned_expansion_defaults_to_authored_interior(solar):
    compiled, root_id, root = _root(solar)
    anchor = _node_named(root, "Station Argo")
    bundle = asyncio.run(solar.expand_node("solar", root_id, anchor["id"]))
    record = bundle["map"]
    # Mock picks the first authored level — the pre-procedural behavior.
    assert record["level_type"] == "interior"
    assert record["generator_id"] == "interior"
    assert all(n.get("name") for n in record["nodes"])


def test_pregenerate_honors_planned_level_type(solar):
    state = solar.load_world("solar")
    state["steps"]["hierarchy_design"]["data"]["pregenerate"] = [
        {"location_name": "Planet Kestrel", "level_type": "planet", "reason": "seed-central"}]
    solar.save_world("solar", state)
    summary = asyncio.run(solar.pregenerate_planned_maps("solar"))
    assert len(summary["built"]) == 1
    compiled = solar.compile_world(solar.load_world("solar"))
    built = compiled["maps"][summary["built"][0]]
    assert built["level_type"] == "planet"
    assert built["generator_id"] == "world_map"


# ---------------------------------------------------------------------------
# Enrichment persistence into child bundles
# ---------------------------------------------------------------------------

def test_child_map_enrichment_persists_into_bundle(solar):
    compiled, root_id, root = _root(solar)
    anchor = _node_named(root, "Planet Kestrel")
    planet = asyncio.run(solar.expand_node("solar", root_id, anchor["id"],
                                           level_type="planet"))["map"]
    target = planet["nodes"][3]
    solar._save_node_enrichment("solar", target["id"], "name", "The Glass Steppe")
    solar._save_node_enrichment("solar", target["id"], "description", "Wind over vitrified dunes.")
    # A fresh load (fresh builder = fresh caches) sees the enrichment.
    fresh = register_default_steps(WorldBuilder(worlds_dir=str(solar._worlds_dir)))
    reloaded = fresh.compile_world(fresh.load_world("solar"))
    node = next(n for n in reloaded["maps"][planet["map_id"]]["nodes"]
                if n["id"] == target["id"])
    assert node["name"] == "The Glass Steppe"
    assert node["description"] == "Wind over vitrified dunes."


def test_root_map_enrichment_path_unchanged(solar):
    compiled, root_id, root = _root(solar)
    unnamed = next(n for n in root["nodes"] if not n.get("name"))
    solar._save_node_enrichment("solar", unnamed["id"], "name", "Beacon Reach")
    solar._flush_enrichment_cache("solar")
    reloaded = solar.compile_world(solar.load_world("solar"))
    node = next(n for n in reloaded["maps"][root_id]["nodes"] if n["id"] == unnamed["id"])
    assert node["name"] == "Beacon Reach"


def test_authored_root_for_interior_root_level(builder):
    # Root-as-first-expansion (M3b): a world whose designed root level uses
    # the authored interior generator gets an authored root map — the whole
    # playable world is one place.
    state = {"seed_prompt": "a haunted mansion the guests cannot leave", "steps": {
        "hierarchy_design": {"data": {"levels": [
            {"level_type": "interior", "label": "The House", "generator_id": "interior",
             "nestable": True, "guidance": "Wings, halls and cellars of one manor."}]},
            "approved": True},
        "lore": {"data": {"world_name": "Blackwood Manor"}, "approved": True}}}
    data = asyncio.run(builder.generate_step("map_generation", state, state["seed_prompt"]))
    assert data["generator_id"] == "interior"
    assert data["nodes"] and all(n.get("name") for n in data["nodes"])
    state["steps"]["map_generation"] = {"data": data, "approved": True}
    builder.save_world("manor", state)
    compiled = builder.compile_world(builder.load_world("manor"))
    root = compiled["maps"][compiled["root_map_id"]]
    assert root["level_type"] == "interior"
    assert root["generator_id"] == "interior"
    # The nestable root still opens further interiors (a vault in the manor).
    assert [l["level_type"] for l in allowed_child_levels(compiled, root)] == ["interior"]
    assert is_expandable(compiled, compiled["root_map_id"], root["nodes"][1])


def test_procedural_roots_keep_the_procedural_path():
    from wbworldgen.worldgen.design import authored_root_level
    # A terrain/abstract/city root never routes through the authored flow.
    assert authored_root_level({"seed_prompt": "s", "steps": {}}, "world_map") is None
    assert authored_root_level({"seed_prompt": "s", "steps": {}}, "city_roadnet") is None
    state = {"seed_prompt": "s", "steps": {"hierarchy_design": {"data": {"levels": [
        {"level_type": "interior", "generator_id": "interior"}]}}}}
    level = authored_root_level(state, "interior")
    assert level and level["level_type"] == "interior"


def test_terrain_flag_normalization():
    from wbworldgen.worldgen.steps.hierarchy_design import normalize_hierarchy_design
    out = normalize_hierarchy_design({"levels": [
        {"level_type": "planet", "generator_id": "world_map", "terrain": "yes"},
        {"level_type": "belt", "generator_id": "world_map", "terrain": "no"},
        {"level_type": "city", "generator_id": "city_roadnet", "terrain": "yes"},
        {"level_type": "interior", "generator_id": "interior", "terrain": True},
    ]}, ["world_map", "city_roadnet", "interior"])
    by_type = {l["level_type"]: l for l in out["levels"]}
    assert by_type["planet"].get("terrain") is True
    assert "terrain" not in by_type["belt"]
    # Terrain is a world_map capability — dropped on other generators.
    assert "terrain" not in by_type["city"]
    assert "terrain" not in by_type["interior"]


def test_terrain_planet_child_gets_rasters(builder):
    import os

    class Settings:
        def get(self, key, default=None):
            return {"world.child_terrain_resolution": 128}.get(key, default)

    builder.set_settings(Settings())
    state = {"seed_prompt": "space opera", "steps": {
        "hierarchy_design": {"data": {"levels": [
            {"level_type": "star_system", "label": "Star System", "generator_id": "world_map",
             "guidance": "Planets and stations."},
            {"level_type": "planet", "label": "Planet", "generator_id": "world_map",
             "terrain": True, "guidance": "One planet's surface."},
            {"level_type": "interior", "label": "Interior", "generator_id": "interior",
             "nestable": True, "guidance": "Rooms."}]},
            "approved": True}}}
    map_data = asyncio.run(builder.generate_step(
        "map_generation", state, "space opera", config={"total_nodes": 40}))
    map_data["nodes"][0]["name"] = "Planet Kestrel"
    state["steps"]["map_generation"] = {"data": map_data, "approved": True}
    builder.save_world("terra", state)

    compiled = builder.compile_world(builder.load_world("terra"))
    root_id = compiled["root_map_id"]
    anchor = next(n for n in compiled["maps"][root_id]["nodes"] if n.get("name"))
    planet = asyncio.run(builder.expand_node("terra", root_id, anchor["id"],
                                             level_type="planet"))["map"]
    # Raster stack + rendered map images persisted under the child map's id —
    # the same key the terrain-image route serves and the map screen fetches.
    tdir = builder._persistence.terrain_dir("terra", planet["map_id"])
    assert {"biome.png", "hillshade.png", "layers.npz"} <= set(os.listdir(tdir))
    meta = planet["config"]["terrain"]
    assert meta["layer_id"] == planet["map_id"]
    assert meta["resolution"] == 128
    assert meta["summary"]
    # Enrichment attaches the child's terrain and samples biome per node.
    compiled2 = builder._enrichment._load_compiled("terra")
    assert planet["map_id"] in compiled2["_terrain_layers"]
    from wbworldgen.worldgen.enrichment.context import (
        build_enrichment_context, collect_nodes_by_layer)
    all_nodes, _ = collect_nodes_by_layer(compiled2)
    child_node = next(n for n in all_nodes if n["map_id"] == planet["map_id"])
    ctx = build_enrichment_context(child_node, all_nodes, compiled2,
                                   include_descriptions=False)
    assert ctx.get("terrain", {}).get("biome")


def test_unflagged_levels_expand_without_terrain(solar):
    compiled, root_id, root = _root(solar)
    anchor = _node_named(root, "Planet Kestrel")
    planet = asyncio.run(solar.expand_node("solar", root_id, anchor["id"],
                                           level_type="planet"))["map"]
    assert "terrain" not in (planet["config"] or {})


def test_parallel_plane_with_non_world_map_root(builder):
    # Generalized parallel maps (M3c): a city street-network root joined to
    # an abstract undercity plane by border crossings.
    state = {"seed_prompt": "a rain-slick city over a buried undercity", "steps": {
        "hierarchy_design": {"data": {
            "levels": [
                {"level_type": "city", "label": "City", "generator_id": "city_roadnet",
                 "guidance": "The streets above."},
                {"level_type": "interior", "label": "Interior", "generator_id": "interior",
                 "nestable": True, "guidance": "Rooms of one building."}],
            "parallel_maps": [{"label": "The Undercity", "level_type": "undercity",
                               "description": "Sewers and buried streets.",
                               "connection_kind": "sewer_grate", "connection_count": 3}]},
            "approved": True}}}
    data = asyncio.run(builder.generate_step(
        "map_generation", state, state["seed_prompt"], config={"total_nodes": 80}))
    assert [l["layer_id"] for l in data["layers"]] == ["root", "the_undercity"]
    state["steps"]["map_generation"] = {"data": data, "approved": True}
    builder.save_world("under", state)
    compiled = builder.compile_world(builder.load_world("under"))
    root = compiled["maps"][compiled["root_map_id"]]
    assert (root["level_type"], root["generator_id"]) == ("city", "city_roadnet")
    under = compiled["maps"]["the_undercity"]
    assert (under["level_type"], under["generator_id"]) == ("undercity", "world_map")
    assert under["parent_map_id"] == compiled["root_map_id"]
    crossings = [c for c in compiled["connections"]
                 if c["from"]["map_id"] != c["to"]["map_id"]]
    assert len(crossings) == 3
    assert {c["kind"] for c in crossings} == {"sewer_grate"}
    # Crossing anchors are marked on both maps and never reused.
    anchors = [n for n in root["nodes"] + under["nodes"]
               if n.get("interlayer_connection_id")]
    assert len(anchors) == 6
    assert len({n["id"] for n in anchors}) == 6
    assert all(n["type"] == "sewer_grate" and n["importance"] >= 4 for n in anchors)


def test_enrichment_engine_sees_child_map_nodes(solar):
    compiled, root_id, root = _root(solar)
    anchor = _node_named(root, "Planet Kestrel")
    planet = asyncio.run(solar.expand_node("solar", root_id, anchor["id"],
                                           level_type="planet"))["map"]
    from wbworldgen.worldgen.enrichment.context import collect_nodes_by_layer
    reloaded = solar.compile_world(solar.load_world("solar"))
    all_nodes, layer_map = collect_nodes_by_layer(reloaded)
    assert planet["map_id"] in layer_map
    assert layer_map[planet["map_id"]]["total"] == len(planet["nodes"])
