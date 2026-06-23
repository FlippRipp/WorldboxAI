from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from backend.engine.registry import ModuleRegistry
from backend.engine.graph import EngineGraph
from backend.engine.llm import LLMProviderError
from backend.engine.llm_inspector import LLMInspector
from backend.engine.session import GameSessionManager
from backend.engine.settings_registry import SettingsRegistry
from backend.engine.world_builder import WorldBuilder, PipelineStep, register_default_steps
from backend.engine.character_builder import CharacterBuilder
from backend.engine.provider_manager import ProviderManager
from backend.engine.prompt_library import PromptLibrary, get_default_library_path
from backend.engine.prompt_pipeline import AVAILABLE_MACROS, default_prompt_pipeline, ALLOWED_ROLES, ALLOWED_PLACEMENTS, ALLOWED_BLOCK_TYPES
from backend.engine.st_importer import SillyTavernImporter
from dotenv import load_dotenv
from pydantic import BaseModel
import os
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
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

llm_inspector = LLMInspector()
engine.llm.set_inspector(llm_inspector)


class CreateSaveRequest(BaseModel):
    save_id: str
    world_id: Optional[str] = None
    start_preference: Optional[str] = None
    character_id: Optional[str] = None


class UndoTurnRequest(BaseModel):
    target_turn: int


class ModuleConfigsRequest(BaseModel):
    module_configs: dict


class PromptPipelineRequest(BaseModel):
    prompt_pipeline: list[dict]


world_builder = WorldBuilder()
world_builder.set_llm_service(engine.llm)
world_builder.set_settings(backend_settings)
world_builder.register_module_hooks(registry)

# The 10 built-in pipeline steps are now self-contained modules under
# backend/engine/worldgen/steps/. Registering them is a single call; adding or
# removing a step is done by editing that package, not this file.
register_default_steps(world_builder)

# Extend world rules schema from module hooks
for mod_id, hook in world_builder._module_hooks.get("on_world_rules_schema", []):
    try:
        extra_fields = hook({}, None)
        if isinstance(extra_fields, dict):
            world_rules_step = world_builder._steps.get("world_rules")
            if world_rules_step:
                world_rules_step.schema.setdefault("module_data", {"type": "object", "label": "Module Data"})
                mod_schema = world_rules_step.schema["module_data"].setdefault("properties", {})
                mod_schema[mod_id] = {"type": "object", "label": f"{mod_id} Rules", "properties": extra_fields}
    except Exception as e:
        logger.warning("Module %s on_world_rules_schema failed: %s", mod_id, e)

world_gen_sessions: dict[str, dict[str, Any]] = {}
world_draft_ids: dict[str, str] = {}

def _get_world_state(session_id: str = "default") -> dict:
    return world_gen_sessions.setdefault(session_id, {})

def _get_world_draft_id(session_id: str = "default") -> str | None:
    return world_draft_ids.get(session_id)

def _auto_save_draft(session_id: str = "default"):
    state = _get_world_state(session_id)
    draft_id = _get_world_draft_id(session_id)
    try:
        if state.get("steps"):
            draft_id = world_builder.save_draft(draft_id or "", state)
            world_draft_ids[session_id] = draft_id
            state["_draft_id"] = draft_id
    except Exception:
        pass  # silently fail — draft is best-effort

character_builder = CharacterBuilder()
character_builder.set_llm_service(engine.llm)
character_builder.set_settings(backend_settings)
character_builder.set_world_builder(world_builder)

for mod_id, mod_data in registry.get_modules().items():
    mod_path = mod_data["path"]
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
    """Serve any .jsx file from a module's directory (e.g. widget_settings.jsx)."""
    if not filename.endswith(".jsx"):
        raise HTTPException(status_code=404, detail="Only .jsx files are served.")
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="Invalid filename.")
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
                file_path = os.path.join(candidate, filename)
                if os.path.isfile(file_path):
                    from fastapi.responses import FileResponse
                    return FileResponse(file_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        # Also try folder-name matching
        if os.path.basename(candidate) == mod_id or os.path.basename(candidate).replace("wb_", "") == mod_id:
            file_path = os.path.join(candidate, filename)
            if os.path.isfile(file_path):
                from fastapi.responses import FileResponse
                return FileResponse(file_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    raise HTTPException(status_code=404, detail=f"File {filename} not found for module {mod_id}.")

@app.get("/api/modules")
async def get_modules():
    modules = []
    for mod_id, mod_data in registry.get_modules().items():
        manifest = mod_data["manifest"]
        modules.append({
            "id": mod_id,
            "name": manifest.get("name", mod_id),
            "ui_slots": manifest.get("ui_slots", []),
            "settings_schema": manifest.get("settings_schema", {}),
            "prompt_blocks": manifest.get("prompt_blocks", []),
            "modes": manifest.get("modes", []),
            "has_character_creation": bool(manifest.get("character_creation")),
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
    state = session_manager.update_module_configs(request.module_configs)
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
        return {"prompt_pipeline": normalized}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/global-prompt-pipeline/reset")
async def reset_global_prompt_pipeline():
    session_manager.save_manager.save_global_prompt_pipeline(default_prompt_pipeline())
    return {"prompt_pipeline": default_prompt_pipeline()}

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


@app.get("/api/session/memories")
async def get_memories():
    if engine.memory is None:
        return {"memories": [], "count": 0, "active_ids": [], "context_query": ""}
    memories = engine.memory.list_all_memories(limit=50)
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


@app.get("/api/llm-inspector/calls")
async def get_llm_inspector_calls(since_id: str = "", limit: int = 50):
    return {"calls": llm_inspector.get_calls(since_id=since_id, limit=limit)}


@app.delete("/api/llm-inspector/calls")
async def clear_llm_inspector_calls():
    llm_inspector.clear()
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

@app.post("/api/saves")
async def create_save(request: CreateSaveRequest):
    try:
        player_location_node_id = None
        player_location_region = None
        player_location_layer_id = None
        character_module_data = None

        if request.character_id:
            try:
                char_data = character_builder.load_character(request.character_id)
                character_module_data = char_data.get("module_data", {})
            except FileNotFoundError:
                pass

        if request.world_id:
            world_state = world_builder.load_world(request.world_id)
            compiled = world_builder.compile_world(world_state)

            # Build adjacency for initial fog-of-war reveal
            def _initial_adjacency(wd):
                edges = wd.get("map", {}).get("edges", [])
                map_layers = wd.get("map_layers", [])
                all_edges = list(edges)
                if map_layers:
                    all_edges = []
                    for layer in map_layers:
                        all_edges.extend(layer.get("map", {}).get("edges", []))
                adj = {}
                for e in all_edges:
                    fr, to = e.get("from"), e.get("to")
                    if fr and to:
                        adj.setdefault(fr, []).append(to)
                        adj.setdefault(to, []).append(fr)
                return adj

            if request.start_preference:
                start_location = await world_builder.llm_pick_start_location(
                    request.world_id, request.start_preference, engine.llm
                )
            else:
                locations = world_builder.get_start_locations(request.world_id)
                import random as _random
                start_location = _random.choice(locations) if locations else None

            revealed_node_ids = []
            if start_location:
                player_location_node_id = start_location.get("node_id")
                player_location_region = start_location.get("region")
                player_location_layer_id = start_location.get("layer_id")

                adjacency = _initial_adjacency(compiled)
                revealed = {player_location_node_id}
                frontier = [player_location_node_id]
                for _ in range(1):
                    next_frontier = []
                    for nid in frontier:
                        for nb in adjacency.get(nid, []):
                            if nb not in revealed:
                                revealed.add(nb)
                                next_frontier.append(nb)
                    frontier = next_frontier
                revealed_node_ids = list(revealed)

            state = session_manager.create_save(
                request.save_id,
                world_id=request.world_id,
                player_location_node_id=player_location_node_id,
                player_location_region=player_location_region,
                player_location_layer_id=player_location_layer_id,
                revealed_node_ids=revealed_node_ids,
                character_module_data=character_module_data,
            )

            save_workspace = session_manager.data_dir / "saves" / request.save_id
            world_dir = save_workspace / "World"
            world_dir.mkdir(parents=True, exist_ok=True)
            import json as _json
            with open(world_dir / "world_data.json", "w", encoding="utf-8") as f:
                _json.dump(compiled, f, indent=2)

            engine.set_memory_path(session_manager.get_memory_path())
            await engine.ensure_memory()
            world_index_path = str(save_workspace / "world_index")
            engine.memory.init_world_index(world_index_path)
            entry_count = await engine.memory.embed_world(compiled, engine.llm)
            print(f"[Server] Embedded {entry_count} world entries for world '{request.world_id}'")

            session_manager.state["world_data"] = compiled
            session_manager.state["world_id"] = request.world_id
            session_manager.state["player_location_node_id"] = player_location_node_id
            session_manager.state["player_location_region"] = player_location_region
            session_manager.state["start_preference"] = request.start_preference

            return {
                "session": session_manager.get_status(),
                "state": state,
                "start_location": start_location,
            }
        else:
            state = session_manager.create_save(
                request.save_id,
                character_module_data=character_module_data,
            )
            engine.set_memory_path(session_manager.get_memory_path())
            return {"session": session_manager.get_status(), "state": state}

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

@app.post("/api/saves/{save_id}/load")
async def load_save(save_id: str):
    try:
        state = session_manager.load_save(save_id)
        engine.set_memory_path(session_manager.get_memory_path())
        _init_world_index_for_save(save_id)
        return {"session": session_manager.get_status(), "state": state}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

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
            vector_field = engine.memory.table.schema.field("vector")
            memory_status = {
                "initialized": True,
                "rows": engine.memory.table.count_rows(),
                "vector_dimension": getattr(vector_field.type, "list_size", None),
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
    }

    providers_status = {}
    for p in provider_manager.get_all():
        config = provider_manager.get_config(p["id"])
        providers_status[p["id"]] = {
            "active": p["active"],
            "api_key_set": bool(config.get("api_key", "")),
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

# === World Builder Routes ===

class WorldGenerateRequest(BaseModel):
    seed_prompt: str
    skip_review: bool = False


@app.get("/api/world/pipeline")
async def get_world_pipeline():
    return {"pipeline": world_builder.get_pipeline()}


@app.post("/api/world/generate")
async def generate_world(request: WorldGenerateRequest, session_id: str = "default"):
    state = {"seed_prompt": request.seed_prompt, "steps": {}}
    world_gen_sessions[session_id] = state

    if request.skip_review:
        enrichment_steps = {"node_labeling", "node_descriptions"}
        for step_id in world_builder._ordered_ids:
            if step_id in enrichment_steps:
                continue
            data = await world_builder.generate_step(step_id, state, request.seed_prompt)
            state["steps"][step_id] = {"data": data, "approved": True}
        state["complete"] = True
        return {"state": state, "complete": True}

    first_step = world_builder._ordered_ids[0] if world_builder._ordered_ids else None
    if not first_step:
        raise HTTPException(status_code=500, detail="No world building steps registered.")

    data = await world_builder.generate_step(first_step, state, request.seed_prompt)
    state["steps"][first_step] = {"data": data, "approved": False}
    state["current_step"] = first_step
    return {"state": state, "current_step": first_step}


@app.post("/api/world/generate-step/{step_id}")
async def generate_world_step(step_id: str, body: dict = None, session_id: str = "default"):
    state = _get_world_state(session_id)

    if step_id not in world_builder._steps:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_id}")

    note = body.get("note", "") if body else ""
    config = body.get("data", None) if body else None

    data = await world_builder.generate_step(
        step_id, state,
        state.get("seed_prompt", ""),
        user_note=note,
        config=config,
    )
    state["steps"][step_id] = {"data": data, "approved": False, "note": note}

    current_idx = world_builder._ordered_ids.index(step_id)
    for idx in range(current_idx + 1, len(world_builder._ordered_ids)):
        downstream_id = world_builder._ordered_ids[idx]
        if downstream_id in state.get("steps", {}):
            state["steps"][downstream_id]["approved"] = False

    state["current_step"] = step_id
    state["complete"] = False
    _auto_save_draft(session_id)
    return {"state": state, "step": step_id, "data": data}


@app.post("/api/world/regenerate-item/{step_id}")
async def regenerate_world_item(step_id: str, body: dict = None, session_id: str = "default"):
    """Regenerate a single entry of a step's string-list field.

    Stateless w.r.t. the session: returns the new entry only. The client splices
    it into its edit buffer and persists via the normal approve/save flow.
    """
    state = _get_world_state(session_id)

    if step_id not in world_builder._steps:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_id}")

    body = body or {}
    field = body.get("field")
    if not field:
        raise HTTPException(status_code=400, detail="Missing 'field'")
    index = int(body.get("index", 0))
    items = body.get("items", []) or []
    note = body.get("note", "")

    try:
        item = await world_builder.regenerate_list_item(
            step_id, field, items, index, state,
            state.get("seed_prompt", ""), user_note=note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"step": step_id, "field": field, "index": index, "item": item}


@app.post("/api/world/approve-step/{step_id}")
async def approve_world_step(step_id: str, body: dict = None, session_id: str = "default"):
    state = _get_world_state(session_id)

    if step_id not in world_builder._steps:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_id}")

    was_already_approved = state.get("steps", {}).get(step_id, {}).get("approved", False)
    edited_data = body.get("data") if body else None

    if edited_data:
        existing_note = state.get("steps", {}).get(step_id, {}).get("note", "")
        state["steps"][step_id] = {"data": edited_data, "approved": True, "note": existing_note}
    else:
        step_entry = state.get("steps", {}).get(step_id, {})
        step_entry["approved"] = True
        state["steps"][step_id] = step_entry

    current_idx = world_builder._ordered_ids.index(step_id)

    if was_already_approved:
        for idx in range(current_idx + 1, len(world_builder._ordered_ids)):
            downstream_id = world_builder._ordered_ids[idx]
            if downstream_id in state.get("steps", {}):
                state["steps"][downstream_id]["approved"] = False

        next_unapproved = None
        for idx in range(current_idx + 1, len(world_builder._ordered_ids)):
            check_id = world_builder._ordered_ids[idx]
            step_entry = state.get("steps", {}).get(check_id, {})
            if step_entry.get("data") and not step_entry.get("approved"):
                next_unapproved = check_id
                break

        if next_unapproved:
            state["current_step"] = next_unapproved
            state["complete"] = False
            _auto_save_draft(session_id)
            return {"state": state, "current_step": next_unapproved}
        else:
            state["current_step"] = None
            state["complete"] = True
            _auto_save_draft(session_id)
            return {"state": state, "complete": True}

    next_step = world_builder._ordered_ids[current_idx + 1] if current_idx + 1 < len(world_builder._ordered_ids) else None

    if next_step:
        if next_step in ("node_labeling", "node_descriptions"):
            step = world_builder._steps[next_step]
            default_data = {k: v.get("default", 0) for k, v in step.schema.items()}
            default_data["results"] = []
            state["steps"][next_step] = {"data": default_data, "approved": False}
            state["current_step"] = next_step
            _auto_save_draft(session_id)
            return {"state": state, "current_step": next_step, "data": default_data}

        try:
            data = await world_builder.generate_step(next_step, state, state.get("seed_prompt", ""))
        except Exception as e:
            import traceback
            logger.error(f"Map generation failed for step {next_step}: {traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"Failed to generate step '{next_step}': {str(e)}")
        state["steps"][next_step] = {"data": data, "approved": False}
        state["current_step"] = next_step
        _auto_save_draft(session_id)
        return {"state": state, "current_step": next_step, "data": data}

    state["current_step"] = None
    state["complete"] = True
    _auto_save_draft(session_id)
    return {"state": state, "complete": True}


@app.get("/api/world/state")
async def get_world_state(session_id: str = "default"):
    return {"state": _get_world_state(session_id), "world_id": _get_world_draft_id(session_id)}


# === Debug / seed endpoints ===

class SeedRequest(BaseModel):
    seed_prompt: str = "A dark fantasy world"
    world_id: Optional[str] = None
    total_nodes: int = 60


@app.post("/api/world/debug/seed")
async def debug_seed_world(request: SeedRequest):
    try:
        result = world_builder.seed_world(
            request.seed_prompt,
            world_id=request.world_id,
            total_nodes=request.total_nodes,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class SkipToRequest(BaseModel):
    world_id: str
    total_nodes: int = 60


@app.post("/api/world/debug/skip-to/{step_id}")
async def debug_skip_to(step_id: str, request: SkipToRequest):
    try:
        world_data = world_builder.load_world(request.world_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"World '{request.world_id}' not found. Use /debug/seed first.")

    steps = world_data.get("steps", {})
    target_idx = None
    for i, sid in enumerate(world_builder._ordered_ids):
        if sid == step_id:
            target_idx = i
            break
    if target_idx is None:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_id}")

    for i in range(target_idx):
        sid = world_builder._ordered_ids[i]
        if sid not in steps or not isinstance(steps.get(sid), dict):
            steps[sid] = {}
        if not isinstance(steps[sid].get("data"), dict):
            steps[sid]["data"] = {}
        steps[sid]["approved"] = True

    for i in range(target_idx, len(world_builder._ordered_ids)):
        sid = world_builder._ordered_ids[i]
        if sid not in steps or not isinstance(steps.get(sid), dict):
            steps[sid] = {}
        steps[sid]["approved"] = False

    session_id = request.world_id
    state = {
        "seed_prompt": world_data.get("seed_prompt", ""),
        "steps": steps,
        "complete": False,
        "current_step": step_id,
        "worldId": request.world_id,
    }
    world_gen_sessions[session_id] = state

    note_for_layer = ""
    for i in range(target_idx):
        sid = world_builder._ordered_ids[i]
        sd = steps.get(sid, {}).get("data", {})
        if sid == "layer_design" and isinstance(sd, dict) and sd.get("layers"):
            note_for_layer = "multi-layer world"

    step_config = {"total_nodes": request.total_nodes} if step_id == "map_generation" else None

    try:
        data = await world_builder.generate_step(
            step_id, state,
            user_prompt=world_data.get("seed_prompt", ""),
            user_note=note_for_layer if step_id in ("terrain_regions", "natural_landmarks", "society_factions") else "",
            config=step_config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    state["steps"][step_id] = {"data": data, "approved": False}

    return {
        "world_id": request.world_id,
        "current_step": step_id,
        "pipeline_position": f"{target_idx + 1}/{len(world_builder._ordered_ids)}",
        "state": state,
    }


# === End debug endpoints ===


@app.post("/api/world/compile")
async def compile_world(body: dict = None, session_id: str = "default"):
    state = _get_world_state(session_id)
    save_id = body.get("save_id") if body else None

    if not state.get("complete"):
        raise HTTPException(status_code=400, detail="World generation is not complete.")

    compiled = world_builder.compile_world(state)

    if save_id:
        session_manager.create_save(save_id)
        save_workspace = session_manager.data_dir / "saves" / save_id
        world_dir = save_workspace / "World"
        world_dir.mkdir(parents=True, exist_ok=True)
        import json as _json
        with open(world_dir / "world_data.json", "w", encoding="utf-8") as f:
            _json.dump(compiled, f, indent=2)
        session_manager.load_save(save_id)
        return {"compiled": compiled, "save_id": save_id, "session": session_manager.get_status()}

    return {"compiled": compiled}

class SaveWorldRequest(BaseModel):
    world_id: str

@app.post("/api/world/save")
async def save_world(request: SaveWorldRequest, session_id: str = "default"):
    state = _get_world_state(session_id)
    try:
        world_id = world_builder.save_world(request.world_id, state)
        world_gen_sessions.pop(session_id, None)
        world_draft_ids.pop(session_id, None)
        return {"world_id": world_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/world/discard")
async def discard_world(session_id: str = "default"):
    world_gen_sessions.pop(session_id, None)
    world_draft_ids.pop(session_id, None)
    return {"discarded": True}

@app.get("/api/world/list")
async def list_worlds():
    return {"worlds": world_builder.list_worlds()}

@app.get("/api/world/load/{world_id}")
async def load_world(world_id: str, session_id: str = "default"):
    try:
        world_gen_sessions[session_id] = world_builder.load_world(world_id)
        return {"state": _get_world_state(session_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class ResumeRequest(BaseModel):
    world_id: str


@app.post("/api/world/resume")
async def resume_world(request: ResumeRequest, session_id: str = "default"):
    try:
        state = world_builder.load_world(request.world_id)
        world_gen_sessions[session_id] = state
        world_draft_ids[session_id] = request.world_id
        state["_draft_id"] = request.world_id
        return {"state": state}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@app.delete("/api/world/{world_id}")
async def delete_world(world_id: str):
    try:
        world_builder.delete_world(world_id)
        return {"deleted": world_id}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

class SaveStepRequest(BaseModel):
    data: dict = None

@app.post("/api/world/save-step/{world_id}/{step_id}")
async def save_world_step(world_id: str, step_id: str, request: SaveStepRequest = None):
    try:
        step_data = request.data if request else None
        if step_data is not None:
            world_builder.save_step(world_id, step_id, step_data)
        return {"saved": True}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class PickStartRequest(BaseModel):
    preference: Optional[str] = ""


def _init_world_index_for_save(save_id: str):
    if engine.memory is None:
        return
    world_index_path = session_manager.data_dir / "saves" / save_id / "world_index"
    if world_index_path.exists():
        engine.memory.init_world_index(str(world_index_path))


@app.get("/api/world/{world_id}/start-locations")
async def get_start_locations(world_id: str):
    try:
        locations = world_builder.get_start_locations(world_id)
        return {"world_id": world_id, "locations": locations}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/world/{world_id}/pick-start")
async def pick_start_location(world_id: str, request: PickStartRequest = None):
    try:
        preference = request.preference if request else ""
        location = await world_builder.llm_pick_start_location(world_id, preference, engine.llm)
        if not location:
            raise HTTPException(status_code=404, detail="No start locations found for this world.")
        return {"world_id": world_id, "location": location}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class EnrichRequest(BaseModel):
    layer_id: Optional[str] = None
    labeled_node_ids: Optional[list[str]] = None


class EnrichCommitRequest(BaseModel):
    step_id: str


@app.post("/api/world/{world_id}/enrich/label-next")
async def enrich_label_next(world_id: str, request: EnrichRequest = None):
    try:
        layer_filter = request.layer_id if request else None
        labeled_ids = request.labeled_node_ids if request else None
        result = await world_builder.enrich_next_label(world_id, labeled_node_ids=labeled_ids, layer_filter=layer_filter)
        return {"world_id": world_id, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/world/{world_id}/enrich/describe-next")
async def enrich_describe_next(world_id: str, request: EnrichRequest = None):
    try:
        layer_filter = request.layer_id if request else None
        labeled_ids = request.labeled_node_ids if request else None
        result = await world_builder.enrich_next_description(world_id, labeled_node_ids=labeled_ids, layer_filter=layer_filter)
        return {"world_id": world_id, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/world/{world_id}/enrich/progress")
async def enrich_progress(world_id: str, layer_id: Optional[str] = None):
    try:
        world_data = world_builder.load_world(world_id)
        compiled = world_builder.compile_world(world_data)
        all_nodes, layer_map = world_builder._collect_nodes_by_layer(compiled, layer_id)
        label_progress = {}
        desc_progress = {}
        for lid, info in layer_map.items():
            lid_nodes = [n for n in all_nodes if n.get("layer_id", "") == lid]
            labeled = sum(1 for n in lid_nodes if n.get("name"))
            described = sum(1 for n in lid_nodes if n.get("description"))
            label_progress[lid] = {"done": labeled, "total": info["total"]}
            desc_progress[lid] = {"done": described, "total": labeled}
        total_labeled = sum(v["done"] for v in label_progress.values())
        total_nodes = sum(v["total"] for v in label_progress.values())
        total_described = sum(v["done"] for v in desc_progress.values())
        total_labeled_nodes = sum(v["total"] for v in desc_progress.values())
        return {
            "world_id": world_id,
            "labeling": {"per_layer": label_progress, "total_labeled": total_labeled, "total_nodes": total_nodes},
            "descriptions": {"per_layer": desc_progress, "total_described": total_described, "total_nodes": total_labeled_nodes},
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/world/{world_id}/enrich/commit")
async def enrich_commit(world_id: str, request: EnrichCommitRequest, session_id: str = "default"):
    try:
        world_data = world_builder.load_world(world_id)
        compiled = world_builder.compile_world(world_data)
        all_nodes, layer_map = world_builder._collect_nodes_by_layer(compiled)

        step_id = request.step_id
        if step_id == "node_labeling":
            labeled_count = sum(1 for n in all_nodes if n.get("name"))
            total_nodes = len(all_nodes)
            step_data = {
                "results": [{"node_id": n["id"], "name": n.get("name"), "label_description": n.get("label_description", "")} for n in all_nodes if n.get("name")],
                "total_nodes": total_nodes,
                "labeled_count": labeled_count,
            }
        elif step_id == "node_descriptions":
            described_count = sum(1 for n in all_nodes if n.get("description"))
            labeled = [n for n in all_nodes if n.get("name")]
            step_data = {
                "results": [{"node_id": n["id"], "description": n.get("description")} for n in all_nodes if n.get("description")],
                "total_nodes": len(labeled),
                "described_count": described_count,
            }
        else:
            raise HTTPException(status_code=400, detail=f"Unknown enrichment step: {step_id}")

        world_builder._flush_enrichment_cache(world_id)

        state = _get_world_state(session_id)
        if world_id == _get_world_draft_id(session_id) and step_id in state.get("steps", {}):
            existing_note = state["steps"].get(step_id, {}).get("note", "")
            state["steps"][step_id] = {"data": step_data, "approved": False, "note": existing_note}

            map_step = state["steps"].get("map_generation", {})
            map_data = map_step.get("data", {}) if isinstance(map_step, dict) else {}
            if isinstance(map_data, dict):
                node_map = {n.get("id"): n for n in all_nodes}
                world_builder.sync_enrichment_to_map_state(map_data, node_map)

            _auto_save_draft(session_id)
            return {"world_id": world_id, "step_id": step_id, "committed": True, "data": step_data, "state": state}

        return {"world_id": world_id, "step_id": step_id, "committed": True, "data": step_data}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


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
    world_id: Optional[str] = None
    gender: str = ""
    race: str = ""
    seed: str = ""


class GenerateAppearanceRequest(BaseModel):
    short_description: str = ""
    world_id: Optional[str] = None
    gender: str = ""
    race: str = ""


class GenerateRaceRequest(BaseModel):
    world_id: Optional[str] = None
    gender: str = ""
    seed: str = ""


class GenerateStatsRequest(BaseModel):
    concept: str = ""
    world_id: Optional[str] = None
    gender: str = ""
    race: str = ""


class SaveCharacterRequest(BaseModel):
    id: str
    name: str = ""
    gender: str = ""
    race: str = ""
    short_appearance: str = ""
    full_appearance: str = ""
    world_id: Optional[str] = None
    module_data: dict = {}


class WorldContextRequest(BaseModel):
    world_id: Optional[str] = None


@app.get("/api/character/list")
async def list_characters():
    return {"characters": character_builder.list_characters()}


@app.post("/api/character/generate-name")
async def generate_character_name(request: GenerateNameRequest):
    result = await character_builder.generate_name(
        world_id=request.world_id,
        gender=request.gender,
        race=request.race,
        seed=request.seed,
    )
    return result


@app.post("/api/character/generate-appearance")
async def generate_character_appearance(request: GenerateAppearanceRequest):
    result = await character_builder.generate_full_appearance(
        short_desc=request.short_description,
        world_id=request.world_id,
        gender=request.gender,
        race=request.race,
    )
    return result


@app.post("/api/character/generate-race")
async def generate_character_race(request: GenerateRaceRequest):
    result = await character_builder.generate_race(
        world_id=request.world_id,
        gender=request.gender,
        seed=request.seed,
    )
    return result


@app.post("/api/character/generate-stats")
async def generate_character_stats(request: GenerateStatsRequest):
    result = await character_builder.generate_stats(
        concept=request.concept,
        world_id=request.world_id,
        gender=request.gender,
        race=request.race,
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
            "world_id": request.world_id,
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
        session_manager.save_manager.delete_save(save_id)
        return {"deleted": save_id}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/character/{character_id}")
async def delete_character(character_id: str):
    try:
        character_builder.delete_character(character_id)
        return {"deleted": character_id}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/character/module-defaults")
async def get_character_module_defaults(world_id: Optional[str] = None):
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
                    world_ctx = {}
                    if world_id:
                        try:
                            world_state = world_builder.load_world(world_id)
                            world_ctx = {"world": world_builder.compile_world(world_state)}
                        except Exception:
                            pass
                    custom_defaults = await backend.on_character_get_defaults({}, world_ctx)
                    if isinstance(custom_defaults, dict):
                        defaults[mod_id] = custom_defaults
                except Exception as e:
                    print(f"[Character] Error getting defaults from {mod_id}: {e}")

    return {"module_defaults": defaults}


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket client connected.")
    
    # Setup streaming callback
    async def stream_token(token: str):
        await websocket.send_json({"type": "token", "content": token})
        
    engine.sdk.ui.on_token = stream_token

    async def broadcast_inspector_call(record):
        try:
            await websocket.send_json({"type": "llm_call", "call": llm_inspector._record_to_dict(record) if hasattr(llm_inspector, '_record_to_dict') else {}})
        except Exception:
            pass

    llm_inspector.set_ws_broadcast(broadcast_inspector_call)

    try:
        while True:
            data = await websocket.receive_json()
            print(f"Received from client: {data}")

            action = data.get("action", "turn")

            if action == "intro":
                state = session_manager.state
                state = await engine.initialize_module_data(state)
                session_manager.state = state
                if len(state.get("history", [])) == 0:
                    await engine.ensure_memory()
                    engine.set_memory_path(session_manager.get_memory_path())
                    _init_world_index_for_save(session_manager.active_save_id)
                    try:
                        intro_text = await engine.generate_intro(
                            state,
                            streaming_callback=stream_token,
                        )
                        state["history"] = [intro_text]
                        state["chat_messages"] = [{"role": "ai", "content": intro_text}]
                        session_manager.save_manager.save_turn(
                            session_manager.active_save_id, state, 0
                        )
                    except Exception as exc:
                        print(f"Error generating intro: {exc}")
                        await websocket.send_json({
                            "type": "error",
                            "code": "intro_failed",
                            "message": "Failed to generate the opening scene.",
                            "detail": str(exc),
                            "state": state,
                        })
                        continue
                else:
                    await websocket.send_json({
                        "type": "state_load",
                        "chat_messages": state.get("chat_messages", []),
                    })
                await websocket.send_json({"type": "done", "state": state})
                continue

            # Update state with user input
            session_manager.set_input(data.get("text", ""))
            engine.set_memory_path(session_manager.get_memory_path())
            _init_world_index_for_save(session_manager.active_save_id)
            
            try:
                # Execute the LangGraph pipeline
                final_state = await engine.app.ainvoke(session_manager.state)
                active_state = session_manager.save_completed_turn(final_state)
                
                # Send final completion signal with the updated state
                await websocket.send_json({"type": "done", "state": active_state})
            except LLMProviderError as exc:
                print(f"LLM provider error during WebSocket turn: {exc}")
                session_manager.set_input("")
                await websocket.send_json({
                    "type": "error",
                    "code": "llm_provider_unavailable",
                    "message": "The AI provider is temporarily unavailable. Please try again in a moment.",
                    "detail": str(exc),
                    "state": session_manager.state,
                })
            except Exception as exc:
                print(f"Unexpected error during WebSocket turn: {exc}")
                session_manager.set_input("")
                await websocket.send_json({
                    "type": "error",
                    "code": "turn_failed",
                    "message": "The turn failed before it could be saved.",
                    "detail": str(exc),
                    "state": session_manager.state,
                })
            
    except WebSocketDisconnect:
        print("WebSocket client disconnected.")


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
