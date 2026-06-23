from backend.sdk.llm_bridge import LLMBridge


class ValidationVeto(Exception):
    """Raised by on_validate_output to trigger a story rewrite loop."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class WorldBoxUI:
    def __init__(self):
        self.on_token = None

    async def emit_token(self, token: str):
        if self.on_token:
            await self.on_token(token)

class WorldBoxSDK:
    """Mock SDK for Phase 1/2/3 to satisfy module function signatures."""
    ValidationVeto = ValidationVeto

    def __init__(self):
        self.version = "1.0.0"
        self.ui = WorldBoxUI()
        self.llm = LLMBridge()
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
