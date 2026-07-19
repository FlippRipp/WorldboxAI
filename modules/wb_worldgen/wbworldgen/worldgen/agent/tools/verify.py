"""The evaluate tool: the agent's verification instrument (D3).

Wraps the evaluator so the agent may check its work at any time — the same
evaluation the harness runs at the done-gate, so a clean mid-build evaluate
is an honest signal the gate will pass."""

from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen.agent.evaluator import evaluate_world
from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool


async def evaluate(ctx, map_id: str = None) -> dict:
    builder = ctx.builder
    compiled = builder.services.compiled.load(ctx.world_id)
    if map_id is not None and map_id not in _ms.maps_by_id(compiled):
        raise ToolError(
            f"Unknown map '{map_id}'. This world's maps: "
            f"{', '.join(_ms.maps_by_id(compiled)) or 'none yet'}")
    world_state = builder.load_world(ctx.world_id)
    return await evaluate_world(
        builder.services, world_state, compiled, map_id=map_id,
        major_floor=type(builder).MAJOR_IMPORTANCE_FLOOR,
        builder=builder, world_id=ctx.world_id, on_event=ctx.on_event)


register_tool(ToolSpec(
    id="evaluate",
    label="Evaluate the build",
    description=(
        "Full verification: the deterministic lint report, (when world "
        "rules exist) an LLM critique of the built content against those "
        "rules, and (when the brief carries notes) the note verifier — a "
        "read-only agent that checks every agreed note against the world "
        "and reports per-note verdicts. Returns findings with stable keys; "
        "severity 'problem' findings block a done claim until fixed or "
        "explicitly accepted — note findings (note:*) are NEVER "
        "auto-accepted. This is the same evaluation the done-gate runs — "
        "evaluate before claiming done."
    ),
    invoke=evaluate,
    params={"map_id": {"type": "string",
                       "description": "Evaluate only one map."}},
))
