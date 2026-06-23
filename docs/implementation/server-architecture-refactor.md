# Implementation Plan: Server Architecture Refactor

## Overview

A codebase review identified several architectural issues in the API layer and service initialization. `server.py` has grown to 1417 lines with all route handlers in a single file. All backend services are created as module-level globals with no lifecycle management. World generation uses a mutable global dict that will race under concurrent requests. The project has zero `__init__.py` files, relying on fragile implicit namespace packages.

---

## Definition of Done

1. `server.py` is under 200 lines -- an app factory that mounts routers.
2. Each domain area has its own router module under `backend/api/routers/`.
3. Services are created with proper lifecycle management (FastAPI lifespan or dependency injection).
4. World generation state is per-session, not global.
5. `__init__.py` files exist in `backend/`, `backend/engine/`, `backend/sdk/`, and `backend/api/`.

---

## Finding 1: Monolithic `server.py`

### Location

`backend/api/server.py` -- 1417 lines, single file.

### Impact

- All route handlers for every domain (session, saves, worlds, characters, providers, prompts, modules, health, settings, memory) are in one file.
- Adding a new route requires navigating a single large file.
- Testing a specific route group requires importing the entire server module.
- Route prefix changes are scattered across the file.

### Current Route Groupings

| Group | Lines | Routes |
|-------|-------|--------|
| Module widgets | 219-280 | `GET /widgets/{mod_id}/{filename}` + dynamic mounts |
| Module metadata | 282-296 | `GET /api/modules` |
| Session management | 298-352 | `GET/PUT /api/session`, `/api/session/module-configs`, `/api/session/prompt-pipeline` |
| Prompt library | 358-416 | `GET/POST /api/prompts`, `PUT/DELETE /api/prompts/{id}`, import |
| Memory | 418-451 | `GET/DELETE /api/session/memories` |
| Settings | 454-470 | `GET/PUT /api/settings` |
| Save management | 472-613 | `GET/POST /api/saves`, `/{save_id}/load`, `/{save_id}/undo` |
| Health | 616-676 | `GET /api/health` |
| World builder | 678-1072 | `GET/POST /api/world/*` (15+ endpoints) |
| Session map reveal | 1079-1085 | `POST /api/session/reveal-node` |
| Character builder | 1088-1250 | `GET/POST /api/character/*` (10+ endpoints) |
| WebSocket chat | 1253-1341 | `WS /ws/chat` |
| Provider management | 1344-1417 | `GET/PUT/POST /api/providers/*` (8 endpoints) |

### Fix: Split into FastAPI Routers

Create a new directory `backend/api/routers/` with one module per domain:

```
backend/api/
├── __init__.py
├── app.py              # App factory -- creates FastAPI, mounts routers
├── routers/
│   ├── __init__.py
│   ├── session.py      # /api/session/*, /api/saves/*
│   ├── world.py        # /api/world/*
│   ├── character.py    # /api/character/*
│   ├── provider.py     # /api/providers/*
│   ├── prompt.py       # /api/prompts/*
│   ├── module.py       # /api/modules, /widgets/*
│   ├── memory.py       # /api/session/memories/*
│   ├── settings.py     # /api/settings
│   ├── health.py       # /api/health
│   └── websocket.py    # WS /ws/chat
```

Each router defines its own prefix and tags:

```python
# backend/api/routers/session.py
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["session"])

@router.get("/session")
async def get_session():
    ...

@router.get("/saves")
async def list_saves():
    ...
```

The app factory in `app.py`:

```python
# backend/api/app.py
from fastapi import FastAPI
from backend.api.routers import session, world, character, provider, prompt, module, memory, settings, health, websocket

def create_app(registry, engine, session_manager, ...) -> FastAPI:
    app = FastAPI(title="WorldBox API")
    app.state.registry = registry
    app.state.engine = engine
    app.state.session_manager = session_manager
    # ...

    app.include_router(session.router)
    app.include_router(world.router)
    app.include_router(character.router)
    app.include_router(provider.router)
    app.include_router(prompt.router)
    app.include_router(module.router)
    app.include_router(memory.router)
    app.include_router(settings.router)
    app.include_router(health.router)
    app.include_router(websocket.router)

    return app
```

`main.py` changes to:

```python
import uvicorn
from backend.api.app import create_app
from backend.api.bootstrap import bootstrap_services

if __name__ == "__main__":
    reload = "--reload" in sys.argv
    services = bootstrap_services()
    app = create_app(**services)
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=reload)
```

`bootstrap_services()` in a new `backend/api/bootstrap.py` handles service creation:

```python
# backend/api/bootstrap.py
def bootstrap_services():
    registry = ModuleRegistry("modules")
    registry.load_all_modules()

    backend_settings = SettingsRegistry()
    provider_manager = ProviderManager()
    engine = EngineGraph(registry, settings_registry=backend_settings, provider_manager=provider_manager)
    session_manager = GameSessionManager("data", settings=backend_settings)
    engine.set_memory_path(session_manager.get_memory_path())

    world_builder = WorldBuilder()
    world_builder.set_llm_service(engine.llm)
    # ... etc

    return {
        "registry": registry,
        "engine": engine,
        "session_manager": session_manager,
        "world_builder": world_builder,
        # ...
    }
```

---

## Finding 2: Module-Level Singletons

### Location

`backend/api/server.py` lines 38-48. Services are created at module import time.

### Code

```python
# server.py -- executed at IMPORT time, not at app startup
registry = ModuleRegistry(modules_dir)
registry.load_all_modules()
engine = EngineGraph(registry, ...)
session_manager = GameSessionManager(data_dir, ...)
world_builder = WorldBuilder()
character_builder = CharacterBuilder()
```

### Impact

- If `server.py` is imported more than once (e.g., by a test runner), duplicate service instances are created.
- No clean shutdown path -- LanceDB connections are never closed.
- Services cannot be scoped to a request or session.
- Mocking services in tests requires monkeypatching module-level variables.

### Fix

Use FastAPI's `lifespan` context manager or `app.state` for service lifecycle:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    services = bootstrap_services()
    app.state.registry = services["registry"]
    app.state.engine = services["engine"]
    app.state.session_manager = services["session_manager"]
    # ...

    yield

    # Shutdown
    if app.state.engine.memory:
        app.state.engine.memory.db.close()
    # ... other cleanup

app = FastAPI(lifespan=lifespan)
```

Routes access services via `request.app.state`:

```python
@router.get("/api/session")
async def get_session(request: Request):
    session_manager = request.app.state.session_manager
    return session_manager.get_status()
```

---

## Finding 3: Global Mutable World Generation State

### Location

`backend/api/server.py` line 212.

### Code

```python
world_gen_state: dict[str, Any] = {}
```

### Impact

If two users trigger world generation simultaneously (e.g., two browser tabs), they will corrupt each other's state. The `global world_gen_state` is read and written in every world builder route handler.

### Fix

Replace the global dict with a session-keyed store:

```python
# backend/api/world_state.py
from collections import defaultdict

class WorldGenSessions:
    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def get(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            self._sessions[session_id] = {"seed_prompt": "", "steps": {}, "complete": False}
        return self._sessions[session_id]

    def remove(self, session_id: str):
        self._sessions.pop(session_id, None)

world_gen_sessions = WorldGenSessions()
```

World builder routes accept a `session_id` parameter (or use the active save ID) to scope state. The frontend passes this ID in world builder API calls.

---

## Finding 4: Missing `__init__.py` Files

### Location

Zero `__init__.py` files exist anywhere in the project.

### Impact

- The project relies on implicit namespace packages (Python 3.3+).
- IDE autocomplete and navigation may not work reliably.
- Other projects cannot install or import `backend` as a package.
- Test discovery can behave unexpectedly.

### Fix

Add minimal `__init__.py` files:

```
backend/__init__.py       (empty)
backend/api/__init__.py   (empty)
backend/engine/__init__.py (empty)
backend/sdk/__init__.py   (empty)
backend/api/routers/__init__.py (empty)
```

No code changes required -- the existing imports are already fully qualified and compatible with both namespace packages and regular packages.

---

## Execution Order

1. **Step 1**: Add `__init__.py` files (zero-risk, no import changes).
2. **Step 2**: Create `backend/api/bootstrap.py` with the `bootstrap_services()` function -- move service creation out of `server.py` without changing any routes yet.
3. **Step 3**: Create FastAPI `lifespan` handler in a new `backend/api/app.py`.
4. **Step 4**: Extract routers one domain at a time, updating `main.py` to use the new app factory.
5. **Step 5**: Replace `global world_gen_state` with `WorldGenSessions`.
6. **Step 6**: Verify all tests pass with the refactored structure.

---

## Testing Impact

Each router module can be tested independently:

```python
# test_routers/test_session.py
from fastapi.testclient import TestClient
from backend.api.app import create_app
from backend.api.bootstrap import bootstrap_services

def test_get_session():
    services = bootstrap_services()
    app = create_app(**services)
    client = TestClient(app)
    response = client.get("/api/session")
    assert response.status_code == 200
    assert "turn" in response.json()
```

---

## Related Issues From Review

| Location | Issue | Plan |
|----------|-------|------|
| `server.py:212` | `global world_gen_state` | WorldGenSessions (Finding 3) |
| `server.py:251-280` | Widget route re-reads manifests each request | Cache module_id -> directory mapping in bootstrap |
| `server.py:232-248` | Closure-based widget endpoint creation in loop | Router extraction naturally fixes this |
| `main.py` | Direct `uvicorn.run` with string import path | Use app factory |
| Zero `__init__.py` files | Implicit namespace packages | Add `__init__.py` (Finding 4) |
