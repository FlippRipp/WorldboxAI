import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

try:
    from scipy.spatial import Delaunay
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


@dataclass
class MapNode:
    id: str
    x: float
    y: float
    importance: int = 1
    name: str = ""
    description: str = ""
    label_description: str = ""
    type: str = "waypoint"
    layer_id: str = ""
    interlayer_connection_id: str = ""
    region: str = ""

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "importance": self.importance,
            "name": self.name,
            "description": self.description,
            "label_description": self.label_description,
            "type": self.type,
        }
        if self.layer_id:
            d["layer_id"] = self.layer_id
        if self.interlayer_connection_id:
            d["interlayer_connection_id"] = self.interlayer_connection_id
        if self.region:
            d["region"] = self.region
        return d


@dataclass
class MapRegion:
    region_name: str
    node_ids: list[str] = field(default_factory=list)
    center_node_id: str = ""

    def to_dict(self) -> dict:
        return {
            "region_name": self.region_name,
            "node_ids": self.node_ids,
            "center_node_id": self.center_node_id,
        }


@dataclass
class WorldLayer:
    layer_id: str
    name: str
    description: str = ""
    layer_type: str = "surface"
    index: int = 0
    map: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {
            "layer_id": self.layer_id,
            "name": self.name,
            "description": self.description,
            "layer_type": self.layer_type,
            "index": self.index,
        }
        if self.map is not None:
            d["map"] = self.map
        return d


@dataclass
class LayerConnection:
    id: str
    from_layer_id: str
    from_node_id: str
    to_layer_id: str
    to_node_id: str
    connection_type: str = "passage"
    name: str = ""
    description: str = ""
    bidirectional: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_layer_id": self.from_layer_id,
            "from_node_id": self.from_node_id,
            "to_layer_id": self.to_layer_id,
            "to_node_id": self.to_node_id,
            "connection_type": self.connection_type,
            "name": self.name,
            "description": self.description,
            "bidirectional": self.bidirectional,
        }


@dataclass
class WorldMap:
    nodes: list[MapNode] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    regions: list[MapRegion] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    layer_id: str = ""

    def to_dict(self) -> dict:
        d = {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": self.edges,
            "regions": [r.to_dict() for r in self.regions],
            "config": self.config,
        }
        if self.layer_id:
            d["layer_id"] = self.layer_id
        return d

    def get_node(self, node_id: str) -> Optional[MapNode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_neighbors(self, node_id: str) -> list[str]:
        neighbors = []
        for e in self.edges:
            if e["from"] == node_id:
                neighbors.append(e["to"])
            elif e["to"] == node_id:
                neighbors.append(e["from"])
        return neighbors


class WorldMapGenerator:
    """Generates an organic node-graph map from compiled world data."""

    def __init__(self, seed: int = None):
        if seed is None:
            seed = random.randint(0, 2**31 - 1)
        self._seed = seed
        self.rng = random.Random(seed)
        self.np_rng = np.random.RandomState(seed)

    def generate(
        self,
        compiled_world: dict,
        total_nodes: int = 100,
        map_width: float = 1000.0,
        map_height: float = 1000.0,
        id_prefix: str = "",
    ) -> WorldMap:
        if total_nodes < 3:
            raise ValueError("total_nodes must be at least 3")

        world_map = WorldMap(
            config={
                "total_nodes": total_nodes,
                "map_width": map_width,
                "map_height": map_height,
                "generated_from": compiled_world.get("generated_from", ""),
            }
        )

        region_data = compiled_world.get("regions", {}).get("regions", [])
        lore_data = compiled_world.get("lore", {})

        points = self._poisson_disc_sampling(total_nodes, map_width, map_height)
        nodes = [
            MapNode(id=f"{id_prefix}n_{i:04d}", x=p[0], y=p[1])
            for i, p in enumerate(points)
        ]

        edges = self._build_edges(nodes, map_width, map_height)
        self._assign_importance(nodes, edges)
        self._assign_types(nodes, edges)
        self._assign_regions(nodes, edges, region_data)

        world_map.nodes = nodes
        world_map.edges = edges
        world_map.regions = self._build_region_list(nodes, region_data)

        return world_map

    def _poisson_disc_sampling(
        self, target_count: int, width: float, height: float
    ) -> list[tuple[float, float]]:
        area = width * height
        r = math.sqrt(area / (target_count * 1.8))
        r = max(r, min(width, height) / (target_count * 0.4))

        cell_size = r / math.sqrt(2)
        cols = max(1, int(width / cell_size) + 1)
        rows = max(1, int(height / cell_size) + 1)
        grid: list[Optional[tuple[float, float]]] = [None] * (cols * rows)

        def grid_index(px: float, py: float) -> int:
            return int(py / cell_size) * cols + int(px / cell_size)

        def is_valid(px: float, py: float) -> bool:
            if px < 0 or px >= width or py < 0 or py >= height:
                return False
            gx = int(px / cell_size)
            gy = int(py / cell_size)
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < cols and 0 <= ny < rows:
                        idx = ny * cols + nx
                        candidate = grid[idx]
                        if candidate is not None:
                            dist = math.hypot(px - candidate[0], py - candidate[1])
                            if dist < r:
                                return False
            return True

        points: list[tuple[float, float]] = []
        active: list[tuple[float, float]] = []

        sx = self.rng.uniform(0, width)
        sy = self.rng.uniform(0, height)
        points.append((sx, sy))
        active.append((sx, sy))
        grid[grid_index(sx, sy)] = (sx, sy)

        k = 30

        while active and len(points) < target_count:
            idx = self.rng.randint(0, len(active) - 1)
            base = active[idx]
            found = False

            for _ in range(k):
                angle = self.rng.uniform(0, 2 * math.pi)
                dist = self.rng.uniform(r, 2 * r)
                nx = base[0] + math.cos(angle) * dist
                ny = base[1] + math.sin(angle) * dist

                if is_valid(nx, ny):
                    points.append((nx, ny))
                    active.append((nx, ny))
                    grid[grid_index(nx, ny)] = (nx, ny)
                    found = True
                    break

            if not found:
                active.pop(idx)

        return points

    def _build_edges(
        self, nodes: list[MapNode], width: float, height: float
    ) -> list[dict]:
        if not HAS_SCIPY:
            return self._build_edges_bruteforce(nodes, width, height)

        points_arr = np.array([[n.x, n.y] for n in nodes])
        tri = Delaunay(points_arr)

        edge_set: set[tuple[int, int]] = set()
        for simplex in tri.simplices:
            for i in range(3):
                a, b = simplex[i], simplex[(i + 1) % 3]
                edge_set.add((min(a, b), max(a, b)))

        max_dist = math.hypot(width, height) * 0.35
        edges: list[dict] = []
        for a, b in edge_set:
            dist = math.hypot(
                nodes[a].x - nodes[b].x, nodes[a].y - nodes[b].y
            )
            if dist <= max_dist:
                edges.append({
                    "from": nodes[a].id,
                    "to": nodes[b].id,
                    "distance": round(dist, 2),
                })

        self._ensure_connectivity(nodes, edges)

        return edges

    def _build_edges_bruteforce(
        self, nodes: list[MapNode], width: float, height: float
    ) -> list[dict]:
        k = min(6, len(nodes) - 1)
        edges: list[dict] = []
        edge_set: set[tuple[int, int]] = set()

        for i, n in enumerate(nodes):
            neighbors = []
            for j, other in enumerate(nodes):
                if i == j:
                    continue
                d = math.hypot(n.x - other.x, n.y - other.y)
                neighbors.append((j, d))
            neighbors.sort(key=lambda x: x[1])
            for j, d in neighbors[:k]:
                key = (min(i, j), max(i, j))
                if key not in edge_set:
                    edge_set.add(key)
                    edges.append({
                        "from": nodes[i].id,
                        "to": nodes[j].id,
                        "distance": round(d, 2),
                    })

        self._ensure_connectivity(nodes, edges)

        return edges

    def _ensure_connectivity(
        self, nodes: list[MapNode], edges: list[dict]
    ):
        node_index: dict[str, int] = {n.id: i for i, n in enumerate(nodes)}
        n = len(nodes)

        adj: list[list[int]] = [[] for _ in range(n)]
        for e in edges:
            a = node_index[e["from"]]
            b = node_index[e["to"]]
            adj[a].append(b)
            adj[b].append(a)

        visited = [False] * n
        components: list[list[int]] = []

        for start in range(n):
            if visited[start]:
                continue
            comp: list[int] = []
            queue = deque([start])
            visited[start] = True
            while queue:
                v = queue.popleft()
                comp.append(v)
                for nb in adj[v]:
                    if not visited[nb]:
                        visited[nb] = True
                        queue.append(nb)
            components.append(comp)

        if len(components) <= 1:
            return

        existing_edge_set = set()
        for e in edges:
            existing_edge_set.add((node_index[e["from"]], node_index[e["to"]]))
            existing_edge_set.add((node_index[e["to"]], node_index[e["from"]]))

        component_ids = [-1] * n
        for cid, comp in enumerate(components):
            for v in comp:
                component_ids[v] = cid

        for cid in range(1, len(components)):
            best_dist = float("inf")
            best_pair = None
            for a in components[0]:
                for b in components[cid]:
                    key = (min(a, b), max(a, b))
                    if key in existing_edge_set:
                        continue
                    d = math.hypot(
                        nodes[a].x - nodes[b].x, nodes[a].y - nodes[b].y
                    )
                    if d < best_dist:
                        best_dist = d
                        best_pair = (a, b)

            if best_pair:
                a, b = best_pair
                edges.append({
                    "from": nodes[a].id,
                    "to": nodes[b].id,
                    "distance": round(best_dist, 2),
                })
                for v in components[cid]:
                    components[0].append(v)
                existing_edge_set.add((a, b))
                existing_edge_set.add((b, a))

    def _assign_importance(self, nodes: list[MapNode], edges: list[dict]):
        node_index: dict[str, int] = {n.id: i for i, n in enumerate(nodes)}
        n = len(nodes)

        degree = [0] * n
        for e in edges:
            a = node_index[e["from"]]
            b = node_index[e["to"]]
            degree[a] += 1
            degree[b] += 1

        max_degree = max(degree) if degree else 1

        betweenness = self._approx_betweenness(nodes, edges, node_index)
        max_betweenness = max(betweenness) if max(betweenness) > 0 else 1

        importance_raw = []
        for i in range(n):
            d_score = degree[i] / max(max_degree, 1)
            b_score = betweenness[i] / max_betweenness
            combined = 0.5 * d_score + 0.5 * b_score
            rand = self.rng.uniform(-0.15, 0.15)
            importance_raw.append(max(0.0, min(1.0, combined + rand)))

        indices_sorted = sorted(range(n), key=lambda i: importance_raw[i], reverse=True)

        importance = [0] * n
        bin_size = max(1, n // 10)
        for rank, idx in enumerate(indices_sorted):
            importance[idx] = min(10, 10 - (rank // bin_size))

        for rank, idx in enumerate(indices_sorted):
            importance[idx] = max(1, importance[idx])

        for i, node in enumerate(nodes):
            node.importance = importance[i]

    def _approx_betweenness(
        self,
        nodes: list[MapNode],
        edges: list[dict],
        node_index: dict[str, int],
    ) -> list[float]:
        n = len(nodes)
        betweenness = [0.0] * n

        adj: list[list[int]] = [[] for _ in range(n)]
        for e in edges:
            a = node_index[e["from"]]
            b = node_index[e["to"]]
            adj[a].append(b)
            adj[b].append(a)

        sample_size = min(50, max(20, n // 3))
        sample_nodes = self.rng.sample(range(n), sample_size)

        for s in sample_nodes:
            stack: list[int] = []
            pred: list[list[int]] = [[] for _ in range(n)]
            sigma = [0] * n
            sigma[s] = 1
            dist = [-1] * n
            dist[s] = 0
            queue = deque([s])

            while queue:
                v = queue.popleft()
                stack.append(v)
                for w in adj[v]:
                    if dist[w] < 0:
                        dist[w] = dist[v] + 1
                        queue.append(w)
                    if dist[w] == dist[v] + 1:
                        sigma[w] += sigma[v]
                        pred[w].append(v)

            delta = [0.0] * n
            while stack:
                w = stack.pop()
                for v in pred[w]:
                    delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
                if w != s:
                    betweenness[w] += delta[w]

        return betweenness

    INTERLAYER_TYPES = {
        "dungeon_entrance", "port", "portal", "cave_entrance",
        "cave_mouth", "rift", "staircase", "bridge",
    }

    def _assign_types(self, nodes: list[MapNode], edges: list[dict]):
        degree_map: dict[str, int] = {}
        for e in edges:
            degree_map[e["from"]] = degree_map.get(e["from"], 0) + 1
            degree_map[e["to"]] = degree_map.get(e["to"], 0) + 1

        for node in nodes:
            if node.type in self.INTERLAYER_TYPES:
                continue
            deg = degree_map.get(node.id, 0)
            if node.importance >= 8:
                node.type = "settlement" if deg >= 4 else "landmark"
            elif node.importance >= 5:
                node.type = "landmark" if deg >= 3 else "waypoint"
            elif deg >= 4:
                node.type = "crossroads"
            else:
                node.type = "wilderness"

    def _assign_regions(
        self,
        nodes: list[MapNode],
        edges: list[dict],
        region_data: list[dict],
    ):
        if not region_data:
            return

        node_index: dict[str, int] = {n.id: i for i, n in enumerate(nodes)}
        n = len(nodes)
        num_regions = len(region_data)

        adj: list[list[int]] = [[] for _ in range(n)]
        for e in edges:
            a = node_index[e["from"]]
            b = node_index[e["to"]]
            adj[a].append(b)
            adj[b].append(a)

        # Compute a relative spacing threshold from the actual node spread
        x_vals = [n.x for n in nodes]
        y_vals = [n.y for n in nodes]
        map_spread = max(max(x_vals) - min(x_vals), max(y_vals) - min(y_vals), 1.0)
        min_center_dist = 0.15 * map_spread

        # Pick center nodes: highest-importance nodes, well-spaced
        candidates = sorted(range(n), key=lambda i: nodes[i].importance, reverse=True)
        centers: list[int] = []
        for c in candidates:
            if len(centers) >= num_regions:
                break
            too_close = False
            for existing in centers:
                d = math.hypot(
                    nodes[c].x - nodes[existing].x,
                    nodes[c].y - nodes[existing].y,
                )
                if d < min_center_dist:
                    too_close = True
                    break
            if not too_close:
                centers.append(c)

        while len(centers) < num_regions:
            remaining = [c for c in candidates if c not in centers]
            if not remaining:
                break
            centers.append(remaining[0])

        assignment = [-1] * n
        for i, c in enumerate(centers):
            assignment[c] = i

        if not centers:
            return

        # BFS-based region expansion
        region_queues: list[deque] = [deque([c]) for c in centers]
        unassigned = set(range(n)) - set(centers)

        while unassigned and any(q for q in region_queues):
            for i, q in enumerate(region_queues):
                if not q:
                    continue
                to_add: list[int] = []
                expanded = False
                qsize = len(q)
                for _ in range(qsize):
                    if not q:
                        break
                    v = q.popleft()
                    for nb in adj[v]:
                        if nb in unassigned:
                            assignment[nb] = i
                            to_add.append(nb)
                            unassigned.discard(nb)
                            expanded = True
                for nb in to_add:
                    q.append(nb)
                if expanded and to_add:
                    pass

        for i in unassigned:
            best_center = min(
                range(num_regions),
                key=lambda c: math.hypot(
                    nodes[i].x - nodes[centers[c]].x,
                    nodes[i].y - nodes[centers[c]].y,
                ),
            )
            assignment[i] = best_center

        # Populate nodes with region data
        for region_idx, region in enumerate(region_data):
            region_nodes = [i for i, a in enumerate(assignment) if a == region_idx]
            if not region_nodes:
                continue

            region_nodes.sort(key=lambda i: nodes[i].importance, reverse=True)

            region_name = region.get("name", f"Region {region_idx}")
            for idx in region_nodes:
                nodes[idx].region = region_name

            # Authored, named entities (with descriptions) to bind onto nodes.
            named_locations = region.get("named_locations", [])
            settlements = [n for n in named_locations if n.get("category") == "settlement"]
            landmark_locs = [n for n in named_locations if n.get("category") == "landmark"]

            # ``slot`` walks region_nodes in importance order, filling settlements
            # first (most important nodes) then landmarks onto distinct nodes.
            slot = 0

            def _bind(loc: dict, node_type: str, min_importance: int) -> bool:
                nonlocal slot
                if slot >= len(region_nodes):
                    return False
                node = nodes[region_nodes[slot]]
                node.name = loc.get("name", "") or node.name
                if loc.get("description"):
                    node.description = loc["description"]
                node.type = node_type
                node.importance = max(node.importance, min_importance)
                slot += 1
                return True

            if settlements:
                for loc in settlements:
                    if not _bind(loc, "settlement", 8):
                        break
            else:
                # No authored settlements: keep a capital so every region still
                # has a settlement center node.
                capital = region_nodes[0]
                nodes[capital].type = "settlement"
                nodes[capital].importance = max(nodes[capital].importance, 9)
                slot = 1

            for loc in landmark_locs:
                if not _bind(loc, "landmark", 6):
                    break

    def _select_interlayer_nodes(
        self,
        nodes: list[MapNode],
        edges: list[dict],
        count: int,
        connection_id_prefix: str,
    ) -> list[str]:
        if count <= 0 or not nodes:
            return []

        degree_map: dict[str, int] = {}
        for e in edges:
            degree_map[e["from"]] = degree_map.get(e["from"], 0) + 1
            degree_map[e["to"]] = degree_map.get(e["to"], 0) + 1

        cx = sum(n.x for n in nodes) / len(nodes)
        cy = sum(n.y for n in nodes) / len(nodes)
        max_dist = max(math.hypot(n.x - cx, n.y - cy) for n in nodes) or 1

        candidates = []
        for i, node in enumerate(nodes):
            if node.name or node.type in self.INTERLAYER_TYPES:
                continue
            deg = degree_map.get(node.id, 0)
            edge_dist = math.hypot(node.x - cx, node.y - cy) / max_dist
            score = edge_dist * 0.6 + (1.0 - min(deg, 8) / 8) * 0.4
            candidates.append((i, score, node))

        candidates.sort(key=lambda x: x[1], reverse=True)

        selected_ids = []
        selected_positions = []

        for i, score, node in candidates:
            if len(selected_ids) >= count:
                break
            too_close = False
            px, py = node.x, node.y
            for sx, sy in selected_positions:
                if math.hypot(px - sx, py - sy) < 0.08:
                    too_close = True
                    break
            if not too_close:
                selected_ids.append(node.id)
                selected_positions.append((node.x, node.y))

        return selected_ids

    def _assign_interlayer_nodes(
        self,
        nodes: list[MapNode],
        connection_type: str,
        node_ids: list[str],
        layer_name: str,
        description: str,
    ):
        for node in nodes:
            if node.id in node_ids:
                node.type = connection_type
                node.importance = max(node.importance, 4)

    def _build_region_list(
        self, nodes: list[MapNode], region_data: list[dict]
    ) -> list[MapRegion]:
        regions: list[MapRegion] = []
        for region in region_data:
            region_name = region.get("name", "")
            mr = MapRegion(region_name=region_name)
            # Collect nodes that belong to this region
            for node in nodes:
                if node.region == region_name:
                    if node.id not in mr.node_ids:
                        mr.node_ids.append(node.id)
                    if node.type == "settlement" and not mr.center_node_id:
                        mr.center_node_id = node.id
            if mr.node_ids:
                regions.append(mr)
        return regions


def generate_map(
    compiled_world: dict,
    total_nodes: int = 100,
    map_width: float = 1000.0,
    map_height: float = 1000.0,
    seed: int = None,
) -> WorldMap:
    gen = WorldMapGenerator(seed=seed)
    return gen.generate(compiled_world, total_nodes, map_width, map_height)


def generate_multilayer_map(
    compiled_world: dict,
    layer_specs: list[dict],
    connections_spec: list[dict],
    total_nodes: int = 100,
    map_width: float = 1000.0,
    map_height: float = 1000.0,
    seed: int = None,
) -> dict:
    gen = WorldMapGenerator(seed=seed)
    region_data = compiled_world.get("regions", {}).get("regions", [])

    layer_specs = [s for s in layer_specs if isinstance(s, dict)]
    if not layer_specs:
        return {"layers": [], "connections": [], "config": {"total_nodes": 0, "map_width": map_width, "map_height": map_height}}

    layers_by_id: dict[str, dict] = {}
    for spec in layer_specs:
        lid = spec.get("layer_id") or spec.get("name", f"layer_{len(layers_by_id)}").lower().replace(" ", "_")
        spec.setdefault("layer_id", lid)
        spec.setdefault("layer_type", "surface")
        spec.setdefault("description", "")
        spec.setdefault("index", len(layers_by_id))
        layers_by_id[spec["layer_id"]] = spec

    # Distribute authored regions to their layer. Regions whose layer_id is
    # empty/unknown fall back to the primary (lowest-index) layer so they are
    # never silently dropped or duplicated across every layer.
    primary_lid = min(layers_by_id.values(), key=lambda s: int(s.get("index", 0)))["layer_id"]
    regions_by_layer: dict[str, list[dict]] = {lid: [] for lid in layers_by_id}
    for region in region_data:
        rlayer = region.get("layer_id", "")
        target = rlayer if rlayer in regions_by_layer else primary_lid
        regions_by_layer[target].append(region)

    nodes_per_layer = max(20, total_nodes // max(1, len(layer_specs)))

    all_nodes_by_layer: dict[str, list[MapNode]] = {}
    all_edges_by_layer: dict[str, list[dict]] = {}
    all_regions_by_layer: dict[str, list[MapRegion]] = {}

    for spec in layer_specs:
        lid = spec["layer_id"]
        layer_seed = (seed + int(spec["index"]) * 1000) if seed is not None else None
        layer_gen = WorldMapGenerator(seed=layer_seed)
        layer_compiled = dict(compiled_world)
        layer_compiled["regions"] = {"regions": regions_by_layer.get(lid, [])}
        wm = layer_gen.generate(
            layer_compiled, nodes_per_layer, map_width, map_height, id_prefix=f"{lid}_"
        )
        wm.layer_id = lid
        for node in wm.nodes:
            node.layer_id = lid
        all_nodes_by_layer[lid] = wm.nodes
        all_edges_by_layer[lid] = wm.edges
        all_regions_by_layer[lid] = wm.regions

    connection_id_counter = 0
    layer_connections: list[dict] = []

    for conn_spec in connections_spec:
        from_layer = conn_spec.get("from_layer", "")
        to_layer = conn_spec.get("to_layer", "")
        conn_type = conn_spec.get("connection_type", "passage")
        desc = conn_spec.get("description", "")
        count_hint = max(1, int(conn_spec.get("count_hint", 2)))

        if from_layer not in all_nodes_by_layer or to_layer not in all_nodes_by_layer:
            continue

        from_nodes = all_nodes_by_layer[from_layer]
        from_edges = all_edges_by_layer[from_layer]
        to_name = layers_by_id.get(to_layer, {}).get("name", to_layer)

        from_selected = gen._select_interlayer_nodes(
            from_nodes, from_edges, count_hint, f"il_{connection_id_counter}"
        )
        gen._assign_interlayer_nodes(from_nodes, conn_type, from_selected, to_name, desc)

        to_nodes = all_nodes_by_layer[to_layer]
        to_edges = all_edges_by_layer[to_layer]
        from_name = layers_by_id.get(from_layer, {}).get("name", from_layer)

        to_conn_type = conn_type
        if conn_type == "dungeon_entrance":
            to_conn_type = "cave_entrance"
        elif conn_type == "cave_entrance":
            to_conn_type = "dungeon_entrance"
        elif conn_type == "port":
            to_conn_type = "port"

        to_selected = gen._select_interlayer_nodes(
            to_nodes, to_edges, min(count_hint, len(from_selected)), f"il_{connection_id_counter}"
        )
        gen._assign_interlayer_nodes(to_nodes, to_conn_type, to_selected, from_name, desc)

        for i in range(min(len(from_selected), len(to_selected))):
            lc = LayerConnection(
                id=f"lc_{connection_id_counter:04d}",
                from_layer_id=from_layer,
                from_node_id=from_selected[i],
                to_layer_id=to_layer,
                to_node_id=to_selected[i],
                connection_type=conn_type,
                name=f"{conn_type.replace('_', ' ').title()} #{i + 1}",
                description=desc,
                bidirectional=True,
            )
            layer_connections.append(lc.to_dict())
            connection_id_counter += 1

            fn = next((n for n in from_nodes if n.id == from_selected[i]), None)
            if fn:
                fn.interlayer_connection_id = lc.id
            tn = next((n for n in to_nodes if n.id == to_selected[i]), None)
            if tn:
                tn.interlayer_connection_id = lc.id

    final_layers = []
    for spec in layer_specs:
        lid = spec["layer_id"]
        wm_nodes = all_nodes_by_layer.get(lid, [])
        wm_edges = all_edges_by_layer.get(lid, [])
        wm_regions = all_regions_by_layer.get(lid, [])

        final_layers.append({
            "layer_id": lid,
            "name": spec.get("name", lid),
            "description": spec.get("description", ""),
            "layer_type": spec.get("layer_type", "surface"),
            "index": spec.get("index", 0),
            "map": {
                "nodes": [n.to_dict() for n in wm_nodes],
                "edges": wm_edges,
                "regions": [r.to_dict() for r in wm_regions],
                "config": {
                    "total_nodes": len(wm_nodes),
                    "map_width": map_width,
                    "map_height": map_height,
                    "generated_from": compiled_world.get("generated_from", ""),
                },
                "layer_id": lid,
            },
        })

    return {
        "layers": final_layers,
        "connections": layer_connections,
        "config": {
            "total_nodes": total_nodes,
            "map_width": map_width,
            "map_height": map_height,
            "generated_from": compiled_world.get("generated_from", ""),
        },
    }


COMPASS_DIRECTIONS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass_direction(from_x: float, from_y: float, to_x: float, to_y: float) -> str:
    angle = math.degrees(math.atan2(to_x - from_x, -(to_y - from_y)))
    if angle < 0:
        angle += 360
    idx = round(angle / 45) % 8
    return COMPASS_DIRECTIONS[idx]
