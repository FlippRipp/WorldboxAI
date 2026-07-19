"""Deterministic lints over a compiled world (D3 of the worldgen plan).

The cheap ground truth the agentic builder verifies against: a pure function
— no LLM, no I/O, no services — that walks the compiled world and reports
mechanical defects (duplicate names, orphan nodes, broken link tokens,
connectivity holes) plus coverage gaps against the major-location floor.
The evaluator (C2) feeds this report to its critique call; the agent reads
it directly through the ``read_lint`` tool.

Every problem entry carries ``kind``, a human/agent-readable ``message``,
and enough ids to act on (``map_id``, ``node_ids``…). Aggregate kinds
(unnamed/undescribed majors) report one entry per map, not one per node, so
a fresh pre-enrichment world lints readable instead of drowning (P9:
structural budgets)."""

import re

from wbworldgen.mapmodel import join_key
from wbworldgen.worldgen import mapspace as _ms

#: ``${link_<id>}`` (unresolved) or ``${link_<id>|<label>}`` (resolved).
_LINK_TOKEN = re.compile(r"\$\{link_([^}|]+)(\|[^}]*)?\}")


def _connection_endpoints(compiled: dict) -> set:
    """{(map_id, node_id)} across every connection's both ends."""
    endpoints = set()
    for c in _ms.connections(compiled):
        for end in (c.get("from") or {}, c.get("to") or {}):
            if end.get("map_id") and end.get("node_id"):
                endpoints.add((end["map_id"], end["node_id"]))
    return endpoints


def _unreachable_maps(compiled: dict, maps: dict) -> set:
    """Map ids not reachable from the root map over connections (either
    direction) and parent-child anchoring. The root is the ``root`` map when
    present, else the first parentless map, else the first map."""
    if len(maps) < 2:
        return set()
    start = _ms.ROOT_MAP_ID if _ms.ROOT_MAP_ID in maps else next(
        (mid for mid, m in maps.items() if not m.get("parent_map_id")),
        next(iter(maps)))
    adjacency: dict = {mid: set() for mid in maps}
    for c in _ms.connections(compiled):
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


def _components(nodes: list, edges: list) -> list:
    """Connected components (lists of node ids) over one map's undirected
    edge list, largest first. Edge endpoints not on the map are ignored
    here — they are reported separately as dangling edges."""
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


def lint_world(compiled: dict, map_id: str = None, major_floor: int = None) -> dict:
    """Lint a compiled world; ``map_id`` narrows per-map findings to one map
    (duplicate detection stays world-global — names are world-unique — but
    only groups touching the scoped map are reported). ``major_floor``
    enables the coverage findings: majors (importance >= floor) still
    missing a name or description. Returns ``{"clean", "problem_count",
    "problems", "stats"}``."""
    maps = _ms.maps_by_id(compiled)
    scoped = {map_id: maps[map_id]} if map_id is not None else maps
    index = _ms.node_index(compiled)
    endpoints = _connection_endpoints(compiled)
    unreachable = _unreachable_maps(compiled, maps)
    problems = []

    # Duplicate names — global grouping by the same normalization every
    # cross-step name join uses (case/article tolerant).
    by_key: dict = {}
    for mid, rec in maps.items():
        for n in rec.get("nodes", []):
            if n.get("name"):
                by_key.setdefault(join_key(n["name"]), []).append(
                    {"node_id": n.get("id"), "map_id": mid, "name": n["name"]})
    for key, group in by_key.items():
        if len(group) < 2 or not any(g["map_id"] in scoped for g in group):
            continue
        names = ", ".join(f'{g["name"]!r} ({g["node_id"]} on {g["map_id"]})' for g in group)
        problems.append({
            "kind": "duplicate_name", "nodes": group,
            "message": f"{len(group)} locations share the same name: {names}. "
                       "Rename all but one (edit_node, or run_pass label with "
                       "rework and node_ids)."})

    for mid, rec in scoped.items():
        nodes = rec.get("nodes", [])
        edges = rec.get("edges", []) or []
        node_ids = {n.get("id") for n in nodes}

        # Dangling edges (endpoints that are not nodes of this map).
        for e in edges:
            missing = [nid for nid in (e.get("from"), e.get("to")) if nid not in node_ids]
            if missing:
                problems.append({
                    "kind": "dangling_edge", "map_id": mid,
                    "edge": {"from": e.get("from"), "to": e.get("to")},
                    "message": f"Edge {e.get('from')} -> {e.get('to')} on map "
                               f"'{mid}' references missing node(s): {missing}."})

        # Orphans: no edge touches the node and no connection leaves it.
        degree: dict = {nid: 0 for nid in node_ids}
        for e in edges:
            for nid in (e.get("from"), e.get("to")):
                if nid in degree:
                    degree[nid] += 1
        if len(nodes) > 1:
            for n in nodes:
                nid = n.get("id")
                if degree.get(nid, 0) == 0 and (mid, nid) not in endpoints:
                    problems.append({
                        "kind": "orphan_node", "map_id": mid, "node_id": nid,
                        "name": n.get("name", ""),
                        "message": f"Node {nid} ({n.get('name') or 'unnamed'}) on map "
                                   f"'{mid}' has no edges and no connections — it is "
                                   "unreachable."})

        # Connectivity: one map should be one component.
        components = _components(nodes, edges)
        if len(components) > 1:
            problems.append({
                "kind": "disconnected_map", "map_id": mid,
                "component_sizes": [len(c) for c in components],
                "samples": [c[0] for c in components],
                "message": f"Map '{mid}' splits into {len(components)} disconnected "
                           f"parts (sizes {[len(c) for c in components]}); sample node "
                           f"ids per part: {[c[0] for c in components]}."})

        # Unreachable maps: every map must be reachable from the root over
        # connections and parent anchors (the root itself never flags).
        if mid in unreachable:
            problems.append({
                "kind": "unreachable_map", "map_id": mid,
                "message": f"Map '{mid}' cannot be reached from the root map — "
                           "no chain of connections or parent anchors leads "
                           "to it."})

        # Link tokens in descriptions.
        for n in nodes:
            desc = n.get("description") or ""
            broken, unresolved = [], []
            for m in _LINK_TOKEN.finditer(desc):
                target, label_part = m.group(1), m.group(2)
                if target not in index:
                    broken.append(target)
                elif not label_part:
                    unresolved.append(target)
            if broken:
                problems.append({
                    "kind": "broken_link_token", "map_id": mid, "node_id": n.get("id"),
                    "targets": broken,
                    "message": f"Description of {n.get('id')} ({n.get('name') or 'unnamed'}) "
                               f"references nonexistent node id(s): {broken}. Rework the "
                               "description or fix the reference."})
            if unresolved:
                problems.append({
                    "kind": "unresolved_link_token", "map_id": mid, "node_id": n.get("id"),
                    "targets": unresolved,
                    "message": f"Description of {n.get('id')} carries bare link token(s) "
                               f"{unresolved} without a resolved '|Name (direction)' part; "
                               "rewriting the description (describe rework or edit_node) "
                               "resolves them."})

        # Coverage against the major-location floor.
        if major_floor is not None:
            majors = [n for n in nodes if n.get("importance", 0) >= major_floor]
            unnamed = [n.get("id") for n in majors if not n.get("name")]
            undescribed = [n.get("id") for n in majors if n.get("name") and not n.get("description")]
            if unnamed:
                problems.append({
                    "kind": "unnamed_major_nodes", "map_id": mid, "node_ids": unnamed,
                    "message": f"{len(unnamed)} major location(s) (importance >= "
                               f"{major_floor}) on map '{mid}' have no name yet: "
                               f"{unnamed}. Run the label pass over them."})
            if undescribed:
                problems.append({
                    "kind": "undescribed_major_nodes", "map_id": mid, "node_ids": undescribed,
                    "message": f"{len(undescribed)} named major location(s) on map "
                               f"'{mid}' have no description yet: {undescribed}. Run "
                               "the describe pass over them."})

    # Dangling connections (either end names a missing map or node) — checked
    # over every connection touching a scoped map.
    for c in _ms.connections(compiled):
        ends = [c.get("from") or {}, c.get("to") or {}]
        if map_id is not None and not any(e.get("map_id") == map_id for e in ends):
            continue
        bad = [e for e in ends
               if e.get("map_id") not in maps or e.get("node_id") not in index]
        if bad:
            problems.append({
                "kind": "dangling_connection", "connection_id": c.get("id"),
                "endpoints": bad,
                "message": f"Connection {c.get('id')} references missing map/node "
                           f"endpoint(s): {bad}."})

    stats = []
    for mid, rec in scoped.items():
        nodes = rec.get("nodes", [])
        entry = {
            "map_id": mid, "label": rec.get("label", mid),
            "nodes": len(nodes),
            "named": sum(1 for n in nodes if n.get("name")),
            "described": sum(1 for n in nodes if n.get("description")),
        }
        if major_floor is not None:
            majors = [n for n in nodes if n.get("importance", 0) >= major_floor]
            entry["majors"] = len(majors)
            entry["majors_named"] = sum(1 for n in majors if n.get("name"))
            entry["majors_described"] = sum(1 for n in majors if n.get("description"))
        stats.append(entry)

    return {"clean": not problems, "problem_count": len(problems),
            "problems": problems, "stats": stats}
