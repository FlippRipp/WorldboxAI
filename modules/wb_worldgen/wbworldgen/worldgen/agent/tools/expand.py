"""The child-map expansion tool (v2d): the systemic fix for the wall both
live builds hit — hierarchies plan child maps (planets, cities, interiors)
that no agent tool could create, while ``read_node`` advertised nodes as
"expandable". ``expand_node`` wraps the facade surface play-time expansion
and the pregenerate pass already trust (P5, the v2a wrap-the-shared-surface
pattern): cached, invariant-handling, terrain-aware (a terrain-flagged
child rasterizes at expansion time — which is also how per-planet terrain
is reached, per the Crucible Stars honest-terrain contract).

There is deliberately no batch pregenerate tool: the hierarchy's
``pregenerate`` plans (read_step 'hierarchy_design') are a work list the
agent drives one node per action — one observation, one checkpoint (v2c)
and one budget unit per child map, instead of one long opaque call."""

from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool
from wbworldgen.worldgen.expansion.maps_expand import (
    MAX_DEPTH, allowed_child_levels, is_expandable)


def _reject_unexpandable(compiled: dict, map_id: str, node: dict, node_id: str):
    """P7: say WHY the node cannot open into a child map — each reason
    names its fix."""
    if not node.get("name"):
        raise ToolError(
            f"Node '{node_id}' is unnamed — only named nodes expand. Name it "
            "first (run_pass 'label' with node_ids, or edit_node).")
    parent = _ms.get_map(compiled, map_id) or {}
    if len(_ms.breadcrumb(compiled, map_id)) >= MAX_DEPTH:
        raise ToolError(
            f"Map '{map_id}' is already {MAX_DEPTH} levels deep — the "
            "hierarchy stops here.")
    if not allowed_child_levels(compiled, parent):
        raise ToolError(
            f"No child level exists below '{parent.get('level_type', map_id)}' "
            "in this world's hierarchy — if this world needs deeper levels, "
            "regenerate hierarchy_design with a steering note.")
    raise ToolError(f"Node '{node_id}' cannot be expanded.")


def _child_summary(record: dict, connections: list, existed: bool) -> dict:
    nodes = record.get("nodes", []) or []
    named = sum(1 for n in nodes if n.get("name"))
    unnamed = len(nodes) - named
    if existed:
        note = ("This child map already existed (expansion is cached) — "
                "nothing was generated. Pass force=true to regenerate it "
                "(checkpointed, so revert covers a bad regeneration), with "
                "'note' as steering.")
    elif unnamed:
        note = (f"{unnamed} node(s) are unnamed: they detail lazily during "
                "play, or run_pass 'label' scoped to this map_id to name "
                "majors now. Use read_map for the full structure.")
    else:
        note = ("The author named and described every location — no label "
                "pass is pending. Use read_map for the full structure.")
    summary = {
        "map_id": record.get("map_id"),
        "label": record.get("label", ""),
        "level_type": record.get("level_type", ""),
        "nodes": len(nodes),
        "named": named,
        "connections": len(connections or []),
        "existed": existed,
        "note": note,
    }
    if (record.get("config") or {}).get("terrain"):
        summary["terrain"] = True
    return summary


async def expand_node(ctx, node_id: str, level_type: str = None,
                      force: bool = False, note: str = "") -> dict:
    builder = ctx.builder
    compiled = builder.services.compiled.load(ctx.world_id)
    node = builder.get_map_node(ctx.world_id, node_id)
    if node is None:
        raise ToolError(
            f"Unknown node '{node_id}'. Use read_map to list a map's node ids.")
    map_id = _ms.map_of_node(compiled, node_id)
    if map_id is None:
        raise ToolError(f"Node '{node_id}' belongs to no known map.")

    existing = _ms.children_by_anchor(compiled).get((map_id, node_id))
    if existing and not force:
        record = _ms.maps_by_id(compiled).get(existing[0]) or {}
        return _child_summary(record, [], existed=True)
    if not existing and not is_expandable(compiled, map_id, node):
        _reject_unexpandable(compiled, map_id, node, node_id)

    if level_type is not None:
        allowed = [str(l.get("level_type", "")).strip()
                   for l in allowed_child_levels(
                       compiled, _ms.get_map(compiled, map_id) or {})]
        if str(level_type).strip() not in allowed:
            raise ToolError(
                f"level_type '{level_type}' is not an allowed child level "
                f"here (allowed: {', '.join(allowed)}). Omit it to let the "
                "author choose.")

    bundle = await builder.expand_node(
        ctx.world_id, map_id, node_id, force=force,
        level_type=(str(level_type).strip() if level_type else None),
        user_note=note or "")
    if force:
        # A regeneration replaces the child bundle behind the compiled
        # cache's in-place update (which only ever ADDS maps/connections) —
        # drop it so reads serve the fresh child, not a blend.
        builder.services.compiled.invalidate(ctx.world_id)
    return _child_summary(bundle.get("map") or {},
                          bundle.get("connections") or [], existed=False)


register_tool(ToolSpec(
    id="expand_node",
    label="Expand a node into its child map",
    description=(
        "Create the child map an expandable node opens into (a planet's "
        "surface, a city's streets, a building's interior) — the ONLY way "
        "child maps are created; run_step map_generation cannot add them. "
        "Wraps the same cached expansion play-time uses: authored levels "
        "come fully named/described, procedural levels are born unnamed, "
        "terrain-flagged levels rasterize their own terrain now. The "
        "hierarchy's pregenerate plans (read_step 'hierarchy_design') are "
        "your work list, one node per call. An existing child returns as-is "
        "unless force=true regenerates it (checkpointed — revert covers a "
        "bad regeneration); 'note' steers the authoring."
    ),
    invoke=expand_node,
    mutates=True,
    params={
        "node_id": {"type": "string", "required": True,
                    "description": "The anchor node to expand (read_node "
                                   "reports 'expandable')."},
        "level_type": {"type": "string",
                       "description": "Pin the child's hierarchy level "
                                      "(e.g. a pregenerate plan's "
                                      "level_type); omit to let the author "
                                      "choose from the allowed levels."},
        "force": {"type": "boolean",
                  "description": "Regenerate an existing child map instead "
                                 "of returning it."},
        "note": {"type": "string",
                 "description": "Steering note threaded into the authoring "
                                "prompt — without it a force regeneration "
                                "is an unsteerable re-roll."},
    },
))
