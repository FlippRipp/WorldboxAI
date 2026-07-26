# WorldBox Module Contract

This document describes the current module contract used by `ModuleRegistry`, `EngineGraph`, the FastAPI module endpoints, and the React dynamic widget loader.

Modules live under `modules/<module_folder>/` and must include:

- `manifest.json`
- `backend.py`

Optional frontend files such as `widget.jsx` and assets may also be placed in the module folder.

## Manifest Fields

Required fields:

- `id`: Lowercase snake_case module id starting with a letter. Example: `wb_core_rpg`.
- `name`: Human-readable module name.
- `version`: Module version string.
- `consumes`: Data flow contract declaring what state this module reads.
- `produces`: Data flow contract declaring what state this module writes.

Optional fields:

- `dependencies`: List of module ids that must load before this module.
- `ui_slots`: List of frontend slots where `widget.jsx` may render.
- `settings_schema`: Settings rendered by the frontend settings modal and persisted into save state.
- `mutation_schema`: Per-module schema sent to the Reader LLM for state mutation extraction.
- `dedicated_reader`: Boolean. When true, this module's mutation schema is extracted by its own Reader LLM call (run concurrently with the shared one) instead of the combined call. The dedicated call also receives the player's declared action and the module's optional `on_reader_context` block as context.
- `prompt_blocks`: Prompt blocks injected into the Storyteller prompt pipeline.
- `commands`: Slash command routing table.
- `modes`: Alternative game mode definitions.
- `character_creation`: Per-module character creation defaults and schema.

## Data Flow Contract (`consumes` / `produces`)

**These fields are mandatory.** They declare exactly what data a module reads and writes, enabling dependency-aware parallel execution. A module always implicitly gets its own `module_data.<self>` and `module_configs.<self>`.

### `consumes`

Declares what state data this module reads. The engine builds a scoped state dict containing only the declared keys before calling module hooks.

| Key | Type | Meaning |
|-----|------|---------|
| `state` | `["key1", "key2"]` or `"*"` | Top-level state keys this module needs. Valid keys: `input_text`, `turn`, `history`, `chat_messages`, `world_id`, `player_location_node_id`, `player_location_region`, `player_location_layer_id`, `revealed_node_ids`, `current_context`, `prompt_pipeline`, `last_prompt_trace`, `needs_rewrite`, `veto_retries`, `veto_reason`, `active_save_id`. |
| `module_data` | `["mod_id1"]` or `"*"` | Which OTHER modules' data this module reads. Implicitly always gets its own data. |
| `module_configs` | `["mod_id1"]` or `"*"` | Which OTHER modules' configs this module reads. Implicitly always gets its own config. |
| `world_data` | `boolean` | Whether this module needs compiled world data (rules, lore, regions, map). |

### `produces`

Declares what output types this module writes. The engine only stores keys declared here from the module's hook return value.

| Key | Type | Meaning |
|-----|------|---------|
| `module_data` | `boolean` | Does this module write module_data (e.g., hp, stats, xp)? |
| `context_string` | `boolean` | Does this module produce context text blocks for Storyteller? |
| `messages` | `boolean` | Does this module produce chat messages (e.g., slash command output)? |

### Example

```json
{
  "consumes": {
    "state": ["input_text", "turn", "history"],
    "module_data": [],
    "module_configs": [],
    "world_data": false
  },
  "produces": {
    "module_data": true,
    "context_string": true,
    "messages": true
  }
}
```

A module with `consumes.module_data: []` only sees its own `module_data`. A module with `consumes.module_data: ["*"]` sees every other module's data.

## Parallel Execution Model

The engine runs module hooks in **dependency-aware parallel levels** using data flow contracts:

```
Level 0:  [Core_RPG] [Weather] [Lore_Generator]     ← independent modules, run in parallel
Level 1:  [Combat_System]                              ← depends on Core_RPG
Level 2:  [Narrator]                                   ← depends on Core_RPG + Weather
```

Between levels, module_data from completed levels is merged into the accumulated state. Modules at the same level see identical pre-level state and never observe each other's outputs.

The `dependencies` manifest field controls execution order within the pipeline. Two modules with no dependency relationship can (and will) execute in parallel if they are in the same level.

**Affected hooks**: `on_gather_context`, `on_mutate_state`, `on_render_prompt_block`, and `on_validate_output` all use this leveled parallel dispatch.

## Currently allowed `ui_slots` values:

- `slot_sidebar`
- `slot_header`
- `slot_chat_feed`
- `slot_modal`
- `slot_tab`

## Currently allowed setting types:

- `slider`: Requires numeric `min`, `max`, and `default`.
- `toggle`: Requires boolean `default`.
- `select`: Requires `options` array.
- `text`: Arbitrary string value.

## Example manifest:

```json
{
  "id": "wb_core_rpg",
  "name": "Core RPG System",
  "version": "1.2.0",
  "dependencies": [],
  "consumes": {
    "state": ["input_text", "turn", "history"],
    "module_data": [],
    "module_configs": [],
    "world_data": false
  },
  "produces": {
    "module_data": true,
    "context_string": true,
    "messages": true
  },
  "ui_slots": ["slot_sidebar"],
  "settings_schema": {
    "progression_system": {
      "type": "select",
      "options": [
        {"value": "xp", "label": "XP-Based"},
        {"value": "practice", "label": "Practice-Based"}
      ],
      "default": "xp"
    }
  },
  "mutation_schema": {
    "hp_change": "integer, HP gained or lost by the player. Use 0 when unchanged."
  },
  "prompt_blocks": [
    {
      "id": "character_sheet",
      "type": "module_prompt",
      "enabled": true,
      "role_type": "system",
      "placement": "system_relative",
      "config": {}
    }
  ]
}
```

Allowed prompt block types in module manifests:

- `static_text`: Requires `config.text`.
- `module_prompt`: Rendered by `on_render_prompt_block` before Storyteller compilation.

Module manifests cannot declare `engine_context`; that block is reserved for the engine.

## App-Wide Module Management

The main menu's **Modules** screen manages the module set for the whole app,
on top of the per-story `__active_modules__` toggles:

- **Enable / disable app-wide.** A disabled module is removed from the live
  registry immediately: it vanishes from `GET /api/modules`, the story start
  screen's module toggles, and engine dispatch — even for saves whose
  `__active_modules__` lists it. Per-story config is untouched, so re-enabling
  restores the previous behavior. Disabling is refused while an enabled module
  depends on the target (disable dependents first); enabling is refused until
  all dependencies are enabled.
- **Install** from an uploaded `.zip` or a GitHub repository URL
  (`https://github.com/user/repo`, optionally a `/tree/branch[/path]` link).
  The archive is searched for a folder containing `manifest.json` +
  `backend.py`, the manifest is validated with the standard registry rules,
  and the module folder is copied into `modules/<id>` and hot-loaded. Installs
  that fail validation or fail to import roll back completely. Note that a
  module's `backend.py` runs with the app's full permissions — install only
  trusted code.
- **Update** replaces an installed module's folder with a fresh copy and
  hot-reloads its backend. GitHub-installed modules re-download from their
  recorded URL (per-module button, and the **Update all** button covers every
  GitHub-sourced module at once — zip-installed ones are reported as skipped);
  zip-installed modules update by uploading a new zip. The old version is kept
  as a backup until the new backend imports cleanly, so a broken update rolls
  back to the previous working files. The new archive's module id must match.
  A module's HTTP routes (module-owned routers) keep serving the pre-update
  handlers until the next restart; engine hooks and disk-served `.jsx`
  widgets refresh immediately.
- **Remove** deletes a manager-installed module's folder from disk. Built-in
  modules (shipped in the repo) can only be disabled, never removed.

State lives in `data/modules_state.json` (`disabled` + `installed` module id
lists, plus per-module install `sources` used for updates), which is
app-global and shared across profiles. Hot changes apply to engine dispatch
immediately; a disabled module's already-mounted HTTP routes remain until the
next restart (the UI no longer links to them).

Manager endpoints: `GET /api/module-manager`,
`PUT /api/module-manager/{mod_id}/enabled`, `POST /api/module-manager/install`,
`POST /api/module-manager/{mod_id}/update`,
`POST /api/module-manager/update-all`, `DELETE /api/module-manager/{mod_id}`.

## Load Order

`ModuleRegistry.load_all_modules()` performs three phases:

1. Discover module folders with both `manifest.json` and `backend.py`.
2. Validate manifests (including `consumes`/`produces`) and resolve dependency order.
3. Import backend modules in topological dependency order.

Modules are skipped if:

- The manifest cannot be parsed.
- Required manifest fields are missing or invalid (including missing `consumes`/`produces`).
- The module id duplicates another valid module.
- A declared dependency is missing.
- A dependency cycle is detected.

## Backend Hooks

Backend hooks are module-level async functions in `backend.py`. Hooks are optional unless a module needs that behavior. All hooks receive a **filtered state dict** scoped to the module's `consumes` declaration.

```python
async def on_gather_context(state: dict, sdk) -> dict | None:
    ...
```

Called before storytelling. Runs in dependency-leveled parallel. Return a dict -- only keys matching `produces` are stored.

```python
async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict | None:
    ...
```

Called after the Reader LLM extracts mutations. `mutation` is this module's extracted mutation object from `mutation_schema`. Runs in dependency-leveled parallel with state merging between levels. Return a partial state update.

```python
async def on_reader_context(state: dict, sdk) -> str | None:
    ...
```

Called when the module is flagged `dedicated_reader` in its manifest, before its dedicated Reader extraction call. Return a text block (extraction guidance, pre-turn world state) injected into that call's prompt as context ahead of the story text.

```python
async def on_render_prompt_block(block: dict, state: dict, sdk) -> str | dict | None:
    ...
```

Called for each `module_prompt` entry in the module manifest before Storyteller prompt compilation. `block` is the manifest block with its local id. All modules' blocks render in parallel. Return a string, `{"content": "..."}`, or `{"text": "..."}`.

```python
async def on_validate_output(llm_output: str, state: dict, sdk) -> None:
    ...
```

Called after the Storyteller generates narrative output. Runs in parallel across all modules. Raise `sdk.ValidationVeto(reason)` to trigger a rewrite. All vetos are collected; if any module vetos, all reasons are injected into the rewrite prompt.

```python
async def on_character_get_defaults(state: dict, world_context: dict) -> dict | None:
    ...
```

Called during character creation. Return a dict of default module_data values.

```python
async def on_command_<name>(args: list[str], state: dict, sdk) -> dict:
    ...
```

Called when the player runs a slash command declared in the manifest's `commands` table. Return `{"message": "..."}` — surfaced to the player as an ephemeral popup, never written into the transcript. Optional keys: `module_data` / `module_data_replace` / `character_update` writebacks, and `error: True` to mark the command as failed. Commands dispatched by module UI buttons (`source: "button"` on the wire) skip the popup on success — the widget already reflects the outcome via `state_update` — but an `error: True` result (or a raised exception) always pops up.

## Turn Lifecycle & Streaming Hooks

Three optional hooks let a module observe a turn as it runs (live trigger
scanning, TTS, haptics, translation). All three respect `__active_modules__`
gating and receive the usual filtered state view.

```python
async def on_turn_start(state: dict, sdk) -> None:
    ...
```

Fired at the turn entry point, before the first streamed token. Plain `def` is
also accepted.

```python
def on_stream_token(token: str, state: dict, sdk) -> None:
    ...
```

Called once per token the storyteller streams, in stream order. **Must be a
plain sync function** — this is the one deliberate exception to the async-hook
convention. The token path is hot: awaiting per token would stall streaming, so
an `async def` registered under this name is skipped with a logged warning, not
awaited. Keep the body O(token) and non-blocking; hand anything slower to a
queue or background task. `state` is the module's filtered view built **once at
turn start** and reused for every token of the turn — it does not update
mid-turn. Exceptions are swallowed and never reach the streaming path or other
subscribers; they are logged at most once per module per turn.

```python
async def on_turn_stopped(state: dict, sdk, reason: str) -> None:
    ...
```

Fired when the turn ends. Plain `def` is also accepted. `reason` is:

- `"completed"` — the turn finished and was saved;
- `"cancelled"` — the player pressed Stop mid-generation;
- `"error"` — the turn died on an exception before completing.

**Guarantees.** For every turn that fired `on_turn_start`, `on_turn_stopped`
fires exactly once — including cancelled and failed turns. Per turn, per
module, the order is always `on_turn_start` → zero or more `on_stream_token`
(in stream order) → `on_turn_stopped`; no token is delivered after
`on_turn_stopped`. Veto rewrites don't re-stream (matching the UI), so a
vetoed turn's tokens come from the first attempt only. The opening-scene
generation (intro) is not a turn and fires none of these hooks. Hook
exceptions are logged and isolated; they never affect the turn or other
modules.

## Feature Detection

`EngineGraph` exposes the capability set

```python
MODULE_API_FEATURES = {"stream_tokens", "turn_lifecycle"}
```

Modules probe it with a safe `getattr` (older builds lack the attribute):

```python
features = getattr(services["engine"], "MODULE_API_FEATURES", set())
if "stream_tokens" in features:
    ...
```

Each capability has its own name so future additions stay independently
detectable.

## State Access

Modules receive a **filtered state dict** that only includes fields declared in `consumes`. Always present:

- `active_save_id`
- `turn`
- Your own `module_data.<self>`
- Your own `module_configs.<self>`
- Your own `module_instructions` (only when the story has instruction overrides for you — see below)

Additional state fields, other modules' data, other modules' configs, and world data are only present if declared in `consumes`.

Modules should treat `state` as read-only unless returning a partial update from a mutation hook.

## Customizable Instruction Slots

A module whose prompts should be customizable per scenario/story can expose
**instruction slots** by defining a module-level function in its backend:

```python
def get_instruction_slots() -> list[dict]:
    return [
        {"id": "slot_id", "label": "Shown in the UI",
         "description": "What this instruction steers.",
         "default": "The built-in directive text."},
    ]
```

The host then:

- Reports `has_instruction_slots: true` for the module in `GET /api/modules` and
  serves the slots at `GET /api/modules/{mod_id}/instruction-slots`.
- Stores per-story overrides under the reserved
  `module_configs["__module_instructions__"]` key, shaped
  `{mod_id: {slot_id: text}}` (editable via
  `GET/PUT /api/saves/{save_id}/module-instructions`). Scenarios carry the same
  shape in their `module_instructions` field and seed new saves with it.
- Injects your own overrides into hook state as
  `state["module_instructions"]` (`{slot_id: text}`, no `consumes` declaration
  needed; absent when the story has none). Router code can read the reserved
  key from `sm.state` directly.
- Offers a generic LLM rewrite at
  `POST /api/modules/{mod_id}/instructions/{slot_id}/rewrite` that adapts the
  slot's default text to a player request.

Keep slot text purely *creative* direction: output formats, JSON contracts,
and exact counts belong in the fixed parts of your prompts so a custom
instruction can never break parsing. An empty or missing override always means
"use the default". See `wb_core_rpg` for a full implementation.
