"""Structural surgery tools (v2a): add/remove nodes, rewire edges and
cross-map connections.

Thin 1:1 wrappers over ``worldgen/surgery.py`` — the shared validated
mutation surface (S3) — translating its ``SurgeryError`` into the
agent-facing ``ToolError``. Hard referential integrity refuses before
execution; soft topology (splits, orphans, stale link tokens) lands in the
result's ``warnings`` for the agent to react to (S1). Content stays the
passes' job: prefer run_pass/edit_node for names and descriptions, surgery
for structure."""

from wbworldgen.worldgen import surgery
from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool


def _translated(fn, ctx, **kwargs):
    try:
        return fn(ctx.builder.services, ctx.world_id, **kwargs)
    except surgery.SurgeryError as e:
        raise ToolError(str(e))


async def add_node(ctx, map_id: str, near_node_id: str, name: str = None,
                   type: str = "waypoint", importance: int = 3,
                   label_description: str = "", description: str = "",
                   additional_details: str = "", edges_to: list = None) -> dict:
    return _translated(surgery.add_node, ctx, map_id=map_id,
                       near_node_id=near_node_id, name=name, type=type,
                       importance=importance,
                       label_description=label_description,
                       description=description,
                       additional_details=additional_details,
                       edges_to=edges_to)


async def remove_node(ctx, node_id: str) -> dict:
    return _translated(surgery.remove_node, ctx, node_id=node_id)


async def add_edge(ctx, map_id: str, from_node_id: str, to_node_id: str) -> dict:
    return _translated(surgery.add_edge, ctx, map_id=map_id,
                       from_node_id=from_node_id, to_node_id=to_node_id)


async def remove_edge(ctx, map_id: str, from_node_id: str, to_node_id: str) -> dict:
    return _translated(surgery.remove_edge, ctx, map_id=map_id,
                       from_node_id=from_node_id, to_node_id=to_node_id)


async def add_connection(ctx, from_map_id: str, from_node_id: str,
                         to_map_id: str, to_node_id: str,
                         kind: str = "passage", name: str = "",
                         description: str = "",
                         bidirectional: bool = True,
                         hidden: bool = False) -> dict:
    return _translated(surgery.add_connection, ctx, from_map_id=from_map_id,
                       from_node_id=from_node_id, to_map_id=to_map_id,
                       to_node_id=to_node_id, kind=kind, name=name,
                       description=description, bidirectional=bidirectional,
                       hidden=hidden)


async def remove_connection(ctx, connection_id: str) -> dict:
    return _translated(surgery.remove_connection, ctx,
                       connection_id=connection_id)


register_tool(ToolSpec(
    id="add_node",
    label="Add a node",
    description=(
        "Append a new location node to a map, placed one route leg beside "
        "near_node_id and edged to edges_to (default: the anchor); it "
        "inherits the anchor's region. Leaving it unnamed is fine — the "
        "label/describe passes fill it in. Names must be unique world-wide; "
        "a description may reference nodes as ${link_<node_id>}."
    ),
    invoke=add_node,
    mutates=True,
    params={
        "map_id": {"type": "string", "required": True,
                   "description": "The map to grow (read_world lists maps)."},
        "near_node_id": {"type": "string", "required": True,
                         "description": "Anchor node the new one is placed "
                                        "beside."},
        "name": {"type": "string",
                 "description": "Optional unique name; omit to let the "
                                "label pass name it."},
        "type": {"type": "string",
                 "description": "Node type (settlement, landmark, waypoint, "
                                "...). Default waypoint."},
        "importance": {"type": "integer", "min": 1, "max": 10,
                       "description": "1-10; majors (high importance) get "
                                      "upfront enrichment. Default 3."},
        "label_description": {"type": "string",
                              "description": "Optional one-line label."},
        "description": {"type": "string",
                        "description": "Optional surface description (what "
                                       "a visitor perceives; player-"
                                       "visible)."},
        "additional_details": {"type": "string",
                               "description": "Optional storyteller-only "
                                              "depth (never shown to the "
                                              "player; mark hidden facts "
                                              "with a leading 'Secret:')."},
        "edges_to": {"type": "list", "item_type": "string",
                     "description": "Node ids on the same map to edge to "
                                    "(default: the anchor)."},
    },
))

register_tool(ToolSpec(
    id="remove_node",
    label="Remove a node",
    description=(
        "Remove a node and its edges/region membership. Refuses while a "
        "child map anchors on it or a connection references it (resolve "
        "those first); warns when the map splits, and lists nodes whose "
        "descriptions still link to the removed one so you can rework them."
    ),
    invoke=remove_node,
    mutates=True,
    params={
        "node_id": {"type": "string", "required": True,
                    "description": "The node to remove."},
    },
))

register_tool(ToolSpec(
    id="add_edge",
    label="Add an edge",
    description=(
        "Join two nodes of one map with a travel edge (distance follows "
        "their positions). Use add_connection for cross-map links."
    ),
    invoke=add_edge,
    mutates=True,
    params={
        "map_id": {"type": "string", "required": True,
                   "description": "The map both nodes are on."},
        "from_node_id": {"type": "string", "required": True,
                         "description": "One endpoint."},
        "to_node_id": {"type": "string", "required": True,
                       "description": "The other endpoint."},
    },
))

register_tool(ToolSpec(
    id="remove_edge",
    label="Remove an edge",
    description=(
        "Remove the edge joining two nodes of one map. Warns when that "
        "orphans a node or splits the map — rejoin or accept deliberately."
    ),
    invoke=remove_edge,
    mutates=True,
    params={
        "map_id": {"type": "string", "required": True,
                   "description": "The map both nodes are on."},
        "from_node_id": {"type": "string", "required": True,
                         "description": "One endpoint."},
        "to_node_id": {"type": "string", "required": True,
                       "description": "The other endpoint."},
    },
))

register_tool(ToolSpec(
    id="add_connection",
    label="Add a connection",
    description=(
        "Join two nodes across maps with a travel connection (portal, "
        "passage, entrance...). Refuses when the endpoints are already "
        "directly connected. hidden=true makes it a SECRET way the player "
        "must discover in play — pair it with a 'Secret:' hint in a nearby "
        "node's additional_details, and never make a map's only way in "
        "hidden."
    ),
    invoke=add_connection,
    mutates=True,
    params={
        "from_map_id": {"type": "string", "required": True,
                        "description": "Map of the from endpoint."},
        "from_node_id": {"type": "string", "required": True,
                         "description": "Node of the from endpoint."},
        "to_map_id": {"type": "string", "required": True,
                      "description": "Map of the to endpoint."},
        "to_node_id": {"type": "string", "required": True,
                       "description": "Node of the to endpoint."},
        "kind": {"type": "string",
                 "description": "Connection kind (passage, portal, "
                                "entrance, ...). Default passage."},
        "name": {"type": "string",
                 "description": "Optional display name."},
        "description": {"type": "string",
                        "description": "Optional flavor description."},
        "bidirectional": {"type": "boolean",
                          "description": "Two-way travel (default true)."},
        "hidden": {"type": "boolean",
                   "description": "Secret way: unknown to the player until "
                                  "the story uncovers it (default false)."},
    },
))

register_tool(ToolSpec(
    id="remove_connection",
    label="Remove a connection",
    description=(
        "Remove a cross-map connection by id (read_world/read_map show "
        "them). Warns when a map becomes unreachable from the root. A "
        "node's connections must be removed before the node itself can be."
    ),
    invoke=remove_connection,
    mutates=True,
    params={
        "connection_id": {"type": "string", "required": True,
                          "description": "The connection to remove."},
    },
))
