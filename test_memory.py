from backend.engine.memory import MemoryManager
from backend.engine.llm import LLMService
from backend.engine.schemas import MemorySummary, MemoryImportance
import asyncio
import os
import tempfile


def test_memory_add_search_filters_future_turns_and_rolls_back(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)

    manager.add_memory([1.0, 0.0, 0.0], "Turn one memory", turn=1, importance=5)
    manager.add_memory([0.0, 1.0, 0.0], "Turn three memory", turn=3, importance=5)

    early_results = manager.search_memories([1.0, 0.0, 0.0], current_turn=1, limit=5)
    assert [result["text"] for result in early_results] == ["Turn one memory"]

    later_results = manager.search_memories([1.0, 0.0, 0.0], current_turn=3, limit=5)
    assert {result["text"] for result in later_results} == {"Turn one memory", "Turn three memory"}

    manager.rollback_memories(target_turn=1)
    rolled_back_results = manager.search_memories([1.0, 0.0, 0.0], current_turn=3, limit=5)
    assert [result["text"] for result in rolled_back_results] == ["Turn one memory"]


def test_memory_purge_removes_old_low_importance_entries(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)

    manager.add_memory([1.0, 0.0, 0.0], "Old low importance", turn=1, importance=3)
    manager.add_memory([0.0, 1.0, 0.0], "Recent low importance", turn=15, importance=3)
    manager.add_memory([0.0, 0.0, 1.0], "Old high importance", turn=1, importance=8)

    manager.purge_decayed_memories(current_turn=20)

    results = manager.search_memories([1.0, 0.0, 0.0], current_turn=20, limit=5)
    texts = {result["text"] for result in results}
    assert "Old low importance" not in texts
    assert "Recent low importance" in texts
    assert "Old high importance" in texts


def test_memory_permanent_entries_survive_purge(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)

    manager.add_memory(
        [1.0, 0.0, 0.0], "Permanent important memory", turn=1, importance=3,
        permanent=True,
    )
    manager.add_memory(
        [0.0, 1.0, 0.0], "Non-permanent low importance", turn=1, importance=3,
        permanent=False,
    )

    manager.purge_decayed_memories(current_turn=20)

    results = manager.search_memories([1.0, 0.0, 0.0], current_turn=20, limit=5)
    texts = {result["text"] for result in results}
    assert "Permanent important memory" in texts
    assert "Non-permanent low importance" not in texts


def test_memory_structured_fields_are_stored(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)

    manager.add_memory(
        [1.0, 0.0, 0.0], "Raw narrative text spanning several actions.",
        turn=5, importance=7,
        summary="Hero defeated the dragon in the mountain pass.",
        entities=["Hero", "Dragon", "Mountain Pass"],
        topics=["combat", "exploration"],
        turn_range="turns 3-5",
        reason="Major combat victory against a named antagonist.",
        permanent=True,
    )

    results = manager.search_memories([1.0, 0.0, 0.0], current_turn=5, limit=1)
    assert len(results) == 1
    entry = results[0]
    assert entry["text"] == "Raw narrative text spanning several actions."
    assert entry["summary"] == "Hero defeated the dragon in the mountain pass."
    assert entry["importance"] == 7
    assert entry["permanent"] is True
    assert entry["reason"] == "Major combat victory against a named antagonist."
    assert entry["turn_range"] == "turns 3-5"

    import json
    entities = json.loads(entry["entities"])
    topics = json.loads(entry["topics"])
    assert "Hero" in entities
    assert "combat" in topics


def test_memory_default_fields_on_legacy_add(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)

    manager.add_memory([1.0, 0.0, 0.0], "Legacy style memory", turn=2, importance=5)

    results = manager.search_memories([1.0, 0.0, 0.0], current_turn=2, limit=1)
    entry = results[0]
    assert entry["text"] == "Legacy style memory"
    assert entry["summary"] == "Legacy style memory"
    assert entry["permanent"] is False
    assert entry["reason"] == ""
    assert entry["turn_range"] == ""
    assert entry["entities"] == "[]"
    assert entry["topics"] == "[]"


async def _mock_structured_summary():
    os.environ["LLM_MODE"] = "mock"
    try:
        service = LLMService()
        result = await service.summarize_memory_structured(
            "The hero entered the dark cave and found a glowing sword.",
            "turns 1-3",
        )
        assert isinstance(result, MemorySummary)
        assert len(result.summary) > 0
        assert isinstance(result.entities, list)
        assert isinstance(result.topics, list)
        assert result.turn_range == "turns 1-3"
    finally:
        pass


def test_mock_structured_summary():
    previous = os.environ.pop("LLM_MODE", None)
    try:
        asyncio.run(_mock_structured_summary())
    finally:
        if previous:
            os.environ["LLM_MODE"] = previous


async def _mock_structured_importance():
    os.environ["LLM_MODE"] = "mock"
    try:
        service = LLMService()
        result = await service.score_memory_importance_structured("Hero found a legendary sword.")
        assert isinstance(result, MemoryImportance)
        assert result.importance == 5
        assert result.permanent is False
        assert len(result.reason) > 0
    finally:
        pass


def test_mock_structured_importance():
    previous = os.environ.pop("LLM_MODE", None)
    try:
        asyncio.run(_mock_structured_importance())
    finally:
        if previous:
            os.environ["LLM_MODE"] = previous
