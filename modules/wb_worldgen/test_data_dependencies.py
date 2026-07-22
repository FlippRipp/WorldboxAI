"""Tests for declared data dependencies (B3 of the worldgen architecture
plan): the byte-exact default pipeline order pin (the guard rail that must
exist before anyone ever derives order from data dependencies — chain-context
order feeds prompts, so a silent reorder changes generations), the
requires/produces declarations on steps and passes, and the standalone
dependency checker the C1 executor will call.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_data_dependencies.py
"""

import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.catalog import capability_catalog, check_data_dependencies


#: The default pipeline, byte-for-byte. resolve_order must keep producing
#: exactly this for the built-in steps; any change alters the chain context
#: every prompt sees and therefore the generations themselves.
PINNED_DEFAULT_ORDER = [
    "world_form",
    "world_rules",
    "lore",
    "codex",
    "hierarchy_design",
    "terrain_generation",
    "natural_landmarks",
    "society_factions",
    "map_generation",
    "node_labeling",
    "node_descriptions",
]


@pytest.fixture
def builder():
    d = tempfile.mkdtemp(prefix="wb_deps_")
    wb = register_default_steps(WorldBuilder(worlds_dir=d))
    yield wb
    shutil.rmtree(d, ignore_errors=True)


def test_default_pipeline_order_is_pinned(builder):
    assert builder._ordered_ids == PINNED_DEFAULT_ORDER


def test_effective_orders_stay_within_the_pin(builder):
    # dynamic_skips only ever removes items; it never reorders.
    abstract_world = {"steps": {"world_form": {"data": {
        "map_style": "abstract",
        "skip_steps": ["natural_landmarks", "society_factions"],
    }}}}
    effective = builder.ordered_ids_for(abstract_world)
    assert effective == [sid for sid in PINNED_DEFAULT_ORDER
                         if sid not in ("terrain_generation", "natural_landmarks",
                                        "society_factions")]


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------

def test_builtin_contracts_are_declared():
    cat = capability_catalog()
    steps = {e["id"]: e for e in cat["steps"]}
    assert steps["world_rules"]["produces"] == ["rules"]
    assert steps["codex"]["produces"] == ["codex"]
    # The codex must not require anything: it can be generated from the
    # seed prompt alone, and skipped worlds never enter the checker.
    assert steps["codex"]["requires"] == []
    assert steps["hierarchy_design"]["produces"] == ["hierarchy"]
    assert steps["terrain_generation"]["requires"] == ["hierarchy"]
    assert steps["map_generation"]["requires"] == ["hierarchy"]
    assert steps["map_generation"]["produces"] == ["maps"]
    assert steps["node_descriptions"]["requires"] == ["maps", "labels"]
    # Landmarks must not require terrain: abstract/city worlds skip terrain
    # and still run landmarks.
    assert steps["natural_landmarks"]["requires"] == []

    passes = {e["id"]: e for e in cat["passes"]}
    assert passes["label"]["requires"] == ["maps"]
    assert passes["label"]["produces"] == ["labels"]
    assert passes["describe"]["requires"] == ["maps", "labels"]
    assert passes["review"]["requires"] == ["maps", "labels"]


# ---------------------------------------------------------------------------
# The checker (the C1 executor's validation, not the sorter's)
# ---------------------------------------------------------------------------

def _step_items(ids):
    return [{"kind": "step", "id": sid} for sid in ids]


def test_default_pipeline_validates_clean():
    assert check_data_dependencies(_step_items(PINNED_DEFAULT_ORDER)) == []


def test_every_legitimate_effective_pipeline_validates_clean():
    # Everything dynamic_skips can produce: any subset of the AI-skippable
    # steps plus terrain (map-style controlled) removed must stay valid —
    # the checker may never flag a pipeline today's wizard runs.
    skippable = ("terrain_generation", "codex", "natural_landmarks",
                 "society_factions")
    for mask in range(16):
        removed = {sid for bit, sid in enumerate(skippable) if mask & (1 << bit)}
        effective = [sid for sid in PINNED_DEFAULT_ORDER if sid not in removed]
        assert check_data_dependencies(_step_items(effective)) == [], removed


def test_mixed_plan_with_pass_items_validates():
    items = _step_items(["world_form", "world_rules", "lore", "hierarchy_design",
                         "map_generation"]) + [
        {"kind": "pass", "id": "label"},
        {"kind": "pass", "id": "review"},
        {"kind": "pass", "id": "describe"},
    ]
    assert check_data_dependencies(items) == []


def test_missing_producer_is_flagged():
    items = _step_items(["world_form", "world_rules"]) + [{"kind": "pass", "id": "label"}]
    problems = check_data_dependencies(items)
    assert len(problems) == 1
    assert "pass:label requires 'maps'" in problems[0]


def test_order_matters_for_availability():
    # describe before label: labels exist in the list but not yet at that point.
    items = _step_items(["hierarchy_design", "map_generation"]) + [
        {"kind": "pass", "id": "describe"},
        {"kind": "pass", "id": "label"},
    ]
    problems = check_data_dependencies(items)
    assert any("pass:describe requires 'labels'" in p for p in problems)


def test_unknown_capabilities_fail_loudly():
    with pytest.raises(ValueError, match="Unknown step capability"):
        check_data_dependencies([{"kind": "step", "id": "no_such_step"}])
    with pytest.raises(ValueError, match="Unknown enrichment pass"):
        check_data_dependencies([{"kind": "pass", "id": "no_such_pass"}])
    with pytest.raises(ValueError, match="Unknown capability kind"):
        check_data_dependencies([{"kind": "generator", "id": "world_map"}])


def test_caller_supplied_steps_mapping_wins(builder):
    # The C1 executor validates against a builder's registered instances
    # (module-contributed steps included), not just the built-in classes.
    items = _step_items(["world_form", "world_rules"])
    assert check_data_dependencies(items, steps=builder._steps) == []
    with pytest.raises(ValueError, match="Unknown step capability"):
        check_data_dependencies([{"kind": "step", "id": "ghost"}], steps=builder._steps)
