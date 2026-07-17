"""World template tests: loader/fallback, schema patching, pinned contract
values, step skipping, template-aware prompts (default = byte-identical to the
historical behavior), map defaults and vocabulary flow.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_templates.py
"""

import asyncio
import json
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen import templates as tpl
from wbworldgen.worldgen.enrichment.context import build_enrichment_context
from wbworldgen.worldgen.enrichment.engine import _connection_block


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_tpl_")
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
    wb.set_llm_service(RecordingLLM({"genre": "g", "tone": "t"}))
    return wb


# ---------------------------------------------------------------------------
# Loader + fallback
# ---------------------------------------------------------------------------

def test_shipped_templates_load_and_unknown_falls_back(builder):
    ids = {t["id"] for t in builder.list_templates()}
    assert {"overworld_fantasy", "single_city", "interplanetary_scifi"} <= ids
    assert builder.get_template(None).id == "overworld_fantasy"
    assert builder.get_template("nonsense").id == "overworld_fantasy"
    assert builder.get_template("single_city").id == "single_city"


def test_default_template_is_empty_and_uses_historical_framing():
    templates = tpl.load_templates()
    default = templates[tpl.DEFAULT_TEMPLATE_ID]
    assert default.skip_steps == []
    assert default.overrides == {}
    assert default.pinned_values == {}
    # The historical system-prompt line, byte for byte.
    assert default.resolved_system_framing() == \
        "You are a world building AI for a tabletop roleplaying game."


def test_apply_schema_patch():
    schema = {
        "genre": {"type": "string", "label": "Genre"},
        "magic_level": {"type": "select", "label": "Magic Level", "options": ["none", "rare"]},
        "total_nodes": {"type": "number", "label": "Density", "default": 100},
    }
    patched = tpl.apply_schema_patch(schema, {
        "remove": ["magic_level"],
        "add": {"ftl_travel": {"type": "select", "label": "FTL", "options": ["none"]}},
        "modify": {"total_nodes": {"default": 60, "label": "Location Density"}},
    })
    assert "magic_level" not in patched
    assert patched["ftl_travel"]["label"] == "FTL"
    assert patched["total_nodes"] == {"type": "number", "label": "Location Density", "default": 60}
    # Original untouched.
    assert "magic_level" in schema and schema["total_nodes"]["default"] == 100


# ---------------------------------------------------------------------------
# Golden: no template == default template == historical prompts
# ---------------------------------------------------------------------------

def test_default_template_prompts_are_identical_to_untemplated(builder):
    state_plain = {"seed_prompt": "seed", "steps": {}}
    state_default = {"seed_prompt": "seed", "steps": {}, "template_id": "overworld_fantasy"}

    asyncio.run(builder.generate_step("world_rules", state_plain, "seed"))
    asyncio.run(builder.generate_step("world_rules", state_default, "seed"))

    plain_msgs, default_msgs = builder._llm_service.calls
    assert plain_msgs == default_msgs
    system = plain_msgs[0]["content"]
    assert system.startswith("You are a world building AI for a tabletop roleplaying game.\n")
    # The step's own guidance and schema drive the prompt, unpatched.
    user = plain_msgs[1]["content"]
    assert "Monsters aggressively hunt humans" in user
    assert '"magic_level"' in user


def test_template_swaps_framing_guidance_and_schema(builder):
    state = {"seed_prompt": "seed", "steps": {}, "template_id": "interplanetary_scifi"}
    asyncio.run(builder.generate_step("world_rules", state, "seed"))

    system = builder._llm_service.calls[0][0]["content"]
    assert system.startswith("You are a world building AI for a science-fiction")
    user = builder._llm_service.calls[0][1]["content"]
    assert "spacer" in user                    # sci-fi guidance
    assert '"ftl_travel"' in user              # added schema field
    assert '"magic_level"' not in user         # removed from the form


def test_pinned_values_keep_contract_keys(builder):
    # The LLM output has no magic_level (removed from the form); the pin
    # guarantees the compiled rules contract stays complete.
    builder.set_llm_service(RecordingLLM({"genre": "space opera", "tone": "gritty"}))
    state = {"seed_prompt": "seed", "steps": {}, "template_id": "interplanetary_scifi"}
    data = asyncio.run(builder.generate_step("world_rules", state, "seed"))
    assert data["magic_level"] == "none"
    assert data["genre"] == "space opera"


# ---------------------------------------------------------------------------
# Step skipping + pipeline views
# ---------------------------------------------------------------------------

def test_city_template_skips_terrain_and_keeps_order(builder):
    plain = builder.ordered_ids_for({})
    city = builder.ordered_ids_for({"template_id": "single_city"})
    assert "terrain_generation" in plain
    assert "terrain_generation" not in city
    assert city == [sid for sid in plain if sid != "terrain_generation"]
    # Enrichment steps are never template-skipped.
    assert "node_labeling" in city and "node_descriptions" in city


def test_get_pipeline_applies_template_view(builder):
    plain = {s["id"]: s for s in builder.get_pipeline()}
    scifi = {s["id"]: s for s in builder.get_pipeline("interplanetary_scifi")}
    city = {s["id"]: s for s in builder.get_pipeline("single_city")}

    assert "magic_level" in plain["world_rules"]["schema"]
    assert "magic_level" not in scifi["world_rules"]["schema"]
    assert "ftl_travel" in scifi["world_rules"]["schema"]
    assert "terrain_generation" not in city
    assert city["terrain_regions"]["label"] == "Districts"
    # The registered steps themselves are never mutated.
    assert "magic_level" in builder._steps["world_rules"].schema
    assert builder._steps["terrain_regions"].label == "Terrain & Regions"


def test_template_map_default_flows_into_map_config(builder):
    captured = {}

    def fake_map_generate(world_state, config=None):
        captured["config"] = config
        return {"nodes": [], "edges": []}

    builder._map_gen.generate = fake_map_generate
    state = {"seed_prompt": "seed", "steps": {}, "template_id": "single_city"}
    asyncio.run(builder.generate_step("map_generation", state, "seed"))
    assert captured["config"] == {"total_nodes": 60}

    # Explicit config always wins; no template means no injected default.
    asyncio.run(builder.generate_step("map_generation", state, "seed", config={"total_nodes": 45}))
    assert captured["config"] == {"total_nodes": 45}
    asyncio.run(builder.generate_step("map_generation", {"seed_prompt": "s", "steps": {}}, "seed"))
    assert captured["config"] is None


# ---------------------------------------------------------------------------
# Persistence + compiled carry-through + vocabulary
# ---------------------------------------------------------------------------

def test_template_id_and_vocab_roundtrip(builder):
    template = builder.get_template("interplanetary_scifi")
    state = {
        "seed_prompt": "seed",
        "steps": {"map_generation": {"data": {"nodes": [], "edges": []}, "approved": True}},
        "template_id": template.id,
        "template_vocab": template.vocabulary,
    }
    wid = builder.save_world("tpl_world", state)
    loaded = builder.load_world(wid)
    assert loaded["template_id"] == "interplanetary_scifi"
    assert loaded["template_vocab"]["connection_looks"]["spaceport"]
    compiled = builder.compile_world(loaded)
    assert compiled["template_id"] == "interplanetary_scifi"
    assert compiled["template_vocab"]["site_sub_noun"]


def test_connection_looks_merge_template_vocab():
    vocab = {"connection_looks": {"spaceport": "a spaceport where ships launch",
                                  "portal": "an ancient teleport ring"}}
    conn = {"type": "spaceport", "target_layer_id": "mars"}
    block = _connection_block(conn, vocab)
    assert "a spaceport where ships launch" in block
    # Template overrides a built-in look.
    assert "an ancient teleport ring" in _connection_block({"type": "portal"}, vocab)
    # Without vocab the built-ins still apply.
    assert "magical portal" in _connection_block({"type": "portal"})
    # Unknown types degrade to a readable phrase.
    assert "a jump gate" in _connection_block({"type": "jump_gate"})


def test_enrichment_context_carries_vocab():
    compiled = {
        "rules": {}, "lore": {}, "layers": [],
        "map": {"nodes": [{"id": "n1", "x": 0, "y": 0}], "edges": []},
        "regions": {"regions": []},
        "template_vocab": {"connection_looks": {"jump_gate": "x"}},
    }
    node = {"id": "n1", "x": 0, "y": 0}
    ctx = build_enrichment_context(node, [node], compiled)
    assert ctx["vocab"]["connection_looks"]["jump_gate"] == "x"
    # No vocab key at all for template-less worlds.
    compiled.pop("template_vocab")
    assert "vocab" not in build_enrichment_context(node, [node], compiled)


def test_user_templates_override_shipped(tmpdir, monkeypatch):
    user_dir = tempfile.mkdtemp(prefix="wb_user_tpl_")
    try:
        with open(f"{user_dir}/single_city.json", "w", encoding="utf-8") as f:
            json.dump({"id": "single_city", "label": "My City", "description": "custom"}, f)
        with open(f"{user_dir}/noir.json", "w", encoding="utf-8") as f:
            json.dump({"id": "noir", "label": "Noir", "description": "user-made"}, f)
        from pathlib import Path
        monkeypatch.setattr(tpl, "_USER_DIR", Path(user_dir))
        templates = tpl.load_templates()
        assert templates["single_city"].label == "My City"  # user wins
        assert templates["noir"].label == "Noir"
        assert "interplanetary_scifi" in templates          # shipped still there
    finally:
        shutil.rmtree(user_dir, ignore_errors=True)
