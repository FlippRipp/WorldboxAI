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


def get_start_locations(compiled: dict) -> list[dict]:
    from wbworldgen.worldgen import mapspace as _ms
    nodes = []
    for mid, m in _ms.maps_by_id(compiled).items():
        for node in m.get("nodes", []):
            nodes.append((node, mid, m.get("label", mid)))

    def build(entry, default_type):
        node, map_id, map_label = entry
        c = {
            "node_id": node.get("id"),
            "name": node.get("name"),
            "type": node.get("type", default_type),
            "description": node.get("description", "")[:300],
            "region": _find_node_region(node.get("id"), compiled),
            "map_id": map_id,
            "map_label": map_label,
        }
        return c

    candidates = [
        build(entry, entry[0].get("type"))
        for entry in nodes
        if entry[0].get("type") in ("settlement", "landmark") and entry[0].get("name")
    ]
    if not candidates:
        candidates = [build(entry, "location") for entry in nodes if entry[0].get("name")]
    return candidates


async def llm_pick_start_location(compiled: dict, candidates: list[dict], preference: str, llm,
                                  allow_no_match: bool = False) -> Optional[dict]:
    """Pick the candidate best matching the player's preference.

    With ``allow_no_match`` the LLM may instead declare that nothing genuinely
    fits, returning ``{"no_match": True, "wanted": "<short spec>"}`` so the
    caller can author a fitting start location on demand instead of forcing
    the least-bad existing one.
    """
    if not candidates:
        return None
    if not preference or preference.lower() == "random":
        if len(candidates) == 1:
            return candidates[0]
        return random.choice(candidates)
    if len(candidates) == 1 and not allow_no_match:
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
    if allow_no_match:
        no_match_instruction = (
            "\nIf NONE of the locations genuinely fits the preference, do not force a poor match: "
            'return {"node_id": "NONE", "wanted": "one short phrase describing the kind of place the player wants"}.'
            "\nBut a preference naming a PART of a listed location (its rooftop, storage building, "
            "courtyard, a room inside it) is NOT a no-match — pick that location; the scene simply "
            "plays out in that part of it."
        )
    else:
        no_match_instruction = ""
    user_msg = f"""World premise: {world_premise}

Player's starting location preference: "{preference}"

Available locations:
{candidates_summary}

Pick the single best matching location. Return JSON: {{"node_id": "...", "name": "...", "reason": "one sentence why"}}{no_match_instruction}"""
    try:
        content = await llm.simple_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            model=llm.reader_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "reader", "step": "world_build:pick_start"},
        )
        choice = json.loads(content)
        chosen_id = choice.get("node_id", "")
        if allow_no_match and str(chosen_id).upper() == "NONE":
            return {"no_match": True, "wanted": choice.get("wanted") or preference}
        for c in candidates:
            if c["node_id"] == chosen_id:
                c["reason"] = choice.get("reason", "")
                return c
        return candidates[0]
    except Exception as e:
        logger.error(f"LLM start location pick failed: {e}")
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

    from wbworldgen.worldgen.enrichment.maps_expand import MapExpansionEngine
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
"reason": "one sentence why this position fits"}}"""
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
    node_id = str(authored.get("node_id", ""))
    if node_id.upper() == "NEW":
        return _found_new_node(compiled, authored, fallback_anchor_id=anchor_node_id)
    name = str(authored.get("name", "")).strip()
    if node_id not in slot_ids or not name:
        logger.warning("Start location generation returned invalid node_id/name; falling back")
        return None
    loc_type = str(authored.get("type", "")).strip().lower()
    if loc_type not in ("settlement", "landmark"):
        loc_type = "landmark"
    description = str(authored.get("description", "")).strip() or str(authored.get("label_description", "")).strip()
    return {
        "node_id": node_id,
        "name": name,
        "type": loc_type,
        "label_description": str(authored.get("label_description", "")).strip(),
        "description": description,
        "reason": str(authored.get("reason", "")).strip(),
    }
