# Implementation Plan: Module-Engine Decoupling

## Overview

A full codebase review revealed that the engine (`backend/engine/`) contains hardcoded references to the `wb_core_rpg` module. This violates the module contract -- a module should be safe to delete from `modules/` without causing errors in save creation, turn execution, intro generation, or automated tests.

This plan catalogs every coupling point and provides a step-by-step fix plan.

---

## Definition of Done

1. Delete `modules/wb_core_rpg/`.
2. Start the server -- no import errors, no startup crashes.
3. Create a new save -- no `wb_core_rpg` in `module_data`.
4. Run a turn -- no key errors looking for `hp`, `stats`, or `level`.
5. All existing tests pass without the `wb_core_rpg` module present.

---

## Finding 1: Hardcoded Default `module_data` in `session.py`

### Location

`backend/engine/session.py` -- `_ensure_default_save()` (lines 43-46) and `create_save()` (line 91).

### Code

```python
# _ensure_default_save, line 43-46
"module_data": {
    "wb_core_rpg": {"hp": 85, "max_hp": 85},
}

# create_save, line 91
module_data = character_module_data if character_module_data else {"wb_core_rpg": {"hp": 85, "max_hp": 85}}
```

### Impact

Every new save gets orphaned RPG module state hardcoded into it. If `wb_core_rpg` is removed, the engine still writes this dead data into every save. The save system should be module-agnostic.

### Fix

1. Remove the hardcoded `"wb_core_rpg"` key from default `module_data`.
2. Populate default `module_data` by calling `registry.get_modules()` and invoking each module's `on_gather_context` or `on_character_get_defaults` hook at save creation time.

**In `session.py`:**

```python
# _ensure_default_save
"module_data": {},  # Empty by default -- modules populate via hooks

# create_save
module_data = character_module_data if character_module_data else {}
```

**In `server.py` `create_save` endpoint**, after save creation, call:

```python
# After session_manager.create_save(...)
for mod_id, mod_data in registry.get_modules().items():
    backend = mod_data["backend"]
    if hasattr(backend, "on_character_get_defaults"):
        try:
            defaults = await backend.on_character_get_defaults({}, {})
            if isinstance(defaults, dict) and defaults:
                session_manager.state.setdefault("module_data", {})[mod_id] = defaults
        except Exception as e:
            print(f"Error getting defaults from {mod_id}: {e}")
```

---

## Finding 2: Duplicated Stat Tier Logic in `graph.py`

### Location

`backend/engine/graph.py` -- `DEFAULT_STAT_TIERS` (lines 15-23) and `_stat_tier_label()` (lines 25-29).

### Code

```python
# graph.py:15-23
DEFAULT_STAT_TIERS = [
    {"min": 1, "max": 4, "label": "Severely Impaired"},
    {"min": 5, "max": 8, "label": "Below Average"},
    {"min": 9, "max": 12, "label": "Average"},
    {"min": 13, "max": 16, "label": "Above Average / Trained"},
    {"min": 17, "max": 20, "label": "Expert / Peak Human"},
    {"min": 21, "max": 25, "label": "Superhuman"},
    {"min": 26, "max": 30, "label": "Legendary / Demigod"},
]

# wb_core_rpg/backend.py:28-36 (DUPLICATE)
DEFAULT_STAT_TIERS = [
    {"min": 1, "max": 4, "label": "Severely Impaired"},
    ...
]
```

### Impact

The engine contains RPG-specific knowledge that belongs solely in the module. If `wb_core_rpg` changes its stat tier labels or ranges, the engine's copy goes stale. The `_stat_tier_label` function in `graph.py` is only used by `generate_intro` (see Finding 3).

### Fix

1. Remove `DEFAULT_STAT_TIERS` and `_stat_tier_label` from `graph.py`.
2. The `generate_intro` method (see Finding 3) will no longer need them once intro rendering is moved to module prompt blocks.

---

## Finding 3: `generate_intro()` Assumes RPG Data Concepts

### Location

`backend/engine/graph.py` -- `generate_intro()` method (lines 156-245). The coupling is in lines 164-186.

### Code

```python
# graph.py:164-186
for mod_name, mod_state in module_data.items():
    if isinstance(mod_state, dict):
        hp = mod_state.get("hp")           # RPG-specific
        max_hp = mod_state.get("max_hp")   # RPG-specific
        stats = mod_state.get("stats")     # RPG-specific
        level = mod_state.get("level")     # RPG-specific
```

### Impact

The intro generator iterates all modules generically but assumes module data contains `hp`, `max_hp`, `stats`, and `level`. These are RPG-specific fields. Without `wb_core_rpg`, they simply won't be found, but the code still looks for them -- a design smell. If a future module accidentally uses these same key names for unrelated data, it will be misrendered as character stats.

### Fix

Replace the hand-rolled character sheet markup in `generate_intro` with the module's own `on_render_prompt_block("character_sheet", state, sdk)` hook. The engine should not be constructing character sheet XML blocks -- that is the module's responsibility.

**In `graph.py` `generate_intro()`:**

```python
# Replace lines 164-186 with:
character_context_blocks = []
for mod_id, mod_data in self.registry.get_modules().items():
    backend = mod_data["backend"]
    manifest = mod_data["manifest"]
    for mb in manifest.get("prompt_blocks", []):
        if mb.get("id") == "character_sheet" and hasattr(backend, "on_render_prompt_block"):
            try:
                result = await backend.on_render_prompt_block(mb, state, self.sdk)
                text = result if isinstance(result, str) else result.get("content", "")
                if text:
                    character_context_blocks.append(text)
            except Exception as e:
                print(f"Error rendering character_sheet for {mod_id}: {e}")
```

If no module provides a `character_sheet` block, the intro proceeds without character stats -- which is correct behavior when no RPG module is loaded.

---

## Finding 4: Tests Hardcode `wb_core_rpg` as a Dependency

### Location and Impact

| Test File | Line(s) | Issue |
|-----------|---------|-------|
| `test_module_contract.py` | 99, 107-117 | Accesses `registry.get_modules()["wb_core_rpg"]` directly. Hardcodes `wb_core_rpg` in state fixture. |
| `test_validation_veto.py` | 66-128 | Imports `modules.wb_core_rpg.backend` directly. All 5 tests require the module. |
| `test_engine.py` | 25-26 | Hardcodes `wb_core_rpg` module data in test state. |

### Fix

1. **`test_module_contract.py`**: Replace the `wb_core_rpg`-specific mutation test with a test that creates a temporary module in a `tempfile.TemporaryDirectory`, similar to the dependency ordering test already in that file. The test should validate that _any_ module with a `mutation_schema` gets its `on_mutate_state` called correctly.

2. **`test_validation_veto.py`**: Move this file to `modules/wb_core_rpg/test_validation_veto.py`. Veto logic is module-specific behavior and should be tested alongside the module it validates.

3. **`test_engine.py`**: Remove the hardcoded `wb_core_rpg` from test state. Use an empty `module_data: {}` or populate it from registry-loaded module defaults.

```python
# test_engine.py -- updated fixture
game_state = {
    "input_text": "I look around.",
    "module_data": {},  # No hardcoded module data
    "current_context": [],
    "history": [],
}
```

---

## Execution Order

1. **Step 1**: Move `test_validation_veto.py` to `modules/wb_core_rpg/test_validation_veto.py` (low-risk, just file relocation).
2. **Step 2**: Remove `DEFAULT_STAT_TIERS` and `_stat_tier_label` from `graph.py` (only used by `generate_intro`).
3. **Step 3**: Refactor `generate_intro()` to use `on_render_prompt_block` hooks instead of hand-rolled character sheet markup.
4. **Step 4**: Replace hardcoded `wb_core_rpg` defaults in `session.py` with hook-driven module data population.
5. **Step 5**: Fix `test_module_contract.py` and `test_engine.py` to not depend on `wb_core_rpg`.
6. **Step 6**: Verification -- delete `wb_core_rpg` and confirm the app starts, creates saves, and runs turns.

---

## Verification Test

```python
# test_module_removal_safety.py
import asyncio
import tempfile
import os

async def test_app_runs_without_core_rpg():
    """The engine must start, create saves, and run turns without wb_core_rpg."""
    with tempfile.TemporaryDirectory() as tmp:
        # Setup: copy all modules EXCEPT wb_core_rpg
        modules_dir = os.path.join(tmp, "modules")
        os.makedirs(modules_dir)
        # Only copy wb_test_sidebar (or create a minimal test module)
        # ...

        registry = ModuleRegistry(modules_dir)
        registry.load_all_modules()
        engine = EngineGraph(registry)

        state = {
            "input_text": "I look around.",
            "module_data": {},
            "module_configs": {},
            "characters": {},
            "current_context": [],
            "history": [],
            "chat_messages": [],
            "turn": 0,
        }

        # Should not raise KeyError or AttributeError
        await engine.gather_context_node(state)
        assert engine.app is not None
```

---

## Related Issues From Review

These are additional coupling points discovered in the full review:

| Location | Issue |
|----------|-------|
| `graph.py:15-29` | Duplicate `DEFAULT_STAT_TIERS` -- covered in Finding 2 |
| `graph.py:164-186` | `generate_intro()` RPG assumptions -- covered in Finding 3 |
| `session.py:43-46` | Hardcoded `wb_core_rpg` in `_ensure_default_save` -- covered in Finding 1 |
| `session.py:91` | Hardcoded `wb_core_rpg` in `create_save` -- covered in Finding 1 |
| `graph.py` module callback error handlers | Use `print()` instead of `logger.exception()` -- covered in code-quality-review.md |
