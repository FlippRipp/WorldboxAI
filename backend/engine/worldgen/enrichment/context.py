"""Pure helpers for building per-node enrichment context and link handling."""

import re

from backend.engine.world_map import compass_direction as _compass_direction


def collect_nodes_by_layer(compiled: dict, layer_filter: str = None) -> tuple:
    """Return (all_nodes, layer_map). Each node is a copy tagged with its
    layer_id/layer_name. layer_map maps layer id -> {done, total}."""
    all_nodes = []
    layer_map = {}
    map_layers = compiled.get("map_layers", [])
    layers_info = compiled.get("layers", [])

    if map_layers:
        for ml in map_layers:
            lid = ml.get("layer_id", "")
            if layer_filter and lid != layer_filter:
                continue
            layer_nodes = ml.get("map", {}).get("nodes", [])
            layer_map[lid] = {"done": 0, "total": len(layer_nodes)}
            for n in layer_nodes:
                nc = dict(n)
                nc["layer_id"] = lid
                nc["layer_name"] = ml.get("name", lid)
                all_nodes.append(nc)
    else:
        flat_nodes = compiled.get("map", {}).get("nodes", [])
        layer_map["main"] = {"done": 0, "total": len(flat_nodes)}
        for n in flat_nodes:
            nc = dict(n)
            nc["layer_id"] = ""
            all_nodes.append(nc)

    layers_by_id = {l.get("layer_id", ""): l for l in layers_info}
    for n in all_nodes:
        lid = n.get("layer_id", "")
        if lid and lid in layers_by_id:
            n["layer_name"] = layers_by_id[lid].get("name", lid)

    return all_nodes, layer_map


def get_neighbor_context(node: dict, all_nodes: list, compiled: dict, include_descriptions: bool) -> list:
    map_layers = compiled.get("map_layers", [])
    flat_edges = compiled.get("map", {}).get("edges", [])

    node_id = node.get("id", "")
    node_x = node.get("x", 0)
    node_y = node.get("y", 0)

    edges = []
    if map_layers:
        for ml in map_layers:
            for e in ml.get("map", {}).get("edges", []):
                if e.get("from") == node_id or e.get("to") == node_id:
                    edges.append(e)
    else:
        for e in flat_edges:
            if e.get("from") == node_id or e.get("to") == node_id:
                edges.append(e)

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


def build_enrichment_context(node: dict, all_nodes: list, compiled: dict, include_descriptions: bool = False) -> dict:
    world_rules = compiled.get("rules", {})
    lore = compiled.get("lore", {})
    layers_info = compiled.get("layers", [])

    node_layer_id = node.get("layer_id", "")
    node_layer = None
    for layer in layers_info:
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

    return {
        "world": {
            "name": lore.get("world_name", "Unknown"),
            "premise": lore.get("premise", ""),
            "genre": world_rules.get("genre", ""),
            "tone": world_rules.get("tone", ""),
        },
        "layer": {
            "name": node_layer.get("name", node_layer_id) if node_layer else node.get("layer_name", ""),
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
