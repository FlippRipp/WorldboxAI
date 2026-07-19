"""Validated structural surgery over a world's maps (v2a of the worldgen
architecture plan).

The one shared mutation surface for map structure — the agent's structure
tools wrap these 1:1 (P5), and any future human map editor gets the same
invariants by calling the same functions. Every operation validates against
the compiled world, writes through persistence's existing homes (child-map
bundle, map_generation step data, or the ``world_connections`` metadata
key), invalidates the compiled cache, and returns a report dict.

Validation is two-tier (S1): hard referential integrity — a mutation that
would leave a dangling reference — raises ``SurgeryError`` before anything
is written; soft topology and content quality (a map splitting, nodes
orphaned, inbound link tokens going stale) are allowed, surfaced in the
report's ``warnings``/``linked_from`` fields, and owned by the lints and
the done-gate. Nothing is ever silently repaired.

``SurgeryError`` messages are written for the correcting caller (the agent
reads them as observations): they name what was wrong and what to do
instead.
"""

import hashlib

from wbworldgen.mapmodel import grow_position, join_key
from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen.enrichment.context import LINK_TOKEN, postprocess_links


class SurgeryError(Exception):
    """A rejected structural mutation. Nothing was written."""


def _compiled(services, world_id: str) -> dict:
    return services.compiled.load(world_id)


def _require_map(compiled: dict, map_id: str) -> dict:
    record = _ms.get_map(compiled, map_id)
    if record is None:
        known = ", ".join(sorted(_ms.maps_by_id(compiled)))
        raise SurgeryError(
            f"Unknown map '{map_id}'. Maps in this world: {known}.")
    return record


def _require_node_on_map(compiled: dict, map_id: str, node_id: str) -> dict:
    node = next((n for n in _ms.map_nodes(compiled, map_id)
                 if n.get("id") == node_id), None)
    if node is None:
        owner = _ms.map_of_node(compiled, node_id)
        hint = (f"it lives on map '{owner}'" if owner
                else "read_map lists each map's node ids")
        raise SurgeryError(
            f"Node '{node_id}' is not on map '{map_id}' — {hint}.")
    return node


def _validate_name(compiled: dict, name: str, exclude_node_id: str = None) -> str:
    name = (name or "").strip()
    if not name:
        raise SurgeryError("A node's name cannot be empty.")
    key = join_key(name)
    clash = next(
        (n for n in _ms.all_nodes(compiled)
         if n.get("id") != exclude_node_id and n.get("name")
         and join_key(n["name"]) == key), None)
    if clash is not None:
        raise SurgeryError(
            f"Name {name!r} collides with node {clash.get('id')} "
            f"({clash.get('name')!r}) on map "
            f"'{_ms.map_of_node(compiled, clash.get('id'))}' — names are "
            "unique world-wide. Pick a different name or rename that node "
            "first.")
    return name


def _validate_description(compiled: dict, description: str, node: dict) -> str:
    index = _ms.node_index(compiled)
    broken = [m.group(1) for m in LINK_TOKEN.finditer(description)
              if m.group(1) not in index]
    if broken:
        raise SurgeryError(
            f"Description references nonexistent node id(s): {broken}. "
            "Link tokens must use real node ids (${link_<node_id>}); "
            "read_map lists them.")
    return postprocess_links(description, node, _ms.all_nodes(compiled))


def _split_warnings(record: dict, nodes: list, edges: list,
                    endpoints: set, map_id: str) -> list:
    """Soft-topology warnings for a map's would-be state: a split into
    components, and nodes left with no edges and no connections."""
    warnings = []
    components = _ms.connected_components(nodes, edges)
    if len(components) > 1:
        warnings.append(
            f"Map '{map_id}' now splits into {len(components)} disconnected "
            f"parts (sizes {[len(c) for c in components]}). Add edges or "
            "connections to rejoin them, or the lint report will flag it.")
    if len(nodes) > 1:
        degree = {n.get("id"): 0 for n in nodes}
        for e in edges:
            for nid in (e.get("from"), e.get("to")):
                if nid in degree:
                    degree[nid] += 1
        orphans = [nid for nid, d in degree.items()
                   if d == 0 and (map_id, nid) not in endpoints]
        if orphans:
            warnings.append(
                f"Node(s) {orphans} on map '{map_id}' are left with no edges "
                "and no connections — unreachable until re-linked.")
    return warnings


# --- nodes ------------------------------------------------------------------

def add_node(services, world_id: str, map_id: str, near_node_id: str,
             name: str = None, type: str = "waypoint", importance: int = 3,
             label_description: str = "", description: str = "",
             additional_details: str = "", edges_to: list = None) -> dict:
    """Append a new node one route leg beside its anchor, linked to
    ``edges_to`` (default: the anchor). Unnamed nodes are legal (S4) —
    enrichment fills them. The node inherits the anchor's region."""
    compiled = _compiled(services, world_id)
    record = _require_map(compiled, map_id)
    anchor = _require_node_on_map(compiled, map_id, near_node_id)

    targets = list(dict.fromkeys(edges_to or [near_node_id]))
    target_nodes = [_require_node_on_map(compiled, map_id, t) for t in targets]

    if name is not None and str(name).strip():
        name = _validate_name(compiled, str(name))
    else:
        name = ""
    if not isinstance(importance, int) or not (1 <= importance <= 10):
        raise SurgeryError("importance must be an integer from 1 to 10.")
    type = str(type or "waypoint").strip() or "waypoint"

    x, y = grow_position(record, target_nodes if anchor.get("id") in targets
                         else [anchor] + target_nodes)
    taken = {n.get("id") for n in _ms.all_nodes(compiled)}
    k = len(record.get("nodes") or []) + 1
    while f"{map_id}:g{k}" in taken:
        k += 1
    node = {
        "id": f"{map_id}:g{k}",
        "name": name,
        "type": type,
        "importance": importance,
        "description": "",
        "label_description": str(label_description or "").strip(),
        "x": x,
        "y": y,
    }
    if description:
        node["description"] = _validate_description(
            compiled, str(description), node)
    if additional_details:
        node["additional_details"] = _validate_description(
            compiled, str(additional_details), node)
    if anchor.get("region"):
        node["region"] = anchor["region"]

    edges = []
    for t in target_nodes:
        dist = ((x - t.get("x", 0.0)) ** 2 + (y - t.get("y", 0.0)) ** 2) ** 0.5
        edges.append({"from": t.get("id"), "to": node["id"],
                      "distance": round(max(dist, 1.0), 2)})

    if not services.enrichment_store.append_map_node(world_id, map_id, node, edges):
        raise SurgeryError(
            f"Map '{map_id}' exists in the compiled world but has no "
            "persisted home — it cannot be edited structurally.")
    services.compiled.invalidate(world_id)
    return {"node": node, "map_id": map_id,
            "edges": edges, "warnings": []}


def remove_node(services, world_id: str, node_id: str) -> dict:
    """Remove a node, cascading its edges and region membership. Refuses
    (S1) while a child map anchors on it or a connection references it;
    reports nodes whose descriptions link to it and any contained
    authored-location bindings lost; warns when the map splits."""
    compiled = _compiled(services, world_id)
    node = _ms.node_index(compiled).get(node_id)
    if node is None:
        raise SurgeryError(
            f"Unknown node '{node_id}'. Use read_map to list a map's node ids.")
    map_id = _ms.map_of_node(compiled, node_id)

    anchored = [mid for mid, m in _ms.maps_by_id(compiled).items()
                if m.get("anchor_node_id") == node_id]
    if anchored:
        raise SurgeryError(
            f"Node {node_id} anchors child map(s) {anchored} — removing it "
            "would strand them. Regenerate or rework those maps' content "
            "instead, or leave the node in place.")
    referencing = [c.get("id") for c in _ms.connections(compiled)
                   if node_id in ((c.get("from") or {}).get("node_id"),
                                  (c.get("to") or {}).get("node_id"))]
    if referencing:
        raise SurgeryError(
            f"Node {node_id} is an endpoint of connection(s) {referencing} — "
            "remove_connection them first, then remove the node.")

    record = _ms.get_map(compiled, map_id) or {}
    remaining = [n for n in record.get("nodes") or [] if n.get("id") != node_id]
    remaining_edges = [e for e in record.get("edges") or []
                       if node_id not in (e.get("from"), e.get("to"))]
    warnings = _split_warnings(record, remaining, remaining_edges,
                               _ms.connection_endpoints(compiled), map_id)

    linked_from = sorted({
        n.get("id") for n in _ms.all_nodes(compiled)
        if n.get("id") != node_id and any(
            m.group(1) == node_id
            for m in LINK_TOKEN.finditer(n.get("description") or ""))})
    if linked_from:
        warnings.append(
            f"Descriptions of {linked_from} reference the removed node — "
            "rework or edit them, or the lint report will flag broken link "
            "tokens.")

    removed = services.enrichment_store.remove_map_node(world_id, node_id)
    if removed is None:
        raise SurgeryError(
            f"Node {node_id} exists in the compiled world but has no "
            "persisted home — it cannot be removed structurally.")
    services.compiled.invalidate(world_id)
    return {
        "removed": node_id,
        "name": node.get("name", ""),
        "map_id": removed.get("map_id", map_id),
        "edges_removed": removed.get("edges_removed", 0),
        "linked_from": linked_from,
        "lost_contained_locations": list(node.get("contained_locations") or []),
        "warnings": warnings,
    }


# --- edges ------------------------------------------------------------------

def add_edge(services, world_id: str, map_id: str,
             from_node_id: str, to_node_id: str) -> dict:
    """Join two nodes of one map with a travel edge."""
    compiled = _compiled(services, world_id)
    _require_map(compiled, map_id)
    _require_node_on_map(compiled, map_id, from_node_id)
    _require_node_on_map(compiled, map_id, to_node_id)
    if from_node_id == to_node_id:
        raise SurgeryError("An edge needs two different nodes.")
    for e in _ms.map_edges(compiled, map_id):
        if {e.get("from"), e.get("to")} == {from_node_id, to_node_id}:
            raise SurgeryError(
                f"Nodes {from_node_id} and {to_node_id} are already joined "
                "by an edge.")

    edge = services.enrichment_store.add_map_edge(
        world_id, map_id, from_node_id, to_node_id)
    if edge is None:
        raise SurgeryError(
            f"Map '{map_id}' exists in the compiled world but has no "
            "persisted home — it cannot be edited structurally.")
    services.compiled.invalidate(world_id)
    return {"edge": edge, "map_id": map_id, "warnings": []}


def remove_edge(services, world_id: str, map_id: str,
                from_node_id: str, to_node_id: str) -> dict:
    """Remove the edge joining two nodes; warns when that orphans a node
    or splits the map."""
    compiled = _compiled(services, world_id)
    record = _require_map(compiled, map_id)
    if not any({e.get("from"), e.get("to")} == {from_node_id, to_node_id}
               for e in record.get("edges") or []):
        raise SurgeryError(
            f"No edge joins {from_node_id} and {to_node_id} on map "
            f"'{map_id}'.")

    remaining_edges = [e for e in record.get("edges") or []
                       if {e.get("from"), e.get("to")} != {from_node_id, to_node_id}]
    warnings = _split_warnings(record, record.get("nodes") or [],
                               remaining_edges,
                               _ms.connection_endpoints(compiled), map_id)

    removed = services.enrichment_store.remove_map_edge(
        world_id, map_id, from_node_id, to_node_id)
    if not removed:
        raise SurgeryError(
            f"Map '{map_id}' exists in the compiled world but has no "
            "persisted home — it cannot be edited structurally.")
    services.compiled.invalidate(world_id)
    return {"removed_edges": removed, "map_id": map_id, "warnings": warnings}


# --- connections ------------------------------------------------------------

def add_connection(services, world_id: str, from_map_id: str, from_node_id: str,
                   to_map_id: str, to_node_id: str, kind: str = "passage",
                   name: str = "", description: str = "",
                   bidirectional: bool = True) -> dict:
    """Join two nodes across maps with a travel connection. Stored in the
    child-map bundle it touches, else the world_connections metadata key."""
    compiled = _compiled(services, world_id)
    _require_map(compiled, from_map_id)
    _require_map(compiled, to_map_id)
    _require_node_on_map(compiled, from_map_id, from_node_id)
    _require_node_on_map(compiled, to_map_id, to_node_id)
    a = {"map_id": from_map_id, "node_id": from_node_id}
    b = {"map_id": to_map_id, "node_id": to_node_id}
    if a == b:
        raise SurgeryError("A connection needs two different endpoints.")
    existing = _ms.connection_between(compiled, a, b)
    if existing is not None:
        raise SurgeryError(
            f"Connection {existing.get('id')} already joins these endpoints.")

    known_ids = {c.get("id") for c in _ms.connections(compiled)}
    seed = f"{from_map_id}/{from_node_id}/{to_map_id}/{to_node_id}"
    salt = 0
    while True:
        conn_id = "c_" + hashlib.sha1(f"{seed}/{salt}".encode()).hexdigest()[:8]
        if conn_id not in known_ids:
            break
        salt += 1
    connection = {
        "id": conn_id,
        "from": a,
        "to": b,
        "kind": str(kind or "passage").strip() or "passage",
        "name": str(name or "").strip(),
        "description": str(description or "").strip(),
        "travel": {"mode": "instant"},
        "bidirectional": bool(bidirectional),
        "requirements": "",
        "hidden": False,
        "origin": "surgery",
    }

    store = services.enrichment_store
    owner = next((mid for mid in (to_map_id, from_map_id)
                  if store.load_child_map(world_id, mid) is not None), None)
    stored_in = store.add_world_connection(world_id, connection,
                                           owner_map_id=owner)
    if stored_in is None:
        raise SurgeryError(
            f"Child map '{owner}' has no persisted bundle — the connection "
            "cannot be stored.")
    services.compiled.invalidate(world_id)
    return {"connection": connection, "stored_in": stored_in, "warnings": []}


def remove_connection(services, world_id: str, connection_id: str) -> dict:
    """Remove a connection by id; warns when a map becomes unreachable."""
    compiled = _compiled(services, world_id)
    connection = _ms.find_connection(compiled, connection_id)
    if connection is None:
        sample = ", ".join(sorted(
            str(c.get("id")) for c in _ms.connections(compiled))[:12]) or "none"
        raise SurgeryError(
            f"Unknown connection '{connection_id}'. Connection ids in this "
            f"world: {sample}.")

    maps = _ms.maps_by_id(compiled)
    view = {"maps": maps,
            "connections": [c for c in _ms.connections(compiled)
                            if c.get("id") != connection_id]}
    newly_unreachable = sorted(
        _ms.unreachable_maps(view, maps) - _ms.unreachable_maps(compiled, maps))
    warnings = []
    if newly_unreachable:
        warnings.append(
            f"Removing it leaves map(s) {newly_unreachable} unreachable from "
            "the root — add another connection or the lint report will flag "
            "them.")

    home = services.enrichment_store.remove_world_connection(
        world_id, connection_id)
    if home is None:
        raise SurgeryError(
            f"Connection '{connection_id}' has no persisted record under "
            "that id (a migrated legacy connection with a synthesized id). "
            "It cannot be removed directly — regenerate the owning step to "
            "rebuild its map's connections instead.")
    services.compiled.invalidate(world_id)
    return {"removed": connection_id, "stored_in": home,
            "from": connection.get("from"), "to": connection.get("to"),
            "warnings": warnings}
