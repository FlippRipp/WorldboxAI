from backend.sdk.llm_bridge import LLMBridge
from backend.sdk.memory_bridge import MemoryBridge


class ValidationVeto(Exception):
    """Raised by on_validate_output to trigger a story rewrite loop."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class WorldBoxUI:
    def __init__(self):
        self.on_token = None
        self.on_reasoning_token = None
        self.on_message_complete = None
        self.on_status = None

    async def emit_token(self, token: str):
        if self.on_token:
            await self.on_token(token)

    async def emit_reasoning_token(self, token: str):
        if self.on_reasoning_token:
            await self.on_reasoning_token(token)

    async def emit_status(self, stage: str, label: str):
        """Tell the client which pipeline stage is running so the wait between
        narration and turn completion isn't a silent dead zone. Best-effort:
        status is cosmetic, so failures must never break a turn."""
        if self.on_status:
            try:
                await self.on_status(stage, label)
            except Exception:
                pass

    async def emit_message_complete(self, content: str, reasoning: str = ""):
        """Signal that the storyteller's narration is fully generated, so the
        client can finalize the message immediately — before the reader agent
        runs its (non-visible) mutation-extraction pass."""
        if self.on_message_complete:
            await self.on_message_complete(content, reasoning)

class WorldBoxSDK:
    """Mock SDK for Phase 1/2/3 to satisfy module function signatures."""
    ValidationVeto = ValidationVeto

    def __init__(self):
        self.version = "1.0.0"
        self.ui = WorldBoxUI()
        self.llm = LLMBridge()
        self.memory = MemoryBridge()
        self._session_state_ref = None

    def bind_session_state(self, state_ref):
        self._session_state_ref = state_ref

    def reveal_map_node(self, node_id: str):
        if self._session_state_ref is not None:
            revealed = list(self._session_state_ref.get("revealed_node_ids", []))
            if node_id not in revealed:
                revealed.append(node_id)
            self._session_state_ref["revealed_node_ids"] = revealed
            return revealed
        return []
