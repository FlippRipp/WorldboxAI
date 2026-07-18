"""Movement: time-based journeys across the whole map hierarchy.

A declared destination — from the pre-storyteller travel-intent call or the
Reader — is planned as a cross-map itinerary (``routing.plan_itinerary``):
route legs on each map plus the connections between maps, measured in
edge-equivalents. The journey's duration is in-world **minutes**: an ETA
from the intent call's estimate (or ``world.travel_minutes_per_edge`` as a
fallback), advanced each turn by the Reader-extracted
``travel_minutes_covered`` — so the story's own pacing moves the player.
The Reader can also declare ``travel_completed`` when the narration covered
the whole trip (an uneventful train ride told in one scene), finishing the
journey early. ``travel_interrupted`` pauses it. Fog-of-war reveal fires
for every waypoint actually passed. ``world.travel_minutes_per_edge`` 0 and
``config.instant_travel`` maps keep their instant semantics.
"""

import hashlib
import heapq

from . import backfill as _backfill_rt
from . import routing as _routing
from . import sync as _sync
from . import expansion as _expansion
from .worldspace import (
    all_map_nodes,
    build_graph_adjacency,
    clean_option,
    connection_between,
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


def travel_minutes_per_edge(host) -> int:
    """In-world minutes an average map route takes; 0 = travel is instant.

    Used as the ETA fallback when no LLM estimate is available, and as the
    global instant-travel switch."""
    try:
        if host._services is not None and host._services.get("settings") is not None:
            return int(host._services["settings"].get("world.travel_minutes_per_edge"))
    except Exception:
        pass
    return 60


def journey_progress(travel: dict) -> dict:
    """Minutes elapsed/remaining and the traveler's computed position for an
    active journey record (context builders share this)."""
    eta = max(1, int(travel.get("eta_minutes", 1)))
    minutes = max(0, int(travel.get("minutes_traveled", 0)))
    itinerary = travel.get("itinerary") or {}
    ee_total = itinerary.get("ee_total", 0.0)
    fraction = min(1.0, minutes / eta)
    position = _routing.advance_position(itinerary, ee_total * fraction)
    return {
        "eta_minutes": eta,
        "minutes_traveled": minutes,
        "minutes_remaining": max(0, eta - minutes),
        "fraction": fraction,
        "position": position,
    }


def plan_journey(host, state: dict, world_data: dict, destination_node_id: str,
                 eta_minutes: int = None, transport: str = "",
                 destination_region: str = None):
    """Plan travel from the player's position to ``destination_node_id``.

    Returns ("instant", map_id, node_id) when the move needs no journey
    (instant pace, zero-cost route, or unreachable — never trap the player),
    ("journey", record) for a started journey, or None when the destination
    doesn't exist."""
    target_map = map_of_node(world_data, destination_node_id)
    if target_map is None:
        return None
    current_map = player_map_id(state)
    current_node = state.get("player_location_node_id")
    if destination_node_id == current_node:
        return None
    minutes_per_edge = travel_minutes_per_edge(host)
    itinerary = _routing.plan_itinerary(
        world_data, current_map, current_node, destination_node_id)
    if itinerary is None:
        return ("instant", target_map, destination_node_id)
    if minutes_per_edge <= 0 or itinerary["ee_total"] <= 0:
        return ("instant", itinerary["destination_map_id"], destination_node_id)
    eta = None
    try:
        eta = int(eta_minutes) if eta_minutes else None
    except (TypeError, ValueError):
        eta = None
    if not eta or eta <= 0:
        eta = max(1, round(itinerary["ee_total"] * minutes_per_edge))
    record = {
        "phase": "journey",
        "itinerary": itinerary,
        "eta_minutes": eta,
        "minutes_traveled": 0,
        "waypoint_cursor": 0,
        "destination_node_id": destination_node_id,
        "destination_map_id": itinerary["destination_map_id"],
        "destination_region": destination_region,
        "transport": transport or "",
        "map_id": current_map,
    }
    return ("journey", record)


def _connection_itinerary(connection: dict, near: dict, far: dict) -> dict:
    """A one-segment itinerary that just crosses ``connection``."""
    ee = _routing.connection_ee(connection)
    return {
        "segments": [{
            "kind": "connection",
            "connection_id": connection.get("id"),
            "from": {"map_id": near.get("map_id"), "node_id": near.get("node_id")},
            "to": {"map_id": far.get("map_id"), "node_id": far.get("node_id")},
            "ee": ee,
        }],
        "ee_total": ee,
        "destination_map_id": far.get("map_id"),
        "destination_node_id": far.get("node_id"),
    }


def _mirror_grown_node(map_record: dict, grown: dict) -> str | None:
    """Mirror a grow result into the session's copy of its map.

    A missing node is appended together with its edges. An already-present
    node is HEALED instead: world-level fields (name, description, …) that
    never reached this session are merged on — the old append-only guard
    left such nodes permanently unnamed here, so the Reader could never
    offer them as destinations. Missing edges are wired in either way (an
    existing-match from another session's growth arrives with the edges it
    has at world level). Returns "added", "named" (a nameless session node
    just gained its identity), "updated" (details refreshed), or None when
    the session copy already matched."""
    node = grown["node"]
    nodes = map_record.setdefault("nodes", [])
    edges = map_record.setdefault("edges", [])
    known = {(e.get("from"), e.get("to")) for e in edges}
    known |= {(b, a) for a, b in known}
    new_edges = [dict(e) for e in grown.get("edges") or []
                 if (e.get("from"), e.get("to")) not in known]
    edges.extend(new_edges)
    existing = next((n for n in nodes if n.get("id") == node.get("id")), None)
    if existing is None:
        nodes.append(dict(node))
        return "added"
    had_name = bool(existing.get("name"))
    if _sync.merge_node_fields(existing, node):
        return "updated" if had_name else "named"
    return "updated" if new_edges else None


def _convert_legacy_travel(host, state: dict, world_data: dict, travel: dict):
    """Re-plan a pre-time-based travel record (route/leg or turn transit)
    from the player's current position. Returns a plan_journey result or
    None when nothing sensible remains."""
    destination = travel.get("destination_node_id") or travel.get("final_node_id")
    if not destination:
        return None
    return plan_journey(host, state, world_data, destination,
                        destination_region=travel.get("destination_region"))


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


def _ancestry_anchor(world_data: dict, map_id: str) -> str | None:
    """The overworld node ultimately anchoring a (chain of) child map(s):
    standing inside the school's gym, an outside placement anchors at the
    school's node on the wider map — so "across the road" lands beside the
    school, not wherever the region vaguely fits."""
    anchor = None
    record = get_map(world_data, map_id)
    seen = set()
    while record is not None and record.get("anchor_node_id") \
            and record.get("map_id") not in seen:
        seen.add(record.get("map_id"))
        anchor = record["anchor_node_id"]
        record = get_map(world_data, record.get("parent_map_id") or "")
    return anchor


async def on_mutate_state(host, mutation: dict, state: dict, sdk) -> dict:
    """Apply player movement (journeys, passages, improvised transitions)."""
    from .worldspace import ensure_v2
    world_data = state.get("world_data")
    if not world_data:
        return {}
    ensure_v2(state)
    mutation = mutation or {}
    travel = get_travel(state)
    current_node = state.get("player_location_node_id")
    current_map = player_map_id(state)

    new_node_id = clean_option(mutation.get("player_location_node_id"))
    new_region = clean_option(mutation.get("player_location_region"))
    passage_id = clean_option(mutation.get("player_passage"))
    if passage_id in ("none", "None", ""):
        passage_id = None
    grow_desc = str(mutation.get("new_sub_location") or "").strip()
    interrupted = bool(mutation.get("travel_interrupted"))
    completed = bool(mutation.get("travel_completed"))
    try:
        minutes_covered = max(0, int(mutation.get("travel_minutes_covered", 0)))
    except (TypeError, ValueError):
        minutes_covered = 0

    # Intra-site movement (instant, inside the current node's interior).
    # Any real node move clears the position — the player walked out.
    site_position_update = resolve_sub_location_move(mutation, state, world_data)

    revealed = list(set(state.get("revealed_node_ids", [])))
    revealed_dirty = False
    newly_revealed: list[str] = []
    fringe_neighbors: list[str] = []

    def reveal_around(nid, map_id=None):
        # Fog opens only where the player actually stands. Direct neighbors
        # stay unrevealed — the map renders them as a faded, name-only fringe
        # (computed from edges client-side) — but they're queued for naming
        # so that fringe has names to show.
        nonlocal revealed_dirty
        if nid not in revealed:
            revealed.append(nid)
            newly_revealed.append(nid)
            revealed_dirty = True
        adjacency = build_graph_adjacency(world_data, map_id or map_of_node(world_data, nid))
        for nb in adjacency.get(nid, []):
            if nb not in revealed and nb not in fringe_neighbors:
                fringe_neighbors.append(nb)

    def queue_revealed_backfill():
        # Newly revealed places and the name-only fringe around them get
        # detailed silently in the background so they have names/descriptions
        # by the time the story (or the map) needs them.
        if not newly_revealed and not fringe_neighbors:
            return
        by_id = node_index(world_data)
        needs = [nid for nid in newly_revealed + fringe_neighbors
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
        _expansion.maybe_expand_node(host, state, node_id)
        return {
            "player_location_node_id": node_id,
            "player_location_map_id": map_id,
            "player_location_region": new_region or region_of(node_id),
            "revealed_node_ids": revealed,
            "module_data": {"wb_worldgen": {"travel": None, "site_position": None}},
            "module_data_replace": ["travel", "site_position"],
        }

    def start_journey(planned):
        """Adopt a plan_journey result: land instantly or set the record."""
        nonlocal travel, site_position_update
        if planned is None:
            return False
        if planned[0] == "instant":
            return land_at(planned[1], planned[2])
        travel = planned[1]
        if new_region:
            travel["destination_region"] = new_region
        site_position_update = {"site_position": None}  # walked out
        if _expansion.site_mode(host) == "prefetch":
            # Start the destination's interior now — the journey's minutes
            # hide the generation latency.
            _expansion.maybe_expand_node(host, state, travel["destination_node_id"])
        return True

    def persist_world():
        sm = host._services.get("session_manager") if host._services else None
        if sm is not None and sm.state.get("world_data") is world_data:
            _sync.write_session_world_data(sm)

    async def absorb_authored(authored):
        """Make an author_location result visible in this session: mirror a
        NEW-founded node (+ link edges) into the session's world_data, or
        sync a claimed slot's fresh fields onto its existing node."""
        new_node = authored.get("new_node")
        if new_node is not None:
            target_record = get_map(world_data, authored.get("map_id")) or {}
            session_nodes = target_record.setdefault("nodes", [])
            if not any(n.get("id") == new_node.get("id") for n in session_nodes):
                session_nodes.append(dict(new_node))
                target_record.setdefault("edges", []).extend(
                    dict(e) for e in authored.get("new_edges") or [])
                persist_world()
                await _sync.embed_backfilled_nodes(
                    host, state.get("world_id"), [new_node["id"]])
        else:
            _sync.sync_enriched_nodes(host, state.get("world_id"), [authored["node_id"]])

    async def grow_inside(parent_node_id, desc):
        """Author ``desc`` as a place inside a site's interior map, creating
        the interior first when it doesn't exist yet (the expansion is told
        it must include this place). Mirrors the growth into the session and
        returns (map_id, node_id) to land at, or None."""
        child = await _expansion.ensure_child_map(
            host, state, parent_node_id, must_include=desc)
        if child is None:
            return None
        map_id = child.get("map_id")
        try:
            grown = await host.world_builder.grow_child_map(
                state.get("world_id"), map_id, desc)
        except Exception:
            grown = None
        # A belongs_outside veto here would bounce back across the boundary —
        # the request already crossed it once; give up instead of ping-ponging.
        if not grown or not (grown.get("node") or {}).get("id"):
            return None
        node = grown["node"]
        outcome = _mirror_grown_node(child, grown)
        if outcome:
            persist_world()
            if outcome in ("added", "named"):
                await _sync.embed_backfilled_nodes(
                    host, state.get("world_id"), [node["id"]])
        return map_id, node["id"]

    # --- Secret discovery: an earned find unhides a connection (no move) ---
    discover_id = clean_option(mutation.get("discover_passage"))
    if discover_id and discover_id not in ("none", "None", ""):
        secret = find_connection(world_data, discover_id)
        if secret is not None and secret.get("hidden"):
            secret["hidden"] = False
            persist_world()

    # --- Improvised transition: a NEW way through (blown wall, picked lock,
    # teleport). Target endpoints are engine-enumerated; the AI chooses what
    # the way leaves behind (one_time / open_passage / conditional_passage). --
    custom_desc = str(mutation.get("custom_transition") or "").strip()
    custom_target = clean_option(mutation.get("custom_transition_target"))
    if custom_target in ("none", "None", ""):
        custom_target = None
    becomes = clean_option(mutation.get("custom_transition_becomes")) or "one_time"
    new_location_desc = str(mutation.get("custom_transition_new_location") or "").strip()

    if custom_desc and not custom_target and new_location_desc:
        # The destination doesn't exist yet — author it onto a fitting
        # unnamed spot (one full-attention call), then land there. The
        # player's current node anchors the placement: a destination
        # described relative to here ("the school's storage building")
        # must end up nearby, not wherever the region vaguely fits.
        try:
            authored = await host.world_builder.author_location(
                state.get("world_id"), new_location_desc,
                anchor_node_id=current_node)
        except Exception:
            authored = None
        if authored and authored.get("belongs_inside"):
            # Cross-boundary redirect: the destination is really a spot
            # INSIDE an existing site — grow that site's interior and land
            # there instead of founding a map node of its own.
            landed = await grow_inside(authored["belongs_inside"], new_location_desc)
            if landed:
                return land_at(*landed)
            authored = None
        if authored and authored.get("node_id"):
            await absorb_authored(authored)
            custom_target = authored["node_id"]

    if custom_desc and custom_target:
        target_map = map_of_node(world_data, custom_target)
        if target_map is not None:
            here = {"map_id": current_map, "node_id": current_node}
            there = {"map_id": target_map, "node_id": custom_target}
            existing = connection_between(world_data, here, there, include_hidden=True)
            if existing is not None:
                # The way already exists — using it discovers it at most.
                if existing.get("hidden"):
                    existing["hidden"] = False
                    persist_world()
            elif becomes in ("open_passage", "conditional_passage"):
                digest = hashlib.sha1(
                    f"{current_node}>{custom_target}>{custom_desc}".encode()).hexdigest()[:8]
                world_data.setdefault("connections", []).append({
                    "id": f"c_{digest}",
                    "from": dict(here),
                    "to": dict(there),
                    "kind": "passage",
                    "name": custom_desc[:60],
                    "description": custom_desc,
                    "travel": {"mode": "instant"},
                    "bidirectional": True,
                    "requirements": custom_desc if becomes == "conditional_passage" else "",
                    "hidden": False,
                    "origin": "improvised",
                })
                persist_world()
            return land_at(target_map, custom_target)

    # --- Entering the current node's own interior AND a specific new place
    # inside it was declared ("I enter the school's pool through a side
    # entrance"): resolve the two together. Processed separately, the passage
    # runs first and lands (or starts a journey) at the generic entrance,
    # returning before the declared place is ever created — the player ends
    # up in the entrance hall instead of the pool. grow_inside creates the
    # interior when needed (told it must include the place), grows or
    # matches the place, and the player lands there directly.
    if grow_desc and current_node and passage_id:
        entering_own_interior = passage_id == f"enter:{current_node}"
        if not entering_own_interior:
            for v in connections_from(world_data, current_map, include_hidden=True):
                if v["connection"].get("id") == passage_id:
                    far_record = get_map(world_data, v["far"].get("map_id") or "") or {}
                    entering_own_interior = far_record.get("anchor_node_id") == current_node
                    break
        if entering_own_interior:
            landed = await grow_inside(current_node, grow_desc)
            if landed:
                return land_at(*landed)
            # Interior generation timed out or vetoed the place — fall
            # through to the plain entrance so the player still gets inside;
            # the place lands on a later turn.

    # --- Entering an unmapped place: its child map is created on demand ----
    if passage_id and passage_id.startswith("enter:"):
        target = passage_id.split(":", 1)[1]
        if target and target == current_node:
            child = await _expansion.ensure_child_map(host, state, target)
            if child is not None:
                entry_views = [v for v in connections_from(world_data, current_map, target)
                               if v["far"].get("map_id") == child.get("map_id")]
                if entry_views:
                    view = entry_views[0]
                    itinerary = _connection_itinerary(
                        view["connection"], view["near"], view["far"])
                    if travel_minutes_per_edge(host) <= 0 or itinerary["ee_total"] <= 0:
                        return land_at(view["far"]["map_id"], view["far"]["node_id"])
                    started = start_journey(("journey", {
                        "phase": "journey",
                        "itinerary": itinerary,
                        "eta_minutes": max(1, round(
                            itinerary["ee_total"] * travel_minutes_per_edge(host))),
                        "minutes_traveled": 0,
                        "waypoint_cursor": 0,
                        "destination_node_id": view["far"]["node_id"],
                        "destination_map_id": view["far"]["map_id"],
                        "destination_region": new_region,
                        "transport": view["connection"].get("kind", "passage"),
                        "map_id": current_map,
                    }))
                    if started is not True:
                        return started
                    passage_id = None
                    new_node_id = None
                else:
                    nodes = child.get("nodes") or []
                    if nodes:
                        return land_at(child.get("map_id"), nodes[0]["id"])
                    passage_id = None
            else:
                passage_id = None
        else:
            passage_id = None

    # --- A passage through a connection to another map --------------------
    # The itinerary planner routes to the connection's far side: walking to
    # the near end first, crossing, all in one journey. Zero-cost plans
    # (instant door at the player's node) land immediately.
    if passage_id:
        connection = find_connection(world_data, passage_id)
        if connection is not None:
            views = [v for v in connections_from(world_data, current_map, include_hidden=True)
                     if v["connection"].get("id") == passage_id]
            if views:
                near, far = views[0]["near"], views[0]["far"]
                planned = plan_journey(host, state, world_data, far.get("node_id"),
                                       transport=connection.get("kind", "passage"),
                                       destination_region=new_region)
                if planned is None:
                    return land_at(far.get("map_id"), far.get("node_id"))
                started = start_journey(planned)
                if started is not True:
                    return started
                new_node_id = None  # the passage decides the movement this turn

    # --- A new place inside the current map (story-created sub-location) ---
    # The Reader described somewhere inside this child map that isn't on it
    # yet ("the storage building behind the school"): author it onto the map
    # right where it belongs — anchored at the player — then move there like
    # any declared destination.
    if grow_desc and not new_node_id and not passage_id:
        current_map_record = get_map(world_data, current_map) or {}
        if current_map_record.get("anchor_node_id"):
            grown = None
            try:
                grown = await host.world_builder.grow_child_map(
                    state.get("world_id"), current_map, grow_desc,
                    near_node_id=current_node)
            except Exception:
                grown = None
            if grown and grown.get("belongs_outside"):
                # The engine vetoed: this is its own destination in the wider
                # world. Author it out there, anchored at the site's overworld
                # node (map ancestry), and step outside to it.
                authored = None
                try:
                    authored = await host.world_builder.author_location(
                        state.get("world_id"), grow_desc,
                        anchor_node_id=_ancestry_anchor(world_data, current_map))
                except Exception:
                    authored = None
                if authored and authored.get("node_id"):
                    await absorb_authored(authored)
                    target_map = map_of_node(world_data, authored["node_id"])
                    if target_map is not None:
                        return land_at(target_map, authored["node_id"])
            elif grown and (grown.get("node") or {}).get("id"):
                node = grown["node"]
                # Mirror the growth onto the session's own world_data copy
                # (the facade grew the world-level bundle, not this dict).
                # An existing-match can still heal a stale session copy whose
                # node never received its world-level name.
                outcome = _mirror_grown_node(current_map_record, grown)
                if outcome:
                    persist_world()
                    if outcome in ("added", "named"):
                        await _sync.embed_backfilled_nodes(
                            host, state.get("world_id"), [node["id"]])
                new_node_id = node["id"]
        elif current_node:
            # On the overworld at an expandable (or already expanded) site:
            # the spot belongs inside it — grow the interior and step in.
            landed = await grow_inside(current_node, grow_desc)
            if landed:
                return land_at(*landed)

    # --- Legacy record: re-plan a turn-based journey in time terms ---------
    if travel and "itinerary" not in travel:
        converted = _convert_legacy_travel(host, state, world_data, travel)
        if converted is None:
            travel = None
        elif converted[0] == "instant":
            return land_at(converted[1], converted[2])
        else:
            travel = converted[1]

    # --- A Reader-declared destination (anywhere in the world) ------------
    wants_move = new_node_id and new_node_id != current_node
    if wants_move:
        if travel and travel.get("destination_node_id") == new_node_id:
            pass  # already journeying there — keep the clock running
        else:
            planned = plan_journey(host, state, world_data, new_node_id,
                                   destination_region=new_region)
            if planned is None:
                # Unknown destination — land rather than trap the player.
                return land_at(current_map, new_node_id)
            started = start_journey(planned)
            if started is not True:
                return started

    if not travel:
        if site_position_update:
            return {"module_data": {"wb_worldgen": dict(site_position_update)}}
        if revealed_dirty:
            queue_revealed_backfill()
            return {"revealed_node_ids": revealed}
        return {}

    # --- Travel switched off mid-journey: the trip completes instantly -----
    if travel_minutes_per_edge(host) <= 0:
        destination = travel.get("destination_node_id")
        if not destination:
            return {"module_data": {"wb_worldgen": {"travel": None, **site_position_update}},
                    "module_data_replace": ["travel"]}
        new_region = new_region or travel.get("destination_region")
        return land_at(travel.get("destination_map_id")
                       or map_of_node(world_data, destination) or current_map,
                       destination)

    # --- Advance the journey by the minutes the story spent traveling ------
    eta = max(1, int(travel.get("eta_minutes", 1)))
    if completed:
        travel["minutes_traveled"] = eta
    elif not interrupted and minutes_covered > 0:
        travel["minutes_traveled"] = travel.get("minutes_traveled", 0) + minutes_covered

    moved = (completed or (not interrupted and minutes_covered > 0))
    location_update = {}
    if moved:
        progress = journey_progress(travel)
        position = progress["position"]
        cursor = travel.get("waypoint_cursor", 0)
        waypoints = position.get("waypoints") or []
        for wp_map, wp_node in waypoints[cursor:]:
            reveal_around(wp_node, wp_map)
        travel["waypoint_cursor"] = max(cursor, len(waypoints))

        if position.get("arrived") or travel["minutes_traveled"] >= eta:
            new_region = new_region or travel.get("destination_region")
            return land_at(travel.get("destination_map_id"),
                           travel.get("destination_node_id"))

        spot = position.get("position") or {}
        if spot.get("node_id") and spot["node_id"] != current_node:
            location_update = {
                "player_location_node_id": spot["node_id"],
                "player_location_map_id": spot.get("map_id") or current_map,
                "player_location_region": region_of(spot["node_id"]),
            }

    result = {"module_data": {"wb_worldgen": {"travel": travel, **site_position_update}},
              "module_data_replace": ["travel"]}
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
