"""Pure helpers for building per-node enrichment context and link handling."""

import re

from wbworldgen.mapmodel import compass_direction as _compass_direction

#: ``${link_<id>}`` (unresolved) or ``${link_<id>|<label>}`` (resolved) — the
#: one scan pattern shared by lints, the edit tool and structural surgery.
LINK_TOKEN = re.compile(r"\$\{link_([^}|]+)(\|[^}]*)?\}")


def collect_nodes_by_layer(compiled: dict, layer_filter: str = None) -> tuple:
    """Return (all_nodes, layer_map). Each node is a copy tagged with its
    map_id/layer_id/layer_name. layer_map maps map id -> {done, total}.

    ``layer_filter`` (and the layer_map keys) are map ids in world_format 2;
    the ``layer_id`` tag carries the map's legacy layer id (terrain rasters
    are stored under it), falling back to the map id."""
    from wbworldgen.worldgen import mapspace as _ms

    all_nodes = []
    layer_map = {}
    for mid, m in _ms.maps_by_id(compiled).items():
        if layer_filter and mid != layer_filter:
            continue
        map_nodes = m.get("nodes", [])
        layer_map[mid] = {"done": 0, "total": len(map_nodes)}
        for n in map_nodes:
            nc = dict(n)
            nc["map_id"] = mid
            nc["layer_id"] = m.get("legacy_layer_id") or mid
            nc["layer_name"] = m.get("label", mid)
            all_nodes.append(nc)
    return all_nodes, layer_map


def get_neighbor_context(node: dict, all_nodes: list, compiled: dict, include_descriptions: bool) -> list:
    from wbworldgen.worldgen import mapspace as _ms

    node_id = node.get("id", "")
    node_x = node.get("x", 0)
    node_y = node.get("y", 0)

    edges = [e for e in _ms.all_edges(compiled)
             if e.get("from") == node_id or e.get("to") == node_id]

    neighbor_ids = set()
    for e in edges:
        nid = e.get("to") if e.get("from") == node_id else e.get("from")
        if nid:
            neighbor_ids.add(nid)

    all_by_id = {n.get("id"): n for n in all_nodes}
    neighbors = []
    for nid in neighbor_ids:
        nb = all_by_id.get(nid)
        if not nb:
            continue
        direction = _compass_direction(node_x, node_y, nb.get("x", 0), nb.get("y", 0))
        info = {
            "link_id": f"${{link_{nid}}}",
            "name": nb.get("name", "") or f"{nb.get('type', 'waypoint')} in {nb.get('region', 'unknown region')}",
            "type": nb.get("type", ""),
            "direction": direction,
        }
        if include_descriptions and nb.get("description"):
            info["description"] = nb.get("description", "")
        neighbors.append(info)
    return neighbors


def get_connection_context(node: dict, compiled: dict) -> dict:
    """When the node is a connection endpoint (a way to another map), return
    its kind, authored name/description and the map it links to. Empty
    otherwise."""
    node_id = node.get("id")
    # world_format 2: connections are a flat top-level list.
    for c in compiled.get("connections") or []:
        frm, to = c.get("from") or {}, c.get("to") or {}
        if frm.get("node_id") == node_id:
            far = to
        elif c.get("bidirectional", True) and to.get("node_id") == node_id:
            far = frm
        else:
            continue
        from wbworldgen.worldgen import mapspace as _ms
        far_map = _ms.get_map(compiled, far.get("map_id", "")) or {}
        return {
            "type": c.get("kind", "passage"),
            "name": c.get("name", ""),
            "description": c.get("description", ""),
            "target_layer_id": far_map.get("label") or far.get("map_id", ""),
        }
    # Legacy inter-layer connection (un-migrated inputs, e.g. tests).
    conn_id = node.get("interlayer_connection_id")
    if not conn_id:
        return {}
    for lc in compiled.get("map_connections", []):
        if lc.get("id") != conn_id:
            continue
        if lc.get("from_node_id") == node.get("id"):
            target_layer = lc.get("to_layer_id", "")
        else:
            target_layer = lc.get("from_layer_id", "")
        return {
            "type": lc.get("connection_type", "passage"),
            "name": lc.get("name", ""),
            "description": lc.get("description", ""),
            "target_layer_id": target_layer,
        }
    return {}


def build_enrichment_context(node: dict, all_nodes: list, compiled: dict, include_descriptions: bool = False) -> dict:
    from wbworldgen.worldgen import mapspace as _ms

    world_rules = compiled.get("rules", {})
    lore = compiled.get("lore", {})

    # The node's map scope (labels/description come from the MapRecord).
    node_map = _ms.get_map(compiled, node.get("map_id", "")) if node.get("map_id") else None
    node_layer = None
    if node_map is not None:
        node_layer = {
            "layer_id": node_map.get("map_id", ""),
            "name": node_map.get("label", ""),
            "description": node_map.get("description", ""),
            "layer_type": node_map.get("level_type", "surface") or "surface",
        }
    else:
        # Legacy inputs: resolve against the old layers catalog.
        node_layer_id = node.get("layer_id", "")
        for layer in compiled.get("layers", []):
            if layer.get("layer_id") == node_layer_id:
                node_layer = layer
                break

    node_region_name = node.get("region", "")
    region_data = {}
    if node_region_name:
        for r in compiled.get("regions", {}).get("regions", []):
            if r.get("name") == node_region_name:
                region_data = r
                break

    neighbors = get_neighbor_context(node, all_nodes, compiled, include_descriptions)

    terrain_block = _terrain_for_node(node, compiled)

    result = {
        "world": {
            "name": lore.get("world_name", "Unknown"),
            "premise": lore.get("premise", ""),
            "genre": world_rules.get("genre", ""),
            "tone": world_rules.get("tone", ""),
        },
        "layer": {
            "name": node_layer.get("name", "") if node_layer else node.get("layer_name", ""),
            "description": node_layer.get("description", "") if node_layer else "",
            "type": node_layer.get("layer_type", "surface") if node_layer else "surface",
        },
        "node": {"id": node.get("id", ""), "type": node.get("type", ""), "importance": node.get("importance", 0)},
        "neighbors": neighbors,
        "region": {
            "name": region_data.get("name", node_region_name),
            "terrain": region_data.get("terrain", ""),
            "climate": region_data.get("climate", ""),
            "description": region_data.get("description", ""),
            "factions": region_data.get("factions", []),
            "landmarks": region_data.get("landmarks", []),
        },
    }
    if terrain_block:
        result["terrain"] = terrain_block
    connection_block = get_connection_context(node, compiled)
    if connection_block:
        result["connection"] = connection_block
    vocab = compiled.get("template_vocab")
    if isinstance(vocab, dict) and vocab:
        result["vocab"] = vocab
    # Agreed design notes bound to this node's map (C5/N3): the compiled
    # world carries the ideation brief; scoped notes reach only their own
    # map's content calls.
    from wbworldgen.worldgen.notes import notes_for_map
    map_notes = notes_for_map(compiled, compiled, node.get("map_id", ""))
    if map_notes:
        result["notes"] = map_notes
    # Codex (the world's reference lore): world-wide entries by summary,
    # entries bound to this node's map in full — same visibility rule as
    # notes (content calls see their scope).
    from wbworldgen.worldgen.codex import node_context_block
    codex_block = node_context_block(compiled, node.get("map_id", ""))
    if codex_block:
        result["codex"] = codex_block
    return result


def _terrain_for_node(node: dict, compiled: dict) -> dict:
    """Sample the node's biome/elevation/features from the layer's terrain
    raster (attached to ``compiled`` by the enrichment engine). Returns {} when
    no terrain is available (e.g. underground layers)."""
    terrain_layers = compiled.get("_terrain_layers")
    if not terrain_layers:
        return {}
    layers = terrain_layers.get(node.get("layer_id", ""))
    if layers is None and len(terrain_layers) == 1:
        layers = next(iter(terrain_layers.values()))
    if not layers:
        return {}
    from wbworldgen.worldgen import mapspace as _ms
    from wbworldgen.worldgen import terrain_store as _ts
    node_map = _ms.get_map(compiled, node.get("map_id", "")) if node.get("map_id") else None
    cfg = (node_map or {}).get("config") or compiled.get("map", {}).get("config", {})
    mw = cfg.get("map_width", 1000.0)
    mh = cfg.get("map_height", 1000.0)
    return _ts.sample_terrain(layers, node.get("x", 0), node.get("y", 0), mw, mh)


def postprocess_links(text: str, node: dict, all_nodes: list) -> str:
    """Rewrite ``${link_<id>}`` tokens into ``${link_<id>|<name> (<direction>)}``."""
    if not text:
        return text
    node_x = node.get("x", 0)
    node_y = node.get("y", 0)
    all_by_id = {n.get("id"): n for n in all_nodes}

    def replacer(match):
        nid = match.group(1)
        nb = all_by_id.get(nid)
        if nb:
            name = nb.get("name", nid)
            direction = _compass_direction(node_x, node_y, nb.get("x", 0), nb.get("y", 0))
            return f"${{link_{nid}|{name} ({direction})}}"
        return f"${{link_{nid}}}"

    return re.sub(r'\$\{link_([^}]+)\}', replacer, text)
