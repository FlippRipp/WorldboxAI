"""World Structure (hierarchy_design) tests: the AI-authored levels contract.

Normalization clamps LLM output to implemented generators; the designed
levels drive the root map generator and ride into the compiled world; the
AI-authored vocabulary fills the template_vocab seam; worlds without a
designed structure (old worlds, junk output) behave exactly as before.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_hierarchy_design.py
"""

import asyncio
import json
import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.enrichment.maps_expand import allowed_child_levels
from wbworldgen.worldgen.migrate import DEFAULT_LEVELS
from wbworldgen.worldgen.steps.hierarchy_design import (
    designed_levels,
    normalize_hierarchy_design,
)

IMPLEMENTED = ["world_map", "city_roadnet", "interior"]


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_hd_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class RecordingLLM:
    """Returns a canned JSON payload and records every call's messages."""

    def __init__(self, payload=None):
        self.mode = "live"
        self.reader_model = "reader-slot"
        self.module_fast_model = "fast-slot"
        self.payload = payload or {}
        self.calls = []

    async def simple_completion(self, messages=None, **kwargs):
        self.calls.append(messages)
        return json.dumps(self.payload)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    register_default_steps(wb)
    return wb


def _all_prompt_text(llm) -> str:
    return "\n".join(m["content"] for call in llm.calls for m in call)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_clamps_unknown_generators_and_appends_interior():
    data = {"levels": [
        {"level_type": "Star System", "label": "", "generator_id": "star_system"},
        {"level_type": "planet", "generator_id": "world_map", "guidance": "one planet"},
    ]}
    out = normalize_hierarchy_design(data, IMPLEMENTED)
    types = [(l["level_type"], l["generator_id"]) for l in out["levels"]]
    # Reserved/unimplemented id lands on the abstract fallback; the level
    # type is slugged; an interior level is guaranteed at the bottom.
    assert types == [("star_system", "world_map"), ("planet", "world_map"),
                     ("interior", "interior")]
    assert out["levels"][0]["label"] == "Star System"
    assert out["levels"][-1]["nestable"] is True


def test_normalize_junk_degrades_to_default_levels():
    for junk in (None, "text", {"levels": "junk"}, {"levels": []},
                 {"levels": [{"label": "no type"}]}):
        out = normalize_hierarchy_design(junk, IMPLEMENTED)
        assert out["levels"] == [dict(l) for l in DEFAULT_LEVELS]


def test_normalize_junk_prefers_fallback_levels():
    declared = [{"level_type": "city", "label": "City", "generator_id": "city_roadnet"},
                {"level_type": "interior", "label": "Interior",
                 "generator_id": "interior", "nestable": True}]
    out = normalize_hierarchy_design({}, IMPLEMENTED, fallback_levels=declared)
    assert [l["generator_id"] for l in out["levels"]] == ["city_roadnet", "interior"]


def test_normalize_dedupes_types_and_caps_depth():
    levels = [{"level_type": f"l{i}", "generator_id": "world_map"} for i in range(9)]
    levels.insert(1, {"level_type": "l0", "generator_id": "world_map"})  # dupe
    out = normalize_hierarchy_design({"levels": levels}, IMPLEMENTED)
    assert len(out["levels"]) == 6  # MAX_LEVELS, interior included
    assert out["levels"][-1]["generator_id"] == "interior"
    assert len({l["level_type"] for l in out["levels"]}) == 6


def test_normalize_city_map_style_aligns_root_generator():
    data = {"levels": [{"level_type": "district", "generator_id": "world_map"},
                       {"level_type": "interior", "generator_id": "interior"}]}
    out = normalize_hierarchy_design(data, IMPLEMENTED, map_style="city")
    assert out["levels"][0]["generator_id"] == "city_roadnet"
    # Other styles leave the choice alone.
    out2 = normalize_hierarchy_design(data, IMPLEMENTED, map_style="terrain")
    assert out2["levels"][0]["generator_id"] == "world_map"


def test_normalize_clamps_side_fields():
    out = normalize_hierarchy_design({
        "notes": " n ",
        "levels": [{"level_type": "world", "generator_id": "world_map"},
                   {"level_type": "interior", "generator_id": "interior"}],
        "parallel_maps": [{"label": "Underworld", "connection_kind": "cave_mouth",
                           "connection_count": 3}, {"no": "label"}, "junk"],
        "pregenerate": [{"location_name": "The Keep", "level_type": "interior"},
                        {"reason": "nameless"}, 7],
        "site_sub_noun": "  decks and domes ",
        "connection_looks": [{"kind": "portal", "look": "a shimmering arch"},
                             {"kind": "portal", "look": "dupe kind dropped"},
                             {"kind": "", "look": "no kind"}, "junk"],
    }, IMPLEMENTED)
    assert out["notes"] == "n"
    assert [p["label"] for p in out["parallel_maps"]] == ["Underworld"]
    assert [p["location_name"] for p in out["pregenerate"]] == ["The Keep"]
    assert out["site_sub_noun"] == "decks and domes"
    assert out["connection_looks"] == [{"kind": "portal", "look": "a shimmering arch"}]


# ---------------------------------------------------------------------------
# designed_levels seam
# ---------------------------------------------------------------------------

def _state_with_levels(levels, extra_data=None):
    data = {"levels": levels}
    data.update(extra_data or {})
    return {"seed_prompt": "s", "steps": {"hierarchy_design": {"data": data}}}


def test_designed_levels_reads_valid_entries_only():
    state = _state_with_levels([
        {"level_type": "world", "generator_id": "world_map"},
        {"level_type": "", "generator_id": "interior"},       # invalid
        {"generator_id": "interior"},                          # invalid
        {"level_type": "interior", "generator_id": "interior"},
    ])
    levels = designed_levels(state)
    assert [l["level_type"] for l in levels] == ["world", "interior"]
    # Player-added interior entries nest even without an explicit flag.
    assert levels[1]["nestable"] is True


def test_designed_levels_empty_for_old_worlds():
    assert designed_levels({"seed_prompt": "s", "steps": {}}) == []
    old_shape = {"notes": "old shape", "parallel_maps": [], "pregenerate": []}
    assert designed_levels({"steps": {"hierarchy_design": {"data": old_shape}}}) == []


# ---------------------------------------------------------------------------
# Root generator resolution + compiled world
# ---------------------------------------------------------------------------

def test_designed_root_generator_drives_map_generation(builder):
    state = _state_with_levels([
        {"level_type": "city", "generator_id": "city_roadnet"},
        {"level_type": "interior", "generator_id": "interior"},
    ])
    assert builder._root_generator_for(state) == "city_roadnet"
    data = asyncio.run(builder.generate_step(
        "map_generation", state, "a city", config={"total_nodes": 40}))
    assert data.get("generator_id") == "city_roadnet"


def test_worlds_without_design_keep_default_and_city_override(builder):
    # Old world, no designed levels: default overworld root.
    old = {"seed_prompt": "s", "steps": {}}
    assert builder._root_generator_for(old) == "world_map"
    # world_form "city" override still applies without a designed structure.
    city = {"seed_prompt": "s", "steps": {
        "world_form": {"data": {"map_style": "city"}}}}
    assert builder._root_generator_for(city) == "city_roadnet"
    # A designed structure wins over the override.
    designed = _state_with_levels([
        {"level_type": "world", "generator_id": "world_map"},
        {"level_type": "interior", "generator_id": "interior"}])
    designed["steps"]["world_form"] = {"data": {"map_style": "city"}}
    assert builder._root_generator_for(designed) == "world_map"


def test_designed_levels_ride_into_compiled_world(builder):
    state = _state_with_levels(
        [{"level_type": "star_system", "label": "Star System", "generator_id": "world_map"},
         {"level_type": "planet", "label": "Planet", "generator_id": "world_map"},
         {"level_type": "interior", "label": "Interior", "generator_id": "interior",
          "nestable": True}],
        extra_data={"site_sub_noun": "decks, domes and installations",
                    "connection_looks": [{"kind": "shuttle", "look": "a shuttle pad"}]})
    state["steps"]["map_generation"] = {"data": asyncio.run(builder.generate_step(
        "map_generation", state, "space opera", config={"total_nodes": 40}))}
    compiled = builder.compile_world(state)
    assert [l["level_type"] for l in compiled["hierarchy"]["levels"]] == \
        ["star_system", "planet", "interior"]
    # The root map record carries the designed root level, not "world".
    root = compiled["maps"][compiled["root_map_id"]]
    assert root["level_type"] == "star_system"
    # AI-authored vocabulary fills the template_vocab seam.
    assert compiled["template_vocab"] == {
        "site_sub_noun": "decks, domes and installations",
        "connection_looks": {"shuttle": "a shuttle pad"}}
    # M1: expansion below the root offers interior levels only.
    assert [l["level_type"] for l in allowed_child_levels(compiled, root)] == ["interior"]


def test_template_vocab_snapshot_wins_over_designed(builder):
    # A template-era world's vocabulary snapshot keeps winning so its
    # play-time prompts never change under it.
    state = _state_with_levels(
        [{"level_type": "world", "generator_id": "world_map"},
         {"level_type": "interior", "generator_id": "interior"}],
        extra_data={"site_sub_noun": "designed noun"})
    state["template_vocab"] = {"site_sub_noun": "template noun"}
    compiled = builder.compile_world(state)
    assert compiled["template_vocab"]["site_sub_noun"] == "template noun"


def test_template_era_world_still_compiles_and_expands(builder):
    # A world created under the deleted template system: old-shape
    # hierarchy_design data (no levels), a template id and vocabulary
    # snapshot in its state. It compiles with the default levels, keeps its
    # snapshot vocabulary, and expansion still offers interiors.
    state = {
        "seed_prompt": "sci-fi colonies", "template_id": "interplanetary_scifi",
        "template_vocab": {"site_sub_noun": "decks, domes and installations",
                           "connection_looks": {"spaceport": "a spaceport"}},
        "steps": {"hierarchy_design": {"data": {
            "notes": "old shape", "parallel_maps": [], "pregenerate": []}}},
    }
    state["steps"]["map_generation"] = {"data": asyncio.run(builder.generate_step(
        "map_generation", state, state["seed_prompt"], config={"total_nodes": 40}))}
    assert builder._root_generator_for(state) == "world_map"
    compiled = builder.compile_world(state)
    assert [l["level_type"] for l in compiled["hierarchy"]["levels"]] == ["world", "interior"]
    assert compiled["template_id"] == "interplanetary_scifi"
    assert compiled["template_vocab"]["site_sub_noun"] == "decks, domes and installations"
    root = compiled["maps"][compiled["root_map_id"]]
    assert [l["level_type"] for l in allowed_child_levels(compiled, root)] == ["interior"]


# ---------------------------------------------------------------------------
# generate(): mock + live prompt contract
# ---------------------------------------------------------------------------

def test_mock_generate_returns_normalized_levels(builder):
    state = {"seed_prompt": "a mythic fantasy overworld", "steps": {}}
    data = asyncio.run(builder.generate_step("hierarchy_design", state, state["seed_prompt"]))
    assert [l["generator_id"] for l in data["levels"]] == ["world_map", "interior"]
    city = asyncio.run(builder.generate_step(
        "hierarchy_design", {"seed_prompt": "one neon city", "steps": {}}, "one neon city"))
    assert city["levels"][0]["generator_id"] == "city_roadnet"


def test_live_generate_injects_catalog_and_normalizes(builder):
    llm = RecordingLLM({
        "notes": "solar system",
        "levels": [{"level_type": "star_system", "label": "System",
                    "generator_id": "star_system", "guidance": "orbits"},
                   {"level_type": "interior", "label": "Interior",
                    "generator_id": "interior"}],
    })
    builder.set_llm_service(llm)
    state = {"seed_prompt": "space opera", "steps": {}}
    data = asyncio.run(builder.generate_step("hierarchy_design", state, "space opera"))
    # Unimplemented reserved id clamped to the abstract fallback.
    assert data["levels"][0]["generator_id"] == "world_map"
    prompts = _all_prompt_text(llm)
    assert "Map generator catalog" in prompts
    for gid in IMPLEMENTED:
        assert gid in prompts
    # Only implemented generators are offered.
    assert "star_system: Star System Map" not in prompts


def test_live_generate_style_note_and_junk_fallback(builder):
    llm = RecordingLLM({"levels": []})
    builder.set_llm_service(llm)
    state = {"seed_prompt": "s", "steps": {
        "world_form": {"data": {"world_kind": "One neon city.", "map_style": "city",
                                "step_directives": []}}}}
    data = asyncio.run(builder.generate_step("hierarchy_design", state, "s"))
    prompts = _all_prompt_text(llm)
    assert 'map_style "city"' in prompts
    # The world design's world_kind is the per-world genre voice.
    assert "This world: One neon city." in prompts
    # Junk levels fall back to the default pair, root aligned to the
    # player-approved city map style.
    assert [l["generator_id"] for l in data["levels"]] == ["city_roadnet", "interior"]


# ---------------------------------------------------------------------------
# Seeded (offline) worlds end to end
# ---------------------------------------------------------------------------

def test_seeded_city_world_designs_street_root(builder):
    builder.seed_world("a neon cyberpunk city of rain", world_id="seed_city", total_nodes=40)
    compiled = builder.compile_world(builder.load_world("seed_city"))
    root = compiled["maps"][compiled["root_map_id"]]
    assert (root["level_type"], root["generator_id"]) == ("city", "city_roadnet")
    assert compiled["template_vocab"]["site_sub_noun"]


def test_seeded_fantasy_world_unchanged(builder):
    builder.seed_world("a mythic fantasy overworld", world_id="seed_world", total_nodes=40)
    compiled = builder.compile_world(builder.load_world("seed_world"))
    root = compiled["maps"][compiled["root_map_id"]]
    assert (root["level_type"], root["generator_id"]) == ("world", "world_map")
    assert [l["level_type"] for l in compiled["hierarchy"]["levels"]] == ["world", "interior"]
