import asyncio
import importlib.util
from pathlib import Path


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_core_rpg" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_core_rpg_backend", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _state(config=None, char=None):
    return {
        "module_configs": {"wb_core_rpg": config or {}},
        "module_data": {"wb_core_rpg": char or {}},
    }


def _char(**overrides):
    base = {
        "stats": {s: 10 for s in ["power", "agility", "vitality", "intelligence", "spirit", "charm"]},
        "skills": {},
        "level": 1,
        "xp": 0,
        "hp": 85,
        "max_hp": 85,
    }
    base.update(overrides)
    return base


def _run(backend, mutation, config, char):
    return asyncio.run(backend.on_mutate_state(mutation, _state(config, char), None))


def test_successful_action_awards_scaled_xp():
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 8, "difficulty": "hard"})
    result = _run(backend, {}, {"xp_gain_condition": "successful_action", "xp_per_action": 10}, char)
    # hard weight = 1.75 -> 18 XP
    assert result["module_data"]["wb_core_rpg"]["xp"] == 18


def test_default_condition_is_successful_action():
    # No xp_gain_condition set: a successful moderate action still earns XP,
    # even though the Reader emitted no xp_gained. This is the core fix.
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 7, "difficulty": "moderate"})
    result = _run(backend, {}, {"xp_per_action": 10}, char)
    assert result["module_data"]["wb_core_rpg"]["xp"] == 10


def test_failed_action_grants_no_xp_when_success_required():
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 1, "difficulty": "impossible"})
    result = _run(backend, {}, {"xp_gain_condition": "successful_action"}, char)
    assert result == {}


def test_any_action_rewards_a_failed_attempt():
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 2, "difficulty": "hard"})
    result = _run(backend, {}, {"xp_gain_condition": "any_action", "xp_per_action": 10}, char)
    assert result["module_data"]["wb_core_rpg"]["xp"] == 18


def test_challenging_condition_skips_easy_actions():
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 9, "difficulty": "easy"})
    result = _run(backend, {}, {"xp_gain_condition": "challenging_action"}, char)
    assert result == {}


def test_challenging_condition_rewards_hard_actions():
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 5, "difficulty": "extreme"})
    result = _run(backend, {}, {"xp_gain_condition": "challenging_action", "xp_per_action": 10}, char)
    # extreme weight = 2.5 -> 25 XP
    assert result["module_data"]["wb_core_rpg"]["xp"] == 25


def test_no_assessment_means_no_xp():
    backend = _load_backend()
    char = _char(action_assessment={})
    result = _run(backend, {}, {"xp_gain_condition": "any_action"}, char)
    assert result == {}


def test_disabled_condition_never_awards():
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 9, "difficulty": "hard"})
    result = _run(backend, {}, {"xp_gain_condition": "disabled"}, char)
    assert result == {}


def test_reader_mode_uses_mutation_xp_gained():
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 9, "difficulty": "hard"})
    result = _run(backend, {"xp_gained": 30}, {"xp_gain_condition": "reader"}, char)
    # Reader value used verbatim; assessment ignored.
    assert result["module_data"]["wb_core_rpg"]["xp"] == 30


def test_reader_mode_ignores_assessment():
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 9, "difficulty": "hard"})
    result = _run(backend, {}, {"xp_gain_condition": "reader"}, char)
    assert result == {}


def test_enough_xp_triggers_level_up():
    backend = _load_backend()
    # L1->L2 needs 50 total XP. extreme moderate math: need >= 50.
    char = _char(xp=40, action_assessment={"feasibility": 8, "difficulty": "extreme"})
    result = _run(backend, {}, {"xp_gain_condition": "successful_action", "xp_per_action": 10}, char)
    rpg = result["module_data"]["wb_core_rpg"]
    # 40 + 25 = 65 XP >= 50 -> level 2
    assert rpg["xp"] == 65
    assert rpg["level"] == 2


def test_stale_assessment_cleared_on_empty_input():
    # A turn with no player input must not re-award XP from a prior turn's
    # assessment. on_gather_context resets the assessment.
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 8, "difficulty": "hard"})
    state = _state({"xp_gain_condition": "successful_action"}, char)
    state["input_text"] = ""
    updated = asyncio.run(backend.on_gather_context(state, None))
    assert updated["module_data"]["wb_core_rpg"]["action_assessment"] == {}
