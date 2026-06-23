import asyncio
import os
import tempfile

import backend.engine.llm as llm_module
from backend.engine.graph import EngineGraph
from backend.engine.llm import LLMService
from backend.engine.registry import ModuleRegistry


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

            result = await engine.app.ainvoke(state)
            assert result["turn"] == 1
            assert result["history"][-1].startswith("Mock outcome:")
            assert result["module_data"]["wb_core_rpg"]["hp"] == 85
            print("Mock engine turn test passed.")
    finally:
        set_env("LLM_MODE", previous_mode)


def test_mock_engine_turn():
    asyncio.run(_mock_engine_turn())


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

        assert story == "Fallback story completed."
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
