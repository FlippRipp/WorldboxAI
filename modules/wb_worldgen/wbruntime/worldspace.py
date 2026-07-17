"""Map-space accessors over the compiled world_data plus small state readers.

These are the only functions that know how nodes/edges are laid out inside
world_data (flat ``map`` vs ``map_layers``); everything else in the runtime
goes through them.
"""


def all_map_nodes(world_data: dict) -> list[dict]:
    map_layers = world_data.get("map_layers", [])
    if map_layers:
        nodes = []
        for layer in map_layers:
            nodes.extend(layer.get("map", {}).get("nodes", []))
        return nodes
    return world_data.get("map", {}).get("nodes", [])


def all_map_edges(world_data: dict) -> list[dict]:
    map_layers = world_data.get("map_layers", [])
    if map_layers:
        edges = []
        for layer in map_layers:
            edges.extend(layer.get("map", {}).get("edges", []))
        return edges
    return world_data.get("map", {}).get("edges", [])


def build_graph_adjacency(world_data: dict) -> dict:
    """Undirected {node_id: [neighbor_id, ...]} across all layers."""
    adj: dict[str, list[str]] = {}
    for e in all_map_edges(world_data):
        fr, to = e.get("from"), e.get("to")
        if fr and to:
            adj.setdefault(fr, []).append(to)
            adj.setdefault(to, []).append(fr)
    return adj


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
