"""Pre-storyteller travel intent.

Before the storyteller writes, one fast LLM call reads the player's input
and decides whether they are setting out on a journey (or redirecting one).
If so, the destination is resolved to a map node, the trip's duration is
estimated in in-world minutes, and the journey starts immediately — so the
storyteller writes *knowing* the destination and ETA, free to narrate the
trip at whatever pace fits (the post-storyteller reader measures how much
of it the narration actually covered).

Destination resolution has two strategies, switched by the
``world.destination_resolution`` setting so they can be compared in play:

- ``roster``: the intent call sees every revealed, named node in the world,
  grouped by map hierarchy, and picks the node id directly.
- ``semantic``: the intent call returns the destination in the player's own
  words; that text is embedded and matched against the save's world RAG
  index (``memory.search_world``), and a second small call picks from the
  shortlist. Only nodes that are already embedded (named + described) can
  be found this way.
"""

import json
import re

from . import routing as _routing
from .travel import plan_journey
from .worldspace import (
    breadcrumb,
    fringe_node_ids,
    get_travel,
    map_nodes,
    maps_by_id,
    node_index,
    player_map_id,
)


def _parse_json(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass
    return {}


def _resolution_mode(host) -> str:
    try:
        settings = host._services.get("settings") if host._services else None
        mode = settings.get("world.destination_resolution") if settings else None
        if mode in ("roster", "semantic"):
            return mode
    except Exception:
        pass
    return "semantic"


def _current_location_line(state: dict, world_data: dict) -> str:
    by_id = node_index(world_data)
    node = by_id.get(state.get("player_location_node_id")) or {}
    from .worldspace import get_map
    current_map = get_map(world_data, player_map_id(state)) or {}
    name = node.get("name") or "an unexplored spot"
    return f"{name} ({node.get('type', 'location')}) on {current_map.get('label', 'this map')}"


def _destination_roster(world_data: dict, state: dict) -> list[str]:
    """Every named node the player could head for — revealed ones plus the
    name-only fringe beside them (their names show on the map) — grouped
    under its map's hierarchy breadcrumb. No caps — resolution must see the
    whole world."""
    revealed = set(state.get("revealed_node_ids") or [])
    visible = revealed | fringe_node_ids(world_data, revealed)
    current = state.get("player_location_node_id")
    lines = []
    for map_id in maps_by_id(world_data):
        entries = []
        for n in map_nodes(world_data, map_id):
            if not n.get("name") or n.get("id") == current:
                continue
            if n.get("id") not in visible:
                continue
            entries.append(f"  - {n['id']}: {n['name']} ({n.get('type', 'location')})")
        if entries:
            crumb = " > ".join(
                m.get("label", m.get("map_id", "")) for m in breadcrumb(world_data, map_id))
            lines.append(f"{crumb}:")
            lines.extend(entries)
    return lines


async def _semantic_candidates(engine, world_data: dict, state: dict,
                               destination_text: str) -> list[dict]:
    """Shortlist nodes for a free-text destination via the world RAG index."""
    memory = getattr(engine, "memory", None)
    if memory is None or not memory.has_world_index():
        return []
    vector = await engine.llm.get_embedding(
        destination_text,
        inspector_ctx={"call_type": "embedding", "step": "travel_intent:destination",
                       "module_source": "wb_worldgen"})
    revealed = set(state.get("revealed_node_ids") or [])
    visible = revealed | fringe_node_ids(world_data, revealed)
    current = state.get("player_location_node_id")
    by_id = node_index(world_data)
    candidates = []
    for entry in memory.search_world(vector, limit=12):
        if entry.get("source_type") != "node":
            continue
        nid = entry.get("source_id")
        node = by_id.get(nid)
        if node is None or nid == current or nid not in visible:
            continue
        if any(c.get("id") == nid for c in candidates):
            continue
        candidates.append(node)
    return candidates


ETA_GUIDANCE = (
    "eta_minutes is your estimate of the TOTAL in-world minutes the whole "
    "journey plausibly takes in this world, given the distance implied and the "
    "means of travel (a walk across town ~15-45, a commuter train ride ~20-60, "
    "a day's ride ~600)."
)

NOT_TRAVEL = (
    "NOT travel: moving around within the current location (another room, "
    "across the street), merely mentioning or remembering a place, or planning "
    "a trip without setting out now."
)


async def evaluate_travel_intent(host, state: dict, sdk) -> dict:
    """Detect and start a journey from this turn's player input.

    Returns {} when the input declares no travel; otherwise a partial
    gather-context result: a <travel_plan> context_string for the
    storyteller and, when a journey starts, the travel record under
    module_data (+ module_data_replace)."""
    world_data = state.get("world_data")
    input_text = (state.get("input_text") or "").strip()
    if not world_data or not input_text or input_text.startswith("/"):
        return {}
    engine = host._services.get("engine") if host._services else None
    llm = getattr(engine, "llm", None)
    if llm is None or getattr(llm, "mode", "") == "mock":
        return {}

    travel = get_travel(state)
    journey_line = ""
    if travel and travel.get("destination_node_id"):
        by_id = node_index(world_data)
        dest = (by_id.get(travel["destination_node_id"]) or {}).get("name") \
            or travel["destination_node_id"]
        journey_line = (f"\nA journey is already underway toward {dest}. Only "
                        f"report traveling=true if the player is changing "
                        f"destination or setting out somewhere NEW.")

    mode = _resolution_mode(host)
    location_line = _current_location_line(state, world_data)

    if mode == "roster":
        roster = _destination_roster(world_data, state)
        roster_block = "\n".join(roster) if roster else "(no known destinations)"
        prompt = f"""The player of a role-played story just typed their action. Decide if it sets out on a journey to another location.

Player input: {input_text}
The player is currently at: {location_line}{journey_line}

{NOT_TRAVEL}

Known destinations (pick destination_node_id from these ids; "none" if the place isn't listed):
{roster_block}

{ETA_GUIDANCE}

Respond ONLY with JSON: {{"traveling": true/false, "destination_node_id": "<id or none>", "destination": "<the place in the player's words>", "transport": "<walking, train, horse, ...>", "eta_minutes": <integer>}}"""
        result = _parse_json(await llm.simple_completion(
            messages=[{"role": "system", "content": "You are a story analysis AI that strictly outputs JSON."},
                      {"role": "user", "content": prompt}],
            model=llm.reader_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "reader", "step": "travel_intent",
                           "module_source": "wb_worldgen"}))
        if not result.get("traveling"):
            return {}
        node_id = str(result.get("destination_node_id") or "").strip()
        destination_text = str(result.get("destination") or "").strip()
        transport = str(result.get("transport") or "").strip()
        eta = result.get("eta_minutes")
    else:
        prompt = f"""The player of a role-played story just typed their action. Decide if it sets out on a journey to another location.

Player input: {input_text}
The player is currently at: {location_line}{journey_line}

{NOT_TRAVEL}

Respond ONLY with JSON: {{"traveling": true/false, "destination": "<where they are going, in the player's words>", "transport": "<walking, train, horse, ...>"}}"""
        result = _parse_json(await llm.simple_completion(
            messages=[{"role": "system", "content": "You are a story analysis AI that strictly outputs JSON."},
                      {"role": "user", "content": prompt}],
            model=llm.reader_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "reader", "step": "travel_intent",
                           "module_source": "wb_worldgen"}))
        if not result.get("traveling"):
            return {}
        destination_text = str(result.get("destination") or "").strip()
        transport = str(result.get("transport") or "").strip()
        node_id, eta = "", None
        if destination_text:
            candidates = await _semantic_candidates(engine, world_data, state, destination_text)
            if candidates:
                lines = "\n".join(
                    f"  - {c['id']}: {c.get('name', '')} ({c.get('type', 'location')}) — "
                    f"{(c.get('description') or '')[:160]}"
                    for c in candidates)
                pick_prompt = f"""The player sets out for "{destination_text}"{f' by {transport}' if transport else ''}.
They are currently at: {location_line}

Which of these known places is that destination? Pick "none" if none of them is it.
{lines}

{ETA_GUIDANCE}

Respond ONLY with JSON: {{"destination_node_id": "<id or none>", "eta_minutes": <integer>}}"""
                pick = _parse_json(await llm.simple_completion(
                    messages=[{"role": "system", "content": "You are a story analysis AI that strictly outputs JSON."},
                              {"role": "user", "content": pick_prompt}],
                    model=llm.reader_model,
                    response_format={"type": "json_object"},
                    inspector_ctx={"call_type": "reader", "step": "travel_intent:resolve",
                                   "module_source": "wb_worldgen"}))
                node_id = str(pick.get("destination_node_id") or "").strip()
                eta = pick.get("eta_minutes")

    if not node_id or node_id.lower() in ("none", "null", ""):
        # Traveling somewhere unresolvable — leave movement to the reader's
        # normal mechanics (improvised transitions can author new places).
        return {}
    by_id = node_index(world_data)
    if node_id not in by_id:
        return {}
    if travel and travel.get("destination_node_id") == node_id:
        return {}  # already journeying there; the travel context covers it

    planned = plan_journey(host, state, world_data, node_id,
                           eta_minutes=eta, transport=transport)
    if planned is None:
        return {}
    dest_name = (by_id.get(node_id) or {}).get("name") or destination_text or node_id
    if planned[0] == "instant":
        return {"context_string": (
            "<travel_plan>\n"
            f"The player is heading to {dest_name}, which is within immediate "
            f"reach — narrate the move freely; it completes this scene.\n"
            "</travel_plan>")}

    record = planned[1]
    from . import expansion as _expansion
    if _expansion.site_mode(host) == "prefetch":
        _expansion.maybe_expand_node(host, state, node_id)
    route_lines = _routing.describe_itinerary(world_data, record["itinerary"])
    plan_parts = ["<travel_plan>"]
    verb = "changes course and sets out" if travel else "sets out"
    header = f"The player {verb} for {dest_name}"
    if transport:
        header += f" by {transport}"
    header += f" — roughly {record['eta_minutes']} in-world minutes of travel."
    plan_parts.append(header)
    if route_lines:
        plan_parts.append("The route: " + "; then ".join(route_lines) + ".")
    plan_parts.append(
        "Narrate this journey at whatever pace feels natural — a single line, a "
        "scene on the road, or the entire trip including the arrival if it "
        "passes uneventfully. The world tracks progress by the in-world minutes "
        "your narration spends traveling; an unfinished journey simply "
        "continues next turn.")
    plan_parts.append("</travel_plan>")
    return {
        "context_string": "\n".join(plan_parts),
        "module_data": {"wb_worldgen": {"travel": record, "site_position": None}},
        "module_data_replace": ["travel", "site_position"],
    }
