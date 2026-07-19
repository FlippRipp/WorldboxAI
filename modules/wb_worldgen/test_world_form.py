"""World Design (world_form) step tests: dynamic skips, coverage-directive
injection, output normalization, mock path, and the routes-level prune of
steps a re-rolled design turned off.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_world_form.py
"""

import asyncio
import json
import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen import design
from wbworldgen.worldgen.steps import world_form as wf


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_wf_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class RecordingLLM:
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


def _state_with_form(data):
    return {"seed_prompt": "seed", "steps": {"world_form": {"data": data, "approved": True}}}


# ---------------------------------------------------------------------------
# dynamic_skips / coverage_directive (pure helpers)
# ---------------------------------------------------------------------------

def test_dynamic_skips_allowlist_and_backcompat():
    # No world_form data (old worlds, seeded worlds) -> nothing skipped.
    assert design.dynamic_skips({}) == set()
    assert design.dynamic_skips({"steps": {}}) == set()

    # Abstract map style turns terrain off; allowlisted skips pass through.
    state = _state_with_form({"map_style": "abstract", "skip_steps": ["society_factions"]})
    assert design.dynamic_skips(state) == {"terrain_generation", "society_factions"}

    # Structural steps can never be skipped, whatever the LLM wrote.
    state = _state_with_form({"map_style": "terrain",
                              "skip_steps": ["world_rules", "lore", "map_generation",
                                             "node_labeling", "natural_landmarks"]})
    assert design.dynamic_skips(state) == {"natural_landmarks"}


def test_coverage_directive_lookup():
    state = _state_with_form({"step_directives": [
        {"step_id": "lore", "directive": "Recent city history, no myths."},
        {"step_id": "society_factions", "directive": "  clubs and workplaces  "},
    ]})
    assert design.coverage_directive(state, "lore") == "Recent city history, no myths."
    assert design.coverage_directive(state, "society_factions") == "clubs and workplaces"
    assert design.coverage_directive(state, "natural_landmarks") == ""
    assert design.coverage_directive({}, "lore") == ""


def test_normalize_world_form_clamps_junk():
    known = ["world_rules", "lore", "society_factions"]
    out = wf.normalize_world_form({
        "world_kind": "  a city  ",
        "map_style": "weird",
        "skip_steps": ["world_rules", "society_factions", "society_factions", "nonsense"],
        "step_directives": [
            {"step_id": "lore", "directive": "x"},
            {"step_id": "unknown_step", "directive": "y"},
            {"step_id": "lore", "directive": "   "},
            "not-a-dict",
        ],
    }, known)
    assert out == {
        "world_kind": "a city",
        "map_style": "terrain",
        "skip_steps": ["society_factions"],
        "step_directives": [{"step_id": "lore", "directive": "x"}],
    }
    # Junk in, safe defaults out.
    assert wf.normalize_world_form(None, known)["map_style"] == "terrain"


# ---------------------------------------------------------------------------
# Effective order applies the design's dynamic skips
# ---------------------------------------------------------------------------

def test_ordered_ids_apply_dynamic_skips(builder):
    plain = builder.ordered_ids_for({})
    assert plain[0] == "world_form"
    assert "terrain_generation" in plain

    state = _state_with_form({"map_style": "abstract", "skip_steps": ["natural_landmarks"]})
    # A leftover template_id from a template-era world changes nothing.
    state["template_id"] = "single_city"
    effective = builder.ordered_ids_for(state)
    assert "terrain_generation" not in effective
    assert "natural_landmarks" not in effective
    assert "society_factions" in effective
    assert effective[0] == "world_form"


# ---------------------------------------------------------------------------
# Coverage-directive prompt injection
# ---------------------------------------------------------------------------

def test_coverage_directive_injected_into_step_prompt(builder):
    builder.set_llm_service(RecordingLLM({"world_name": "Tokyo"}))
    state = _state_with_form({"map_style": "abstract", "step_directives": [
        {"step_id": "lore", "directive": "The city's founding and recent history — no creation myths."},
    ]})
    asyncio.run(builder.generate_step("lore", state, "seed"))
    user = builder._llm_service.calls[0][1]["content"]
    assert "For THIS world, this step should cover: The city's founding and recent history" in user
    assert "the directive wins" in user

    # A step without a directive gets no injection block at all.
    builder._llm_service.calls.clear()
    asyncio.run(builder.generate_step("society_factions", state, "seed"))
    user = builder._llm_service.calls[0][1]["content"]
    assert "For THIS world" not in user


# ---------------------------------------------------------------------------
# world_form generation: mock path + live path
# ---------------------------------------------------------------------------

def test_world_form_mock_path_matches_todays_behavior(builder):
    # No LLM service -> mock: terrain style, no skips, normalized shape.
    data = asyncio.run(builder.generate_step("world_form", {"seed_prompt": "s", "steps": {}}, "s"))
    assert data["map_style"] == "terrain"
    assert data["skip_steps"] == []
    assert all(d["step_id"] in builder._steps for d in data["step_directives"])
    state = {"seed_prompt": "s", "steps": {"world_form": {"data": data, "approved": True}}}
    assert design.dynamic_skips(state) == set()


def test_world_form_live_path_catalog_and_normalization(builder):
    builder.set_llm_service(RecordingLLM({
        "world_kind": "Modern Tokyo slice of life.",
        "map_style": "abstract",
        "skip_steps": ["world_rules", "natural_landmarks"],
        "step_directives": [
            {"step_id": "lore", "directive": "Neighborhood history."},
            {"step_id": "bogus", "directive": "z"},
        ],
    }))
    data = asyncio.run(builder.generate_step("world_form", {"seed_prompt": "tokyo", "steps": {}}, "tokyo"))

    # The prompt carries the live step catalog (ids + labels the AI keys on).
    user = builder._llm_service.calls[0][1]["content"]
    for sid in ("world_rules", "lore", "terrain_generation", "society_factions"):
        assert f"- {sid}:" in user
    assert "world_form" not in [ln.split(":")[0].strip("- ") for ln in user.splitlines()
                                if ln.startswith("- ")]

    # Output normalized: structural skip dropped, unknown directive dropped.
    assert data["map_style"] == "abstract"
    assert data["skip_steps"] == ["natural_landmarks"]
    assert data["step_directives"] == [{"step_id": "lore", "directive": "Neighborhood history."}]


def test_compiled_world_carries_world_design(builder):
    state = _state_with_form({"world_kind": "A city.", "map_style": "abstract"})
    state["steps"]["map_generation"] = {"data": {"nodes": [], "edges": []}, "approved": True}
    compiled = builder.compile_world(state)
    assert compiled["world_design"] == {"world_kind": "A city.", "map_style": "abstract"}


# ---------------------------------------------------------------------------
# Routes: prune stale data for steps a re-rolled design turned off
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# "city" map style -> street-network generator
# ---------------------------------------------------------------------------

def test_city_map_style_normalizes_and_skips_terrain():
    known = ["lore", "terrain_generation"]
    data = wf.normalize_world_form({"map_style": "city"}, known)
    assert data["map_style"] == "city"
    state = _state_with_form({"map_style": "city"})
    assert "terrain_generation" in design.dynamic_skips(state)
    assert design.map_generator_override(state) == "city_roadnet"
    assert design.map_generator_override(_state_with_form({"map_style": "terrain"})) == ""
    assert design.map_generator_override({"seed_prompt": "s", "steps": {}}) == ""


def test_city_map_style_routes_map_generation_to_roadnet(builder):
    captured = {}

    def fake_map_generate(world_state, config=None, generator_id="world_map"):
        captured["generator_id"] = generator_id
        return {"nodes": [], "edges": []}

    builder._map_gen.generate = fake_map_generate
    # No designed structure: the design's city style wins.
    state = _state_with_form({"map_style": "city"})
    asyncio.run(builder.generate_step("map_generation", state, "seed"))
    assert captured["generator_id"] == "city_roadnet"
    # Terrain style leaves the default overworld generator in charge.
    state = _state_with_form({"map_style": "terrain"})
    asyncio.run(builder.generate_step("map_generation", state, "seed"))
    assert captured["generator_id"] == "world_map"


def test_mock_world_form_picks_city_for_city_prompts(builder):
    data = asyncio.run(builder.generate_step(
        "world_form", {"seed_prompt": "a neon city of rain", "steps": {}},
        "a neon city of rain"))
    assert data["map_style"] == "city"
