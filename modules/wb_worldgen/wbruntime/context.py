"""Turn-time context blocks: the per-turn <current_location> string and the
richer opening-scene world block. world_format 2: the block leads with the
hierarchy breadcrumb and always carries an <exits> list of visible
connections, so the storyteller can surface the ways in and out naturally."""

from . import backfill as _backfill_rt
from . import expansion as _expansion
from . import routing as _routing
from .travel import journey_progress
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
Journeys are measured in in-world time, not turns: when the player travels,
narrate at whatever pace feels natural — a passing line or a whole scene,
including the arrival when the trip passes uneventfully. The world tracks
the minutes your narration spends on the road.
</world_navigation>"""


def build_travel_context(host, travel: dict, state: dict, world_data: dict) -> str:
    """<current_location> variant for a player who is on the road.

    Time-based: reports minutes elapsed/remaining and where along the
    itinerary the traveler is (mid-leg or aboard a connection), and invites
    the storyteller to narrate at any pace — the journey advances by the
    minutes the narration spends on the road."""
    from .worldspace import find_connection
    by_id = node_index(world_data)
    if "itinerary" not in travel:
        # Legacy turn-based record; it re-plans on the next state mutation.
        dest = by_id.get(travel.get("destination_node_id")
                         or travel.get("final_node_id")) or {}
        return ("<current_location>\nStatus: EN ROUTE toward "
                f"{dest.get('name', 'the destination')}.\n</current_location>")

    progress = journey_progress(travel)
    position = progress["position"]
    dest_node = by_id.get(travel.get("destination_node_id")) or {}
    dest_map = get_map(world_data, travel.get("destination_map_id", "")) or {}
    dest_name = dest_node.get("name") or dest_map.get("label") or "the destination"

    parts = ["<current_location>"]
    header = f"Status: EN ROUTE — the player is traveling toward {dest_name}"
    if travel.get("transport"):
        header += f" by {travel['transport']}"
    header += (f". About {progress['minutes_traveled']} in-world minutes into a "
               f"roughly {progress['eta_minutes']}-minute journey; "
               f"~{progress['minutes_remaining']} minutes remain.")
    parts.append(header)

    transit = position.get("transit")
    leg = position.get("leg")
    if transit:
        connection = find_connection(world_data, transit.get("connection_id", "")) or {}
        name = connection.get("name") or connection.get("kind", "passage")
        parts.append(
            f"Right now the player is aboard/passing through '{name}' "
            f"({connection.get('kind', 'passage')}), about "
            f"{int(round(100 * transit.get('fraction', 0.0)))}% across.")
        if connection.get("description"):
            parts.append(f"The way: {connection['description'][:300]}")
    elif leg:
        from_node = by_id.get(leg.get("from")) or {}
        to_node = by_id.get(leg.get("to")) or {}
        from_name = from_node.get("name") or "the last waypoint"
        to_name = to_node.get("name") or "the next waypoint"
        pct = int(round(100 * min(leg.get("fraction", 0.0), 1.0)))
        parts.append(f"Right now the player is between {from_name} and {to_name}, "
                     f"about {pct}% of the way along this stretch.")
        to_desc = to_node.get("description") or to_node.get("label_description")
        if to_desc:
            parts.append(f"Ahead lies {to_name} ({to_node.get('type', 'location')}) — {to_desc[:300]}")

    remaining_legs = _routing.describe_itinerary(world_data, travel.get("itinerary") or {})
    if remaining_legs:
        parts.append("The route: " + "; then ".join(remaining_legs) + ".")

    spot = position.get("position") or {}
    region_name = ((by_id.get(spot.get("node_id")) or {}).get("region")
                   or state.get("player_location_region"))
    if region_name:
        regions = world_data.get("regions", {}).get("regions", [])
        current_region = next((r for r in regions if r.get("name") == region_name), None)
        parts.append(f"Region: {region_name}")
        if current_region:
            parts.append(f"Terrain: {current_region.get('terrain', 'N/A')[:400]}")
            parts.append(f"Climate: {current_region.get('climate', 'N/A')[:200]}")
    parts.append(
        f"Narrate the journey at whatever pace feels natural — a passing line, a "
        f"scene on the road, or the entire remaining trip including the arrival at "
        f"{dest_name} if it passes uneventfully. The world advances the journey by "
        f"the in-world minutes your narration spends traveling and completes it "
        f"when you narrate the full trip.")
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


def build_location_context(host, state: dict, world_data: dict,
                           include_storyteller_notes: bool = True) -> str:
    """The per-turn <current_location> block. ``include_storyteller_notes``
    gates the <storyteller_notes> sub-block (the additional_details channel —
    see docs/design/node_info_layering_plan.md): on for the storyteller and
    the opening scene, off for the reader/extractor call, which only needs
    the player-knowable world."""
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
    storyteller_notes = []
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
        if current_node.get("additional_details"):
            storyteller_notes.append(
                f"{node_name or 'This location'}: {current_node['additional_details']}")
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
            if site.get("additional_details"):
                storyteller_notes.append(
                    f"{node_name or 'This place'} as a whole: {site['additional_details']}")
            if current_sub and current_sub.get("additional_details"):
                storyteller_notes.append(
                    f"{current_sub.get('name', 'The current spot')}: "
                    f"{current_sub['additional_details']}")
    if include_storyteller_notes and storyteller_notes:
        parts.append("<storyteller_notes>")
        parts.append(
            "(Your private prep — the player cannot read any of this. Weave "
            "unmarked details into the story freely when they fit; reveal "
            "facts marked \"Secret:\" only when the fiction genuinely earns "
            "the discovery. Notes you never use are fine.)")
        for note in storyteller_notes:
            parts.append(f"- {note}")
        parts.append("</storyteller_notes>")
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
    """Per-turn world context: the player's current location block, plus the
    pre-storyteller travel-intent pass (which may start a journey and add a
    <travel_plan> block)."""
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

    intent_result = {}
    try:
        from . import intent as _intent
        intent_result = await _intent.evaluate_travel_intent(host, state, sdk) or {}
    except Exception as e:
        print(f"[wb_worldgen] travel intent failed: {e}")

    location_context = build_location_context(host, state, world_data)
    blocks = [b for b in (location_context, intent_result.get("context_string")) if b]
    result = {}
    if blocks:
        result["context_string"] = "\n".join(blocks)
    for key in ("module_data", "module_data_replace"):
        if intent_result.get(key):
            result[key] = intent_result[key]
    return result


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
    travel = get_travel(state)
    if travel and "itinerary" in travel:
        progress = journey_progress(travel)
        by_id = node_index(world_data)
        dest = (by_id.get(travel.get("destination_node_id")) or {}).get("name") \
            or "the destination"
        guidance += (
            f"\n\nA journey toward {dest} is underway: about "
            f"{progress['minutes_traveled']} of ~{progress['eta_minutes']} "
            f"in-world minutes covered so far "
            f"(~{progress['minutes_remaining']} remain). Report the minutes of "
            f"actual travel this turn's story covered in travel_minutes_covered, "
            f"and set travel_completed only if the narration finished the whole "
            f"remaining trip (e.g. it ended with the player arriving)."
        )
    # The reader only extracts movement — the storyteller-notes channel is
    # deliberately withheld from this call (N5 of the layering plan).
    location_context = build_location_context(host, state, world_data,
                                              include_storyteller_notes=False)
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
