"""The dynamic movement mutation schema offered to the Reader each turn.

world_format 2: same-map destinations come from the player's CURRENT map
only; crossing to another map goes through ``player_passage`` (a connection
id from the listed exits). Hidden connections are never offered."""

from .worldspace import (
    children_by_anchor,
    connections_from,
    fringe_node_ids,
    get_map,
    map_nodes,
    map_of_node,
    node_index,
    player_map_id,
)


def _custom_transition_targets(world_data: dict, state: dict) -> list[str]:
    """Engine-enumerated endpoints for an improvised transition: the parent
    anchor, child-map entrances, adjacent-map connection far-ends, then
    visited named nodes anywhere in the world (teleports). Node ids are
    globally unique, so the option is just the node id."""
    current_map = player_map_id(state)
    current_node = state.get("player_location_node_id")
    by_id = node_index(world_data)
    revealed = set(state.get("revealed_node_ids", []))
    seen = set()
    options = []

    def _add(node_id, note):
        if not node_id or node_id in seen or node_id == current_node:
            return
        node = by_id.get(node_id)
        if node is None:
            return
        seen.add(node_id)
        label = node.get("name") or f"unexplored {node.get('type', 'spot')}"
        options.append(f"{node_id} ({label} — {note})")

    this_map = get_map(world_data, current_map) or {}
    if this_map.get("anchor_node_id"):
        parent_node = by_id.get(this_map["anchor_node_id"]) or {}
        _add(this_map["anchor_node_id"],
             f"back out to {parent_node.get('name', 'the outside')}")
    for (mid, anchor), child_ids in children_by_anchor(world_data).items():
        if mid == current_map:
            for child_id in child_ids:
                child = get_map(world_data, child_id) or {}
                for n in (child.get("nodes") or [])[:1]:
                    _add(n.get("id"), f"inside {child.get('label', child_id)}")
    for view in connections_from(world_data, current_map, include_hidden=True):
        _add(view["far"].get("node_id"), "beyond a known way")
    # Visited named places anywhere (teleport targets), nearest-listed last.
    for nid in revealed:
        node = by_id.get(nid)
        if node is not None and node.get("name") and len(options) < 15:
            far_map = get_map(world_data, map_of_node(world_data, nid) or "") or {}
            _add(nid, f"visited, on {far_map.get('label', 'this world')}")
    return options[:15]


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
    # revealed ones — and the fringe right beside them — as explicit
    # "unexplored" destinations so the player can still head toward them
    # (they get detailed on approach).
    if state and len(location_options) < 30:
        revealed = set(state.get("revealed_node_ids", []))
        reachable = revealed | fringe_node_ids(world_data, revealed)
        for n in nodes:
            if len(location_options) >= 30:
                break
            if not n.get("name") and n.get("id") in reachable:
                location_options.append(f"{n['id']} (unexplored {n.get('type', 'waypoint')})")
    if not location_options:
        location_options = ["any"]
    schema = {
        "player_location_changed": {"type": "boolean", "label": "Did the player move toward or arrive at a new location?"},
        "player_location_node_id": {
            "type": "select",
            "label": "Destination node ID",
            "options": location_options[:30],
            "description": "The node_id of a destination on the CURRENT map the player moved to or set out toward. Distant destinations are fine — the journey plays out over in-world time. For a way that leads to another map, use player_passage instead. Set only if player_location_changed is true."
        },
        "travel_interrupted": {
            "type": "boolean",
            "label": "Did the player pause an ongoing journey this turn (camping, resting, fighting, exploring a stop)?"
        },
        "travel_minutes_covered": {
            "type": "string",
            "label": "In-world minutes the player spent traveling this turn",
            "description": (
                "Integer. Your best estimate of the in-world minutes the narration "
                "spent actually MOVING the player toward their destination this turn "
                "(riding, walking, driving). Time spent at a stop — talking, resting, "
                "fighting — does not count. 0 when no journey progressed."
            ),
        },
    }
    travel = (state or {}).get("module_data", {}).get("wb_worldgen", {}).get("travel") \
        if state else None
    if travel:
        schema["travel_completed"] = {
            "type": "boolean",
            "label": "Did the narration finish the ENTIRE remaining journey?",
            "description": (
                "True when the story covered the rest of the trip — e.g. an "
                "uneventful ride narrated start to finish, ending with the player "
                "arriving. The player is placed at the destination now. Leave false "
                "if the journey is still underway when the scene ends."
            ),
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
    # Entering an unmapped place: offer an enter: token for the current node
    # when it can open into its own map (its map is created on use).
    if state:
        current_node_id = state.get("player_location_node_id")
        node = node_index(world_data).get(current_node_id)
        expandable = False
        if node is not None:
            from wbworldgen.worldgen.expansion.maps_expand import is_expandable as _is_exp
            expandable = _is_exp(world_data, player_map_id(state), node)
        if expandable:
            passage_options.append(
                f"enter:{current_node_id} (venture inside {node.get('name', current_node_id)} — its map will be created)")
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

    # Improvised transitions: a NEW way through that no listed exit covers
    # (a blown-up wall, a picked lock, a teleport spell).
    if state:
        targets = _custom_transition_targets(world_data, state)
        if targets:
            schema["custom_transition"] = {
                "type": "string",
                "label": "How the player got through by an UNLISTED way",
                "description": (
                    "ONLY when the story just established a NEW way through — blew a hole in a "
                    "wall, picked a lock on a window, cast a teleport. If a listed exit fits, use "
                    "player_passage instead. Describe the way briefly (e.g. 'lockpicked the rear "
                    "window'). Leave empty otherwise."
                ),
            }
            schema["custom_transition_target"] = {
                "type": "select",
                "label": "Where the unlisted way leads",
                "options": targets + ["none"],
                "description": "The destination of the improvised way. Set only with custom_transition.",
            }
            schema["custom_transition_becomes"] = {
                "type": "select",
                "label": "Does the new way persist?",
                "options": [
                    "one_time (leaves no usable way behind — picked locks, teleports)",
                    "open_passage (a permanent open way — the hole in the wall stays)",
                    "conditional_passage (permanent but gated — reachable only by repeating the effort)",
                ],
                "description": "What the improvised way leaves behind. Set only with custom_transition.",
            }
            schema["custom_transition_new_location"] = {
                "type": "string",
                "label": "Describe the destination if it does not exist yet",
                "description": (
                    "When the player names a destination that exists nowhere on any map (e.g. "
                    "teleporting to 'the Sunken Library'), describe it in one sentence and leave "
                    "custom_transition_target as none — the world will author it in a fitting "
                    "unexplored spot. If no spot fits, the player arrives at the nearest known "
                    "place instead; narrate accordingly."
                ),
            }

        # Secrets: hidden connections at the player's node the fiction may
        # reveal (searching the wall, finding the old map).
        hidden_here = [v for v in connections_from(
            world_data, player_map_id(state), state.get("player_location_node_id"),
            include_hidden=True) if v["connection"].get("hidden")]
        if hidden_here:
            by_id_h = node_index(world_data)
            opts = []
            for v in hidden_here[:6]:
                c = v["connection"]
                far_map_h = get_map(world_data, v["far"].get("map_id", "")) or {}
                far_node_h = by_id_h.get(v["far"].get("node_id")) or {}
                target = far_node_h.get("name") or far_map_h.get("label", "")
                opts.append(f"{c.get('id')} ({c.get('kind', 'passage')}: {c.get('name') or 'hidden way'} -> {target})")
            schema["discover_passage"] = {
                "type": "select",
                "label": "Secret way the player just DISCOVERED",
                "options": opts + ["none"],
                "description": (
                    "Set when the story has the player genuinely find one of these hidden ways "
                    "(searching, a clue, a map). It becomes a normal listed exit afterwards. "
                    "Never reveal a secret the player hasn't earned."
                ),
            }

    # Growing into the current place: the story may need a spot inside a
    # location that isn't mapped yet — inside a child map it is authored onto
    # this map; on the overworld, standing at an expandable site, it grows
    # (or first creates) that site's interior. The discriminating rule in
    # both descriptions: could you walk there without leaving the place?
    if state:
        current = get_map(world_data, player_map_id(state)) or {}
        if current.get("anchor_node_id"):
            place = current.get("label", "this location")
            schema["new_sub_location"] = {
                "type": "string",
                "label": f"NEW place inside {place} the story went to",
                "description": (
                    f"ONLY when the story takes the player to a spot inside {place} — "
                    "somewhere you could walk to without leaving it (e.g. 'the storage "
                    "building behind the school') — that no listed destination covers. "
                    "Describe it in one short sentence — it is added to this map beside "
                    "the places it belongs to and the player moves there. If a listed "
                    "destination fits, use player_location_node_id instead. Leave empty "
                    "otherwise."
                ),
            }
        elif node is not None and node.get("name") and (
                expandable or children_by_anchor(world_data).get(
                    (player_map_id(state), state.get("player_location_node_id")))):
            place = node.get("name", "this place")
            schema["new_sub_location"] = {
                "type": "string",
                "label": f"NEW place inside {place} the story went to",
                "description": (
                    f"ONLY when the story takes the player to a spot inside {place} — on "
                    "its premises, somewhere you could walk to without leaving it (e.g. "
                    "'the storage building behind the school'). Describe it in one short "
                    f"sentence — {place}'s interior gains that place and the player moves "
                    "inside to it. Leave empty otherwise."
                ),
            }
        if "new_sub_location" in schema and "custom_transition_new_location" in schema:
            schema["custom_transition_new_location"]["description"] += (
                " Only for a place that is its OWN destination in the wider world — a "
                "spot inside the location the player is at belongs in new_sub_location "
                "instead."
            )
            schema["new_sub_location"]["description"] += (
                " A destination of its own that merely happens to be close belongs in "
                "custom_transition_new_location instead."
            )

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
