"""Shared map data model: the node/edge/region/layer types every part of the
module exchanges, plus the tiny geometry/normalization helpers that go with
them. Dependency-free by design (stdlib only) — generators, enrichment,
expansion, the compiler and the runtime all import from here, so this package
must never grow an import back into any of them.
"""

import math
from dataclasses import dataclass, field
from typing import Optional


def join_key(name) -> str:
    """Case/whitespace/article-tolerant key for matching authored names —
    the normalization every cross-step name join uses, so "The Halo Ring"
    anchors a location whose part_of says "Halo Ring". A leading "The " is
    dropped because independent LLM calls author each side of a join, and
    "The Neon Docks" vs "Neon Docks" mismatches are routine."""
    key = str(name or "").strip().lower()
    if key.startswith("the ") and len(key) > 4:
        key = key[4:]
    return key


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
    contained_locations: list = field(default_factory=list)

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
        if self.contained_locations:
            d["contained_locations"] = self.contained_locations
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


def grow_position(map_record: dict, anchors: list) -> tuple:
    """Deterministic spot for a grown node: one typical-edge-length step
    away from its anchors' centroid, in whichever direction keeps it
    farthest from the existing nodes (so it doesn't land on one)."""
    nodes = map_record.get("nodes") or []
    cfg = map_record.get("config") or {}
    width = float(cfg.get("map_width", 100.0) or 100.0)
    height = float(cfg.get("map_height", 100.0) or 100.0)
    if not anchors:
        return round(width / 2, 2), round(height / 2, 2)
    cx = sum(a.get("x", 0.0) for a in anchors) / len(anchors)
    cy = sum(a.get("y", 0.0) for a in anchors) / len(anchors)
    distances = [e.get("distance") for e in map_record.get("edges", []) if e.get("distance")]
    spacing = (sum(distances) / len(distances)) if distances else min(width, height) / 8
    best = (cx, cy)
    best_score = -1.0
    for i in range(12):
        angle = 2 * math.pi * i / 12
        x = min(width * 0.95, max(width * 0.05, cx + spacing * math.cos(angle)))
        y = min(height * 0.95, max(height * 0.05, cy + spacing * math.sin(angle)))
        score = min((math.hypot(x - n.get("x", 0.0), y - n.get("y", 0.0))
                     for n in nodes), default=spacing)
        if score > best_score:
            best_score = score
            best = (x, y)
    return round(best[0], 2), round(best[1], 2)


COMPASS_DIRECTIONS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass_direction(from_x: float, from_y: float, to_x: float, to_y: float) -> str:
    angle = math.degrees(math.atan2(to_x - from_x, -(to_y - from_y)))
    if angle < 0:
        angle += 360
    idx = round(angle / 45) % 8
    return COMPASS_DIRECTIONS[idx]
