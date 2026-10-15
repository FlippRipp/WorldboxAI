import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import backend.engine.llm as llm_module
from backend.engine.graph import EngineGraph
from backend.engine.llm import LLMService
from backend.engine.registry import ModuleRegistry


def _scenario_revision_engine():
    # Exercise the actual intro orchestration without loading modules or making
    # provider calls; the captured messages are what the provider would receive.
    engine = object.__new__(EngineGraph)
    engine.sdk = SimpleNamespace(ui=SimpleNamespace(
        emit_status=AsyncMock(), emit_reasoning_token=AsyncMock(),
    ))
    engine.llm = SimpleNamespace(
        simple_completion=AsyncMock(return_value='{"scenario_description": "Revised setting."}'),
        generate_story_from_messages=AsyncMock(return_value={
            "content": "Revised opening.", "reasoning": "",
        }),
    )
    return engine


@pytest.mark.parametrize("revision_mode", [None, "strict"])
def test_scenario_revision_preserves_original_strict_prompts(revision_mode):
    engine = _scenario_revision_engine()
    scenario = {
        "scenario_description": "Original setting.",
        "starting_prompt": "Original opening.",
        "pending_modification_request": "Set it in winter.",
    }
    if revision_mode is not None:
        scenario["revision_mode"] = revision_mode
    state = {"scenario_data": scenario}

    asyncio.run(engine.generate_intro(state))

    description_messages = engine.llm.simple_completion.call_args.args[0]
    assert description_messages[0]["content"] == (
        "You revise role-play scenario descriptions. Apply the player's requested "
        "changes while keeping the same base premise, setting, and roughly the same "
        'length. Output only valid JSON: {"scenario_description": "..."}'
    )
    opening_messages = engine.llm.generate_story_from_messages.call_args.args[0]
    assert opening_messages[0]["content"] == "\n\n".join([
        "You are a creative storyteller adapting the opening message of a text-based RPG scenario.",
        "<scenario>", "Revised setting.", "</scenario>",
        "<original_opening>", "Original opening.", "</original_opening>",
        "<instructions>",
        "Rewrite the opening message so it satisfies the player's requested changes while keeping the original's base situation, voice, structure, and approximate length.",
        "Output only the rewritten opening as narrative prose — no preamble, lists, or XML tags.",
        "</instructions>",
    ])


def test_flexible_revision_updates_description_before_adapting_opening():
    engine = _scenario_revision_engine()
    request = "Make the protagonist a rescue pilot on a distant planet."
    scenario = {
        "scenario_description": "A blacksmith owes the king a sword.",
        "starting_prompt": "You hammer a blade in the castle forge.",
        "pending_modification_request": request,
        "revision_mode": "flexible",
    }
    state = {"scenario_data": scenario}
    stream = AsyncMock()

    result = asyncio.run(engine.generate_intro(state, stream))

    description_call = engine.llm.simple_completion.call_args
    description_messages = description_call.args[0]
    assert scenario["scenario_description"] in description_messages[1]["content"]
    assert request in description_messages[1]["content"]
    assert description_call.kwargs["response_format"] == {"type": "json_object"}
    opening_call = engine.llm.generate_story_from_messages.call_args
    opening_messages = opening_call.args[0]
    assert "<scenario>\n\nRevised setting.\n\n</scenario>" in opening_messages[0]["content"]
    assert scenario["scenario_description"] not in opening_messages[0]["content"]
    assert scenario["starting_prompt"] in opening_messages[0]["content"]
    assert request in opening_messages[1]["content"]
    assert opening_call.args[1] is stream
    for messages in (description_messages, opening_messages):
        instructions = messages[0]["content"].lower()
        assert "priority" in instructions
        assert "coheren" in instructions
    assert "keeping the same base premise" not in description_messages[0]["content"]
    assert "keeping the original's base situation" not in opening_messages[0]["content"]
    assert result == {"content": "Revised opening.", "reasoning": ""}
    assert state["scenario_data"]["scenario_description"] == "Revised setting."
    assert state["scenario_data"]["starting_prompt"] == "Revised opening."
    assert state["scenario_data"]["revision_mode"] == "flexible"
    assert state["scenario_data"]["applied_modification_request"] == request
    assert "pending_modification_request" not in state["scenario_data"]
    # The engine revises the save's new copy, without changing the input record.
    assert scenario["starting_prompt"] == "You hammer a blade in the castle forge."
    assert scenario["pending_modification_request"] == request


@pytest.mark.parametrize("revision_mode", ["strict", "flexible"])
def test_revision_mode_without_request_keeps_literal_opening(revision_mode):
    engine = _scenario_revision_engine()
    stream = AsyncMock()
    state = {"scenario_data": {
        "scenario_description": "Original setting.",
        "starting_prompt": "Original opening.",
        "revision_mode": revision_mode,
        "pending_modification_request": "  ",
    }}

    result = asyncio.run(engine.generate_intro(state, stream))

    assert result == {"content": "Original opening.", "reasoning": ""}
    stream.assert_awaited_once_with("Original opening.")
    engine.llm.simple_completion.assert_not_awaited()
    engine.llm.generate_story_from_messages.assert_not_awaited()


def test_flexible_revision_with_empty_description_still_adapts_opening():
    engine = _scenario_revision_engine()
    state = {"scenario_data": {
        "scenario_description": "",
        "starting_prompt": "Original opening.",
        "revision_mode": "flexible",
        "pending_modification_request": "Set it in winter.",
    }}

    result = asyncio.run(engine.generate_intro(state))

    engine.llm.simple_completion.assert_not_awaited()
    engine.llm.generate_story_from_messages.assert_awaited_once()
    messages = engine.llm.generate_story_from_messages.call_args.args[0]
    assert "Set it in winter." in messages[1]["content"]
    assert result["content"] == "Revised opening."
    assert state["scenario_data"]["scenario_description"] == ""


def set_env(name: str, value: str | None):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


async def _mock_engine_turn():
    previous_mode = os.getenv("LLM_MODE")
    os.environ["LLM_MODE"] = "mock"

    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        registry = ModuleRegistry(os.path.join(base_dir, "modules"))
        registry.load_all_modules()

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = EngineGraph(registry)
            engine.set_memory_path(os.path.join(temp_dir, "vector_index"))

            state = {
                "active_save_id": "mock_test",
                "input_text": "I inspect the room.",
                "module_data": {"wb_core_rpg": {"hp": 85, "max_hp": 85}},
                "module_configs": {"wb_core_rpg": {"progression_system": "xp"}},
                "characters": {},
                "current_context": [],
                "history": [],
                "chat_messages": [],
                "turn": 0,
            }

            try:
                result = await engine.app.ainvoke(state)
                assert result["turn"] == 1
                assert result["history"][-1].startswith("Mock outcome:")
                assert result["module_data"]["wb_core_rpg"]["hp"] == 85
                print("Mock engine turn test passed.")
            finally:
                # Windows: the open SQLite handle would make the temp dir
                # cleanup fail with PermissionError.
                engine.close_memory()
    finally:
        set_env("LLM_MODE", previous_mode)


def test_mock_engine_turn():
    asyncio.run(_mock_engine_turn())


async def _turn_output_carries_retrieval_tracking():
    # LangGraph drops node-returned keys that aren't declared in the WorldState
    # schema, which silently killed retrieval tracking once before. Pin that
    # the tracking keys survive ainvoke into the final state.
    previous_mode = os.getenv("LLM_MODE")
    os.environ["LLM_MODE"] = "mock"

    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        registry = ModuleRegistry(os.path.join(base_dir, "modules"))
        registry.load_all_modules()

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = EngineGraph(registry)
            engine.set_memory_path(os.path.join(temp_dir, "vector_index"))

            try:
                await engine.ensure_memory()
                vector = await engine.llm.get_embedding("An earlier event")
                engine.memory.add_memory(vector, "An earlier event", turn=0, importance=5)
                engine.memory.init_world_index(os.path.join(temp_dir, "world_index"))
                await engine.memory.embed_world(
                    {"lore": {"premise": "A quiet harbor town."}}, engine.llm)

                state = {
                    "active_save_id": "mock_test",
                    "input_text": "I inspect the room.",
                    "module_data": {},
                    "module_configs": {},
                    "characters": {},
                    "current_context": [],
                    "history": [],
                    "chat_messages": [],
                    "turn": 1,
                }
                result = await engine.app.ainvoke(state)

                assert result["last_context_query"] == "I inspect the room."
                assert len(result["last_retrieved_memory_ids"]) > 0
                assert len(result["last_retrieved_world_ids"]) > 0
            finally:
                engine.close_memory()
    finally:
        set_env("LLM_MODE", previous_mode)


def test_turn_output_carries_retrieval_tracking():
    asyncio.run(_turn_output_carries_retrieval_tracking())


async def _reader_fallback_on_malformed_json():
    previous_mode = os.getenv("LLM_MODE")
    original_acompletion = llm_module.acompletion

    class Message:
        content = "not json"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    async def fake_acompletion(**kwargs):
        return Response()

    try:
        os.environ["LLM_MODE"] = "live"
        llm_module.acompletion = fake_acompletion

        service = LLMService()
        result = await service.extract_mutations("A malformed response should not crash.", {"wb_test": {"hp_change": "integer"}})
        assert result == {}
        print("Reader malformed JSON fallback test passed.")
    finally:
        llm_module.acompletion = original_acompletion
        set_env("LLM_MODE", previous_mode)


def test_reader_fallback_on_malformed_json():
    asyncio.run(_reader_fallback_on_malformed_json())


async def _reader_context_is_included_in_prompt():
    # A dedicated reader call passes pre-turn context; it must land in the
    # extraction prompt, before the story text it is meant to disambiguate.
    previous_mode = os.getenv("LLM_MODE")
    original_acompletion = llm_module.acompletion
    captured = {}

    class Message:
        content = "{}"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    async def fake_acompletion(**kwargs):
        captured["messages"] = kwargs.get("messages")
        return Response()

    try:
        os.environ["LLM_MODE"] = "live"
        llm_module.acompletion = fake_acompletion

        service = LLMService()
        await service.extract_mutations(
            "The hero walks north.", {"wb_test": {"moved": "boolean"}},
            context="PRE-TURN CONTEXT")
        prompt = captured["messages"][-1]["content"]
        assert "PRE-TURN CONTEXT" in prompt
        assert prompt.index("PRE-TURN CONTEXT") < prompt.index("The hero walks north.")
        print("Reader context in prompt test passed.")
    finally:
        llm_module.acompletion = original_acompletion
        set_env("LLM_MODE", previous_mode)


def test_reader_context_is_included_in_prompt():
    asyncio.run(_reader_context_is_included_in_prompt())


async def _storyteller_stream_failure_uses_non_stream_fallback():
    previous_mode = os.getenv("LLM_MODE")
    previous_retry_delay = os.getenv("LLM_PROVIDER_RETRY_DELAY_SECONDS")
    original_acompletion = llm_module.acompletion

    class FailingStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("503 Service Unavailable")

    class Message:
        content = "Fallback story completed."

    class Choice:
        message = Message()
        finish_reason = "stop"

    class Response:
        choices = [Choice()]

    async def fake_acompletion(**kwargs):
        if kwargs.get("stream"):
            return FailingStream()
        return Response()

    try:
        os.environ["LLM_MODE"] = "live"
        os.environ["LLM_PROVIDER_RETRY_DELAY_SECONDS"] = "0"
        llm_module.acompletion = fake_acompletion

        streamed_tokens = []

        async def stream_token(token: str):
            streamed_tokens.append(token)

        service = LLMService()
        story = await service.generate_story_from_messages(
            [{"role": "user", "content": "Test fallback."}],
            streaming_callback=stream_token,
        )

        assert story["content"] == "Fallback story completed."
        assert streamed_tokens == []
        print("Storyteller stream fallback test passed.")
    finally:
        llm_module.acompletion = original_acompletion
        set_env("LLM_MODE", previous_mode)
        set_env("LLM_PROVIDER_RETRY_DELAY_SECONDS", previous_retry_delay)


async def run_all_tests():
    await _mock_engine_turn()
    await _reader_fallback_on_malformed_json()
    await _storyteller_stream_failure_uses_non_stream_fallback()


def test_storyteller_stream_failure_uses_non_stream_fallback():
    asyncio.run(_storyteller_stream_failure_uses_non_stream_fallback())


if __name__ == "__main__":
    asyncio.run(run_all_tests())
