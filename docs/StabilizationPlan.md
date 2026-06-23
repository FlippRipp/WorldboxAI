# WorldBox Stabilization Plan

This document records the current state of the WorldBox prototype after Phases 1-6, the systems that are still incomplete, and the recommended stabilization work before starting Phase 7.

## Current Status

WorldBox has a working vertical slice across Phases 1-6:

- The LangGraph engine runs context gathering, storytelling, reader mutation, and librarian memory nodes.
- FastAPI serves WebSocket chat, module metadata, widgets, and module assets.
- The React frontend supports chat, streaming text, dynamic widgets, UI slots, and generated settings UI.
- The save manager can create saves, write turn snapshots, and undo turns in isolated tests.
- LanceDB memory can store and retrieve semantic memories.
- Test modules prove sidebar, header, modal, and chat-feed slot injection.

The project is still prototype-grade. Most systems work on the happy path, but they need stronger contracts, persistence, error handling, and automated tests before advanced SDK features are added.

## Stabilization Progress

### Stabilization A: Project Hygiene

Status: Started.

Completed items:

- Added `requirements.txt` for backend direct dependencies.
- Added `pytest` and `pytest.ini` for deterministic backend test discovery.
- Added `backend/.env.example` for required and optional environment settings.
- Added `SETUP.md` with local setup, startup, health check, and smoke test instructions.
- Added `GET /api/health` to report module, LLM, memory, and current session status.
- Improved `start.bat` with preflight checks for `venv`, `backend/.env`, `npm`, and frontend dependencies.

Remaining hygiene follow-up:

- Add a stricter frontend lint pass once the dynamic widget loader design settles.

### Stabilization B: Save-Backed Sessions

Status: Complete (remaining items are future enhancements).

Completed items:

- Added `GameSessionManager` to own the active local play session.
- The backend now creates or loads a default `autosave` under `data/saves/`.
- WebSocket turns now run against session state instead of a standalone global `game_state` object.
- Completed turns are saved through `SaveManager.save_turn`.
- Added `GET /api/session` for active save/session status.
- Added explicit save management endpoints:
  - `GET /api/saves`
  - `POST /api/saves`
  - `POST /api/saves/{save_id}/load`
  - `POST /api/saves/{save_id}/undo`
- Updated `/api/health` to report save-backed session status.
- Moved LanceDB memory path from `test_data/saves/active_save/vector_index` to the active save workspace path.
- Undo now invokes active memory rollback through `EngineGraph.rollback_memory` when a memory index has been initialized.
- Added a basic frontend save panel for active save, current turn, save selection, save creation, loading, and undo.
- Added `test_session_manager.py` for save-backed session persistence smoke testing.
- Added `Core/module_configs.json` persistence to saves.
- Added module config APIs:
  - `GET /api/session/module-configs`
  - `PUT /api/session/module-configs`
- Wired the settings modal to load and save module configs through the backend.
- Added `Core/chat_messages.json` persistence so saves retain both user and AI chat messages.
- Updated turn snapshots to include chat history/message files for future undo consistency.

Remaining Stabilization B follow-up:

- Decide how `.wbx` packing should handle live LanceDB files during active gameplay.
- Remove remaining single-session assumptions before multi-session support.
- Make backend modules actively consume `state["module_configs"]`.

### Stabilization C: Module Contract

Status: Complete (remaining items are future enhancements).

Completed items:

- Added manifest validation to `ModuleRegistry`.
- Registry now validates required manifest fields, module ids, UI slots, dependencies, settings schemas, and mutation schemas.
- Registry now stores each loaded module's filesystem path for later module tooling.
- Added `mutation_schema` to `core_combat/manifest.json`.
- Added `core_combat.on_mutate_state` so combat owns HP mutation behavior.
- Updated `core_combat.on_gather_context` to read saved module config from `state["module_configs"]`.
- Updated `EngineGraph.reader_node` to build Reader schemas from module manifests.
- Updated `EngineGraph.reader_node` to dispatch extracted mutations to module-owned `on_mutate_state` hooks.
- Added `test_module_contract.py` for deterministic module-owned mutation dispatch testing without live LLM calls.
- Added dependency sorting/topological load order to `ModuleRegistry`.
- Added smoke tests for dependency ordering, invalid manifest rejection, missing dependencies, and dependency cycles.
- Added `MODULES.md` to document current manifest fields, load behavior, state access, and supported backend hook signatures.
- Added `prompt_blocks` manifest validation for module-declared Storyteller prompt injection.
- Added `core_combat.prompt_blocks` and `core_combat.on_render_prompt_block` for dynamic combat prompt context.

Remaining Stabilization C follow-up:

- Add `on_validate_output` contract before implementing Validation Veto.
- Add `commands` manifest validation before implementing slash command pre-router.
- Move any remaining module-specific behavior out of engine core. See [implementation/module-engine-decoupling.md](./implementation/module-engine-decoupling.md) for the decoupling plan based on the full codebase review.

### Stabilization D: LLM Pipeline Hardening

Status: Started.

Completed items:

- Added `LLM_MODE=mock|live` support to `LLMService`.
- Mock mode now provides deterministic story generation, mutation extraction, and embeddings without live provider calls.
- Reader mutation extraction now retries malformed live JSON responses once and falls back to `{}` instead of crashing the turn.
- Added explicit `summarize_memory` and `score_memory_importance` LLM methods for Librarian behavior.
- Updated `EngineGraph.librarian_node` to use the role-specific memory methods.
- Added `test_engine_mock.py` for full graph smoke testing in mock mode and malformed Reader JSON fallback coverage.
- Updated `/api/health` to report `LLM_MODE` and treat mock mode as healthy without `GEMINI_API_KEY`.
- Added `PromptCompiler` and default prompt block pipeline in `backend/engine/prompt_pipeline.py`.
- Storyteller prompt assembly now compiles prompt blocks into LiteLLM message arrays instead of building one hardcoded string.
- Prompt pipelines are persisted per save in `Core/prompt_pipeline.json`.
- Added `GET /api/session/prompt-pipeline` and `PUT /api/session/prompt-pipeline`.
- Added `test_prompt_pipeline.py` for prompt block ordering, chat-injection depth, veto placement, invalid pipeline rejection, and graph prompt trace coverage.
- Added `PROMPTS.md` to document the current backend prompt pipeline contract.
- Storyteller prompt compilation now merges save-owned prompt blocks with module-declared prompt blocks.
- Added backend support for `on_render_prompt_block(block, state, sdk)` hooks.
- Added first-pass frontend Prompt Studio for editing save-owned prompt blocks, viewing module-owned blocks, and inspecting the latest compile trace.
- Added `POST /api/session/prompt-pipeline/preview` for non-persistent draft prompt compilation.
- Prompt Studio now previews the full compiled message order, roles, content, and trace before saving.
- Storyteller live calls now retry transient provider failures, attempt a non-stream fallback after streaming errors, and support optional `STORYTELLER_FALLBACK_MODELS`.
- WebSocket turn failures now return structured `type: "error"` payloads instead of crashing the ASGI connection or saving partial turns.

Remaining Stabilization D follow-up:

- Add stronger structured output contracts for Librarian summaries and importance scores.
- Validate configured live models through startup or `/api/health`.
- Decide whether mock mode should be exposed in the frontend health panel once that exists.
- Upgrade Prompt Studio with drag/drop ordering.
- Add provider-specific message adaptation if Gemini/LiteLLM behaves poorly with multiple system messages.

## Immediate Todo List

| Priority | Task | Goal |
| --- | --- | --- |
| Done | Add dependency and setup docs | Make the project reproducible from a fresh checkout. |
| Done | Add backend health checks | Report modules, LLM config, save status, memory status, and readiness. |
| Done | Replace global `game_state` | Move from one in-memory server state to save-backed game sessions. |
| Done | Integrate `SaveManager` into turn execution | Save every completed turn, snapshot state, and support resume. |
| High | Formalize `WorldState` | Replace TypedDict with Pydantic BaseModel for API, graph, saves, modules. |
| Done | Validate module manifests | Enforce module ids, slots, settings schema, dependencies, and mutation schemas. |
| Done | Move mutation logic into modules | Remove hardcoded combat mutation from `EngineGraph`. |
| Done | Started: Harden LLM service | Structured outputs, retries, fallbacks, and mock test mode done. Remaining: D2 (live model validation), D3 (provider adaptation). |
| Done | Harden RAG memory | Embedding metadata, model change handling, rollback -- done. Remaining: wire auto-purge, LanceDB stale index handling. |
| Done | Persist module settings | Save settings to backend state and expose them to modules. |
| Done | Add automated tests | Deterministic backend tests cover API, memory, engine (mock), save lifecycle, session, prompt pipeline, module contract, validation veto. |
| Medium | Delay Phase 7 | Start Event Bus, commands, and veto only after Phases 1-6 are stable. |

## Missing Systems And Technical Debt

### 1. Save-Backed Game Sessions

The API currently keeps active gameplay in a global in-memory `game_state`. This creates several problems:

- All browser clients share the same state.
- Backend restart loses active gameplay.
- The save manager is not used during normal WebSocket turns.
- Undo does not affect the active chat session.
- RAG memory is not tied cleanly to a specific `.wbx` save.

Required work:

- Add a `GameSessionManager` or equivalent session owner.
- Load or create an active save before play starts.
- Save every completed turn.
- Store turn number, history, module data, character data, module settings, and memory metadata.
- Add save APIs for listing, creating, loading, saving, and undoing.

Suggested API endpoints:

- `GET /api/saves`
- `POST /api/saves`
- `POST /api/saves/{save_id}/load`
- `POST /api/saves/{save_id}/undo`
- `GET /api/session`

Definition of done:

- Play several turns, restart the backend, and continue from the same state.
- Undo restores module data, turn metadata, and relevant memory state.
- Two sessions do not accidentally share the same mutable state.

### 2. Formal WorldState Contract

The current state is still a loose dictionary passed between the API, graph, modules, saves, and frontend.

Required work:

- Define one canonical `WorldState` structure.
- Include at least these fields:
  - `active_save_id`
  - `turn`
  - `input_text`
  - `history`
  - `module_data`
  - `module_configs`
  - `characters`
  - `current_context`
  - `memory_context`
- Make graph nodes return predictable partial state updates.
- Avoid accidental in-place mutation of nested state unless explicitly intended.

Definition of done:

- Tests can construct a valid state fixture.
- Every graph node accepts and returns documented fields.
- Save/load round-trips preserve the complete state contract.

### 3. Module Backend Contract

Modules currently load and can inject widgets, but backend gameplay behavior is still thin.

Current gaps:

- No manifest validation.
- No dependency sorting.
- No declared mutation schemas.
- No generalized `on_mutate_state` dispatch.
- No module-owned validation.
- No persisted module settings.
- Some test modules contain placeholder backend files.

Required manifest fields:

- `id`
- `name`
- `version`
- `dependencies`
- `ui_slots`
- `settings_schema`
- `prompt_blocks`
- `mutation_schema`
- `commands`

Required backend hooks:

- `on_init(sdk)`
- `on_gather_context(state, sdk)`
- `on_render_block_<block_id>(state, sdk)`
- `on_mutate_state(mutations, state, sdk)`
- `on_validate_output(llm_output, state, sdk)`

Definition of done:

- `EngineGraph` has no combat-specific mutation logic.
- `core_combat` owns its HP mutation behavior.
- Invalid manifests fail with clear errors.
- Module settings are available through `state["module_configs"]`.

### 4. LLM Pipeline Hardening

The LLM layer works, but it is not robust enough for long sessions or automated testing.

Current gaps:

- Reader mutation schema is hardcoded.
- Mutation parsing has weak failure handling.
- Librarian uses the general storyteller call for summarization.
- Memory importance is mocked.
- No mock LLM mode exists for deterministic tests.

Required work:

- Split LLM methods by role:
  - `generate_story`
  - `extract_mutations`
  - `summarize_memory`
  - `score_memory_importance`
  - `embed_text`
- Use structured JSON contracts for Reader and Librarian outputs.
- Add retries for malformed JSON and transient provider failures.
- Add a mock LLM provider for automated tests.
- Validate configured models at startup or through a health endpoint.

Definition of done:

- Tests can run without live LLM calls.
- Reader failures do not crash the WebSocket session.
- Librarian produces structured memory data with importance scores.

### 5. RAG Memory Integration

LanceDB memory now works, but it is not yet fully save-aware.

Current gaps:

- Memory path is currently a test path rather than save-derived.
- Embedding model metadata is not stored as a durable save setting.
- Embedding dimension mismatch warns but does not enforce a safe migration path.
- Undo rollback is not connected to memory deletion.
- Librarian currently runs during the turn instead of as a background task.

Required memory fields:

- `id`
- `text`
- `summary`
- `source_turn_start`
- `source_turn_end`
- `turn_generated`
- `importance`
- `created_at`
- `embedding_model`
- `embedding_dimension`

Embedding model change policy options:

- Block loading with a clear error until the user rebuilds memory.
- Create a new table per embedding model.
- Rebuild the vector index from stored text summaries.

Recommended policy:

- Store raw memory text and metadata independent of vectors.
- Store vectors in a model-specific LanceDB table.
- If the embedding model changes, rebuild vectors from stored memory text.

Definition of done:

- Memory is stored under the active save.
- Undo deletes or ignores memories from future turns.
- Changing embedding models has a deliberate, testable behavior.

### 6. Frontend Durability And Module UI Boundaries

The frontend proves the UI concept, but it needs stronger persistence and error isolation.

Current gaps:

- Settings are local-only and are lost on refresh.
- Settings do not reach backend modules.
- Dynamic widgets are executed with `new Function`, which is acceptable for local trusted prototypes but unsafe for untrusted modules.
- There is no active save selector.
- There is no turn indicator.
- There is no backend health panel.
- Slot-specific mobile behavior needs more testing.

Required work:

- Persist settings through backend APIs.
- Add active save and current turn display.
- Add backend health display.
- Add stronger widget error boundaries.
- Pass explicit widget props:
  - `state`
  - `config`
  - `assetsBaseUrl`
  - `emitEvent`
- Document the trust boundary for dynamically loaded frontend widgets.

Definition of done:

- Settings persist after refresh and restart.
- Widgets can access their module config.
- Broken widgets do not break the entire app.

### 7. Automated Testing

Current tests are mostly manual scripts and live smoke tests.

Required backend tests:

- Module manifest validation.
- Registry loading and dependency ordering.
- Save create/load/save/undo lifecycle.
- Graph turn execution with mock LLM.
- Module-owned mutation dispatch.
- Memory add/search/rollback.
- API health and module endpoints.

Required frontend checks:

- `npm run build`
- Widget loader error display.
- Settings schema rendering.
- Basic chat input behavior.

Definition of done:

- One command validates backend core behavior.
- One command validates frontend build.
- Live LLM tests are optional and clearly marked.

## Recommended Implementation Order

### Stabilization A: Project Hygiene

Goal: Make the project easy to run, diagnose, and reproduce.

Tasks:

- Add backend dependency manifest.
- Add `.env.example`.
- Add setup instructions.
- Add `/api/health`.
- Improve `start.bat` messages and failure handling.

Definition of done:

- A fresh setup can install dependencies and start both servers.
- Health endpoint reports backend readiness.

### Stabilization B: Save-Backed Sessions

Goal: Make gameplay durable.

Tasks:

- Add `GameSessionManager`.
- Replace global `game_state`.
- Load or create active save on startup.
- Save every completed turn.
- Add save/load/undo APIs.
- Connect undo to active memory rollback.

Definition of done:

- Play several turns, restart server, and continue from the same state.
- Undo restores state and memory consistency.

### Stabilization C: Module Contract

Goal: Make modules first-class backend participants.

Tasks:

- Add manifest schema validation.
- Add dependency ordering.
- Add mutation schemas.
- Add `on_mutate_state` dispatch.
- Move combat HP mutation into `core_combat`.
- Persist module settings.

Definition of done:

- Engine no longer contains module-specific combat mutation logic.
- Module settings affect backend behavior.

### Stabilization D: LLM And RAG Hardening

Goal: Make AI behavior reliable and testable.

Tasks:

- Split LLM methods by role.
- Add structured Reader and Librarian outputs.
- Add memory metadata.
- Handle embedding model changes deliberately.
- Connect memory rollback to save undo.
- Add mock LLM mode.

Definition of done:

- RAG memories survive restart.
- Undo removes or ignores future memories.
- Tests can run without live LLM calls.

### Stabilization E: Automated Test Suite

Goal: Stop relying on manual browser testing for core behavior.

Status: Started.

Completed items:

- Added `pytest` to backend dependencies.
- Added `pytest.ini` so default collection runs deterministic tests only and excludes live/manual provider scripts.
- Converted async smoke tests to pytest-compatible sync wrappers using `asyncio.run`.
- Added `test_api.py` for FastAPI health/session/module/save/prompt-preview endpoint coverage.
- Added mock-mode WebSocket turn coverage through FastAPI `TestClient`.
- Added `test_memory.py` for LanceDB memory add/search/future-turn filtering, rollback, and decay purge coverage.
- Added API coverage for save undo restoring prior turn state.
- Added WebSocket coverage for structured `llm_provider_unavailable` errors without saving failed turns.
- Kept direct script execution working for existing smoke tests.

Tasks:

- Continue converting scripts into automated tests where useful.
- Expand API tests for additional edge cases such as invalid save ids, inactive-save undo conflicts, and invalid prompt drafts.
- Add module registry tests.
- Add save lifecycle tests.
- Add frontend build checks.

Definition of done:

- Backend core can be validated with one command.
- Frontend can be validated with one command.
- Live LLM tests are clearly separated from deterministic tests.

## Deferred Until After Stabilization

Phase 7 should wait until the previous systems are reliable.

Deferred items:

- Event Bus (Phase 7).
- Slash command pre-router (Phase 7).
- Advanced module security / AST inspector (Phase 7).
- Multi-save frontend polish.

## Recommended Next Action

Start with Stabilization A, then immediately move into Stabilization B. Save-backed sessions are the biggest reliability gap because every other system depends on durable, isolated game state.
