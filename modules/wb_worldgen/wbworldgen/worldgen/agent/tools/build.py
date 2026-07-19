"""Build tools: the agent's write surface over the capability catalogs (D1).

``run_step`` and ``run_pass`` drive exactly the orchestration the wizard
drives (``generate_step``, ``enrich_run`` — P5) with B3's declared data
dependencies enforced per action (P7): a step or pass whose ``requires``
names an artifact this world has not produced yet is rejected with an error
telling the agent what to run first. ``run_custom_pass`` is the one ad-hoc
capability (`pass:custom`): an agent-authored instruction run as an
ephemeral ``PassSpec`` through the same enrichment engine — same scheduler,
retries, flush and cancel — writing a namespaced ``custom_<slot>`` node
field so core fields cannot be clobbered by construction. ``patch_step``
is the user-parity step-data edit; map and terrain data are deliberately
not patchable (structural surgery and terrain edits are withheld in v1 —
regenerate the owning step or use edit_node instead)."""

import re

from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool
from wbworldgen.worldgen.base import USES_ENRICHMENT
from wbworldgen.worldgen.catalog import produced_artifacts
from wbworldgen.worldgen.enrichment.context import build_enrichment_context
from wbworldgen.worldgen.enrichment.registry import PassSpec, get_pass
from wbworldgen.worldgen.enrichment.passes.common import call_with_retries, terrain_line

_SLOT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Step entries the engine writes through its own paths; raw patches would
#: bypass their invariants (enrichment cache coherence, map structure).
#: terrain_generation is here because its entries are records of rasters on
#: disk — a patch would fabricate geography no raster backs, and downstream
#: steps trust those summaries as generated ground truth (terrain edits are
#: withheld until v2b).
_UNPATCHABLE_STEPS = ("map_generation", "node_labeling", "node_descriptions",
                      "terrain_generation")


def _require_artifacts(kind: str, cap, world_state: dict, compiled: dict, steps: dict):
    """B3's per-action precondition (P7): every artifact the capability
    requires must already be produced by this world's content."""
    produced = produced_artifacts(world_state, compiled, steps=steps)
    missing = [r for r in (getattr(cap, "requires", ()) or ()) if r not in produced]
    if missing:
        raise ToolError(
            f"{kind} '{cap.id}' requires artifact(s) {missing} which this world "
            f"has not produced yet (available: {sorted(produced) or 'none'}). "
            "Run the capability that produces them first — the catalog lists "
            "requires/produces per entry.")


def _validate_scope(compiled: dict, map_id: str = None, node_ids: list = None):
    if map_id is not None and map_id not in _ms.maps_by_id(compiled):
        raise ToolError(
            f"Unknown map '{map_id}'. This world's maps: "
            f"{', '.join(_ms.maps_by_id(compiled)) or 'none yet'}")
    if node_ids:
        index = _ms.node_index(compiled)
        unknown = [nid for nid in node_ids if nid not in index]
        if unknown:
            raise ToolError(
                f"Unknown node id(s): {unknown}. Use read_map to list a map's "
                "node ids.")


def _map_inventory(compiled: dict) -> list:
    out = []
    for rec in _ms.maps_by_id(compiled).values():
        nodes = rec.get("nodes", [])
        out.append({"map_id": rec.get("map_id"), "label": rec.get("label", ""),
                    "level_type": rec.get("level_type", ""),
                    "nodes": len(nodes),
                    "named": sum(1 for n in nodes if n.get("name"))})
    return out


async def run_step(ctx, step_id: str, config: dict = None, note: str = "") -> dict:
    """Generate (or regenerate) one pipeline step and save it approved.
    ``note`` is the steering channel — regeneration with a steering note is
    the v1 recourse for structural problems (D1)."""
    builder = ctx.builder
    steps = builder.steps_by_id()
    step = steps.get(step_id)
    if step is None:
        raise ToolError(
            f"Unknown step '{step_id}'. Registered steps: {', '.join(steps)}")
    if getattr(step, "uses", "") == USES_ENRICHMENT:
        raise ToolError(
            f"Step '{step_id}' is engine-driven enrichment, not a generable "
            "step — use run_pass ('label'/'describe') instead.")

    world_state = builder.load_world(ctx.world_id)
    compiled = builder.services.compiled.load(ctx.world_id)
    _require_artifacts("step", step, world_state, compiled, steps)

    # Terrain (and anything else writing per-world artifacts mid-generation)
    # resolves its target directory from this pin.
    world_state["_draft_id"] = ctx.world_id
    data = await builder.generate_step(
        step_id, world_state, world_state.get("seed_prompt", ""),
        user_note=note or "", config=config)
    builder.save_step(ctx.world_id, step_id, {"data": data, "approved": True})

    if step_id == "map_generation":
        fresh = builder.services.compiled.load(ctx.world_id)
        return {"step_id": step_id, "saved": True,
                "maps": _map_inventory(fresh),
                "note": "Nodes are unnamed until the label pass runs; use "
                        "read_map for the full structure."}
    return {"step_id": step_id, "saved": True, "data": data,
            "note": "Downstream steps are not re-run automatically — "
                    "regenerate them yourself if they must reflect this."}


async def patch_step(ctx, step_id: str, data: dict) -> dict:
    """Merge authored values into one step's data (user-parity with the
    wizard's save-step surface). Top-level keys are set; a null value
    removes the key. Creates the step entry when absent."""
    builder = ctx.builder
    steps = builder.steps_by_id()
    if step_id not in steps:
        raise ToolError(
            f"Unknown step '{step_id}'. Registered steps: {', '.join(steps)}")
    if step_id in _UNPATCHABLE_STEPS:
        raise ToolError(
            f"Step '{step_id}' is not patchable: map structure, terrain "
            "rasters and enrichment state have their own write paths. Use "
            "edit_node for names/descriptions, run_pass for enrichment, or "
            "run_step to regenerate (structural surgery and terrain edits "
            "are deliberately withheld in v1).")
    if not data:
        raise ToolError("patch_step: 'data' must carry at least one key to set.")

    world_state = builder.load_world(ctx.world_id)
    if step_id == "world_rules" and "custom_rules" in data:
        from wbworldgen.worldgen.steps.world_rules import brief_rules
        agreed = brief_rules(world_state)
        patched = data.get("custom_rules")
        patched = ([str(r).strip() for r in patched]
                   if isinstance(patched, list) else [])
        missing = [r for r in agreed if r not in patched]
        if missing:
            raise ToolError(
                "patch_step: custom_rules must keep every co-authored brief "
                f"rule verbatim; missing: {missing}. The brief's rules are "
                "fixed design decisions — extend them, don't drop them.")
    entry = world_state.get("steps", {}).get(step_id) or {}
    merged = dict(entry.get("data") or {})
    merged.update(data)
    merged = {k: v for k, v in merged.items() if v is not None}
    builder.save_step(ctx.world_id, step_id,
                      {"data": merged, "approved": True})
    return {"step_id": step_id, "saved": True, "data_keys": sorted(merged)}


async def run_pass(ctx, pass_id: str, map_id: str = None, node_ids: list = None,
                   importance_floor: int = None, count: int = None,
                   rework: bool = False, guidance: str = "") -> dict:
    """Run one registered enrichment pass, scoped and optionally steered.
    ``guidance`` threads into every LLM call of the run (the C1 guidance
    channel) — with ``rework``, steered regeneration is the primary fix
    instrument for content findings (D1)."""
    builder = ctx.builder
    try:
        spec = get_pass(pass_id)
    except ValueError as e:
        from wbworldgen.worldgen.enrichment.registry import registered_passes
        raise ToolError(
            f"{e}. Registered passes: "
            f"{', '.join(s.id for s in registered_passes())}")

    world_state = builder.load_world(ctx.world_id)
    compiled = builder.services.compiled.load(ctx.world_id)
    _require_artifacts("pass", spec, world_state, compiled, builder.steps_by_id())
    _validate_scope(compiled, map_id, node_ids)
    if spec.unit == "map" and (node_ids or importance_floor is not None):
        raise ToolError(
            f"Pass '{pass_id}' works per map; node_ids/importance_floor do "
            "not apply — scope it with map_id (and count for max maps).")

    summary = await builder.enrich_run(
        ctx.world_id, phase=pass_id, count=count, layer_filter=map_id,
        rework=rework, node_ids=node_ids, importance_floor=importance_floor,
        guidance=guidance or None, on_event=ctx.on_event)
    return {"pass_id": pass_id, "summary": summary}


async def generate_custom_content(services, node: dict, context: dict,
                                  instruction: str) -> str:
    """One custom-pass LLM call for one node: the agent's instruction over
    the standard node context. Module-level so tests monkeypatch it — the
    same patch-point contract as ``label.generate_label``."""
    world = context.get("world", {})
    region = context.get("region", {})
    layer = context.get("layer", {})
    neighbors = [n.get("name") for n in context.get("neighbors", []) if n.get("name")]
    system = (
        "You are a world-building AI running a custom content pass over map "
        "locations, one location per call. Follow the pass instruction "
        "precisely. Output ONLY the requested content as plain text — no "
        "JSON wrapper, no preamble, no markdown fences.")
    if context.get("guidance"):
        system += f"\n\nSteering note for this run: {context['guidance']}"
    user_msg = f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Region: {region.get('name', 'unknown')} ({region.get('terrain', '')}, {region.get('climate', '')})
{terrain_line(context.get('terrain', {}))}
Location: {node.get('name', 'Unnamed')}
Type: {node.get('type', 'waypoint')}
Label: {node.get('label_description', '')}
Description: {node.get('description', '')}
Layer: {layer.get('name', 'surface')} ({layer.get('type', 'surface')})
Nearby: {', '.join(neighbors) if neighbors else 'nothing named'}

Pass instruction: {instruction}"""
    content = await services.llm.simple_completion(
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user_msg}],
        model=services.llm.reader_model,
        temperature=services.temperature or 0.9,
        inspector_ctx={"call_type": "world_build", "step": "enrich:custom"},
    )
    content = re.sub(r"^```[a-zA-Z]*\s*", "", (content or "").strip())
    content = re.sub(r"\s*```$", "", content).strip()
    if not content:
        raise ValueError("custom pass returned empty content")
    return content


def _custom_spec(prompt: str, slot: str) -> PassSpec:
    field_name = f"custom_{slot}"

    async def _run(services, node, state):
        context = build_enrichment_context(node, state.all_nodes, state.compiled,
                                           include_descriptions=True)
        if state.guidance:
            context["guidance"] = state.guidance
        result = await call_with_retries(
            services,
            lambda: generate_custom_content(services, node, context, prompt),
            what=f"Custom pass '{slot}'", node_id=node.get("id"))
        return None if result is None else {field_name: result}

    return PassSpec(
        id=f"custom:{slot}",
        label=f"Custom pass ({slot})",
        description=f"Agent-authored content pass writing node field '{field_name}'.",
        unit="node",
        run=_run,
        is_done=lambda n: bool(n.get(field_name)),
        in_domain=lambda n: bool(n.get("name")),
        requires=("maps", "labels"),
        summary_key=field_name,
    )


async def run_custom_pass(ctx, prompt: str, slot: str, map_id: str = None,
                          node_ids: list = None, importance_floor: int = None,
                          count: int = None, rework: bool = False) -> dict:
    """The `pass:custom` capability (D1): run an agent-authored instruction
    over named nodes as a one-off pass through the regular enrichment
    engine, storing each node's output under ``custom_<slot>``."""
    builder = ctx.builder
    if not _SLOT_RE.match(slot or ""):
        raise ToolError(
            f"Invalid slot '{slot}': must match [a-z][a-z0-9_]{{0,31}} "
            "(it becomes the node field custom_<slot>).")
    if not (prompt or "").strip():
        raise ToolError("run_custom_pass: 'prompt' must carry the pass instruction.")

    world_state = builder.load_world(ctx.world_id)
    compiled = builder.services.compiled.load(ctx.world_id)
    spec = _custom_spec(prompt.strip(), slot)
    _require_artifacts("pass", spec, world_state, compiled, builder.steps_by_id())
    _validate_scope(compiled, map_id, node_ids)

    summary = await builder.enrich_run(
        ctx.world_id, spec=spec, count=count, layer_filter=map_id,
        rework=rework, node_ids=node_ids, importance_floor=importance_floor,
        on_event=ctx.on_event)
    return {"pass_id": spec.id, "field": f"custom_{slot}", "summary": summary}


register_tool(ToolSpec(
    id="run_step",
    label="Run a pipeline step",
    description=(
        "Generate (or regenerate) one pipeline step through the same "
        "orchestration the wizard uses, and save the result approved. "
        "Preconditions are checked against the step's declared requires. "
        "Regenerating with a steering note is the v1 fix for structural "
        "problems; downstream steps are not re-run automatically."
    ),
    invoke=run_step,
    mutates=True,
    params={
        "step_id": {"type": "string", "required": True,
                    "description": "A registered step id (see the catalog)."},
        "config": {"type": "object",
                   "description": "Step config, e.g. {\"total_nodes\": 60} "
                                  "for map_generation."},
        "note": {"type": "string",
                 "description": "Steering note threaded into the step's "
                                "generation prompt."},
    },
))

register_tool(ToolSpec(
    id="patch_step",
    label="Patch step data",
    description=(
        "Set (or remove, via null) top-level keys of one step's saved data "
        "— the same write surface the wizard's editor uses. Map structure, "
        "terrain and enrichment state are not patchable; use edit_node, "
        "run_pass, or regenerate instead."
    ),
    invoke=patch_step,
    mutates=True,
    params={
        "step_id": {"type": "string", "required": True,
                    "description": "A registered step id."},
        "data": {"type": "object", "required": True,
                 "description": "Keys to set on the step's data; null "
                                "removes a key."},
    },
))

register_tool(ToolSpec(
    id="run_pass",
    label="Run an enrichment pass",
    description=(
        "Run one registered enrichment pass (label, describe, review, ...) "
        "over the world or a scope of it, with the run-level guidance "
        "channel for steering. rework=true regenerates existing output — "
        "steered rework over explicit node_ids is the primary fix for "
        "content findings."
    ),
    invoke=run_pass,
    mutates=True,
    params={
        "pass_id": {"type": "string", "required": True,
                    "description": "A registered pass id (see the catalog)."},
        "map_id": {"type": "string", "description": "Limit the run to one map."},
        "node_ids": {"type": "list", "item_type": "string",
                     "description": "Explicit target nodes (wins over "
                                    "importance_floor)."},
        "importance_floor": {"type": "integer", "min": 0, "max": 10,
                             "description": "Only nodes at or above this "
                                            "importance."},
        "count": {"type": "integer", "min": 1,
                  "description": "Max work items this run (nodes, or maps "
                                 "for map passes)."},
        "rework": {"type": "boolean",
                   "description": "Regenerate output that already exists."},
        "guidance": {"type": "string",
                     "description": "Steering note threaded into every LLM "
                                    "call of the run."},
    },
))

register_tool(ToolSpec(
    id="run_custom_pass",
    label="Run a custom content pass",
    description=(
        "Run a one-off, agent-authored content instruction over named "
        "locations (the pass:custom capability): each node's output is "
        "stored under the namespaced field custom_<slot>. Rides the regular "
        "enrichment engine — bespoke content goes through a validated "
        "capability, not around it."
    ),
    invoke=run_custom_pass,
    mutates=True,
    params={
        "prompt": {"type": "string", "required": True,
                   "description": "The pass instruction, applied per node."},
        "slot": {"type": "string", "required": True,
                 "description": "Output slot name ([a-z][a-z0-9_]{0,31}); "
                                "stored as node field custom_<slot>."},
        "map_id": {"type": "string", "description": "Limit the run to one map."},
        "node_ids": {"type": "list", "item_type": "string",
                     "description": "Explicit target nodes."},
        "importance_floor": {"type": "integer", "min": 0, "max": 10,
                             "description": "Only nodes at or above this "
                                            "importance."},
        "count": {"type": "integer", "min": 1,
                  "description": "Max nodes this run."},
        "rework": {"type": "boolean",
                   "description": "Regenerate slots that already exist."},
    },
))
