import math
from collections import deque

import pytest

from wbworldgen.world_map import (
    MapNode,
    MapRegion,
    WorldMap,
    WorldMapGenerator,
    compass_direction,
    generate_map,
    generate_multilayer_map,
)


def _mock_compiled_world():
    return {
        "generated_from": "test world",
        "rules": {
            "genre": "fantasy",
            "tone": "grim",
            "magic_level": "rare",
            "tech_era": "iron age",
            "lethality": 7,
        },
        "lore": {
            "world_name": "TestWorld",
            "premise": "A test world.",
            "creation_myth": "Test.",
            "historical_eras": [],
            "central_conflict": "Test.",
        },
        "regions": {
            "regions": [
                {
                    "name": "Test Region A",
                    "layer_id": "",
                    "terrain": "forest",
                    "climate": "temperate",
                    "description": "A test forest.",
                    "landmarks": ["Old Oak", "Misty Lake"],
                    "factions": ["Woodwardens"],
                },
                {
                    "name": "Test Region B",
                    "layer_id": "",
                    "terrain": "mountains",
                    "climate": "cold",
                    "description": "A test mountain.",
                    "landmarks": ["Frostpeak"],
                    "factions": ["Stoneguard"],
                },
                {
                    "name": "Test Region C",
                    "layer_id": "",
                    "terrain": "desert",
                    "climate": "hot",
                    "description": "A test desert.",
                    "landmarks": [],
                    "factions": [],
                },
            ]
        },
    }


def _bfs_component_count(nodes, edges):
    """Return number of connected components in a node+edge graph."""
    node_index = {n.id: i for i, n in enumerate(nodes)}
    n = len(nodes)
    adj = [[] for _ in range(n)]
    for e in edges:
        a = node_index[e["from"]]
        b = node_index[e["to"]]
        adj[a].append(b)
        adj[b].append(a)
    visited = [False] * n
    components = 0
    for start in range(n):
        if visited[start]:
            continue
        components += 1
        queue = deque([start])
        visited[start] = True
        while queue:
            v = queue.popleft()
            for nb in adj[v]:
                if not visited[nb]:
                    visited[nb] = True
                    queue.append(nb)
    return components


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_poisson_disc_point_count():
    gen = WorldMapGenerator(seed=42)
    for target in [10, 50, 100]:
        points = gen._poisson_disc_sampling(target, 1000.0, 1000.0)
        assert len(points) == target, f"Expected {target}, got {len(points)}"


def test_poisson_disc_minimum_distance():
    target = 80
    width = 1000.0
    height = 1000.0
    gen = WorldMapGenerator(seed=42)
    points = gen._poisson_disc_sampling(target, width, height)

    area = width * height
    r = math.sqrt(area / (target * 1.8))
    r = max(r, min(width, height) / (target * 0.4))

    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            d = math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])
            assert d >= r, (
                f"Points {i} and {j} too close: {d:.4f} < {r:.4f}"
            )


def test_build_edges_has_no_self_loops():
    gen = WorldMapGenerator(seed=42)
    points = gen._poisson_disc_sampling(50, 1000.0, 1000.0)
    nodes = [
        MapNode(id=f"n_{i:04d}", x=p[0], y=p[1])
        for i, p in enumerate(points)
    ]
    edges = gen._build_edges(nodes, 1000.0, 1000.0)
    for e in edges:
        assert e["from"] != e["to"], f"Self-loop on {e['from']}"


def test_ensure_connectivity():
    gen = WorldMapGenerator(seed=42)

    nodes = []
    for i in range(15):
        nodes.append(MapNode(id=f"n_{i:04d}", x=100.0, y=50.0 + i * 15.0))

    edges = []
    for i in range(15):
        for j in range(i + 1, 15):
            d = math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y)
            edges.append(
                {"from": nodes[i].id, "to": nodes[j].id, "distance": round(d, 2)}
            )

    for i in range(15, 30):
        nodes.append(MapNode(id=f"n_{i:04d}", x=900.0, y=50.0 + (i - 15) * 15.0))

    for i in range(15, 30):
        for j in range(i + 1, 30):
            d = math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y)
            edges.append(
                {"from": nodes[i].id, "to": nodes[j].id, "distance": round(d, 2)}
            )

    assert _bfs_component_count(nodes, edges) == 2

    gen._ensure_connectivity(nodes, edges)

    assert _bfs_component_count(nodes, edges) == 1


def test_importance_scoring_range():
    comp = _mock_compiled_world()
    gen = WorldMapGenerator(seed=42)
    wm = gen.generate(comp, total_nodes=60)
    for node in wm.nodes:
        assert 1 <= node.importance <= 10, (
            f"Node {node.id} importance {node.importance} out of range"
        )


def test_importance_distribution():
    comp = _mock_compiled_world()
    gen = WorldMapGenerator(seed=42)
    wm = gen.generate(comp, total_nodes=80)
    high_nodes = [n for n in wm.nodes if n.importance >= 8]
    assert len(high_nodes) > 0, "No nodes with importance >= 8"


def test_assign_types_has_settlement():
    comp = _mock_compiled_world()
    gen = WorldMapGenerator(seed=42)
    wm = gen.generate(comp, total_nodes=80)
    settlements = [n for n in wm.nodes if n.type == "settlement"]
    assert len(settlements) > 0, "No settlement nodes found"


def test_region_assignment_coverage():
    comp = _mock_compiled_world()
    gen = WorldMapGenerator(seed=42)
    wm = gen.generate(comp, total_nodes=60)
    unassigned = [n.id for n in wm.nodes if not n.region]
    assert len(unassigned) == 0, (
        f"{len(unassigned)} nodes without region: {unassigned[:5]}"
    )


def test_region_center_is_settlement():
    comp = _mock_compiled_world()
    gen = WorldMapGenerator(seed=42)
    wm = gen.generate(comp, total_nodes=60)
    for region in wm.regions:
        if region.center_node_id:
            center = wm.get_node(region.center_node_id)
            assert center is not None
            assert center.type == "settlement", (
                f"Center {region.center_node_id} is {center.type}, expected settlement"
            )


def test_compass_direction_basic():
    origin = (500.0, 500.0)
    assert compass_direction(*origin, 500.0, 400.0) == "N"
    assert compass_direction(*origin, 500.0, 600.0) == "S"
    assert compass_direction(*origin, 600.0, 500.0) == "E"
    assert compass_direction(*origin, 400.0, 500.0) == "W"
    assert compass_direction(*origin, 600.0, 400.0) == "NE"
    assert compass_direction(*origin, 600.0, 600.0) == "SE"
    assert compass_direction(*origin, 400.0, 600.0) == "SW"
    assert compass_direction(*origin, 400.0, 400.0) == "NW"


def test_generate_map_returns_worldmap():
    comp = _mock_compiled_world()
    wm = generate_map(comp, total_nodes=30, seed=42)
    assert isinstance(wm, WorldMap), f"Expected WorldMap, got {type(wm)}"
    assert len(wm.nodes) == 30
    assert len(wm.edges) > 0
    assert len(wm.regions) > 0


def test_generate_map_with_custom_seed_produces_different_map():
    comp = _mock_compiled_world()
    wm1 = generate_map(comp, total_nodes=50, seed=100)
    wm2 = generate_map(comp, total_nodes=50, seed=200)

    pos1 = {(n.x, n.y) for n in wm1.nodes}
    pos2 = {(n.x, n.y) for n in wm2.nodes}

    assert pos1 != pos2, "Maps with different seeds produced identical node positions"


def test_generate_multilayer_map_layers():
    comp = _mock_compiled_world()
    layer_specs = [
        {"name": "Surface", "layer_type": "surface", "index": 0},
        {"name": "Underdark", "layer_type": "cave", "index": 1},
    ]
    connections_spec = [
        {
            "from_layer": "surface",
            "to_layer": "underdark",
            "connection_type": "cave_entrance",
            "count_hint": 2,
        }
    ]
    result = generate_multilayer_map(
        comp, layer_specs, connections_spec, total_nodes=50, seed=42
    )

    assert "layers" in result
    assert "connections" in result
    assert "config" in result
    assert len(result["layers"]) == 2

    for layer in result["layers"]:
        assert "layer_id" in layer
        assert "map" in layer
        assert "nodes" in layer["map"]
        assert "edges" in layer["map"]
        assert len(layer["map"]["nodes"]) > 0


def test_betweenness_scaling():
    """Betweenness works and scales for different node counts."""
    for node_count in [20, 60, 120]:
        gen = WorldMapGenerator(seed=42)
        points = gen._poisson_disc_sampling(node_count, 1000.0, 1000.0)
        nodes = [
            MapNode(id=f"n_{i:04d}", x=p[0], y=p[1])
            for i, p in enumerate(points)
        ]
        edges = gen._build_edges(nodes, 1000.0, 1000.0)

        node_index = {n.id: i for i, n in enumerate(nodes)}
        betweenness = gen._approx_betweenness(nodes, edges, node_index)

        assert len(betweenness) == node_count
        assert any(b > 0 for b in betweenness), (
            f"All betweenness zero for {node_count} nodes"
        )


def test_not_enough_regions_for_all_centers():
    """_assign_regions handles case where num_regions > available spread-out candidates."""
    comp = _mock_compiled_world()
    # Use compact map where nodes are close together, making spread-out
    # candidates scarce relative to the number of regions.
    gen = WorldMapGenerator(seed=42)
    wm = gen.generate(comp, total_nodes=50, map_width=500.0, map_height=500.0)

    assert len(wm.regions) > 0

    assigned_nodes = [n for n in wm.nodes if n.region]
    assert len(assigned_nodes) == len(wm.nodes), (
        f"{len(wm.nodes) - len(assigned_nodes)} nodes unassigned"
    )

    region_names = set(n.region for n in wm.nodes)
    assert len(region_names) <= len(
        comp["regions"]["regions"]
    ), "More regions assigned than exist in data"


def test_region_list_matches_node_regions():
    """_build_region_list produces regions that correctly aggregate node regions."""
    comp = _mock_compiled_world()
    gen = WorldMapGenerator(seed=42)
    wm = gen.generate(comp, total_nodes=60)

    region_names_in_data = {
        r["name"] for r in comp["regions"]["regions"]
    }
    for mr in wm.regions:
        assert mr.region_name in region_names_in_data
        for nid in mr.node_ids:
            node = wm.get_node(nid)
            assert node is not None
            assert node.region == mr.region_name, (
                f"Node {nid} region '{node.region}' != MapRegion '{mr.region_name}'"
            )
        if mr.center_node_id:
            center = wm.get_node(mr.center_node_id)
            assert center is not None
            assert center.type == "settlement"


def test_select_interlayer_nodes_respects_count():
    gen = WorldMapGenerator(seed=42)
    comp = _mock_compiled_world()
    wm = gen.generate(comp, total_nodes=60)
    for count in [0, 1, 3, 10]:
        selected = gen._select_interlayer_nodes(
            wm.nodes, wm.edges, count, "test_prefix"
        )
        if count == 0:
            assert selected == []
        else:
            assert len(selected) <= count, (
                f"Selected {len(selected)} > requested {count}"
            )
            assert len(set(selected)) == len(selected), "Duplicate node IDs returned"


def test_build_edges_bruteforce_has_no_self_loops():
    gen = WorldMapGenerator(seed=42)
    points = gen._poisson_disc_sampling(50, 1000.0, 1000.0)
    nodes = [
        MapNode(id=f"n_{i:04d}", x=p[0], y=p[1])
        for i, p in enumerate(points)
    ]
    edges = gen._build_edges_bruteforce(nodes, 1000.0, 1000.0)
    for e in edges:
        assert e["from"] != e["to"], f"Self-loop on {e['from']}"


def test_generate_map_with_invalid_node_count_raises():
    comp = _mock_compiled_world()
    gen = WorldMapGenerator(seed=42)
    with pytest.raises(ValueError, match="at least 3"):
        gen.generate(comp, total_nodes=2)
    with pytest.raises(ValueError, match="at least 3"):
        gen.generate(comp, total_nodes=1)


def test_empty_region_data_does_not_assign_regions():
    comp = _mock_compiled_world()
    comp["regions"]["regions"] = []
    gen = WorldMapGenerator(seed=42)
    wm = gen.generate(comp, total_nodes=40)
    assert len(wm.regions) == 0
    assert all(not n.region for n in wm.nodes), (
        "No regions should be assigned when region_data is empty"
    )


def test_worldmap_get_node_and_neighbors():
    comp = _mock_compiled_world()
    wm = generate_map(comp, total_nodes=30, seed=42)

    first = wm.nodes[0]
    fetched = wm.get_node(first.id)
    assert fetched is not None
    assert fetched.id == first.id
    assert fetched.x == first.x

    neighbors = wm.get_neighbors(first.id)
    adjacent = sum(
        1
        for e in wm.edges
        if e["from"] == first.id or e["to"] == first.id
    )
    assert len(neighbors) == adjacent

    assert wm.get_node("nonexistent") is None
    assert wm.get_neighbors("nonexistent") == []
