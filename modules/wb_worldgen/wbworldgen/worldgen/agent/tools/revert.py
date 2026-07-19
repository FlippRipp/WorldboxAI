"""The revert tool (v2c): restore the world to a pre-action checkpoint.

The harness snapshots the world before every mutating tool call, keyed by
that action's log index — this tool is the way back when a mutation made
the world worse. World CONTENT rewinds byte-exact; the agent's own state
(todo, observations, budgets) and the brief (the user's contract, note
amendments included) do not — a revert is `git revert`, not a time
machine. Revert is itself a mutating action, so it gets its own checkpoint
and can be reverted too: the whole build timeline stays reachable until
the build ends and the store is cleared.

Born from the Ecstasy Veil live run: a silently-destructive map_generation
re-run cost the agent six turns of diagnosis plus a from-memory
"restoration" that produced a sibling map it could not tell from the
original. With checkpoints that recovery is one call, and actually exact.
"""

from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool
from wbworldgen.worldgen.agent.tools.build import _map_inventory


async def revert(ctx, checkpoint: int) -> dict:
    if ctx.build is None:
        raise ToolError("revert is only available inside an agent build — "
                        "checkpoints are build-scoped.")
    store = ctx.builder.services.enrichment_store
    tags = store.list_checkpoints(ctx.world_id)
    if not tags:
        raise ToolError(
            "No checkpoints exist yet. Only mutating actions create them — "
            "each mutating action's observation carries its checkpoint id.")
    tag = str(checkpoint)
    if tag not in tags:
        raise ToolError(
            f"No checkpoint '{checkpoint}'. Available checkpoints — each is "
            "the world state just before that action index ran: "
            + ", ".join(tags))
    store.restore_world(ctx.world_id, tag)
    # The compiled cache is facade-owned; a restore behind its back must
    # invalidate it or every later read serves the abandoned timeline.
    ctx.builder.services.compiled.invalidate(ctx.world_id)
    fresh = ctx.builder.services.compiled.load(ctx.world_id)
    return {
        "reverted_to_before_action": checkpoint,
        "maps": _map_inventory(fresh),
        "note": ("World content restored byte-exact to the state before "
                 f"action {checkpoint}. Your todo, observations and budgets "
                 "are unchanged, and the brief (rules and notes, amendments "
                 "included) always stays current. Earlier reads may now be "
                 "stale — re-read what you rely on."),
    }


register_tool(ToolSpec(
    id="revert",
    label="Revert to a checkpoint",
    description=(
        "Restore the world to the state just before an earlier mutating "
        "action ran: every mutating action is checkpointed automatically "
        "and its observation carries the checkpoint id. Use this when a "
        "mutation made the world worse — a regeneration replaced content "
        "you needed, an edit or surgery broke structure — instead of "
        "rebuilding lost state from memory: the restore is byte-exact. "
        "World content only: your todo, observations, budgets and the "
        "brief are untouched. Revert is itself checkpointed, so a revert "
        "can be reverted."
    ),
    invoke=revert,
    mutates=True,
    params={
        "checkpoint": {"type": "integer", "required": True, "min": 0,
                       "description": "The checkpoint to restore: the "
                                      "action-log index carried as "
                                      "'checkpoint' in that mutating "
                                      "action's observation."},
    },
))
