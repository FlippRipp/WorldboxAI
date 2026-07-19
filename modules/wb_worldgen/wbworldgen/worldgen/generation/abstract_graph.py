"""Authored abstract root maps: a conceptual node graph, not a scatter.

Worlds whose design declares ``map_style: "abstract"`` (a solar system, a
dream web, a pocket plane) used to get the same Poisson-disc scatter as a
fantasy overworld — 50 anonymous filler nodes with authored places stamped
onto whichever were most important. The result never read as the designed
structure: no planets, venue-scale places as siblings of moons (the "Lustra
System" failure — see docs/design/worldgen_quality_fixes.md).

Abstract maps are now AUTHORED: one LLM call per layer designs the map's
actual structure at its own scale — every node a named, meaningful place,
every edge a real travel route — and this module turns that authored graph
into the standard per-map shape. It is the pure, deterministic half:

- ``normalize_abstract_graph``: clamp LLM output to the engine contract
  (server-side ids, deduped names, regions resolved against the authored
  areas, venue-scale places folded into ``contained_locations``, and a
  safety net guaranteeing no authored place is ever dropped).
- ``ensure_crossing_nodes``: the nodes that carry connections to a parallel
  plane, synthesized when the author under-delivered.
- ``layout_abstract_graph``: deterministic positions (region clusters on a
  ring, golden-spiral placement inside each, relaxation to a minimum
  spacing) plus graph connectivity repair.
- ``mock_abstract_parsed``: the offline stand-in for the LLM call, built
  from the authored areas and named locations, so mock worlds and tests
  exercise the exact same pipeline.

The async orchestration (prompts, per-plane calls, crossing pairing) lives
in ``enrichment/maps_expand.py``.
"""

import math

from wbworldgen.world_map import _join_key

#: Node-count bounds for one authored abstract map.
MAX_ROOT_NODES = 20
MAX_PLANE_NODES = 12


def _importance(raw, default=5) -> int:
    try:
        return max(1, min(10, int(raw)))
    except (TypeError, ValueError):
        return default


def _engine_type(importance: int) -> str:
    """Engine-facing node type (start-location preference, map styling) —
    the world's own noun lives in ``kind``."""
    if importance >= 8:
        return "settlement"
    if importance >= 5:
        return "landmark"
    return "waypoint"


def normalize_abstract_graph(parsed: dict, named_locations: list, areas: list,
                             id_prefix: str = "", max_nodes: int = MAX_ROOT_NODES) -> dict:
    """Clamp an authored abstract graph to the engine contract.

    Returns ``{"description", "nodes", "edges"}`` where nodes are
    MapNode-shaped dicts without positions (``layout_abstract_graph`` adds
    those). Ids are assigned server-side; names dedup (article-tolerant);
    ``region`` resolves against the authored area names or blanks; authored
    ``contains`` entries become ``contained_locations`` (with descriptions
    recovered from the named-location list); and every named location the
    author failed to place is folded into the best-fitting node's
    ``contained_locations`` — authored content is never dropped.
    """
    if not isinstance(parsed, dict):
        parsed = {}
    area_by_key = {}
    for a in areas or []:
        name = str((a or {}).get("name", "")).strip()
        if name:
            area_by_key.setdefault(_join_key(name), name)
    loc_by_key = {}
    for loc in named_locations or []:
        name = str((loc or {}).get("name", "")).strip()
        if name:
            loc_by_key.setdefault(_join_key(name), loc)

    layer_id = id_prefix.rstrip("_")
    nodes: list[dict] = []
    by_key: dict[str, dict] = {}
    raw_adjacent: dict[str, list] = {}
    raw_contains: dict[str, list] = {}
    placed: set[str] = set()

    for raw in parsed.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        key = _join_key(name)
        if not name or key in by_key:
            continue
        if len(nodes) >= max_nodes:
            break
        importance = _importance(raw.get("importance"))
        authored_loc = loc_by_key.get(key)
        if authored_loc is not None:
            placed.add(key)
            if authored_loc.get("category") == "settlement":
                importance = max(importance, 8)
        description = str(raw.get("description", "")).strip()
        if not description and authored_loc is not None:
            description = str(authored_loc.get("description", "")).strip()
        node = {
            "id": f"{id_prefix}n_{len(nodes):04d}",
            "name": name,
            "kind": str(raw.get("kind", "") or "").strip(),
            "importance": importance,
            "description": description,
            "label_description": "",
            "type": _engine_type(importance),
            "region": area_by_key.get(_join_key(raw.get("region", "")), ""),
        }
        if layer_id:
            node["layer_id"] = layer_id
        crossing = str(raw.get("crossing", "") or "").strip()
        if crossing:
            node["crossing"] = crossing
        nodes.append(node)
        by_key[key] = node
        if isinstance(raw.get("adjacent"), list):
            raw_adjacent[node["id"]] = raw["adjacent"]
        if isinstance(raw.get("contains"), list):
            raw_contains[node["id"]] = raw["contains"]

    # Authored containment: venue-scale places live INSIDE their node.
    for node in nodes:
        for contained_name in raw_contains.get(node["id"], []):
            contained_name = str(contained_name or "").strip()
            ckey = _join_key(contained_name)
            if not contained_name or ckey in by_key or ckey in placed:
                continue
            loc = loc_by_key.get(ckey)
            node.setdefault("contained_locations", []).append({
                "name": loc.get("name", contained_name) if loc else contained_name,
                "description": (loc or {}).get("description", ""),
            })
            placed.add(ckey)

    # Safety net: an authored place the LLM neither made a node nor contained
    # anywhere folds into the best-fitting node (its anchor, then the most
    # important node of its region, then the map's most important node).
    def _most_important(candidates):
        return max(candidates, key=lambda n: n.get("importance", 0), default=None)

    for key, loc in loc_by_key.items():
        if key in placed or key in by_key:
            continue
        anchor = by_key.get(_join_key(loc.get("part_of", "")))
        if anchor is None and loc.get("region"):
            anchor = _most_important(
                [n for n in nodes
                 if _join_key(n.get("region")) == _join_key(loc["region"])])
        if anchor is None:
            anchor = _most_important(nodes)
        if anchor is None:
            break  # no nodes at all; caller falls back to the mock graph
        anchor.setdefault("contained_locations", []).append({
            "name": loc.get("name", ""),
            "description": loc.get("description", ""),
        })
        placed.add(key)

    # Authored adjacency -> edges (resolved by tolerant name, symmetric dedup).
    edges: list[dict] = []
    seen_pairs: set[frozenset] = set()
    for node in nodes:
        for other_name in raw_adjacent.get(node["id"], []):
            other = by_key.get(_join_key(str(other_name or "")))
            if other is None or other["id"] == node["id"]:
                continue
            pair = frozenset((node["id"], other["id"]))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            edges.append({"from": node["id"], "to": other["id"]})

    return {
        "description": str(parsed.get("description", "")).strip(),
        "nodes": nodes,
        "edges": edges,
    }


def ensure_crossing_nodes(graph: dict, label: str, kind: str, count: int,
                          id_prefix: str = "") -> list:
    """The ``count`` nodes of this graph that carry crossings to the parallel
    plane ``label``. Authored crossing nodes (``crossing`` matching the
    label, or any ``crossing`` mark when none match exactly) are used first;
    the shortfall is synthesized. Crossing nodes take the connection kind as
    their engine type (mirroring the procedural interlayer convention)."""
    nodes = graph["nodes"]
    kind = (kind or "passage").strip() or "passage"
    matched = [n for n in nodes
               if _join_key(n.get("crossing", "")) == _join_key(label)]
    if not matched:
        matched = [n for n in nodes
                   if n.get("crossing") and not n.get("interlayer_connection_id")]
    crossings = matched[:max(1, count)]
    for node in crossings:
        node["type"] = kind

    layer_id = id_prefix.rstrip("_")
    while len(crossings) < max(1, count):
        node = {
            "id": f"{id_prefix}n_{len(nodes):04d}",
            "name": "",
            "kind": kind,
            "importance": 4,
            "description": "",
            "label_description": "",
            "type": kind,
            "region": "",
        }
        if layer_id:
            node["layer_id"] = layer_id
        nodes.append(node)
        anchor = max((n for n in nodes if n is not node),
                     key=lambda n: n.get("importance", 0), default=None)
        if anchor is not None:
            graph["edges"].append({"from": anchor["id"], "to": node["id"]})
        crossings.append(node)
    return crossings


def layout_abstract_graph(graph: dict, areas: list, map_width: float = 1000.0,
                          map_height: float = 1000.0,
                          generated_from: str = "") -> dict:
    """Deterministic positions + connectivity for a normalized abstract graph.

    Region clusters sit on a ring around the map center (regionless nodes
    cluster at the center); nodes fill each cluster along a golden-angle
    spiral, most important first; a relaxation pass enforces minimum spacing;
    disconnected components are joined by their closest node pair. Returns
    the standard per-map shape ``{nodes, edges, regions, config}`` (plus the
    authored ``description``)."""
    nodes = graph["nodes"]
    edges = graph["edges"]

    ordered_regions = []
    node_regions = {n.get("region", "") for n in nodes}
    for a in areas or []:
        name = str((a or {}).get("name", "")).strip()
        if name and name in node_regions and name not in ordered_regions:
            ordered_regions.append(name)
    for r in sorted(node_regions):
        if r and r not in ordered_regions:
            ordered_regions.append(r)

    cx, cy = map_width / 2.0, map_height / 2.0
    ring_radius = min(map_width, map_height) * 0.32
    centers = {"": (cx, cy)}
    k = len(ordered_regions)
    for i, region in enumerate(ordered_regions):
        if k == 1:
            centers[region] = (cx, cy)
        else:
            angle = -math.pi / 2 + (2 * math.pi * i) / k
            centers[region] = (cx + ring_radius * math.cos(angle),
                               cy + ring_radius * math.sin(angle))
    if "" in node_regions and k >= 1:
        # Regionless nodes keep the center; region clusters ring around them.
        centers[""] = (cx, cy)

    golden = math.pi * (3 - math.sqrt(5))
    spacing = 68.0
    margin = 50.0
    by_region: dict[str, list] = {}
    for n in sorted(nodes, key=lambda n: -n.get("importance", 0)):
        by_region.setdefault(n.get("region", ""), []).append(n)
    for region, members in by_region.items():
        ox, oy = centers.get(region, (cx, cy))
        for j, node in enumerate(members):
            r = spacing * math.sqrt(j)
            theta = j * golden
            node["x"] = min(map_width - margin, max(margin, ox + r * math.cos(theta)))
            node["y"] = min(map_height - margin, max(margin, oy + r * math.sin(theta)))

    # Relaxation: push overlapping pairs apart to a minimum spacing.
    min_dist = 58.0
    for _ in range(60):
        moved = False
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                a, b = nodes[i], nodes[j]
                dx, dy = b["x"] - a["x"], b["y"] - a["y"]
                d = math.hypot(dx, dy)
                if d >= min_dist:
                    continue
                if d < 1e-6:
                    dx, dy, d = 1.0, 0.5, 1.0
                push = (min_dist - d) / 2.0
                ux, uy = dx / d, dy / d
                a["x"] = min(map_width - margin, max(margin, a["x"] - ux * push))
                a["y"] = min(map_height - margin, max(margin, a["y"] - uy * push))
                b["x"] = min(map_width - margin, max(margin, b["x"] + ux * push))
                b["y"] = min(map_height - margin, max(margin, b["y"] + uy * push))
                moved = True
        if not moved:
            break
    for n in nodes:
        n["x"] = round(n["x"], 4)
        n["y"] = round(n["y"], 4)

    # Connectivity repair: join components by their closest node pair.
    parent = {n["id"]: n["id"] for n in nodes}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for e in edges:
        if e["from"] in parent and e["to"] in parent:
            _union(e["from"], e["to"])
    while True:
        roots = {}
        for n in nodes:
            roots.setdefault(_find(n["id"]), []).append(n)
        components = list(roots.values())
        if len(components) <= 1:
            break
        base = max(components, key=len)
        best = None
        for comp in components:
            if comp is base:
                continue
            for a in base:
                for b in comp:
                    d = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
                    if best is None or d < best[0]:
                        best = (d, a, b)
        _d, a, b = best
        edges.append({"from": a["id"], "to": b["id"]})
        _union(a["id"], b["id"])

    regions = []
    for region in ordered_regions:
        members = by_region.get(region, [])
        if not members:
            continue
        regions.append({
            "region_name": region,
            "node_ids": [n["id"] for n in members],
            "center_node_id": members[0]["id"],
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "regions": regions,
        "config": {
            "total_nodes": len(nodes),
            "map_width": map_width,
            "map_height": map_height,
            "generated_from": generated_from,
        },
        "description": graph.get("description", ""),
    }


def mock_abstract_parsed(label: str, areas: list, named_locations: list,
                         max_nodes: int = MAX_ROOT_NODES) -> dict:
    """Deterministic offline stand-in for the authored-layer LLM call.

    Standalone authored places become nodes in their (resolved) region;
    ``inside``-related places are left for the normalizer's containment
    safety net; each area without an authored place gets a hub node so every
    region exists on the map. Adjacency chains each region and rings the
    region heads together, giving a connected, fully named mock map."""
    area_names = [str((a or {}).get("name", "")).strip()
                  for a in areas or [] if str((a or {}).get("name", "")).strip()]
    nodes = []
    by_region: dict[str, list] = {}

    def _add(name, kind, region, importance, description):
        node = {"name": name, "kind": kind, "region": region,
                "importance": importance, "description": description,
                "adjacent": []}
        nodes.append(node)
        by_region.setdefault(region, []).append(node)
        return node

    for loc in named_locations or []:
        if len(nodes) >= max_nodes:
            break
        if not str((loc or {}).get("name", "")).strip():
            continue
        if loc.get("relation") == "inside":
            continue  # folded into its parent by the normalizer
        _add(loc["name"],
             "site" if loc.get("category") == "settlement" else "feature",
             str(loc.get("region", "") or "").strip(),
             8 if loc.get("category") == "settlement" else 6,
             loc.get("description", "") or f"Mock {label} place.")
    present = {_join_key(n["region"]) for n in nodes if n["region"]}
    for area in area_names:
        if len(nodes) >= max_nodes:
            break
        if _join_key(area) not in present:
            _add(f"{area} Hub", "hub", area, 7, f"Mock hub of {area}.")

    if not nodes:
        _add(f"{label} Core", "hub", area_names[0] if area_names else "", 8,
             f"Mock heart of {label}.")

    heads = []
    for _region, members in by_region.items():
        heads.append(members[0])
        for prev, nxt in zip(members, members[1:]):
            nxt["adjacent"].append(prev["name"])
    for prev, nxt in zip(heads, heads[1:]):
        nxt["adjacent"].append(prev["name"])
    if len(heads) > 2:
        heads[0]["adjacent"].append(heads[-1]["name"])

    return {
        "description": f"Mock abstract map of {label}: "
                       f"{len(nodes)} authored places.",
        "nodes": nodes,
    }
