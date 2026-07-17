"""Movement: within-map journeys plus passages between maps.

Within one map a Reader-declared destination starts a gradual journey along
the edge graph (a `travel` record in module_data tracks route + leg progress;
pace from `world.travel_turns_per_edge`, 0 = instant). Between maps the
Reader uses `player_passage` — a connection id. If the player isn't standing
at the passage's near end, a normal journey to it starts first with
`pending_connection_id` set; on arrival the transit fires. Instant
connections land immediately; journey-mode connections become a "transit"
phase counted down in turns (a shuttle crossing, a long descent). Layer
teleports are gone — crossing maps ALWAYS goes through a connection (or an
improvised/custom transition, which is a later phase).
"""

import heapq

from . import backfill as _backfill_rt
from . import expansion as _expansion
from .worldspace import (
    all_map_nodes,
    build_graph_adjacency,
    clean_option,
    connections_from,
    find_connection,
    get_map,
    get_site_position,
    get_travel,
    map_edges,
    map_of_node,
    node_index,
    node_needs_detail,
    player_map_id,
    reveal_bfs,
)


def weighted_adjacency(world_data: dict, map_id: str = None) -> dict:
    """{node_id: [(neighbor_id, distance), ...]} for one map (or all maps
    when map_id is None — edges never cross maps, so a route search naturally
    stays on the player's map either way). Missing distances fall back to
    node-coordinate length."""
    coords = {n.get("id"): (n.get("x", 0.0), n.get("y", 0.0)) for n in all_map_nodes(world_data)}
    if map_id:
        edges = map_edges(world_data, map_id)
    else:
        from .worldspace import all_map_edges
        edges = all_map_edges(world_data)
    adj: dict[str, list[tuple[str, float]]] = {}
    for e in edges:
        fr, to = e.get("from"), e.get("to")
        if not fr or not to:
            continue
        dist = e.get("distance")
        if not dist:
            (x1, y1), (x2, y2) = coords.get(fr, (0, 0)), coords.get(to, (0, 0))
            dist = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 or 1.0
        adj.setdefault(fr, []).append((to, float(dist)))
        adj.setdefault(to, []).append((fr, float(dist)))
    return adj


def find_route(adjacency: dict, start: str, goal: str) -> list | None:
    """Shortest node-id path from start to goal (Dijkstra), or None."""
    if start not in adjacency or goal not in adjacency:
        return None
    dist = {start: 0.0}
    prev: dict[str, str] = {}
    pq = [(0.0, start)]
    visited = set()
    while pq:
        d, nid = heapq.heappop(pq)
        if nid in visited:
            continue
        visited.add(nid)
        if nid == goal:
            break
        for nb, w in adjacency.get(nid, []):
            nd = d + w
            if nd < dist.get(nb, float("inf")):
                dist[nb] = nd
                prev[nb] = nid
                heapq.heappush(pq, (nd, nb))
    if goal not in visited:
        return None
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def edge_length(adjacency: dict, a: str, b: str) -> float:
    for nb, w in adjacency.get(a, []):
        if nb == b:
            return w
    return 1.0


def travel_speed(host, world_data: dict, map_id: str = None) -> float | None:
    """Map-units covered per turn, or None when travel is instant.

    Interior-style maps mark ``config.instant_travel`` — movement inside them
    is always instant regardless of the pace setting."""
    if map_id:
        m = get_map(world_data, map_id)
        if m is not None and (m.get("config") or {}).get("instant_travel"):
            return None
    turns_per_edge = 2
    try:
        if host._services is not None and host._services.get("settings") is not None:
            turns_per_edge = int(host._services["settings"].get("world.travel_turns_per_edge"))
    except Exception:
        turns_per_edge = 2
    if turns_per_edge <= 0:
        return None
    if map_id:
        edges = map_edges(world_data, map_id)
    else:
        from .worldspace import all_map_edges
        edges = all_map_edges(world_data)
    distances = [e.get("distance") for e in edges if e.get("distance")]
    if not distances:
        return None
    avg = sum(distances) / len(distances)
    return avg / turns_per_edge


def remaining_travel(travel: dict, adjacency: dict) -> float:
    """Total map-distance left from the player's position to the destination."""
    route = travel.get("route", [])
    leg_index = travel.get("leg_index", 0)
    remaining = travel.get("leg_distance", 0.0) - travel.get("leg_progress", 0.0)
    for i in range(leg_index + 1, len(route) - 1):
        remaining += edge_length(adjacency, route[i], route[i + 1])
    return max(remaining, 0.0)


def resolve_sub_location_move(mutation: dict, state: dict, world_data: dict) -> dict:
    """Interpret a Reader-declared move within the current location's interior.

    Returns {} (no change), {"site_position": None} (stepped back out) or
    {"site_position": {parent_node_id, sub_location_id}}. Sub-moves are
    instant and never interact with travel, fog or the node graph."""
    raw = clean_option(mutation.get("player_sub_location"))
    if not raw:
        return {}
    if raw == "leave_site":
        return {"site_position": None} if get_site_position(state) else {}
    current_node = state.get("player_location_node_id")
    site = (world_data.get("site_maps") or {}).get(current_node)
    if not site:
        return {}
    if not any(sub.get("id") == raw for sub in site.get("sub_locations", [])):
        return {}
    existing = get_site_position(state)
    if existing and existing.get("sub_location_id") == raw:
        return {}
    return {"site_position": {"parent_node_id": current_node, "sub_location_id": raw}}


def _connection_turns(connection: dict) -> int:
    """Turns a journey-mode connection takes to transit (0 = instant)."""
    travel = connection.get("travel") or {}
    if travel.get("mode") == "journey":
        try:
            return max(1, int(travel.get("turns", 1)))
        except (TypeError, ValueError):
            return 1
    return 0


async def on_mutate_state(host, mutation: dict, state: dict, sdk) -> dict:
    """Apply player movement (same-map moves, passages, journey advance)."""
    from .worldspace import ensure_v2
    world_data = state.get("world_data")
    if not world_data:
        return {}
    ensure_v2(state)
    mutation = mutation or {}
    travel = get_travel(state)
    current_node = state.get("player_location_node_id")
    current_map = player_map_id(state)
    speed = travel_speed(host, world_data, current_map)

    new_node_id = clean_option(mutation.get("player_location_node_id"))
    new_region = clean_option(mutation.get("player_location_region"))
    passage_id = clean_option(mutation.get("player_passage"))
    if passage_id in ("none", "None", ""):
        passage_id = None
    interrupted = bool(mutation.get("travel_interrupted"))

    # Intra-site movement (instant, inside the current node's interior).
    # Any real node move clears the position — the player walked out.
    site_position_update = resolve_sub_location_move(mutation, state, world_data)

    revealed = list(set(state.get("revealed_node_ids", [])))
    revealed_dirty = False
    newly_revealed: list[str] = []

    def reveal_around(nid, map_id=None):
        nonlocal revealed_dirty
        adjacency = build_graph_adjacency(world_data, map_id or map_of_node(world_data, nid))
        for x in reveal_bfs(nid, adjacency, radius=1):
            if x not in revealed:
                revealed.append(x)
                newly_revealed.append(x)
                revealed_dirty = True

    def queue_revealed_backfill():
        # Newly revealed places get detailed silently in the background so
        # they have names/descriptions by the time the story reaches them.
        if not newly_revealed:
            return
        by_id = node_index(world_data)
        needs = [nid for nid in newly_revealed
                 if nid in by_id and node_needs_detail(by_id[nid])]
        if needs:
            _backfill_rt.queue_backfill(host, state, needs, front=True)

    def region_of(nid):
        node = node_index(world_data).get(nid) or {}
        return node.get("region") or state.get("player_location_region")

    def land_at(map_id, node_id):
        """Arrive somewhere — possibly on another map — instantly."""
        reveal_around(node_id, map_id)
        queue_revealed_backfill()
        _expansion.maybe_expand_site(host, state, node_id)
        return {
            "player_location_node_id": node_id,
            "player_location_map_id": map_id,
            "player_location_region": new_region or region_of(node_id),
            "revealed_node_ids": revealed,
            "module_data": {"wb_worldgen": {"travel": None, "site_position": None}},
        }

    def begin_transit(connection, far):
        turns = _connection_turns(connection)
        if turns <= 0:
            return land_at(far.get("map_id"), far.get("node_id"))
        transit = {
            "phase": "transit",
            "connection_id": connection.get("id"),
            "transit_turns_left": turns - 1,  # this turn counts as the first
            "final_map_id": far.get("map_id"),
            "final_node_id": far.get("node_id"),
            "map_id": current_map,
        }
        if transit["transit_turns_left"] <= 0:
            return land_at(far.get("map_id"), far.get("node_id"))
        queue_revealed_backfill()
        return {"module_data": {"wb_worldgen": {"travel": transit, "site_position": None}}}

    # --- A passage through a connection to another map --------------------
    if passage_id:
        connection = find_connection(world_data, passage_id)
        if connection is not None:
            views = [v for v in connections_from(world_data, current_map, include_hidden=True)
                     if v["connection"].get("id") == passage_id]
            if views:
                near, far = views[0]["near"], views[0]["far"]
                if near.get("node_id") == current_node or speed is None:
                    return begin_transit(connection, far)
                # Walk to the passage first, then transit on arrival.
                adjacency = weighted_adjacency(world_data, current_map)
                route = find_route(adjacency, current_node, near.get("node_id")) if current_node else None
                if not route or len(route) < 2:
                    return begin_transit(connection, far)
                travel = {
                    "route": route,
                    "leg_index": 0,
                    "leg_progress": 0.0,
                    "leg_distance": edge_length(adjacency, route[0], route[1]),
                    "destination_node_id": near.get("node_id"),
                    "destination_region": new_region,
                    "map_id": current_map,
                    "phase": "approach",
                    "pending_connection_id": passage_id,
                }
                interrupted = False
                site_position_update = {"site_position": None}
                new_node_id = None  # the passage decides the movement this turn

    # --- A Reader-declared same-map destination ---------------------------
    wants_move = new_node_id and new_node_id != current_node
    if wants_move and not (travel or {}).get("pending_connection_id"):
        target_map = map_of_node(world_data, new_node_id)
        if target_map is not None and target_map != current_map:
            # Not offered by the schema, but never trap the player: land there.
            return land_at(target_map, new_node_id)
        if speed is None:
            return land_at(current_map, new_node_id)
        if not travel or travel.get("destination_node_id") != new_node_id:
            # (Re)route from the last node the player actually reached; any
            # partial progress on the current leg is abandoned.
            adjacency = weighted_adjacency(world_data, current_map)
            route = find_route(adjacency, current_node, new_node_id) if current_node else None
            if not route or len(route) < 2:
                # Unknown or unreachable destination — fall back to an
                # instant arrival rather than trap the player.
                return land_at(current_map, new_node_id)
            travel = {
                "route": route,
                "leg_index": 0,
                "leg_progress": 0.0,
                "leg_distance": edge_length(adjacency, route[0], route[1]),
                "destination_node_id": new_node_id,
                "destination_region": new_region,
                "map_id": current_map,
                "phase": "approach",
            }
            interrupted = False  # setting out counts as traveling this turn
            site_position_update = {"site_position": None}  # walked out of the interior
            if _expansion.site_mode(host) == "prefetch":
                # Start the destination's interior now — the journey's turns
                # hide the generation latency.
                _expansion.maybe_expand_site(host, state, new_node_id)

    if not travel:
        if site_position_update:
            return {"module_data": {"wb_worldgen": dict(site_position_update)}}
        if revealed_dirty:
            queue_revealed_backfill()
            return {"revealed_node_ids": revealed}
        return {}

    # --- Advance a transit (aboard a journey-mode connection) --------------
    if travel.get("phase") == "transit":
        if not interrupted:
            travel["transit_turns_left"] = travel.get("transit_turns_left", 1) - 1
            if travel["transit_turns_left"] <= 0:
                return land_at(travel.get("final_map_id"), travel.get("final_node_id"))
        return {"module_data": {"wb_worldgen": {"travel": travel, **site_position_update}}}

    if speed is None:
        # Travel was switched off mid-journey; the player simply stays at the
        # last reached node and the journey record is dropped.
        return {"module_data": {"wb_worldgen": {"travel": None, **site_position_update}}}

    # --- Advance the journey ----------------------------------------------
    location_update = {}
    arrived_pending_connection = None
    if not interrupted:
        adjacency = weighted_adjacency(world_data, travel.get("map_id") or current_map)
        route = travel["route"]
        budget = speed
        while budget > 0:
            need = travel["leg_distance"] - travel["leg_progress"]
            if budget < need:
                travel["leg_progress"] += budget
                break
            budget -= need
            travel["leg_index"] += 1
            reached_id = travel["route"][travel["leg_index"]]
            reveal_around(reached_id, travel.get("map_id"))
            reached_node = node_index(world_data).get(reached_id, {})
            location_update = {
                "player_location_node_id": reached_id,
                "player_location_map_id": travel.get("map_id") or current_map,
                "player_location_region": reached_node.get("region") or state.get("player_location_region"),
            }
            if travel["leg_index"] >= len(route) - 1:
                # Arrived at the end of the route.
                if travel.get("destination_region"):
                    location_update["player_location_region"] = travel["destination_region"]
                arrived_pending_connection = travel.get("pending_connection_id")
                travel = None
                break
            travel["leg_progress"] = 0.0
            travel["leg_distance"] = edge_length(adjacency, route[travel["leg_index"]], route[travel["leg_index"] + 1])

    if arrived_pending_connection:
        # The approach completed — roll straight into the transit.
        connection = find_connection(world_data, arrived_pending_connection)
        if connection is not None:
            arrived_node = location_update.get("player_location_node_id")
            views = [v for v in connections_from(world_data, current_map, arrived_node, include_hidden=True)
                     if v["connection"].get("id") == arrived_pending_connection]
            if views:
                # Update position bookkeeping before the hop so region history
                # is consistent, then transit.
                state = {**state, **location_update}
                result = begin_transit(connection, views[0]["far"])
                result.setdefault("revealed_node_ids", revealed)
                for key, value in location_update.items():
                    result.setdefault(key, value)
                queue_revealed_backfill()
                return result

    result = {"module_data": {"wb_worldgen": {"travel": travel, **site_position_update}}}
    if location_update:
        # The player physically moved to another node — they are no longer
        # inside the previous location's interior.
        result["module_data"]["wb_worldgen"]["site_position"] = None
        result.update(location_update)
        result["revealed_node_ids"] = revealed
    elif revealed_dirty:
        result["revealed_node_ids"] = revealed
    queue_revealed_backfill()
    return result
