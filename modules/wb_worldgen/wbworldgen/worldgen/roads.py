"""Terrain-following road network between settlement-class nodes.

Roads are recomputed from terrain (not the abstract Delaunay edges): we build a
travel-cost field (slope + biome + near-impassable water), run least-cost
Dijkstra paths between settlements, connect them with a minimum spanning tree so
every settlement is reachable, and add a few low-cost shortcuts for loops. Each
road is returned as a smoothed polyline in map space.

Routing runs on a downsampled cost grid (capped at ``_ROUTE_RES``) for speed;
path coordinates are scaled back into the abstract ``map_width × map_height``
space the renderer uses.
"""

import heapq
import math

import numpy as np

from wbworldgen.terrain import biomes as _bm

_ROUTE_RES = 128
_SETTLEMENT_TYPES = {"settlement", "port", "stronghold", "city", "crossroads"}

# Per-cell base move cost multipliers by biome (1.0 = open ground). Bare rock and
# snow are no longer biomes — high ground is penalised by a height-based term in
# ``build_cost_grid`` (see ``biomes.alpine_cover``) instead.
_BIOME_COST = {
    _bm.ICE: 6.0, _bm.DESERT: 2.0,
    _bm.JUNGLE: 2.2, _bm.TEMPERATE_RAINFOREST: 1.8, _bm.TAIGA: 1.6,
    _bm.FOREST: 1.4, _bm.SHRUBLAND: 1.1, _bm.SAVANNA: 1.1,
    _bm.GRASSLAND: 1.0, _bm.BEACH: 1.2,
}
# Extra multiplier at full bare-rock / full snow cover (roughly the old
# ROCK=4 / SNOW=5 biome costs, applied on top of the slope+biome cost).
_ROCK_COST, _SNOW_COST = 3.0, 4.0
_WATER_COST = 40.0  # crossable only at the narrowest straits (bridges/fords)


def _downsample(arr, route_res):
    res = arr.shape[0]
    if res <= route_res:
        return arr
    step = res // route_res
    return arr[::step, ::step][:route_res, :route_res]


def build_cost_grid(terrain: dict, route_res: int):
    """A [R,R] positive per-cell traversal cost (higher = harder)."""
    slope = _downsample(np.asarray(terrain["slope"], dtype=np.float64), route_res)
    biome = _downsample(np.asarray(terrain["biome"]).astype(int), route_res)
    water = _downsample(np.asarray(terrain["water"]).astype(bool), route_res)
    height = _downsample(np.asarray(terrain["height"], dtype=np.float64), route_res)
    sea = float(terrain.get("sea_level", 0.4))
    temp = terrain.get("temperature")
    temp = _downsample(np.asarray(temp, dtype=np.float64), route_res) \
        if temp is not None else None

    s = slope / (slope.max() + 1e-9)
    cost = 1.0 + 6.0 * s
    bcost = np.ones_like(cost)
    for bid, mult in _BIOME_COST.items():
        bcost[biome == bid] = mult
    cost *= bcost
    # Height-based alpine penalty: bare rock and snow make high ground costly.
    rock_w, snow_w = _bm.alpine_cover(height, sea, temp)
    cost *= 1.0 + _ROCK_COST * rock_w + _SNOW_COST * snow_w
    cost = np.where(water, _WATER_COST, cost)
    return cost


def _dijkstra(cost, start):
    """Single-source least-cost over an 8-connected grid. Returns (dist, prev)."""
    R = cost.shape[0]
    INF = math.inf
    dist = np.full(R * R, INF)
    prev = np.full(R * R, -1, dtype=np.int32)
    sr, sc = start
    s = sr * R + sc
    dist[s] = 0.0
    pq = [(0.0, s)]
    neigh = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
             (-1, -1, 1.41421356), (-1, 1, 1.41421356),
             (1, -1, 1.41421356), (1, 1, 1.41421356))
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        ur, uc = divmod(u, R)
        for dr, dc, w in neigh:
            vr, vc = ur + dr, uc + dc
            if 0 <= vr < R and 0 <= vc < R:
                v = vr * R + vc
                # Step cost = average of the two cells' costs, scaled by distance.
                nd = d + w * 0.5 * (cost[ur, uc] + cost[vr, vc])
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
    return dist, prev


def _reconstruct(prev, start_cell, end_cell, R):
    path = []
    cur = end_cell[0] * R + end_cell[1]
    s = start_cell[0] * R + start_cell[1]
    guard = 0
    while cur != -1 and guard < R * R:
        path.append(divmod(cur, R))
        if cur == s:
            break
        cur = int(prev[cur])
        guard += 1
    path.reverse()
    return path


def _chaikin(points, iterations=2):
    pts = points
    for _ in range(iterations):
        if len(pts) < 3:
            break
        out = [pts[0]]
        for i in range(len(pts) - 1):
            p, q = pts[i], pts[i + 1]
            out.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            out.append((0.25 * p[0] + 0.75 * q[0], 0.25 * q[1] + 0.75 * p[1]))
        out.append(pts[-1])
        pts = out
    return pts


def build_roads(nodes, terrain: dict, map_width: float, map_height: float,
                shortcut_count: int = 3) -> list:
    """Return ``[{from, to, path:[[x,y],...], cost}]`` connecting settlements."""
    hubs = [n for n in nodes if n.type in _SETTLEMENT_TYPES]
    if len(hubs) < 2:
        return []

    full_res = int(terrain["height"].shape[0])
    route_res = min(_ROUTE_RES, full_res)
    cost = build_cost_grid(terrain, route_res)
    R = cost.shape[0]

    def to_cell(n):
        c = int(np.clip(round(n.x / max(1e-6, map_width) * (R - 1)), 0, R - 1))
        r = int(np.clip(round(n.y / max(1e-6, map_height) * (R - 1)), 0, R - 1))
        return (r, c)

    cells = [to_cell(n) for n in hubs]

    # Single-source Dijkstra from each hub: gives all pairwise costs + paths.
    dists, prevs = [], []
    for cell in cells:
        d, p = _dijkstra(cost, cell)
        dists.append(d)
        prevs.append(p)

    n = len(hubs)
    pair_cost = [[dists[i][cells[j][0] * R + cells[j][1]] for j in range(n)]
                 for i in range(n)]

    # Prim's MST over the complete hub graph.
    in_tree = [False] * n
    in_tree[0] = True
    tree_edges = []
    for _ in range(n - 1):
        best = (math.inf, -1, -1)
        for i in range(n):
            if not in_tree[i]:
                continue
            for j in range(n):
                if in_tree[j]:
                    continue
                if pair_cost[i][j] < best[0]:
                    best = (pair_cost[i][j], i, j)
        _, i, j = best
        if j < 0:
            break
        in_tree[j] = True
        tree_edges.append((i, j))

    chosen = set((min(i, j), max(i, j)) for i, j in tree_edges)

    # A few redundant shortcuts: cheapest non-tree pairs.
    extras = sorted(
        ((pair_cost[i][j], i, j) for i in range(n) for j in range(i + 1, n)
         if (i, j) not in chosen),
        key=lambda t: t[0])
    for _, i, j in extras[:shortcut_count]:
        chosen.add((i, j))

    roads = []
    for i, j in chosen:
        c = pair_cost[i][j]
        if not math.isfinite(c):
            continue
        cell_path = _reconstruct(prevs[i], cells[i], cells[j], R)
        if len(cell_path) < 2:
            continue
        pts = [(cc / (R - 1) * map_width, rr / (R - 1) * map_height)
               for rr, cc in cell_path]
        pts = _chaikin(pts, iterations=2)
        roads.append({
            "from": hubs[i].id,
            "to": hubs[j].id,
            "path": [[round(x, 2), round(y, 2)] for x, y in pts],
            "cost": round(float(c), 2),
            "tier": "road",
        })

    # Minor paths: connect every non-hub node to its nearest hub over the same
    # cost field, reusing the per-hub Dijkstra distances/predecessors (no extra
    # routing). Each carries the node's importance so the renderer can fade
    # low-importance spurs.
    hub_ids = {h.id for h in hubs}
    for node in nodes:
        if node.id in hub_ids:
            continue
        nr, nc = to_cell(node)
        ncell_idx = nr * R + nc
        best_i, best_c = -1, math.inf
        for i in range(n):
            c = dists[i][ncell_idx]
            if c < best_c:
                best_c = c
                best_i = i
        if best_i < 0 or not math.isfinite(best_c):
            continue
        cell_path = _reconstruct(prevs[best_i], cells[best_i], (nr, nc), R)
        if len(cell_path) < 2:
            continue
        pts = [(cc / (R - 1) * map_width, rr / (R - 1) * map_height)
               for rr, cc in cell_path]
        pts = _chaikin(pts, iterations=2)
        roads.append({
            "from": hubs[best_i].id,
            "to": node.id,
            "path": [[round(x, 2), round(y, 2)] for x, y in pts],
            "cost": round(float(best_c), 2),
            "tier": "path",
            "importance": int(getattr(node, "importance", 0) or 0),
        })

    return roads
