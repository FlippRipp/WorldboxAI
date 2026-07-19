"""Read tools: the agent's eyes (D1 — "read everything").

Compiled world, step data, ``design.py`` queries, the capability catalog,
the world rules and the lint report, each as a validated tool over the
surfaces the app already exposes. Results are complete within their declared
structure — summaries are structural (counts + ids with a pointer to the
detail tool), never character-truncated (P9 and the project's no-token-caps
rule)."""

from wbworldgen.worldgen import design as _design
from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen.agent.lints import lint_world
from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool


def _compiled(ctx):
    return ctx.builder.services.compiled.load(ctx.world_id)


def _map_or_error(compiled: dict, map_id: str) -> dict:
    rec = _ms.maps_by_id(compiled).get(map_id)
    if rec is None:
        raise ToolError(
            f"Unknown map '{map_id}'. This world's maps: "
            f"{', '.join(_ms.maps_by_id(compiled)) or 'none yet'}")
    return rec


def _map_stats(rec: dict) -> dict:
    nodes = rec.get("nodes", [])
    return {
        "map_id": rec.get("map_id"), "label": rec.get("label", ""),
        "level_type": rec.get("level_type", ""),
        "nodes": len(nodes),
        "named": sum(1 for n in nodes if n.get("name")),
        "described": sum(1 for n in nodes if n.get("description")),
        "detailed": sum(1 for n in nodes if n.get("additional_details")),
    }


async def read_world(ctx) -> dict:
    """World overview: step status, the AI's design decisions, map
    inventory, world rules, available artifacts."""
    builder = ctx.builder
    world_state = builder.load_world(ctx.world_id)
    compiled = _compiled(ctx)
    from wbworldgen.worldgen.catalog import produced_artifacts

    effective = set(builder.ordered_ids_for(world_state))
    steps_report = []
    for frontend in builder.get_pipeline():
        sid = frontend["id"]
        entry = world_state.get("steps", {}).get(sid) or {}
        steps_report.append({
            "id": sid, "label": frontend["label"],
            "present": bool(entry.get("data")),
            "approved": bool(entry.get("approved")),
            "skipped_by_design": sid not in effective,
        })

    rules_entry = world_state.get("steps", {}).get("world_rules") or {}
    return {
        "world_id": ctx.world_id,
        "seed_prompt": world_state.get("seed_prompt", ""),
        "complete": bool(world_state.get("complete")),
        "steps": steps_report,
        "design": {
            "world_kind": _design.world_kind(world_state),
            "map_style": _design.map_style(world_state),
            "skipped_steps": sorted(_design.dynamic_skips(world_state)),
            "levels": [
                {"level_type": lv.get("level_type"), "label": lv.get("label"),
                 "generator_id": lv.get("generator_id")}
                for lv in _design.designed_levels(world_state)],
            "parallel_maps": [p.get("label") for p in _design.parallel_maps(world_state)],
            "pregenerate": [p.get("location_name")
                            for p in _design.pregenerate_plans(world_state)],
        },
        "maps": [_map_stats(rec) for rec in _ms.maps_by_id(compiled).values()],
        "world_rules": rules_entry.get("data") or {},
        "artifacts": sorted(produced_artifacts(
            world_state, compiled, steps=builder.steps_by_id())),
    }


async def read_step(ctx, step_id: str) -> dict:
    """One step's saved data. Map data lives behind read_map."""
    builder = ctx.builder
    steps = builder.steps_by_id()
    if step_id not in steps:
        raise ToolError(
            f"Unknown step '{step_id}'. Registered steps: {', '.join(steps)}")
    world_state = builder.load_world(ctx.world_id)
    entry = world_state.get("steps", {}).get(step_id) or {}
    if step_id == "map_generation":
        compiled = _compiled(ctx)
        return {"step_id": step_id, "present": bool(entry.get("data")),
                "approved": bool(entry.get("approved")),
                "maps": [_map_stats(rec) for rec in _ms.maps_by_id(compiled).values()],
                "note": "Map content is served per map — use read_map for "
                        "nodes and edges, read_node for one node."}
    return {"step_id": step_id, "present": bool(entry.get("data")),
            "approved": bool(entry.get("approved")),
            "data": entry.get("data") or {}}


async def read_map(ctx, map_id: str) -> dict:
    """One map: compact node list, edges, regions, cross-map connections."""
    compiled = _compiled(ctx)
    rec = _map_or_error(compiled, map_id)
    nodes = []
    for n in rec.get("nodes", []):
        entry = {"id": n.get("id"), "name": n.get("name", ""),
                 "type": n.get("type", ""), "importance": n.get("importance", 0),
                 "described": bool(n.get("description")),
                 "detailed": bool(n.get("additional_details"))}
        if n.get("region"):
            entry["region"] = n["region"]
        nodes.append(entry)
    connections = [
        {"from_node": view["near"].get("node_id"),
         "to_map": view["far"].get("map_id"), "to_node": view["far"].get("node_id"),
         "kind": view["connection"].get("kind", ""),
         "name": view["connection"].get("name", "")}
        for view in _ms.connections_from(compiled, map_id, include_hidden=True)
    ]
    return {
        **_map_stats(rec),
        "description": rec.get("description", ""),
        "parent_map_id": rec.get("parent_map_id"),
        "anchor_node_id": rec.get("anchor_node_id"),
        "generator_id": rec.get("generator_id", ""),
        "node_list": nodes,
        "edges": [[e.get("from"), e.get("to")] for e in rec.get("edges", []) or []],
        "regions": [r.get("name", "") for r in rec.get("regions", []) or []],
        "connections": connections,
    }


async def read_node(ctx, node_id: str) -> dict:
    """Everything about one node: all fields (custom slots included),
    neighbors, cross-map connections, expandability."""
    builder = ctx.builder
    compiled = _compiled(ctx)
    node = builder.get_map_node(ctx.world_id, node_id)
    if node is None:
        raise ToolError(
            f"Unknown node '{node_id}'. Use read_map to list a map's node ids.")
    map_id = _ms.map_of_node(compiled, node_id)
    rec = _ms.maps_by_id(compiled).get(map_id) or {}
    by_id = {n.get("id"): n for n in rec.get("nodes", [])}
    neighbors = []
    for e in rec.get("edges", []) or []:
        other = None
        if e.get("from") == node_id:
            other = by_id.get(e.get("to"))
        elif e.get("to") == node_id:
            other = by_id.get(e.get("from"))
        if other is not None:
            neighbors.append({"id": other.get("id"), "name": other.get("name", "")})
    connections = [
        {"to_map": view["far"].get("map_id"), "to_node": view["far"].get("node_id"),
         "kind": view["connection"].get("kind", "")}
        for view in _ms.connections_from(compiled, map_id, node_id, include_hidden=True)
    ]
    return {
        "node": dict(node),
        "map_id": map_id,
        "neighbors": neighbors,
        "connections": connections,
        "expandable": builder.is_node_expandable(compiled, map_id, node),
    }


async def read_lint(ctx, map_id: str = None) -> dict:
    """The deterministic lint report (D3): duplicate names, orphans,
    connectivity, link tokens, major-location coverage."""
    compiled = _compiled(ctx)
    if map_id is not None:
        _map_or_error(compiled, map_id)
    floor = type(ctx.builder).MAJOR_IMPORTANCE_FLOOR
    return lint_world(compiled, map_id=map_id, major_floor=floor)


async def read_catalog(ctx) -> dict:
    """The full capability catalog (steps, generators, passes, tools)."""
    from wbworldgen.worldgen.catalog import render_catalog_markdown
    return {"markdown": render_catalog_markdown()}


register_tool(ToolSpec(
    id="read_world",
    label="World overview",
    description=(
        "The state of the whole build: which steps have data, the world's "
        "own design decisions (kind, map style, levels, skips), a per-map "
        "content inventory, the world rules, and the artifact names already "
        "available for dependency checks."
    ),
    invoke=read_world,
))

register_tool(ToolSpec(
    id="read_step",
    label="Read step data",
    description=(
        "The saved data of one pipeline step (world_rules, lore, "
        "hierarchy_design, ...). For map_generation this returns the map "
        "inventory — use read_map / read_node for map content."
    ),
    invoke=read_step,
    params={"step_id": {"type": "string", "required": True,
                        "description": "A registered step id (see the catalog)."}},
))

register_tool(ToolSpec(
    id="read_map",
    label="Read one map",
    description=(
        "One map in full structure: every node (id, name, type, importance, "
        "described flag), edges, regions, and its connections to other maps."
    ),
    invoke=read_map,
    params={"map_id": {"type": "string", "required": True,
                       "description": "A map id from read_world's inventory."}},
))

register_tool(ToolSpec(
    id="read_node",
    label="Read one node",
    description=(
        "Every field of one map node (description and custom slots "
        "included), its neighbors, its cross-map connections, and whether "
        "it can host a child map."
    ),
    invoke=read_node,
    params={"node_id": {"type": "string", "required": True,
                        "description": "A node id (read_map lists them)."}},
))

register_tool(ToolSpec(
    id="read_lint",
    label="Lint the world",
    description=(
        "Deterministic defect report over the current world: duplicate "
        "names, orphan nodes, disconnected or unreachable maps, broken or "
        "unresolved link tokens, dangling edges/connections, and unnamed/"
        "undescribed major locations. Cheap ground truth — run it after "
        "content changes and before declaring work done."
    ),
    invoke=read_lint,
    params={"map_id": {"type": "string",
                       "description": "Limit per-map findings to one map."}},
))

register_tool(ToolSpec(
    id="read_catalog",
    label="Read the capability catalog",
    description=(
        "The combined capability catalog (steps, map generators, enrichment "
        "passes, agent tools) as markdown — the same document rendered into "
        "the build prompt, re-readable on demand."
    ),
    invoke=read_catalog,
))
