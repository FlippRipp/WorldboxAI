import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from wbworldgen.worldgen import PipelineStep, WorldBuilder


def _make_step(id, label="Test Step", description="A test step", after=None, schema=None):
    return PipelineStep(
        id=id,
        label=label,
        description=description,
        after=after,
        schema=schema or {},
    )


# ---------------------------------------------------------------------------
# Temp dir fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return WorldBuilder(worlds_dir=tmpdir)


@pytest.fixture
def builder_with_steps(tmpdir):
    """WorldBuilder pre-loaded with all standard server steps in order."""
    wb = WorldBuilder(worlds_dir=tmpdir)
    wb.register_step(_make_step("world_rules", "World Rules", "Define genre, tone, magic level", after=None))
    wb.register_step(_make_step("lore", "Lore", "History, creation myth, eras", after="world_rules"))
    wb.register_step(_make_step("layer_design", "Layer Design", "Define layers", after="lore"))
    wb.register_step(_make_step("layer_rules", "Layer Rules", "Per-layer rules", after="layer_design"))
    wb.register_step(_make_step("terrain_regions", "Terrain Regions", "Regions and terrain", after="layer_rules"))
    wb.register_step(_make_step("natural_landmarks", "Natural Landmarks", "Landmarks", after="terrain_regions"))
    wb.register_step(_make_step("society_factions", "Society Factions", "Factions", after="natural_landmarks"))
    wb.register_step(_make_step("map_generation", "Map Generation", "Generate map", after="society_factions"))
    return wb


# ---------------------------------------------------------------------------
# Pipeline order tests
# ---------------------------------------------------------------------------

def test_pipeline_order(builder):
    builder.register_step(_make_step("a", after=None))
    builder.register_step(_make_step("b", after="a"))
    builder.register_step(_make_step("c", after="b"))
    assert builder._ordered_ids == ["a", "b", "c"]


def test_pipeline_order_with_gaps(builder):
    builder.register_step(_make_step("first", after=None))
    builder.register_step(_make_step("second", after="first"))
    builder.register_step(_make_step("third", after="second"))
    assert builder._ordered_ids == ["first", "second", "third"]


def test_pipeline_order_parallel(builder):
    builder.register_step(_make_step("a", after=None))
    builder.register_step(_make_step("b1", after="a"))
    builder.register_step(_make_step("b2", after="a"))
    builder.register_step(_make_step("c", after="b1"))
    # a must be first; b1, b2 after a; c after b1
    assert builder._ordered_ids[0] == "a"
    a_idx = builder._ordered_ids.index("a")
    b1_idx = builder._ordered_ids.index("b1")
    b2_idx = builder._ordered_ids.index("b2")
    c_idx = builder._ordered_ids.index("c")
    assert a_idx < b1_idx
    assert a_idx < b2_idx
    assert b1_idx < c_idx


def test_pipeline_circular_dependency(builder):
    builder.register_step(_make_step("x", after=None))
    builder.register_step(_make_step("y", after="x"))
    builder._steps["x"] = _make_step("x", after="y")
    with pytest.raises(ValueError, match="Circular or missing"):
        builder._resolve_order()


def test_pipeline_missing_dependency(builder):
    builder.register_step(_make_step("a", after=None))
    with pytest.raises(ValueError, match="Circular or missing"):
        builder._steps["b"] = _make_step("b", after="nonexistent")
        builder._resolve_order()


def test_pipeline_duplicate_registration(builder):
    builder.register_step(_make_step("a"))
    with pytest.raises(ValueError, match="already registered"):
        builder.register_step(_make_step("a"))


def test_pipeline_step_to_frontend():
    step = PipelineStep(
        id="world_rules",
        label="World Rules",
        description="Genre, tone, magic",
        after=None,
        schema={"genre": "string", "tone": "string"},
    )
    fe = step.to_frontend()
    assert fe["id"] == "world_rules"
    assert fe["label"] == "World Rules"
    assert fe["description"] == "Genre, tone, magic"
    assert fe["after"] is None
    assert "genre" in fe["schema"]


def test_get_pipeline(builder):
    builder.register_step(_make_step("a", label="Alpha", after=None))
    builder.register_step(_make_step("b", label="Beta", after="a"))
    pipeline = builder.get_pipeline()
    assert len(pipeline) == 2
    assert pipeline[0]["id"] == "a"
    assert pipeline[1]["id"] == "b"


# ---------------------------------------------------------------------------
# Mock generator tests
# ---------------------------------------------------------------------------

def test_mock_world_rules_fields(builder):
    data = builder._mock_rules("any prompt")
    assert data["genre"] == "dark fantasy"
    assert data["tone"] == "grim and mysterious"
    assert data["magic_level"] == "rare"
    assert data["tech_era"] == "iron age"
    assert data["lethality"] == 7
    assert isinstance(data["custom_rules"], list)
    assert len(data["custom_rules"]) == 3


def test_mock_lore_has_world_name(builder):
    data = builder._mock_lore("any prompt")
    assert data["world_name"] == "Mycelium"
    assert "premise" in data
    assert "creation_myth" in data
    assert "central_conflict" in data
    assert isinstance(data["historical_eras"], list)
    assert len(data["historical_eras"]) >= 1


def test_mock_layer_design_has_multiple_layers(builder):
    data = builder._mock_layer_design("any prompt")
    assert data["has_multiple_layers"] is True
    assert isinstance(data["layers"], list)
    assert len(data["layers"]) >= 2
    assert "layer_id" in data["layers"][0]
    assert "connections" in data
    assert len(data["connections"]) >= 1


def test_mock_layer_rules_structure(builder):
    data = builder._mock_layer_rules("any prompt")
    assert isinstance(data["layer_rules"], list)
    assert len(data["layer_rules"]) >= 1
    assert "layer_id" in data["layer_rules"][0]
    assert "rules" in data["layer_rules"][0]
    assert isinstance(data["world_rules"], list)


def test_mock_terrain_regions_has_regions(builder):
    data = builder._mock_terrain_regions("any prompt")
    assert "regions" in data
    assert isinstance(data["regions"], list)
    assert len(data["regions"]) >= 2
    reg = data["regions"][0]
    assert "name" in reg
    assert "terrain" in reg
    assert "climate" in reg
    assert "description" in reg


def test_mock_terrain_regions_multi_layer_note(builder):
    data = builder._mock_terrain_regions("any prompt", "multi-layer world")
    assert "regions" in data
    for reg in data["regions"]:
        assert reg.get("layer_id", "") != ""


def test_mock_natural_landmarks_has_landmarks(builder):
    data = builder._mock_natural_landmarks("any prompt")
    assert "landmarks" in data
    assert isinstance(data["landmarks"], list)
    assert len(data["landmarks"]) >= 2
    lm = data["landmarks"][0]
    assert "name" in lm
    assert "region" in lm
    assert "type" in lm
    assert "description" in lm


def test_mock_natural_landmarks_multi_layer(builder):
    data = builder._mock_natural_landmarks("any prompt", "multi-layer world")
    assert "landmarks" in data
    for lm in data["landmarks"]:
        assert lm.get("layer_id", "") != ""


def test_mock_society_factions_has_factions(builder):
    data = builder._mock_society_factions("any prompt")
    assert "factions" in data
    assert isinstance(data["factions"], list)
    assert len(data["factions"]) >= 2
    f = data["factions"][0]
    assert "name" in f
    assert "region" in f
    assert "type" in f
    assert "description" in f


def test_mock_society_factions_multi_layer(builder):
    data = builder._mock_society_factions("any prompt", "multi-layer world")
    assert "factions" in data
    for f in data["factions"]:
        assert f.get("layer_id", "") != ""


# ---------------------------------------------------------------------------
# Generate step (mock mode)
# ---------------------------------------------------------------------------

def test_generate_step_mock(builder_with_steps):
    wb = builder_with_steps
    state = {"steps": {}, "seed_prompt": "test"}
    data = asyncio.run(wb.generate_step("world_rules", state, "fantasy world"))
    assert data["genre"] == "dark fantasy"
    assert data["tone"] == "grim and mysterious"


def test_generate_step_mock_unknown_step(builder):
    with pytest.raises(ValueError, match="Unknown step"):
        asyncio.run(builder.generate_step("nonexistent", {}, "test"))


def test_generate_step_mock_dispatches_generic(builder):
    builder.register_step(_make_step("unknown_mock_step", after=None))
    data = asyncio.run(builder.generate_step("unknown_mock_step", {}, "test prompt"))
    assert data["_mock"] is True
    assert data["step"] == "unknown_mock_step"


# ---------------------------------------------------------------------------
# Compile world
# ---------------------------------------------------------------------------

def test_compile_world_structure(builder_with_steps):
    wb = builder_with_steps
    steps_data = {}
    for sid in wb._ordered_ids:
        mock = asyncio.run(wb.generate_step(sid, {"steps": steps_data, "seed_prompt": "dark fantasy world"}, "dark fantasy world"))
        steps_data[sid] = {"data": mock}
    state = {"steps": steps_data, "seed_prompt": "dark fantasy world"}
    compiled = wb.compile_world(state)
    assert "rules" in compiled
    assert "lore" in compiled
    assert "regions" in compiled
    assert "generated_from" in compiled
    assert compiled["generated_from"] == "dark fantasy world"
    assert compiled["rules"]["genre"] == "dark fantasy"
    assert compiled["lore"]["world_name"] == "Mycelium"
    assert len(compiled["regions"]["regions"]) >= 1


def test_compile_world_includes_layer_data(builder_with_steps):
    wb = builder_with_steps
    steps = {
        "layer_design": {"data": wb._mock_layer_design("test")},
        "layer_rules": {"data": wb._mock_layer_rules("test")},
    }
    state = {"steps": steps, "seed_prompt": "test"}
    compiled = wb.compile_world(state)
    assert "layers" in compiled
    assert isinstance(compiled["layers"], list)
    assert "layer_rules" in compiled
    assert isinstance(compiled["layer_rules"], list)


# ---------------------------------------------------------------------------
# Merge geography
# ---------------------------------------------------------------------------

def test_merge_geography_steps(builder_with_steps):
    wb = builder_with_steps
    steps = {
        "terrain_regions": {"data": wb._mock_terrain_regions("test")},
        "natural_landmarks": {"data": wb._mock_natural_landmarks("test")},
        "society_factions": {"data": wb._mock_society_factions("test")},
    }
    result = wb._merge_geography_steps(steps)
    assert "regions" in result
    assert isinstance(result["regions"], list)
    for reg in result["regions"]:
        assert "name" in reg
        assert "terrain" in reg
        assert "climate" in reg
        assert isinstance(reg.get("landmarks", []), list)
        assert isinstance(reg.get("factions", []), list)


def test_merge_geography_attaches_landmarks_to_region(builder_with_steps):
    wb = builder_with_steps
    landmarks_data = {
        "landmarks": [
            {"name": "Test Peak", "region": "Test Region", "type": "mountain", "description": "A peak"},
        ]
    }
    terrain_data = {
        "regions": [{"name": "Test Region", "terrain": "mountains", "climate": "cold", "description": "A cold region"}]
    }
    steps = {
        "terrain_regions": {"data": terrain_data},
        "natural_landmarks": {"data": landmarks_data},
        "society_factions": {"data": {"factions": []}},
    }
    result = wb._merge_geography_steps(steps)
    assert result["regions"][0]["landmarks"] == ["Test Peak"]


def test_merge_geography_attaches_factions_to_region(builder_with_steps):
    wb = builder_with_steps
    factions_data = {
        "factions": [
            {"name": "Test Guild", "region": "Test Region", "type": "guild",
             "description": "A guild", "settlements": [], "significant_landmarks": ["Guild Hall"]},
        ]
    }
    terrain_data = {
        "regions": [{"name": "Test Region", "terrain": "plains", "climate": "temperate", "description": "Plains region"}]
    }
    steps = {
        "terrain_regions": {"data": terrain_data},
        "natural_landmarks": {"data": {"landmarks": []}},
        "society_factions": {"data": factions_data},
    }
    result = wb._merge_geography_steps(steps)
    assert result["regions"][0]["factions"] == ["Test Guild"]
    assert "Guild Hall" in result["regions"][0]["landmarks"]


# ---------------------------------------------------------------------------
# Build chain context
# ---------------------------------------------------------------------------

def test_build_chain_context_order(builder_with_steps):
    wb = builder_with_steps
    rules_data = wb._mock_rules("test")
    lore_data = wb._mock_lore("test")
    world_state = {
        "steps": {
            "world_rules": {"data": rules_data},
            "lore": {"data": lore_data},
        },
        "seed_prompt": "test",
    }
    ctx = wb._build_chain_context(world_state, "layer_design")
    assert "world_rules" in ctx
    assert "lore" in ctx
    assert ctx["world_rules"]["genre"] == "dark fantasy"
    assert ctx["lore"]["world_name"] == "Mycelium"


def test_build_chain_context_stops_before_target(builder_with_steps):
    wb = builder_with_steps
    world_state = {
        "steps": {
            "world_rules": {"data": wb._mock_rules("test")},
            "lore": {"data": wb._mock_lore("test")},
            "layer_design": {"data": wb._mock_layer_design("test")},
        },
        "seed_prompt": "test",
    }
    ctx = wb._build_chain_context(world_state, "lore")
    assert "world_rules" in ctx
    assert "lore" not in ctx


def test_build_chain_context_first_step(builder_with_steps):
    wb = builder_with_steps
    world_state = {"steps": {}, "seed_prompt": "test"}
    ctx = wb._build_chain_context(world_state, "world_rules")
    assert ctx == {}


# ---------------------------------------------------------------------------
# Save / Load / List / Delete
# ---------------------------------------------------------------------------

def test_save_and_load_world(builder, tmpdir):
    state = {
        "seed_prompt": "test world",
        "steps": {
            "world_rules": {"data": builder._mock_rules("test"), "approved": True},
            "lore": {"data": builder._mock_lore("test"), "approved": True},
        },
    }
    wid = builder.save_world("my_world", state)
    loaded = builder.load_world(wid)
    assert loaded["seed_prompt"] == "test world"
    assert "world_rules" in loaded["steps"]
    assert "lore" in loaded["steps"]
    assert loaded["steps"]["world_rules"]["data"]["genre"] == "dark fantasy"
    assert loaded["steps"]["lore"]["data"]["world_name"] == "Mycelium"
    assert loaded["complete"] is True


def test_save_and_load_world_preserves_approved_flag(builder):
    state = {
        "seed_prompt": "test",
        "steps": {"world_rules": {"data": builder._mock_rules("test"), "approved": False}},
    }
    wid = builder.save_world("test_approval", state)
    loaded = builder.load_world(wid)
    assert loaded["steps"]["world_rules"]["approved"] is False


def test_list_worlds(builder):
    builder.save_world("alpha", {"seed_prompt": "a", "steps": {}})
    builder.save_world("beta", {"seed_prompt": "b", "steps": {}})
    worlds = builder.list_worlds()
    ids = [w["id"] for w in worlds]
    assert "alpha" in ids
    assert "beta" in ids


def test_list_worlds_skips_non_world_dirs(builder, tmpdir):
    builder.save_world("real_world", {"seed_prompt": "test", "steps": {}})
    junk_path = Path(tmpdir) / "not_a_world"
    junk_path.mkdir()
    worlds = builder.list_worlds()
    ids = [w["id"] for w in worlds]
    assert "real_world" in ids
    assert "not_a_world" not in ids


def test_load_world_not_found(builder):
    with pytest.raises(FileNotFoundError):
        builder.load_world("no_such_world")


def test_delete_world(builder):
    builder.save_world("to_delete", {"seed_prompt": "t", "steps": {}})
    assert len(builder.list_worlds()) >= 1
    builder.delete_world("to_delete")
    worlds_after = builder.list_worlds()
    assert not any(w["id"] == "to_delete" for w in worlds_after)


def test_delete_world_not_found(builder):
    with pytest.raises(FileNotFoundError):
        builder.delete_world("non_existent")


def test_save_world_sanitizes_id(builder):
    state = {"seed_prompt": "test", "steps": {}}
    wid = builder.save_world("My Space World!", state)
    assert " " not in wid
    assert "!" not in wid
    assert wid == wid.lower()


def test_save_world_sanitizes_id_all_special(builder):
    state = {"seed_prompt": "test", "steps": {}}
    wid = builder.save_world("!!!", state)
    assert len(wid) > 0
    assert wid.isalnum() or all(c.isalnum() for c in wid if c not in "_-")


def test_save_step_and_load(builder):
    builder.save_world("step_world", {"seed_prompt": "s", "steps": {}})
    step_data = {"data": builder._mock_rules("s"), "approved": True}
    builder.save_step("step_world", "world_rules", step_data)
    loaded = builder.load_world("step_world")
    assert "world_rules" in loaded["steps"]
    assert loaded["steps"]["world_rules"]["data"]["genre"] == "dark fantasy"


def test_save_step_world_not_found(builder):
    with pytest.raises(FileNotFoundError):
        builder.save_step("no_world", "world_rules", {})


def test_save_draft_auto_generates_id(builder):
    state = {
        "seed_prompt": "test draft",
        "steps": {"lore": {"data": builder._mock_lore("test"), "approved": False}},
    }
    wid = builder.save_draft("", state)
    assert len(wid) > 0
    wid2 = builder.save_draft("", {"seed_prompt": "another", "steps": {}})
    assert len(wid2) > 0
    assert wid != wid2


def test_save_draft_uses_lore_name(builder):
    state = {
        "seed_prompt": "test",
        "steps": {"lore": {"data": builder._mock_lore("test"), "approved": False}},
    }
    wid = builder.save_draft("", state)
    assert wid == "mycelium"


def test_save_draft_marked_in_progress(builder):
    wid = builder.save_draft("draft_world", {"seed_prompt": "d", "steps": {}})
    loaded = builder.load_world(wid)
    assert loaded["complete"] is False


def test_save_world_marked_complete(builder):
    wid = builder.save_world("done_world", {"seed_prompt": "d", "steps": {}})
    loaded = builder.load_world(wid)
    assert loaded["complete"] is True


# ---------------------------------------------------------------------------
# seed_world
# ---------------------------------------------------------------------------

def test_seed_world_completes(builder_with_steps):
    wb = builder_with_steps
    result = wb.seed_world("a dark fantasy fungal world", "test_seed")
    assert result["world_id"] == "test_seed"
    assert result["seed_prompt"] == "a dark fantasy fungal world"
    assert result["step_count"] > 0
    assert "compiled_keys" in result
    assert result["total_map_nodes"] >= 0


def test_seed_world_auto_generates_id(builder_with_steps):
    wb = builder_with_steps
    result = wb.seed_world("test prompt")
    assert result["world_id"]
    assert len(result["world_id"]) > 0


def test_seed_world_creates_save(builder_with_steps):
    wb = builder_with_steps
    result = wb.seed_world("fantasy world", "seed_save_test")
    worlds = wb.list_worlds()
    assert any(w["id"] == result["world_id"] for w in worlds)


# ---------------------------------------------------------------------------
# Enrichment cache
# ---------------------------------------------------------------------------

def test_enrichment_cache_flush(builder):
    wid = builder.save_world("cache_test", {
        "seed_prompt": "test",
        "steps": {"map_generation": {"data": {"nodes": [{"id": "n1", "name": "", "description": ""}], "edges": []}, "approved": True}},
    })
    node_data = {"id": "n1"}
    builder._save_node_enrichment(wid, "n1", "name", "Abyss Gate")
    builder._enrichment_cache[wid]["data"]["nodes"][0]["name"] = "Abyss Gate"
    builder._flush_enrichment_cache(wid)
    assert wid not in builder._enrichment_cache
    loaded = builder.load_world(wid)
    map_step = loaded["steps"]["map_generation"]["data"]
    assert map_step["nodes"][0]["name"] == "Abyss Gate"


def test_enrichment_cache_flush_all(builder):
    wid1 = builder.save_world("ec1", {
        "seed_prompt": "t1",
        "steps": {"map_generation": {"data": {"nodes": [{"id": "n1", "name": "", "description": ""}]}, "approved": True}},
    })
    wid2 = builder.save_world("ec2", {
        "seed_prompt": "t2",
        "steps": {"map_generation": {"data": {"nodes": [{"id": "n1", "name": "", "description": ""}]}, "approved": True}},
    })
    builder._save_node_enrichment(wid1, "n1", "name", "Alpha")
    builder._save_node_enrichment(wid2, "n1", "name", "Beta")
    assert wid1 in builder._enrichment_cache
    assert wid2 in builder._enrichment_cache
    builder._flush_enrichment_cache()
    assert wid1 not in builder._enrichment_cache
    assert wid2 not in builder._enrichment_cache


def test_enrichment_cache_save_node_no_map_file(builder):
    builder.save_world("no_map_world", {"seed_prompt": "t", "steps": {}})
    builder._save_node_enrichment("no_map_world", "n1", "name", "Test")
    assert "no_map_world" not in builder._enrichment_cache


def test_enrichment_cache_flush_nonexistent_world(builder):
    builder.save_world("fake_world", {
        "seed_prompt": "t",
        "steps": {"map_generation": {"data": {"nodes": []}, "approved": True}},
    })
    builder._enrichment_cache["fake_world"] = {"data": {"nodes": []}}
    builder._flush_enrichment_cache("fake_world")
    assert "fake_world" not in builder._enrichment_cache


# ---------------------------------------------------------------------------
# Module hooks
# ---------------------------------------------------------------------------

def test_module_hooks_registration():
    wb = WorldBuilder(worlds_dir=tempfile.mkdtemp())

    class MockBackend:
        pass

    backend1 = MockBackend()
    backend1.on_world_rules_generate = lambda p, d, s: {"extra": "data"}
    backend1.on_region_generate = lambda r, s, sd: {"region_extra": True}

    backend2 = MockBackend()
    backend2.on_faction_generate = lambda f, s, sd: {"faction_extra": True}

    mock_registry = type('obj', (object,), {
        'loaded_modules': {
            'mod_a': {'backend': backend1},
            'mod_b': {'backend': backend2},
            'mod_c': {},  # no backend key
        }
    })()

    wb.register_module_hooks(mock_registry)

    assert len(wb._module_hooks["on_world_rules_generate"]) == 1
    assert wb._module_hooks["on_world_rules_generate"][0][0] == "mod_a"
    assert len(wb._module_hooks["on_region_generate"]) == 1
    assert wb._module_hooks["on_region_generate"][0][0] == "mod_a"
    assert len(wb._module_hooks["on_faction_generate"]) == 1
    assert wb._module_hooks["on_faction_generate"][0][0] == "mod_b"
    assert len(wb._module_hooks["on_world_rules_schema"]) == 0
    assert len(wb._module_hooks["on_world_compiled"]) == 0


def test_module_hooks_registration_no_modules(builder):
    mock_registry = type('obj', (object,), {
        'loaded_modules': {}
    })()
    builder.register_module_hooks(mock_registry)
    for hook_list in builder._module_hooks.values():
        assert hook_list == []


def test_module_hooks_registration_no_hooks_on_backend():
    wb = WorldBuilder(worlds_dir=tempfile.mkdtemp())

    class MockBackend:
        pass

    backend = MockBackend()
    mock_registry = type('obj', (object,), {
        'loaded_modules': {
            'plain_mod': {'backend': backend},
        }
    })()
    wb.register_module_hooks(mock_registry)
    for hook_list in wb._module_hooks.values():
        assert hook_list == []


# ---------------------------------------------------------------------------
# get_start_locations
# ---------------------------------------------------------------------------

def test_get_start_locations_no_world(builder):
    with pytest.raises(FileNotFoundError):
        builder.get_start_locations("no_world")


def test_get_start_locations_empty_nodes(builder_with_steps):
    wb = builder_with_steps
    world_state = {
        "seed_prompt": "test",
        "steps": {
            "world_rules": {"data": wb._mock_rules("test"), "approved": True},
            "map_generation": {"data": {"nodes": [{"id": "n1", "type": "waypoint", "name": ""}], "edges": []}, "approved": True},
        },
    }
    wid = wb.save_world("loc_test", world_state)
    locs = wb.get_start_locations(wid)
    assert locs == []


def test_get_start_locations_settlements(builder_with_steps):
    wb = builder_with_steps
    world_state = {
        "seed_prompt": "test",
        "steps": {
            "world_rules": {"data": wb._mock_rules("test"), "approved": True},
            "map_generation": {"data": {
                "nodes": [
                    {"id": "n1", "type": "settlement", "name": "Dusthaven", "description": "A dusty settlement"},
                    {"id": "n2", "type": "landmark", "name": "Old Tower", "description": "An ancient tower"},
                ],
                "edges": [],
            }, "approved": True},
        },
    }
    wid = wb.save_world("loc_test2", world_state)
    locs = wb.get_start_locations(wid)
    assert len(locs) == 2
    names = [l["name"] for l in locs]
    assert "Dusthaven" in names
    assert "Old Tower" in names


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_get_pipeline_empty(builder):
    assert builder.get_pipeline() == []


def test_resolve_order_single_step(builder):
    builder.register_step(_make_step("only_step", after=None))
    assert builder._ordered_ids == ["only_step"]


def test_list_worlds_empty(builder):
    assert builder.list_worlds() == []


# ---------------------------------------------------------------------------
# Modularity: free-standing add / remove of pipeline steps
# ---------------------------------------------------------------------------

from wbworldgen.worldgen import register_default_steps


def test_register_default_steps_produces_known_pipeline(builder):
    """The production step modules self-register into the expected ordered set."""
    register_default_steps(builder)
    ids = [s["id"] for s in builder.get_pipeline()]
    assert ids == [
        "world_rules",
        "lore",
        "layer_design",
        "layer_rules",
        "terrain_regions",
        "natural_landmarks",
        "society_factions",
        "map_generation",
        "node_labeling",
        "node_descriptions",
    ]


def test_add_custom_step_is_freestanding(builder):
    """A brand-new step can be slotted in via `after` without touching others."""
    builder.register_step(_make_step("a", after=None))
    builder.register_step(_make_step("c", after="a"))
    # Insert a new step between a and c by re-pointing c, then adding b.
    builder._steps["c"].after = "b"
    builder.register_step(_make_step("b", after="a"))
    assert builder._ordered_ids == ["a", "b", "c"]


def test_remove_step_and_repoint(builder):
    """Removing a step (and re-pointing its dependents) still resolves cleanly."""
    builder.register_step(_make_step("a", after=None))
    builder.register_step(_make_step("b", after="a"))
    builder.register_step(_make_step("c", after="b"))
    # Remove b: drop it and re-point c onto a.
    del builder._steps["b"]
    builder._steps["c"].after = "a"
    builder._ordered_ids = builder._resolve_order()
    assert builder._ordered_ids == ["a", "c"]


def test_custom_step_generate_override(builder):
    """A step carrying its own `generate` callable bypasses the standard path."""
    async def custom_generate(ctx):
        return {"made_by": "custom", "prompt": ctx.user_prompt, "note": ctx.user_note}

    step = PipelineStep(
        id="phantom",
        label="Phantom",
        description="A free-standing custom step",
        after=None,
        schema={},
    )
    step.generate = custom_generate
    builder.register_step(step)

    data = asyncio.run(
        builder.generate_step("phantom", {"seed_prompt": "x", "steps": {}}, "seedprompt", "guidance")
    )
    assert data["made_by"] == "custom"
    assert data["prompt"] == "seedprompt"
    assert data["note"] == "guidance"
