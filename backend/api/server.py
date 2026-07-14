from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from backend.engine.registry import ModuleRegistry
from backend.engine.graph import EngineGraph, CHARACTER_UPDATE_FIELDS
from backend.engine.llm import LLMProviderError
from backend.engine.llm_inspector import LLMInspector
from backend.engine.log_store import LogStore, install_log_capture
from backend.engine.session import GameSessionManager
from backend.engine.settings_registry import SettingsRegistry
from backend.engine.character_builder import CharacterBuilder
from backend.engine.scenario import ScenarioStore
from backend.engine.prompt_pipeline import STORY_STYLE_FIELDS
from backend.engine.lorebook import LorebookStore, make_story_entry, patch_story_entry, story_entries_book
from backend.engine.provider_manager import ProviderManager
from backend.engine.prompt_library import PromptLibrary, get_default_library_path
from backend.engine.prompt_pipeline import AVAILABLE_MACROS, default_prompt_pipeline, ALLOWED_ROLES, ALLOWED_PLACEMENTS, ALLOWED_BLOCK_TYPES, DEFAULT_CONTINUE_PROMPT
from backend.engine.st_importer import SillyTavernImporter
from backend.engine.theme_store import ThemeStore
from dotenv import load_dotenv
from pydantic import BaseModel
import os
import sys
import asyncio
import logging
from datetime import datetime, timezone

# On Windows, stdout/stderr default to cp1252 ("charmap"), which raises
# UnicodeEncodeError when LLM output containing non-ASCII characters is
# printed. Force UTF-8 here so the uvicorn worker process (e.g. under
# --reload, where main.py's reconfigure does not apply) is safe too.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logger = logging.getLogger(__name__)

# Capture everything the server emits (logging, print(), tracebacks) into an
# in-memory ring buffer that /api/logs serves to the frontend log viewer.
# Installed before the module registry and engine are constructed below so
# their startup output is captured too.
log_store = LogStore()
install_log_capture(log_store)
from typing import Any, Optional

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

app = FastAPI(title="WorldBox API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
modules_dir = os.path.join(base_dir, "modules")
data_dir = os.path.join(base_dir, "data")

registry = ModuleRegistry(modules_dir)
registry.load_all_modules()

backend_settings = SettingsRegistry()
provider_manager = ProviderManager()
engine = EngineGraph(registry, settings_registry=backend_settings, provider_manager=provider_manager)
session_manager = GameSessionManager(data_dir, settings=backend_settings)
engine.set_memory_path(session_manager.get_memory_path())

prompt_library = PromptLibrary(get_default_library_path())
st_importer = SillyTavernImporter()
scenario_store = ScenarioStore(data_dir)
lorebook_store = LorebookStore(data_dir)
theme_store = ThemeStore(data_dir)

llm_inspector = LLMInspector()
engine.llm.set_inspector(llm_inspector)


class ChatHub:
    """Owns the chat client socket and the running turn, independently of each
    other. A turn must survive its socket: if the client disconnects (closed
    tab, sleeping laptop, network blip) mid-generation, the turn keeps running
    headless, saves normally, and the client catches up on reconnect — either
    live (re-attached stream via `snapshot`) or from the saved transcript.
    Single-client by design, like the rest of the app: a newer connection
    simply replaces the socket the stream goes to.
    """

    def __init__(self):
        self.ws: Optional[WebSocket] = None
        self.turn_task: Optional[asyncio.Task] = None
        self.turn_action: str = ""
        self.turn_input: str = ""
        self.story_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        # Set by message_complete: narration is final but reader/librarian
        # are still running.
        self.narration: Optional[dict] = None
        self.status: Optional[dict] = None
        # Terminal payloads (error / turn_stopped) that couldn't be delivered
        # because no client was connected. State replay can't convey these, so
        # they're held for the next sync.
        self.undelivered: list[dict] = []

    def turn_running(self) -> bool:
        return self.turn_task is not None and not self.turn_task.done()

    def begin_turn(self, action: str, input_text: str):
        self.turn_action = action
        self.turn_input = input_text
        self.story_parts = []
        self.reasoning_parts = []
        self.narration = None
        self.status = None
        self.undelivered = []

    def attach(self, websocket: WebSocket):
        self.ws = websocket

    def detach(self, websocket: WebSocket):
        # Only clear our reference if this socket is still the active one — a
        # newer connection may already have replaced it.
        if self.ws is websocket:
            self.ws = None

    async def send(self, payload: dict) -> bool:
        """Best-effort send to the active client. No client (or a socket that
        dies mid-send) is not an error — the turn keeps generating headless.
        Never raises, so a disconnect can't kill the generation pipeline."""
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.detach(ws)
            return False

    async def send_or_queue(self, payload: dict):
        if not await self.send(payload):
            self.undelivered.append(payload)

    async def flush_undelivered(self):
        pending, self.undelivered = self.undelivered, []
        for payload in pending:
            await self.send(payload)

    def snapshot(self) -> dict:
        """The in-flight turn condensed into one message, so a client that
        (re)connected mid-generation can repaint it and pick up the live
        stream from there."""
        if self.narration is not None:
            story = self.narration["content"]
            reasoning = self.narration["reasoning"]
        else:
            story = "".join(self.story_parts)
            reasoning = "".join(self.reasoning_parts)
        return {
            "type": "generation_snapshot",
            "action": self.turn_action,
            "input": self.turn_input,
            "story": story,
            "reasoning": reasoning,
            "narration_complete": self.narration is not None,
            "status": self.status,
        }


chat_hub = ChatHub()


async def _stream_token(token: str):
    chat_hub.story_parts.append(token)
    await chat_hub.send({"type": "token", "content": token})


async def _stream_reasoning(token: str):
    chat_hub.reasoning_parts.append(token)
    await chat_hub.send({"type": "reasoning_token", "content": token})


async def _stream_message_complete(content: str, reasoning: str):
    chat_hub.narration = {"content": content, "reasoning": reasoning}
    await chat_hub.send({
        "type": "message_complete",
        "content": content,
        "reasoning": reasoning,
    })


async def _stream_status(stage: str, label: str):
    chat_hub.status = {"stage": stage, "label": label}
    await chat_hub.send({"type": "status", "stage": stage, "label": label})


# Bound once at module level (not per connection): the pipeline resolves these
# at call time, so a reconnect redirects the stream to the new socket simply
# by the hub swapping which websocket it sends to.
engine.sdk.ui.on_token = _stream_token
engine.sdk.ui.on_reasoning_token = _stream_reasoning
engine.sdk.ui.on_message_complete = _stream_message_complete
engine.sdk.ui.on_status = _stream_status


async def _broadcast_inspector_call(record):
    try:
        payload = llm_inspector._record_to_dict(record) if hasattr(llm_inspector, "_record_to_dict") else {}
        await chat_hub.send({"type": "llm_call", "call": payload})
    except Exception:
        pass


llm_inspector.set_ws_broadcast(_broadcast_inspector_call)


class CreateSaveRequest(BaseModel):
    save_id: str
    world_id: Optional[str] = None
    scenario_id: Optional[str] = None
    start_preference: Optional[str] = None
    start_location_node_id: Optional[str] = None
    scenario_request: Optional[str] = None
    character_id: Optional[str] = None
    active_modules: Optional[list[str]] = None


class UndoTurnRequest(BaseModel):
    target_turn: int


class ModuleConfigsRequest(BaseModel):
    module_configs: dict


class ActiveModulesRequest(BaseModel):
    active_modules: list[str]


class PromptPipelineRequest(BaseModel):
    prompt_pipeline: list[dict]



character_builder = CharacterBuilder()
character_builder.set_llm_service(engine.llm)
character_builder.set_settings(backend_settings)

# Inject shared engine services into modules that opt in via set_services(services).
# Module-owned routers (mounted below) rely on this to reach engine internals.
_module_services = {
    "engine": engine,
    "registry": registry,
    "session_manager": session_manager,
    "settings": backend_settings,
    "character_builder": character_builder,
    "data_dir": data_dir,
}
for _mod_id, _mod_data in registry.get_modules().items():
    _set_services = getattr(_mod_data.get("backend"), "set_services", None)
    if callable(_set_services):
        try:
            _set_services(_module_services)
        except Exception as _exc:
            logger.warning("Module %s set_services failed: %s", _mod_id, _exc)

for mod_id, mod_data in registry.get_modules().items():
    mod_path = mod_data["path"]

    # Mount module-owned API routes. Routers whose routes already carry an
    # absolute "/api/..." path (e.g. wb_worldgen's relocated /api/world routes)
    # are mounted at root so their paths are preserved; everything else is
    # namespaced under /api/modules/{mod_id}/*.
    mod_router = mod_data.get("router")
    if mod_router is not None:
        owns_absolute_paths = any(
            getattr(r, "path", "").startswith("/api/") for r in mod_router.routes
        )
        if owns_absolute_paths:
            app.include_router(mod_router)
            print(f"Mounted API router for {mod_id} at root (absolute paths)")
        else:
            app.include_router(mod_router, prefix=f"/api/modules/{mod_id}")
            print(f"Mounted API router for {mod_id} at /api/modules/{mod_id}")

    assets_path = os.path.join(mod_path, "__assets__")

    if os.path.exists(assets_path) and os.path.isdir(assets_path):
        app.mount(f"/assets/{mod_id}", StaticFiles(directory=assets_path), name=f"assets_{mod_id}")
        print(f"Mounted assets for {mod_id} at /assets/{mod_id}")
        
    widget_path = os.path.join(mod_path, "widget.jsx")
    if os.path.exists(widget_path):
        from fastapi.responses import FileResponse
        
        # We need a closure to capture the correct path for each iteration
        def create_widget_endpoint(path):
            async def get_widget():
                return FileResponse(path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
            return get_widget
            
        app.get(f"/widgets/{mod_id}/widget.jsx")(create_widget_endpoint(widget_path))
        print(f"Mounted widget for {mod_id} at /widgets/{mod_id}/widget.jsx")

    character_widget_path = os.path.join(mod_path, "character_widget.jsx")
    if os.path.exists(character_widget_path):
        def create_char_widget_endpoint(path):
            async def get_char_widget():
                from fastapi.responses import FileResponse
                return FileResponse(path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
            return get_char_widget
        app.get(f"/widgets/{mod_id}/character_widget.jsx")(create_char_widget_endpoint(character_widget_path))
        print(f"Mounted character widget for {mod_id} at /widgets/{mod_id}/character_widget.jsx")


@app.get("/widgets/{mod_id}/{filename:path}")
async def get_module_jsx(mod_id: str, filename: str):
    """Serve a .jsx file from a module's directory.

    Supports nested paths (e.g. ``WorldBuilder/steps/MapStepView.jsx``) so a
    module can ship a multi-file UI tree, while blocking path traversal: ``..``
    is rejected and the resolved file must stay inside the module directory.
    """
    if not filename.endswith(".jsx"):
        raise HTTPException(status_code=404, detail="Only .jsx files are served.")
    if ".." in filename or "\\" in filename or filename.startswith("/"):
        raise HTTPException(status_code=404, detail="Invalid filename.")

    from fastapi.responses import FileResponse

    def _serve_from(candidate: str):
        file_path = os.path.join(candidate, *filename.split("/"))
        candidate_real = os.path.realpath(candidate)
        file_real = os.path.realpath(file_path)
        # Containment guard: the resolved path must live under the module dir.
        if not (file_real == candidate_real or file_real.startswith(candidate_real + os.sep)):
            return None
        if os.path.isfile(file_real):
            return FileResponse(file_real, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        return None

    for item in os.listdir(modules_dir):
        candidate = os.path.join(modules_dir, item)
        if not os.path.isdir(candidate):
            continue
        manifest_path = os.path.join(candidate, "manifest.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = __import__("json").load(f)
            except Exception:
                continue
            if manifest.get("id") == mod_id:
                resp = _serve_from(candidate)
                if resp is not None:
                    return resp
        # Also try folder-name matching
        if os.path.basename(candidate) == mod_id or os.path.basename(candidate).replace("wb_", "") == mod_id:
            resp = _serve_from(candidate)
            if resp is not None:
                return resp
    raise HTTPException(status_code=404, detail=f"File {filename} not found for module {mod_id}.")

@app.get("/api/modules")
async def get_modules():
    modules = []
    for mod_id, mod_data in registry.get_modules().items():
        manifest = mod_data["manifest"]
        modules.append({
            "id": mod_id,
            "name": manifest.get("name", mod_id),
            "icon": manifest.get("icon"),
            "commands": manifest.get("commands", {}),
            "command_help": manifest.get("command_help", {}),
            "ui_slots": manifest.get("ui_slots", []),
            "settings_schema": manifest.get("settings_schema", {}),
            "prompt_blocks": manifest.get("prompt_blocks", []),
            "modes": manifest.get("modes", []),
            "has_character_creation": bool(manifest.get("character_creation")),
            "storyteller_start": manifest.get("storyteller_start"),
            "character_context": manifest.get("character_context"),
            "game_overlay": manifest.get("game_overlay"),
            "character_panel": manifest.get("character_panel"),
            "character_tab": manifest.get("character_tab"),
            "character_tab_label": manifest.get("character_tab_label"),
        })
    return {"modules": modules}

@app.get("/api/session")
async def get_session():
    return session_manager.get_status()

@app.get("/api/session/module-configs")
async def get_module_configs():
    return {"module_configs": session_manager.state.get("module_configs", {})}

@app.put("/api/session/module-configs")
async def update_module_configs(request: ModuleConfigsRequest):
    try:
        state = session_manager.update_module_configs(request.module_configs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "session": session_manager.get_status(),
        "module_configs": state.get("module_configs", {}),
    }

@app.get("/api/session/prompt-pipeline")
async def get_prompt_pipeline():
    return {
        "prompt_pipeline": session_manager.state.get("prompt_pipeline", []),
        "last_prompt_trace": session_manager.state.get("last_prompt_trace", []),
    }

@app.put("/api/session/prompt-pipeline")
async def update_prompt_pipeline(request: PromptPipelineRequest):
    try:
        state = session_manager.update_prompt_pipeline(request.prompt_pipeline)
        return {
            "session": session_manager.get_status(),
            "prompt_pipeline": state.get("prompt_pipeline", []),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/session/prompt-pipeline/preview")
async def preview_prompt_pipeline(request: PromptPipelineRequest):
    try:
        compiled = await engine.compile_prompt_preview(session_manager.state, request.prompt_pipeline)
        return {
            "messages": compiled["messages"],
            "trace": compiled["trace"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/session/prompt-pipeline/reset")
async def reset_prompt_pipeline():
    try:
        state = session_manager.update_prompt_pipeline(default_prompt_pipeline())
        return {
            "session": session_manager.get_status(),
            "prompt_pipeline": state.get("prompt_pipeline", []),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/global-prompt-pipeline")
async def get_global_prompt_pipeline():
    pipeline = session_manager.save_manager.load_global_prompt_pipeline()
    return {
        "prompt_pipeline": pipeline,
        "macros": AVAILABLE_MACROS,
    }


class GlobalPromptPipelineRequest(BaseModel):
    prompt_pipeline: list[dict]


@app.put("/api/global-prompt-pipeline")
async def update_global_prompt_pipeline(request: GlobalPromptPipelineRequest):
    try:
        normalized = session_manager.prompt_compiler.normalize_pipeline(request.prompt_pipeline)
        session_manager.save_manager.save_global_prompt_pipeline(normalized)
        # Apply to the live session immediately so the currently-loaded story
        # uses the updated pipeline without needing a reload.
        session_manager.state["prompt_pipeline"] = normalized
        return {"prompt_pipeline": normalized}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/global-prompt-pipeline/reset")
async def reset_global_prompt_pipeline():
    defaults = default_prompt_pipeline()
    session_manager.save_manager.save_global_prompt_pipeline(defaults)
    session_manager.state["prompt_pipeline"] = session_manager.prompt_compiler.normalize_pipeline(defaults)
    return {"prompt_pipeline": defaults}


class ContinuePromptRequest(BaseModel):
    text: str


@app.get("/api/continue-prompt")
async def get_continue_prompt():
    return {
        "text": session_manager.save_manager.load_continue_prompt(),
        "default": DEFAULT_CONTINUE_PROMPT,
        "macros": AVAILABLE_MACROS,
    }


@app.put("/api/continue-prompt")
async def update_continue_prompt(request: ContinuePromptRequest):
    text = session_manager.update_continue_prompt(request.text)
    return {"text": text}


@app.post("/api/continue-prompt/reset")
async def reset_continue_prompt():
    text = session_manager.update_continue_prompt(DEFAULT_CONTINUE_PROMPT)
    return {"text": text}


@app.get("/api/theme")
async def get_theme():
    return {"theme": theme_store.load()}


class ThemeRequest(BaseModel):
    preset: Optional[str] = None
    colors: dict


@app.put("/api/theme")
async def update_theme(request: ThemeRequest):
    saved = theme_store.save({"preset": request.preset, "colors": request.colors})
    return {"theme": saved}


@app.get("/api/prompts/defaults")
async def list_default_prompt_blocks():
    return {"defaults": default_prompt_pipeline()}


@app.get("/api/prompts")
async def list_prompt_templates(category: str = None):
    return {"templates": prompt_library.list_templates(category)}

@app.get("/api/prompts/macros")
async def list_available_macros():
    return {
        "macros": AVAILABLE_MACROS,
        "roles": sorted(ALLOWED_ROLES),
        "placements": sorted(ALLOWED_PLACEMENTS),
        "types": sorted(ALLOWED_BLOCK_TYPES),
    }

@app.post("/api/prompts")
async def create_prompt_template(body: dict):
    try:
        name = body.get("name", "Untitled")
        config = body.get("config", {})
        category = body.get("category", "other")
        template = prompt_library.create_template(name, config, category)
        return {"template": template}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.put("/api/prompts/{template_id}")
async def update_prompt_template(template_id: str, body: dict):
    try:
        template = prompt_library.update_template(template_id, body)
        return {"template": template}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.delete("/api/prompts/{template_id}")
async def delete_prompt_template(template_id: str):
    try:
        prompt_library.delete_template(template_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/prompts/{template_id}/to-block")
async def template_to_block(template_id: str, body: dict = None):
    try:
        block_id = body.get("block_id") if body else None
        block = prompt_library.template_to_block(template_id, block_id)
        return {"block": block}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/prompts/import-sillytavern")
async def import_sillytavern_preset(body: dict):
    try:
        result = st_importer.import_preset(body)
        return result
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _ensure_browsing_memory() -> bool:
    """Bind the memory store and world index to the active save so the memory
    browser works right after a save loads — the turn pipeline otherwise
    initializes them lazily on the first generation. False when no save is
    loaded; 503 when the embedding probe fails (provider unreachable)."""
    if engine.memory is not None:
        return True
    if not engine.memory_db_path:
        return False
    try:
        await engine.ensure_memory()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Memory system unavailable: {exc}")
    _init_world_index_for_save(session_manager.active_save_id)
    return True


async def _embedding_for_edit(text: str, step: str) -> list[float]:
    """Embed edited entry text, guarding against a vector that no longer
    matches the store's dimension (provider/mode changed since the save was
    created) — a mismatched row would silently break similarity search."""
    vector = await engine.llm.get_embedding(
        text, inspector_ctx={"call_type": "embedding", "step": step})
    expected = engine.memory.get_vector_dimension()
    if expected and len(vector) != expected:
        raise HTTPException(
            status_code=400,
            detail=f"Embedding dimension mismatch: store expects {expected}, "
                   f"current provider returned {len(vector)}. "
                   "Text edits need the same embedding model the save was created with.",
        )
    return vector


@app.get("/api/session/memories")
async def get_memories():
    if not await _ensure_browsing_memory():
        return {"memories": [], "count": 0, "active_ids": [], "context_query": ""}
    memories = engine.memory.list_all_memories(limit=500)
    active_ids = session_manager.state.get("last_retrieved_memory_ids", [])
    return {
        "memories": memories,
        "count": engine.memory.get_memory_count(),
        "active_ids": active_ids,
        "context_query": session_manager.state.get("last_context_query", ""),
        "last_stored_id": session_manager.state.get("last_stored_memory_id", ""),
    }

@app.get("/api/session/memories/context")
async def get_memory_context():
    active_ids = session_manager.state.get("last_retrieved_memory_ids", [])
    query = session_manager.state.get("last_context_query", "")
    if not active_ids or engine.memory is None:
        return {"active_memories": [], "context_query": query}
    active_memories = engine.memory.get_memories_by_ids(active_ids)
    return {
        "active_memories": active_memories,
        "context_query": query,
    }

@app.delete("/api/session/memories/{memory_id}")
async def delete_memory(memory_id: str):
    if engine.memory is None:
        raise HTTPException(status_code=503, detail="Memory system not initialized.")
    deleted = engine.memory.delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found.")
    return {"deleted": memory_id}


class MemoryUpdateRequest(BaseModel):
    text: Optional[str] = None
    summary: Optional[str] = None
    importance: Optional[int] = None
    permanent: Optional[bool] = None
    entities: Optional[list[str]] = None
    topics: Optional[list[str]] = None


@app.put("/api/session/memories/{memory_id}")
async def update_memory(memory_id: str, request: MemoryUpdateRequest):
    if not await _ensure_browsing_memory():
        raise HTTPException(status_code=503, detail="Memory system not initialized.")
    rows = engine.memory.get_memories_by_ids([memory_id])
    if not rows:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found.")
    existing = rows[0]
    fields = {k: v for k, v in request.model_dump().items() if v is not None}
    if "importance" in fields:
        fields["importance"] = max(1, min(10, fields["importance"]))
    if "text" in fields:
        fields["text"] = fields["text"].strip()
        if not fields["text"]:
            raise HTTPException(status_code=400, detail="Memory text cannot be empty.")
        # Rows that never had a distinct summary (module/bridge memories store
        # summary == text) keep the two in step, since the summary is what the
        # browser displays and what the embedding is derived from.
        if "summary" not in fields and existing["summary"] == existing["text"]:
            fields["summary"] = fields["text"]
    if "summary" in fields:
        # An emptied summary falls back to the (possibly new) text, mirroring add_memory.
        fields["summary"] = fields["summary"].strip() or fields.get("text", existing["text"])
    # The stored embedding is derived from the summary (the librarian embeds its
    # summary; bridge memories embed text, where summary == text), so re-embed
    # whenever the effective summary changes — otherwise RAG retrieval keeps
    # matching against the pre-edit content.
    vector = None
    new_summary = fields.get("summary", existing["summary"])
    if new_summary != existing["summary"]:
        vector = await _embedding_for_edit(new_summary, "memory_edit")
    memory = engine.memory.update_memory(memory_id, fields, vector=vector)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found.")
    return {"memory": memory}


@app.get("/api/session/world-entries")
async def get_world_entries():
    if not await _ensure_browsing_memory() or not engine.memory.has_world_index():
        return {"entries": [], "count": 0, "active_ids": [], "context_query": ""}
    entries = engine.memory.list_world_entries()
    return {
        "entries": entries,
        "count": len(entries),
        "active_ids": session_manager.state.get("last_retrieved_world_ids", []),
        "context_query": session_manager.state.get("last_context_query", ""),
        # Sticky lorebook entries currently held in context: source_id -> last
        # turn they stay active. Paired with `turn` so the UI can show how many
        # turns each has left.
        "sticky_source_ids": session_manager.state.get("sticky_world_entries", {}) or {},
        "turn": session_manager.state.get("turn", 0),
    }


class WorldEntryUpdateRequest(BaseModel):
    text: str


@app.put("/api/session/world-entries/{entry_id}")
async def update_world_entry(entry_id: str, request: WorldEntryUpdateRequest):
    if not await _ensure_browsing_memory() or not engine.memory.has_world_index():
        raise HTTPException(status_code=503, detail="World index not initialized.")
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Entry text cannot be empty.")
    existing = engine.memory.get_world_entry(entry_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"World entry {entry_id} not found.")
    if existing["source_type"] == "lorebook":
        # Lorebook rows are re-synced from their JSON source; a direct edit
        # would be silently wiped. Edit via the lorebook entry endpoint instead.
        raise HTTPException(status_code=400,
                            detail="Lorebook entries must be edited through the lorebook.")
    vector = await _embedding_for_edit(text, "world_entry_edit")
    entry = engine.memory.update_world_entry(entry_id, text, vector)
    return {"entry": entry}


class RagDebugRequest(BaseModel):
    query: str
    limit: int = 10


@app.post("/api/session/memories/rag-debug")
async def rag_debug_query(request: RagDebugRequest):
    """Dry-run RAG retrieval for the memory browser's debug tab: embed the
    given text and return the ranked memories/world entries with distance
    scores, mirroring what gather_context_node would retrieve this turn."""
    import json as _json
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query text cannot be empty.")
    if not await _ensure_browsing_memory():
        raise HTTPException(status_code=503, detail="No save loaded.")
    limit = max(1, min(50, request.limit))
    turn = session_manager.state.get("turn", 0)

    vector = await _embedding_for_edit(query, "rag_debug")
    memories = engine.memory.search_memories(vector, turn, limit=limit, with_scores=True)
    for m in memories:
        # search_memories keeps entities/topics as stored JSON strings.
        for key in ("entities", "topics"):
            try:
                parsed = _json.loads(m[key]) if isinstance(m[key], str) else m[key]
            except (ValueError, TypeError):
                parsed = []
            m[key] = parsed if isinstance(parsed, list) else []

    # Mirror the location-hint enrichment the engine applies to world queries
    # so vague inputs rank the same way they would in a real turn.
    world_query = query
    world_entries = []
    if engine.memory.has_world_index():
        location_hints = " ".join(filter(None, [
            session_manager.state.get("player_location_region", ""),
            session_manager.state.get("player_location_layer_id", ""),
        ]))
        if location_hints:
            world_query = f"{query} {location_hints}".strip()
            world_vector = await _embedding_for_edit(world_query, "rag_debug_world")
        else:
            world_vector = vector
        world_entries = engine.memory.search_world(world_vector, limit=limit, with_scores=True)

    return {
        "query": query,
        "world_query": world_query,
        "turn": turn,
        "rag_limit": engine.settings.get("memory.rag_limit"),
        "world_rag_limit": engine.settings.get("world.rag_limit"),
        "memories": memories,
        "world_entries": world_entries,
    }


@app.get("/api/llm-inspector/calls")
async def get_llm_inspector_calls(since_id: str = "", limit: int = 50):
    return {"calls": llm_inspector.get_calls(since_id=since_id, limit=limit)}


@app.delete("/api/llm-inspector/calls")
async def clear_llm_inspector_calls():
    llm_inspector.clear()
    return {"cleared": True}


@app.get("/api/logs")
async def get_logs(since_id: int = 0, level: str = "", limit: int = 1000):
    return {"logs": log_store.get_logs(since_id=since_id, level=level, limit=limit)}


@app.delete("/api/logs")
async def clear_logs():
    log_store.clear()
    return {"cleared": True}


class SettingsUpdateRequest(BaseModel):
    settings: dict[str, Any]
    scope: str = "story"


@app.get("/api/settings")
async def get_settings(scope: str = "story"):
    return {"settings": session_manager.get_settings_descriptors(scope=scope)}


@app.put("/api/settings")
async def update_settings(request: SettingsUpdateRequest):
    try:
        result = await session_manager.update_settings(request.settings, scope=request.scope)
        return {"settings": result}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/saves")
async def list_saves():
    return {"saves": session_manager.list_saves()}

def _refuse_session_switch_mid_turn():
    """Creating or loading a save replaces the live session state. A turn can
    outlive its client (the app may be closed mid-generation and the turn
    finishes headless), and `save_completed_turn` writes into whatever save is
    active when it lands — so switching the session mid-turn would bleed the
    finished turn into the wrong story."""
    if chat_hub.turn_running():
        raise HTTPException(
            status_code=409,
            detail="A turn is still generating in the active story. Stop it or wait for it to finish first.",
        )


@app.post("/api/saves")
async def create_save(request: CreateSaveRequest):
    _refuse_session_switch_mid_turn()
    try:
        player_location_node_id = None
        player_location_region = None
        player_location_layer_id = None
        character_module_data = None
        character_data = None

        def _persist_active_modules():
            # Record which modules are active for this save (from the start-screen
            # toggle). Stored under a reserved key the engine reads to skip
            # inactive modules.
            if request.active_modules is not None:
                cfgs = dict(session_manager.state.get("module_configs", {}))
                cfgs["__active_modules__"] = request.active_modules
                session_manager.update_module_configs(cfgs)

        if request.character_id:
            try:
                char_data = character_builder.load_character(request.character_id)
                character_module_data = char_data.get("module_data", {})
                character_data = char_data
            except FileNotFoundError:
                pass

        async def _inherit_lorebooks():
            # A new save inherits the lorebook links of its story source(s):
            # world links first, then scenario links. Must run after the save
            # workspace exists (metadata.json lives there).
            inherited = []
            if request.world_id:
                inherited.extend(lorebook_store.get_links("world", request.world_id))
            if request.scenario_id:
                for lid in lorebook_store.get_links("scenario", request.scenario_id):
                    if lid not in inherited:
                        inherited.append(lid)
            if inherited:
                session_manager.save_manager.update_metadata(
                    request.save_id, {"lorebook_ids": inherited}
                )
                await _sync_lorebooks_for_save(request.save_id)

        # A scenario can be used alone or alongside a world: the world supplies
        # the setting, the scenario supplies (or rewrites) the opening message.
        # Loaded up front so a missing scenario fails before any save is created.
        scenario = None
        if request.scenario_id:
            scenario = scenario_store.load_scenario(request.scenario_id)
            # A modification request is stored on the save's scenario copy and
            # applied by the engine when the intro is generated (over the
            # websocket, so the rewritten opening can stream). Persisting it in
            # the workspace file means it survives a restart between create
            # and intro. The library scenario is never touched.
            if request.scenario_request and request.scenario_request.strip():
                scenario["pending_modification_request"] = request.scenario_request.strip()

        def _persist_scenario():
            # Write the save's scenario copy (parallel to World/world_data.json)
            # and expose it in session state for the intro. Must run after the
            # save workspace exists.
            if scenario is None:
                return
            save_workspace = session_manager.data_dir / "saves" / request.save_id
            scenario_dir = save_workspace / "Scenario"
            scenario_dir.mkdir(parents=True, exist_ok=True)
            import json as _json
            with open(scenario_dir / "scenario.json", "w", encoding="utf-8") as f:
                _json.dump(scenario, f, indent=2, ensure_ascii=False)
            session_manager.state["scenario_data"] = scenario
            # Seed the save's editable story direction from the scenario's
            # themes/tags/pacing. Stored in save metadata so it stays editable
            # per story, independently of the frozen scenario copy.
            style = {key: str(scenario.get(key) or "").strip() for key, _ in STORY_STYLE_FIELDS}
            if any(style.values()):
                session_manager.set_story_style(request.save_id, style)

        if request.world_id:
            # World is an optional story source provided by the wb_worldgen module.
            provider = engine.story_sources.get("world")
            if provider is None:
                raise HTTPException(
                    status_code=400,
                    detail="World generation module is not enabled, but a world was requested.",
                )
            result = await provider(
                save_id=request.save_id,
                source_id=request.world_id,
                start_preference=request.start_preference,
                start_location_node_id=request.start_location_node_id,
                session_manager=session_manager,
                engine=engine,
                character_module_data=character_module_data,
                character_data=character_data,
            )
            _persist_scenario()
            _persist_active_modules()
            await _inherit_lorebooks()
            return {
                "session": session_manager.get_status(),
                "state": result.get("state"),
                "start_location": result.get("start_location"),
            }
        elif request.scenario_id:
            # Basic scenario story source: no world, just a system prompt +
            # optional literal opening message (persisted parallel to world_data).
            state = session_manager.create_save(
                request.save_id,
                character_module_data=character_module_data,
                character_data=character_data,
            )
            _persist_scenario()
            engine.set_memory_path(session_manager.get_memory_path())
            session_manager.state["start_preference"] = request.start_preference
            _persist_active_modules()
            await _inherit_lorebooks()
            return {"session": session_manager.get_status(), "state": state}
        else:
            state = session_manager.create_save(
                request.save_id,
                character_module_data=character_module_data,
                character_data=character_data,
            )
            engine.set_memory_path(session_manager.get_memory_path())
            _persist_active_modules()
            return {"session": session_manager.get_status(), "state": state}

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@app.post("/api/saves/{save_id}/load")
async def load_save(save_id: str):
    if chat_hub.turn_running():
        if save_id == session_manager.active_save_id:
            # Reopening the story whose turn is still generating (the client
            # closed mid-turn and came back). Reloading from disk here would
            # clobber the live session — including the pending input_text, so
            # the player's message would vanish from the transcript when the
            # turn completes. Hand back the live state instead; the websocket
            # intro/sync path re-attaches the client to the running stream.
            return {"session": session_manager.get_status(), "state": session_manager.state}
        _refuse_session_switch_mid_turn()
    try:
        state = session_manager.load_save(save_id)
        engine.set_memory_path(session_manager.get_memory_path())
        _init_world_index_for_save(save_id)
        # Pick up lorebook link/entry changes made while the save was unloaded
        # (fingerprint short-circuits to a no-op when nothing changed).
        await _sync_lorebooks_for_save(save_id)
        return {"session": session_manager.get_status(), "state": state}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@app.get("/api/saves/{save_id}/active-modules")
async def get_save_active_modules(save_id: str):
    """Which modules a save has enabled (None == all modules, e.g. legacy saves)."""
    try:
        return {"active_modules": session_manager.get_save_active_modules(save_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/saves/{save_id}/active-modules")
async def set_save_active_modules(save_id: str, request: ActiveModulesRequest):
    """Edit a save's active module set after creation (without loading it)."""
    try:
        active = session_manager.set_save_active_modules(save_id, request.active_modules)
        return {"active_modules": active}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class BranchRequest(BaseModel):
    new_save_id: Optional[str] = None
    target_turn: Optional[int] = None


class RenameSaveRequest(BaseModel):
    display_name: str


class StoryStyleRequest(BaseModel):
    themes: Optional[str] = ""
    tags: Optional[str] = ""
    pacing: Optional[str] = ""


@app.get("/api/saves/{save_id}/export")
async def export_save(save_id: str, format: str = "md"):
    """Download a save's transcript as markdown, plain text, or JSONL."""
    try:
        content, media_type, filename = session_manager.export_transcript(save_id, format)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/saves/{save_id}/branch")
async def branch_save(save_id: str, request: BranchRequest):
    """Fork a save into a new one, optionally rolled back to `target_turn`."""
    try:
        branch = session_manager.branch_save(save_id, request.new_save_id, request.target_turn)
        return {"branch": branch, "saves": session_manager.list_saves()}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/saves/{save_id}/story-style")
async def get_story_style(save_id: str):
    """The save's editable story direction (themes/tags/pacing)."""
    try:
        return {"story_style": session_manager.get_story_style(save_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/saves/{save_id}/story-style")
async def set_story_style(save_id: str, request: StoryStyleRequest):
    """Edit a save's story direction; injected at depth 0 on every turn."""
    try:
        return {"story_style": session_manager.set_story_style(save_id, request.model_dump())}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/saves/{save_id}/name")
async def rename_save(save_id: str, request: RenameSaveRequest):
    try:
        return session_manager.rename_save(save_id, request.display_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/saves/{save_id}/undo")
async def undo_save(save_id: str, request: UndoTurnRequest):
    if save_id != session_manager.active_save_id:
        raise HTTPException(status_code=409, detail="Undo is only supported for the active save.")
    try:
        state = session_manager.undo_turn(request.target_turn)
        memory_rolled_back = engine.rollback_memory(request.target_turn)
        return {
            "session": session_manager.get_status(),
            "state": state,
            "memory_rolled_back": memory_rolled_back,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class SwipeRequest(BaseModel):
    index: int


@app.post("/api/session/swipe")
async def select_swipe(request: SwipeRequest):
    try:
        state = session_manager.select_swipe(request.index)
        # Keep the vector DB consistent with the rolled-back turn (the variant's
        # own turn-level memory, if any, is not separately restored — see plan).
        meta = session_manager.swipes_meta()
        if meta and meta.get("turn"):
            engine.rollback_memory(meta["turn"] - 1)
        return {"session": session_manager.get_status(), "state": state}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class EditMessageRequest(BaseModel):
    content: str


@app.put("/api/session/messages/{index}")
async def edit_message(index: int, request: EditMessageRequest):
    try:
        state = session_manager.edit_message(index, request.content)
        return {"session": session_manager.get_status(), "state": state}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/session/messages/{index}")
async def delete_message(index: int):
    try:
        state = session_manager.delete_message(index)
        engine.rollback_memory(session_manager.state.get("turn", 0))
        return {"session": session_manager.get_status(), "state": state}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/health")
async def health_check():
    modules = []
    for mod_id, mod_data in registry.get_modules().items():
        manifest = mod_data["manifest"]
        modules.append({
            "id": mod_id,
            "name": manifest.get("name", mod_id),
            "version": manifest.get("version", "unknown"),
            "ui_slots": manifest.get("ui_slots", []),
            "has_settings": bool(manifest.get("settings_schema")),
            "has_prompt_blocks": bool(manifest.get("prompt_blocks")),
        })

    memory_status = {"initialized": False}
    if engine.memory is not None:
        try:
            memory_status = {
                "initialized": True,
                "rows": engine.memory.get_memory_count(),
                "vector_dimension": engine.memory.get_vector_dimension(),
            }
        except Exception as exc:
            memory_status = {
                "initialized": True,
                "error": str(exc),
            }

    env = {
        "LLM_MODE": engine.llm.mode,
        "active_provider": provider_manager.get_active(),
        "STORYTELLER_MODEL": engine.llm.storyteller_model,
        "STORYTELLER_FALLBACK_MODELS": engine.llm.storyteller_fallback_models,
        "READER_MODEL": engine.llm.reader_model,
        "EMBEDDING_MODEL": engine.llm.embedding_model,
        "MODULE_FAST_MODEL": engine.llm.module_fast_model,
    }

    providers_status = {}
    for p in provider_manager.get_all():
        providers_status[p["id"]] = {
            "active": p["active"],
            # Counts keys from the provider config *or* its env var (.env).
            "api_key_set": provider_manager.has_api_key(p["id"]),
        }

    has_provider = (
        env["LLM_MODE"] == "mock"
        or any(p["api_key_set"] for p in providers_status.values())
    )

    return {
        "status": "ok" if has_provider else "missing_api_key",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "modules": modules,
        "module_count": len(modules),
        "llm": env,
        "providers": providers_status,
        "memory": memory_status,
        "session": session_manager.get_status(),
    }

class ScenarioRequest(BaseModel):
    id: Optional[str] = None
    name: str
    scenario_description: Optional[str] = ""
    starting_prompt: Optional[str] = ""
    themes: Optional[str] = ""
    tags: Optional[str] = ""
    pacing: Optional[str] = ""


@app.get("/api/scenarios")
async def list_scenarios():
    return {"scenarios": scenario_store.list_scenarios()}


@app.get("/api/scenarios/{scenario_id}")
async def load_scenario(scenario_id: str):
    try:
        return {"scenario": scenario_store.load_scenario(scenario_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/scenarios")
async def save_scenario(request: ScenarioRequest):
    try:
        return {"scenario": scenario_store.save_scenario(request.model_dump())}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/scenarios/{scenario_id}")
async def delete_scenario(scenario_id: str):
    try:
        scenario_store.delete_scenario(scenario_id)
        return {"deleted": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _init_world_index_for_save(save_id: str):
    if engine.memory is None:
        return
    world_index_path = session_manager.data_dir / "saves" / save_id / "world_index"
    if world_index_path.exists():
        engine.memory.init_world_index(str(world_index_path))


async def _sync_lorebooks_for_save(save_id: str):
    """Embed the save's linked lorebook entries into its world index when the
    linked set (or any linked book) changed since the last sync. Only valid
    for the active save — engine.memory is bound to the active save's paths."""
    meta = session_manager.save_manager.read_core_json(save_id, "metadata.json", {}) or {}
    lorebook_ids = meta.get("lorebook_ids", []) or []
    story_entries = meta.get("story_lorebook_entries", []) or []
    if not lorebook_ids and not story_entries and "lorebook_embed_fingerprint" not in meta:
        # Never had lorebooks: don't create a world index for nothing.
        return
    fingerprint = lorebook_store.embed_fingerprint(lorebook_ids, story_entries)
    if meta.get("lorebook_embed_fingerprint") == fingerprint:
        return
    engine.set_memory_path(session_manager.get_memory_path())
    await engine.ensure_memory()
    world_index_path = session_manager.data_dir / "saves" / save_id / "world_index"
    engine.memory.init_world_index(str(world_index_path))
    books = lorebook_store.resolve_save_lorebooks(lorebook_ids)
    if story_entries:
        books.append(story_entries_book(story_entries))
    count = await engine.memory.embed_lorebooks(books, engine.llm)
    session_manager.save_manager.update_metadata(
        save_id, {"lorebook_embed_fingerprint": fingerprint}
    )
    print(f"[Lorebook] Embedded {count} lorebook entries for save '{save_id}'.")


# === Lorebook Routes ===

_LOREBOOK_LINK_KINDS = {"scenario", "world"}


class LorebookImportRequest(BaseModel):
    data: dict
    name: Optional[str] = None


class LorebookEntryUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    title: Optional[str] = None
    keys: Optional[list[str]] = None
    secondary_keys: Optional[list[str]] = None
    content: Optional[str] = None
    constant: Optional[bool] = None
    # Per-entry sticky override; an explicit null clears it (inherit the book).
    sticky_turns: Optional[int] = None
    # ST '@ depth' injection; an explicit null reverts to normal placement.
    injection_depth: Optional[int] = None


class LorebookUpdateRequest(BaseModel):
    sticky_turns: Optional[int] = None


# Entry-PUT fields where an explicit null is a meaningful "clear this" value
# rather than "leave untouched" (requests use exclude_unset to tell them apart).
_NULLABLE_ENTRY_FIELDS = {"sticky_turns", "injection_depth"}


class LorebookLinksRequest(BaseModel):
    lorebook_ids: list[str]


@app.post("/api/lorebooks/import")
async def import_lorebook(request: LorebookImportRequest):
    try:
        return lorebook_store.import_lorebook(request.data, name=request.name)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/lorebooks")
async def list_lorebooks():
    return {"lorebooks": lorebook_store.list_lorebooks()}


@app.get("/api/lorebooks/links/{kind}/{target_id}")
async def get_lorebook_links(kind: str, target_id: str):
    if kind not in _LOREBOOK_LINK_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid link kind: {kind!r}")
    return {"lorebook_ids": lorebook_store.get_links(kind, target_id)}


@app.put("/api/lorebooks/links/{kind}/{target_id}")
async def set_lorebook_links(kind: str, target_id: str, request: LorebookLinksRequest):
    if kind not in _LOREBOOK_LINK_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid link kind: {kind!r}")
    return {"lorebook_ids": lorebook_store.set_links(kind, target_id, request.lorebook_ids)}


@app.get("/api/lorebooks/{lorebook_id}")
async def get_lorebook(lorebook_id: str):
    try:
        record = lorebook_store.load_lorebook(lorebook_id)
        return {"lorebook": record, "links": lorebook_store.get_reverse_links(lorebook_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/lorebooks/{lorebook_id}")
async def delete_lorebook(lorebook_id: str):
    try:
        lorebook_store.delete_lorebook(lorebook_id)
        return {"deleted": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _resync_active_save_if_uses(lorebook_id: str) -> bool:
    """Re-embed the active save now if it has this book attached, so the edit
    applies from the next turn; other saves pick it up on load via the
    fingerprint check."""
    if not session_manager.active_save_id:
        return False
    meta = session_manager.save_manager.read_core_json(
        session_manager.active_save_id, "metadata.json", {}) or {}
    if lorebook_id not in (meta.get("lorebook_ids", []) or []):
        return False
    await _sync_lorebooks_for_save(session_manager.active_save_id)
    return True


@app.put("/api/lorebooks/{lorebook_id}")
async def update_lorebook(lorebook_id: str, request: LorebookUpdateRequest):
    """Patch book-level settings (currently sticky_turns)."""
    patch = {k: v for k, v in request.model_dump(exclude_unset=True).items()
             if v is not None}
    try:
        record = lorebook_store.update_lorebook(lorebook_id, patch)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    synced = await _resync_active_save_if_uses(lorebook_id)
    return {"lorebook": record, "synced": synced}


@app.put("/api/lorebooks/{lorebook_id}/entries/{uid}")
async def update_lorebook_entry(lorebook_id: str, uid: str, request: LorebookEntryUpdateRequest):
    # exclude_unset so an explicit null clears nullable fields (sticky
    # override, injection depth) while omitted fields stay untouched.
    raw = request.model_dump(exclude_unset=True)
    patch = {k: v for k, v in raw.items() if v is not None or k in _NULLABLE_ENTRY_FIELDS}
    try:
        record = lorebook_store.update_entry(lorebook_id, uid, patch)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    synced = await _resync_active_save_if_uses(lorebook_id)
    return {"lorebook": record, "synced": synced}


@app.get("/api/saves/{save_id}/lorebooks")
async def get_save_lorebooks(save_id: str):
    meta = session_manager.save_manager.read_core_json(save_id, "metadata.json", None)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Save '{save_id}' not found.")
    return {
        "lorebook_ids": meta.get("lorebook_ids", []),
        "story_entries": meta.get("story_lorebook_entries", []) or [],
    }


@app.put("/api/saves/{save_id}/lorebooks")
async def set_save_lorebooks(save_id: str, request: LorebookLinksRequest):
    ids = [lid for lid in dict.fromkeys(request.lorebook_ids) if lorebook_store.exists(lid)]
    try:
        session_manager.save_manager.update_metadata(save_id, {"lorebook_ids": ids})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # Embedding only works for the active save; inactive saves re-embed on
    # next load via the fingerprint check.
    if save_id == session_manager.active_save_id:
        await _sync_lorebooks_for_save(save_id)
    return {"lorebook_ids": ids}


# ── free-standing story entries ──────────────────────────────────────────────
# Lorebook entries that belong to a single save rather than an imported book.
# Stored in the save's metadata; embedded via the same path as book entries
# (source ids '__story__:{uid}'), so keywords, constant injection, and the
# enabled flag behave identically.

class StoryEntryRequest(BaseModel):
    content: Optional[str] = None
    title: Optional[str] = None
    keys: Optional[list[str]] = None
    secondary_keys: Optional[list[str]] = None
    constant: Optional[bool] = None
    enabled: Optional[bool] = None
    sticky_turns: Optional[int] = None
    injection_depth: Optional[int] = None


def _read_story_entries(save_id: str) -> list:
    meta = session_manager.save_manager.read_core_json(save_id, "metadata.json", None)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Save '{save_id}' not found.")
    return list(meta.get("story_lorebook_entries", []) or [])


async def _write_story_entries(save_id: str, entries: list) -> None:
    session_manager.save_manager.update_metadata(
        save_id, {"story_lorebook_entries": entries})
    # Embedding only works for the active save; inactive saves re-embed on
    # next load via the fingerprint check.
    if save_id == session_manager.active_save_id:
        await _sync_lorebooks_for_save(save_id)


@app.post("/api/saves/{save_id}/lorebooks/entries")
async def add_story_lorebook_entry(save_id: str, request: StoryEntryRequest):
    entries = _read_story_entries(save_id)
    data = {k: v for k, v in request.model_dump().items() if v is not None}
    try:
        entry = make_story_entry(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    entries.append(entry)
    await _write_story_entries(save_id, entries)
    return {"entry": entry, "story_entries": entries}


@app.put("/api/saves/{save_id}/lorebooks/entries/{uid}")
async def update_story_lorebook_entry(save_id: str, uid: str, request: StoryEntryRequest):
    entries = _read_story_entries(save_id)
    # exclude_unset so an explicit `"injection_depth": null` reverts the entry
    # to normal placement while omitted fields stay untouched.
    raw = request.model_dump(exclude_unset=True)
    patch = {k: v for k, v in raw.items() if v is not None or k == "injection_depth"}
    for i, entry in enumerate(entries):
        if str(entry.get("uid")) == uid:
            try:
                entries[i] = patch_story_entry(entry, patch)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            updated = entries[i]
            break
    else:
        raise HTTPException(
            status_code=404,
            detail=f"Story entry '{uid}' not found in save '{save_id}'.")
    await _write_story_entries(save_id, entries)
    return {"entry": updated, "story_entries": entries}


@app.delete("/api/saves/{save_id}/lorebooks/entries/{uid}")
async def delete_story_lorebook_entry(save_id: str, uid: str):
    entries = _read_story_entries(save_id)
    remaining = [e for e in entries if str(e.get("uid")) != uid]
    if len(remaining) == len(entries):
        raise HTTPException(
            status_code=404,
            detail=f"Story entry '{uid}' not found in save '{save_id}'.")
    await _write_story_entries(save_id, remaining)
    return {"story_entries": remaining, "deleted": True}



class RevealNodeRequest(BaseModel):
    node_id: str


@app.post("/api/session/reveal-node")
async def session_reveal_node(request: RevealNodeRequest):
    revealed = list(session_manager.state.get("revealed_node_ids", []))
    if request.node_id not in revealed:
        revealed.append(request.node_id)
    session_manager.state["revealed_node_ids"] = revealed
    return {"revealed_node_ids": revealed}


# === Character Builder Routes ===

class GenerateNameRequest(BaseModel):
    context: dict = {}
    gender: str = ""
    race: str = ""
    seed: str = ""


class GenerateAppearanceRequest(BaseModel):
    short_description: str = ""
    context: dict = {}
    gender: str = ""
    race: str = ""
    name: str = ""


class GenerateRaceRequest(BaseModel):
    context: dict = {}
    gender: str = ""
    seed: str = ""


class GenerateStatsRequest(BaseModel):
    concept: str = ""
    context: dict = {}
    gender: str = ""
    race: str = ""
    name: str = ""
    short_appearance: str = ""
    full_appearance: str = ""


class SaveCharacterRequest(BaseModel):
    id: str
    name: str = ""
    gender: str = ""
    race: str = ""
    short_appearance: str = ""
    full_appearance: str = ""
    context: dict = {}
    module_data: dict = {}


class WorldContextRequest(BaseModel):
    context: dict = {}


@app.get("/api/character/list")
async def list_characters():
    return {"characters": character_builder.list_characters()}


@app.post("/api/character/generate-name")
async def generate_character_name(request: GenerateNameRequest):
    result = await character_builder.generate_name(
        context=request.context,
        gender=request.gender,
        race=request.race,
        seed=request.seed,
    )
    return result


@app.post("/api/character/generate-appearance")
async def generate_character_appearance(request: GenerateAppearanceRequest):
    result = await character_builder.generate_full_appearance(
        short_desc=request.short_description,
        context=request.context,
        gender=request.gender,
        race=request.race,
        name=request.name,
    )
    return result


@app.post("/api/character/generate-race")
async def generate_character_race(request: GenerateRaceRequest):
    result = await character_builder.generate_race(
        context=request.context,
        gender=request.gender,
        seed=request.seed,
    )
    return result


@app.post("/api/character/generate-stats")
async def generate_character_stats(request: GenerateStatsRequest):
    result = await character_builder.generate_stats(
        concept=request.concept,
        context=request.context,
        gender=request.gender,
        race=request.race,
        name=request.name,
        short_appearance=request.short_appearance,
        full_appearance=request.full_appearance,
    )
    return result


@app.post("/api/character/save")
async def save_character(request: SaveCharacterRequest):
    try:
        char_id = character_builder.save_character(request.id, {
            "name": request.name,
            "gender": request.gender,
            "race": request.race,
            "short_appearance": request.short_appearance,
            "full_appearance": request.full_appearance,
            "context": request.context,
            "module_data": request.module_data,
        })
        return {"id": char_id, "saved": True}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/character/load/{character_id}")
async def load_character(character_id: str):
    try:
        return character_builder.load_character(character_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/saves/{save_id}")
async def delete_save(save_id: str):
    try:
        session_manager.delete_save(save_id)
        return {"deleted": save_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/character/{character_id}")
async def delete_character(character_id: str):
    try:
        character_builder.delete_character(character_id)
        return {"deleted": character_id}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class ModuleDefaultsRequest(BaseModel):
    context: dict = {}


@app.post("/api/character/module-defaults")
async def get_character_module_defaults(request: ModuleDefaultsRequest):
    defaults = {}
    for mod_id, mod_data in registry.get_modules().items():
        manifest = mod_data.get("manifest", {})
        cc = manifest.get("character_creation", {})
        if cc:
            default_state = cc.get("default_state", {})
            if default_state:
                defaults[mod_id] = default_state

            backend = mod_data.get("backend")
            if backend and hasattr(backend, "on_character_get_defaults"):
                try:
                    # Generic module-contributed generation context (e.g. the
                    # world module reads context["world_id"]); modules ignore keys
                    # they don't own.
                    custom_defaults = await backend.on_character_get_defaults({}, request.context)
                    if isinstance(custom_defaults, dict):
                        defaults[mod_id] = custom_defaults
                except Exception as e:
                    print(f"[Character] Error getting defaults from {mod_id}: {e}")

    return {"module_defaults": defaults}


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket client connected.")
    chat_hub.attach(websocket)

    async def handle_intro():
        state = session_manager.state
        if len(state.get("history", [])) == 0:
            # New story: seed module_data by running each module's
            # on_gather_context before generating the opening scene.
            #
            # This pass is deliberately skipped for an already-started story.
            # There it would only re-run every module's per-turn context work
            # (including the NPC system's introduction + scene-presence LLM
            # calls) and immediately discard the result -- the else branch below
            # just replays the saved transcript, and module_data already came
            # from the loaded save. Since handle_intro fires on every load,
            # resume and branch, that made those NPC checks run in the
            # background on each open, burning tokens for nothing. The hooks
            # still run normally inside a real turn (gather_context_node).
            state = await engine.initialize_module_data(state)
            session_manager.state = state
            await engine.ensure_memory()
            engine.set_memory_path(session_manager.get_memory_path())
            _init_world_index_for_save(session_manager.active_save_id)
            try:
                intro_result = await engine.generate_intro(
                    state,
                    streaming_callback=_stream_token,
                )
                intro_text = intro_result["content"]
                intro_reasoning = intro_result.get("reasoning", "")
                ai_message = {"role": "ai", "content": intro_text}
                if intro_reasoning:
                    ai_message["reasoning"] = intro_reasoning
                ai_message["meta"] = session_manager.build_message_meta(
                    intro_result.get("model"), intro_result.get("usage")
                )
                state["history"] = [intro_text]
                state["chat_messages"] = [ai_message]
                session_manager.save_manager.save_turn(
                    session_manager.active_save_id, state, 0
                )
                # The engine may have rewritten the scenario to satisfy a
                # modification request; the workspace copy is what save-load
                # restores scenario_data from, so it must reflect the rewrite.
                scenario_data = state.get("scenario_data")
                if isinstance(scenario_data, dict) and scenario_data.get("modified_by_request"):
                    scenario_file = (
                        session_manager.data_dir / "saves" / session_manager.active_save_id
                        / "Scenario" / "scenario.json"
                    )
                    if scenario_file.parent.exists():
                        import json as _json
                        with open(scenario_file, "w", encoding="utf-8") as f:
                            _json.dump(scenario_data, f, indent=2, ensure_ascii=False)
            except Exception as exc:
                print(f"Error generating intro: {exc}")
                await chat_hub.send_or_queue({
                    "type": "error",
                    "code": "intro_failed",
                    "message": "Failed to generate the opening scene.",
                    "detail": str(exc),
                    "state": state,
                })
                return
        else:
            await chat_hub.send({
                "type": "state_load",
                "chat_messages": state.get("chat_messages", []),
            })
            # A client that reopened the story missed any terminal event from
            # a turn that ended while it was gone (error, stopped turn).
            await chat_hub.flush_undelivered()
        state["swipes"] = session_manager.swipes_meta()
        await chat_hub.send({"type": "done", "state": state})

    def restore_after_aborted_regenerate():
        # `prepare_regenerate` rolls the workspace back to before the last turn,
        # so a stop or failure mid-generation must put the previously active
        # swipe variant back — otherwise the last user+ai pair silently
        # disappears from the transcript.
        try:
            session_manager.restore_active_swipe()
        except Exception as exc:
            print(f"Failed to restore active swipe after aborted regenerate: {exc}")

    async def handle_regenerate():
        # Re-run the most recent turn, keeping each generation as a swipe.
        try:
            regen_turn = session_manager.prepare_regenerate()
        except (ValueError, FileNotFoundError) as exc:
            await chat_hub.send({
                "type": "error", "code": "regenerate_unavailable",
                "message": str(exc), "detail": str(exc),
                "state": session_manager.state,
            })
            return
        # Capture the re-seated input now: the live session state could be
        # reloaded while this turn generates (client closed and reopened the
        # story), which would blank input_text before the save.
        regen_input = session_manager.state.get("input_text", "")
        try:
            engine.rollback_memory(regen_turn - 1)
            engine.set_memory_path(session_manager.get_memory_path())
            _init_world_index_for_save(session_manager.active_save_id)
            final_state = await engine.app.ainvoke(session_manager.state)
            active_state = session_manager.save_completed_turn(final_state, user_text=regen_input)
            session_manager.add_regenerated_swipe()
            active_state["swipes"] = session_manager.swipes_meta()
            await chat_hub.send({"type": "done", "state": active_state})
        except asyncio.CancelledError:
            restore_after_aborted_regenerate()
            raise
        except LLMProviderError as exc:
            restore_after_aborted_regenerate()
            await chat_hub.send_or_queue({
                "type": "error", "code": "llm_provider_unavailable",
                "message": "The AI provider is temporarily unavailable. Please try again in a moment.",
                "detail": str(exc), "state": session_manager.state,
            })
        except Exception as exc:
            print(f"Unexpected error during regenerate: {exc}")
            restore_after_aborted_regenerate()
            await chat_hub.send_or_queue({
                "type": "error", "code": "regenerate_failed",
                "message": "Regeneration failed.", "detail": str(exc),
                "state": session_manager.state,
            })

    async def handle_turn(data):
        # Update state with user input
        text = data.get("text", "")
        if engine.settings.get("storyteller.auto_mode"):
            # Storyteller auto mode: the AI plays the player. Whatever was
            # typed stays out of the story and only steers the generated action,
            # which then runs as a completely normal player turn. On generation
            # failure fall back to treating the typed text as the action.
            generated = await engine.generate_auto_player_action(session_manager.state, text)
            if generated:
                text = generated
                # The generated action is what actually enters the story, so a
                # reconnect snapshot must echo it, not the typed guidance.
                chat_hub.turn_input = generated
                # Show the generated action in the client as the user message
                # (replacing any locally echoed guidance text).
                await chat_hub.send({"type": "player_action", "content": generated})
        session_manager.set_input(text)
        engine.set_memory_path(session_manager.get_memory_path())
        # ensure_memory first: after a server restart engine.memory is None and
        # _init_world_index_for_save would silently skip, dropping world RAG and
        # constant lore for the first turn.
        if engine.memory_db_path:
            await engine.ensure_memory()
        _init_world_index_for_save(session_manager.active_save_id)

        try:
            # Execute the LangGraph pipeline
            final_state = await engine.app.ainvoke(session_manager.state)
            active_state = session_manager.save_completed_turn(final_state, user_text=text)
            session_manager.begin_turn_swipes()
            active_state["swipes"] = session_manager.swipes_meta()

            # Send final completion signal with the updated state. If the
            # client is gone this is a no-op: the turn is already saved, so a
            # later sync replays it from authoritative state.
            await chat_hub.send({"type": "done", "state": active_state})
        except LLMProviderError as exc:
            print(f"LLM provider error during WebSocket turn: {exc}")
            session_manager.set_input("")
            await chat_hub.send_or_queue({
                "type": "error",
                "code": "llm_provider_unavailable",
                "message": "The AI provider is temporarily unavailable. Please try again in a moment.",
                "detail": str(exc),
                "state": session_manager.state,
            })
        except Exception as exc:
            print(f"Unexpected error during WebSocket turn: {exc}")
            session_manager.set_input("")
            await chat_hub.send_or_queue({
                "type": "error",
                "code": "turn_failed",
                "message": "The turn failed before it could be saved.",
                "detail": str(exc),
                "state": session_manager.state,
            })

    def apply_command_writebacks(result: dict, mod_id: str, manifest: dict):
        """Merge a command handler's sanctioned state write-backs.

        Commands may return the same keys the librarian node collects from
        on_librarian hooks: whitelisted player-identity fields under
        ``character_update``, and the module's OWN ``module_data`` subtree
        (gated on the manifest's ``produces.module_data``). Everything else in
        the result is ignored — commands cannot touch other modules' data.
        """
        state = session_manager.state
        update = result.get("character_update")
        if isinstance(update, dict):
            player = state.get("characters", {}).get("default_player")
            if isinstance(player, dict):
                applied = {k: update[k] for k in CHARACTER_UPDATE_FIELDS if update.get(k)}
                if applied:
                    player.update(applied)
                    print(f"[Command] {mod_id}: player character updated: {', '.join(applied.keys())}")

        own_data = (result.get("module_data") or {}).get(mod_id)
        if isinstance(own_data, dict) and manifest.get("produces", {}).get("module_data"):
            module_data = state.setdefault("module_data", {})
            merged = engine._deep_merge(module_data.get(mod_id) or {}, own_data)
            # Deep-merge is additive and can't delete a dict entry (e.g. removing
            # a character from the NPC bank). A handler may list top-level keys of
            # its own subtree to overwrite wholesale with the authoritative value
            # it returned, so removals actually take effect.
            for key in result.get("module_data_replace") or []:
                if key in own_data:
                    merged[key] = own_data[key]
            module_data[mod_id] = merged

    async def try_handle_command(data) -> bool:
        """Route ``/command`` inputs to module handlers declared in manifests.

        Command turns bypass the story pipeline entirely. Their output is
        ephemeral: it is surfaced to the client as a ``command_result`` popup
        rather than written into the transcript, so status readouts don't clutter
        the story. Any state the handler wrote back (module_data, player fields)
        is persisted and pushed to the client via ``state_update`` so widgets
        refresh. Unknown commands fall through to a normal turn.
        """
        text = (data.get("text") or "").strip()
        if not text.startswith("/"):
            return False

        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        state = session_manager.state
        active = state.get("module_configs", {}).get("__active_modules__")
        active_set = set(active) if isinstance(active, list) else None

        for mod_id, mod_data in registry.get_modules().items():
            if active_set is not None and mod_id not in active_set:
                continue
            manifest = mod_data["manifest"]
            handler_name = (manifest.get("commands") or {}).get(command)
            if not handler_name:
                continue
            handler = getattr(mod_data["backend"], handler_name, None)
            if handler is None:
                continue

            module_state = engine._build_module_state(state, mod_id, manifest.get("consumes", {}))
            try:
                engine.sdk.llm._current_module = mod_id
                result = await handler(args, module_state, engine.sdk)
                message = result.get("message", "") if isinstance(result, dict) else ""
                if isinstance(result, dict):
                    apply_command_writebacks(result, mod_id, manifest)
            except Exception as exc:
                print(f"Error in {mod_id}.{handler_name}: {exc}")
                message = f"[{manifest.get('name', mod_id)}] Command failed."
            finally:
                engine.sdk.llm._current_module = ""

            # Command output is ephemeral — it goes to a popup, not the
            # transcript. Persist any writebacks the handler made and push the
            # refreshed state so widgets (inventory, image trigger…) update.
            session_manager.save_manager.save_turn(
                session_manager.active_save_id, state, state.get("turn", 0)
            )
            # Command output never enters the transcript, so state replay can't
            # resurface it — queue it if the client is gone.
            await chat_hub.send_or_queue({
                "type": "command_result",
                "command": text,
                "name": manifest.get("name", mod_id),
                "icon": manifest.get("icon"),
                "message": message or "(no output)",
            })
            await chat_hub.send({"type": "state_update", "state": dict(state)})
            return True

        return False

    async def run_action(data):
        action = data.get("action", "turn")
        # Every action generates into the active save; without one there is
        # nowhere to save, so refuse instead of failing mid-pipeline.
        if session_manager.active_save_id is None:
            await chat_hub.send({
                "type": "error",
                "code": "no_active_save",
                "message": "No story is loaded. Create or load a story first.",
                "state": session_manager.state,
            })
            return
        try:
            if action == "intro":
                await handle_intro()
            elif action == "regenerate":
                await handle_regenerate()
            else:
                if not await try_handle_command(data):
                    await handle_turn(data)
        except asyncio.CancelledError:
            # User pressed stop. For a normal turn nothing was saved, so the
            # turn simply didn't happen; a regenerate has already restored the
            # previously active variant. Echo the input back (empty for
            # regenerate) so the client can restore the composer.
            print("Turn cancelled by client stop request.")
            session_manager.set_input("")
            state = dict(session_manager.state)
            state["swipes"] = session_manager.swipes_meta()
            await chat_hub.send_or_queue({
                "type": "turn_stopped",
                "input": data.get("text", ""),
                "state": state,
            })
            raise
        except Exception as exc:
            # The handlers report their own errors, so this is reached only by
            # failures outside their try blocks. Still tell the client — a
            # silent task death would leave it waiting for a `done` that never
            # comes.
            print(f"Unhandled error in turn task: {exc}")
            await chat_hub.send_or_queue({
                "type": "error",
                "code": "turn_failed",
                "message": "The turn failed unexpectedly.",
                "detail": str(exc),
                "state": session_manager.state,
            })

    # Turns run as a cancellable task (held by the hub, not this connection)
    # so the receive loop stays responsive: a {"action": "stop"} message can
    # interrupt generation mid-stream — and the task outlives this socket if
    # the client drops.
    try:
        while True:
            data = await websocket.receive_json()
            print(f"Received from client: {data}")

            action = data.get("action", "turn")

            if action == "stop":
                if chat_hub.turn_running():
                    chat_hub.turn_task.cancel()
                continue

            if action == "sync":
                # Reconnect recovery: replay the authoritative transcript and
                # state without generating anything. Cheap, so it runs inline.
                state = dict(session_manager.state)
                state["swipes"] = session_manager.swipes_meta()
                await chat_hub.send({
                    "type": "state_load",
                    "chat_messages": state.get("chat_messages", []),
                })
                await chat_hub.flush_undelivered()
                if chat_hub.turn_running():
                    # The turn the old socket started is still generating.
                    # Repaint it and let its live stream (already redirected
                    # here by the hub) continue; its own done/error terminates
                    # the client's generating state, so no `done` now.
                    await chat_hub.send(chat_hub.snapshot())
                else:
                    await chat_hub.send({"type": "done", "state": state})
                continue

            if chat_hub.turn_running():
                if action == "intro":
                    # A page reload mid-generation boots with a quiet intro.
                    # Answer like a sync instead of `busy`, so the client
                    # re-attaches to the running turn instead of erroring.
                    state = dict(session_manager.state)
                    await chat_hub.send({
                        "type": "state_load",
                        "chat_messages": state.get("chat_messages", []),
                    })
                    await chat_hub.send(chat_hub.snapshot())
                else:
                    await chat_hub.send({
                        "type": "error",
                        "code": "busy",
                        "message": "A turn is already in progress.",
                    })
                continue

            chat_hub.begin_turn(action, data.get("text", ""))
            chat_hub.turn_task = asyncio.create_task(run_action(data))

    except WebSocketDisconnect:
        print("WebSocket client disconnected.")
    finally:
        # Deliberately do NOT cancel the running turn here: generation must
        # survive a disconnect so the finished turn is saved and waiting when
        # the client returns. Only an explicit stop cancels it.
        chat_hub.detach(websocket)


# === Provider Management Routes ===

class ProviderUpdateRequest(BaseModel):
    config: dict[str, Any]

class PresetApplyRequest(BaseModel):
    preset: str


@app.get("/api/providers")
async def get_providers():
    return {"providers": provider_manager.get_all()}


@app.get("/api/providers/active")
async def get_active_provider():
    pid = provider_manager.get_active()
    return {"active": pid, "config": provider_manager.get_config(pid)}


@app.put("/api/providers/active")
async def set_active_provider(body: dict):
    provider_id = body.get("provider_id", "")
    if not provider_id:
        raise HTTPException(status_code=400, detail="provider_id is required")
    try:
        provider_manager.set_active(provider_id)
        return {"active": provider_manager.get_active(), "config": provider_manager.get_config(provider_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/providers/{provider_id}/config")
async def get_provider_config(provider_id: str):
    try:
        return {"id": provider_id, "config": provider_manager.get_config(provider_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.put("/api/providers/{provider_id}/config")
async def update_provider_config(provider_id: str, request: ProviderUpdateRequest):
    try:
        provider_manager.save_config(provider_id, request.config)
        return {"id": provider_id, "config": provider_manager.get_config(provider_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/providers/{provider_id}/test")
async def test_provider_connection(provider_id: str):
    try:
        result = await provider_manager.test_connection(provider_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/providers/{provider_id}/models")
async def fetch_provider_models(provider_id: str):
    try:
        result = await provider_manager.fetch_models(provider_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/providers/{provider_id}/preset")
async def apply_provider_preset(provider_id: str, request: PresetApplyRequest):
    try:
        config = provider_manager.apply_preset(provider_id, request.preset)
        return {"id": provider_id, "config": config}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
