"""The dynamic movement mutation schema offered to the Reader each turn.

world_format 2: same-map destinations come from the player's CURRENT map
only; crossing to another map goes through ``player_passage`` (a connection
id from the listed exits). Hidden connections are never offered."""

from .worldspace import (
    connections_from,
    get_map,
    map_nodes,
    node_index,
    player_map_id,
)


def _passage_options(world_data: dict, state: dict) -> list[str]:
    """Selectable connection options reachable from the player's map."""
    current_map = player_map_id(state)
    current_node = state.get("player_location_node_id")
    by_id = node_index(world_data)
    options = []
    seen = set()
    views = connections_from(world_data, current_map)
    # Connections at the player's node first — they're one step away.
    views.sort(key=lambda v: v["near"].get("node_id") != current_node)
    for view in views:
        c = view["connection"]
        if c.get("id") in seen:
            continue
        seen.add(c.get("id"))
        far = view["far"]
        far_map = get_map(world_data, far.get("map_id", "")) or {}
        far_node = by_id.get(far.get("node_id")) or {}
        near_node = by_id.get(view["near"].get("node_id")) or {}
        kind = c.get("kind", "passage")
        name = c.get("name") or kind
        target = far_map.get("label", far.get("map_id", ""))
        if far_node.get("name"):
            target = f"{target}: {far_node['name']}"
        via = ""
        if view["near"].get("node_id") != current_node and near_node.get("name"):
            via = f", via {near_node['name']}"
        options.append(f"{c.get('id')} ({kind}: {name} -> {target}{via})")
        if len(options) >= 12:
            break
    return options


def build_location_mutation_schema(world_data: dict, state: dict = None) -> dict:
    state = state or {}
    current_map = player_map_id(state) if state else None
    if current_map and get_map(world_data, current_map) is not None:
        nodes = map_nodes(world_data, current_map)
    else:
        from .worldspace import all_map_nodes
        nodes = all_map_nodes(world_data)
    regions = world_data.get("regions", {}).get("regions", [])
    location_options = []
    for n in nodes:
        if n.get("name"):
            location_options.append(f"{n['id']} ({n.get('name', '')})")
    # Lazy worlds leave minor waypoints unnamed until visited; offer the
    # revealed ones as explicit "unexplored" destinations so the player can
    # still head toward them (they get detailed on approach).
    if state and len(location_options) < 30:
        revealed = set(state.get("revealed_node_ids", []))
        for n in nodes:
            if len(location_options) >= 30:
                break
            if not n.get("name") and n.get("id") in revealed:
                location_options.append(f"{n['id']} (unexplored {n.get('type', 'waypoint')})")
    if not location_options:
        location_options = ["any"]
    schema = {
        "player_location_changed": {"type": "boolean", "label": "Did the player move toward or arrive at a new location?"},
        "player_location_node_id": {
            "type": "select",
            "label": "Destination node ID",
            "options": location_options[:30],
            "description": "The node_id of a destination on the CURRENT map the player moved to or set out toward. Distant destinations are fine — the journey plays out over multiple turns. For a way that leads to another map, use player_passage instead. Set only if player_location_changed is true."
        },
        "travel_interrupted": {
            "type": "boolean",
            "label": "Did the player pause an ongoing journey this turn (camping, resting, fighting, exploring a stop)?"
        },
    }
    region_names = [r.get("name", "") for r in regions if r.get("name")]
    if region_names:
        schema["player_location_region"] = {
            "type": "select",
            "label": "New region name",
            "options": region_names[:20],
            "description": "The region the player moved into. Set only if player_location_changed is true."
        }

    passage_options = _passage_options(world_data, state) if state else []
    if passage_options:
        schema["player_passage"] = {
            "type": "select",
            "label": "Passage the player took to another map",
            "options": passage_options + ["none"],
            "description": (
                "Use when the player passes between maps through one of these listed ways "
                "(a door, gate, shuttle, portal...). If the player isn't at the passage yet "
                "they will travel to it first, then pass through. If a passage states a "
                "requirement the player hasn't met, do NOT select it — narrate the obstacle "
                "instead. Leave as none for ordinary same-map movement."
            ),
        }

    # Intra-site movement: when the player's current location has an expanded
    # interior, offer its sub-locations as instant moves within the place.
    if state:
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
    from .worldspace import ensure_v2
    world_data = state.get("world_data")
    if not world_data:
        return {}
    ensure_v2(state)
    return build_location_mutation_schema(world_data, state)
