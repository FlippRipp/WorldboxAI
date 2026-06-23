# WorldBox: Comprehensive Task List

This is the master task list for all remaining WorldBox work, ordered by priority. Each item includes a link to its detailed implementation plan.

Legend:
- `[X]` Complete
- `[~]` In progress
- `[ ]` Not started

---

## 1. Security Critical

### 1.1 Remove Committed API Key
**Priority: Immediate** | **Effort: 5 min**

The `backend/.env` file contains a live Gemini API key committed to the repository.

- [ ] Revoke the exposed API key in Google Cloud Console
- [ ] Generate a new key
- [ ] Verify `.env` is in `.gitignore`
- [ ] Create `.env.example` is the only committed env file

---

## 2. Stabilization D: LLM Pipeline Hardening

**Status: In Progress** | **Plan: [implementation/llm-pipeline-hardening.md](./implementation/llm-pipeline-hardening.md)**

- [X] `LLM_MODE=mock|live` support
- [X] Deterministic mock story, mutation, and embedding behavior
- [X] Reader JSON retry with fallback on malformed responses
- [X] `summarize_memory` and `score_memory_importance` LLM methods
- [X] Prompt block compiler and default pipeline
- [X] Save-backed prompt pipeline persistence
- [X] Prompt pipeline APIs (GET/PUT/preview)
- [X] `on_render_prompt_block` hooks
- [X] Frontend Prompt Studio (block editor, preview, trace)
- [X] Storyteller retry and fallback model support
- [X] Structured WebSocket error payloads
- [X] **D1: Structured output contracts for Librarian** (summaries, importance scores)
- [ ] **D2: Live model validation** at startup or `/api/health`
- [ ] **D3: Provider-specific message adaptation** for multi-system-message restrictions
- [X] **D4: Validation veto/rewrite loop** wired into LangGraph graph
- [X] **D5: Drag/drop ordering** in Prompt Studio
- [~] **D6: Mock mode exposed** in frontend health panel (panel exists; missing `llm_mode_note` and periodic polling)

---

## 3. Placeholder Module Removal

**Status: Complete**

- [X] Removed `core_dice`, `core_inventory`, `core_weather` placeholder modules
- [X] Fixed `test_save_manager.py` references
- [X] Fixed `garrick.json` test data

---

## 4. Module System: RPG Core

**Status: Complete** | **Docs: [modules/wb_core_rpg.md](./modules/wb_core_rpg.md)**

- [X] Created `wb_core_rpg` module: 6 stats (STR/DEX/CON/INT/WIS/CHA), open-ended AI skills
- [X] HP derived from CON + level, 3 progression systems (XP/Practice/Milestone)
- [X] Action feasibility via prompt injection, stat/skill improvement via Reader
- [X] Slash commands (`/stats`, `/skills`, `/level`), sidebar widget
- [X] AI practice detection via `sdk.llm.generate()`, model preference setting
- [X] Death/unconscious state, context-aware level-up, stat usage tracking
- [X] Per-module stat tier settings with widget_settings.jsx
- [X] Replaced `core_combat`, migrated all tests
- [X] Added `on_validate_output` hook (unconscious character veto)

---

## 5. Phase 7: Advanced SDK Features

**Status: In Progress (1/4 features done)** | **Plan: [implementation/phase7-sdk-features.md](./implementation/phase7-sdk-features.md)**

> Validation Veto (5.3) is fully implemented. Event Bus (5.1), Slash-Command Pre-Router (5.2), and AST Inspector (5.4) are not yet started.

### 5.1 Event Bus (Pub/Sub)
- [ ] Implement `sdk.events.on(event_name, callback)` 
- [ ] Implement `sdk.events.emit(event_name, payload)`
- [ ] Implement event dispatch in engine graph (after mutation, after turn)
- [ ] Add event registration in `on_init` hook
- [ ] Add engine-level event hooks (turn_start, turn_end, story_complete)
- [ ] Write tests for cross-module event communication

### 5.2 Slash-Command Pre-Router
- [ ] Implement command interception at Node 0 of LangGraph (before LLM)
- [ ] Add `commands` manifest field validation to ModuleRegistry
- [ ] Implement command parsing (extract command name + args from input)
- [ ] Implement command dispatch to module `backend.py` hooks
- [ ] Support `sdk.Signal.END_TURN` for LLM bypass
- [ ] Wire `sdk.ui.push_chat_message()` for system messages
- [ ] Write tests

### 5.3 Validation Veto & Rewrite Loop
- [X] Implement `on_validate_output` hook contract
- [X] Implement `sdk.ValidationVeto` exception class
- [X] Wire LangGraph conditional edge for veto detection
- [X] Implement retry loop (max 3 attempts) with veto reason injection
- [X] Implement graceful failure fallback after max retries
- [X] Write tests for veto-triggered rewrite behavior

### 5.4 AST Inspector (Module Security)
- [ ] Implement Python AST parser for `backend.py` files
- [ ] Define blocklist: `os`, `sys`, `subprocess`, `socket`, `requests`, `urllib`, `shutil`, `pathlib.Path.unlink`, `eval`, `exec`, `compile`, `__import__`
- [ ] Implement whitelist for safe imports (stdlib math, json, re, etc.)
- [ ] Integrate into ModuleRegistry loading pipeline
- [ ] Add user-facing security warning dialog
- [ ] Write tests for blocked and allowed imports

---

## 6. Technical Debt & Polish

**Status: Partially Complete** | **Plan: [implementation/technical-debt.md](./implementation/technical-debt.md)**

### 6.0 Module-Engine Decoupling
**Status: Not Started** | **Plan: [implementation/module-engine-decoupling.md](./implementation/module-engine-decoupling.md)** | **Priority: High**

- [ ] Remove hardcoded `wb_core_rpg` from `session.py` default module_data (lines 43-46, 91)
- [ ] Remove duplicate `DEFAULT_STAT_TIERS` and `_stat_tier_label` from `graph.py`
- [ ] Refactor `generate_intro()` to use `on_render_prompt_block` hooks
- [ ] Move `test_validation_veto.py` to `modules/wb_core_rpg/`
- [ ] Fix `test_module_contract.py` and `test_engine.py` module dependency
- [ ] Verify app starts, creates saves, and runs turns without `wb_core_rpg`

### 6.0b Server Architecture Refactor
**Status: Not Started** | **Plan: [implementation/server-architecture-refactor.md](./implementation/server-architecture-refactor.md)** | **Priority: High**

- [ ] Add `__init__.py` files to `backend/`, `backend/engine/`, `backend/sdk/`, `backend/api/`
- [ ] Create `backend/api/bootstrap.py` with `bootstrap_services()`
- [ ] Create FastAPI lifespan handler in `backend/api/app.py`
- [ ] Split `server.py` into routers (session, world, character, provider, prompt, module, memory, settings, health, websocket)
- [ ] Replace `global world_gen_state` with session-keyed `WorldGenSessions`
- [ ] Cache module_id -> directory mapping for widget routes

### 6.0c Code Quality Fixes
**Status: Not Started** | **Plan: [implementation/code-quality-review.md](./implementation/code-quality-review.md)** | **Priority: Medium**

- [ ] Replace `datetime.utcnow()` with `datetime.now(timezone.utc)` in `world_builder.py`
- [ ] Replace `print()` error handlers with `logger.exception()` in `graph.py`
- [ ] Fix linear scan in `memory.py` `get_memories_by_ids` to use LanceDB `.where()`
- [ ] Raise `RuntimeError` for embedding dimension mismatch instead of print warning
- [ ] Fix fragile snapshot filename parsing in `save_manager.py`
- [ ] Remove dummy `$t(` macro from `prompt_pipeline.py`
- [ ] Remove dead `set_world_index_path` from `graph.py`
- [ ] Extract mock location database from `world_builder.py` to fixture file

### 6.1 State Contract
- [ ] Replace `WorldState` TypedDict with Pydantic BaseModel
- [ ] Add runtime validation at API boundaries
- [ ] Ensure save/load round-trips preserve complete state contract
- [ ] Add state fixture factory for tests

### 6.2 Frontend Quality (UI System Overhaul)
**Plan: [implementation/ui-system-overhaul.md](./implementation/ui-system-overhaul.md)** | **Status: Complete**

- [X] Sprint 1: Deleted dead code (`App.css`, unused assets), fixed HTML title, Vite proxy + relative URLs
- [X] Sprint 1: Created `lib/api.js`, `WidgetErrorBoundary`, `SkeletonLoader`, compiled widget cache
- [X] Sprint 2: Extracted `useWebSocket`, `useSession`, `useModules`, `usePromptPipeline` hooks
- [X] Sprint 2: Created `ChatFeed`, `ChatInput`, `ChatMessage`, `StreamingMessage`, `MarkdownRenderer`
- [X] Sprint 2: Created `Header`, `ConnectionStatus`, `Sidebar`, `SlotRenderer`
- [X] Sprint 2: Thinned `App.jsx` to orchestrator
- [X] Sprint 3: Mobile sidebar drawer, connection status banner, markdown rendering
- [X] Sprint 3: ARIA labels, roles on modals and inputs
- [X] Sprint 4: Module Event Bus React Context, fixed widget state consumption
- [X] Sprint 4: Drag/drop ordering in PromptStudio, unsaved changes warning

### 6.3 Backend Quality
- [ ] Add stricter frontend linting pass
- [ ] Add embedding dimension migration path
- [ ] Handle LanceDB table loading with stale/incompatible indices
- [ ] Add API endpoints for additional edge cases (invalid save IDs, inactive-save undo)

### 6.4 Module Contract Polish
- [X] Added `on_validate_output` contract (Validation Veto wired)
- [ ] Add `commands` manifest validation (prerequisite for slash commands)
- [X] Make backend modules actively consume `state["module_configs"]`

---

## 7. Multi-Save & Multi-Session

**Status: Not Started** | **Plan: [implementation/multi-save-session.md](./implementation/multi-save-session.md)**

- [ ] Remove remaining single-session assumptions in engine
- [ ] Design concurrency model (one active save per session? multiple?)
- [ ] Implement session-scoped LanceDB paths
- [ ] Implement `.wbx` packing with live LanceDB files
- [ ] Multi-save frontend polish (save browser, metadata display)
- [~] Add save metadata (created_at, last_played missing; playtime and turn count implemented)

---

## 8. Testing Expansion

### 7.1 Backend Tests
- [ ] Module registry validation edge cases
- [ ] Invalid save lifecycle error paths
- [ ] Memory rollback with active LanceDB state
- [ ] Prompt pipeline edge cases (malformed blocks, missing text)
- [ ] Extended API error coverage

### 7.2 Integration Tests
- [ ] Full turn execution with mock LLM + real module dispatch
- [ ] Save/load cycle with module state preservation
- [ ] Undo with chat message consistency

### 7.3 Frontend Tests
- [ ] Widget loader error display verification
- [ ] Settings schema rendering correctness
- [ ] Chat input and WebSocket reconnection behavior

---

## 9. World Building System

**Status: Not Started** | **Plan: [systems/world-building.md](./systems/world-building.md)**

### Phase 1: Core Cascade Engine
- [ ] World generation orchestrator (`backend/engine/world_builder.py`)
- [ ] Stage 1: World Rules generation (structured output with Pydantic schema)
- [ ] Stage 2: Overarching Lore generation
- [ ] Stage 3: Regions & Geography generation
- [ ] Stage 4: Factions & Powers generation
- [ ] Stage 5: Key Characters generation
- [ ] Cascade state management (current stage, locked stages, user notes)
- [ ] World data persistence to `World/` directory in save workspace
- [ ] RAG embedding of all world entries
- [ ] Backend API: `POST /api/world/generate/stage/{n}`, `GET /api/world/status`, `PUT`/`POST`/`DELETE` stage
- [ ] Integration with GameSessionManager (detect world data, inject into initial state)

### Phase 2: Module Extension Hooks
- [ ] `on_world_rules_schema` hook — modules add fields to rules schema
- [ ] `on_world_rules_generate` hook — modules generate their rules section
- [ ] `on_region_generate` hook — modules add per-region data
- [ ] `on_faction_generate` hook — modules add per-faction data
- [ ] `on_character_generate` hook — modules add per-character data
- [ ] `on_world_compiled` hook — cross-reference validation and finalization

### Phase 3: Gameplay Integration
- [ ] World embeddings stored in LanceDB under world namespace
- [ ] Context injection in `gather_context_node` (region, factions, lore)
- [ ] Player location tracking in WorldState
- [ ] Region transition detection via Reader LLM
- [ ] World prompt blocks (auto-generated from world data)

### Phase 4: Frontend Wizard
- [ ] WorldBuilder component (multi-step wizard with stage navigation)
- [ ] Per-stage renderers: RulesForm, LoreEditor, RegionGrid, FactionList, CharacterCards
- [ ] Inline editing for all generated fields
- [ ] Re-roll and approve buttons per stage
- [ ] User note input for directing next stage generation
- [ ] Stage history breadcrumbs with go-back capability
- [ ] Preview + finalize world overview before committing
- [ ] Integration with App.jsx (world builder replaces autosave on new game)

---

## Priority Order (Recommended Execution)

1. **Immediate**: Remove committed API key (1.1)
2. **High**: Module-Engine Decoupling (6.0) -- hardcoded module references in engine
3. **High**: Server Architecture Refactor (6.0b) -- monolithic server split, DI, global state
4. **High**: World Building System — Phase 1 Core Cascade Engine (9.1)
5. **High**: LLM Pipeline Hardening remaining items (2.D1-D3, D6)
6. **High**: Code Quality Fixes (6.0c) -- error handling, dim mismatch, query perf
7. **High**: Module Contract Polish — `commands` validation (6.4)
8. **Medium**: World Building — Phase 2 Module Hooks + Phase 3 Gameplay Integration (9.2-9.3)
9. **Medium**: World Building — Phase 4 Frontend Wizard (9.4)
10. **Medium**: Phase 7 SDK Features — Event Bus, Slash Commands (5.1-5.2)
11. **Medium**: Technical Debt & Polish (6.1, 6.3)
12. **Medium**: AST Inspector Security (5.4)
13. **Medium**: Multi-Save & Multi-Session (7)
14. **Low**: Testing Expansion (8)

---

## Completed Milestones

<details>
<summary>Phase 1: The Naked Engine</summary>

- [X] WorldState TypedDict schema
- [X] ModuleRegistry (scan modules/, load manifest.json + backend.py)
- [X] WorldBoxSDK mock object
- [X] LangGraph DAG: gather -> storyteller -> reader -> librarian
</details>

<details>
<summary>Phase 2: API & Communication Layer</summary>

- [X] FastAPI app initialization
- [X] Dynamic static file mounting for module `/__assets__/`
- [X] WebSocket `/ws/chat` endpoint
- [X] Module metadata and widget endpoints
</details>

<details>
<summary>Phase 3: Multi-Agent AI Core</summary>

- [X] LiteLLM integration and configuration
- [X] Storyteller agent (streaming LLM prose via WebSocket)
- [X] Reader agent (post-story JSON mutation extraction)
- [X] Prompt block compiler and assembly
</details>

<details>
<summary>Phase 4: Save System</summary>

- [X] Template management (.wbp player templates loaded and saved; .wbs scenario templates not yet implemented)
- [X] Instance generation (.wbx save creation)
- [X] Rolling snapshots (last 10 turns, auto-save)
- [X] Undo system (restore state from snapshots)
</details>

<details>
<summary>Phase 5: Frontend Shell</summary>

- [X] React + Vite + TailwindCSS base layout
- [X] Chat window and input bar
- [X] WebSocket client with streaming text rendering
- [X] Dynamic widget loader (module widget.jsx -> slot injection)
- [X] Auto-generated settings UI from manifest settings_schema
</details>

<details>
<summary>Phase 6: RAG Memory Layer</summary>

- [X] LanceDB vector store integration
- [X] Librarian agent (summarize, score importance, store)
- [X] Recency bias (turn_generated timestamps)
- [X] Memory fading and decay-based purging
- [X] Context injection into gather node
</details>

<details>
<summary>Stabilization A: Project Hygiene</summary>

- [X] requirements.txt
- [X] pytest + pytest.ini
- [X] backend/.env.example
- [X] SETUP.md
- [X] GET /api/health
- [X] start.bat preflight checks
</details>

<details>
<summary>Stabilization B: Save-Backed Sessions</summary>

- [X] GameSessionManager
- [X] Default autosave creation/loading
- [X] Turn execution against session state
- [X] Save every completed turn
- [X] GET /api/session
- [X] Save management APIs (list/create/load/undo)
- [X] LanceDB memory tied to active save
- [X] Undo with memory rollback
- [X] Frontend save panel
- [X] Module config persistence (Core/module_configs.json)
- [X] Module config APIs (GET/PUT)
- [X] Settings modal backed by API
- [X] Chat message persistence (Core/chat_messages.json)
</details>

<details>
<summary>Stabilization C: Module Contract</summary>

- [X] Manifest validation in ModuleRegistry
- [X] mutation_schema manifest field support
- [X] core_combat.on_mutate_state ownership
- [X] Reader schema built from module manifests
- [X] Reader mutation dispatch to module hooks
- [X] Dependency sorting / topological load order
- [X] Module contract tests
- [X] MODULES.md contract documentation
- [X] prompt_blocks manifest validation
- [X] core_combat.on_render_prompt_block
</details>

<details>
<summary>Stabilization D: LLM Pipeline Hardening (Partial)</summary>

- [X] LLM_MODE mock/live
- [X] Deterministic mock behavior
- [X] Reader JSON retry + fallback
- [X] Role-specific Librarian methods
- [X] Mock engine turn tests
- [X] PromptCompiler + default pipeline
- [X] Save-backed prompt pipeline
- [X] Prompt pipeline APIs
- [X] on_render_prompt_block hooks
- [X] Prompt Studio frontend
- [X] PROMPTS.md documentation
- [X] Storyteller retry + fallback
- [X] Structured WebSocket errors
</details>

<details>
<summary>Stabilization E: Automated Testing</summary>

- [X] pytest + pytest.ini setup
- [X] test_api.py (health, session, module, save, prompt endpoints)
- [X] test_memory.py (add/search/rollback/purge)
- [X] test_engine_mock.py (mock mode graph turns)
- [X] test_module_contract.py (dependency ordering, mutation dispatch)
- [X] test_prompt_pipeline.py (compilation, depth, veto, preview)
- [X] test_save_manager.py (save lifecycle)
- [X] test_session_manager.py (session persistence)
- [X] WebSocket error payload tests
- [X] Save undo state restoration tests
</details>
