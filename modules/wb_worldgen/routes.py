"""World-generation + terrain API routes (relocated from backend/api/server.py).

These endpoints are owned by the wb_worldgen module and mounted by the core
server at their original paths (/api/world/*, /api/terrain/*). Every route is
world-scoped (a saved world id on disk) — the classic wizard's in-memory
session/draft machinery is gone with the sequential build flow; worlds are
born through the agent build and read/edited through the explorer surface
(load, compiled, save-step, regenerate-step). The shared engine services are
populated by ``configure()`` which backend.py calls from ``set_services``.
"""

import asyncio
import os
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
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


# === World Builder Routes ===

def _ordered_ids_for(state: dict) -> list[str]:
    """Effective step order for a world's state (world_form skip filters);
    tolerates fakes/legacy builders without the resolver."""
    resolver = getattr(world_builder, "ordered_ids_for", None)
    if callable(resolver):
        return resolver(state)
    return world_builder._ordered_ids


def _state_response(state: dict, **extra) -> dict:
    """A session-state payload plus the effective (post-skip) step order the
    client should render. Recomputed per response, never persisted — the
    wizard fetches the full pipeline once, so this is how it learns which
    steps the world's own design turned off."""
    return {"state": state, "effective_steps": _ordered_ids_for(state), **extra}


@router.get("/api/world/pipeline")
async def get_world_pipeline():
    return {"pipeline": world_builder.get_pipeline()}


def _apply_scenario(state: dict, scenario_id: Optional[str], scenario: str):
    """Resolve a request's scenario link/text onto a generation state
    (shared by the wizard and the agent-build launch)."""
    if scenario_id:
        from backend.engine.scenario import ScenarioStore
        from wbworldgen.worldgen.prompts import scenario_grounding_text
        try:
            record = ScenarioStore(session_manager.data_dir).load_scenario(scenario_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail=f"Scenario '{scenario_id}' not found.")
        state["scenario_id"] = record.get("id", scenario_id)
        grounding = scenario_grounding_text(record)
        if grounding:
            state["scenario"] = grounding
    elif scenario.strip():
        state["scenario"] = scenario.strip()


class RewriteWorldPromptRequest(BaseModel):
    #: The player's free-form direction typed into the enrich field.
    instruction: str = ""
    #: The current World Prompt draft, if any (built on when present).
    current_text: Optional[str] = None
    #: Optional linked scenario for grounding (same store as world creation).
    scenario_id: Optional[str] = None


def _load_scenario_or_404(scenario_id: Optional[str]) -> Optional[dict]:
    if not scenario_id:
        return None
    from backend.engine.scenario import ScenarioStore
    try:
        return ScenarioStore(session_manager.data_dir).load_scenario(scenario_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail=f"Scenario '{scenario_id}' not found.")


async def _seed_prompt_completion(messages: list[dict], call_type: str, what: str) -> str:
    """Run a seed-prompt authoring call ({"text": ...} JSON) with the shared
    error handling; `what` names the operation in error details."""
    try:
        from backend.engine.llm import LLMProviderError
    except ImportError:  # isolated module-test context: core pkg not on path
        LLMProviderError = RuntimeError
    try:
        content = await engine.llm.simple_completion(
            messages,
            model=engine.llm.storyteller_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": call_type, "step": "world_build:seed_prompt"},
        )
        text = str(json.loads(content).get("text") or "").strip()
    except LLMProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=502, detail=f"{what} returned unusable output: {exc}")
    if not text:
        raise HTTPException(status_code=502, detail=f"{what} returned no text.")
    return text


@router.post("/api/world/rewrite-prompt")
async def rewrite_world_prompt(request: RewriteWorldPromptRequest):
    """LLM-as-author for the World Prompt: turns the player's notes (the enrich
    field), the current draft, and an optional linked scenario into a world
    seed prompt. Mirrors the scenario editor's prompt rewrite."""
    from wbworldgen.worldgen.prompts import build_world_prompt_messages

    instruction = (request.instruction or "").strip()
    current_text = (request.current_text or "").strip()
    scenario = _load_scenario_or_404(request.scenario_id)

    if not instruction and not current_text and scenario is None:
        raise HTTPException(status_code=400,
                            detail="Enter some direction or link a scenario to write a world prompt.")

    messages = build_world_prompt_messages(instruction, current_text, scenario)
    text = await _seed_prompt_completion(
        messages, "world_prompt_rewrite", "World prompt rewrite")
    return {"text": text}


class IdeationTurnRequest(BaseModel):
    #: Conversation so far, oldest first, the player's newest message last.
    #: Each item: {"role": "player"|"assistant", "text": str}.
    messages: list[dict] = []
    #: Current seed-prompt draft (the shared field — may be hand-edited).
    prompt: Optional[str] = None
    #: Current world-rules draft.
    rules: list[str] = []
    #: Current design-notes draft (C5): [{"text", "subject"}], empty
    #: subject = world-scoped.
    notes: list[dict] = []
    #: Optional linked scenario: its content counts as already decided.
    scenario_id: Optional[str] = None


@router.post("/api/world/ideation-turn")
async def ideation_turn(request: IdeationTurnRequest):
    """One turn of the ideation conversation (C4): the model answers the
    player and returns the updated seed-prompt + world-rules drafts, plus
    ``ready`` — its judgment that the idea feels settled (the go offer; the
    player's go-ahead stays the approval moment). Stateless like the other
    seed-prompt endpoints: the client holds the conversation (localStorage,
    relaunch-safe) and the drafts round-trip every turn, so hand edits
    between turns are simply the current truth."""
    from wbworldgen.worldgen.notes import clean_notes
    from wbworldgen.worldgen.prompts import build_ideation_turn_messages
    try:
        from backend.engine.llm import LLMProviderError
    except ImportError:  # isolated module-test context: core pkg not on path
        LLMProviderError = RuntimeError

    if not any(str(m.get("text") or "").strip()
               for m in request.messages if m.get("role") != "assistant"):
        raise HTTPException(status_code=400,
                            detail="Say something to the design partner first.")
    scenario = _load_scenario_or_404(request.scenario_id)
    messages = build_ideation_turn_messages(
        request.messages, (request.prompt or "").strip(), request.rules, scenario,
        notes_draft=request.notes)
    try:
        content = await engine.llm.simple_completion(
            messages,
            model=engine.llm.storyteller_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "world_ideation", "step": "world_build:ideation"},
        )
        data = json.loads(content)
        reply = str(data.get("reply") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        raw_rules = data.get("rules")
        rules = ([str(r).strip() for r in raw_rules if str(r).strip()]
                 if isinstance(raw_rules, list) else None)
        raw_notes = data.get("notes")
        notes = clean_notes(raw_notes) if isinstance(raw_notes, list) else None
        ready = bool(data.get("ready"))
    except LLMProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=502,
                            detail=f"Ideation turn returned unusable output: {exc}")
    if not reply:
        raise HTTPException(status_code=502, detail="Ideation turn returned no reply.")
    # A model that omits a draft leaves it unchanged — the client always
    # overwrites its local drafts with this response.
    return {
        "reply": reply,
        "prompt": prompt or (request.prompt or "").strip(),
        "rules": (rules if rules is not None
                  else [str(r).strip() for r in request.rules if str(r).strip()]),
        "notes": notes if notes is not None else clean_notes(request.notes),
        "ready": ready,
    }


# === Debug / seed endpoints ===

class SeedRequest(BaseModel):
    seed_prompt: str = "A dark fantasy world"
    world_id: Optional[str] = None
    total_nodes: int = 60


@router.post("/api/world/debug/seed")
async def debug_seed_world(request: SeedRequest):
    try:
        result = await world_builder.seed_world(
            request.seed_prompt,
            world_id=request.world_id,
            total_nodes=request.total_nodes,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/world/list")
async def list_worlds():
    from wbworldgen.worldgen.agent import harness as agent_harness
    worlds = world_builder.list_worlds()
    for w in worlds:
        # Routes an in-progress world's recovery affordance: reattach to its
        # build's observer when an artifact exists, offer a fresh adopt run
        # ("Finish with AI") when none does (e.g. a pre-agent-era draft).
        w["has_agent_build"] = agent_harness.has_build_artifact(
            world_builder, w["id"])
    return {"worlds": worlds}

@router.get("/api/world/load/{world_id}")
async def load_world(world_id: str, session_id: str = "default"):
    try:
        return _state_response(world_builder.load_world(world_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/world/{world_id}/compiled")
async def get_compiled_world(world_id: str):
    """The compiled (game-ready) view of a saved world: every step's output
    plus post-generation child-map bundles and surgery connections merged —
    the same world a session would load. This is the world explorer's read
    surface; raw step files stay reachable via /api/world/load for editing.

    Compiled fresh from disk per call rather than through the size-1
    CompiledWorldCache: a browse must never evict the actively-enriched
    world's cache entry, and the cache's terrain-raster attach would
    decompress tens of MB only for this route to strip them again.
    """
    from wbworldgen.worldgen.compiler import compile_world
    try:
        state = world_builder.load_world(world_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    compiled = compile_world(state, world_builder.steps_by_id())
    return {"compiled": {k: v for k, v in compiled.items()
                         if not str(k).startswith("_")}}


class RegenerateStepRequest(BaseModel):
    #: Steering note threaded into the step's prompt (same channel as the
    #: wizard-era guidance notes and the agent's run_step steering).
    note: str = ""
    #: Optional config override for the step's schema fields.
    data: Optional[dict] = None


#: Steps the world-scoped regenerate refuses: map/terrain regeneration on a
#: saved world is structural surgery through a side door (child-map anchors
#: and enrichment would silently desync — agent territory, per D1), and the
#: enrichment steps are engine-driven (the enrichment panel runs them).
_UNREGENERATABLE_STEPS = {"map_generation", "terrain_generation",
                          "node_labeling", "node_descriptions"}


@router.post("/api/world/{world_id}/regenerate-step/{step_id}")
async def regenerate_saved_world_step(world_id: str, step_id: str,
                                      request: RegenerateStepRequest = None):
    """Regenerate one step of a saved world in place: load the world's state
    from disk, run the step with the full chain context (and the brief, for
    world_rules' agreed-rules enforcement), persist just that step back, and
    invalidate the compiled cache. World-scoped on purpose — the session
    draft machinery is not involved, so no phantom draft copies appear and
    the world's completion status is untouched."""
    steps = world_builder.steps_by_id()
    if step_id not in steps:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_id}")
    if step_id in _UNREGENERATABLE_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Step '{step_id}' cannot be regenerated on a saved world — "
                   "map and terrain structure changes go through an agent build, "
                   "and enrichment runs through the enrichment panel.")
    try:
        state = world_builder.load_world(world_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # Any artifact-writing side effects resolve their target directory from
    # the state's draft id — pin it so nothing lands in a fresh directory.
    state["_draft_id"] = world_id
    note = (request.note if request else "") or ""
    config = request.data if request else None
    data = await world_builder.generate_step(
        step_id, state,
        state.get("seed_prompt", ""),
        user_note=note,
        config=config,
    )
    prev = state.get("steps", {}).get(step_id) or {}
    entry = {**prev, "data": data}
    if note:
        entry["note"] = note
    world_builder.save_step(world_id, step_id, entry)
    return {"step": step_id, "data": data}


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


class ExpandSiteRequest(BaseModel):
    force: bool = False
    #: Optional pin for the child map's level (e.g. "planet", "city",
    #: "interior"); the LLM chooses from the allowed levels when omitted.
    level_type: Optional[str] = None


@router.post("/api/world/{world_id}/site/{node_id}/expand")
async def expand_world_site(world_id: str, node_id: str, request: ExpandSiteRequest = None):
    """Generate (or return the cached) child map for a location. World-scoped:
    used at authoring time to pre-bake key cities. (Path kept for the old
    site-era clients; the payload now carries the map bundle.)"""
    try:
        force = request.force if request else False
        level_type = (request.level_type or None) if request else None
        compiled = world_builder.compile_world(world_builder.load_world(world_id))
        from wbworldgen.worldgen import mapspace as _ms
        map_id = _ms.map_of_node(compiled, node_id) or compiled.get("root_map_id", "root")
        bundle = await world_builder.expand_node(world_id, map_id, node_id, force=force,
                                                 level_type=level_type)
        return {"world_id": world_id, "node_id": node_id, **bundle}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/world/{world_id}/sites")
async def get_world_sites(world_id: str):
    """Expanded child maps for a world (path kept for old clients)."""
    bundles = world_builder._persistence.load_child_maps(world_id)
    return {"world_id": world_id, "maps": bundles,
            "sites": world_builder._persistence.load_sites(world_id)}


class SessionExpandSiteRequest(BaseModel):
    node_id: str
    level_type: Optional[str] = None


@router.post("/api/world/session/expand-site")
async def expand_site_in_session(request: SessionExpandSiteRequest):
    """Play-time manual trigger (the map's Explore button): expand a site in
    the active save's world and sync it into the live session, the save's
    world_data.json and the RAG world index."""
    world_id = session_manager.state.get("world_id")
    if not world_id:
        raise HTTPException(status_code=400, detail="No world-backed save is active.")
    wd = session_manager.state.get("world_data")
    try:
        from wbworldgen.worldgen import mapspace as _ms
        map_id = (_ms.map_of_node(wd, request.node_id) if wd else None) or "root"
        bundle = await world_builder.expand_node(world_id, map_id, request.node_id,
                                                 level_type=request.level_type or None)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    record = bundle.get("map") or {}
    if wd is not None and record.get("map_id"):
        wd.setdefault("maps", {})[record["map_id"]] = record
        existing_ids = {c.get("id") for c in wd.setdefault("connections", [])}
        wd["connections"].extend(
            c for c in bundle.get("connections", []) if c.get("id") not in existing_ids)
        try:
            save_id = session_manager.state.get("active_save_id")
            if save_id:
                world_dir = session_manager.data_dir / "saves" / save_id / "World"
                if world_dir.is_dir():
                    with open(world_dir / "world_data.json", "w", encoding="utf-8") as f:
                        json.dump(wd, f, indent=2)
        except Exception:
            logger.exception("failed to persist world_data after map expansion")
    if engine is not None and engine.memory is not None and engine.memory.has_world_index():
        from wbworldgen.worldgen.expansion.maps_expand import map_world_entries
        entries = map_world_entries(record, bundle.get("connections"),
                                    maps_by_id=(wd or {}).get("maps") or {})
        if entries:
            try:
                await engine.memory.embed_world_entries(entries, engine.llm)
            except Exception:
                logger.exception("failed to embed child map entries")
    return {"world_id": world_id, "node_id": request.node_id, **bundle}


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


@router.post("/api/world/{world_id}/enrich/review")
async def enrich_review(world_id: str, request: EnrichRequest = None):
    """Coherence-review enriched names (one map via layer_id, or all maps):
    an LLM flags names that don't make sense where they sit (e.g. a place
    implying membership of an institution far across the map) and each
    flagged node is relabeled. Runs the registered review pass standalone;
    the same pass also fires automatically whenever an enrichment run
    completes a map's naming."""
    try:
        layer_filter = request.layer_id if request else None
        summary = await world_builder.enrich_run(
            world_id, phase="review", layer_filter=layer_filter)
        result = summary.get("review") or {"reviewed_maps": 0, "flagged": 0,
                                           "relabeled": []}
        return {"world_id": world_id, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class EnrichRunRequest(BaseModel):
    phase: str = "all"  # a registered pass id ("label" | "describe" | "review") or "all"
    count: Optional[int] = None
    layer_id: Optional[str] = None
    rework: bool = False
    # Rework batching: nodes already redone this session, so consecutive
    # partial runs move through the remaining nodes instead of repeating.
    exclude_node_ids: Optional[list[str]] = None
    # Explicit importance floor; None resolves from the world.upfront_detail
    # setting ("major_locations" details only importance >= 6 upfront).
    importance_floor: Optional[int] = None


@router.post("/api/world/{world_id}/enrich/run")
async def enrich_run(world_id: str, request: EnrichRunRequest, session_id: str = "default"):
    """Server-driven enrichment over many nodes in one request. Streams one SSE
    ``data:`` JSON object per event: {type:"phase"|"node"|"failed"} during the
    run, then a terminal {type:"done", labeled, described, ...} (or
    {type:"error", detail}). Replaces the old frontend loop of one
    label-next/describe-next call per node."""
    queue: asyncio.Queue = asyncio.Queue()

    async def on_event(evt: dict):
        queue.put_nowait(evt)

    importance_floor = request.importance_floor
    if importance_floor is None and not request.rework:
        resolver = getattr(world_builder, "default_importance_floor", None)
        importance_floor = resolver() if callable(resolver) else None

    async def runner():
        try:
            await world_builder.enrich_run(
                world_id, phase=request.phase, count=request.count,
                layer_filter=request.layer_id, rework=request.rework,
                exclude_node_ids=request.exclude_node_ids,
                on_event=on_event,
                importance_floor=importance_floor,
            )
        except FileNotFoundError as exc:
            queue.put_nowait({"type": "error", "detail": str(exc)})
        except Exception as exc:
            logger.exception("enrichment run failed for world %s", world_id)
            queue.put_nowait({"type": "error", "detail": str(exc)})
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(runner())

    async def event_stream():
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield "data: " + json.dumps(item) + "\n\n"
        finally:
            # Client disconnected (or stream ended): stop the run; the engine
            # flushes already-generated results on cancellation.
            if not task.done():
                task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/world/{world_id}/enrich/cancel")
async def enrich_cancel(world_id: str):
    world_builder.enrich_cancel(world_id)
    return {"world_id": world_id, "cancelling": True}


# === Agent builds (C2: the agentic builder) ===

class AgentBuildRequest(BaseModel):
    seed_prompt: str
    #: Co-authored world rules from the ideation conversation (C4) — the
    #: brief's fixed design decisions, fed to the world_rules step as input.
    rules: list[str] = []
    #: Design notes from the ideation conversation (C5): [{"text",
    #: "subject"}] — world-scoped facts and per-subject notes the build is
    #: verified against.
    notes: list[dict] = []
    #: Adopt an existing world instead of drafting a fresh one: the build
    #: keeps the world's current content (and, when rules/notes are empty,
    #: its recorded brief) and works from there — the recovery path for
    #: interrupted or pre-agent-era in-progress worlds.
    world_id: Optional[str] = None
    scenario_id: Optional[str] = None
    scenario: str = ""


@router.post("/api/world/agent/build")
async def agent_build_start(request: AgentBuildRequest):
    """Launch a server-side agent build from the ideation brief (seed prompt
    + co-authored world rules). Returns immediately with the new world id;
    the loop keeps running server-side — watch it via the status/events
    endpoints, cancel any time. The finished world saves itself; until then
    it lives as an in-progress draft."""
    from wbworldgen.worldgen.agent import harness as agent_harness
    if not request.seed_prompt.strip():
        raise HTTPException(status_code=400, detail="seed_prompt is required")
    scenario_state: dict = {}
    _apply_scenario(scenario_state, request.scenario_id, request.scenario)
    try:
        handle = agent_harness.start_agent_build(
            world_builder, request.seed_prompt.strip(),
            rules=request.rules,
            notes=request.notes,
            world_id=request.world_id,
            scenario=scenario_state.get("scenario", ""),
            scenario_id=scenario_state.get("scenario_id"))
    except ValueError as exc:
        status = 409 if "already running" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc))
    return {"world_id": handle.world_id, "status": handle.status}


class AgentVetoRequest(BaseModel):
    #: Ids of the brief notes whose compromise/acceptance the user rejects.
    note_ids: list[str] = []


@router.post("/api/world/{world_id}/agent/veto")
async def agent_build_veto(world_id: str, request: AgentVetoRequest):
    """The end-of-build review's veto (C5/N7): re-assert the vetoed notes
    as binding (a vetoed compromise restores the original text and can
    never be amended again) and relaunch the agent on the finished world
    as a bounded fix run. Not vetoing needs no call at all — the world is
    done."""
    from wbworldgen.worldgen.agent import harness as agent_harness
    try:
        world_builder.load_world(world_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Unknown world: {world_id}")
    try:
        handle = agent_harness.veto_notes(
            world_builder, world_id, [str(n).strip() for n in request.note_ids
                                      if str(n).strip()])
    except ValueError as exc:
        status = 409 if "already running" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc))
    return {"world_id": handle.world_id, "status": handle.status,
            "vetoed": request.note_ids}


@router.get("/api/world/{world_id}/agent/status")
async def agent_build_status(world_id: str):
    """Current build snapshot (status, turn/tool counters, todo, result).
    Serves the live handle when the build's backend is still up, else the
    persisted artifact (finished builds survive a restart)."""
    from wbworldgen.worldgen.agent import harness as agent_harness
    handle = agent_harness.get_build(world_id)
    if handle is not None:
        return handle.snapshot()
    artifact = agent_harness.load_build_artifact(world_builder, world_id)
    if artifact is None:
        raise HTTPException(status_code=404,
                            detail=f"No agent build for world '{world_id}'.")
    artifact.pop("log", None)
    return artifact


class AgentEventsRequest(BaseModel):
    #: Replay persisted events with index >= after, then stream live. A
    #: reattaching client passes the last index it saw plus one.
    after: int = 0


@router.post("/api/world/{world_id}/agent/events")
async def agent_build_events(world_id: str, request: AgentEventsRequest = None):
    """SSE stream of one build's events: replays the persisted action log
    from ``after``, then streams live (persisted events carry their index
    ``i``; transient progress events don't and are never replayed). Ends
    after the terminal {type:"done"} event."""
    from wbworldgen.worldgen.agent import harness as agent_harness
    after = max(0, request.after if request else 0)
    handle = agent_harness.get_build(world_id)

    if handle is None:
        artifact = agent_harness.load_build_artifact(world_builder, world_id)
        if artifact is None:
            raise HTTPException(status_code=404,
                                detail=f"No agent build for world '{world_id}'.")

        async def replay_stream():
            for evt in (artifact.get("log") or [])[after:]:
                yield "data: " + json.dumps(evt) + "\n\n"

        return StreamingResponse(replay_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    queue = handle.subscribe()
    finished_at_subscribe = handle.status != "running"
    replay_end = len(handle.log)

    async def event_stream():
        try:
            for evt in handle.log[after:replay_end]:
                yield "data: " + json.dumps(evt) + "\n\n"
            if finished_at_subscribe:
                return
            while True:
                item = await queue.get()
                if item is None:
                    break
                i = item.get("i")
                if i is not None and i < replay_end:
                    continue  # landed in the replay window already
                yield "data: " + json.dumps(item) + "\n\n"
        finally:
            handle.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/world/{world_id}/agent/cancel")
async def agent_build_cancel(world_id: str):
    from wbworldgen.worldgen.agent import harness as agent_harness
    return {"world_id": world_id,
            "cancelling": agent_harness.cancel_build(world_id)}


class AgentMessageRequest(BaseModel):
    #: What the user says to the running build's agent — delivered verbatim
    #: at the next turn boundary (C7a/U3).
    text: str = ""


@router.post("/api/world/{world_id}/agent/message")
async def agent_build_message(world_id: str, request: AgentMessageRequest):
    """Speak into a running build (C7a): the message queues on the build
    handle and reaches the agent as a plain observation at the next turn
    boundary — mid-action it waits until the current tool call returns.
    Returns the queued message's id (echoed by the ``user_message`` event
    when it lands) and its queue position. Only a RUNNING build listens:
    no build is 404, a finished one is 409."""
    from wbworldgen.worldgen.agent import harness as agent_harness
    text = (request.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    handle = agent_harness.get_build(world_id)
    if handle is None:
        raise HTTPException(status_code=404,
                            detail=f"No agent build for world '{world_id}'.")
    if handle.status != "running":
        raise HTTPException(
            status_code=409,
            detail=(f"The build for '{world_id}' is {handle.status} — "
                    "messages reach only a running build."))
    queued = handle.post_message(text)
    return {"world_id": world_id, "queued": True, **queued}


@router.get("/api/world/{world_id}/enrich/passes")
async def enrich_passes(world_id: str):
    """The enrichment pass slice of the capability catalog: what passes are
    registered, so the panel renders one row per pass instead of hardcoding
    phases (a dropped-in pass module appears here without frontend edits)."""
    from wbworldgen.worldgen.enrichment.registry import describe_passes
    return {"world_id": world_id, "passes": describe_passes()}


@router.get("/api/world/{world_id}/enrich/progress")
async def enrich_progress(world_id: str, layer_id: Optional[str] = None):
    """Per-pass enrichment progress, computed from each registered node
    pass's is_done/in_domain predicates: done/total plus a per-map
    breakdown, and the lazy-detail (importance floor) scope."""
    from wbworldgen.worldgen.enrichment import node_passes
    try:
        world_data = world_builder.load_world(world_id)
        compiled = world_builder.compile_world(world_data)
        all_nodes, layer_map = world_builder._collect_nodes_by_layer(compiled, layer_id)

        importance_floor = world_builder.default_importance_floor()
        upfront = {"importance_floor": importance_floor, "passes": {}}
        majors = ([n for n in all_nodes if n.get("importance", 0) >= importance_floor]
                  if importance_floor is not None else [])

        # Bucket nodes exactly like the run's SSE events do (map id first,
        # legacy layer id fallback) so the panel can merge the two sources.
        def _map_key(n):
            return n.get("map_id", n.get("layer_id", ""))

        passes = {}
        for spec in node_passes():
            per_layer = {}
            for lid, info in layer_map.items():
                lid_nodes = [n for n in all_nodes if _map_key(n) == lid]
                per_layer[lid] = {
                    "done": sum(1 for n in lid_nodes if spec.is_done(n)),
                    "total": sum(1 for n in lid_nodes if spec.in_domain(n)),
                }
            passes[spec.id] = {
                "label": spec.label,
                "unit": spec.unit,
                "done": sum(v["done"] for v in per_layer.values()),
                "total": sum(v["total"] for v in per_layer.values()),
                "per_layer": per_layer,
            }
            if importance_floor is not None:
                upfront["passes"][spec.id] = {
                    "done": sum(1 for n in majors if spec.is_done(n)),
                    "total": sum(1 for n in majors if spec.in_domain(n)),
                }
        return {"world_id": world_id, "passes": passes, "upfront": upfront}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))



