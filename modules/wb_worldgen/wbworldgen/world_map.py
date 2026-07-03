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
    roads: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": self.edges,
            "regions": [r.to_dict() for r in self.regions],
            "config": self.config,
        }
        if self.roads:
            d["roads"] = self.roads
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
        terrain: dict = None,
    ) -> WorldMap:
        if total_nodes < 3:
            raise ValueError("total_nodes must be at least 3")

        # Terrain-aware placement: when raster terrain is supplied for this
        # layer, anchor authored settlements/landmarks on fitting cells and fill
        # the rest by suitability, then run roads over the terrain cost field.
        if terrain:
            return self._generate_with_terrain(
                compiled_world, total_nodes, map_width, map_height, id_prefix, terrain
            )

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

    def _generate_with_terrain(
        self,
        compiled_world: dict,
        total_nodes: int,
        map_width: float,
        map_height: float,
        id_prefix: str,
        terrain: dict,
    ) -> WorldMap:
        """Terrain-aware node placement + road network.

        Authored settlements land on city/port-suitable cells, authored
        landmarks on cells matching their ``environment`` tag, and the remaining
        budget fills as wilderness/waypoint nodes by general suitability. Edges
        and importance reuse the abstract graph helpers; roads are least-cost
        terrain paths between settlements.
        """
        from wbworldgen.worldgen import terrain_placement as _tp
        from wbworldgen.worldgen import roads as _roads

        res = int(terrain["height"].shape[0])
        fields = _tp.suitability_fields(terrain)
        region_data = compiled_world.get("regions", {}).get("regions", [])

        nodes: list[MapNode] = []
        taken_cells: list[tuple] = []
        counter = 0

        def _new_node(x, y, **kw) -> MapNode:
            nonlocal counter
            n = MapNode(id=f"{id_prefix}n_{counter:04d}", x=x, y=y, **kw)
            counter += 1
            nodes.append(n)
            return n

        # 1) Anchor authored settlements + landmarks on fitting terrain.
        for region in region_data:
            rname = region.get("name", "")
            for loc in region.get("named_locations", []):
                cat = loc.get("category")
                if cat == "settlement":
                    pt = _tp.sample_points(
                        fields["city"], 1, res, map_width, map_height, self.np_rng,
                        min_sep_cells=max(3.0, res * 0.05), taken=taken_cells)
                    node_type, imp = "settlement", 8
                elif cat == "landmark":
                    pt = [_tp.place_landmark(
                        loc.get("environment", ""), terrain, fields, res,
                        map_width, map_height, self.np_rng, taken_cells)]
                    pt = [p for p in pt if p]
                    node_type, imp = "landmark", 6
                else:
                    continue
                if not pt:
                    continue
                x, y, r, c = pt[0]
                taken_cells.append((r, c))
                _new_node(x, y, name=loc.get("name", ""),
                          description=loc.get("description", "") or "",
                          type=node_type, importance=imp, region=rname,
                          layer_id=id_prefix.rstrip("_"))

        # 2) Fill the remaining budget with general-suitability wilderness nodes.
        remaining = max(0, total_nodes - len(nodes))
        if remaining:
            general = self._general_field(fields)
            fillers = _tp.sample_points(
                general, remaining, res, map_width, map_height, self.np_rng,
                min_sep_cells=max(2.0, res * 0.025), taken=taken_cells)
            for x, y, r, c in fillers:
                taken_cells.append((r, c))
                _new_node(x, y, type="wilderness",
                          layer_id=id_prefix.rstrip("_"))

        if len(nodes) < 3:
            # Degenerate terrain (tiny landmass): fall back to abstract placement.
            return self.generate(compiled_world, total_nodes, map_width,
                                 map_height, id_prefix, terrain=None)

        # 3) Edges + importance via the existing graph helpers.
        edges = self._build_edges(nodes, map_width, map_height)
        self._assign_importance(nodes, edges)
        # Restore authored importance/types the graph pass may have lowered.
        for n in nodes:
            if n.type == "settlement":
                n.importance = max(n.importance, 8)
            elif n.type == "landmark":
                n.importance = max(n.importance, 6)
        self._terrain_assign_types(nodes, edges, terrain, map_width, map_height)
        self._assign_filler_regions(nodes, edges, region_data)

        # 4) Roads: least-cost terrain paths between settlement-class nodes.
        road_data = _roads.build_roads(nodes, terrain, map_width, map_height)

        world_map = WorldMap(
            config={
                "total_nodes": len(nodes),
                "map_width": map_width,
                "map_height": map_height,
                "generated_from": compiled_world.get("generated_from", ""),
                "has_terrain": True,
            }
        )
        world_map.nodes = nodes
        world_map.edges = edges
        world_map.regions = self._build_region_list(nodes, region_data)
        world_map.roads = road_data
        return world_map

    @staticmethod
    def _general_field(fields: dict):
        """A broad habitability field for filler nodes: favors decent land
        everywhere (so the map isn't empty) without dropping nodes in water."""
        import numpy as np
        city = fields["city"]
        # Lift the floor on positive-land cells so wilderness can spread, keep
        # water forbidden (city is -1e9 on water).
        out = np.where(city > -1e8, np.maximum(city, 0.05), -1e9)
        return out

    def _terrain_assign_types(self, nodes, edges, terrain, map_width, map_height):
        """Refine node types using terrain: coastal settlements become ports,
        high-ground settlements become strongholds."""
        from wbworldgen.worldgen import terrain_store as _ts
        res = int(terrain["height"].shape[0])
        water = terrain.get("water")
        height = terrain["height"]
        sea = float(terrain.get("sea_level", 0.4))
        import numpy as np
        land_h = height[~np.asarray(water).astype(bool)] if water is not None else height.ravel()
        hi_ref = float(np.quantile(land_h, 0.75)) if land_h.size else sea + 0.3
        water_mask = np.asarray(water).astype(bool) if water is not None else None
        for n in nodes:
            if n.type != "settlement":
                continue
            r, c = _ts.cell_at(n.x, n.y, res, map_width, map_height)
            if water_mask is not None and _ts._within(water_mask, r, c, 2):
                n.type = "port"
            elif float(height[r, c]) >= hi_ref:
                n.type = "stronghold"

    def _assign_filler_regions(self, nodes, edges, region_data):
        """Grow regions outward from authored anchors over the Delaunay
        adjacency (claim-once multi-source BFS), so each region forms a
        contiguous band rather than a scatter of nearest-anchor islands, then
        enforce single-component contiguity."""
        anchor_idx = [i for i, n in enumerate(nodes) if n.region]
        if not anchor_idx:
            # No authored anchors: leave regions empty (handled downstream).
            return

        n = len(nodes)
        node_index = {nd.id: i for i, nd in enumerate(nodes)}
        adj: list[list[int]] = [[] for _ in range(n)]
        for e in edges:
            a = node_index[e["from"]]
            b = node_index[e["to"]]
            adj[a].append(b)
            adj[b].append(a)

        region_of = [nd.region for nd in nodes]  # anchors set, "" otherwise
        queue = deque(anchor_idx)
        while queue:
            v = queue.popleft()
            for nb in adj[v]:
                if not region_of[nb]:
                    region_of[nb] = region_of[v]
                    queue.append(nb)

        # Disconnected leftovers (no edge path to any anchor): nearest anchor.
        for i in range(n):
            if not region_of[i]:
                nearest = min(
                    anchor_idx,
                    key=lambda a: (nodes[a].x - nodes[i].x) ** 2
                    + (nodes[a].y - nodes[i].y) ** 2,
                )
                region_of[i] = nodes[nearest].region

        for i in range(n):
            nodes[i].region = region_of[i]

        self._enforce_region_contiguity(nodes, edges)

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

        self._clean_edges(nodes, edges, width, height)
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

        self._clean_edges(nodes, edges, width, height)
        self._ensure_connectivity(nodes, edges)

        return edges

    def _clean_edges(
        self, nodes: list[MapNode], edges: list[dict], width: float, height: float
    ):
        """Untangle the raw graph so edges read like sensible travel routes.

        Two passes:
        1. Collinear-merge: if an edge runs almost straight through a third
           node, replace it with the two shorter legs via that node instead
           of leaving a route that "skips past" a settlement on the way.
        2. Crossing-removal: drop the longer of any two edges that cross
           each other in screen space (no shared endpoint), since a road
           crossing another road with no junction looks broken on the map.
        """
        if len(nodes) < 3 or not edges:
            return

        node_index = {n.id: i for i, n in enumerate(nodes)}
        spacing = math.sqrt((width * height) / max(len(nodes), 1))
        collinear_thresh = max(8.0, spacing * 0.25)

        def dist(i, j):
            return math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y)

        def point_segment_distance(a, b, c):
            ax, ay = nodes[a].x, nodes[a].y
            bx, by = nodes[b].x, nodes[b].y
            cx, cy = nodes[c].x, nodes[c].y
            dx, dy = bx - ax, by - ay
            seg_len2 = dx * dx + dy * dy
            if seg_len2 == 0:
                return math.hypot(cx - ax, cy - ay), 0.0
            t = ((cx - ax) * dx + (cy - ay) * dy) / seg_len2
            proj_x, proj_y = ax + t * dx, ay + t * dy
            return math.hypot(cx - proj_x, cy - proj_y), t

        # --- Pass 1: split edges that run almost through a third node ---
        edge_keys = {
            (min(node_index[e["from"]], node_index[e["to"]]),
             max(node_index[e["from"]], node_index[e["to"]]))
            for e in edges
        }
        new_edges: list[dict] = []
        for e in edges:
            a = node_index[e["from"]]
            b = node_index[e["to"]]
            edge_len = dist(a, b)
            best_c = None
            best_perp = collinear_thresh
            for c in range(len(nodes)):
                if c == a or c == b:
                    continue
                perp, t = point_segment_distance(a, b, c)
                if t <= 0.1 or t >= 0.9:
                    continue
                if perp >= best_perp:
                    continue
                if dist(a, c) >= edge_len or dist(c, b) >= edge_len:
                    continue
                best_perp = perp
                best_c = c

            if best_c is None:
                new_edges.append(e)
                continue

            for x, y in ((a, best_c), (best_c, b)):
                key = (min(x, y), max(x, y))
                if key in edge_keys:
                    continue
                edge_keys.add(key)
                new_edges.append({
                    "from": nodes[x].id,
                    "to": nodes[y].id,
                    "distance": round(dist(x, y), 2),
                })

        edges[:] = new_edges

        # --- Pass 2: drop the longer edge of any pair that crosses ---
        def segments_intersect(a, b, c, d) -> bool:
            if len({a, b, c, d}) < 4:
                return False

            def ccw(p, q, r):
                px, py = nodes[p].x, nodes[p].y
                qx, qy = nodes[q].x, nodes[q].y
                rx, ry = nodes[r].x, nodes[r].y
                return (ry - py) * (qx - px) - (qy - py) * (rx - px)

            d1, d2 = ccw(c, d, a), ccw(c, d, b)
            d3, d4 = ccw(a, b, c), ccw(a, b, d)
            return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))

        sorted_edges = sorted(
            (
                (node_index[e["from"]], node_index[e["to"]], e["distance"])
                for e in edges
            ),
            key=lambda t: t[2],
        )
        removed: set[tuple[int, int]] = set()
        for idx, (a, b, d_ab) in enumerate(sorted_edges):
            key_ab = (min(a, b), max(a, b))
            if key_ab in removed:
                continue
            for c, dd, d_cd in sorted_edges[idx + 1:]:
                key_cd = (min(c, dd), max(c, dd))
                if key_cd in removed:
                    continue
                if segments_intersect(a, b, c, dd):
                    removed.add(key_cd)

        if removed:
            edges[:] = [
                e for e in edges
                if (min(node_index[e["from"]], node_index[e["to"]]),
                    max(node_index[e["from"]], node_index[e["to"]])) not in removed
            ]

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

        # The edge graph is guaranteed connected (see _ensure_connectivity), so
        # the multi-source BFS above normally claims every node. Any stragglers
        # from a disconnected fragment attach to the dominant assigned neighbor
        # region, propagating outward so they stay contiguous instead of being
        # scattered by raw distance.
        leftover = list(unassigned)
        guard = 0
        while leftover and guard < n:
            still: list[int] = []
            progressed = False
            for i in leftover:
                counts: dict[int, int] = {}
                for nb in adj[i]:
                    a = assignment[nb]
                    if a >= 0:
                        counts[a] = counts.get(a, 0) + 1
                if counts:
                    assignment[i] = max(counts.items(), key=lambda kv: kv[1])[0]
                    progressed = True
                else:
                    still.append(i)
            leftover = still
            guard += 1
            if not progressed:
                break
        # Truly edge-less remainder: nearest center as a last resort.
        for i in leftover:
            assignment[i] = min(
                range(num_regions),
                key=lambda c: math.hypot(
                    nodes[i].x - nodes[centers[c]].x,
                    nodes[i].y - nodes[centers[c]].y,
                ),
            )

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

        self._enforce_region_contiguity(nodes, edges)

    def _enforce_region_contiguity(
        self, nodes: list[MapNode], edges: list[dict], max_passes: int = 8
    ):
        """Make every region a single connected component over the Delaunay
        adjacency (which is exactly when its Voronoi cells merge into one shape
        on the map). Stray filler fragments are relabeled to the dominant
        neighboring region, eroded from the outside in over a few passes.
        Authored anchor nodes (those carrying a name) are pinned and never
        moved, so authored placement is preserved."""
        n = len(nodes)
        if n == 0:
            return

        node_index = {nd.id: i for i, nd in enumerate(nodes)}
        adj: list[list[int]] = [[] for _ in range(n)]
        for e in edges:
            a = node_index[e["from"]]
            b = node_index[e["to"]]
            adj[a].append(b)
            adj[b].append(a)

        pinned = [bool(nodes[i].name) for i in range(n)]

        for _ in range(max_passes):
            by_region: dict[str, list[int]] = {}
            for i in range(n):
                if nodes[i].region:
                    by_region.setdefault(nodes[i].region, []).append(i)

            changed = False
            for region, members in by_region.items():
                member_set = set(members)
                seen: set[int] = set()
                components: list[list[int]] = []
                for s in members:
                    if s in seen:
                        continue
                    comp: list[int] = []
                    stack = [s]
                    seen.add(s)
                    while stack:
                        v = stack.pop()
                        comp.append(v)
                        for nb in adj[v]:
                            if nb in member_set and nb not in seen:
                                seen.add(nb)
                                stack.append(nb)
                    components.append(comp)

                if len(components) <= 1:
                    continue

                # Keep the largest component; relabel the rest to the dominant
                # adjacent (different) region.
                components.sort(key=len, reverse=True)
                for comp in components[1:]:
                    for v in comp:
                        if pinned[v]:
                            continue
                        counts: dict[str, int] = {}
                        for nb in adj[v]:
                            r = nodes[nb].region
                            if r and r != region:
                                counts[r] = counts.get(r, 0) + 1
                        if counts:
                            nodes[v].region = max(
                                counts.items(), key=lambda kv: kv[1]
                            )[0]
                            changed = True

            if not changed:
                break

    def _select_interlayer_nodes(
        self,
        nodes: list[MapNode],
        edges: list[dict],
        count: int,
        connection_id_prefix: str,
        placement: str = "edges",
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
            deg_norm = min(deg, 8) / 8
            if placement == "central":
                # Favor central, well-connected hub nodes.
                score = (1.0 - edge_dist) * 0.6 + deg_norm * 0.4
            elif placement in ("random", "scattered"):
                # Unbiased ordering; "scattered" additionally enforces spread below.
                score = self.rng.random()
            else:
                # "edges" (default): favor peripheral, low-degree nodes.
                score = edge_dist * 0.6 + (1.0 - deg_norm) * 0.4
            candidates.append((i, score, node))

        candidates.sort(key=lambda x: x[1], reverse=True)

        # "scattered" pushes picks far apart; others keep the historical near-zero
        # threshold (poisson spacing already prevents true overlap).
        min_sep = 0.5 * max_dist if placement == "scattered" else 0.08

        selected_ids = []
        selected_positions = []

        for i, score, node in candidates:
            if len(selected_ids) >= count:
                break
            too_close = False
            px, py = node.x, node.y
            for sx, sy in selected_positions:
                if math.hypot(px - sx, py - sy) < min_sep:
                    too_close = True
                    break
            if not too_close:
                selected_ids.append(node.id)
                selected_positions.append((node.x, node.y))

        # If the spread constraint blocked us from reaching `count`, relax it and
        # fill from the remaining best candidates so we never under-deliver.
        if len(selected_ids) < count:
            for i, score, node in candidates:
                if len(selected_ids) >= count:
                    break
                if node.id not in selected_ids:
                    selected_ids.append(node.id)
                    selected_positions.append((node.x, node.y))

        # Last resort: the eligible pool (unnamed, non-interlayer nodes) was too
        # small to satisfy `count` on its own. Connection points are mandatory —
        # relax the name/type exclusion and pull from every remaining node
        # (ranked by the same score) rather than silently under-delivering.
        if len(selected_ids) < count:
            fallback = []
            for i, node in enumerate(nodes):
                if node.id in selected_ids:
                    continue
                deg = degree_map.get(node.id, 0)
                edge_dist = math.hypot(node.x - cx, node.y - cy) / max_dist
                deg_norm = min(deg, 8) / 8
                if placement == "central":
                    score = (1.0 - edge_dist) * 0.6 + deg_norm * 0.4
                elif placement in ("random", "scattered"):
                    score = self.rng.random()
                else:
                    score = edge_dist * 0.6 + (1.0 - deg_norm) * 0.4
                fallback.append((score, node))
            fallback.sort(key=lambda x: x[0], reverse=True)
            for score, node in fallback:
                if len(selected_ids) >= count:
                    break
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
                    if node.type in ("settlement", "port", "stronghold", "city") and not mr.center_node_id:
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
    terrain: dict = None,
) -> WorldMap:
    gen = WorldMapGenerator(seed=seed)
    return gen.generate(compiled_world, total_nodes, map_width, map_height,
                        terrain=terrain)


def generate_multilayer_map(
    compiled_world: dict,
    layer_specs: list[dict],
    connections_spec: list[dict],
    total_nodes: int = 100,
    map_width: float = 1000.0,
    map_height: float = 1000.0,
    seed: int = None,
    connection_placement: str = "edges",
    terrain_by_layer: dict = None,
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
    all_roads_by_layer: dict[str, list[dict]] = {}
    terrain_by_layer = terrain_by_layer or {}

    for spec in layer_specs:
        lid = spec["layer_id"]
        layer_seed = (seed + int(spec["index"]) * 1000) if seed is not None else None
        layer_gen = WorldMapGenerator(seed=layer_seed)
        layer_compiled = dict(compiled_world)
        layer_compiled["regions"] = {"regions": regions_by_layer.get(lid, [])}
        wm = layer_gen.generate(
            layer_compiled, nodes_per_layer, map_width, map_height,
            id_prefix=f"{lid}_", terrain=terrain_by_layer.get(lid) or None
        )
        wm.layer_id = lid
        for node in wm.nodes:
            node.layer_id = lid
        all_nodes_by_layer[lid] = wm.nodes
        all_edges_by_layer[lid] = wm.edges
        all_regions_by_layer[lid] = wm.regions
        all_roads_by_layer[lid] = wm.roads

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
            from_nodes, from_edges, count_hint, f"il_{connection_id_counter}", connection_placement
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
            to_nodes, to_edges, count_hint, f"il_{connection_id_counter}", connection_placement
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
        wm_roads = all_roads_by_layer.get(lid, [])

        layer_map = {
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
        }
        if wm_roads:
            layer_map["roads"] = wm_roads

        final_layers.append({
            "layer_id": lid,
            "name": spec.get("name", lid),
            "description": spec.get("description", ""),
            "layer_type": spec.get("layer_type", "surface"),
            "index": spec.get("index", 0),
            "map": layer_map,
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
