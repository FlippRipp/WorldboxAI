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


def test_default_condition_defers_to_post_turn_judge():
    # No xp_gain_condition set: the default is the LLM judge, which rules in a
    # dedicated post-turn call — the mutate phase itself awards nothing.
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 7, "difficulty": "moderate"})
    result = _run(backend, {}, {"xp_per_action": 10}, char)
    assert result == {}


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


# ---------------------------------------------------------------------------
# llm_judge mode: the post-turn XP judge (runs in on_librarian)
# ---------------------------------------------------------------------------

def _judge_sdk(reply, calls):
    from types import SimpleNamespace

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls.append({"prompt": prompt, "model_preference": model_preference})
        return reply

    return SimpleNamespace(llm=SimpleNamespace(generate=generate, _current_module=""))


def _judge_state(char, config=None, instructions=None):
    state = {
        "turn": 3,
        "history": ["You crest the wall.", "The chasm yawns; somehow you land it."],
        "last_input_text": "I leap the chasm",
        "module_data": {"wb_core_rpg": char},
        # Isolate the judge: the external-events call is disabled.
        "module_configs": {"wb_core_rpg": {
            "external_skill_events_enabled": False, "xp_per_action": 10, **(config or {}),
        }},
    }
    if instructions:
        state["module_instructions"] = instructions
    return state


def _run_librarian(backend, state, sdk):
    return asyncio.run(backend.on_librarian(state, sdk))


def test_llm_judge_awards_scaled_xp_when_deserved():
    backend = _load_backend()
    calls = []
    sdk = _judge_sdk('{"xp_deserved": true, "reason": "bold leap"}', calls)
    char = _char(action_assessment={"feasibility": 8, "difficulty": "hard"})
    result = _run_librarian(backend, _judge_state(char), sdk)
    # hard weight = 1.75 -> 18 XP, judged post-turn
    assert result["module_data"]["wb_core_rpg"]["xp"] == 18
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    assert "You are the XP judge" in prompt
    assert "I leap the chasm" in prompt
    assert "somehow you land it" in prompt  # the turn's outcome is in scope


def test_llm_judge_withholds_xp_when_not_deserved():
    backend = _load_backend()
    calls = []
    sdk = _judge_sdk('{"xp_deserved": false, "reason": "routine"}', calls)
    char = _char(action_assessment={"feasibility": 8, "difficulty": "hard"})
    result = _run_librarian(backend, _judge_state(char), sdk)
    assert result is None
    assert len(calls) == 1


def test_llm_judge_a_deserving_failure_still_earns():
    # The judge rules on merit, not on the outcome bands: a bold failure can
    # earn XP (this is the point of judge mode over successful_action).
    backend = _load_backend()
    sdk = _judge_sdk('{"xp_deserved": true, "reason": "ambitious failure taught much"}', [])
    char = _char(action_assessment={"feasibility": 2, "difficulty": "extreme"})
    result = _run_librarian(backend, _judge_state(char), sdk)
    assert result["module_data"]["wb_core_rpg"]["xp"] == 25


def test_llm_judge_skips_without_assessment_or_action():
    backend = _load_backend()
    for char, state_patch in (
        (_char(action_assessment={}), {}),                     # no substantive action
        (_char(action_assessment={"feasibility": 8}), {"last_input_text": ""}),  # no input
    ):
        calls = []
        sdk = _judge_sdk('{"xp_deserved": true}', calls)
        state = _judge_state(char)
        state.update(state_patch)
        assert _run_librarian(backend, state, sdk) is None
        assert calls == []  # no LLM call was spent


def test_llm_judge_garbage_reply_awards_nothing():
    backend = _load_backend()
    sdk = _judge_sdk("not json at all", [])
    char = _char(action_assessment={"feasibility": 8, "difficulty": "hard"})
    assert _run_librarian(backend, _judge_state(char), sdk) is None


def test_llm_judge_not_called_for_other_conditions():
    backend = _load_backend()
    calls = []
    sdk = _judge_sdk('{"xp_deserved": true}', calls)
    char = _char(action_assessment={"feasibility": 8, "difficulty": "hard"})
    state = _judge_state(char, config={"xp_gain_condition": "successful_action"})
    assert _run_librarian(backend, state, sdk) is None
    assert calls == []


def test_llm_judge_award_triggers_level_up():
    backend = _load_backend()
    sdk = _judge_sdk('{"xp_deserved": true, "reason": "earned"}', [])
    # L1->L2 needs 50 total XP; 40 + 25 (extreme) = 65 -> level 2.
    char = _char(xp=40, action_assessment={"feasibility": 8, "difficulty": "extreme"})
    result = _run_librarian(backend, _judge_state(char), sdk)
    rpg = result["module_data"]["wb_core_rpg"]
    assert rpg["xp"] == 65
    assert rpg["level"] == 2


def test_llm_judge_uses_custom_instruction_and_model_pref():
    backend = _load_backend()
    calls = []
    sdk = _judge_sdk('{"xp_deserved": true, "reason": "cooked well"}', calls)
    char = _char(action_assessment={"feasibility": 8, "difficulty": "hard"})
    state = _judge_state(
        char, config={"xp_judge_ai_model": "smartest"},
        instructions={"xp_judgment": "Only cooking-related actions ever deserve XP."},
    )
    result = _run_librarian(backend, state, sdk)
    assert result["module_data"]["wb_core_rpg"]["xp"] == 18
    assert calls[0]["model_preference"] == "smartest"
    assert "Only cooking-related actions ever deserve XP." in calls[0]["prompt"]
    assert "Award XP when the attempt genuinely earned it" not in calls[0]["prompt"]


def test_stale_assessment_cleared_on_empty_input():
    # A turn with no player input must not re-award XP from a prior turn's
    # assessment. on_gather_context resets the assessment.
    backend = _load_backend()
    char = _char(action_assessment={"feasibility": 8, "difficulty": "hard"})
    state = _state({"xp_gain_condition": "successful_action"}, char)
    state["input_text"] = ""
    updated = asyncio.run(backend.on_gather_context(state, None))
    assert updated["module_data"]["wb_core_rpg"]["action_assessment"] == {}
