"""Accessors over the hierarchical map space (world_format 2).

A v2 world dict carries ``maps`` ({map_id: MapRecord}) and ``connections``
(flat list of ConnectionRecords). These helpers are the single place that
knows that layout; they also tolerate legacy dicts (flat ``map`` or
``map_layers``) so tests and not-yet-migrated inputs keep working.

MapRecord: {map_id, label, level_type, description, parent_map_id,
            anchor_node_id, generator_id, nodes, edges, regions, roads,
            config, legacy_layer_id?, rules?, landmarks?, factions?}
ConnectionRecord: {id, from: {map_id, node_id}, to: {map_id, node_id},
            kind, name, description, travel: {mode, turns?}, bidirectional,
            requirements, hidden, origin}
"""

ROOT_MAP_ID = "root"


def maps_by_id(wd: dict) -> dict:
    """{map_id: MapRecord} view of any world dict (v2 native, legacy adapted).

    Legacy adaptation shares the underlying node/edge lists (no copies), so
    in-place node mutation stays visible through either view.
    """
    if isinstance(wd.get("maps"), dict):
        return wd["maps"]
    result = {}
    map_layers = wd.get("map_layers", [])
    if map_layers:
        for layer in map_layers:
            lid = layer.get("layer_id") or ROOT_MAP_ID
            m = dict(layer.get("map", {}))
            m["map_id"] = lid
            m["label"] = layer.get("name", lid)
            result[lid] = m
        return result
    flat = wd.get("map")
    if isinstance(flat, dict) and flat.get("nodes") is not None:
        m = dict(flat)
        m["map_id"] = ROOT_MAP_ID
        m["label"] = "World"
        return {ROOT_MAP_ID: m}
    return result


def get_map(wd: dict, map_id: str) -> dict | None:
    return maps_by_id(wd).get(map_id)


def all_nodes(wd: dict) -> list[dict]:
    nodes: list[dict] = []
    for m in maps_by_id(wd).values():
        nodes.extend(m.get("nodes", []))
    return nodes


def all_edges(wd: dict) -> list[dict]:
    edges: list[dict] = []
    for m in maps_by_id(wd).values():
        edges.extend(m.get("edges", []))
    return edges


def map_nodes(wd: dict, map_id: str) -> list[dict]:
    m = get_map(wd, map_id)
    return m.get("nodes", []) if m else []


def map_edges(wd: dict, map_id: str) -> list[dict]:
    m = get_map(wd, map_id)
    return m.get("edges", []) if m else []


def node_index(wd: dict) -> dict:
    """{node_id: node} across all maps (node ids are globally unique)."""
    return {n.get("id"): n for n in all_nodes(wd)}


def map_of_node(wd: dict, node_id: str) -> str | None:
    """map_id containing the node, or None."""
    for mid, m in maps_by_id(wd).items():
        for n in m.get("nodes", []):
            if n.get("id") == node_id:
                return mid
    return None


def connections(wd: dict) -> list[dict]:
    return wd.get("connections") or []


def connections_from(wd: dict, map_id: str, node_id: str = None,
                     include_hidden: bool = False) -> list[dict]:
    """Outgoing connection views for a map (optionally one node).

    Bidirectional connections whose far side is on this map are returned
    reversed, so callers always see {connection, near: {map_id, node_id},
    far: {map_id, node_id}} with ``near`` on the requested map.
    """
    out = []
    for c in connections(wd):
        if c.get("hidden") and not include_hidden:
            continue
        frm, to = c.get("from") or {}, c.get("to") or {}
        if frm.get("map_id") == map_id and (node_id is None or frm.get("node_id") == node_id):
            out.append({"connection": c, "near": frm, "far": to})
        elif c.get("bidirectional", True) and to.get("map_id") == map_id \
                and (node_id is None or to.get("node_id") == node_id):
            out.append({"connection": c, "near": to, "far": frm})
    return out


def find_connection(wd: dict, connection_id: str) -> dict | None:
    for c in connections(wd):
        if c.get("id") == connection_id:
            return c
    return None


def connection_between(wd: dict, a: dict, b: dict,
                       include_hidden: bool = True) -> dict | None:
    """A connection directly joining endpoints a/b ({map_id, node_id}), if any."""
    def _same(x, y):
        return x.get("map_id") == y.get("map_id") and x.get("node_id") == y.get("node_id")
    for c in connections(wd):
        frm, to = c.get("from") or {}, c.get("to") or {}
        if c.get("hidden") and not include_hidden:
            continue
        if (_same(frm, a) and _same(to, b)) or (_same(frm, b) and _same(to, a)):
            return c
    return None


def breadcrumb(wd: dict, map_id: str) -> list[dict]:
    """MapRecords from the root down to (and including) map_id."""
    by_id = maps_by_id(wd)
    trail = []
    seen = set()
    cur = by_id.get(map_id)
    while cur is not None and cur.get("map_id") not in seen:
        seen.add(cur.get("map_id"))
        trail.append(cur)
        parent = cur.get("parent_map_id")
        cur = by_id.get(parent) if parent else None
    trail.reverse()
    return trail


def children_by_anchor(wd: dict) -> dict:
    """{(parent_map_id, anchor_node_id): [map_id, ...]} derived index."""
    idx: dict[tuple, list] = {}
    for mid, m in maps_by_id(wd).items():
        parent, anchor = m.get("parent_map_id"), m.get("anchor_node_id")
        if parent and anchor:
            idx.setdefault((parent, anchor), []).append(mid)
    return idx


def connection_endpoints(wd: dict) -> set:
    """{(map_id, node_id)} across every connection's both ends."""
    endpoints = set()
    for c in connections(wd):
        for end in (c.get("from") or {}, c.get("to") or {}):
            if end.get("map_id") and end.get("node_id"):
                endpoints.add((end["map_id"], end["node_id"]))
    return endpoints


def connected_components(nodes: list, edges: list) -> list:
    """Connected components (lists of node ids) over one map's undirected
    edge list, largest first. Edge endpoints not in ``nodes`` are ignored."""
    ids = [n.get("id") for n in nodes if n.get("id")]
    idset = set(ids)
    adjacency = {nid: [] for nid in ids}
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if a in idset and b in idset:
            adjacency[a].append(b)
            adjacency[b].append(a)
    seen = set()
    components = []
    for nid in ids:
        if nid in seen:
            continue
        stack, comp = [nid], []
        seen.add(nid)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adjacency[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        components.append(comp)
    components.sort(key=len, reverse=True)
    return components


def unreachable_maps(wd: dict, maps: dict = None) -> set:
    """Map ids not reachable from the root map over connections (either
    direction) and parent-child anchoring. The root is the ``root`` map when
    present, else the first parentless map, else the first map."""
    if maps is None:
        maps = maps_by_id(wd)
    if len(maps) < 2:
        return set()
    start = ROOT_MAP_ID if ROOT_MAP_ID in maps else next(
        (mid for mid, m in maps.items() if not m.get("parent_map_id")),
        next(iter(maps)))
    adjacency: dict = {mid: set() for mid in maps}
    for c in connections(wd):
        a = (c.get("from") or {}).get("map_id")
        b = (c.get("to") or {}).get("map_id")
        if a in adjacency and b in adjacency:
            adjacency[a].add(b)
            adjacency[b].add(a)
    for mid, m in maps.items():
        parent = m.get("parent_map_id")
        if parent in adjacency and m.get("anchor_node_id"):
            adjacency[parent].add(mid)
            adjacency[mid].add(parent)
    seen = {start}
    stack = [start]
    while stack:
        for nb in adjacency[stack.pop()]:
            if nb not in seen:
                seen.add(nb)
                stack.append(nb)
    return set(maps) - seen


def parallel_siblings(wd: dict, map_id: str) -> list[dict]:
    """Other maps sharing this map's parent with no anchor (parallel planes),
    plus the parent-anchored view for the root's parallels."""
    by_id = maps_by_id(wd)
    me = by_id.get(map_id)
    if me is None:
        return []
    parent = me.get("parent_map_id")
    return [m for mid, m in by_id.items()
            if mid != map_id and m.get("parent_map_id") == parent
            and not m.get("anchor_node_id") and not me.get("anchor_node_id")]
