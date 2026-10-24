"""Service-level tests for imported presets compiled through real story paths."""
from copy import deepcopy
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.engine.graph import EngineGraph
from backend.engine.prompt_pipeline import PromptCompiler
from backend.engine.st_importer import SillyTavernImporter


def preset_pipeline():
    return SillyTavernImporter().import_preset({
        "prompts": [
            {"identifier": "render", "name": "Render template", "content": "{{getvar::rules}} Roll={{getvar::die}}", "injection_position": 1},
            {"identifier": "disabled", "content": "{{setvar::rules::WRONG}}", "enabled": False},
            {"identifier": "setup", "content": "{{// note {{user}} stays hidden}} {{setvar::rules::Respect {{user}}.}}{{setvar::die::{{roll::1d1000000}}}}"},
            {"identifier": "normal", "content": "NORMAL ONLY", "injection_trigger": ["normal"]},
            {"identifier": "continue", "content": "CONTINUE ONLY", "injection_trigger": ["continue"]},
            {"identifier": "regenerate", "content": "REGENERATE ONLY", "injection_trigger": ["regenerate"]},
            {"identifier": "impersonate", "content": "WRITE THE PLAYER TURN", "injection_trigger": ["impersonate"]},
        ],
        "prompt_order": [{"character_id": 100001, "order": [
            {"identifier": key, "enabled": key != "disabled"}
            for key in ["setup", "disabled", "render", "normal", "continue", "regenerate", "impersonate"]
        ]}],
    })["blocks"]


def state_for(action="normal", turn=4):
    state = {"active_save_id": "compat-test", "turn": turn, "input_text": "I enter.",
            "prompt_generation_action": action,
            "characters": {"default_player": {"name": "Robin"}},
            "prompt_pipeline": preset_pipeline()}
    return state


@pytest.mark.parametrize("action", ["normal", "continue", "regenerate"])
def test_imported_templates_compile_in_order_and_filter_actions(action):
    state = state_for(action)
    original = deepcopy(state)
    result = PromptCompiler().compile(state)
    text = "\n".join(m["content"] for m in result["messages"])
    assert "Respect Robin. Roll=" in text
    assert "WRONG" not in text and "{{" not in text and "hidden" not in text
    assert "WRITE THE PLAYER TURN" not in text
    for other in ["normal", "continue", "regenerate"]:
        assert (f"{other.upper()} ONLY" in text) == (action == other)
    assert state == original


def test_dice_stay_stable_for_same_turn_without_cross_call_variable_leaks():
    compiler = PromptCompiler()
    state = state_for()
    first = compiler.compile(state)
    assert compiler.compile(state)["messages"] == first["messages"]
    assert compiler.compile(state_for(turn=5))["messages"] != first["messages"]
    state["prompt_pipeline"][0]["enabled"] = False
    text = "\n".join(m["content"] for m in compiler.compile(state)["messages"])
    assert "Respect Robin" not in text


def test_native_storytelling_filter_includes_continue_but_st_normal_does_not():
    state = state_for("continue")
    native = deepcopy(state["prompt_pipeline"][-1])
    native.update(id="native", generation_types=["storytelling"], config={"text": "NATIVE STORY RULE"})
    state["prompt_pipeline"].append(native)
    text = "\n".join(m["content"] for m in PromptCompiler().compile(state)["messages"])
    assert "NATIVE STORY RULE" in text and "NORMAL ONLY" not in text


def test_action_filter_does_not_shift_dice_in_shared_blocks():
    state = state_for()
    normal = next(b for b in state["prompt_pipeline"] if b["id"] == "normal")
    normal["config"]["text"] = "{{roll::1d1000000}}"
    shared = deepcopy(normal)
    shared.update(id="shared_roll", generation_types=None, config={"text": "Shared={{roll::1d1000000}}", "st_macros": True})
    state["prompt_pipeline"].append(shared)
    compiler = PromptCompiler()
    first = next(m["content"] for m in compiler.compile(state)["messages"] if m["content"].startswith("Shared="))
    state["prompt_generation_action"] = "regenerate"
    second = next(m["content"] for m in compiler.compile(state)["messages"] if m["content"].startswith("Shared="))
    assert first == second


def test_player_text_and_chat_history_are_not_evaluated_as_st_commands():
    state = state_for()
    state["characters"]["default_player"]["name"] = "{{setvar::rules::INJECTED}}"
    state["input_text"] = "{{setvar::rules::INPUT}}"
    state["chat_messages"] = [{"role": "ai", "content": "{{setvar::rules::HISTORY}}"}]
    result = PromptCompiler().compile(state)
    text = "\n".join(m["content"] for m in result["messages"])
    assert "Respect {{setvar::rules::INJECTED}}. Roll=" in text
    assert any(m["content"] == "{{setvar::rules::INPUT}}" for m in result["messages"])
    assert any(m["content"] == "{{setvar::rules::HISTORY}}" for m in result["messages"])


def test_runtime_macro_warning_is_visible_even_for_empty_block():
    state = state_for()
    block = state["prompt_pipeline"][0]
    block["config"]["text"] = "{{roll::invalid}}"
    result = PromptCompiler().compile(state)
    entry = next(t for t in result["trace"] if t["id"] == block["id"])
    assert entry["warnings"] and entry["skipped"]
    assert any(w["block_id"] == block["id"] for w in result["warnings"])


async def _check_storyteller_and_preview_use_same_macro_and_trigger_path():
    engine = EngineGraph.__new__(EngineGraph)
    engine.prompt_compiler = PromptCompiler()
    engine.registry = SimpleNamespace(get_modules=lambda: {})
    engine._module_prompt_blocks = AsyncMock(return_value=[])
    engine._wrap_stream_callback = lambda callback: callback
    ui = SimpleNamespace(emit_status=AsyncMock(), emit_token=AsyncMock(), emit_reasoning_token=AsyncMock(), emit_message_complete=AsyncMock())
    engine.sdk = SimpleNamespace(ui=ui)
    engine.llm = SimpleNamespace(generate_story_from_messages=AsyncMock(return_value={"content": "The door opens.", "reasoning": ""}))
    state = state_for("regenerate")
    preview = await engine.compile_prompt_preview(state, state["prompt_pipeline"])
    result = await engine.storyteller_node(state)
    sent = engine.llm.generate_story_from_messages.call_args.args[0]
    assert sent == preview["messages"]
    assert result["last_prompt_trace"] == preview["trace"]
    assert "WRITE THE PLAYER TURN" not in str(sent)


def test_storyteller_and_preview_use_same_macro_and_trigger_path():
    asyncio.run(_check_storyteller_and_preview_use_same_macro_and_trigger_path())


def test_session_actions_reset_between_regenerate_and_next_input(tmp_path):
    from backend.engine.session import GameSessionManager
    session = GameSessionManager(str(tmp_path))
    session.state["prompt_generation_action"] = "regenerate"
    session.set_input("I step forward.")
    assert session.state["prompt_generation_action"] == "normal"
    session.set_input("")
    assert session.state["prompt_generation_action"] == "continue"


def test_placeholder_comment_does_not_discard_real_instructions_after_it():
    result = SillyTavernImporter().import_preset({"prompts": [
        {"identifier": "useful", "content": "{{//Keep toggled off}}Keep the actual instructions.", "enabled": False},
        {"identifier": "comment_only", "content": "{{//Keep toggled off}}{{trim}}"},
    ]})
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["config"]["text"] == "Keep the actual instructions."
    assert result["blocks"][0]["enabled"] is False
    assert result["stats"]["skipped"] == 1
