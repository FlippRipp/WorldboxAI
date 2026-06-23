# Implementation Plan: Technical Debt & Polish

## Overview

This plan covers all non-feature technical improvements: state contract formalization, frontend quality, backend robustness, and remaining module contract gaps.

---

## 5.1 State Contract: TypedDict → Pydantic

### Current State

`WorldState` is a Python `TypedDict` in `backend/engine/state.py`. It provides type hints but no runtime validation, no default values, and no serialization control.

### Required Changes

#### Step 1: Define Pydantic model

File: `backend/engine/state.py`

```python
from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime

class WorldState(BaseModel):
    # Session
    active_save_id: str = ""
    
    # Turn
    turn: int = 0
    input_text: str = ""
    
    # History
    history: list[str] = []
    chat_messages: list[dict] = []
    
    # Module state
    module_data: dict[str, dict] = {}
    module_configs: dict[str, dict] = {}
    characters: dict[str, dict] = {}
    
    # Context
    current_context: str = ""
    memory_context: str = ""
    
    # Prompt
    prompt_pipeline: list[dict] = []
    last_prompt_trace: list[dict] = []
    
    # Veto/rewrite
    veto_retries: int = 0
    veto_reason: Optional[str] = None
    needs_rewrite: bool = False
    command_end_turn: bool = False
    command_message: str = ""
    
    # Internal
    sdk: Optional[Any] = None  # SDK reference, not serialized
    _lancedb_table: Optional[Any] = None  # Internal, not serialized
    
    class Config:
        extra = "allow"  # Allow modules to store extra fields
        arbitrary_types_allowed = True  # For SDK reference
```

#### Step 2: Update graph nodes to use Pydantic model

Each graph node already returns `dict` updates. Pydantic models can be updated with `.copy(update=...)` or `model_dump()` + merge. Simplest approach: keep nodes returning dicts, validate at boundaries.

```python
# In graph.py nodes:
async def gather_context_node(self, state: WorldState) -> dict:
    # state is the Pydantic model
    context = state.current_context
    # ... return partial dict
    return {"current_context": context}

# In graph execution:
result_dict = await graph.ainvoke(state.model_dump())
validated = WorldState(**result_dict)
```

#### Step 3: API validation

```python
# In server.py endpoints:
@router.get("/api/session")
async def get_session():
    state = session_manager.get_state()
    return state.model_dump(exclude={"sdk", "_lancedb_table"})
```

#### Step 4: Save serialization

```python
# In save_manager.py:
def save_turn(self, state: WorldState):
    data = state.model_dump(exclude={"sdk", "_lancedb_table"}, exclude_none=True)
    # ... write to disk ...
```

#### Step 5: Test fixture factory

```python
# tests/conftest.py
import pytest
from backend.engine.state import WorldState

@pytest.fixture
def empty_state():
    return WorldState()

@pytest.fixture
def state_with_turn():
    return WorldState(
        active_save_id="test_save",
        turn=5,
        input_text="I look around",
        history=["turn 1 story", "turn 2 story"],
        module_data={"wb_core_dice": {"last_roll": None}},
        module_configs={"wb_core_dice": {"default_dice": 20}}
    )
```

### Testing

- Test: WorldState() creates valid empty state with defaults
- Test: model_dump() serializes correctly for saves
- Test: extra fields from modules preserved via Config.extra = "allow"
- Test: Save/load round-trip preserves all fields
- Test: API response excludes internal fields

---

## 5.2 Frontend Quality (UI System Overhaul)

The frontend has a comprehensive UI overhaul plan documented in [ui-system-overhaul.md](./ui-system-overhaul.md). This section summarizes the deferred items from that plan.

Key items covered in the full plan:
- Component decomposition (splitting App.jsx into hooks + components)
- Vite proxy + API abstraction layer
- `new Function()` security hardening with widget caching + error boundaries
- Mobile sidebar drawer
- Cross-module communication via React Context (replacing `window` events)
- Markdown rendering for AI messages
- Accessibility (ARIA labels, focus traps, roles)
- Performance (memoization, scroll optimization, virtual lists)
- Loading/error/empty states
- Confirmation dialogs for destructive actions
- PromptStudio drag/drop + unsaved changes warning
- Widget contract documentation

Priority order is documented in Section 14 of the UI System Overhaul plan.

---

## 5.3 Backend Quality

### 5.3.1 Embedding Dimension Migration

**Current issue**: If the embedding model changes (e.g., from `text-embedding-004` to a newer model), LanceDB tables may have incompatible dimensions.

**Solution**: Store embedding metadata in LanceDB table metadata.

```python
# backend/engine/memory.py

class MemoryManager:
    EMBEDDING_METADATA_KEY = "embedding_model"
    EMBEDDING_DIM_KEY = "embedding_dimension"
    
    def _check_embedding_compatibility(self, table):
        """Check if stored embeddings match current model. Rebuild if not."""
        try:
            schema = table.schema
            current_model = self.embedding_model
            current_dim = self._get_embedding_dimension()
            
            stored_model = table.metadata.get(self.EMBEDDING_METADATA_KEY)
            stored_dim = table.metadata.get(self.EMBEDDING_DIM_KEY)
            
            if stored_model and stored_model != current_model:
                logger.warning(f"Embedding model changed: {stored_model} → {current_model}")
                self._rebuild_embeddings(table)
            
            if stored_dim and stored_dim != current_dim:
                logger.warning(f"Embedding dimension mismatch: {stored_dim} → {current_dim}")
                self._rebuild_embeddings(table)
        except Exception:
            pass  # Table might be new
    
    def _rebuild_embeddings(self, table):
        """Re-embed all stored texts with current model."""
        # Read all entries, re-embed, update table
        # This is expensive — warn the user
        logger.info("Rebuilding embedding index with current model...")
        ...
    
    def _get_embedding_dimension(self):
        """Get current model's embedding dimension by making a test embedding call."""
        ...
```

Add to `/api/health` memory status:
```json
{
  "memory": {
    "status": "ready",
    "entry_count": 42,
    "embedding_model": "gemini/text-embedding-004",
    "embedding_dimension": 768
  }
}
```

### 5.3.2 LanceDB Stale Index Handling

If LanceDB index files are corrupted or from an older version:

```python
def _open_table(self, table_name: str):
    try:
        return self.db.open_table(table_name)
    except Exception as e:
        logger.warning(f"Failed to open table '{table_name}': {e}")
        logger.info("Creating new table...")
        # Old table is left as-is; new table created
        return self._create_table(table_name)
```

### 5.3.3 API Edge Case Coverage

Expand `test_api.py`:

```python
def test_get_session_when_no_save():
    """GET /api/session returns 404 or empty session when no save loaded."""

def test_load_invalid_save_returns_error():
    """Loading non-existent save returns structured error."""

def test_undo_on_inactive_save_returns_error():
    """Undoing when no save is active returns error."""

def test_undo_beyond_turn_bounds_returns_error():
    """target_turn > current turn or < 0 returns error."""

def test_prompt_preview_with_invalid_block():
    """Preview with bad block definition returns validation error."""

def test_prompt_preview_with_duplicate_ids():
    """Duplicate block IDs return validation error."""

def test_websocket_message_before_session_ready():
    """Sending turn before session init returns error."""

def test_concurrent_turns_rejected():
    """Sending turn while another is processing returns busy error."""
```

---

## 5.4 Module Contract Polish

### 5.4.1 Add `on_validate_output` Contract

This is prerequisite for Validation Veto (Phase 7). Add to `MODULES.md` and implement dispatch.

Signature:
```python
async def on_validate_output(llm_output: str, state: dict, sdk) -> None:
    """
    Called after storyteller generates narrative, before it reaches the user.
    Raise sdk.ValidationVeto(reason) to trigger a story rewrite.
    Return None if output is acceptable.
    """
```

Engine dispatch in `graph.py` `storyteller_node`:
```python
# After generating story, before returning:
async def storyteller_node(self, state: WorldState) -> dict:
    # ... generate story ...
    
    # Dispatch validate hooks
    story_output = state.get("_story_output", "")
    for module in self.registry.loaded_modules.values():
        backend = module["backend"]
        if hasattr(backend, "on_validate_output"):
            try:
                await backend.on_validate_output(story_output, state, state.get("sdk"))
            except ValidationVeto as veto:
                state.needs_rewrite = True
                state.veto_reason = str(veto)
                state.veto_retries = state.get("veto_retries", 0)
                break  # First veto triggers rewrite
    
    return {"_story_output": story_output}
```

### 5.4.2 Add `commands` Manifest Validation

Already covered in `phase7-sdk-features.md` section 4.2.

### 5.4.3 Make Modules Consume `state["module_configs"]`

All implemented backend modules should read settings from `module_configs` consistently. This is already done in `core_combat`; ensure it's followed in new modules.

Pattern:
```python
async def on_gather_context(self, state, sdk):
    config = state.get("module_configs", {}).get("wb_my_module", {})
    setting_value = config.get("my_setting", default_value)
    # Use setting_value in logic
```

---

## Execution Order

1. **5.1** State Contract (foundation for all other work)
2. **5.2.2** Widget Error Boundaries (prevents frontend crashes)
3. **5.2.5** HTML Title (trivial, immediate)
4. **5.2.6** Remove App.css (trivial, immediate)
5. **5.2.4** Save Name + Turn Display (UX improvement)
6. **5.2.1** Replace new Function() (security)
7. **5.2.3** Health Panel (depends on D6: health endpoint extension)
8. **5.3** Backend Quality (embedding migration, stale index, API edge cases)
9. **5.4** Module Contract Polish (prep for Phase 7)
