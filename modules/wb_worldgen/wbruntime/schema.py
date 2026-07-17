"""The dynamic movement mutation schema offered to the Reader each turn."""


def build_location_mutation_schema(world_data: dict, state: dict = None) -> dict:
    nodes = world_data.get("map", {}).get("nodes", [])
    map_layers = world_data.get("map_layers", [])
    if map_layers:
        all_nodes = []
        for layer in map_layers:
            all_nodes.extend(layer.get("map", {}).get("nodes", []))
        nodes = all_nodes
    regions = world_data.get("regions", {}).get("regions", [])
    location_options = []
    for n in nodes:
        if n.get("name"):
            location_options.append(f"{n['id']} ({n.get('name', '')})")
    # Lazy worlds leave minor waypoints unnamed until visited; offer the
    # revealed ones as explicit "unexplored" destinations so the player can
    # still head toward them (they get detailed on approach).
    if state is not None and len(location_options) < 30:
        revealed = set(state.get("revealed_node_ids", []))
        for n in nodes:
            if len(location_options) >= 30:
                break
            if not n.get("name") and n.get("id") in revealed:
                location_options.append(f"{n['id']} (unexplored {n.get('type', 'waypoint')})")
    if not location_options:
        location_options = ["any"]
    region_names = [r.get("name", "") for r in regions if r.get("name")]
    layers_list = world_data.get("layers", [])
    layer_options = [f"{l.get('layer_id', '')} ({l.get('name', '')})" for l in layers_list if l.get("layer_id")]
    if not layer_options:
        layer_options = ["surface"]
    schema = {
        "player_location_changed": {"type": "boolean", "label": "Did the player move toward or arrive at a new location?"},
        "player_location_node_id": {
            "type": "select",
            "label": "Destination node ID",
            "options": location_options[:30],
            "description": "The node_id of the location the player moved to or set out toward. Distant destinations are fine — the journey plays out over multiple turns. Set only if player_location_changed is true."
        },
        "travel_interrupted": {
            "type": "boolean",
            "label": "Did the player pause an ongoing journey this turn (camping, resting, fighting, exploring a stop)?"
        },
        "player_location_region": {
            "type": "select",
            "label": "New region name",
            "options": region_names[:20],
            "description": "The region the player moved into. Set only if player_location_changed is true."
        },
        "player_location_layer_id": {
            "type": "select",
            "label": "New layer ID",
            "options": layer_options[:10],
            "description": "The layer_id the player moved to (e.g., overworld, underground). Set only if the layer changed."
        },
    }

    # Intra-site movement: when the player's current location has an expanded
    # interior, offer its sub-locations as instant moves within the place.
    if state is not None:
        site = (world_data.get("site_maps") or {}).get(state.get("player_location_node_id"))
        if site and site.get("sub_locations"):
            sub_options = [
                f"{sub['id']} ({sub.get('name', '')})"
                for sub in site["sub_locations"][:16] if sub.get("id")
            ]
            sub_options.append("leave_site (step back out to the location as a whole)")
            schema["player_sub_location"] = {
                "type": "select",
                "label": f"Where inside {site.get('name', 'this location')} is the player now?",
                "options": sub_options,
                "description": "The specific place within the current location the player moved to. Moving between these is instant (no travel). Set only when the player moves within the location; use leave_site when they step back out.",
            }
    return schema


async def on_mutation_schema(host, state: dict, sdk) -> dict:
    """Offer the Reader a dynamic movement schema derived from the world map."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    return build_location_mutation_schema(world_data, state)
