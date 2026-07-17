"""City map generator glue: street network -> playable world-map dict.

Runs the roadnet algorithm (``roadnet.py``) inside an organic city
boundary, clusters the resulting blocks (planar faces) into the authored
districts, and derives a bounded playable layer from the geometry:

- one plaza per district (``type=settlement``) — travel hubs and start
  location candidates;
- venues on blocks (``type=landmark``) — the enrichable places of the city;
- a few avenue crossroads and street-corner waypoints for texture.

The full street fabric is exported as ``roads`` polylines (tier
``avenue``/``street``) attributed to the nearest playable nodes so
fog-of-war reveal keeps working; playable ``edges`` are street-routed
adjacencies whose ``distance`` is the actual path length along streets.
"""

import heapq
import math
import random

from wbworldgen.world_map import bind_named_locations
from wbworldgen.worldgen.generation.roadnet import (
    DEFAULT_LEVELS,
    OUTWARD_GENERATIONS,
    generate_roadnet,
    point_in_polygon,
    polygon_area,
    polygon_centroid,
)

GENERATOR_ID = "city_roadnet"
MIN_PLAYABLE = 30
MAX_PLAYABLE = 120
_FALLBACK_DISTRICTS = 4
_EDGE_KNN = 3
_PLAYABLE_DEGREE_CAP = 5


def _city_boundary(rng, width, height):
    """Jittered ellipse so the city silhouette reads organic."""
    n = rng.randint(16, 24)
    cx, cy = width / 2.0, height / 2.0
    base = rng.uniform(0.24, 0.36)
    pts = []
    for i in range(n):
        ang = 2.0 * math.pi * i / n + rng.uniform(-0.5, 0.5) * math.pi / n
        r = base * rng.uniform(0.90, 1.12)
        pts.append((cx + math.cos(ang) * r * width,
                    cy + math.sin(ang) * r * height))
    return pts


def _city_plan(rng, width, height):
    """Pick a starting arrangement — the article gets its shape variety from
    different seed-node layouts: a ring road, a center with cardinal seeds,
    a line (long narrow city), or two nuclei."""
    mode = rng.choices(("ring", "radial", "linear", "twin"),
                       weights=(3, 2, 1, 1))[0]
    cx, cy = width / 2.0, height / 2.0
    if mode == "ring":
        return {"mode": mode, "boundary": _city_boundary(rng, width, height),
                "seed_points": None, "generations": 0}
    if mode == "radial":
        r = rng.uniform(0.16, 0.23) * min(width, height)
        pts = [(cx, cy)]
        for i in range(4):
            a = math.pi / 2.0 * i + rng.uniform(-0.35, 0.35)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        gens = rng.uniform(2.0, 2.6)
    elif mode == "linear":
        a = rng.uniform(0.0, math.pi)
        n = rng.randint(3, 5)
        span = 0.48 * max(width, height)
        pts = []
        for i in range(n):
            t = (i / (n - 1) - 0.5) * span
            pts.append((cx + t * math.cos(a) + rng.uniform(-40, 40),
                        cy + t * math.sin(a) + rng.uniform(-40, 40)))
        gens = rng.uniform(1.6, 2.2)
    else:  # twin nuclei
        a = rng.uniform(0.0, math.pi)
        r = rng.uniform(0.15, 0.21) * min(width, height)
        pts = [(cx + r * math.cos(a), cy + r * math.sin(a)),
               (cx - r * math.cos(a), cy - r * math.sin(a))]
        gens = rng.uniform(2.0, 2.6)
    pts = [(min(max(x, 60.0), width - 60.0), min(max(y, 60.0), height - 60.0))
           for x, y in pts]
    return {"mode": mode, "boundary": None, "seed_points": pts,
            "generations": gens}


def _district_names(compiled_world, rng):
    entries = (compiled_world or {}).get("regions", {}).get("regions", []) or []
    names = []
    for entry in entries:
        name = (entry.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    if not names:
        names = [f"District {i + 1}" for i in range(_FALLBACK_DISTRICTS)]
    return names, entries


def _face_adjacency(faces):
    """Faces sharing an undirected boundary edge are adjacent."""
    edge_faces = {}
    for fi, face in enumerate(faces):
        n = len(face)
        for k in range(n):
            a, b = face[k], face[(k + 1) % n]
            edge_faces.setdefault((a, b) if a < b else (b, a), []).append(fi)
    adj = {fi: set() for fi in range(len(faces))}
    for owners in edge_faces.values():
        for i in owners:
            for j in owners:
                if i != j:
                    adj[i].add(j)
    return adj


def _farthest_point_seeds(centroids, order_hint, count):
    """Greedy farthest-point sampling; first seed = order_hint[0]."""
    seeds = [order_hint[0]]
    while len(seeds) < count:
        best, best_d = None, -1.0
        for fi in order_hint:
            if fi in seeds:
                continue
            d = min(math.dist(centroids[fi], centroids[s]) for s in seeds)
            if d > best_d:
                best_d, best = d, fi
        if best is None:
            break
        seeds.append(best)
    return seeds


def _assign_districts(faces, centroids, adj, count, eligible):
    """Balanced contiguous region growing over the face-adjacency graph.

    Seeds spread by farthest-point sampling (only well-connected core faces
    qualify), then the district with the least claimed area expands next, so
    no district gets enclosed while still tiny. Districts grow over
    ``eligible`` (city-core) faces only; every other face (outskirts sprawl)
    inherits the district of its nearest assigned face so vertex lookups
    always resolve.
    """
    interior = [fi for fi in sorted(eligible)
                if len(adj[fi] & eligible) >= 2] or sorted(eligible)
    hint = sorted(interior, key=lambda fi: -abs(polygon_area_cached(fi)))
    seeds = _farthest_point_seeds(centroids, hint, count)
    assignment = {fi: di for di, fi in enumerate(seeds)}
    frontier = {di: [fi] for di, fi in enumerate(seeds)}
    area = {di: abs(polygon_area_cached(fi)) for di, fi in enumerate(seeds)}
    heap = [(area[di], di) for di in frontier]
    heapq.heapify(heap)
    while heap:
        a, di = heapq.heappop(heap)
        if a != area[di]:
            continue  # stale entry
        grabbed = None
        queue = frontier[di]
        while queue and grabbed is None:
            fi = queue[0]
            for nb in sorted(adj[fi]):
                if nb in eligible and nb not in assignment:
                    grabbed = nb
                    break
            if grabbed is None:
                queue.pop(0)  # frontier face exhausted
        if grabbed is None:
            continue  # district can grow no further
        assignment[grabbed] = di
        queue.append(grabbed)
        area[di] += abs(polygon_area_cached(grabbed))
        heapq.heappush(heap, (area[di], di))
    for fi in range(len(faces)):
        if fi not in assignment:
            nearest = min(assignment,
                          key=lambda o: math.dist(centroids[fi], centroids[o]))
            assignment[fi] = assignment[nearest]
    return assignment


# polygon area cache filled per build (module-level to keep helpers simple)
_AREA_CACHE = {}


def polygon_area_cached(fi):
    return _AREA_CACHE[fi]


def _longest_edge_midpoint(poly):
    best, best_len = None, -1.0
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        d = math.dist(p, q)
        if d > best_len:
            best_len = d
            best = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
    return best


def _pull(p, target, t):
    return (p[0] + (target[0] - p[0]) * t, p[1] + (target[1] - p[1]) * t)


def _street_graph(net):
    graph = {i: [] for i in range(len(net.points))}
    for a, b in net.edges:
        d = math.dist(net.points[a], net.points[b])
        graph[a].append((b, d))
        graph[b].append((a, d))
    return graph


def _dijkstra(graph, source):
    dist = {source: 0.0}
    heap = [(0.0, source)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        for v, w in graph[u]:
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


def _nearest_owner(graph, sources):
    """Multi-source Dijkstra: every street vertex -> owning playable id."""
    dist = {}
    owner = {}
    heap = []
    for vertex, pid in sources:
        if vertex not in dist or 0.0 < dist[vertex]:
            dist[vertex] = 0.0
            owner[vertex] = pid
            heapq.heappush(heap, (0.0, vertex, pid))
    while heap:
        d, u, pid = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        for v, w in graph[u]:
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                owner[v] = pid
                heapq.heappush(heap, (nd, v, pid))
    return owner


def build_city_map(spec: dict) -> dict:
    """Build the standard {nodes, edges, regions, config, roads} map dict.

    spec: {compiled_world, total_nodes?, map_width?, map_height?, seed?,
    id_prefix?}.
    """
    compiled = spec.get("compiled_world") or {}
    budget = int(spec.get("total_nodes") or 60)
    budget = max(MIN_PLAYABLE, min(MAX_PLAYABLE, budget))
    seed = spec.get("seed")
    if not seed:
        seed = random.randrange(1, 2 ** 31)
    seed = int(seed)
    id_prefix = spec.get("id_prefix", "") or ""
    rng = random.Random(seed)

    if spec.get("map_width") and spec.get("map_height"):
        width = float(spec["map_width"])
        height = float(spec["map_height"])
    else:
        # Vary the canvas shape per city; the longest side stays 1000.
        aspect = rng.choice((1.0, 1.0, 1.3, 1.6))
        width, height = 1000.0, round(1000.0 / aspect)
        if rng.random() < 0.5:
            width, height = height, width

    # Generate on an overscanned virtual canvas so organic growth is never
    # clipped by the map border, then uniformly scale/centre the result into
    # the real map — the fringe peters out instead of squishing into a box.
    over = 1.5
    off = ((over - 1.0) / 2.0 * width, (over - 1.0) / 2.0 * height)
    plan = _city_plan(rng, width, height)
    boundary = plan["boundary"]
    if boundary is not None:
        boundary = [(x + off[0], y + off[1]) for x, y in boundary]
    seed_pts = plan["seed_points"]
    if seed_pts is not None:
        seed_pts = [(x + off[0], y + off[1]) for x, y in seed_pts]
    net = generate_roadnet(seed, width * over, height * over, DEFAULT_LEVELS,
                           boundary,
                           outward_generations=OUTWARD_GENERATIONS,
                           seed_points=seed_pts,
                           seed_generations=plan["generations"])
    xs = [p[0] for p in net.points]
    ys = [p[1] for p in net.points]
    pad = 0.025 * min(width, height)
    bcx, bcy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    scale = min(1.0,
                (width - 2 * pad) / max(1.0, max(xs) - min(xs)),
                (height - 2 * pad) / max(1.0, max(ys) - min(ys)))

    def _fit(p):
        return ((p[0] - bcx) * scale + width / 2.0,
                (p[1] - bcy) * scale + height / 2.0)

    net.points = [_fit(p) for p in net.points]
    if boundary is not None:
        boundary = [_fit(p) for p in boundary]
    if seed_pts is not None:
        seed_pts = [_fit(p) for p in seed_pts]
    faces = net.faces

    _AREA_CACHE.clear()
    for fi, face in enumerate(faces):
        _AREA_CACHE[fi] = polygon_area([net.points[i] for i in face])
    centroids = [polygon_centroid([net.points[i] for i in face])
                 for face in faces]

    # Blocks outside the city core belong to the outskirts: streets render
    # there, but districts, plazas and venues stay in the core. Ring cities
    # bound the core with the boundary polygon; seed-grown cities bound it
    # by distance to the arterial seed points.
    if boundary is not None:
        def _is_core(p):
            return point_in_polygon(p, boundary)
    else:
        seeds_xy = seed_pts
        reach = (plan["generations"] * DEFAULT_LEVELS[0].extension_max
                 * 0.75 * scale)

        def _is_core(p):
            return min(math.dist(p, s) for s in seeds_xy) <= reach
    core_vertex = [_is_core(p) for p in net.points]
    core_faces = {fi for fi in range(len(faces)) if _is_core(centroids[fi])}
    if not core_faces:
        core_faces = set(range(len(faces)))

    names, region_entries = _district_names(compiled, rng)
    count = max(1, min(len(names), len(core_faces)))
    names = names[:count]
    adj = _face_adjacency(faces)
    face_district = _assign_districts(faces, centroids, adj, count, core_faces)

    district_faces = {di: [] for di in range(count)}
    for fi in core_faces:
        district_faces[face_district[fi]].append(fi)
    for di in district_faces:
        district_faces[di].sort(key=lambda fi: -abs(_AREA_CACHE[fi]))
    district_area = {di: sum(abs(_AREA_CACHE[fi]) for fi in fis)
                     for di, fis in district_faces.items()}
    heart = max(district_area, key=district_area.get) if district_area else 0

    nodes = []
    used_faces = set()

    def _add_node(pos, node_type, importance, district, city_kind):
        nid = f"{id_prefix}c_{len(nodes) + 1}"
        nodes.append({
            "id": nid,
            "x": round(min(max(pos[0], 0.0), width), 4),
            "y": round(min(max(pos[1], 0.0), height), 4),
            "importance": importance,
            "name": "",
            "description": "",
            "label_description": "",
            "type": node_type,
            "region": names[district],
            "city_kind": city_kind,
        })
        return nid

    # Plazas: one per district, on its largest block.
    plaza_ids = {}
    for di in range(count):
        if not district_faces[di]:
            continue
        fi = district_faces[di][0]
        used_faces.add(fi)
        poly = [net.points[i] for i in faces[fi]]
        pos = _pull(centroids[fi], _longest_edge_midpoint(poly), 0.5)
        plaza_ids[di] = _add_node(
            pos, "settlement", 8 if di == heart else 7, di, "plaza")

    # Venues: round-robin across districts over the remaining largest blocks.
    n_crossroads = max(2, budget // 8)
    n_waypoints = max(2, budget // 10)
    n_venues = max(count, budget - len(nodes) - n_crossroads - n_waypoints)
    cursor = {di: 1 for di in range(count)}
    di = 0
    placed = 0
    stalled = 0
    while placed < n_venues and stalled < count:
        fis = district_faces[di % count]
        c = cursor[di % count]
        if c < len(fis):
            fi = fis[c]
            cursor[di % count] += 1
            if fi not in used_faces:
                used_faces.add(fi)
                poly = [net.points[i] for i in faces[fi]]
                pos = _pull(centroids[fi], _longest_edge_midpoint(poly), 0.7)
                area = abs(_AREA_CACHE[fi])
                big = sorted(_AREA_CACHE.values(), key=abs)[-1]
                importance = 4 + min(2, int(2.9 * area / abs(big)))
                _add_node(pos, "landmark", importance, di % count, "venue")
                placed += 1
            stalled = 0
        else:
            stalled += 1
        di += 1

    def _vertex_district(v):
        p = net.points[v]
        nearest = min(range(len(faces)),
                      key=lambda fi: math.dist(p, centroids[fi]))
        return face_district[nearest]

    # Crossroads: intersections where level-0 avenues meet.
    vertex_used = set()
    avenue_deg = {}
    for (a, b), lvl in net.edge_levels.items():
        if lvl == 0:
            avenue_deg[a] = avenue_deg.get(a, 0) + 1
            avenue_deg[b] = avenue_deg.get(b, 0) + 1
    candidates = sorted(v for v, d in avenue_deg.items()
                        if d >= 2 and v not in net.anchors and core_vertex[v])
    node_positions = [(n["x"], n["y"]) for n in nodes]

    def _spread_pick(cands, want, min_gap):
        picked = []
        for v in cands:
            if len(picked) >= want:
                break
            p = net.points[v]
            near_node = any(math.dist(p, q) < min_gap for q in node_positions)
            near_pick = any(math.dist(p, net.points[o]) < min_gap for o in picked)
            if not near_node and not near_pick:
                picked.append(v)
        return picked

    for v in _spread_pick(candidates, n_crossroads, DEFAULT_LEVELS[0].clearance):
        vertex_used.add(v)
        _add_node(net.points[v], "crossroads", 3 if avenue_deg.get(v, 0) >= 3 else 2,
                  _vertex_district(v), "corner")

    # Waypoints: spread street corners to fill the budget.
    remaining = budget - len(nodes)
    if remaining > 0:
        corner_cands = sorted(v for v in range(len(net.points))
                              if v not in vertex_used and v not in net.anchors
                              and core_vertex[v])
        node_positions = [(n["x"], n["y"]) for n in nodes]
        for v in _spread_pick(corner_cands, remaining,
                              DEFAULT_LEVELS[1].clearance * 1.5):
            _add_node(net.points[v], "waypoint", 1 + (net.point_levels[v] == 0),
                      _vertex_district(v), "corner")

    # Snap playable nodes to street vertices, route edges along streets.
    graph = _street_graph(net)
    snap = {}
    for n in nodes:
        p = (n["x"], n["y"])
        snap[n["id"]] = min(range(len(net.points)),
                            key=lambda v: math.dist(p, net.points[v]))
    by_snap = {}
    for nid, v in snap.items():
        by_snap.setdefault(v, nid)

    dists = {}  # (id_a, id_b) sorted -> street distance
    for n in nodes:
        dist = _dijkstra(graph, snap[n["id"]])
        for m in nodes:
            if m["id"] <= n["id"]:
                continue
            d = dist.get(snap[m["id"]])
            if d is not None:
                key = (n["id"], m["id"])
                straight = math.dist((n["x"], n["y"]), (m["x"], m["y"]))
                dists[key] = max(d, straight * 0.5) or 1.0

    edges = []
    edge_keys = set()
    degree = {n["id"]: 0 for n in nodes}

    def _add_playable_edge(key):
        if key in edge_keys:
            return
        edge_keys.add(key)
        degree[key[0]] += 1
        degree[key[1]] += 1
        edges.append({"from": key[0], "to": key[1],
                      "distance": round(dists[key], 1)})

    # Minimum spanning tree first (guarantees one component)...
    ids = [n["id"] for n in nodes]
    in_tree = {ids[0]}
    while len(in_tree) < len(ids):
        best = None
        for key, d in dists.items():
            a_in, b_in = key[0] in in_tree, key[1] in in_tree
            if a_in == b_in:
                continue
            if best is None or d < dists[best]:
                best = key
        if best is None:
            break  # unreachable snap (shouldn't happen on a connected net)
        _add_playable_edge(best)
        in_tree.update(best)
    # ...then k nearest neighbors for local texture, under a degree cap.
    for n in nodes:
        neigh = sorted((key for key in dists if n["id"] in key),
                       key=lambda key: dists[key])
        added = 0
        for key in neigh:
            if added >= _EDGE_KNN:
                break
            if key in edge_keys:
                added += 1
                continue
            if degree[key[0]] >= _PLAYABLE_DEGREE_CAP or \
               degree[key[1]] >= _PLAYABLE_DEGREE_CAP:
                continue
            _add_playable_edge(key)
            added += 1

    # Street fabric export, owned by nearest playable nodes. Same-tier runs
    # through degree-2 vertices merge into single polylines — an order of
    # magnitude fewer road entries for the SVG renderer to draw.
    owner = _nearest_owner(graph, [(snap[nid], nid) for nid in snap])
    fallback = ids[0]

    def _tier(lvl):
        return "avenue" if lvl == 0 else "lane" if lvl >= 3 else "street"

    adj_by_tier = {}
    for (a, b), lvl in net.edge_levels.items():
        t = _tier(lvl)
        adj_by_tier.setdefault(t, {}).setdefault(a, set()).add(b)
        adj_by_tier[t].setdefault(b, set()).add(a)

    _MAX_STROKE = 10  # segments per polyline; keeps fog reveal granular

    def _extend(chain, tadj, used):
        while len(chain) <= _MAX_STROKE:
            tail, prev = chain[-1], chain[-2]
            pt, pp = net.points[tail], net.points[prev]
            din = (pt[0] - pp[0], pt[1] - pp[1])
            nin = math.hypot(*din)
            best, best_align = None, 0.77  # only continue near-straight (<40 deg)
            for n in sorted(tadj.get(tail, ())):
                if n == prev:
                    continue
                if ((tail, n) if tail < n else (n, tail)) in used:
                    continue
                pn = net.points[n]
                dout = (pn[0] - pt[0], pn[1] - pt[1])
                nout = math.hypot(*dout)
                if nin < 1e-9 or nout < 1e-9:
                    continue
                align = (din[0] * dout[0] + din[1] * dout[1]) / (nin * nout)
                if align > best_align:
                    best_align, best = align, n
            if best is None:
                break
            used.add((tail, best) if tail < best else (best, tail))
            chain.append(best)

    roads = []
    used = set()
    for (a, b) in sorted(net.edges):
        if (a, b) in used:
            continue
        lvl = net.edge_levels.get((a, b), 1)
        t = _tier(lvl)
        tadj = adj_by_tier[t]
        chain = [a, b]
        used.add((a, b))
        # Grow the stroke both ways along near-straight same-tier segments.
        _extend(chain, tadj, used)
        chain.reverse()
        _extend(chain, tadj, used)
        roads.append({
            "from": owner.get(chain[0], fallback),
            "to": owner.get(chain[-1], fallback),
            "path": [[round(net.points[v][0], 2), round(net.points[v][1], 2)]
                     for v in chain],
            "tier": t,
            "importance": 5 if lvl == 0 else 2 if lvl < 3 else 1,
        })

    regions = []
    for di in range(count):
        member_ids = [n["id"] for n in nodes if n["region"] == names[di]]
        regions.append({
            "region_name": names[di],
            "node_ids": member_ids,
            "center_node_id": plaza_ids.get(di, member_ids[0] if member_ids else ""),
        })

    # Authored district content (faction seats, landmarks) binds onto the
    # district's own nodes; leftover entries without a district bind anywhere.
    entry_by_name = {(e.get("name") or "").strip(): e for e in region_entries}
    for di, name in enumerate(names):
        named = (entry_by_name.get(name) or {}).get("named_locations") or []
        if named:
            members = [n for n in nodes if n["region"] == name]
            settlements = [l for l in named if l.get("category") == "settlement"]
            landmarks = [l for l in named if l.get("category") != "settlement"]
            # Faction seats may claim the plaza; landmarks keep off it so the
            # district center stays settlement-typed.
            bind_named_locations(members, settlements)
            bind_named_locations(
                [n for n in members if n.get("city_kind") != "plaza"], landmarks)
    stray = [loc for name, e in entry_by_name.items() if name not in names
             for loc in (e.get("named_locations") or [])]
    if stray:
        bind_named_locations(nodes, stray)

    config = {
        "total_nodes": budget,
        "map_width": width,
        "map_height": height,
        "seed": seed,
        "city_plan": plan["mode"],
        "generator_id": GENERATOR_ID,
        "generated_from": compiled.get("generated_from", ""),
    }
    return {
        "nodes": nodes,
        "edges": edges,
        "regions": regions,
        "config": config,
        "roads": roads,
        "generator_id": GENERATOR_ID,
    }
