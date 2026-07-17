"""Gradual travel: route finding over the edge graph and the movement
mutation flow. Instead of teleporting on a Reader move, the player walks the
edge graph over multiple turns: a `travel` record in module_data tracks the
route (node id path) and the distance covered on the current leg. Pace comes
from the `world.travel_turns_per_edge` setting (0 = classic instant moves).
"""

import heapq

from . import backfill as _backfill_rt
from . import expansion as _expansion
from .worldspace import (
    all_map_edges,
    all_map_nodes,
    build_graph_adjacency,
    clean_option,
    get_site_position,
    get_travel,
    node_needs_detail,
    reveal_bfs,
)


def weighted_adjacency(world_data: dict) -> dict:
    """{node_id: [(neighbor_id, distance), ...]} across all layers.

    Edges never cross layers, so a route search naturally stays on the
    player's layer. Missing distances fall back to node-coordinate length.
    """
    coords = {n.get("id"): (n.get("x", 0.0), n.get("y", 0.0)) for n in all_map_nodes(world_data)}
    adj: dict[str, list[tuple[str, float]]] = {}
    for e in all_map_edges(world_data):
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


def travel_speed(host, world_data: dict) -> float | None:
    """Map-units covered per turn, or None when travel is instant."""
    turns_per_edge = 2
    try:
        if host._services is not None and host._services.get("settings") is not None:
            turns_per_edge = int(host._services["settings"].get("world.travel_turns_per_edge"))
    except Exception:
        turns_per_edge = 2
    if turns_per_edge <= 0:
        return None
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


async def on_mutate_state(host, mutation: dict, state: dict, sdk) -> dict:
    """Apply player movement.

    With travel enabled (world.travel_turns_per_edge > 0) a Reader-declared
    destination starts a journey along the edge graph instead of teleporting:
    the route is stored in module_data and progress advances every turn,
    revealing fog and updating the player node as each waypoint is reached.
    Instant mode (setting 0), layer changes, and off-graph destinations keep
    the classic teleport behavior.
    """
    world_data = state.get("world_data")
    if not world_data:
        return {}
    mutation = mutation or {}
    travel = get_travel(state)
    current_node = state.get("player_location_node_id")
    speed = travel_speed(host, world_data)

    new_node_id = clean_option(mutation.get("player_location_node_id"))
    new_region = clean_option(mutation.get("player_location_region"))
    new_layer_id = clean_option(mutation.get("player_location_layer_id"))
    interrupted = bool(mutation.get("travel_interrupted"))

    # Intra-site movement (instant, inside the current node's interior).
    # Any real node move clears the position — the player walked out.
    site_position_update = resolve_sub_location_move(mutation, state, world_data)

    revealed = list(set(state.get("revealed_node_ids", [])))
    revealed_dirty = False
    newly_revealed: list[str] = []

    def reveal_around(nid):
        nonlocal revealed_dirty
        adjacency = build_graph_adjacency(world_data)
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
        by_id = {n.get("id"): n for n in all_map_nodes(world_data)}
        needs = [nid for nid in newly_revealed
                 if nid in by_id and node_needs_detail(by_id[nid])]
        if needs:
            _backfill_rt.queue_backfill(host, state, needs, front=True)

    def teleport(node_id):
        reveal_around(node_id)
        queue_revealed_backfill()
        _expansion.maybe_expand_site(host, state, node_id)
        return {
            "player_location_node_id": node_id,
            "player_location_region": new_region or state.get("player_location_region"),
            "player_location_layer_id": new_layer_id or state.get("player_location_layer_id"),
            "revealed_node_ids": revealed,
            "module_data": {"wb_worldgen": {"travel": None, "site_position": None}},
        }

    # --- A Reader-declared destination -----------------------------------
    wants_move = new_node_id and new_node_id != current_node
    if wants_move:
        layer_changed = bool(new_layer_id) and new_layer_id != (state.get("player_location_layer_id") or new_layer_id)
        if speed is None or layer_changed:
            # Instant mode, or an inter-layer transition (portals, stairs,
            # cave mouths) — those are narrative jumps, not overland travel.
            return teleport(new_node_id)
        if not travel or travel.get("destination_node_id") != new_node_id:
            # (Re)route from the last node the player actually reached; any
            # partial progress on the current leg is abandoned.
            adjacency = weighted_adjacency(world_data)
            route = find_route(adjacency, current_node, new_node_id) if current_node else None
            if not route or len(route) < 2:
                # Unknown or unreachable destination — fall back to teleport
                # rather than trap the player.
                return teleport(new_node_id)
            travel = {
                "route": route,
                "leg_index": 0,
                "leg_progress": 0.0,
                "leg_distance": edge_length(adjacency, route[0], route[1]),
                "destination_node_id": new_node_id,
                "destination_region": new_region,
            }
            interrupted = False  # setting out counts as traveling this turn
            site_position_update = {"site_position": None}  # walked out of the interior
            if _expansion.site_mode(host) == "prefetch":
                # Start the destination's interior now — the journey's turns
                # hide the generation latency.
                _expansion.maybe_expand_site(host, state, new_node_id)

    if travel and speed is None:
        # Travel was switched off mid-journey; the player simply stays at the
        # last reached node and the journey record is dropped.
        return {"module_data": {"wb_worldgen": {"travel": None, **site_position_update}}}

    if not travel:
        if site_position_update:
            return {"module_data": {"wb_worldgen": dict(site_position_update)}}
        return {}

    # --- Advance the journey ----------------------------------------------
    location_update = {}
    if not interrupted:
        adjacency = weighted_adjacency(world_data)
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
            reveal_around(reached_id)
            reached_node = next((n for n in all_map_nodes(world_data) if n.get("id") == reached_id), {})
            location_update = {
                "player_location_node_id": reached_id,
                "player_location_region": reached_node.get("region") or state.get("player_location_region"),
                "player_location_layer_id": state.get("player_location_layer_id"),
            }
            if travel["leg_index"] >= len(route) - 1:
                # Arrived at the final destination.
                if travel.get("destination_region"):
                    location_update["player_location_region"] = travel["destination_region"]
                travel = None
                break
            travel["leg_progress"] = 0.0
            travel["leg_distance"] = edge_length(adjacency, route[travel["leg_index"]], route[travel["leg_index"] + 1])

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
