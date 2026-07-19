"""The node edit tool: user-parity direct writes (D1).

``edit_node`` writes a node's name / description / label_description through
the enrichment store's existing write path — the same path enrichment and
the app's own editors use — with the invariants the engines enforce made
explicit: names must be unique world-wide (the same case/article-tolerant
identity every name join uses) and description link tokens must reference
real nodes (bare tokens are resolved to the ``|Name (direction)`` form the
frontend renders). Structural surgery (adding/removing nodes, rewiring)
is deliberately not offered in v1."""

from wbworldgen.mapmodel import join_key
from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen.agent.lints import _LINK_TOKEN
from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool
from wbworldgen.worldgen.enrichment.context import postprocess_links


async def edit_node(ctx, node_id: str, name: str = None, description: str = None,
                    label_description: str = None, type: str = None,
                    importance: int = None) -> dict:
    builder = ctx.builder
    if (name is None and description is None and label_description is None
            and type is None and importance is None):
        raise ToolError(
            "edit_node: nothing to change — provide name, description, "
            "label_description, type and/or importance.")

    compiled = builder.services.compiled.load(ctx.world_id)
    node = builder.get_map_node(ctx.world_id, node_id)
    if node is None:
        raise ToolError(
            f"Unknown node '{node_id}'. Use read_map to list a map's node ids.")
    all_nodes = _ms.all_nodes(compiled)

    updates = {}
    if name is not None:
        name = name.strip()
        if not name:
            raise ToolError("edit_node: a node's name cannot be empty.")
        key = join_key(name)
        clash = next(
            (n for n in all_nodes
             if n.get("id") != node_id and n.get("name")
             and join_key(n["name"]) == key), None)
        if clash is not None:
            raise ToolError(
                f"Name {name!r} collides with node {clash.get('id')} "
                f"({clash.get('name')!r}) on map "
                f"'{_ms.map_of_node(compiled, clash.get('id'))}' — names are "
                "unique world-wide. Pick a different name or rename that "
                "node first.")
        updates["name"] = name

    if description is not None:
        index = _ms.node_index(compiled)
        broken = [m.group(1) for m in _LINK_TOKEN.finditer(description)
                  if m.group(1) not in index]
        if broken:
            raise ToolError(
                f"Description references nonexistent node id(s): {broken}. "
                "Link tokens must use real node ids (${link_<node_id>}); "
                "read_map lists them.")
        # Resolve bare tokens to the '|Name (direction)' form the app renders.
        updates["description"] = postprocess_links(description, node, all_nodes)

    if label_description is not None:
        updates["label_description"] = label_description

    if type is not None:
        type = type.strip()
        if not type:
            raise ToolError("edit_node: a node's type cannot be empty.")
        updates["type"] = type

    if importance is not None:
        updates["importance"] = importance

    store = builder.services.enrichment_store
    for field_name, value in updates.items():
        store.save_node_enrichment(ctx.world_id, node_id, field_name, value)
        builder.services.compiled.update_node(compiled, node_id, field_name, value)
    store.flush_enrichment_cache(ctx.world_id)
    return {"node_id": node_id,
            "map_id": _ms.map_of_node(compiled, node_id),
            "updated": updates}


register_tool(ToolSpec(
    id="edit_node",
    label="Edit a node",
    description=(
        "Directly set a node's name, description, label_description, type "
        "and/or importance through the app's own enrichment write path. "
        "Enforces world-wide name uniqueness and validates description "
        "link tokens (${link_<node_id>}), resolving bare ones. For "
        "wholesale content regeneration prefer run_pass with rework and "
        "guidance."
    ),
    invoke=edit_node,
    mutates=True,
    params={
        "node_id": {"type": "string", "required": True,
                    "description": "The node to edit (read_map lists ids)."},
        "name": {"type": "string",
                 "description": "New unique name for the node."},
        "description": {"type": "string",
                        "description": "New flavor description; may "
                                       "reference other nodes as "
                                       "${link_<node_id>}."},
        "label_description": {"type": "string",
                              "description": "New one-line label."},
        "type": {"type": "string",
                 "description": "New node type (settlement, landmark, "
                                "waypoint, ...)."},
        "importance": {"type": "integer", "min": 1, "max": 10,
                       "description": "New importance (1-10; majors get "
                                      "upfront enrichment)."},
    },
))
