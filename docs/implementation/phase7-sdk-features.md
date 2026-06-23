# Implementation Plan: Phase 7 SDK Features

## Overview

Phase 7 adds the "bells and whistles" for complex modding: an Event Bus for cross-module communication, a slash-command pre-router for LLM bypass, a validation veto system for enforcing mechanical rules, and an AST inspector for module security.

## 4.1 Event Bus (Pub/Sub)

### Design

The Event Bus is an in-memory publish/subscribe system embedded in the `WorldBoxSDK` object. Modules register listeners and emit events through the SDK, enabling decoupled cross-module communication.

### API Surface

```python
# SDK object exposed to modules
class WorldBoxSDK:
    events: EventBus  # New sub-object
```

```python
class EventBus:
    def on(self, event_name: str, callback: Callable) -> None:
        """Register a listener for an event. Callback signature: async def(payload, state, sdk)."""
        
    def emit(self, event_name: str, payload: dict) -> None:
        """Emit an event. All registered listeners are called asynchronously in order."""
        
    def off(self, event_name: str, callback: Callable) -> None:
        """Remove a specific listener."""
        
    def clear(self, event_name: str = None) -> None:
        """Remove all listeners for an event, or all listeners if no event specified."""
```

### Engine-Level Events

The engine emits these built-in events at specific pipeline stages:

| Event | Payload | When |
|---|---|---|
| `turn_start` | `{"turn": int, "input_text": str}` | Beginning of a new turn |
| `context_gathered` | `{"context": str, "turn": int}` | After gather_context_node completes |
| `story_complete` | `{"narrative": str, "turn": int}` | After storyteller_node completes |
| `reader_complete` | `{"mutations": dict, "turn": int}` | After reader_node completes |
| `turn_end` | `{"turn": int, "state_snapshot": dict}` | End of turn, before librarian |

### Implementation

#### Step 1: Create EventBus class

File: `backend/sdk/event_bus.py`

```python
import asyncio
import logging
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

class EventBus:
    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}
    
    def on(self, event_name: str, callback: Callable) -> None:
        if event_name not in self._listeners:
            self._listeners[event_name] = []
        self._listeners[event_name].append(callback)
        logger.debug(f"Registered listener for '{event_name}'")
    
    async def emit(self, event_name: str, payload: dict, state: dict = None, sdk = None) -> None:
        listeners = self._listeners.get(event_name, [])
        if not listeners:
            return
        
        logger.debug(f"Emitting '{event_name}' to {len(listeners)} listener(s)")
        
        for callback in listeners:
            try:
                await callback(payload, state, sdk)
            except Exception as e:
                logger.error(f"Listener for '{event_name}' raised: {e}")
                # Don't crash the pipeline; log and continue
    
    def off(self, event_name: str, callback: Callable) -> None:
        if event_name in self._listeners:
            self._listeners[event_name] = [
                cb for cb in self._listeners[event_name] if cb != callback
            ]
    
    def clear(self, event_name: str = None) -> None:
        if event_name:
            self._listeners.pop(event_name, None)
        else:
            self._listeners.clear()
```

#### Step 2: Integrate into WorldBoxSDK

```python
# backend/sdk/mock_sdk.py (or new backend/sdk/sdk.py)

class WorldBoxSDK:
    def __init__(self):
        self.events = EventBus()
        self.ui = UIBridge()
        # ... other attributes ...
```

#### Step 3: Wire engine events into graph nodes

In `backend/engine/graph.py`:

```python
async def gather_context_node(self, state: WorldState) -> WorldState:
    sdk = state.get("sdk")
    if sdk:
        await sdk.events.emit("turn_start", {"turn": state["turn"], "input_text": state["input_text"]}, state, sdk)
    # ... existing logic ...
    if sdk:
        await sdk.events.emit("context_gathered", {"context": state.get("current_context", ""), "turn": state["turn"]}, state, sdk)
    return state
```

Same pattern for storyteller_node (`story_complete`), reader_node (`reader_complete`), and session.py after turn completion (`turn_end`).

#### Step 4: Support on_init for event registration

Update the module contract to call `on_init(sdk)` after module loading:

```python
# backend/engine/registry.py

for module in loaded_modules:
    backend = module["backend"]
    if hasattr(backend, "on_init"):
        await backend.on_init(sdk)
```

This allows modules to register event listeners at load time:

```python
# modules/core_thirst/backend.py

async def on_init(sdk):
    sdk.events.on("character_sleep", on_character_sleep)

async def on_character_sleep(payload, state, sdk):
    hours = payload.get("duration", 8)
    # Increase thirst...
```

### Testing

File: `test_event_bus.py`

- Test: register listener, emit event, verify callback fires
- Test: multiple listeners for same event, all fire
- Test: listener exception doesn't prevent other listeners
- Test: `off()` removes specific listener
- Test: `clear()` removes all listeners
- Test: emit with no listeners (no-op, no crash)
- Test: engine-level events fire in correct pipeline order
- Test: `on_init` correctly registers listeners

---

## 4.2 Slash-Command Pre-Router

### Design

Commands defined in manifest.json are intercepted at Node 0 of the graph before any LLM call. If the input text matches a registered command, the engine executes the module's handler and optionally terminates the turn (saving API costs).

### Manifest Schema Addition

```json
{
  "commands": {
    "/roll": "on_command_roll",
    "/inventory": "on_command_inventory",
    "/weather": "on_command_weather"
  }
}
```

Values are method names on the `backend.py` module class/instance.

### Command Handler Signature

```python
async def on_command_roll(args: list[str], state: dict, sdk: WorldBoxSDK) -> dict:
    """
    args: The space-separated arguments after the command (e.g., ["2d6", "+3"])
    state: Current WorldState
    sdk: WorldBoxSDK instance
    
    Returns a dict with:
    - "message": str (system message to display in chat)
    - "signal": str (optional, "end_turn" to bypass LLM)
    - "state_update": dict (optional, partial WorldState update)
    """
```

### Implementation

#### Step 1: Add commands field validation to ModuleRegistry

```python
# backend/engine/registry.py — in _validate_manifest()

# Validate commands field
commands = manifest.get("commands", {})
if not isinstance(commands, dict):
    raise ValueError(f"Module {module_id}: 'commands' must be a dict mapping command strings to handler names")

for cmd, handler in commands.items():
    if not cmd.startswith("/"):
        raise ValueError(f"Module {module_id}: command '{cmd}' must start with '/'")
    if not isinstance(handler, str):
        raise ValueError(f"Module {module_id}: handler for '{cmd}' must be a string method name")
```

#### Step 2: Build command registry

```python
# backend/engine/registry.py

class ModuleRegistry:
    def __init__(self):
        self._command_handlers: Dict[str, tuple] = {}  # {"/roll": (module_backend, "on_command_roll")}
    
    def _register_commands(self, module_backend, manifest: dict) -> None:
        commands = manifest.get("commands", {})
        for cmd, handler in commands.items():
            if cmd in self._command_handlers:
                logger.warning(f"Command '{cmd}' already registered; overwriting with {manifest['id']}")
            self._command_handlers[cmd] = (module_backend, handler)
    
    def get_command_handler(self, command: str) -> tuple | None:
        return self._command_handlers.get(command)
```

#### Step 3: Add command interception to LangGraph

Two options:
- **Option A**: New `command_router_node` before `gather_context_node` (cleaner DAG)
- **Option B**: Check in `gather_context_node` itself (simpler, fewer nodes)

**Recommend Option A** for clean separation:

```python
# backend/engine/graph.py

# Add to graph construction:
self.graph.add_node("command_router", self.command_router_node)
self.graph.set_entry_point("command_router")
self.graph.add_conditional_edges(
    "command_router",
    self._after_command_router,
    {
        "gather": "gather_context_node",
        "end": END
    }
)

async def command_router_node(self, state: WorldState) -> WorldState:
    input_text = state.get("input_text", "").strip()
    if not input_text.startswith("/"):
        return state  # Not a command, proceed normally
    
    # Parse command and args
    parts = input_text.split()
    command = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []
    
    handler = self.registry.get_command_handler(command)
    if not handler:
        # Unknown command — let it go through to LLM as text
        return state
    
    module_backend, handler_name = handler
    handler_func = getattr(module_backend, handler_name, None)
    if not handler_func:
        logger.error(f"Handler '{handler_name}' not found in module")
        return state
    
    try:
        result = await handler_func(args, state, state.get("sdk"))
        message = result.get("message", "")
        
        if message:
            state["command_message"] = message
            if "sdk" in state:
                state["sdk"].ui.push_chat_message(message)
        
        if result.get("state_update"):
            state.update(result["state_update"])
        
        if result.get("signal") == "end_turn":
            state["command_end_turn"] = True
    except Exception as e:
        logger.error(f"Command handler '{command}' failed: {e}")
    
    return state

def _after_command_router(self, state: WorldState) -> str:
    if state.get("command_end_turn"):
        return "end"
    return "gather"
```

#### Step 4: Frontend handling

- Command messages appear in chat as system messages (distinct styling)
- Input bar should support tab-completion for known commands (future enhancement)
- Show available commands in a help panel or on `/help`

### Testing

File: `test_commands.py`

- Test: `/roll` triggers registered handler
- Test: Unknown command passes through to LLM
- Test: Command with `end_turn` bypasses LLM
- Test: Command without `end_turn` continues pipeline
- Test: Command arguments parsed correctly
- Test: Handler exception doesn't crash turn
- Test: Duplicate command registration overwrites with warning
- Test: Command payload appears in chat as system message

---

## 4.3 Validation Veto & Rewrite Loop

### Design

Modules can inspect the LLM's output before it reaches the user. If the LLM violates a mechanical rule (e.g., spending gold the player doesn't have), the module raises a `ValidationVeto`. The LangGraph then loops back to the Storyteller, injecting the veto reason, and forces a rewrite (max 2 retries = 3 total attempts).

### API Surface

```python
# backend/sdk/sdk.py

class ValidationVeto(Exception):
    """Raised by on_validate_output to trigger a story rewrite."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)

class WorldBoxSDK:
    ValidationVeto = ValidationVeto  # Convenience accessor
```

### Module Hook

```python
# modules/core_economy/backend.py

async def on_validate_output(llm_output: str, state: dict, sdk) -> None:
    """Called after Storyteller generates narrative, before it reaches the user."""
    gold = state["module_data"].get("core_economy", {}).get("gold", 0)
    
    if "buys" in llm_output.lower() and gold < 50:
        raise sdk.ValidationVeto(
            "The player has less than 50 gold. Do not narrate purchases. "
            "Instead, have the merchant refuse or the player realize they can't afford it."
        )
```

### Implementation

This is covered in detail in `llm-pipeline-hardening.md` section D4. Key components:

1. **WorldState fields**: `veto_retries`, `veto_reason`, `needs_rewrite`
2. **on_validate_output dispatch**: After storyteller, call each module's hook
3. **Conditional edge**: `reader_node` → check veto → loop back or continue
4. **Veto injection**: Storyteller receives veto reason as system message at depth 0
5. **Max retries**: 3 total attempts, then fallback message
6. **Graceful failure**: System message explaining failure after exhaustion

### Testing

File: `test_validation_veto.py`

- Test: Single veto triggers rewrite
- Test: Veto reason appears in rewritten prompt
- Test: Max 3 attempts enforced
- Test: Module with no `on_validate_output` is skipped silently
- Test: Two modules both can veto (first veto triggers rewrite)
- Test: Fallback message after exhaustion
- Test: Veto does not save partial turns

---

## 4.4 AST Inspector (Module Security)

### Design

Before loading a module's `backend.py`, the engine parses its Python Abstract Syntax Tree and blocks dangerous imports. Modules may only import from an explicit whitelist of safe standard library modules plus the WorldBox SDK.

### Blocked Imports

```
os, sys, subprocess, socket, requests, urllib, http, ftplib,
shutil, pathlib (write operations), eval, exec, compile,
__import__, importlib, ctypes, multiprocessing, threading,
signal, pty, pdb, code, codeop, fileinput
```

### Allowed Imports (Whitelist)

```
json, re, math, random, datetime, collections, itertools,
functools, typing, enum, dataclasses, copy, hashlib,
textwrap, string, uuid, decimal, fractions, statistics
```

Plus: `worldbox_sdk` (special exemption)

### Implementation

File: `backend/engine/ast_inspector.py`

```python
import ast
import logging

logger = logging.getLogger(__name__)

BLOCKED_IMPORTS = {
    "os", "sys", "subprocess", "socket", "requests", "urllib", "http",
    "ftplib", "shutil", "pathlib", "ctypes", "multiprocessing", "threading",
    "signal", "pty", "pdb", "code", "codeop", "fileinput", "importlib",
}

ALLOWED_IMPORTS = {
    "json", "re", "math", "random", "datetime", "collections", "itertools",
    "functools", "typing", "enum", "dataclasses", "copy", "hashlib",
    "textwrap", "string", "uuid", "decimal", "fractions", "statistics",
    "asyncio", "logging", "traceback", "pathlib",
}

BLOCKED_FUNCTIONS = {"eval", "exec", "compile", "__import__", "open", "breakpoint"}

class ASTInspector:
    def inspect_file(self, filepath: str) -> tuple[bool, list[str]]:
        """
        Returns (is_safe, violations[]).
        """
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
        
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return False, [f"Syntax error: {e}"]
        
        violations = []
        visitor = self._SecurityVisitor(violations)
        visitor.visit(tree)
        
        return len(violations) == 0, violations
    
    class _SecurityVisitor(ast.NodeVisitor):
        def __init__(self, violations: list):
            self.violations = violations
        
        def visit_Import(self, node):
            for alias in node.names:
                name = alias.name.split(".")[0]
                if name in BLOCKED_IMPORTS:
                    self.violations.append(f"Blocked import: {alias.name} (line {node.lineno})")
        
        def visit_ImportFrom(self, node):
            if node.module:
                name = node.module.split(".")[0]
                if name in BLOCKED_IMPORTS:
                    self.violations.append(f"Blocked import from: {node.module} (line {node.lineno})")
        
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_FUNCTIONS:
                self.violations.append(f"Blocked function call: {node.func.id}() (line {node.lineno})")
            self.generic_visit(node)
```

### Integration into ModuleRegistry

```python
# backend/engine/registry.py

def _load_module_backend(self, module_folder: Path):
    backend_path = module_folder / "backend.py"
    
    # AST inspection
    inspector = ASTInspector()
    is_safe, violations = inspector.inspect_file(str(backend_path))
    
    if not is_safe:
        logger.warning(f"Security violations in {module_folder.name}:")
        for v in violations:
            logger.warning(f"  {v}")
        # Skip loading this module
        return None
    
    # ... proceed with import ...
```

### User-Facing Warning

When enabling a module via the frontend settings, display:

```
WARNING: Third-party code execution

This module contains Python code that will run on your computer. 
Malicious modules can:
- Steal data
- Incur API costs
- Compromise your system

WorldBox takes NO responsibility for community modules.

AST inspection passed: 0 violations
Module source: [link to repository]

☐ I understand and accept the risk
[Enable Module]
```

### Testing

File: `test_ast_inspector.py`

- Test: Safe module (only whitelisted imports) → passes
- Test: Blocked import (`import os`) → fails with violation
- Test: Blocked import from (`from os import path`) → fails
- Test: Blocked function call (`eval()`, `exec()`) → fails
- Test: Module with no imports → passes
- Test: Syntax error → fails with clear message
- Test: Submodule of blocked package (`os.path`) → fails

---

## Execution Order

1. **4.1 Event Bus** — Foundation for 4.2 and 4.3 (commands and validation use event hooks)
2. **4.2 Slash Commands** — Unlocks module interactivity without LLM cost
3. **4.3 Validation Veto** — Enforces game mechanics integrity
4. **4.4 AST Inspector** — Security gate before third-party modules are supported
