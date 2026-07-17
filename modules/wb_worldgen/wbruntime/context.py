"""Turn-time context blocks: the per-turn <current_location> string and the
richer opening-scene world block."""

from . import backfill as _backfill_rt
from . import expansion as _expansion
from .travel import remaining_travel, travel_speed, weighted_adjacency
from .worldspace import all_map_nodes, get_site_position, get_travel


def build_travel_context(host, travel: dict, state: dict, world_data: dict) -> str:
    """<current_location> variant for a player who is on the road between nodes."""
    nodes_by_id = {n.get("id"): n for n in all_map_nodes(world_data)}
    route = travel.get("route", [])
    leg_index = travel.get("leg_index", 0)
    if len(route) < 2 or leg_index >= len(route) - 1:
        return ""
    from_node = nodes_by_id.get(route[leg_index], {})
    to_node = nodes_by_id.get(route[leg_index + 1], {})
    dest_node = nodes_by_id.get(travel.get("destination_node_id"), {})

    def _label(node, fallback):
        return node.get("name") or node.get("id") or fallback

    from_name = _label(from_node, "the last waypoint")
    to_name = _label(to_node, "the next waypoint")
    dest_name = _label(dest_node, "the destination")

    leg_distance = travel.get("leg_distance") or 1.0
    pct = int(round(100 * min(travel.get("leg_progress", 0.0) / leg_distance, 1.0)))
    speed = travel_speed(host, world_data)
    turns_left = None
    if speed:
        adjacency = weighted_adjacency(world_data)
        turns_left = max(1, int(-(-remaining_travel(travel, adjacency) // speed)))

    parts = ["<current_location>"]
    parts.append(f"Status: EN ROUTE — the player is traveling from {from_name} toward {to_name}, about {pct}% of the way along this stretch.")
    if dest_name != to_name:
        parts.append(f"Final destination: {dest_name}.")
    if turns_left is not None:
        parts.append(f"Estimated travel remaining: about {turns_left} turn(s) until arrival at {dest_name}.")
    to_desc = to_node.get("description") or to_node.get("label_description")
    if to_desc:
        parts.append(f"Ahead lies {to_name} ({to_node.get('type', 'location')}) — {to_desc[:300]}")
    region_name = from_node.get("region") or state.get("player_location_region")
    if region_name:
        regions = world_data.get("regions", {}).get("regions", [])
        current_region = next((r for r in regions if r.get("name") == region_name), None)
        parts.append(f"Region: {region_name}")
        if current_region:
            parts.append(f"Terrain: {current_region.get('terrain', 'N/A')[:400]}")
            parts.append(f"Climate: {current_region.get('climate', 'N/A')[:200]}")
    parts.append(f"The player has NOT yet arrived at {dest_name}. Narrate the journey itself — the road, terrain, weather, fellow travelers, or incidents along the way. Do not narrate arrival at {dest_name} this turn; travel completes on its own.")
    parts.append("</current_location>")
    return "\n".join(parts)


def build_location_context(host, state: dict, world_data: dict) -> str:
    travel = get_travel(state)
    if travel:
        travel_context = build_travel_context(host, travel, state, world_data)
        if travel_context:
            return travel_context
    node_id = state.get("player_location_node_id")
    region_name = state.get("player_location_region")
    layer_id = state.get("player_location_layer_id")
    nodes = world_data.get("map", {}).get("nodes", [])
    map_layers = world_data.get("map_layers", [])
    regions = world_data.get("regions", {}).get("regions", [])
    layer_info = world_data.get("layers", [])

    if map_layers:
        all_nodes = []
        for layer in map_layers:
            all_nodes.extend(layer.get("map", {}).get("nodes", []))
        nodes = all_nodes

    current_node = None
    for n in nodes:
        if n.get("id") == node_id:
            current_node = n
            break

    current_region = None
    if region_name:
        for r in regions:
            if r.get("name") == region_name:
                current_region = r
                break

    current_layer = None
    if layer_id:
        for layer in layer_info:
            if layer.get("layer_id") == layer_id:
                current_layer = layer
                break

    if not current_node and not current_region and not current_layer:
        return ""

    parts = ["<current_location>"]
    if current_layer:
        parts.append(f"Layer: {current_layer.get('name', layer_id)} — {current_layer.get('description', '')[:300]}")
        layer_rules = world_data.get("layer_rules", [])
        for lr in layer_rules:
            if lr.get("layer_id") == layer_id:
                rules = lr.get("rules", [])
                if rules:
                    parts.append("<layer_rules>")
                    for rule in rules:
                        parts.append(f"  - {rule}")
                    parts.append("</layer_rules>")
                break
    if current_node:
        node_name = current_node.get("name", "")
        node_type = current_node.get("type", "location")
        node_desc = current_node.get("description", "") or current_node.get("label_description", "")
        if node_name and node_desc:
            parts.append(f"Location: {node_name} ({node_type}) — {node_desc[:600]}")
        elif node_name:
            parts.append(f"Location: {node_name} ({node_type})")
        else:
            # Not yet generated (lazy world detail) — give the storyteller an
            # honest basis to improvise from the region/terrain context below.
            parts.append(
                f"Location: an unexplored {node_type} — this place has no established "
                "name or details yet. Improvise fitting local color from the region "
                "and terrain context; keep any specifics provisional.")
        if current_node.get("interlayer_connection_id"):
            map_connections = world_data.get("map_connections", [])
            for lc in map_connections:
                if lc.get("id") == current_node.get("interlayer_connection_id"):
                    target_layer = lc.get("to_layer_id") if lc.get("from_layer_id") == layer_id else lc.get("from_layer_id")
                    parts.append(f"Inter-layer connection: {lc.get('connection_type', 'passage')} to layer '{target_layer}' — {lc.get('description', '')[:200]}")
                    break
        site = (world_data.get("site_maps") or {}).get(node_id)
        if site:
            subs = site.get("sub_locations", [])
            site_position = get_site_position(state)
            current_sub = None
            if site_position and site_position.get("parent_node_id") == node_id:
                current_sub = next(
                    (s for s in subs if s.get("id") == site_position.get("sub_location_id")), None)
            parts.append("<location_interior>")
            if current_sub:
                parts.append(
                    f"The player is currently at: {current_sub.get('name', '')} "
                    f"({current_sub.get('type', 'place')}) — {current_sub.get('description', '')[:300]}")
                adjacent_ids = set(current_sub.get("adjacent", []))
                adjacent_names = [s.get("name", "") for s in subs
                                  if s.get("id") in adjacent_ids and s.get("name")]
                if adjacent_names:
                    parts.append(f"Directly adjoining: {', '.join(adjacent_names)}")
            if site.get("layout_summary"):
                parts.append(f"Layout: {site['layout_summary']}")
            if subs:
                parts.append("Places within this location:")
                for sub in subs[:12]:
                    line = f"  - {sub.get('name', '')} ({sub.get('type', 'place')})"
                    if sub.get("description"):
                        line += f": {sub['description'][:200]}"
                    parts.append(line)
            parts.append("</location_interior>")
    if current_region:
        parts.append(f"Region: {current_region.get('name', '')}")
        parts.append(f"Terrain: {current_region.get('terrain', 'N/A')[:400]}")
        parts.append(f"Climate: {current_region.get('climate', 'N/A')[:200]}")
        landmarks = current_region.get("landmarks", [])
        if landmarks:
            parts.append(f"Nearby Landmarks: {', '.join(landmarks[:5])}")
        factions = current_region.get("factions", [])
        if factions:
            parts.append(f"Local Factions: {', '.join(factions[:5])}")
    if not current_region and region_name:
        parts.append(f"Region: {region_name}")
    parts.append("</current_location>")
    return "\n".join(parts)


async def on_gather_context(host, state: dict, sdk) -> dict:
    """Per-turn world context: the player's current location block."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    await _backfill_rt.ensure_current_node_detailed(host, state)
    _backfill_rt.kick_background_detail(host, state)
    if not get_travel(state):
        # Arrived (or standing) somewhere expandable whose interior is still
        # missing (prefetch missed / on_arrival mode) — start it now.
        _expansion.maybe_expand_site(host, state, state.get("player_location_node_id"))
    location_context = build_location_context(host, state, world_data)
    if not location_context:
        return {}
    return {"context_string": location_context}


async def on_intro_context(host, state: dict, sdk) -> dict:
    """Opening-scene world block: rules + premise + current location."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    parts = []
    rules = world_data.get("rules", {})
    lore = world_data.get("lore", {})
    if rules:
        parts.append("<world_rules>")
        parts.append(f"Genre: {rules.get('genre', 'N/A')}")
        parts.append(f"Tone: {rules.get('tone', 'N/A')}")
        parts.append(f"Magic Level: {rules.get('magic_level', 'N/A')}")
        parts.append(f"Technology Era: {rules.get('tech_era', 'N/A')}")
        parts.append(f"Lethality: {rules.get('lethality', 'N/A')}/10")
        custom_rules = rules.get("custom_rules", [])
        if custom_rules:
            parts.append("Custom Rules:")
            for rule in custom_rules:
                parts.append(f"  - {rule}")
        parts.append("</world_rules>")
    if lore:
        parts.append("<world_premise>")
        world_name = lore.get("world_name", "")
        if world_name:
            parts.append(f"World: {world_name}")
        premise = lore.get("premise", "")
        if premise:
            parts.append(premise)
        central_conflict = lore.get("central_conflict", "")
        if central_conflict:
            parts.append(f"Central Conflict: {central_conflict}")
        creation_myth = lore.get("creation_myth", "")
        if creation_myth:
            parts.append(f"Creation Myth: {creation_myth}")
        eras = lore.get("historical_eras", [])
        if eras:
            parts.append("Historical Eras:")
            for era in eras:
                parts.append(f"  - {era.get('name', '')}: {era.get('summary', '')}")
        parts.append("</world_premise>")
    location_text = build_location_context(host, state, world_data)
    if location_text:
        parts.append(location_text)
    if not parts:
        return {}
    return {"content": "\n".join(parts)}
