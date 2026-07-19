"""Related locations stay together: authored part_of/relation anchoring,
the restored region join, contained_locations through expansion, and the
label coherence review pass.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_location_anchoring.py
"""

import asyncio
import json
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen.generation.binding import bind_named_locations
from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.compiler import collect_scope_content, merge_geography_steps
from wbworldgen.worldgen.enrichment.passes import describe as describe_pass
from wbworldgen.worldgen.enrichment.passes import label as label_pass
from wbworldgen.worldgen.enrichment.passes import review as review_pass


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_anchor_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    register_default_steps(wb)
    return wb


def _chain(n):
    """n nodes in a line, importance descending from n0."""
    nodes = [{"id": f"n{i}", "importance": n - i, "name": "", "type": "waypoint",
              "description": ""} for i in range(n)]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(n - 1)]
    return nodes, edges


# ---------------------------------------------------------------------------
# bind_named_locations: anchoring + dedup
# ---------------------------------------------------------------------------

def test_standalone_binding_and_dedup_guard():
    nodes, edges = _chain(4)
    nodes[3]["name"] = "Old Quarter"
    locs = [{"name": "Seika High", "category": "settlement", "description": "d"},
            {"name": "Old Quarter", "category": "landmark"},  # already on map
            {"name": "Night Market", "category": "landmark"}]
    bound = bind_named_locations(nodes, locs, edges)
    assert bound == 2
    assert nodes[0]["name"] == "Seika High" and nodes[0]["type"] == "settlement"
    assert nodes[0]["importance"] >= 8
    assert sum(1 for n in nodes if n.get("name") == "Old Quarter") == 1


def test_adjacent_binds_next_to_anchor_not_by_importance():
    nodes, edges = _chain(6)
    nodes[5]["name"] = "Seika High School"
    locs = [{"name": "Student Council Office", "category": "landmark",
             "part_of": "Seika High School", "relation": "adjacent"}]
    bind_named_locations(nodes, locs, edges)
    # n4 adjoins the school; n0 (highest importance) must NOT get the office.
    assert nodes[4]["name"] == "Student Council Office"
    assert nodes[0]["name"] == ""


def test_anchor_chain_resolves_in_one_call():
    nodes, edges = _chain(6)
    locs = [{"name": "Annex", "category": "landmark",
             "part_of": "Seika High School", "relation": "adjacent"},
            {"name": "Seika High School", "category": "settlement"}]
    bind_named_locations(nodes, locs, edges)
    school = next(n for n in nodes if n["name"] == "Seika High School")
    annex = next(n for n in nodes if n["name"] == "Annex")
    pair = {school["id"], annex["id"]}
    assert any({e["from"], e["to"]} == pair for e in edges)


def test_inside_attaches_contained_location_without_a_node():
    nodes, edges = _chain(4)
    nodes[0]["name"] = "Seika High School"
    locs = [{"name": "Student Council Office", "category": "landmark",
             "part_of": "Seika High School", "relation": "inside",
             "description": "Where the council meets."}]
    bound = bind_named_locations(nodes, locs, edges)
    assert bound == 1
    assert nodes[0]["contained_locations"] == [
        {"name": "Student Council Office", "description": "Where the council meets."}]
    assert not any(n.get("name") == "Student Council Office" for n in nodes)
    # Re-binding the same location never duplicates the containment.
    bind_named_locations(nodes, locs, edges)
    assert len(nodes[0]["contained_locations"]) == 1


def test_missing_anchor_falls_back_to_standalone():
    nodes, edges = _chain(3)
    locs = [{"name": "Lost Office", "category": "landmark",
             "part_of": "Nowhere Hall", "relation": "inside"}]
    assert bind_named_locations(nodes, locs, edges) == 1
    assert nodes[0]["name"] == "Lost Office"


# ---------------------------------------------------------------------------
# Compiler: restored region join + part_of carry-through
# ---------------------------------------------------------------------------

def _steps_data():
    return {
        "terrain_regions": {"data": {"regions": [
            {"layer_id": "", "name": "Northside", "terrain": "urban", "climate": "",
             "description": ""}]}},
        "natural_landmarks": {"data": {"landmarks": [
            {"scope": "", "region": "  northside ", "name": "Seika High",
             "type": "school", "part_of": "", "description": "d"},
            {"scope": "", "region": "Northside", "name": "Council Office",
             "type": "office", "part_of": "Seika High", "relation": "inside",
             "description": "o"}]}},
        "society_factions": {"data": {"factions": [
            {"scope": "", "region": "Northside", "name": "Student Council",
             "type": "club", "description": "", "settlements": ["Seika High"],
             "significant_landmarks": ["Council Archive"]}]}},
    }


def test_merge_regions_joins_v2_entries_and_carries_anchors():
    merged = merge_geography_steps(_steps_data())
    region = merged["regions"][0]
    by_name = {l["name"]: l for l in region["named_locations"]}
    # Case/whitespace-tolerant join; no layer_id on v2 entries.
    assert "Seika High" in by_name and "Council Office" in by_name
    assert by_name["Council Office"]["part_of"] == "Seika High"
    assert by_name["Council Office"]["relation"] == "inside"
    # Faction landmarks anchor beside the faction's first settlement.
    assert by_name["Council Archive"]["part_of"] == "Seika High"
    assert by_name["Council Archive"]["relation"] == "adjacent"


def test_scope_content_carries_anchors():
    scopes = collect_scope_content(_steps_data())
    by_name = {l["name"]: l for l in scopes[""]["named_locations"]}
    assert by_name["Council Office"]["part_of"] == "Seika High"
    assert by_name["Council Office"]["relation"] == "inside"
    assert by_name["Council Archive"]["part_of"] == "Seika High"
    assert by_name["Council Archive"]["relation"] == "adjacent"
    assert by_name["Council Archive"]["region"] == "Northside"


# ---------------------------------------------------------------------------
# Tolerant region references (the "Lustra System" failure)
# ---------------------------------------------------------------------------

def _lustra_steps():
    """Mirrors the real failure mode: faction region references name authored
    landmarks ("Fleshport") or drop the article ("Neon Docks") instead of
    copying an area name exactly."""
    return {
        "natural_landmarks": {"data": {
            "areas": [
                {"name": "The Glimmering Core", "terrain": "orbital habitats",
                 "description": ""},
                {"name": "The Tattered Belt", "terrain": "drifting asteroids",
                 "description": ""},
                {"name": "The Neon Docks", "terrain": "freeport station",
                 "description": ""},
            ],
            "landmarks": [
                {"scope": "", "region": "The Tattered Belt", "name": "Fleshport",
                 "type": "asteroid den", "description": "lawless"},
                {"scope": "", "region": "The Glimmering Core",
                 "name": "The Halo Ring", "type": "orbital", "description": "ring"},
            ]}},
        "society_factions": {"data": {"factions": [
            {"scope": "", "region": "Fleshport", "name": "The Velvet Chain",
             "type": "cartel", "description": "",
             "settlements": ["The Flesh Markets", "Chain's Den"],
             "significant_landmarks": ["Slave Docks"]},
            {"scope": "", "region": "Neon Docks", "name": "CyberSleaze",
             "type": "megacorp", "description": "",
             "settlements": ["The CyberSleaze Spire"],
             "significant_landmarks": []},
            {"scope": "", "region": "Nowhere Nebula", "name": "Lost Cult",
             "type": "cult", "description": "",
             "settlements": ["Hidden Shrine"], "significant_landmarks": []},
        ]}},
    }


def test_region_reference_naming_a_landmark_resolves_and_anchors():
    scopes = collect_scope_content(_lustra_steps())
    by_name = {l["name"]: l for l in scopes[""]["named_locations"]}
    # "Fleshport" is a landmark in The Tattered Belt: the cartel's places
    # land in that area, anchored beside the landmark itself.
    assert by_name["The Flesh Markets"]["region"] == "The Tattered Belt"
    assert by_name["The Flesh Markets"]["part_of"] == "Fleshport"
    assert by_name["The Flesh Markets"]["relation"] == "adjacent"
    # The group's landmarks still chain to its seat settlement.
    assert by_name["Slave Docks"]["part_of"] == "The Flesh Markets"
    assert by_name["Slave Docks"]["region"] == "The Tattered Belt"


def test_region_reference_without_article_resolves_to_area():
    scopes = collect_scope_content(_lustra_steps())
    by_name = {l["name"]: l for l in scopes[""]["named_locations"]}
    spire = by_name["The CyberSleaze Spire"]
    assert spire["region"] == "The Neon Docks"
    assert "part_of" not in spire


def test_unresolvable_region_reference_blanks_out():
    scopes = collect_scope_content(_lustra_steps())
    by_name = {l["name"]: l for l in scopes[""]["named_locations"]}
    # "Nowhere Nebula" matches no area and no landmark: better unplaced than
    # scattered by a reference that matches nothing.
    assert "region" not in by_name["Hidden Shrine"]
    assert "part_of" not in by_name["Hidden Shrine"]


def test_merge_joins_factions_through_landmark_and_articleless_references():
    merged = merge_geography_steps(_lustra_steps())
    by_region = {r["name"]: r for r in merged["regions"]}
    belt_names = [l["name"] for l in by_region["The Tattered Belt"]["named_locations"]]
    assert "Fleshport" in belt_names and "The Flesh Markets" in belt_names
    assert "The Velvet Chain" in by_region["The Tattered Belt"]["factions"]
    assert "CyberSleaze" in by_region["The Neon Docks"]["factions"]
    # The unresolvable faction joins no region at all.
    for region in merged["regions"]:
        assert "Hidden Shrine" not in [l["name"] for l in region["named_locations"]]


def test_bind_anchor_lookup_is_article_tolerant():
    nodes, edges = _chain(6)
    nodes[5]["name"] = "The Halo Ring"
    locs = [{"name": "Council Dome", "category": "settlement",
             "part_of": "Halo Ring", "relation": "adjacent"}]
    bind_named_locations(nodes, locs, edges)
    # n4 adjoins the ring; n0 (highest importance) must NOT get the dome.
    assert nodes[4]["name"] == "Council Dome"
    assert nodes[0]["name"] == ""


def test_bind_dedup_is_article_tolerant():
    nodes, edges = _chain(4)
    nodes[3]["name"] = "The CyberSleaze Spire"
    locs = [{"name": "CyberSleaze Spire", "category": "settlement"}]
    assert bind_named_locations(nodes, locs, edges) == 0
    assert nodes[0]["name"] == ""


def test_bind_region_preference_is_article_tolerant():
    nodes, edges = _chain(4)
    for i, n in enumerate(nodes):
        n["region"] = "The Neon Docks" if i >= 2 else "The Glimmering Core"
    locs = [{"name": "Freeport Market", "category": "settlement",
             "region": "Neon Docks"}]
    bind_named_locations(nodes, locs, edges)
    market = next(n for n in nodes if n["name"] == "Freeport Market")
    assert market["region"] == "The Neon Docks"


# ---------------------------------------------------------------------------
# Expansion honors contained_locations (mock mode)
# ---------------------------------------------------------------------------

LEVELS = [
    {"level_type": "world", "label": "World", "generator_id": "world_map",
     "guidance": ""},
    {"level_type": "city", "label": "City", "generator_id": "city_roadnet",
     "guidance": ""},
    {"level_type": "interior", "label": "Interior", "generator_id": "interior",
     "nestable": True, "guidance": ""},
]


@pytest.fixture
def world(builder):
    state = {"seed_prompt": "a slice of life school world", "steps": {
        "hierarchy_design": {"data": {"levels": [dict(l) for l in LEVELS]},
                             "approved": True}}}
    map_data = asyncio.run(builder.generate_step(
        "map_generation", state, state["seed_prompt"], config={"total_nodes": 30}))
    map_data["nodes"][0]["name"] = "Seika High School"
    map_data["nodes"][0]["contained_locations"] = [
        {"name": "Student Council Office", "description": "Where the council meets."}]
    state["steps"]["map_generation"] = {"data": map_data, "approved": True}
    builder.save_world("school", state)
    return builder


def _root(builder, world_id="school"):
    compiled = builder.compile_world(builder.load_world(world_id))
    return compiled, compiled["root_map_id"], compiled["maps"][compiled["root_map_id"]]


def test_authored_expansion_includes_contained_locations(world):
    compiled, root_id, root = _root(world)
    node = next(n for n in root["nodes"] if n.get("name") == "Seika High School")
    result = asyncio.run(world._maps_expand.expand(
        compiled, root_id, node, level_type="interior"))
    names = [n["name"] for n in result["map"]["nodes"]]
    assert "Student Council Office" in names
    office = next(n for n in result["map"]["nodes"]
                  if n["name"] == "Student Council Office")
    assert office["description"] == "Where the council meets."


def test_procedural_expansion_binds_contained_locations(world):
    compiled, root_id, root = _root(world)
    node = next(n for n in root["nodes"] if n.get("name") == "Seika High School")
    result = asyncio.run(world._maps_expand.expand(
        compiled, root_id, node, level_type="city", total_nodes=30))
    names = [n.get("name") for n in result["map"]["nodes"] if n.get("name")]
    assert "Student Council Office" in names


# ---------------------------------------------------------------------------
# Label coherence review
# ---------------------------------------------------------------------------

def _map_world(builder, world_id="rev_world"):
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": 6 - i,
         "x": float(i), "y": 0.0, "name": f"Place {i}",
         "description": "", "region": ""}
        for i in range(6)
    ]
    nodes[0]["name"] = "Seika High School"
    nodes[5]["name"] = "Student Council Office"
    nodes[5]["description"] = "The council office, far from anything."
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(5)]
    return builder.save_world(world_id, {
        "seed_prompt": "test",
        "steps": {"map_generation": {"data": {"nodes": nodes, "edges": edges}, "approved": True}},
    })


def test_review_relabels_flagged_node(builder, monkeypatch):
    wid = _map_world(builder)
    review_calls = []

    async def fake_completion(messages=None, **kwargs):
        review_calls.append(messages)
        return json.dumps({"issues": [
            {"id": "n5", "problem": "Implies a school that is across the map."},
            {"id": "bogus", "problem": "no such node"}]})

    builder._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot",
        simple_completion=fake_completion)

    label_calls = []

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        label_calls.append((node["id"], problem_note))
        return "Riverside Atelier", "a quiet workshop"

    async def fake_desc(services, node, context, existing_description="",
                        existing_details=""):
        return f"Rewritten around {node['name']}.", f"Details for {node['name']}."

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    monkeypatch.setattr(describe_pass, "generate_description", fake_desc)

    summary = asyncio.run(builder.enrich_run(wid, phase="review"))["review"]

    assert summary["reviewed_maps"] == 1
    assert summary["flagged"] == 1
    assert summary["relabeled"][0]["old"] == "Student Council Office"
    assert summary["relabeled"][0]["new"] == "Riverside Atelier"
    assert label_calls == [("n5", "Implies a school that is across the map.")]
    # The reviewer saw names WITH their actual neighbors.
    prompt = review_calls[0][1]["content"]
    assert "Seika High School" in prompt and "near:" in prompt
    # Persisted: new name and a description reworked to match it.
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    fixed = next(n for n in nodes if n["id"] == "n5")
    assert fixed["name"] == "Riverside Atelier"
    assert fixed["description"] == "Rewritten around Riverside Atelier."


def test_enrich_run_reviews_maps_it_completes(builder, monkeypatch):
    wid = _map_world(builder, world_id="rev_world2")
    # Strip names so the run has labeling work to finish the map with.
    world = builder.load_world(wid)
    for n in world["steps"]["map_generation"]["data"]["nodes"]:
        n["name"] = ""
        n["description"] = ""
    builder.save_world(wid, world)

    builder._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot")
    builder._enrichment_batch_size = 1

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        return f"Name {node['id']}", ""

    reviewed = []

    async def fake_review_map(services, rec, state):
        reviewed.append(rec.get("map_id"))
        return {"reviewed_maps": 1, "flagged": 0, "relabeled": []}

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    monkeypatch.setattr(review_pass, "review_map", fake_review_map)

    summary = asyncio.run(builder.enrich_run(wid, phase="label"))
    assert summary["labeled"] == 6
    assert len(reviewed) == 1  # exactly the one map this run completed
    assert summary["review"] == {"reviewed_maps": 1, "flagged": 0, "relabeled": []}


def test_standalone_binding_prefers_own_region():
    nodes, edges = _chain(4)
    for i, n in enumerate(nodes):
        n["region"] = "Northside" if i >= 2 else "Harborfront"
    locs = [{"name": "Seika High", "category": "settlement", "region": "Northside"}]
    bind_named_locations(nodes, locs, edges)
    school = next(n for n in nodes if n["name"] == "Seika High")
    assert school["region"] == "Northside"


def test_areas_become_regions_when_no_legacy_step_data():
    steps = _steps_data()
    del steps["terrain_regions"]
    steps["natural_landmarks"]["data"]["areas"] = [
        {"name": "Northside", "terrain": "hilly campus streets", "description": "d"}]
    merged = merge_geography_steps(steps)
    assert [r["name"] for r in merged["regions"]] == ["Northside"]
    names = [l["name"] for l in merged["regions"][0]["named_locations"]]
    assert "Seika High" in names and "Council Office" in names


def test_parallel_scoped_entries_never_join_main_areas():
    steps = _steps_data()
    steps["natural_landmarks"]["data"]["landmarks"].append(
        {"scope": "Undercity", "region": "Northside", "name": "Sump Shrine",
         "type": "shrine", "description": ""})
    merged = merge_geography_steps(steps)
    names = [l["name"] for l in merged["regions"][0]["named_locations"]]
    assert "Sump Shrine" not in names
