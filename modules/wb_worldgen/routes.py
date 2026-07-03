"""World-generation + terrain API routes (relocated from backend/api/server.py).

These endpoints are owned by the wb_worldgen module and mounted by the core
server at their original paths (/api/world/*, /api/terrain/*) so the frontend is
unaffected. Module-local state (the in-progress generation sessions/drafts) and
the shared engine services are populated by ``configure()`` which backend.py
calls from ``set_services``.
"""

import os
import json
import random
from typing import Optional, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

# --- injected by configure() (from the module's set_services) ---
world_builder = None      # the module's WorldBuilder instance
engine = None             # core EngineGraph (for .memory, .llm)
session_manager = None    # core GameSessionManager


def configure(*, builder, engine_ref, session_manager_ref):
    global world_builder, engine, session_manager
    world_builder = builder
    engine = engine_ref
    session_manager = session_manager_ref


def _init_world_index_for_save(save_id: str):
    if engine is None or engine.memory is None:
        return
    world_index_path = session_manager.data_dir / "saves" / save_id / "world_index"
    if world_index_path.exists():
        engine.memory.init_world_index(str(world_index_path))


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


def _sync_enrichment_result_to_draft(session_id: str, world_id: str, result: dict):
    """Mirror a single enrichment result (label/description) into the in-memory
    draft's map_generation nodes immediately, so the next auto-save or step
    approval never clobbers the names/descriptions already written to disk.
    This is what makes enrichment results persist without a manual commit."""
    if not result or not result.get("node_id"):
        return
    if world_id != _get_world_draft_id(session_id):
        return
    map_step = _get_world_state(session_id).get("steps", {}).get("map_generation")
    if not isinstance(map_step, dict):
        return
    map_data = map_step.get("data", {})
    if not isinstance(map_data, dict):
        return
    enriched = {}
    if result.get("label"):
        enriched["name"] = result["label"]
    if result.get("label_description"):
        enriched["label_description"] = result["label_description"]
    if result.get("description"):
        enriched["description"] = result["description"]
    if enriched:
        world_builder.sync_enrichment_to_map_state(map_data, {result["node_id"]: enriched})


# === World Builder Routes ===

class WorldGenerateRequest(BaseModel):
    seed_prompt: str
    skip_review: bool = False


@router.get("/api/world/pipeline")
async def get_world_pipeline():
    return {"pipeline": world_builder.get_pipeline()}


@router.post("/api/world/generate")
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


@router.post("/api/world/generate-step/{step_id}")
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


@router.post("/api/world/regenerate-item/{step_id}")
async def regenerate_world_item(step_id: str, body: dict = None, session_id: str = "default"):
    """Regenerate a single entry of a step's list field.

    Handles both string lists (returns a string) and structured/object lists
    (returns the new entry dict, or just one sub-field value when ``subfield``
    is supplied). Stateless w.r.t. the session: returns the new value only. The
    client splices it into its edit buffer and persists via the approve/save flow.
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
    subfield = body.get("subfield") or None

    try:
        item = await world_builder.regenerate_list_item(
            step_id, field, items, index, state,
            state.get("seed_prompt", ""), user_note=note, subfield=subfield,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"step": step_id, "field": field, "index": index, "item": item, "subfield": subfield}


@router.post("/api/world/approve-step/{step_id}")
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


@router.get("/api/world/state")
async def get_world_state(session_id: str = "default"):
    return {"state": _get_world_state(session_id), "world_id": _get_world_draft_id(session_id)}


# === Debug / seed endpoints ===

class SeedRequest(BaseModel):
    seed_prompt: str = "A dark fantasy world"
    world_id: Optional[str] = None
    total_nodes: int = 60


@router.post("/api/world/debug/seed")
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


@router.post("/api/world/debug/skip-to/{step_id}")
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


@router.post("/api/world/compile")
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

@router.post("/api/world/save")
async def save_world(request: SaveWorldRequest, session_id: str = "default"):
    state = _get_world_state(session_id)
    try:
        world_id = world_builder.save_world(request.world_id, state)
        world_gen_sessions.pop(session_id, None)
        world_draft_ids.pop(session_id, None)
        return {"world_id": world_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/world/discard")
async def discard_world(session_id: str = "default"):
    world_gen_sessions.pop(session_id, None)
    world_draft_ids.pop(session_id, None)
    return {"discarded": True}

@router.get("/api/world/list")
async def list_worlds():
    return {"worlds": world_builder.list_worlds()}

@router.get("/api/world/load/{world_id}")
async def load_world(world_id: str, session_id: str = "default"):
    try:
        world_gen_sessions[session_id] = world_builder.load_world(world_id)
        return {"state": _get_world_state(session_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class ResumeRequest(BaseModel):
    world_id: str


@router.post("/api/world/resume")
async def resume_world(request: ResumeRequest, session_id: str = "default"):
    try:
        state = world_builder.load_world(request.world_id)
        world_gen_sessions[session_id] = state
        world_draft_ids[session_id] = request.world_id
        state["_draft_id"] = request.world_id
        return {"state": state}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.delete("/api/world/{world_id}")
async def delete_world(world_id: str):
    try:
        world_builder.delete_world(world_id)
        return {"deleted": world_id}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/world/{world_id}/terrain/{layer_id}/{image}")
async def get_world_terrain_image(world_id: str, layer_id: str, image: str):
    """Serve a world layer's rendered terrain image (biome / hillshade PNG)."""
    from fastapi.responses import FileResponse
    fname = {"biome": "biome.png", "hillshade": "hillshade.png"}.get(image)
    if not fname:
        raise HTTPException(status_code=404, detail="unknown image")
    try:
        out_dir = world_builder._persistence.terrain_dir(world_id, layer_id or "main")
    except Exception:
        raise HTTPException(status_code=404, detail="not found")
    path = os.path.join(str(out_dir), fname)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-cache"})

class SaveStepRequest(BaseModel):
    data: dict = None

@router.post("/api/world/save-step/{world_id}/{step_id}")
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


@router.get("/api/world/{world_id}/start-locations")
async def get_start_locations(world_id: str):
    try:
        locations = world_builder.get_start_locations(world_id)
        return {"world_id": world_id, "locations": locations}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/world/{world_id}/pick-start")
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
    rework: bool = False


class EnrichCommitRequest(BaseModel):
    step_id: str


@router.post("/api/world/{world_id}/enrich/label-next")
async def enrich_label_next(world_id: str, request: EnrichRequest = None, session_id: str = "default"):
    try:
        layer_filter = request.layer_id if request else None
        labeled_ids = request.labeled_node_ids if request else None
        rework = request.rework if request else False
        result = await world_builder.enrich_next_label(world_id, labeled_node_ids=labeled_ids, layer_filter=layer_filter, rework=rework)
        _sync_enrichment_result_to_draft(session_id, world_id, result)
        return {"world_id": world_id, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/api/world/{world_id}/enrich/describe-next")
async def enrich_describe_next(world_id: str, request: EnrichRequest = None, session_id: str = "default"):
    try:
        layer_filter = request.layer_id if request else None
        labeled_ids = request.labeled_node_ids if request else None
        rework = request.rework if request else False
        result = await world_builder.enrich_next_description(world_id, labeled_node_ids=labeled_ids, layer_filter=layer_filter, rework=rework)
        _sync_enrichment_result_to_draft(session_id, world_id, result)
        return {"world_id": world_id, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/world/{world_id}/enrich/progress")
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


@router.post("/api/world/{world_id}/enrich/commit")
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

