# Implementation Plan: Code Quality Review Findings

## Overview

A full audit of the codebase identified several code quality issues ranging from deprecated API usage to error handling gaps and performance concerns. None are blockers individually, but collectively they erode maintainability.

---

## Finding Index

| # | Finding | Location | Severity | Effort |
|---|---------|----------|----------|--------|
| 1 | `datetime.utcnow()` deprecated | `world_builder.py:355` | Medium | 1 min |
| 2 | Dead `set_world_index_path` method | `graph.py:110-111` | Low | 1 min |
| 3 | Linear scan in `get_memories_by_ids` | `memory.py:117-126` | Medium | 15 min |
| 4 | Widget route re-parses manifests each request | `server.py:251-280` | Medium | 15 min |
| 5 | Fragile snapshot filename parsing | `save_manager.py:261` | Medium | 5 min |
| 6 | Error swallowing without tracebacks | `graph.py:146,313,445,478` | Medium | 10 min |
| 7 | Dummy `$t(` macro in `resolve_macros` | `prompt_pipeline.py:124` | Low | 1 min |
| 8 | LLMBridge stale env var reads | `llm_bridge.py:10-21` | Low | 10 min |
| 9 | Hardcoded mock location database | `world_builder.py:880-955` | Low | 20 min |
| 10 | Dimension mismatch is only a warning | `memory.py:48-49` | Medium | 15 min |
| 11 | Inline `import json as _json` in methods | `server.py:553`, `session.py:154` | Low | 2 min |
| 12 | Missing type hints on module SDK interfaces | Multiple files | Low | 30 min |

---

## Finding 1: `datetime.utcnow()` Deprecated

### Location

`backend/engine/world_builder.py` line 355.

### Code

```python
"created_at": datetime.utcnow().isoformat() + "Z",
```

### Issue

`datetime.utcnow()` is deprecated in Python 3.12+. It returns a naive datetime that is treated as UTC, which is ambiguous.

### Fix

```python
"created_at": datetime.now(timezone.utc).isoformat(),
```

`datetime.now(timezone.utc)` is already imported at line 6 of `world_builder.py` (unused), so no new import needed.

---

## Finding 2: Dead `set_world_index_path` Method

### Location

`backend/engine/graph.py` lines 110-111.

### Code

```python
def set_world_index_path(self, world_index_path: str):
    pass
```

### Issue

This is a completely dead method -- a no-op. If it's meant to be a placeholder, it should raise `NotImplementedError` or include a TODO comment. As-is, calling it silently does nothing, which is misleading.

### Fix

Either implement it or remove it. If it's planned for future use:

```python
def set_world_index_path(self, world_index_path: str):
    """TODO: Implement world index path switching for multi-save support."""
    raise NotImplementedError("set_world_index_path is not yet implemented")
```

---

## Finding 3: Linear Scan in `get_memories_by_ids`

### Location

`backend/engine/memory.py` lines 117-126.

### Code

```python
def get_memories_by_ids(self, memory_ids: list[str]) -> list[dict]:
    if self.table.count_rows() == 0 or not memory_ids:
        return []
    results = []
    for row in self.table.search().limit(200).to_list():
        if row.get("id") in memory_ids:
            results.append(self._format_memory_row(row))
        if len(results) >= len(memory_ids):
            break
    return results
```

### Issue

This fetches up to 200 rows from LanceDB and does a Python-level linear scan with `in` membership checks. As memory grows past 200 entries, older but active memories may not be found because only the first 200 rows are scanned (no ordering guarantee).

### Fix

Use LanceDB's native filtering:

```python
def get_memories_by_ids(self, memory_ids: list[str]) -> list[dict]:
    if self.table.count_rows() == 0 or not memory_ids:
        return []
    ids_filter = " OR ".join(f"id = '{mid}'" for mid in memory_ids)
    rows = self.table.search().where(ids_filter).limit(len(memory_ids)).to_list()
    return [self._format_memory_row(row) for row in rows]
```

LanceDB supports compound `WHERE` clauses, so this is both more correct and more performant.

---

## Finding 4: Widget Route Re-parses Manifests Each Request

### Location

`backend/api/server.py` lines 251-280, function `get_module_jsx`.

### Code

```python
@app.get("/widgets/{mod_id}/{filename:path}")
async def get_module_jsx(mod_id: str, filename: str):
    for item in os.listdir(modules_dir):
        candidate = os.path.join(modules_dir, item)
        if not os.path.isdir(candidate):
            continue
        manifest_path = os.path.join(candidate, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = __import__("json").load(f)
            # ... check manifest["id"] == mod_id ...
        # Also try folder-name matching
        if os.path.basename(candidate) == mod_id or ...:
            ...
```

### Issue

On every widget file request, this function:
1. Lists the modules directory.
2. Opens and parses every `manifest.json`.
3. Falls through to folder-name matching.

This is O(modules) file I/O on every request. With 10+ modules, that's 10+ file reads per widget load.

### Fix

Build a `module_id -> path` mapping at startup:

```python
# In bootstrap:
widget_paths: dict[str, str] = {}
for mod_id, mod_data in registry.get_modules().items():
    widget_paths[mod_id] = mod_data["path"]

# In route:
@router.get("/widgets/{mod_id}/{filename:path}")
async def get_module_jsx(mod_id: str, filename: str):
    mod_path = widget_paths.get(mod_id)
    if not mod_path:
        raise HTTPException(status_code=404)
    file_path = os.path.join(mod_path, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404)
    return FileResponse(file_path, ...)
```

The dynamic mounts in lines 219-248 can also be simplified using this pre-built mapping.

---

## Finding 5: Fragile Snapshot Filename Parsing

### Location

`backend/engine/save_manager.py` line 261.

### Code

```python
snaps = sorted(list((save_path / "Snapshots").glob("turn_*.zip")),
              key=lambda x: int(x.stem.split("_")[1]))
```

### Issue

`int(x.stem.split("_")[1])` assumes every file matching `turn_*.zip` has a stem that splits into exactly two parts, with the second being a valid integer. If a file like `turn_backup.zip` or `turn_corrupted.zip` exists, the code crashes with `ValueError` or `IndexError`.

### Fix

```python
def _snapshot_turn_number(path: Path) -> int:
    """Extract turn number from turn_N.zip, return -1 for unparseable names."""
    try:
        return int(path.stem.split("_", 1)[1])
    except (ValueError, IndexError):
        return -1

snaps = sorted(
    [p for p in (save_path / "Snapshots").glob("turn_*.zip") if _snapshot_turn_number(p) >= 0],
    key=_snapshot_turn_number,
)
```

---

## Finding 6: Error Swallowing Without Tracebacks

### Location

`backend/engine/graph.py` lines 146, 313, 445, 478.

### Code

```python
except Exception as e:
    print(f"Error in {mod_id} gather_context: {e}")
```

### Issue

When a module hook raises an exception, only the exception message is printed to stdout -- no stack trace, no logging, no structured error. This makes debugging module issues very difficult, especially in production where stdout may not be captured.

### Fix

Use `logging.exception()` which includes the full traceback:

```python
import logging
logger = logging.getLogger(__name__)

# In each exception handler:
except Exception as e:
    logger.exception(f"Error in {mod_id} gather_context")
```

This logs the full stack trace at ERROR level. Affected locations:
- `graph.py:146` -- `initialize_module_data`
- `graph.py:313` -- `gather_context_node`
- `graph.py:445` -- `storyteller_node` (validate_output dispatch)
- `graph.py:478` -- `reader_node` (on_mutate_state dispatch)

---

## Finding 7: Dummy `$t(` Macro in `resolve_macros`

### Location

`backend/engine/prompt_pipeline.py` line 124.

### Code

```python
"$t(": "<invalid_macro>$t(",
```

### Issue

This substitution maps the string `$t(` to `<invalid_macro>$t(`. The `$t(` pattern does not match the `r"\$\{[a-z_]+\}"` regex used in the replacer function, so this entry in the `substitutions` dict is never actually reached. It appears to be a leftover from a planned `$template()` macro that was never implemented.

### Fix

Remove the line. If a `$template()` macro is planned, add a TODO comment instead:

```python
# TODO: Implement $template(name) macro for reusable prompt fragments
```

---

## Finding 8: LLMBridge Stale Env Var Reads

### Location

`backend/sdk/llm_bridge.py` lines 10-21.

### Code

```python
class LLMBridge:
    def __init__(self):
        self._service = None
        self._storyteller_model = os.getenv("STORYTELLER_MODEL", "gemini/gemini-2.5-flash")
        self._reader_model = os.getenv("READER_MODEL", "gemini/gemini-2.5-flash")
        self._fast_model = os.getenv("MODULE_FAST_MODEL", self._reader_model)
        self._mode = os.getenv("LLM_MODE", "live").strip().lower()

    def _set_service(self, service):
        self._service = service
        self._storyteller_model = service.storyteller_model
        self._reader_model = service.reader_model
        self._fast_model = os.getenv("MODULE_FAST_MODEL", self._reader_model)
        self._mode = service.mode
```

### Issue

The pattern is: init reads env vars, `_set_service` overwrites with service values, but `_fast_model` re-reads the env var on every `_set_service` call. If a module calls `sdk.llm.generate()` before `_set_service` runs, it uses env-based defaults. After `_set_service`, it uses the LLMService values. In practice this works because the SDK is wired during engine init before any module hooks fire, but the design is fragile.

### Fix

Make the initialization explicit -- don't read env vars at init time, only read them if no service has been set:

```python
class LLMBridge:
    def __init__(self):
        self._service = None

    def _set_service(self, service):
        self._service = service

    def _pick_model(self, preference: str) -> str:
        if self._service is None:
            # Fallback: read from env
            default = os.getenv("READER_MODEL", "gemini/gemini-2.5-flash")
            if preference == "fastest":
                return os.getenv("MODULE_FAST_MODEL", default)
            elif preference == "smartest":
                return os.getenv("STORYTELLER_MODEL", default)
            return default
        # Service is set: use it directly
        if preference == "fastest":
            return self._service.module_fast_model
        elif preference == "smartest":
            return self._service.storyteller_model
        return self._service.reader_model
```

---

## Finding 9: Hardcoded Mock Location Database

### Location

`backend/engine/world_builder.py` lines 880-955, method `_mock_single_label`.

### Code

A 75-line inline dictionary of location names organized by `(node_type, layer_type)` tuples. Contains ~50 hardcoded location names like "Stonebridge", "Ironhearth", "Frostfang Tor", etc.

### Issue

- Makes `world_builder.py` unnecessarily large.
- Changing mock world data requires searching through the main source file.
- The mock data doesn't reference the actual seed prompt -- it ignores the user's world theme.

### Fix

Extract to a separate fixture file:

```python
# backend/engine/mock_world_data.py
LOCATION_NAMES = {
    ("settlement", "surface"): [
        ("Stonebridge", "Dusty caravan stop on the old imperial road"),
        # ...
    ],
    # ...
}
```

Then import in `world_builder.py`:

```python
from backend.engine.mock_world_data import LOCATION_NAMES
```

---

## Finding 10: Dimension Mismatch Is Only a Warning

### Location

`backend/engine/memory.py` lines 47-48.

### Code

```python
if actual_dim != embedding_dim:
    print(f"WARNING: Database vector dimension ({actual_dim}) does not match current LLM dimension ({embedding_dim}). This may cause errors.")
```

### Issue

If the embedding model changes (e.g., from 768-dim to 1536-dim), LanceDB silently returns garbage or crashes with opaque errors on query. A print warning is insufficient -- the user will not see it unless they watch stdout.

### Fix

Raise an exception with a clear error message and recovery instructions:

```python
if actual_dim != embedding_dim:
    raise RuntimeError(
        f"Vector dimension mismatch: database has {actual_dim}-dim vectors "
        f"but the current embedding model produces {embedding_dim}-dim vectors. "
        f"This occurs when switching between embedding models (e.g., Gemini to OpenAI). "
        f"To fix: delete the vector_index directory at '{db_path}' and restart. "
        f"Memories will be regenerated from saved turn history."
    )
```

Additionally, store the embedding model name in LanceDB table metadata so the health endpoint can report it.

---

## Finding 11: Inline `import json as _json` in Methods

### Location

- `backend/api/server.py` line 553: `import json as _json` inside `create_save`
- `backend/engine/session.py` line 154: `import json as _json` inside `load_active_state`

### Issue

Redundant re-imports inside method bodies. `json` is already imported at the top of `server.py` (not at `session.py`, to be fair). The `as _json` alias suggests a naming conflict concern, but no variable named `json` exists in these scopes. In `session.py`, the top of the file doesn't import `json` at all -- it should.

### Fix

In `session.py`, add `import json` at the top of the file (alongside the other imports) and use `json.load()` directly in `load_active_state`. In `server.py`, use the existing `json` import.

---

## Finding 12: Missing Type Hints on Module SDK Interfaces

### Location

Module backend hooks in `graph.py` and `registry.py` use untyped parameters for the `sdk` and `state` objects.

### Code

```python
# wb_core_rpg/backend.py
async def on_gather_context(state: dict, sdk) -> dict:
    char = Character.from_dict(state.get(...))
```

### Issue

The `sdk` parameter is completely untyped. Module authors get no IDE autocomplete for `sdk.llm.generate()`, `sdk.ValidationVeto`, `sdk.ui.emit_token()`, etc. The `state` parameter uses `dict` which provides no insight into available keys.

### Fix

Create a Protocol or abstract base class in the SDK:

```python
# backend/sdk/__init__.py  (new file)
from typing import Protocol

class WorldBoxSDKProtocol(Protocol):
    """Interface exposed to modules via the WorldBox SDK."""
    llm: Any  # LLMBridge
    ui: Any   # WorldBoxUI
    ValidationVeto: type[Exception]

    def reveal_map_node(self, node_id: str) -> list[str]: ...
    def bind_session_state(self, state_ref: dict) -> None: ...
```

Then annotate module hooks:

```python
from backend.sdk import WorldBoxSDKProtocol

async def on_gather_context(state: dict[str, Any], sdk: WorldBoxSDKProtocol) -> dict:
    ...
```

This gives module authors autocomplete for the SDK surface without importing the concrete implementation.

---

## Priority Order

1. **Finding 1** -- `datetime.utcnow()` (trivial, 1 line)
2. **Finding 7** -- Dummy `$t(` macro (trivial, 1 line)
3. **Finding 2** -- Dead `set_world_index_path` (trivial)
4. **Finding 11** -- Inline json imports (trivial)
5. **Finding 6** -- Error swallowing (medium impact, easy fix)
6. **Finding 10** -- Dimension mismatch warning (potential data corruption)
7. **Finding 5** -- Fragile filename parsing (potential crash)
8. **Finding 8** -- LLMBridge stale env reads (fragile design)
9. **Finding 3** -- Linear memory scan (performance)
10. **Finding 4** -- Widget route manifest re-parse (performance)
11. **Finding 9** -- Mock location database (maintainability)
12. **Finding 12** -- Missing type hints (DX improvement)
