"""Turn-time context blocks: the per-turn <current_location> string and the
richer opening-scene world block. world_format 2: the block leads with the
hierarchy breadcrumb and always carries an <exits> list of visible
connections, so the storyteller can surface the ways in and out naturally."""

from . import backfill as _backfill_rt
from . import expansion as _expansion
from .travel import remaining_travel, travel_speed, weighted_adjacency
from .worldspace import (
    all_map_nodes,
    breadcrumb,
    connections_from,
    ensure_v2,
    get_map,
    get_site_position,
    get_travel,
    node_index,
    player_map_id,
)

# Appended once to the opening-scene world block: how the storyteller works
# the hierarchical map system.
MOVEMENT_PRIMER = """<world_navigation>
The world is a hierarchy of maps: large maps (a world, a planet) contain
locations that can open into their own smaller maps (a city, an interior).
You move the player with these tools:
  - player_location_node_id: places on the player's CURRENT map.
  - player_passage: one of the listed exits/ways that lead to another map
    (doors, gates, shuttles, stairs, portals). If the player isn't there yet,
    they will travel to it first.
  - custom_transition: ONLY when the story creates a brand-new way through
    (a blown-up wall, a picked window, a teleport). You choose whether it
    persists as a new passage or leaves no trace.
Requirements on an exit are enforced by YOU: if the player hasn't met one,
do not select that passage — narrate the obstacle instead.
Exits marked SECRET are unknown to the player: never volunteer them; use
discover_passage when the story genuinely uncovers one.
When the player arrives somewhere, looks around, or asks where they can go,
weave the listed adjoining places and exits naturally into the narration.
Never invent geography that contradicts them.
</world_navigation>"""


def _transit_context(host, travel: dict, state: dict, world_data: dict) -> str:
    """<current_location> variant while aboard a journey-mode connection."""
    from .worldspace import find_connection
    connection = find_connection(world_data, travel.get("connection_id", "")) or {}
    by_id = node_index(world_data)
    final_node = by_id.get(travel.get("final_node_id")) or {}
    final_map = get_map(world_data, travel.get("final_map_id", "")) or {}
    dest = final_node.get("name") or final_map.get("label") or "the destination"
    kind = connection.get("kind", "passage")
    name = connection.get("name") or kind
    turns = travel.get("transit_turns_left", 1)
    parts = ["<current_location>"]
    parts.append(
        f"Status: IN TRANSIT — the player is passing through '{name}' ({kind}) "
        f"toward {dest} ({final_map.get('label', '')}).")
    if connection.get("description"):
        parts.append(f"The way: {connection['description'][:300]}")
    parts.append(f"About {max(1, turns)} turn(s) remain until arrival at {dest}.")
    parts.append(
        f"The player has NOT yet arrived. Narrate the crossing itself — the "
        f"passage, the vessel, fellow travelers, incidents along the way. Do "
        f"not narrate arrival at {dest} this turn; the transit completes on its own.")
    parts.append("</current_location>")
    return "\n".join(parts)


def build_travel_context(host, travel: dict, state: dict, world_data: dict) -> str:
    """<current_location> variant for a player who is on the road between nodes."""
    if travel.get("phase") == "transit":
        return _transit_context(host, travel, state, world_data)
    nodes_by_id = node_index(world_data)
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
    speed = travel_speed(host, world_data, travel.get("map_id"))
    turns_left = None
    if speed:
        adjacency = weighted_adjacency(world_data, travel.get("map_id"))
        turns_left = max(1, int(-(-remaining_travel(travel, adjacency) // speed)))

    parts = ["<current_location>"]
    parts.append(f"Status: EN ROUTE — the player is traveling from {from_name} toward {to_name}, about {pct}% of the way along this stretch.")
    if dest_name != to_name:
        parts.append(f"Final destination: {dest_name}.")
    if travel.get("pending_connection_id"):
        from .worldspace import find_connection
        pc = find_connection(world_data, travel["pending_connection_id"]) or {}
        if pc:
            parts.append(
                f"At {dest_name} the player will pass through '{pc.get('name') or pc.get('kind', 'the way onward')}' "
                f"({pc.get('kind', 'passage')}) and continue beyond this map.")
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


def _exits_block(world_data: dict, state: dict) -> list[str]:
    """<exits> lines: every visible connection reachable from this map,
    at-the-player ones first."""
    current_map = player_map_id(state)
    current_node = state.get("player_location_node_id")
    by_id = node_index(world_data)
    views = connections_from(world_data, current_map, include_hidden=True)
    views.sort(key=lambda v: (v["connection"].get("hidden", False),
                              v["near"].get("node_id") != current_node))
    lines = ["<exits>"]
    for view in views[:12]:
        c = view["connection"]
        far = view["far"]
        far_map = get_map(world_data, far.get("map_id", "")) or {}
        far_node = by_id.get(far.get("node_id")) or {}
        near_node = by_id.get(view["near"].get("node_id")) or {}
        kind = c.get("kind", "passage")
        name = c.get("name") or kind
        target = far_map.get("label", far.get("map_id", ""))
        if far_node.get("name"):
            target = f"{target}: {far_node['name']}"
        travel_spec = c.get("travel") or {}
        if travel_spec.get("mode") == "journey":
            how = f"a journey of about {travel_spec.get('turns', 1)} turn(s)"
        else:
            how = "instant"
        line = f"- [{c.get('id')}] {kind} \"{name}\" -> {target}. Passage is {how}."
        if c.get("description"):
            line += f" {c['description'][:150]}"
        if c.get("requirements"):
            line += f" Requires: {c['requirements']}"
        if view["near"].get("node_id") != current_node:
            where = near_node.get("name") or "another spot on this map"
            line += f" (elsewhere on this map, at {where} — the player would travel there first)"
        if c.get("hidden"):
            line += " (SECRET — the player has NOT found this; never volunteer it. Use discover_passage when the fiction earns it.)"
        lines.append(line)
    if len(lines) == 1:
        lines.append("- (no known ways off this map)")
    lines.append("</exits>")
    return lines


def build_location_context(host, state: dict, world_data: dict) -> str:
    travel = get_travel(state)
    if travel:
        travel_context = build_travel_context(host, travel, state, world_data)
        if travel_context:
            return travel_context
    node_id = state.get("player_location_node_id")
    region_name = state.get("player_location_region")
    current_map_id = player_map_id(state)
    current_map = get_map(world_data, current_map_id)
    regions = world_data.get("regions", {}).get("regions", [])
    current_node = node_index(world_data).get(node_id)

    current_region = None
    if region_name:
        for r in regions:
            if r.get("name") == region_name:
                current_region = r
                break

    if not current_node and not current_region and current_map is None:
        return ""

    parts = ["<current_location>"]
    trail = breadcrumb(world_data, current_map_id)
    if trail:
        crumbs = " > ".join(
            f"{m.get('label', m.get('map_id'))} ({m.get('level_type', 'map')})" for m in trail)
        parts.append(f"Scope: {crumbs}")
    if current_map is not None:
        if current_map.get("description"):
            parts.append(f"Map: {current_map.get('label', '')} — {current_map['description'][:300]}")
        map_rules = current_map.get("rules") or []
        if map_rules:
            parts.append("<map_rules>")
            for rule in map_rules:
                parts.append(f"  - {rule}")
            parts.append("</map_rules>")
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
    parts.extend(_exits_block(world_data, state))
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
    ensure_v2(state)
    await _backfill_rt.ensure_current_node_detailed(host, state)
    _backfill_rt.kick_background_detail(host, state)
    if not get_travel(state):
        # Arrived (or standing) somewhere expandable whose interior is still
        # missing (prefetch missed / on_arrival mode) — start it now.
        _expansion.maybe_expand_node(host, state, state.get("player_location_node_id"))
    location_context = build_location_context(host, state, world_data)
    if not location_context:
        return {}
    return {"context_string": location_context}


async def on_reader_context(host, state: dict, sdk) -> str:
    """Context for the module's dedicated reader call: movement-extraction
    guidance plus the player's pre-turn <current_location> block, so the
    extractor knows where the player stood when the story began."""
    world_data = state.get("world_data")
    if not world_data:
        return ""
    ensure_v2(state)
    guidance = (
        "You are extracting the player's MOVEMENT through the world from this "
        "turn's story. The schema lists every way the player can move: "
        "destinations on the current map, passages to other maps, improvised "
        "transitions, and places inside the current location. Report only "
        "movement that actually happened or clearly began in the story — a "
        "place merely mentioned, remembered, or planned for later is not "
        "movement. The location below is where the player was BEFORE this "
        "turn's story."
    )
    location_context = build_location_context(host, state, world_data)
    if location_context:
        return f"{guidance}\n\n{location_context}"
    return guidance


async def on_intro_context(host, state: dict, sdk) -> dict:
    """Opening-scene world block: rules + premise + navigation primer +
    current location."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    ensure_v2(state)
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
    if world_data.get("maps"):
        parts.append(MOVEMENT_PRIMER)
    location_text = build_location_context(host, state, world_data)
    if location_text:
        parts.append(location_text)
    if not parts:
        return {}
    return {"content": "\n".join(parts)}
