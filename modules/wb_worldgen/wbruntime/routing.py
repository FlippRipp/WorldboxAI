"""Cross-map route planning.

A journey is planned over the whole world at once: vertices are
``(map_id, node_id)`` pairs, edges are each map's own routes plus the
connections that join maps (doors, gates, shuttles, portals). Dijkstra over
that graph turns one declared destination — "the school", three maps away —
into a single itinerary: leave the interior, cross the city, board the train,
walk the last stretch.

Costs are measured in **edge-equivalents** (ee): one average-length route on
a map counts as 1.0, so legs on differently-scaled maps still contribute
comparable shares. Interior-style maps flagged ``config.instant_travel``
cost 0, instant connections cost 0, journey-mode connections cost their
``turns``. The unit is deliberately relative — absolute duration comes from
an LLM estimate (``eta_minutes``); ee only distributes that time across the
route so waypoints and fog-of-war reveal land in the right order.
"""

import heapq

from .worldspace import (
    connections_from,
    get_map,
    map_edges,
    map_nodes,
    map_of_node,
    maps_by_id,
)


def _map_leg_ee(world_data: dict) -> dict:
    """{map_id: ee-per-map-unit} — the inverse average edge length of each
    map (0.0 for instant-travel maps), so a map's average route costs 1 ee."""
    factors = {}
    for map_id, record in maps_by_id(world_data).items():
        if (record.get("config") or {}).get("instant_travel"):
            factors[map_id] = 0.0
            continue
        coords = {n.get("id"): (n.get("x", 0.0), n.get("y", 0.0))
                  for n in map_nodes(world_data, map_id)}
        lengths = []
        for e in map_edges(world_data, map_id):
            dist = e.get("distance")
            if not dist:
                (x1, y1) = coords.get(e.get("from"), (0.0, 0.0))
                (x2, y2) = coords.get(e.get("to"), (0.0, 0.0))
                dist = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 or 1.0
            lengths.append(float(dist))
        avg = sum(lengths) / len(lengths) if lengths else 1.0
        factors[map_id] = 1.0 / avg if avg else 1.0
    return factors


def connection_ee(connection: dict) -> float:
    """A connection's cost: 0 when instant, its journey ``turns`` otherwise."""
    travel = connection.get("travel") or {}
    if travel.get("mode") == "journey":
        try:
            return float(max(1, int(travel.get("turns", 1))))
        except (TypeError, ValueError):
            return 1.0
    return 0.0


def _global_adjacency(world_data: dict) -> dict:
    """{(map_id, node_id): [((map_id, node_id), ee, connection_id|None)]}.

    Hidden connections are excluded — an itinerary must not plan through a
    way the player hasn't found.
    """
    leg_ee = _map_leg_ee(world_data)
    adj: dict[tuple, list] = {}

    def _add(a, b, ee, connection_id=None):
        adj.setdefault(a, []).append((b, ee, connection_id))

    for map_id, record in maps_by_id(world_data).items():
        coords = {n.get("id"): (n.get("x", 0.0), n.get("y", 0.0))
                  for n in map_nodes(world_data, map_id)}
        factor = leg_ee.get(map_id, 1.0)
        for e in map_edges(world_data, map_id):
            fr, to = e.get("from"), e.get("to")
            if not fr or not to:
                continue
            dist = e.get("distance")
            if not dist:
                (x1, y1) = coords.get(fr, (0.0, 0.0))
                (x2, y2) = coords.get(to, (0.0, 0.0))
                dist = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 or 1.0
            ee = float(dist) * factor
            _add((map_id, fr), (map_id, to), ee)
            _add((map_id, to), (map_id, fr), ee)
        for view in connections_from(world_data, map_id):
            c = view["connection"]
            near, far = view["near"], view["far"]
            if not near.get("node_id") or not far.get("node_id"):
                continue
            _add((map_id, near["node_id"]),
                 (far.get("map_id"), far["node_id"]),
                 connection_ee(c), c.get("id"))
    return adj


def plan_itinerary(world_data: dict, start_map: str, start_node: str,
                   goal_node: str) -> dict | None:
    """Shortest itinerary from the player's position to ``goal_node``
    (anywhere in the world), or None when no path exists.

    Returns {"segments": [...], "ee_total": float, "destination_map_id",
    "destination_node_id"}. Segments are either
    {"kind": "route", "map_id", "nodes": [id, ...], "leg_ee": [float, ...]}
    or {"kind": "connection", "connection_id", "from": {map_id, node_id},
    "to": {map_id, node_id}, "ee": float}.
    """
    goal_map = map_of_node(world_data, goal_node)
    if goal_map is None or not start_node:
        return None
    start = (start_map, start_node)
    goal = (goal_map, goal_node)
    if start == goal:
        return None

    adj = _global_adjacency(world_data)
    if start not in adj:
        return None
    dist = {start: 0.0}
    prev: dict[tuple, tuple] = {}  # vertex -> (prior vertex, connection_id|None, ee)
    pq = [(0.0, start)]
    visited = set()
    while pq:
        d, v = heapq.heappop(pq)
        if v in visited:
            continue
        visited.add(v)
        if v == goal:
            break
        for nb, ee, connection_id in adj.get(v, []):
            nd = d + ee
            if nd < dist.get(nb, float("inf")):
                dist[nb] = nd
                prev[nb] = (v, connection_id, ee)
                heapq.heappush(pq, (nd, nb))
    if goal not in visited:
        return None

    # Unwind into (vertex, connection_id, ee) steps, then compress into
    # route segments (runs on one map) and connection segments.
    steps = []
    v = goal
    while v != start:
        p, connection_id, ee = prev[v]
        steps.append((v, connection_id, ee))
        v = p
    steps.reverse()

    segments = []
    ee_total = 0.0
    for (map_id, node_id), connection_id, ee in steps:
        ee_total += ee
        if connection_id is not None:
            prev_vertex = _segment_end(segments, start)
            segments.append({
                "kind": "connection",
                "connection_id": connection_id,
                "from": {"map_id": prev_vertex[0], "node_id": prev_vertex[1]},
                "to": {"map_id": map_id, "node_id": node_id},
                "ee": ee,
            })
        else:
            last = segments[-1] if segments else None
            if last is None or last["kind"] != "route" or last["map_id"] != map_id:
                prev_vertex = _segment_end(segments, start)
                segments.append({"kind": "route", "map_id": map_id,
                                 "nodes": [prev_vertex[1], node_id],
                                 "leg_ee": [ee]})
            else:
                last["nodes"].append(node_id)
                last["leg_ee"].append(ee)
    return {
        "segments": segments,
        "ee_total": ee_total,
        "destination_map_id": goal_map,
        "destination_node_id": goal_node,
    }


def _segment_end(segments: list, start: tuple) -> tuple:
    """The (map_id, node_id) where the itinerary-so-far currently ends."""
    if not segments:
        return start
    last = segments[-1]
    if last["kind"] == "route":
        return (last["map_id"], last["nodes"][-1])
    return (last["to"]["map_id"], last["to"]["node_id"])


def advance_position(itinerary: dict, ee_done: float) -> dict:
    """Where ``ee_done`` edge-equivalents of progress puts the traveler.

    Returns {"position": {"map_id", "node_id"}, "waypoints": [(map_id,
    node_id), ...] — every vertex reached so far in order (excluding the
    start), "arrived": bool, and, when mid-leg, "leg": {"map_id", "from",
    "to", "fraction"} or "transit": {"connection_id", "fraction"}}.
    """
    segments = itinerary.get("segments") or []
    remaining = max(0.0, float(ee_done))
    waypoints = []
    position = None
    result = {"arrived": False}

    for segment in segments:
        if segment["kind"] == "connection":
            ee = segment.get("ee", 0.0)
            if remaining >= ee:
                remaining -= ee
                position = (segment["to"]["map_id"], segment["to"]["node_id"])
                waypoints.append(position)
                continue
            # Mid-transit: the player is aboard the connection. Position
            # stays at the near side until the crossing completes.
            result["transit"] = {
                "connection_id": segment["connection_id"],
                "fraction": (remaining / ee) if ee else 1.0,
            }
            result["position"] = _position_dict(position, segment["from"])
            result["waypoints"] = waypoints
            return result
        nodes, leg_ee = segment["nodes"], segment["leg_ee"]
        for i, ee in enumerate(leg_ee):
            if remaining >= ee:
                remaining -= ee
                position = (segment["map_id"], nodes[i + 1])
                waypoints.append(position)
                continue
            result["leg"] = {
                "map_id": segment["map_id"],
                "from": nodes[i], "to": nodes[i + 1],
                "fraction": (remaining / ee) if ee else 1.0,
            }
            result["position"] = _position_dict(position, {
                "map_id": segment["map_id"], "node_id": nodes[i]})
            result["waypoints"] = waypoints
            return result

    result["arrived"] = True
    result["position"] = _position_dict(position, None)
    result["waypoints"] = waypoints
    return result


def _position_dict(position: tuple | None, fallback) -> dict | None:
    if position is not None:
        return {"map_id": position[0], "node_id": position[1]}
    if isinstance(fallback, dict):
        return {"map_id": fallback.get("map_id"), "node_id": fallback.get("node_id")}
    return None


def describe_itinerary(world_data: dict, itinerary: dict) -> list[str]:
    """Human-readable leg lines for prompt context ("Cross Verdant City to
    the Station", "Take the Coastal Line train to Harborview")."""
    from .worldspace import find_connection, node_index
    by_id = node_index(world_data)
    lines = []
    for segment in itinerary.get("segments") or []:
        if segment["kind"] == "connection":
            c = find_connection(world_data, segment["connection_id"]) or {}
            far_map = get_map(world_data, segment["to"]["map_id"]) or {}
            target = (by_id.get(segment["to"]["node_id"]) or {}).get("name") \
                or far_map.get("label", "the far side")
            name = c.get("name") or c.get("kind", "passage")
            line = f"Take {name} to {target}"
            if c.get("requirements"):
                line += f" (requires: {c['requirements']})"
            lines.append(line)
        else:
            m = get_map(world_data, segment["map_id"]) or {}
            end = (by_id.get(segment["nodes"][-1]) or {}).get("name") \
                or "an unexplored spot"
            named = [by_id.get(n, {}).get("name") for n in segment["nodes"][1:-1]]
            named = [n for n in named if n]
            line = f"Cross {m.get('label', 'the map')} to {end}"
            if named:
                line += f" (via {', '.join(named[:4])})"
            lines.append(line)
    return lines
