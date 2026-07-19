"""Start-location discovery + LLM-assisted selection for a saved world."""

import json
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)


def _find_node_region(node_id: str, compiled: dict) -> str:
    from wbworldgen.worldgen import mapspace as _ms
    for m in _ms.maps_by_id(compiled).values():
        for region in m.get("regions", []) or []:
            if node_id in region.get("node_ids", []):
                return region.get("region_name", "")
    for node in _ms.all_nodes(compiled):
        if node.get("id") == node_id:
            desc = node.get("description", "")
            for region_data in compiled.get("regions", {}).get("regions", []):
                region_name = region_data.get("name", "")
                if region_name and region_name.lower() in desc.lower():
                    return region_name
    return ""


def _candidate(node: dict, map_id: str, map_label: str, compiled: dict) -> dict:
    return {
        "node_id": node.get("id"),
        "name": node.get("name"),
        "type": node.get("type", "location"),
        "description": node.get("description", "")[:300],
        "region": _find_node_region(node.get("id"), compiled),
        "map_id": map_id,
        "map_label": map_label,
    }


def get_start_locations(compiled: dict) -> list[dict]:
    from wbworldgen.worldgen import mapspace as _ms
    nodes = []
    for mid, m in _ms.maps_by_id(compiled).items():
        for node in m.get("nodes", []):
            nodes.append((node, mid, m.get("label", mid)))

    candidates = [
        _candidate(entry[0], entry[1], entry[2], compiled)
        for entry in nodes
        if entry[0].get("type") in ("settlement", "landmark") and entry[0].get("name")
    ]
    if not candidates:
        candidates = [_candidate(entry[0], entry[1], entry[2], compiled)
                      for entry in nodes if entry[0].get("name")]
    return candidates


def map_candidates(compiled: dict, map_ids: list[str]) -> list[dict]:
    """Start candidates drawn from the given maps only — one level of the
    hierarchical descent. Top-level maps keep the settlement/landmark
    preference (minor named waypoints only appear when nothing better
    exists); anchored child maps (interiors) offer every named node, since
    rooms and halls carry types of their own."""
    from wbworldgen.worldgen import mapspace as _ms
    out = []
    for mid in map_ids:
        m = _ms.get_map(compiled, mid)
        if m is None:
            continue
        named = [n for n in m.get("nodes", []) if n.get("name")]
        if not m.get("anchor_node_id"):
            preferred = [n for n in named
                         if n.get("type") in ("settlement", "landmark")]
            named = preferred or named
        label = m.get("label", mid)
        out.extend(_candidate(n, mid, label, compiled) for n in named)
    return out


def find_start_candidate(compiled: dict, node_id: str) -> Optional[dict]:
    """Candidate-shaped entry for one named node, wherever it lives — used to
    resolve an explicitly pre-picked start node id (which may point into an
    interior map that the type-filtered candidate list never offers)."""
    from wbworldgen.worldgen import mapspace as _ms
    for mid, m in _ms.maps_by_id(compiled).items():
        for node in m.get("nodes", []):
            if node.get("id") == node_id and node.get("name"):
                return _candidate(node, mid, m.get("label", mid), compiled)
    return None


async def llm_pick_start_location(compiled: dict, candidates: list[dict], preference: str, llm,
                                  allow_no_match: bool = False,
                                  inside_of: dict = None,
                                  scene_hint: str = "") -> Optional[dict]:
    """Pick the candidate best matching the player's preference.

    With ``allow_no_match`` the LLM may instead declare that nothing genuinely
    fits, returning ``{"no_match": True, "wanted": "<short spec>"}`` so the
    caller can author a fitting start location on demand instead of forcing
    the least-bad existing one.

    ``inside_of`` marks a descent step: the candidates are the places inside
    that already-chosen location, and the LLM may answer ``EXTERIOR``
    (returned as ``{"exterior": True}``) when the scene happens at the
    location itself rather than at any interior spot. ``scene_hint`` is the
    part named by the previous level's pick ("the living room"), carried down
    as extra context.

    A pick may carry an ``inside_hint`` — the LLM naming the specific part of
    the chosen location where the scene takes place — which the caller uses
    to keep descending (or to expand an interior that doesn't exist yet).
    """
    if not candidates:
        return None
    if not preference or preference.lower() == "random":
        if len(candidates) == 1:
            return candidates[0]
        return random.choice(candidates)
    if len(candidates) == 1 and not allow_no_match and inside_of is None:
        return candidates[0]

    world_name = compiled.get("lore", {}).get("world_name", "the world")
    world_premise = compiled.get("lore", {}).get("premise", "")
    candidates_summary = "\n".join(
        f"- {c['node_id']}: {c['name']} ({c['type']}) in {c['region']} — {c['description'][:200]}"
        for c in candidates
    )
    system = (
        f"You are helping a player choose a starting location in the world of {world_name}. "
        "Pick the best match based on their preference. Output only valid JSON."
    )
    parent_name = (inside_of or {}).get("name", "")
    inside_context = ""
    exterior_instruction = ""
    if inside_of is not None:
        inside_context = (
            f"\nThe start is at {parent_name}; the locations listed below are "
            "the known places INSIDE it. Choose the spot where the opening "
            "scene takes place.\n"
        )
        candidates_summary += (
            f"\n- EXTERIOR: none of the above — the opening scene happens at "
            f"{parent_name} itself (or just outside it), not at any specific "
            "spot inside"
        )
        exterior_instruction = (
            '\n"EXTERIOR" is a valid node_id: use it when the scene plays out at '
            f"{parent_name} in general or outside it, rather than at a listed spot."
        )
    hint_line = (
        f'\nThe opening scene is specifically at: "{scene_hint}".\n'
        if scene_hint else "")
    inside_capture = (
        "\nIf the opening scene takes place in a specific PART of your chosen "
        "location that is not itself in the list (a particular room, cellar, "
        'rooftop, a building on its grounds...), add "inside": "<that part in a '
        'few words>" to your answer.'
    )
    if allow_no_match:
        if inside_of is not None:
            no_match_instruction = (
                "\nIf the scene happens at a specific spot inside "
                f"{parent_name} that is NOT in the list (and EXTERIOR does not "
                'fit either), return {"node_id": "NONE", "wanted": "one short '
                'phrase describing that spot"} and it will be created.'
            )
        else:
            no_match_instruction = (
                "\nIf NONE of the locations genuinely fits the preference, do not force a poor match: "
                'return {"node_id": "NONE", "wanted": "one short phrase describing the kind of place the player wants"}.'
                "\nBut a preference naming a PART of a listed location (its rooftop, storage building, "
                "courtyard, a room inside it) is NOT a no-match — pick that location and name the part "
                'in "inside"; the scene plays out in that part of it.'
            )
    else:
        no_match_instruction = ""
    user_msg = f"""World premise: {world_premise}

Player's starting location preference: "{preference}"
{inside_context}{hint_line}
Available locations:
{candidates_summary}

Pick the single best matching location. Return JSON: {{"node_id": "...", "name": "...", "reason": "one sentence why"}}{exterior_instruction}{inside_capture}{no_match_instruction}"""
    try:
        content = await llm.simple_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            model=llm.reader_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "reader", "step": "world_build:pick_start"},
        )
        choice = json.loads(content)
        chosen_id = choice.get("node_id", "")
        if inside_of is not None and str(chosen_id).upper() == "EXTERIOR":
            return {"exterior": True}
        if allow_no_match and str(chosen_id).upper() == "NONE":
            return {"no_match": True, "wanted": choice.get("wanted") or preference}
        for c in candidates:
            if c["node_id"] == chosen_id:
                c["reason"] = choice.get("reason", "")
                hint = str(choice.get("inside", "") or "").strip()
                if hint:
                    c["inside_hint"] = hint
                return c
        return candidates[0]
    except Exception as e:
        logger.error(f"LLM start location pick failed: {e}")
        if inside_of is not None:
            # A failed descent pick must not strand the start at a random
            # room — staying at the (already well-chosen) parent is safe.
            return {"exterior": True}
        return random.choice(candidates)


def _distance(a: dict, b: dict) -> float:
    return ((a.get("x", 0.0) - b.get("x", 0.0)) ** 2
            + (a.get("y", 0.0) - b.get("y", 0.0)) ** 2) ** 0.5


def _typical_leg(map_record: dict) -> float:
    """The map's typical route-leg length (mean edge distance) — the distance
    unit the placement rules reason in. Matches the spacing rule used when
    positioning grown nodes (maps_expand._grow_position)."""
    distances = [e.get("distance") for e in (map_record.get("edges") or [])
                 if e.get("distance")]
    if distances:
        return sum(distances) / len(distances)
    cfg = map_record.get("config") or {}
    width = float(cfg.get("map_width", 100.0) or 100.0)
    height = float(cfg.get("map_height", 100.0) or 100.0)
    return min(width, height) / 8


def _found_new_node(compiled: dict, authored: dict,
                    fallback_anchor_id: str = None) -> Optional[dict]:
    """Resolve a ``{"node_id": "NEW", "near_node_id": ...}`` answer into a
    fully-built map node placed one typical route leg beside its named anchor,
    plus the edge linking them. Pure computation — nothing is persisted and
    ``compiled`` is not mutated. Returns None on unusable output (unknown or
    unnamed anchor, missing name) so callers fall back to existing behavior."""
    from wbworldgen.worldgen import mapspace as _ms
    near_id = str(authored.get("near_node_id", "")).strip() or (fallback_anchor_id or "")
    anchor = anchor_map = anchor_map_id = None
    for mid, m in _ms.maps_by_id(compiled).items():
        for n in m.get("nodes", []):
            if n.get("id") == near_id:
                anchor, anchor_map, anchor_map_id = n, m, mid
                break
        if anchor is not None:
            break
    if anchor is None or not anchor.get("name"):
        logger.warning("NEW location answer names no usable anchor (near_node_id=%r)", near_id)
        return None
    name = str(authored.get("name", "")).strip()
    if not name:
        logger.warning("NEW location answer carries no name; falling back")
        return None
    loc_type = str(authored.get("type", "")).strip().lower()
    if loc_type not in ("settlement", "landmark"):
        loc_type = "landmark"
    description = (str(authored.get("description", "")).strip()
                   or str(authored.get("label_description", "")).strip())

    from wbworldgen.worldgen.expansion.maps_expand import MapExpansionEngine
    x, y = MapExpansionEngine._grow_position(anchor_map, [anchor])
    taken = {n.get("id") for n in _ms.all_nodes(compiled)}
    k = len(anchor_map.get("nodes") or []) + 1
    while f"{anchor_map_id}:g{k}" in taken:
        k += 1
    node = {
        "id": f"{anchor_map_id}:g{k}",
        "name": name,
        "type": loc_type,
        "description": description,
        "label_description": str(authored.get("label_description", "")).strip(),
        "x": x,
        "y": y,
    }
    if anchor.get("region"):
        node["region"] = anchor["region"]
    edge = {"from": anchor["id"], "to": node["id"],
            "distance": round(max(_distance(node, anchor), 1.0), 2)}
    return {
        "node_id": node["id"],
        "name": name,
        "type": loc_type,
        "label_description": node["label_description"],
        "description": description,
        "reason": str(authored.get("reason", "")).strip(),
        "map_id": anchor_map_id,
        "new_node": node,
        "new_edges": [edge],
    }


def _unnamed_slots(compiled: dict, limit: int = 60, anchor_node_id: str = None) -> list[dict]:
    """Unnamed map nodes that a brand-new start location could be founded on,
    tagged with their layer id and annotated with the nearest named places
    (``near_named``: name + map-unit distance) so the picker knows what each
    spot is actually close to.

    With an ``anchor_node_id`` (the player's position, or the place a request
    is relative to) slots on the anchor's map come first, closest to the
    anchor first, and carry ``anchor_distance``; otherwise most important
    (best-connected) first."""
    from wbworldgen.worldgen import mapspace as _ms
    slots = []
    named_by_map = {}
    anchor = None
    for mid, m in _ms.maps_by_id(compiled).items():
        for n in m.get("nodes", []):
            if n.get("name"):
                named_by_map.setdefault(mid, []).append(n)
            else:
                slots.append({**n, "map_id": mid, "map_label": m.get("label", mid)})
            if anchor_node_id and n.get("id") == anchor_node_id:
                anchor = {**n, "map_id": mid}
    for slot in slots:
        by_dist = sorted(named_by_map.get(slot["map_id"], []), key=lambda n: _distance(slot, n))
        slot["near_named"] = [
            {"name": n["name"], "distance": _distance(slot, n)} for n in by_dist[:3]
        ]
        if anchor and slot["map_id"] == anchor["map_id"]:
            slot["anchor_distance"] = _distance(slot, anchor)
    if anchor:
        slots.sort(key=lambda s: (0, s["anchor_distance"]) if "anchor_distance" in s
                   else (1, -s.get("importance", 0)))
    else:
        slots.sort(key=lambda n: -n.get("importance", 0))
    return slots[:limit]


async def generate_start_location(compiled: dict, preference: str, wanted: str, llm,
                                  anchor_node_id: str = None) -> Optional[dict]:
    """Author a brand-new location matching the player's request on one of
    the world's unnamed map positions (one full-attention LLM call).

    ``anchor_node_id`` is where the request is being made from (the player's
    current node during play): slots are then offered nearest-first with
    distances, so a place described relative to somewhere ("the school's
    storage building") lands next to it instead of across the map.

    The prompt carries distance limits (in the map's typical route-leg unit):
    when no offered position is close enough or fitting, the LLM may answer
    ``{"node_id": "NEW", "near_node_id": ...}`` and a brand-new node is built
    one route leg beside that named place instead of claiming a slot. A place
    that is really INSIDE an existing site may be redirected across the
    boundary with ``{"belongs_inside": "<node id>"}`` — returned as-is for
    the caller to grow that site's interior instead.

    Returns ``{"node_id", "name", "type", "label_description", "description",
    "reason"}`` — plus ``{"map_id", "new_node", "new_edges"}`` when a NEW node
    was founded (nothing persisted; the caller appends it) — or None when the
    world has no free slot or the call fails, in which case the caller falls
    back to picking the best existing candidate.
    """
    slots = _unnamed_slots(compiled, anchor_node_id=anchor_node_id)
    if not slots:
        return None

    lore = compiled.get("lore", {})
    rules = compiled.get("rules", {})
    regions = compiled.get("regions", {}).get("regions", [])
    from wbworldgen.worldgen import mapspace as _ms
    world_maps = _ms.maps_by_id(compiled)

    regions_block = "\n".join(
        f"- {r.get('name', '')}: terrain {r.get('terrain', 'unknown')[:150]}; climate {r.get('climate', 'unknown')[:100]}"
        for r in regions if r.get("name")
    ) or "- (no region details)"
    layers_block = "\n".join(
        f"- {mid}: {m.get('label', '')} ({m.get('level_type', 'surface')}) — {m.get('description', '')[:150]}"
        for mid, m in world_maps.items()
    ) if len(world_maps) > 1 else ""
    layers_section = f"\nWorld maps:\n{layers_block}\n" if layers_block else ""

    def _slot_line(n):
        parts = [f"- {n.get('id')}: type {n.get('type', 'waypoint')}"]
        if n.get("region"):
            parts.append(f"region {n['region']}")
        if n.get("map_id") and n.get("map_id") != "root":
            parts.append(f"map {n['map_id']}")
        near = n.get("near_named") or []
        if near:
            parts.append("near " + ", ".join(
                f"{e['name']} ({e['distance']:.0f})" for e in near))
        if "anchor_distance" in n:
            parts.append(f"distance from player {n['anchor_distance']:.0f}")
        return ", ".join(parts)

    slots_block = "\n".join(_slot_line(n) for n in slots)
    world_name = lore.get("world_name", "the world")

    # Named places (with ids) double as founding anchors for the NEW answer;
    # the typical route leg is the unit the distance rules speak in.
    slot_maps = {s.get("map_id") for s in slots}
    named_lines = []
    for mid, m in world_maps.items():
        for n in m.get("nodes", []):
            if n.get("name"):
                line = f"- {n['id']}: {n['name']} ({n.get('type', 'place')})"
                if len(world_maps) > 1:
                    line += f", map {mid}"
                named_lines.append(line)
    named_block = "\n".join(named_lines) or "- (none yet)"
    leg_lines = [
        f"- {m.get('label', mid)}: about {_typical_leg(m):.0f} map units"
        for mid, m in world_maps.items() if mid in slot_maps
    ]
    leg_block = "\n".join(leg_lines)

    anchor_name = ""
    if anchor_node_id:
        from wbworldgen.worldgen import mapspace as _ms2
        anchor_node = next((n for n in _ms2.all_nodes(compiled)
                            if n.get("id") == anchor_node_id), None)
        if anchor_node is not None:
            anchor_name = anchor_node.get("name") or anchor_node_id
    anchor_line = (
        f'\nThe player is currently at: {anchor_name}. Distances are map units.\n'
        if anchor_name else "")

    system = (
        f"You are helping place a new location for a player's story in the world of {world_name}. "
        "No existing named location matches the request, so you will found a brand-new location at one of "
        "the available unnamed map positions. Output only valid JSON."
    )
    placement_rules = (
        "Placement matters — pick the position whose surroundings fit the request:\n"
        "- If the request is a part of, or right by, an existing named place (its storage "
        "building, rooftop, gate, outskirts...), pick the position CLOSEST to that place — "
        "check each position's near list.\n"
        "- Otherwise, if the player's current position is given, prefer a position near them "
        "unless the request implies somewhere farther away.\n"
        "- Otherwise pick the position whose region/layer/type fits the request best.\n"
        "Distance limits — a position only qualifies when it is genuinely close enough "
        "(a typical route leg is the average distance between neighboring places):\n"
        "- A part of a named place (its rooftop, storage building, gate...): within about "
        "ONE route leg of that place.\n"
        "- Right by / just outside a named place, or somewhere near the player: within "
        "about TWO route legs.\n"
        "- Somewhere in a region: any position in that region.\n"
        "- No stated whereabouts: whichever position fits the place's nature best.\n"
        "Within those limits the best-FITTING position wins — closeness qualifies a "
        "position, fit ranks it.\n"
        "If NO listed position qualifies, do not force a distant or unfitting one: return "
        '{"node_id": "NEW", "near_node_id": "<id of the named place it belongs beside>"} '
        "with the other fields as usual, and a brand-new position will be created "
        "directly beside that place.\n"
        "If the requested place is actually INSIDE one of the existing named places — on "
        "its premises, somewhere you could walk to without leaving it (a room, hall, "
        "rooftop, cellar, courtyard building...) — do not place it on this map at all: "
        'return {"belongs_inside": "<that named place\'s id>"} and nothing else, and it '
        "will be created inside that place instead."
    )
    user_msg = f"""World premise: {lore.get('premise', '')}
Genre: {rules.get('genre', '')} | Tone: {rules.get('tone', '')}

Regions:
{regions_block}
{layers_section}
Player's location request: "{preference}"
What they want: {wanted or preference}
{anchor_line}
Existing named places:
{named_block}

Typical route leg:
{leg_block}

Available unnamed map positions:
{slots_block}

{placement_rules}

Found a new location matching the request at the best-fitting position. Return JSON:
{{"node_id": "<one of the ids above, or NEW>", "near_node_id": "<only with NEW: a named place id>",
"name": "...", "type": "settlement" or "landmark",
"label_description": "one-line label", "description": "2-3 sentence flavor description",
"reason": "one sentence why this position fits",
"scene_inside": "<ONLY when the requested scene happens at a specific spot INSIDE the new location (a room, hall, deck...): that spot in a few words — otherwise omit>"}}"""
    try:
        content = await llm.simple_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            model=llm.reader_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "reader", "step": "world_build:generate_start"},
        )
        authored = json.loads(content)
    except Exception as e:
        logger.error(f"Start location generation failed: {e}")
        return None

    slot_ids = {str(s.get("id")) for s in slots}
    inside_ref = str(authored.get("belongs_inside", "")).strip()
    if inside_ref:
        # The place is a sub-location of an existing site, not a map position
        # of its own — hand the caller the redirect (it grows that interior).
        from wbworldgen.worldgen import mapspace as _ms3
        parent = next((n for n in _ms3.all_nodes(compiled)
                       if n.get("id") == inside_ref), None)
        if parent is not None and parent.get("name"):
            return {"belongs_inside": inside_ref}
        logger.warning("belongs_inside answer names no known place (%r); ignoring", inside_ref)
    scene_inside = str(authored.get("scene_inside", "") or "").strip()
    node_id = str(authored.get("node_id", ""))
    if node_id.upper() == "NEW":
        founded = _found_new_node(compiled, authored, fallback_anchor_id=anchor_node_id)
        if founded is not None and scene_inside:
            founded["scene_inside"] = scene_inside
        return founded
    name = str(authored.get("name", "")).strip()
    if node_id not in slot_ids or not name:
        logger.warning("Start location generation returned invalid node_id/name; falling back")
        return None
    loc_type = str(authored.get("type", "")).strip().lower()
    if loc_type not in ("settlement", "landmark"):
        loc_type = "landmark"
    description = str(authored.get("description", "")).strip() or str(authored.get("label_description", "")).strip()
    result = {
        "node_id": node_id,
        "name": name,
        "type": loc_type,
        "label_description": str(authored.get("label_description", "")).strip(),
        "description": description,
        "reason": str(authored.get("reason", "")).strip(),
    }
    if scene_inside:
        result["scene_inside"] = scene_inside
    return result


# --- the full start pick (facade-driven orchestration) -----------------------
# These compose the WorldBuilder facade's public operations (compile_world /
# load_world / expand_node / grow_child_map) and read engine dependencies
# through ``builder.services`` — never facade privates.

#: Safety net for the start-location descent: author→expand→author chains
#: deeper than this are pathological, not worlds.
MAX_START_DESCENT = 5


async def pick_start_location(builder, world_id: str, preference: str, llm):
    """Choose where the story starts, descending the map hierarchy.

    Level by level (world → city → building → room ...) the best match
    for the preference is picked among one map's places; when the chosen
    place has a child map the descent continues inside it (with an
    EXTERIOR answer to stop at the place itself). At any level a no-match
    authors a brand-new location — on the level's unnamed map positions
    (or founded beside an anchor) at the top, or grown onto the interior
    map below — and when the scene calls for a spot inside a place with
    no interior map yet, the interior is expanded on the spot with that
    spot required to exist (``must_include``).

    The returned candidate may carry ``ancestor_node_ids`` (the container
    chain walked down, for fog-of-war reveal) and ``world_modified``
    (something was authored or expanded; callers holding a compiled world
    should recompile)."""
    from wbworldgen.worldgen import mapspace as _mapspace

    compiled = builder.compile_world(builder.load_world(world_id))
    live = llm is not None and getattr(llm, "mode", "mock") != "mock"

    top_map_ids = [mid for mid, m in _mapspace.maps_by_id(compiled).items()
                   if not m.get("anchor_node_id")]
    candidates = (map_candidates(compiled, top_map_ids)
                  or get_start_locations(compiled))
    result = await llm_pick_start_location(
        compiled, candidates, preference, llm, allow_no_match=live)

    world_modified = False
    if isinstance(result, dict) and result.get("no_match"):
        wanted = result.get("wanted", "")
        authored = await generate_start_location(
            compiled, preference, wanted, llm)
        if authored and authored.get("belongs_inside"):
            # The requested start is inside an existing place — start the
            # descent at that place, with the request as the spot to find
            # (or create) inside it.
            inside = next((c for c in candidates
                           if c.get("node_id") == authored["belongs_inside"]), None)
            if inside is not None:
                result = dict(inside)
                result["inside_hint"] = wanted
            else:
                authored = None
                result = None
        elif authored:
            scene_inside = authored.get("scene_inside", "")
            result = _persist_generated_start(builder, world_id, authored)
            world_modified = True
            compiled = builder.compile_world(builder.load_world(world_id))
            if scene_inside:
                result["inside_hint"] = scene_inside
        if not authored:
            # Generation failed — settle for the best existing candidate.
            result = await llm_pick_start_location(
                compiled, candidates, preference, llm)

    if result is None or not live or not preference \
            or preference.lower() == "random":
        return result

    result = await _descend_start_location(
        builder, world_id, compiled, result, preference, llm)
    if world_modified:
        result["world_modified"] = True
    return result


async def _descend_start_location(builder, world_id: str, compiled: dict,
                                  result: dict, preference: str, llm) -> dict:
    """Walk the picked start down the map hierarchy (see
    ``pick_start_location``). Sets ``world_modified`` on the result
    when an interior was expanded or a room grown along the way."""
    from wbworldgen.worldgen import mapspace as _mapspace
    from wbworldgen.worldgen.expansion import maps_expand as _maps_expand

    ancestors = []
    for _ in range(MAX_START_DESCENT):
        node_id = result.get("node_id")
        map_id = result.get("map_id") or _mapspace.ROOT_MAP_ID
        hint = str(result.pop("inside_hint", "") or "").strip()
        children = _mapspace.children_by_anchor(compiled).get((map_id, node_id))
        if not children:
            if not hint:
                break
            # The scene wants a spot inside a place with no interior map:
            # expand it now, requiring that spot to exist on the new map.
            node = _mapspace.node_index(compiled).get(node_id)
            if not _maps_expand.is_expandable(compiled, map_id, node):
                break
            try:
                bundle = await builder.expand_node(
                    world_id, map_id, node_id, must_include=hint)
            except Exception:
                logger.exception(
                    "start descent: interior expansion failed for %s", node_id)
                break
            record = bundle["map"]
            compiled.setdefault("maps", {})[record["map_id"]] = record
            compiled.setdefault("connections", []).extend(
                bundle.get("connections") or [])
            compiled.pop("_node_by_id", None)
            children = [record["map_id"]]
            result["world_modified"] = True
        child_id = children[0]
        sub_candidates = map_candidates(compiled, [child_id])
        if not sub_candidates:
            break
        sub = await llm_pick_start_location(
            compiled, sub_candidates, preference, llm,
            allow_no_match=True, inside_of=result, scene_hint=hint)
        if sub is None or sub.get("exterior"):
            break
        if sub.get("no_match"):
            grown = await builder.grow_child_map(
                world_id, child_id, sub.get("wanted") or hint or preference)
            node = (grown or {}).get("node")
            if node and node.get("name"):
                child_map = _mapspace.get_map(compiled, child_id) or {}
                sub = {
                    "node_id": node.get("id"),
                    "name": node.get("name"),
                    "type": node.get("type", "location"),
                    "description": node.get("description", ""),
                    "region": "",
                    "map_id": child_id,
                    "map_label": child_map.get("label", child_id),
                    "generated": True,
                }
                if grown.get("created"):
                    result["world_modified"] = True
                    compiled = builder.compile_world(builder.load_world(world_id))
            else:
                sub = await llm_pick_start_location(
                    compiled, sub_candidates, preference, llm,
                    inside_of=result, scene_hint=hint)
                if sub is None or sub.get("exterior"):
                    break
        if result.pop("world_modified", False):
            sub["world_modified"] = True
        ancestors.append(node_id)
        result = sub
    if ancestors:
        result["ancestor_node_ids"] = ancestors
    return result


async def author_location(builder, world_id: str, description: str,
                          anchor_node_id: str = None) -> dict | None:
    """Author a brand-new named location matching a free-text description
    onto one of the world's unnamed map positions (one full-attention
    call) — used when the story needs a place that doesn't exist yet
    (e.g. a teleport to a named-but-unmapped destination).
    ``anchor_node_id`` (the player's current node) makes the placement
    spatially aware: slots are offered nearest-first so a place described
    relative to here lands nearby. Returns the candidate-shaped entry
    (node_id, map_id, ...) or None when no slot fits or the call fails."""
    services = builder.services
    if not services.llm or services.llm.mode == "mock":
        return None
    compiled = services.compiled.load(world_id)
    try:
        authored = await generate_start_location(
            compiled, description, description, services.llm,
            anchor_node_id=anchor_node_id)
    except Exception:
        logger.exception("on-demand location authoring failed")
        return None
    if not authored:
        return None
    if authored.get("belongs_inside"):
        # Cross-boundary redirect: the place lives inside an existing
        # site, not on the overworld — the caller grows that interior.
        return {"belongs_inside": authored["belongs_inside"]}
    result = _persist_generated_start(builder, world_id, authored)
    services.compiled.invalidate(world_id)
    return result


def _persist_generated_start(builder, world_id: str, authored: dict) -> dict:
    """Write an on-demand location into the world — onto its claimed map
    node (name, type, description, importance bump), or, for a NEW-founded
    node, appended wholesale to its map — and return it in candidate
    shape (NEW results additionally carry ``new_node``/``new_edges``/
    ``map_id`` so play-time callers can mirror them into the session)."""
    services = builder.services
    store = services.enrichment_store
    node_id = authored["node_id"]
    importance = builder.MAJOR_IMPORTANCE_FLOOR if authored["type"] == "landmark" else 8
    new_node = authored.get("new_node")
    if new_node is not None:
        new_node["importance"] = importance
        store.append_map_node(
            world_id, authored.get("map_id", ""), new_node,
            authored.get("new_edges") or [])
    else:
        writes = {
            "name": authored["name"],
            "type": authored["type"],
            "importance": importance,
        }
        if authored.get("label_description"):
            writes["label_description"] = authored["label_description"]
        if authored.get("description"):
            writes["description"] = authored["description"]
        for field, value in writes.items():
            store.save_node_enrichment(world_id, node_id, field, value)
        store.flush_enrichment_cache(world_id)
    services.compiled.invalidate(world_id)

    compiled = builder.compile_world(builder.load_world(world_id))
    for entry in get_start_locations(compiled):
        if entry.get("node_id") == node_id:
            entry["reason"] = authored.get("reason", "")
            entry["generated"] = True
            if new_node is not None:
                entry["new_node"] = dict(new_node)
                entry["new_edges"] = [dict(e) for e in authored.get("new_edges") or []]
            return entry
    # Node fell outside the candidate filter (shouldn't happen) — return
    # the authored fields directly so the caller still gets a start.
    return {**authored, "generated": True}
