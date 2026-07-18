"""Map-space accessors over the world_data dict plus small state readers.

The heavy lifting lives in ``wbworldgen.worldgen.mapspace`` (shared with the
generation side); this module re-exports it for the runtime and adds the
session-state readers. All accessors tolerate legacy (flat ``map`` /
``map_layers``) inputs, but at hook time ``ensure_v2`` migrates the session's
world_data to world_format 2 first.
"""

from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen.mapspace import (  # noqa: F401  (re-exports)
    ROOT_MAP_ID,
    breadcrumb,
    children_by_anchor,
    connection_between,
    connections_from,
    find_connection,
    get_map,
    map_edges,
    map_nodes,
    map_of_node,
    maps_by_id,
    node_index,
    parallel_siblings,
)


def all_map_nodes(world_data: dict) -> list[dict]:
    return _ms.all_nodes(world_data)


def all_map_edges(world_data: dict) -> list[dict]:
    return _ms.all_edges(world_data)


def build_graph_adjacency(world_data: dict, map_id: str = None) -> dict:
    """Undirected {node_id: [neighbor_id, ...]}; one map or all of them."""
    edges = _ms.map_edges(world_data, map_id) if map_id else _ms.all_edges(world_data)
    adj: dict[str, list[str]] = {}
    for e in edges:
        fr, to = e.get("from"), e.get("to")
        if fr and to:
            adj.setdefault(fr, []).append(to)
            adj.setdefault(to, []).append(fr)
    return adj


def player_map_id(state: dict) -> str:
    """The map the player is on (root when unset)."""
    wd = state.get("world_data") or {}
    return state.get("player_location_map_id") or wd.get("root_map_id") or ROOT_MAP_ID


def ensure_v2(state: dict) -> bool:
    """Migrate the session's world_data + state keys to world_format 2 in
    place. Returns True when anything changed (caller persists lazily via the
    normal write path)."""
    wd = state.get("world_data")
    if not isinstance(wd, dict):
        return False
    from wbworldgen.worldgen.migrate import migrate_session_state, migrate_world_data
    was_v2 = wd.get("world_format", 0) >= 2 and isinstance(wd.get("maps"), dict)
    migrate_world_data(wd)
    changed = not was_v2
    if migrate_session_state(state):
        changed = True
    return changed


def fringe_node_ids(world_data: dict, revealed: set) -> set:
    """Nodes one edge beyond the revealed set. The map shows them as faded,
    name-only markers (nameless ones stay hidden until backfill names them);
    they are valid travel destinations but their details stay unknown until
    the player actually goes there."""
    fringe = set()
    for e in all_map_edges(world_data):
        fr, to = e.get("from"), e.get("to")
        if not fr or not to:
            continue
        if fr in revealed and to not in revealed:
            fringe.add(to)
        elif to in revealed and fr not in revealed:
            fringe.add(fr)
    return fringe


def reveal_bfs(start_id: str, adjacency: dict, radius: int) -> set:
    visited = {start_id}
    frontier = [start_id]
    for _ in range(radius):
        next_frontier = []
        for nid in frontier:
            for nb in adjacency.get(nid, []):
                if nb not in visited:
                    visited.add(nb)
                    next_frontier.append(nb)
        frontier = next_frontier
    return visited


def clean_option(value):
    """Mutation selects offer 'node_id (Name)' options; keep only the id."""
    if isinstance(value, str) and " (" in value:
        return value.split(" (", 1)[0].strip()
    return value


def get_travel(state: dict):
    return (state.get("module_data", {}).get("wb_worldgen") or {}).get("travel")


def get_site_position(state: dict):
    return (state.get("module_data", {}).get("wb_worldgen") or {}).get("site_position")


def node_needs_detail(node: dict) -> bool:
    return not node.get("name") or not node.get("description")
