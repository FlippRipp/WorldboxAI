# WorldBox Prompt Pipeline

WorldBox now has a prompt block compiler and a first-pass frontend Prompt Studio. Saves store a configurable prompt block array, the frontend can edit save-owned blocks, and the engine can preview or run the combined save/module block list as the Storyteller LLM message payload.

## Current Files

- `backend/engine/prompt_pipeline.py`: prompt block validation, default pipeline, compiler, chat-depth insertion, and debug trace generation.
- `backend/engine/graph.py`: Storyteller node uses `PromptCompiler` instead of a hardcoded prompt string, and renders module-declared prompt blocks before compilation.
- `backend/engine/llm.py`: `generate_story_from_messages()` sends compiled message arrays to LiteLLM.
- `frontend/src/PromptStudio.jsx`: frontend editor/debug view for save-owned prompt blocks, module-owned blocks, compiled draft previews, and compile traces.
- `frontend/src/App.jsx`: loads/saves prompt pipeline state and opens Prompt Studio from the header.
- `Core/prompt_pipeline.json`: per-save prompt pipeline persistence.

## Supported Block Shape

```json
{
  "id": "core_narrator_rules",
  "type": "static_text",
  "source": "engine",
  "enabled": true,
  "role_type": "system",
  "placement": "system_relative",
  "depth": null,
  "config": {
    "text": "You are a creative storyteller in a text-based RPG."
  }
}
```

Required fields:

- `id`: Unique block id within the pipeline.
- `type`: Currently `static_text`, `engine_context`, or `module_prompt`.
- `role_type`: `system`, `user`, or `assistant`.
- `placement`: `system_relative` or `chat_injection`.
- `config`: Block-specific settings.

Optional fields:

- `source`: `engine`, `user`, or future values like `module:wb_core_combat`.
- `enabled`: Defaults to `true`.
- `depth`: Required for `chat_injection`; `0` means absolute bottom after the latest user message.

## Supported Block Types

### Static Text

Uses `config.text` as the block content.

### Engine Context

Renders `state["current_context"]` as:

```text
Current Game State:
...
```

For now, RAG memories and module context are still gathered by `EngineGraph.gather_context_node()` before compilation. The compiler then renders those gathered strings through the `engine_context` block.

### Module Prompt

Module manifests can declare dynamic prompt blocks with `type: "module_prompt"`. The engine calls the module's `on_render_prompt_block(block, state, sdk)` hook before compilation and stores the returned text in `config.text`.

The hook may return a string or a dictionary with `content` or `text`:

```python
async def on_render_prompt_block(block: dict, state: dict, sdk) -> str | dict | None:
    return {"content": "<combat_status>Player HP: 85.</combat_status>"}
```

Module prompt block ids are namespaced in traces as `<module_id>:<block_id>`.

## Default Pipeline

New saves start with:

- `core_narrator_rules`: base Storyteller identity.
- `engine_context`: gathered RAG/module/game context.
- `storyteller_task`: instruction to describe the action outcome and environment.

The current user action is appended as a `user` message after historical chat messages.

## API Endpoints

```text
GET /api/session/prompt-pipeline
PUT /api/session/prompt-pipeline
POST /api/session/prompt-pipeline/preview
```

`PUT` body:

```json
{
  "prompt_pipeline": []
}
```

The backend validates block ids, types, roles, placements, depth values, and required static text before saving.

`POST /preview` uses the same body shape as `PUT`, but it does not save. It compiles the submitted draft against the active session state and module-declared prompt blocks, then returns:

```json
{
  "messages": [
    {"role": "system", "content": "..."}
  ],
  "trace": []
}
```

## Frontend Prompt Studio

The header `Prompts` button opens the first Prompt Studio UI.

Current capabilities:

- Edit save-owned prompt blocks.
- Toggle blocks enabled/disabled.
- Edit block id, type, role, placement, chat injection depth, and text/empty-context config.
- Reorder save-owned blocks with Up/Down controls.
- Add and remove static text blocks.
- Save changes through `PUT /api/session/prompt-pipeline`.
- Preview the current draft through `POST /api/session/prompt-pipeline/preview` before saving.
- Show the full compiled message order, roles, and content for the current draft preview.
- Show module-declared prompt blocks as read-only entries.
- Show the preview trace, or the latest prompt compile trace after a turn when no preview is loaded.
- Show skipped trace entries and skip reasons.

Current limitations:

- Drag/drop ordering is not implemented yet.
- Module-owned prompt blocks are visible but not editable from saves.
- There is no local frontend validation beyond form controls; backend validation remains authoritative.

## Debug Trace

Every Storyteller turn stores `last_prompt_trace` in state. The trace records which blocks were included, skipped, and where they landed in the compiled message array.

Module-declared blocks appear in the same trace as save-owned blocks. For example, `wb_core_combat:combat_status` is inserted as a chat-depth prompt block before the current user action.

Prompt Studio can also compile a draft preview without saving it or running a turn. Preview trace data is transient and only exists in the frontend until another preview is requested or the modal is reopened.

## Implemented But Not Fully Wired Yet

- The compiler supports a hardcoded Validation Veto block injected at `depth: 0`.
- Module-provided prompt blocks and `on_render_prompt_block` hooks are backend-active and visible in Prompt Studio, but module blocks are not save-editable.
- The frontend drag/drop editor is not implemented yet; Prompt Studio currently uses Up/Down controls.
- Provider-specific message adaptation is not implemented yet; the compiler currently emits the logical message array directly.
