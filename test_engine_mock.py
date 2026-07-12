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


async def _retrieval_query_spans_recent_messages():
    # RAG retrieval must key off the recent player/AI exchange, not just the
    # latest input — including empty-input (continue) turns, where the recent
    # messages alone carry the query.
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

                chat_messages = [
                    {"role": "user", "content": "I enter the crypt."},
                    {"role": "ai", "content": "Dust swirls around the sarcophagus."},
                ]
                state = {
                    "active_save_id": "mock_test",
                    "input_text": "I open the lid.",
                    "module_data": {},
                    "module_configs": {},
                    "characters": {},
                    "current_context": [],
                    "history": [],
                    "chat_messages": chat_messages,
                    "turn": 1,
                }
                result = await engine.gather_context_node(state)
                assert result["last_context_query"].splitlines() == [
                    "I enter the crypt.",
                    "Dust swirls around the sarcophagus.",
                    "I open the lid.",
                ]
                assert len(result["last_retrieved_memory_ids"]) > 0

                # Continue turn: no input, but the recent messages still
                # drive retrieval.
                state["input_text"] = ""
                result = await engine.gather_context_node(state)
                assert result["last_context_query"].splitlines() == [
                    "I enter the crypt.",
                    "Dust swirls around the sarcophagus.",
                ]
                assert len(result["last_retrieved_memory_ids"]) > 0

                # With no input and no history there is no query at all.
                state["chat_messages"] = []
                result = await engine.gather_context_node(state)
                assert result["last_context_query"] == ""
                assert result["last_retrieved_memory_ids"] == []
            finally:
                engine.close_memory()
    finally:
        set_env("LLM_MODE", previous_mode)


def test_retrieval_query_spans_recent_messages():
    asyncio.run(_retrieval_query_spans_recent_messages())


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
