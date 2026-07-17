"""Deterministic interior map layout.

Turns LLM-authored interior content — a list of locations with a name-level
adjacency graph — into a renderable, travelable map: x/y positions from a
seeded force-directed layout (Fruchterman–Reingold), edges with distances,
importance from connectivity. No LLM, no randomness beyond the map-id seed,
so the same input always lays out the same way (site migration relies on
this).

Contract: ``layout_interior(map_id, locations)`` where each location is
``{id?, name, type, description, adjacent: [ids-or-names], is_entrance?}``.
Returns ``{nodes, edges, config}`` in the standard per-map shape. The
entrance node is pinned toward the bottom edge so interiors read
"door at the bottom"; ``config.instant_travel`` marks room-to-room movement
as instant for the travel engine.
"""

import math
import random

MAP_SIZE = 100.0
_ITERATIONS = 150


def _resolve_adjacency(locations: list[dict]) -> list[tuple[int, int]]:
    """Adjacency as index pairs; entries may reference ids or names."""
    by_id = {}
    by_name = {}
    for i, loc in enumerate(locations):
        if loc.get("id"):
            by_id[str(loc["id"])] = i
        if loc.get("name"):
            by_name[str(loc["name"]).strip().lower()] = i
    pairs = set()
    for i, loc in enumerate(locations):
        for ref in loc.get("adjacent", []) or []:
            j = by_id.get(str(ref))
            if j is None:
                j = by_name.get(str(ref).strip().lower())
            if j is None or j == i:
                continue
            pairs.add((min(i, j), max(i, j)))
    return sorted(pairs)


def _connect_components(n: int, pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Bridge disconnected components (highest-degree nodes of each)."""
    if n == 0:
        return pairs
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    degree = [0] * n
    for a, b in pairs:
        union(a, b)
        degree[a] += 1
        degree[b] += 1

    components: dict[int, list[int]] = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)
    if len(components) <= 1:
        return pairs
    reps = [max(members, key=lambda i: (degree[i], -i)) for members in components.values()]
    bridged = list(pairs)
    for a, b in zip(reps, reps[1:]):
        bridged.append((min(a, b), max(a, b)))
        union(a, b)
    return bridged


def layout_interior(map_id: str, locations: list[dict]) -> dict:
    """Lay out authored interior locations into a {nodes, edges, config} map."""
    n = len(locations)
    if n == 0:
        return {"nodes": [], "edges": [],
                "config": {"map_width": MAP_SIZE, "map_height": MAP_SIZE,
                           "instant_travel": True, "generated_from": "interior_layout"}}

    pairs = _connect_components(n, _resolve_adjacency(locations))
    entrance_idx = next((i for i, loc in enumerate(locations) if loc.get("is_entrance")), 0)

    # Seeded initial ring; deterministic per map id.
    rng = random.Random(f"interior:{map_id}")
    pos = []
    for i in range(n):
        angle = 2 * math.pi * i / n + rng.uniform(-0.1, 0.1)
        radius = MAP_SIZE * 0.35
        pos.append([MAP_SIZE / 2 + radius * math.cos(angle),
                    MAP_SIZE / 2 + radius * math.sin(angle)])

    # Fruchterman–Reingold with the entrance pinned to the bottom edge.
    k = MAP_SIZE / max(math.sqrt(n), 1.0)
    temperature = MAP_SIZE / 8
    for _ in range(_ITERATIONS):
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                dx = pos[i][0] - pos[j][0]
                dy = pos[i][1] - pos[j][1]
                dist = max(math.hypot(dx, dy), 0.01)
                force = k * k / dist
                disp[i][0] += dx / dist * force
                disp[i][1] += dy / dist * force
                disp[j][0] -= dx / dist * force
                disp[j][1] -= dy / dist * force
        for a, b in pairs:
            dx = pos[a][0] - pos[b][0]
            dy = pos[a][1] - pos[b][1]
            dist = max(math.hypot(dx, dy), 0.01)
            force = dist * dist / k
            disp[a][0] -= dx / dist * force
            disp[a][1] -= dy / dist * force
            disp[b][0] += dx / dist * force
            disp[b][1] += dy / dist * force
        for i in range(n):
            if i == entrance_idx:
                continue
            d = max(math.hypot(*disp[i]), 0.01)
            step = min(d, temperature)
            pos[i][0] += disp[i][0] / d * step
            pos[i][1] += disp[i][1] / d * step
            pos[i][0] = min(MAP_SIZE * 0.92, max(MAP_SIZE * 0.08, pos[i][0]))
            pos[i][1] = min(MAP_SIZE * 0.92, max(MAP_SIZE * 0.08, pos[i][1]))
        pos[entrance_idx] = [MAP_SIZE / 2, MAP_SIZE * 0.9]
        temperature *= 0.95

    degree = [0] * n
    for a, b in pairs:
        degree[a] += 1
        degree[b] += 1

    nodes = []
    for i, loc in enumerate(locations):
        importance = min(10, max(1, 3 + degree[i] + (2 if i == entrance_idx else 0)))
        nodes.append({
            "id": str(loc.get("id") or f"{map_id}:n{i}"),
            "name": loc.get("name", ""),
            "type": loc.get("type", "room"),
            "description": loc.get("description", ""),
            "label_description": loc.get("label_description", ""),
            "x": round(pos[i][0], 2),
            "y": round(pos[i][1], 2),
            "importance": importance,
        })
    edges = []
    for a, b in pairs:
        dist = math.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1])
        edges.append({"from": nodes[a]["id"], "to": nodes[b]["id"],
                      "distance": round(max(dist, 1.0), 2)})
    return {
        "nodes": nodes,
        "edges": edges,
        "config": {"map_width": MAP_SIZE, "map_height": MAP_SIZE,
                   "instant_travel": True, "generated_from": "interior_layout"},
        "entrance_node_id": nodes[entrance_idx]["id"],
    }
