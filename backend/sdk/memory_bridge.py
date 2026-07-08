"""Memory access bridge exposed to modules through the WorldBox SDK."""
import logging

logger = logging.getLogger(__name__)


class MemoryBridge:
    def __init__(self):
        self._engine = None

    def _set_engine(self, engine):
        self._engine = engine

    async def remember(self, npc_id: str, text: str, turn: int, importance: int = 5,
                       permanent: bool = False, tags: list[str] | None = None) -> str:
        if self._engine is None or self._engine.memory is None or not text:
            return ""
        try:
            vector = await self._engine.llm.get_embedding(text)
            return self._engine.memory.add_memory(
                vector=vector, text=text, turn=turn, importance=importance,
                entities=[f"npc:{npc_id}", *(tags or [])],
                permanent=permanent,
            )
        except Exception as e:
            logger.error(f"[MemoryBridge] remember failed for {npc_id}: {e}")
            return ""

    async def forget(self, npc_id: str, tags: list[str] | None = None) -> int:
        """Delete an NPC's stored memories -- all of them, or only those carrying
        every tag in ``tags`` (e.g. tags=["profile"] removes just the embedded
        profile). Returns the number of memories deleted."""
        if self._engine is None or self._engine.memory is None:
            return 0
        try:
            wanted = set(tags or [])
            rows = self._engine.memory.get_memories_by_entity(f"npc:{npc_id}", limit=10000)
            deleted = 0
            for row in rows:
                if wanted and not wanted.issubset(set(row.get("entities", []))):
                    continue
                if self._engine.memory.delete_memory(row["id"]):
                    deleted += 1
            return deleted
        except Exception as e:
            logger.error(f"[MemoryBridge] forget failed for {npc_id}: {e}")
            return 0

    async def recall(self, npc_id: str, limit: int = 3) -> list[dict]:
        if self._engine is None or self._engine.memory is None:
            return []
        try:
            return self._engine.memory.get_memories_by_entity(f"npc:{npc_id}", limit=limit)
        except Exception as e:
            logger.error(f"[MemoryBridge] recall failed for {npc_id}: {e}")
            return []
