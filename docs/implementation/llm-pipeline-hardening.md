# Implementation Plan: LLM Pipeline Hardening (Stabilization D Remainder)

## Overview

The LLM pipeline is functional but needs stronger structured output contracts, provider validation, message adaptation, and the veto/rewrite loop. This plan covers the 6 remaining items from Stabilization D.

## D1: Structured Output Contracts for Librarian

**Current state**: `summarize_memory` and `score_memory_importance` use basic prompt-based LLM calls without strict JSON schema enforcement.

**Required changes**:

### 1.1 Define Pydantic output schemas

```python
# backend/engine/llm.py or new backend/engine/schemas.py

from pydantic import BaseModel, Field
from typing import Optional

class MemorySummary(BaseModel):
    summary: str = Field(description="One-paragraph summary of the memory content")
    entities: list[str] = Field(description="Named entities mentioned (characters, places, items)")
    topics: list[str] = Field(description="Key topics or themes")
    turn_range: Optional[str] = Field(default=None, description="Range of turns covered, e.g. 'turns 12-15'")

class MemoryImportance(BaseModel):
    importance: int = Field(ge=1, le=10, description="Importance score 1-10")
    reason: str = Field(description="Brief justification for the score")
    permanent: bool = Field(default=False, description="Whether this memory should never decay")
```

### 1.2 Add structured generation methods to LLMService

```python
# backend/engine/llm.py

async def summarize_memory_structured(self, messages: list[dict]) -> MemorySummary:
    """Calls LLM with response_format=MemorySummary schema."""
    ...

async def score_memory_importance_structured(self, memory_text: str) -> MemoryImportance:
    """Calls LLM with response_format=MemoryImportance schema."""
    ...
```

### 1.3 Update Librarian node

In `backend/engine/graph.py` `librarian_node`:
- Replace loose `summarize_memory()` with `summarize_memory_structured()`
- Replace loose `score_memory_importance()` with `score_memory_importance_structured()`
- Store `entities`, `topics`, `reason`, and `permanent` flags in memory metadata
- Use `permanent=True` to exempt memories from decay purging

### 1.4 Update memory storage

- `backend/engine/memory.py` `add_memory()`: accept and store new fields (`entities`, `topics`, `permanent`)
- `purge_decayed_memories()`: skip entries with `permanent=True`

### 1.5 Testing

- Add `test_memory_structured.py`: verify structured output parsing
- Mock LLM should return valid `MemorySummary` / `MemoryImportance` objects
- Test that permanent memories survive purge
- Test that entities/topics are stored and retrievable

---

## D2: Live Model Validation

**Current state**: Models are configured via `.env` but never validated at startup. If `STORYTELLER_MODEL` is invalid, the first turn fails.

**Required changes**:

### 2.1 Startup validation in LLMService constructor

```python
# backend/engine/llm.py

class LLMService:
    def __init__(self, mode: str = "live"):
        self.mode = mode
        if mode == "live":
            self._validate_models()
    
    def _validate_models(self):
        """Check that configured models exist and are accessible."""
        models_to_check = []
        if self.storyteller_model:
            models_to_check.append(self.storyteller_model)
        for model in self.storyteller_fallback_models:
            models_to_check.append(model)
        
        for model in models_to_check:
            try:
                # Use LiteLLM's model list or a simple ping
                litellm.validate_models([model])
            except Exception as e:
                logger.warning(f"Model {model} may not be available: {e}")
                # Don't crash — just warn. The health endpoint will report status.
```

### 2.2 Add model health to /api/health

Extend the existing health response:

```json
{
  "models": {
    "storyteller": {
      "model": "gemini/gemini-2.0-flash",
      "status": "available",
      "last_checked": "2026-06-20T12:00:00Z"
    },
    "fallback_models": [],
    "embedding": {
      "model": "gemini/text-embedding-004",
      "status": "available"
    }
  }
}
```

### 2.3 Lazy re-validation

- Validate on first turn if startup validation was skipped (e.g., mock mode)
- Cache validation results for 5 minutes
- Expose `POST /api/health/validate-models` for manual re-check

### 2.4 Testing

- Verify health endpoint returns model status
- Verify mock mode skips validation
- Verify invalid model only warns, doesn't crash startup

---

## D3: Provider-Specific Message Adaptation

**Current state**: The compiler emits a logical message array directly. Some providers (Gemini) handle multiple system messages poorly or have restrictions.

**Required changes**:

### 3.1 Add adaptor layer in LLMService

```python
# backend/engine/llm.py

class LLMService:
    def _adapt_messages_for_provider(self, messages: list[dict], provider: str) -> list[dict]:
        """Adapt message array for provider-specific requirements."""
        if provider == "gemini":
            return self._adapt_for_gemini(messages)
        return messages
    
    def _adapt_for_gemini(self, messages: list[dict]) -> list[dict]:
        """Gemini specifics:
        - Merge multiple system messages into one (join with newlines)
        - Ensure system message is first
        - System message cannot be empty (use placeholder if needed)
        - role: 'system' may need to be 'user' for some Gemini variants
        """
        adapted = []
        system_parts = []
        for msg in messages:
            if msg["role"] == "system":
                system_parts.append(msg["content"])
            else:
                adapted.append(msg)
        if system_parts:
            adapted.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
        return adapted
```

### 3.2 Detect provider from model name

```python
def _detect_provider(self, model_name: str) -> str:
    if model_name.startswith("gemini/"):
        return "gemini"
    if model_name.startswith("openai/"):
        return "openai"
    if model_name.startswith("anthropic/"):
        return "anthropic"
    return "generic"
```

### 3.3 Wire adaptation into generate_story_from_messages

In `generate_story_from_messages()`, call `_adapt_messages_for_provider()` before sending to LiteLLM.

### 3.4 Testing

- Unit test: Gemini adaptor merges multiple system messages
- Unit test: System message is always first after adaptation
- Integration: Storyteller works with Gemini after adaptation

---

## D4: Validation Veto & Rewrite Loop

**Current state**: The PromptCompiler supports a veto block injection at `depth: 0`, but the LangGraph retry loop is not wired.

**Required changes**:

### 4.1 Add veto state to WorldState

```python
# backend/engine/state.py

class WorldState(TypedDict, total=False):
    # ... existing fields ...
    veto_retries: int          # Current retry count (0-2)
    veto_reason: str | None    # Reason from last veto
    needs_rewrite: bool        # Flag set by on_validate_output
```

### 4.2 Wire conditional edge in LangGraph

```python
# backend/engine/graph.py

def add_conditional_edges(self):
    """After reader_node, check if a veto was raised."""
    self.graph.add_conditional_edges(
        "reader_node",
        self._check_veto,
        {
            "rewrite": "storyteller_node",  # Loop back
            "continue": "librarian_node"     # Proceed normally
        }
    )

def _check_veto(self, state: WorldState) -> str:
    if state.get("needs_rewrite") and state.get("veto_retries", 0) < 3:
        return "rewrite"
    return "continue"
```

### 4.3 Veto injection in Storyteller

When the storyteller detects `needs_rewrite=True`:
- Inject the `veto_reason` as an additional system message at `depth: 0`
- Increment `veto_retries`
- Clear `needs_rewrite` and `veto_reason` after injection

### 4.4 Graceful fallback

After 3 failed attempts (retries >= 3):
- Skip the LLM call
- Return a generic system fallback message: "The Storyteller was unable to produce a valid response. Please try a different action."
- Log the veto chain for debugging

### 4.5 Testing

- Mock module raises veto → verify rewrite loop triggers
- Verify max retries (3) is enforced
- Verify fallback message after exhaustion

---

## D5: Drag/Drop Ordering in Prompt Studio

**Current state**: Prompt Studio uses Up/Down buttons for reordering.

**Required changes**:

### 5.1 Implement drag-and-drop in PromptStudio.jsx

Use HTML5 Drag and Drop API (no library needed):

```jsx
// Each block row gets draggable + event handlers
<div
  draggable
  onDragStart={(e) => handleDragStart(e, index)}
  onDragOver={(e) => handleDragOver(e, index)}
  onDrop={(e) => handleDrop(e, index)}
>
```

State management:
- `dragIndex`: which block is being dragged
- `hoverIndex`: where it's hovering
- `handleDrop`: reorder the blocks array via splice

### 5.2 Visual feedback

- Dragging block: reduced opacity + border highlight
- Drop target: dashed border indicator between blocks
- Touch support: basic touch event handlers for mobile

### 5.3 Testing

- Manual verification: drag block from position 1 to position 5, save, verify order persists
- Edge cases: drag to same position (no-op), drag to start, drag to end

---

## D6: Mock Mode in Frontend Health Panel

**Current state**: Mock mode is backend-only. The frontend has no indication.

**Required changes**:

### 6.1 Extend /api/health response

```json
{
  "llm_mode": "mock",
  "llm_mode_note": "Running in deterministic mock mode. No live API calls.",
  "models": { ... }
}
```

### 6.2 Add health panel component

Create `frontend/src/HealthPanel.jsx`:
- Display LLM mode (mock/live) with color indicator
- Display model statuses
- Show loaded modules list
- Show active save name and current turn
- Accessible from header or sidebar

### 6.3 Integrate into App.jsx

- Fetch `/api/health` on mount and periodically (every 30s)
- Store in React state
- Display in a collapsible panel or modal

---

## Execution Order

1. D1 (Structured Librarian output) — foundation for better memory quality
2. D4 (Veto/rewrite loop) — enables module output validation
3. D3 (Provider adaptation) — improves Gemini reliability
4. D2 (Model validation) — prevents silent startup failures
5. D5 (Drag/drop ordering) — frontend UX improvement
6. D6 (Health panel) — frontend visibility
