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


class _FakeEmbedder:
    """3-dim embeddings keyed by recognizable words, mirroring the fixed-vector
    style used above (no get_embeddings attribute → exercises the per-text path)."""

    async def get_embedding(self, text: str, inspector_ctx=None):
        lowered = text.lower()
        if "dragon" in lowered:
            return [1.0, 0.0, 0.0]
        if "harbor" in lowered:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


class _FakeBatchEmbedder(_FakeEmbedder):
    """Adds the batched API so _embed_texts takes the get_embeddings path;
    records call shapes to assert batching actually happened."""

    def __init__(self):
        self.batch_sizes = []
        self.single_calls = 0

    async def get_embedding(self, text: str, inspector_ctx=None):
        self.single_calls += 1
        return await super().get_embedding(text)

    async def get_embeddings(self, texts, inspector_ctx=None):
        self.batch_sizes.append(len(texts))
        return [await _FakeEmbedder.get_embedding(self, t) for t in texts]


def _lorebook_record(book_id="realm_lore"):
    return {
        "id": book_id,
        "entries": [
            {"uid": "0", "title": "Dragon Peak", "keys": ["dragon"], "secondary_keys": [],
             "content": "A dragon sleeps beneath the peak.", "constant": False, "enabled": True},
            {"uid": "1", "title": "World Truth", "keys": [], "secondary_keys": [],
             "content": "The gods are silent.", "constant": True, "enabled": True},
            {"uid": "2", "title": "Disabled", "keys": ["secret"], "secondary_keys": [],
             "content": "Never embedded.", "constant": False, "enabled": False},
        ],
    }


def _run(coro):
    return asyncio.run(coro)


def test_embed_lorebooks_inserts_enabled_entries_idempotently(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)
    manager.init_world_index(str(tmp_path / "world_index"))

    count = _run(manager.embed_lorebooks([_lorebook_record()], _FakeEmbedder()))
    assert count == 2  # disabled entry skipped

    rows = manager._world_conn.execute(
        "SELECT source_id, constant, text FROM world_entries WHERE source_type = 'lorebook' ORDER BY source_id"
    ).fetchall()
    assert [r["source_id"] for r in rows] == ["realm_lore:0", "realm_lore:1"]
    assert rows[0]["constant"] == 0
    assert rows[1]["constant"] == 1
    assert "Lore — Dragon Peak (keywords: dragon)" in rows[0]["text"]

    # Re-running replaces rather than duplicates.
    _run(manager.embed_lorebooks([_lorebook_record()], _FakeEmbedder()))
    total = manager._world_conn.execute(
        "SELECT COUNT(*) AS n FROM world_entries WHERE source_type = 'lorebook'"
    ).fetchone()["n"]
    assert total == 2


def test_embed_world_preserves_lorebook_rows(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)
    manager.init_world_index(str(tmp_path / "world_index"))
    _run(manager.embed_lorebooks([_lorebook_record()], _FakeEmbedder()))

    _run(manager.embed_world({"lore": {"premise": "A quiet harbor town."}}, _FakeEmbedder()))

    types = {row["source_type"] for row in manager._world_conn.execute(
        "SELECT source_type FROM world_entries"
    )}
    assert types == {"lore", "lorebook"}

    # A second world embed still doesn't touch lorebook rows.
    _run(manager.embed_world({"lore": {"premise": "A quiet harbor town."}}, _FakeEmbedder()))
    lorebook_count = manager._world_conn.execute(
        "SELECT COUNT(*) AS n FROM world_entries WHERE source_type = 'lorebook'"
    ).fetchone()["n"]
    assert lorebook_count == 2


def test_embed_lorebooks_couples_each_text_with_its_vector(tmp_path):
    # Concurrent embedding must not scramble text/vector pairing.
    from backend.engine.memory import _serialize

    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)
    manager.init_world_index(str(tmp_path / "world_index"))
    book = {
        "id": "coupling",
        "entries": [
            {"uid": "0", "title": "", "keys": [], "secondary_keys": [],
             "content": "A dragon circles the peak.", "constant": False, "enabled": True},
            {"uid": "1", "title": "", "keys": [], "secondary_keys": [],
             "content": "The harbor smells of tar.", "constant": False, "enabled": True},
            {"uid": "2", "title": "", "keys": [], "secondary_keys": [],
             "content": "Nothing notable here.", "constant": False, "enabled": True},
        ],
    }
    embedder = _FakeEmbedder()
    _run(manager.embed_lorebooks([book], embedder))

    rows = manager._world_conn.execute(
        "SELECT text, embedding FROM world_entries WHERE source_type = 'lorebook'"
    ).fetchall()
    assert len(rows) == 3
    for row in rows:
        expected = _run(embedder.get_embedding(row["text"]))
        assert row["embedding"] == _serialize(expected)


def test_embed_world_uses_batched_embeddings(tmp_path):
    from backend.engine.memory import _serialize

    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)
    manager.init_world_index(str(tmp_path / "world_index"))
    embedder = _FakeBatchEmbedder()
    world = {"lore": {
        "premise": "A dragon rules the skies.",
        "central_conflict": "The harbor cities resist its tithe.",
        "creation_myth": "The world hatched from an egg.",
    }}

    count = _run(manager.embed_world(world, embedder))
    assert count == 3
    assert embedder.batch_sizes == [3]  # one provider batch, not per-text calls
    assert embedder.single_calls == 0

    rows = manager._world_conn.execute(
        "SELECT text, embedding FROM world_entries"
    ).fetchall()
    for row in rows:
        expected = _run(_FakeEmbedder().get_embedding(row["text"]))
        assert row["embedding"] == _serialize(expected)


def test_lorebook_search_and_constant_injection(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory"), embedding_dim=3)
    manager.init_world_index(str(tmp_path / "world_index"))
    _run(manager.embed_lorebooks([_lorebook_record()], _FakeEmbedder()))

    constants = manager.get_constant_lorebook_entries()
    assert len(constants) == 1
    assert "The gods are silent." in constants[0]["text"]

    # Non-constant entries surface through search_world; constant ones are
    # excluded (they're always injected separately).
    results = manager.search_world([1.0, 0.0, 0.0], limit=5)
    texts = [r["text"] for r in results]
    assert any("dragon sleeps" in t for t in texts)
    assert not any("gods are silent" in t for t in texts)
    assert all(r["source_type"] == "lorebook" for r in results)


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
