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
    generate_roadnet,
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
    pts = []
    for i in range(n):
        ang = 2.0 * math.pi * i / n + rng.uniform(-0.5, 0.5) * math.pi / n
        r = rng.uniform(0.40, 0.48)
        pts.append((cx + math.cos(ang) * r * width,
                    cy + math.sin(ang) * r * height))
    return pts


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


def _assign_districts(faces, centroids, adj, count):
    """Contiguous multi-source BFS over face adjacency. Returns face->district."""
    by_area_hint = sorted(range(len(faces)),
                          key=lambda fi: -abs(polygon_area_cached(fi)))
    seeds = _farthest_point_seeds(centroids, by_area_hint, count)
    assignment = {}
    queue = []
    for di, fi in enumerate(seeds):
        assignment[fi] = di
        queue.append(fi)
    head = 0
    while head < len(queue):
        fi = queue[head]
        head += 1
        for nb in sorted(adj[fi]):
            if nb not in assignment:
                assignment[nb] = assignment[fi]
                queue.append(nb)
    # Faces in disconnected pockets: nearest assigned centroid.
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
    width = float(spec.get("map_width") or 1000)
    height = float(spec.get("map_height") or 1000)
    budget = int(spec.get("total_nodes") or 60)
    budget = max(MIN_PLAYABLE, min(MAX_PLAYABLE, budget))
    seed = spec.get("seed")
    if not seed:
        seed = random.randrange(1, 2 ** 31)
    seed = int(seed)
    id_prefix = spec.get("id_prefix", "") or ""
    rng = random.Random(seed)

    boundary = _city_boundary(rng, width, height)
    net = generate_roadnet(seed, width, height, DEFAULT_LEVELS, boundary)
    faces = net.faces

    _AREA_CACHE.clear()
    for fi, face in enumerate(faces):
        _AREA_CACHE[fi] = polygon_area([net.points[i] for i in face])
    centroids = [polygon_centroid([net.points[i] for i in face])
                 for face in faces]

    names, region_entries = _district_names(compiled, rng)
    count = max(1, min(len(names), len(faces)))
    names = names[:count]
    adj = _face_adjacency(faces)
    face_district = _assign_districts(faces, centroids, adj, count)

    district_faces = {di: [] for di in range(count)}
    for fi, di in face_district.items():
        district_faces[di].append(fi)
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
                        if d >= 2 and v not in net.anchors)
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
                              if v not in vertex_used and v not in net.anchors)
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

    # Street fabric export: every raw segment, owned by nearest playable node.
    owner = _nearest_owner(graph, [(snap[nid], nid) for nid in snap])
    fallback = ids[0]
    roads = []
    for (a, b) in sorted(net.edges):
        lvl = net.edge_levels.get((a, b), 1)
        pa, pb = net.points[a], net.points[b]
        roads.append({
            "from": owner.get(a, fallback),
            "to": owner.get(b, fallback),
            "path": [[round(pa[0], 2), round(pa[1], 2)],
                     [round(pb[0], 2), round(pb[1], 2)]],
            "tier": "avenue" if lvl == 0 else "street",
            "importance": 5 if lvl == 0 else 2,
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
