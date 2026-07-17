"""Invariant tests for the planar street-network generator (roadnet)."""

import math

import pytest

from wbworldgen.worldgen.generation.roadnet import (
    DEFAULT_LEVELS,
    LevelParams,
    MAX_DEGREE,
    MIN_ANGLE_DEG,
    extract_faces,
    generate_roadnet,
    point_in_polygon,
    polygon_area,
    segments_intersect,
)

SEEDS = [1234, 7, 99]
_CACHE = {}


def _net(seed):
    if seed not in _CACHE:
        _CACHE[seed] = generate_roadnet(seed, 1000, 1000)
    return _CACHE[seed]


def _adjacency(net):
    adj = {i: set() for i in range(len(net.points))}
    for a, b in net.edges:
        adj[a].add(b)
        adj[b].add(a)
    return adj


def _angle_deg(v1, v2):
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    d = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, d))))


def test_determinism():
    a = generate_roadnet(555, 1000, 1000)
    b = generate_roadnet(555, 1000, 1000)
    assert a.points == b.points
    assert a.edges == b.edges
    assert a.edge_levels == b.edge_levels
    assert a.faces == b.faces
    c = generate_roadnet(556, 1000, 1000)
    assert c.edges != a.edges


@pytest.mark.parametrize("seed", SEEDS)
def test_planarity(seed):
    net = _net(seed)
    edges = sorted(net.edges)
    for i, (a, b) in enumerate(edges):
        for c, d in edges[i + 1:]:
            if len({a, b, c, d}) < 4:
                continue
            assert not segments_intersect(
                net.points[a], net.points[b], net.points[c], net.points[d]), \
                f"edges ({a},{b}) and ({c},{d}) cross"


@pytest.mark.parametrize("seed", SEEDS)
def test_degree_cap_and_no_isolated(seed):
    net = _net(seed)
    adj = _adjacency(net)
    for v, ns in adj.items():
        assert 1 <= len(ns) <= MAX_DEGREE


@pytest.mark.parametrize("seed", SEEDS)
def test_min_angle(seed):
    """Incident street pairs meet at >= 55 degrees.

    Edges added by the relaxed connectivity pass (net.relaxed_edges) are
    exempt — they trade the angle rule for a connected network.
    """
    net = _net(seed)
    adj = _adjacency(net)
    for v, ns in adj.items():
        pv = net.points[v]
        ns = sorted(ns)
        for i, a in enumerate(ns):
            for b in ns[i + 1:]:
                if (min(v, a), max(v, a)) in net.relaxed_edges:
                    continue
                if (min(v, b), max(v, b)) in net.relaxed_edges:
                    continue
                pa, pb = net.points[a], net.points[b]
                ang = _angle_deg((pa[0] - pv[0], pa[1] - pv[1]),
                                 (pb[0] - pv[0], pb[1] - pv[1]))
                assert ang >= 55.0, f"{ang:.1f} deg at vertex {v}"


@pytest.mark.parametrize("seed", SEEDS)
def test_no_triangles(seed):
    net = _net(seed)
    adj = _adjacency(net)
    for a, b in net.edges:
        if (a, b) in net.relaxed_edges:
            continue
        for c in adj[a] & adj[b]:
            assert (min(a, c), max(a, c)) in net.relaxed_edges or \
                   (min(b, c), max(b, c)) in net.relaxed_edges, \
                   f"triangle {a},{b},{c} among strict edges"


@pytest.mark.parametrize("seed", SEEDS)
def test_single_component(seed):
    net = _net(seed)
    adj = _adjacency(net)
    seen = set()
    stack = [0]
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        stack.extend(adj[u] - seen)
    assert len(seen) == len(net.points)


@pytest.mark.parametrize("seed", SEEDS)
def test_coordinates_in_bounds(seed):
    net = _net(seed)
    for x, y in net.points:
        assert 0.0 <= x <= 1000.0
        assert 0.0 <= y <= 1000.0


def test_face_extraction_grid():
    # Hand-built 3x3-vertex grid (2x2 cells): 4 interior faces + outer.
    points = [(x * 100.0, y * 100.0) for y in range(3) for x in range(3)]
    edges = set()
    for y in range(3):
        for x in range(3):
            i = y * 3 + x
            if x < 2:
                edges.add((i, i + 1))
            if y < 2:
                edges.add((i, i + 3))
    interior, outer = extract_faces(points, edges)
    assert len(interior) == 4
    assert all(len(f) == 4 for f in interior)
    for f in interior:
        assert abs(abs(polygon_area([points[i] for i in f])) - 100.0 * 100.0) < 1e-6
    assert len(outer) == 8  # boundary ring
    # Euler: V - E + F = 2 (F counts the outer face).
    assert len(points) - len(edges) + (len(interior) + 1) == 2


@pytest.mark.parametrize("seed", SEEDS)
def test_faces_and_euler(seed):
    """Faces are non-degenerate; Euler holds on the pruned 2-core."""
    net = _net(seed)
    assert len(net.faces) > 50
    assert len(net.outer_face) >= 3
    for face in net.faces:
        assert len(face) >= 3
        assert abs(polygon_area([net.points[i] for i in face])) > 0.0
    # Rebuild the 2-core the face walk ran on and check Euler's formula.
    adj = _adjacency(net)
    while True:
        leaves = [v for v, ns in adj.items() if len(ns) <= 1]
        if not leaves:
            break
        for v in leaves:
            for n in adj[v]:
                adj[n].discard(v)
            del adj[v]
    core_vertices = len(adj)
    core_edges = sum(len(ns) for ns in adj.values()) // 2
    # The 2-core of a connected graph can split into components; Euler
    # generalizes to V - E + F = 1 + C.
    comps = 0
    seen = set()
    for v in adj:
        if v in seen:
            continue
        comps += 1
        stack = [v]
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            stack.extend(adj[u] - seen)
    assert core_vertices - core_edges + (len(net.faces) + 1) == 1 + comps


def test_densification_adds_smaller_blocks():
    coarse = generate_roadnet(1234, 1000, 1000, levels=DEFAULT_LEVELS[:1])
    full = _net(1234)
    assert len(full.faces) > len(coarse.faces)
    assert max(full.point_levels) >= 1
    # Densified (scattered, non-anchor) points respect the level-1 clearance.
    lvl1 = [p for i, (p, lv) in enumerate(zip(full.points, full.point_levels))
            if lv == 1 and i not in full.anchors]
    clearance = DEFAULT_LEVELS[1].clearance
    for i, p in enumerate(lvl1):
        for q in lvl1[i + 1:]:
            assert math.hypot(p[0] - q[0], p[1] - q[1]) >= clearance - 1e-6


def test_boundary_polygon_respected():
    tri = [(500.0, 50.0), (950.0, 950.0), (50.0, 950.0)]
    net = generate_roadnet(42, 1000, 1000, boundary=tri)
    assert len(net.points) > 10
    for p in net.points:
        assert point_in_polygon(p, tri)


def test_avenue_edges_are_level_zero():
    net = _net(1234)
    assert 0 in set(net.edge_levels.values())
    for e, lvl in net.edge_levels.items():
        assert e in net.edges
        assert 0 <= lvl < len(DEFAULT_LEVELS)


# ---------------------------------------------------------------------------
# build_city_map contract


def _mock_city_compiled():
    return {
        "generated_from": "a coastal test city",
        "regions": {"regions": [
            {"name": "Downtown", "named_locations": [
                {"name": "City Hall", "category": "settlement",
                 "description": "Seat of power."}]},
            {"name": "Docklands", "named_locations": [
                {"name": "Fish Market", "category": "landmark",
                 "description": "Smelly."}]},
            {"name": "Old Town", "named_locations": []},
        ]},
    }


def _city(seed=1234, total_nodes=60):
    from wbworldgen.worldgen.generation.city_map import build_city_map
    key = ("city", seed, total_nodes)
    if key not in _CACHE:
        _CACHE[key] = build_city_map({
            "compiled_world": _mock_city_compiled(),
            "total_nodes": total_nodes,
            "seed": seed,
        })
    return _CACHE[key]


def test_city_map_shape():
    m = _city()
    assert set(m) >= {"nodes", "edges", "regions", "config", "roads"}
    assert m["generator_id"] == "city_roadnet"
    assert m["config"]["generator_id"] == "city_roadnet"
    assert m["config"]["seed"] == 1234
    assert m["config"]["map_width"] == 1000
    ids = {n["id"] for n in m["nodes"]}
    assert 30 <= len(ids) <= 60
    assert len(ids) == len(m["nodes"])  # unique ids
    for n in m["nodes"]:
        assert 0.0 <= n["x"] <= 1000.0
        assert 0.0 <= n["y"] <= 1000.0
        assert n["type"] in ("settlement", "landmark", "crossroads", "waypoint")
        assert 1 <= n["importance"] <= 10
        assert n["region"]
        assert n["city_kind"] in ("plaza", "venue", "corner")
    for e in m["edges"]:
        assert e["from"] in ids and e["to"] in ids
        assert e["distance"] > 0


def test_city_map_playable_graph_connected():
    m = _city()
    adj = {}
    for e in m["edges"]:
        adj.setdefault(e["from"], set()).add(e["to"])
        adj.setdefault(e["to"], set()).add(e["from"])
    start = m["nodes"][0]["id"]
    seen = set()
    stack = [start]
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        stack.extend(adj.get(u, set()) - seen)
    assert len(seen) == len(m["nodes"])


def test_city_map_regions():
    m = _city()
    ids = {n["id"] for n in m["nodes"]}
    names = {r["region_name"] for r in m["regions"]}
    assert names == {"Downtown", "Docklands", "Old Town"}
    by_id = {n["id"]: n for n in m["nodes"]}
    for r in m["regions"]:
        assert r["node_ids"]
        assert set(r["node_ids"]) <= ids
        center = by_id[r["center_node_id"]]
        assert center["type"] == "settlement"
        assert center["city_kind"] == "plaza"
    # every node belongs to exactly one region
    all_ids = [nid for r in m["regions"] for nid in r["node_ids"]]
    assert sorted(all_ids) == sorted(ids)


def test_city_map_roads():
    m = _city()
    ids = {n["id"] for n in m["nodes"]}
    assert len(m["roads"]) > len(m["edges"])  # full fabric, not just adjacency
    tiers = {r["tier"] for r in m["roads"]}
    assert tiers == {"avenue", "street"}
    for r in m["roads"]:
        assert r["from"] in ids and r["to"] in ids  # fog-of-war reveal ids
        assert len(r["path"]) >= 2


def test_city_map_named_location_binding():
    m = _city()
    named = {n["name"]: n for n in m["nodes"] if n["name"]}
    assert named["City Hall"]["type"] == "settlement"
    assert named["City Hall"]["region"] == "Downtown"
    assert named["City Hall"]["importance"] >= 8
    assert named["Fish Market"]["type"] == "landmark"
    assert named["Fish Market"]["region"] == "Docklands"
    assert named["Fish Market"]["importance"] >= 6
    assert named["Fish Market"]["city_kind"] != "plaza"


def test_city_map_deterministic():
    from wbworldgen.worldgen.generation.city_map import build_city_map
    spec = {"compiled_world": _mock_city_compiled(), "total_nodes": 60,
            "seed": 42}
    assert build_city_map(dict(spec)) == build_city_map(dict(spec))


def test_city_map_random_seed_when_unset():
    from wbworldgen.worldgen.generation.city_map import build_city_map
    m = build_city_map({"compiled_world": {}, "total_nodes": 40})
    assert m["config"]["seed"]
    # fallback district names when no regions are authored
    assert all(n["region"].startswith("District ") for n in m["nodes"])


def test_city_map_budget_clamped():
    m = _city(seed=9, total_nodes=500)
    assert len(m["nodes"]) <= 120


def test_registry_city_generator():
    from wbworldgen.worldgen.generation.registry import get_generator, list_generators
    spec = get_generator("city_roadnet")
    assert spec.build is not None
    assert spec.needs_llm_content is False
    listed = {g["id"]: g for g in list_generators()}
    assert listed["city_roadnet"]["implemented"] is True
