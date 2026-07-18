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

    Returns ``{"node_id", "name", "type", "label_description", "description",
    "reason"}`` or None when the world has no free slot or the call fails —
    the caller then falls back to picking the best existing candidate.
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
        "- Otherwise pick the position whose region/layer/type fits the request best."
    )
    user_msg = f"""World premise: {lore.get('premise', '')}
Genre: {rules.get('genre', '')} | Tone: {rules.get('tone', '')}

Regions:
{regions_block}
{layers_section}
Player's location request: "{preference}"
What they want: {wanted or preference}
{anchor_line}
Available unnamed map positions:
{slots_block}

{placement_rules}

Found a new location matching the request at the best-fitting position. Return JSON:
{{"node_id": "<one of the ids above>", "name": "...", "type": "settlement" or "landmark",
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
    node_id = str(authored.get("node_id", ""))
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
