"""World-generation + terrain API routes (relocated from backend/api/server.py).

These endpoints are owned by the wb_worldgen module and mounted by the core
server at their original paths (/api/world/*, /api/terrain/*) so the frontend is
unaffected. Module-local state (the in-progress generation sessions/drafts) and
the shared engine services are populated by ``configure()`` which backend.py
calls from ``set_services``.
"""

import asyncio
import os
import json
import random
from typing import Optional, Any

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
        # A seed prompt alone is enough to draft: generation starts by saving
        # the session before the first step runs, so even a backend kill
        # mid-first-step leaves something to resume from.
        if state.get("steps") or state.get("seed_prompt"):
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
    #: Optional link to a saved scenario (backend.engine.scenario). The
    #: scenario's description and starting prompt are fed to every generation
    #: step, and the world remembers the link (metadata + compiled
    #: ``scenario_id``) so story creation can pair them back up.
    scenario_id: Optional[str] = None
    #: Optional free-form source material the world is grounded in alongside
    #: the seed prompt (API-level alternative to a saved scenario;
    #: ``scenario_id`` wins when both are given).
    scenario: str = ""
    skip_review: bool = False


def _ordered_ids_for(state: dict) -> list[str]:
    """Effective step order for a generation session (world_form skip
    filters); tolerates fakes/legacy builders without the resolver."""
    resolver = getattr(world_builder, "ordered_ids_for", None)
    if callable(resolver):
        return resolver(state)
    return world_builder._ordered_ids


def _prune_dynamic_skips(state: dict):
    """Drop generated data for steps the (re-rolled) world design now skips —
    otherwise stale entries would still compile into the world and the step
    cards would linger in the UI."""
    try:
        from wbworldgen.worldgen.design import dynamic_skips
    except ImportError:  # fakes/legacy builders in tests
        return
    for sid in dynamic_skips(state):
        state.get("steps", {}).pop(sid, None)


def _state_response(state: dict, **extra) -> dict:
    """A session-state payload plus the effective (post-skip) step order the
    client should render. Recomputed per response, never persisted — the
    wizard fetches the full pipeline once, so this is how it learns which
    steps the world's own design turned off."""
    return {"state": state, "effective_steps": _ordered_ids_for(state), **extra}


@router.get("/api/world/pipeline")
async def get_world_pipeline():
    return {"pipeline": world_builder.get_pipeline()}


async def _run_one_shot_generation(state: dict, session_id: str):
    """Generate every remaining step of a one-shot (skip_review) session.

    Steps that already hold data are skipped, so the same loop powers the
    initial "skip review" run and continues one that was interrupted — on
    Android the whole backend process can be killed while the app is
    backgrounded, and /api/world/continue re-enters here after the draft is
    resumed from disk. Every finished step is auto-saved to the on-disk draft,
    so an interruption loses at most the step that was in flight.
    """
    seed_prompt = state.get("seed_prompt", "")
    state.setdefault("steps", {})
    enrichment_steps = {"node_labeling", "node_descriptions"}
    terrain_task = None
    state["_generating"] = "all"
    try:
        # Iterate the full registry and re-check the effective list per
        # step: world_form (the first step) may turn steps off mid-run,
        # and a snapshot taken before it ran would not see those skips.
        for step_id in list(world_builder._ordered_ids):
            if step_id not in _ordered_ids_for(state):
                continue
            if step_id in enrichment_steps:
                continue
            if step_id == "terrain_generation" and terrain_task is not None:
                data = await terrain_task
                terrain_task = None
            elif state.get("steps", {}).get(step_id, {}).get("data"):
                # Generated before an interruption — keep it.
                continue
            else:
                # Strictly sequential: society_factions runs AFTER
                # natural_landmarks (its ``region`` field must reference the
                # areas that step authors). A former optimization ran the two
                # concurrently, so the factions call invented region names
                # that matched nothing and every faction place landed on a
                # random node — see docs/design/worldgen_quality_fixes.md.
                data = await world_builder.generate_step(step_id, state, seed_prompt)
            state["steps"][step_id] = {"data": data, "approved": True}
            _auto_save_draft(session_id)
            if (step_id == "layer_design" and "terrain_generation" in world_builder._steps
                    and not state["steps"].get("terrain_generation", {}).get("data")):
                # Terrain generation only reads layer_design data, so start
                # it now and let it overlap the layer_rules LLM call. Pin
                # the draft id first: the step assigns it, and it must not
                # race the concurrently-running step's own resolution.
                from wbworldgen.worldgen.persistence import resolve_world_id
                state["_draft_id"] = resolve_world_id(state)
                terrain_task = asyncio.create_task(
                    world_builder.generate_step("terrain_generation", state, seed_prompt))
    except Exception:
        if terrain_task is not None and not terrain_task.done():
            terrain_task.cancel()
        raise
    finally:
        state.pop("_generating", None)
    state["complete"] = True
    # Final draft save records draft_complete, so even a backend restart
    # before the player hits "Save World" keeps the finished result resumable.
    _auto_save_draft(session_id)


@router.post("/api/world/generate")
async def generate_world(request: WorldGenerateRequest, session_id: str = "default"):
    state = {"seed_prompt": request.seed_prompt, "steps": {}}
    if request.scenario_id:
        from backend.engine.scenario import ScenarioStore
        from wbworldgen.worldgen.prompts import scenario_grounding_text
        try:
            record = ScenarioStore(session_manager.data_dir).load_scenario(request.scenario_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail=f"Scenario '{request.scenario_id}' not found.")
        state["scenario_id"] = record.get("id", request.scenario_id)
        grounding = scenario_grounding_text(record)
        if grounding:
            state["scenario"] = grounding
    elif request.scenario.strip():
        state["scenario"] = request.scenario.strip()
    # Persisted so a relaunched client (Android kills the backgrounded PWA)
    # can restore the right screen and keep polling for progress. While a
    # generation is in flight, state["_generating"] names the running step
    # ("all" for one-shot mode) — the request handler keeps running server-side
    # even if the client that started it is gone, so polling /api/world/state
    # picks up the finished result.
    state["skip_review"] = request.skip_review
    world_gen_sessions[session_id] = state
    # Draft the session before any step runs: if the whole backend process is
    # killed during the first step (Android reaping Termux right after the
    # player submits and minimizes), the prompt + settings are already on disk
    # and /api/world/continue can regenerate from them.
    _auto_save_draft(session_id)

    if request.skip_review:
        await _run_one_shot_generation(state, session_id)
        return _state_response(state, complete=True)

    ordered = _ordered_ids_for(state)
    first_step = ordered[0] if ordered else None
    if not first_step:
        raise HTTPException(status_code=500, detail="No world building steps registered.")

    state["_generating"] = first_step
    try:
        data = await world_builder.generate_step(first_step, state, request.seed_prompt)
    finally:
        state.pop("_generating", None)
    state["steps"][first_step] = {"data": data, "approved": False}
    state["current_step"] = first_step
    _auto_save_draft(session_id)
    return _state_response(state, current_step=first_step)


@router.post("/api/world/continue")
async def continue_world_generation(session_id: str = "default"):
    """Pick an interrupted generation back up.

    The relaunched client calls this after restoring a session that stopped
    without completing — the backend process was killed mid-run (Android
    reaping Termux while the app was minimized) and the draft was resumed
    from disk, possibly with zero finished steps. One-shot sessions rerun the
    remaining pipeline; review-mode sessions regenerate the step that was in
    flight so there is something to review again. Idempotent while a run is
    already in flight, and a no-op when a step is already awaiting review.
    """
    state = _get_world_state(session_id)
    if not state.get("steps") and not state.get("seed_prompt"):
        raise HTTPException(status_code=404, detail="No world generation session to continue.")
    if state.get("_generating") or state.get("complete"):
        return _state_response(state)

    if state.get("skip_review"):
        await _run_one_shot_generation(state, session_id)
        return _state_response(state, complete=True)

    state.setdefault("steps", {})
    ordered = _ordered_ids_for(state)

    # A step already awaiting the player's review means nothing was lost —
    # just make sure the pointer names it and hand the session back.
    reviewable = [sid for sid in ordered
                  if state["steps"].get(sid, {}).get("data")
                  and not state["steps"][sid].get("approved")]
    if reviewable:
        if state.get("current_step") not in reviewable:
            state["current_step"] = reviewable[0]
        return _state_response(state, current_step=state["current_step"])

    target = next((sid for sid in ordered
                   if not state["steps"].get(sid, {}).get("data")), None)
    if target is None:
        # Every step generated and approved: the interruption hit right at
        # the finish line — just mark the session complete.
        state["current_step"] = None
        state["complete"] = True
        _auto_save_draft(session_id)
        return _state_response(state, complete=True)

    if target in ("node_labeling", "node_descriptions"):
        # Mirror approve-step: enrichment steps start from defaults, no LLM.
        step = world_builder._steps[target]
        default_data = {k: v.get("default", 0) for k, v in step.schema.items()}
        default_data["results"] = []
        state["steps"][target] = {"data": default_data, "approved": False}
        state["current_step"] = target
        _auto_save_draft(session_id)
        return _state_response(state, current_step=target)

    state["_generating"] = target
    try:
        data = await world_builder.generate_step(target, state, state.get("seed_prompt", ""))
    finally:
        state.pop("_generating", None)
    state["steps"][target] = {"data": data, "approved": False}
    state["current_step"] = target
    _auto_save_draft(session_id)
    return _state_response(state, current_step=target)


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


class WorldPromptQuestionsRequest(BaseModel):
    #: The current World Prompt draft — may be empty (interview from scratch).
    current_text: Optional[str] = None
    #: Question/answer pairs from previous interview rounds, oldest first.
    #: Each item: {"question": str, "answer": str} (empty answer = skipped).
    history: list[dict] = []
    #: Optional linked scenario: its content counts as already decided.
    scenario_id: Optional[str] = None


@router.post("/api/world/prompt-questions")
async def world_prompt_questions(request: WorldPromptQuestionsRequest):
    """One round of the world-prompt interview: the LLM reads the draft (and
    any previous rounds) and asks a few clarifying questions about details the
    prompt leaves open. An empty draft is fine — the questions then help the
    player shape the world from scratch."""
    from wbworldgen.worldgen.prompts import build_world_questions_messages
    try:
        from backend.engine.llm import LLMProviderError
    except ImportError:  # isolated module-test context: core pkg not on path
        LLMProviderError = RuntimeError

    scenario = _load_scenario_or_404(request.scenario_id)
    messages = build_world_questions_messages(
        (request.current_text or "").strip(), request.history, scenario)
    try:
        content = await engine.llm.simple_completion(
            messages,
            model=engine.llm.storyteller_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "world_prompt_questions", "step": "world_build:seed_prompt"},
        )
        raw = json.loads(content).get("questions")
        questions = [str(q).strip() for q in raw if str(q).strip()] if isinstance(raw, list) else []
    except LLMProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=502, detail=f"World prompt interview returned unusable output: {exc}")
    if not questions:
        raise HTTPException(status_code=502, detail="World prompt interview returned no questions.")
    return {"questions": questions}


class FoldWorldAnswersRequest(BaseModel):
    #: The current World Prompt draft — may be empty (answers become the draft).
    current_text: Optional[str] = None
    #: This round's question/answer pairs: {"question": str, "answer": str}.
    answers: list[dict] = []
    #: Optional linked scenario the prompt must stay consistent with.
    scenario_id: Optional[str] = None


@router.post("/api/world/fold-answers")
async def fold_world_answers(request: FoldWorldAnswersRequest):
    """Fold a round of interview answers into the World Prompt: every answer
    lands — added or rewritten into the prompt as needed — while parts the
    answers don't touch keep the player's text."""
    from wbworldgen.worldgen.prompts import build_world_prompt_fold_messages

    answered = [a for a in request.answers or []
                if str(a.get("answer") or "").strip()]
    if not answered:
        raise HTTPException(status_code=400,
                            detail="Answer at least one question to update the prompt.")

    scenario = _load_scenario_or_404(request.scenario_id)
    messages = build_world_prompt_fold_messages(
        (request.current_text or "").strip(), answered, scenario)
    text = await _seed_prompt_completion(
        messages, "world_prompt_fold", "World prompt update")
    return {"text": text}


@router.post("/api/world/generate-step/{step_id}")
async def generate_world_step(step_id: str, body: dict = None, session_id: str = "default"):
    state = _get_world_state(session_id)

    if step_id not in world_builder._steps:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_id}")

    note = body.get("note", "") if body else ""
    config = body.get("data", None) if body else None

    state["_generating"] = step_id
    try:
        data = await world_builder.generate_step(
            step_id, state,
            state.get("seed_prompt", ""),
            user_note=note,
            config=config,
        )
    finally:
        state.pop("_generating", None)
    state["steps"][step_id] = {"data": data, "approved": False, "note": note}
    if step_id == "world_form":
        _prune_dynamic_skips(state)

    ordered = _ordered_ids_for(state)
    if step_id not in ordered:
        raise HTTPException(status_code=409,
                            detail="This step was skipped by the world design — "
                                   "re-roll World Design to bring it back.")
    current_idx = ordered.index(step_id)
    for idx in range(current_idx + 1, len(ordered)):
        downstream_id = ordered[idx]
        if downstream_id in state.get("steps", {}):
            state["steps"][downstream_id]["approved"] = False

    state["current_step"] = step_id
    state["complete"] = False
    _auto_save_draft(session_id)
    return _state_response(state, step=step_id, data=data)


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
    if step_id == "world_form":
        _prune_dynamic_skips(state)

    ordered = _ordered_ids_for(state)
    if step_id not in ordered:
        raise HTTPException(status_code=409,
                            detail="This step was skipped by the world design — "
                                   "re-roll World Design to bring it back.")
    current_idx = ordered.index(step_id)

    if was_already_approved:
        for idx in range(current_idx + 1, len(ordered)):
            downstream_id = ordered[idx]
            if downstream_id in state.get("steps", {}):
                state["steps"][downstream_id]["approved"] = False

        next_unapproved = None
        for idx in range(current_idx + 1, len(ordered)):
            check_id = ordered[idx]
            step_entry = state.get("steps", {}).get(check_id, {})
            if step_entry.get("data") and not step_entry.get("approved"):
                next_unapproved = check_id
                break

        if next_unapproved:
            state["current_step"] = next_unapproved
            state["complete"] = False
            _auto_save_draft(session_id)
            return _state_response(state, current_step=next_unapproved)
        else:
            state["current_step"] = None
            state["complete"] = True
            _auto_save_draft(session_id)
            return _state_response(state, complete=True)

    next_step = ordered[current_idx + 1] if current_idx + 1 < len(ordered) else None

    if next_step:
        if next_step in ("node_labeling", "node_descriptions"):
            step = world_builder._steps[next_step]
            default_data = {k: v.get("default", 0) for k, v in step.schema.items()}
            default_data["results"] = []
            state["steps"][next_step] = {"data": default_data, "approved": False}
            state["current_step"] = next_step
            _auto_save_draft(session_id)
            return _state_response(state, current_step=next_step, data=default_data)

        state["_generating"] = next_step
        try:
            data = await world_builder.generate_step(next_step, state, state.get("seed_prompt", ""))
        except Exception as e:
            import traceback
            logger.error(f"Map generation failed for step {next_step}: {traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"Failed to generate step '{next_step}': {str(e)}")
        finally:
            state.pop("_generating", None)
        state["steps"][next_step] = {"data": data, "approved": False}
        state["current_step"] = next_step
        _auto_save_draft(session_id)
        return _state_response(state, current_step=next_step, data=data)

    state["current_step"] = None
    state["complete"] = True
    _auto_save_draft(session_id)
    return _state_response(state, complete=True)


@router.get("/api/world/state")
async def get_world_state(session_id: str = "default"):
    return _state_response(_get_world_state(session_id), world_id=_get_world_draft_id(session_id))


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
    ordered = _ordered_ids_for(world_data)
    target_idx = None
    for i, sid in enumerate(ordered):
        if sid == step_id:
            target_idx = i
            break
    if target_idx is None:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_id}")

    for i in range(target_idx):
        sid = ordered[i]
        if sid not in steps or not isinstance(steps.get(sid), dict):
            steps[sid] = {}
        if not isinstance(steps[sid].get("data"), dict):
            steps[sid]["data"] = {}
        steps[sid]["approved"] = True

    for i in range(target_idx, len(ordered)):
        sid = ordered[i]
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
    for key in ("template_id", "template_vocab", "scenario", "scenario_id"):
        if world_data.get(key):
            state[key] = world_data[key]
    world_gen_sessions[session_id] = state

    note_for_layer = ""
    for i in range(target_idx):
        sid = ordered[i]
        sd = steps.get(sid, {}).get("data", {})
        if sid == "hierarchy_design" and isinstance(sd, dict) and sd.get("parallel_maps"):
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
        "pipeline_position": f"{target_idx + 1}/{len(ordered)}",
        "state": state,
        "effective_steps": _ordered_ids_for(state),
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
        save_id = session_manager.derive_save_id(save_id)
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
    draft_id = _get_world_draft_id(session_id)
    try:
        world_id = world_builder.save_world(request.world_id, state)
        if draft_id and draft_id != world_id:
            # The auto-saved draft can live under a different id than the
            # final world (drafts started before lore names the world get a
            # random id) — drop it so the list doesn't keep a phantom
            # "In Progress" copy of a world that was just saved.
            try:
                world_builder.delete_world(draft_id)
            except FileNotFoundError:
                pass
        world_gen_sessions.pop(session_id, None)
        world_draft_ids.pop(session_id, None)
        return {"world_id": world_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/api/world/discard")
async def discard_world(session_id: str = "default"):
    draft_id = world_draft_ids.get(session_id)
    if draft_id:
        # Drafts are created eagerly when generation starts, so one that never
        # produced a step is pure clutter — remove it. Drafts with real steps
        # stay on disk: the list's Resume button keeps them recoverable.
        try:
            if not world_builder.load_world(draft_id).get("steps"):
                world_builder.delete_world(draft_id)
        except Exception:
            pass
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
        return _state_response(_get_world_state(session_id))
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
        return _state_response(state)
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


class EnrichCommitRequest(BaseModel):
    step_id: str


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
                if item.get("type") == "node":
                    _sync_enrichment_result_to_draft(session_id, world_id, item)
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


@router.get("/api/world/{world_id}/enrich/passes")
async def enrich_passes(world_id: str):
    """The enrichment pass slice of the capability catalog: what passes are
    registered, so the panel renders one row per pass instead of hardcoding
    phases (a dropped-in pass module appears here without frontend edits)."""
    from wbworldgen.worldgen.enrichment import registered_passes
    return {
        "world_id": world_id,
        "passes": [
            {"id": s.id, "label": s.label, "description": s.description,
             "unit": s.unit, "batchable": s.batchable}
            for s in registered_passes()
        ],
    }


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

        if step_id == "node_descriptions":
            # Names exist now — build the child maps hierarchy_design planned
            # for upfront creation (seed-central locations). Best-effort: an
            # unmatched or failed entry simply expands lazily during play.
            try:
                summary = await world_builder.pregenerate_planned_maps(world_id)
                if summary.get("built"):
                    logger.info("pregenerated %d planned child maps for %s",
                                len(summary["built"]), world_id)
            except Exception:
                logger.exception("pregeneration of planned child maps failed")

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

