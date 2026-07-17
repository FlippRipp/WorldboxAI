"""On-demand child-map expansion: a location opens into its own map.

One full-attention LLM call authors the child map's content (label, level,
locations with adjacency, connections back out); the level's generator lays
it out deterministically. The result is a real MapRecord + ConnectionRecords
— rendered, traveled and fogged like any map. Generated lazily the first
time the story approaches the location (or on explicit request), cached
write-once in the world's ``maps/`` directory.

Generator contract: every expansion MUST anchor the child to its parent with
at least one entrance connection — a child map you cannot enter or leave is
a hard error, never silently accepted.
"""

import hashlib
import logging

from wbworldgen.worldgen.generation.llm import json_retry_completion
from wbworldgen.worldgen.enrichment.context import build_enrichment_context, collect_nodes_by_layer

logger = logging.getLogger(__name__)

MAX_DEPTH = 6


def child_map_id(parent_map_id: str, node_id: str) -> str:
    """Deterministic child map id for an anchor position."""
    digest = hashlib.sha1(f"{parent_map_id}/{node_id}".encode("utf-8")).hexdigest()
    return f"m_{digest[:8]}"


def allowed_child_levels(compiled: dict, parent_map: dict) -> list[dict]:
    """Levels a child of this map may use: any level strictly below the
    parent's, plus the parent's own level when it declares ``nestable`` (a
    ship inside a station, a vault inside a castle).

    For now only levels whose generator is implemented for expansion
    (``interior``) are offered; wider-scale child generators arrive with the
    pipeline rework."""
    levels = (compiled.get("hierarchy") or {}).get("levels") or []
    parent_type = (parent_map or {}).get("level_type", "")
    parent_idx = next((i for i, l in enumerate(levels)
                       if l.get("level_type") == parent_type), 0)
    allowed = []
    for i, level in enumerate(levels):
        if i > parent_idx or (i == parent_idx and level.get("nestable")):
            allowed.append(level)
    return [l for l in allowed if l.get("generator_id", "interior") == "interior"]


def is_expandable(compiled: dict, map_id: str, node: dict) -> bool:
    """Whether a node can open into a child map: it is NAMED (the AI/player
    decide what deserves depth — no importance gate), a child level exists,
    depth allows it, and no child map is anchored there yet."""
    if not node or not node.get("name"):
        return False
    from wbworldgen.worldgen import mapspace as _ms
    parent = _ms.get_map(compiled, map_id)
    if parent is None:
        return False
    if len(_ms.breadcrumb(compiled, map_id)) >= MAX_DEPTH:
        return False
    if not allowed_child_levels(compiled, parent):
        return False
    return not _ms.children_by_anchor(compiled).get((map_id, node.get("id")))


def map_world_entries(map_record: dict, connections: list = None,
                      maps_by_id: dict = None) -> list[dict]:
    """RAG world-index entries for one child map + its connections. Must stay
    in the same format ``memory._build_world_entries`` emits for non-root v2
    maps so incremental embedding and a later full re-embed agree."""
    entries = []
    label = map_record.get("label", map_record.get("map_id", ""))
    if label or map_record.get("description"):
        entries.append({
            "text": f"Map: {label} ({map_record.get('level_type', 'map')}). "
                    f"{map_record.get('description', '')}".strip(),
            "source_type": "map",
            "source_id": map_record.get("map_id", ""),
            "region": label,
        })
    for node in map_record.get("nodes", []):
        if not node.get("name") or not node.get("description"):
            continue
        entries.append({
            "text": f"Location [{label}]: {node['name']} ({node.get('type', 'location')}). {node['description']}",
            "source_type": "node",
            "source_id": node.get("id", ""),
            "region": label,
        })
    for c in connections or []:
        if c.get("hidden"):
            continue
        by_id = maps_by_id or {}
        from_label = (by_id.get((c.get("from") or {}).get("map_id")) or {}).get("label") \
            or (c.get("from") or {}).get("map_id", "")
        to_label = (by_id.get((c.get("to") or {}).get("map_id")) or {}).get("label") \
            or (c.get("to") or {}).get("map_id", "")
        entries.append({
            "text": f"Connection: {c.get('kind', 'passage')} '{c.get('name', '')}' linking "
                    f"{from_label} and {to_label}. {c.get('description', '')}".strip(),
            "source_type": "connection",
            "source_id": c.get("id", ""),
            "region": from_label,
        })
    return entries


class MapExpansionEngine:
    """One-call child-map expansion. Shares the host's (WorldBuilder facade)
    LLM service, prompt library, temperature and the enrichment engine's
    semaphore/backoff, exactly like the (deprecated) SiteExpansionEngine it
    replaces."""

    def __init__(self, host):
        self._host = host

    @property
    def _llm(self):
        return self._host._llm_service

    async def expand(self, compiled: dict, parent_map_id: str, node: dict, *,
                     max_locations: int = 10, template_vocab: dict = None) -> dict:
        """Generate {"map": MapRecord, "connections": [ConnectionRecord]} for
        one anchor node. Raises on LLM failure or a violated entrance
        contract — nothing is persisted here."""
        from wbworldgen.worldgen import mapspace as _ms
        max_locations = max(4, min(int(max_locations or 10), 16))
        parent_map = _ms.get_map(compiled, parent_map_id) or {}
        levels = allowed_child_levels(compiled, parent_map)
        if not levels:
            raise ValueError(f"No child levels available below map {parent_map_id}")

        if not self._llm or self._llm.mode == "mock":
            parsed = self._mock_content(node, levels, max_locations)
        else:
            all_nodes, _ = collect_nodes_by_layer(compiled)
            context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=True)
            parsed = await self._live_expand(node, context, parent_map, levels,
                                             max_locations, template_vocab)
        return self._build_map(compiled, parent_map_id, node, parsed, levels, max_locations)

    # --- result shaping -----------------------------------------------------

    def _build_map(self, compiled: dict, parent_map_id: str, node: dict,
                   parsed: dict, levels: list, max_locations: int) -> dict:
        node_id = node.get("id", "")
        map_id = child_map_id(parent_map_id, node_id)
        raw_locations = parsed.get("locations")
        if not isinstance(raw_locations, list) or not raw_locations:
            raise ValueError(f"Map expansion for {node_id} returned no locations")

        level_type = str(parsed.get("level_type", "")).strip()
        level = next((l for l in levels if l.get("level_type") == level_type), levels[0])

        # Ids are assigned server-side — never trust LLM ids. Names dedup.
        locations = []
        seen = set()
        entrance_seen = False
        for raw in raw_locations[:max_locations]:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            is_entrance = bool(raw.get("is_entrance")) and not entrance_seen
            entrance_seen = entrance_seen or is_entrance
            locations.append({
                "id": f"{map_id}:n{len(locations) + 1}",
                "name": name,
                "type": str(raw.get("type", "room")).strip() or "room",
                "description": str(raw.get("description", "")).strip(),
                "adjacent": raw.get("adjacent") if isinstance(raw.get("adjacent"), list) else [],
                "is_entrance": is_entrance,
            })
        if not locations:
            raise ValueError(f"Map expansion for {node_id} produced no valid locations")
        if not entrance_seen:
            locations[0]["is_entrance"] = True

        from wbworldgen.worldgen.generation.registry import get_generator
        generated = get_generator(level.get("generator_id", "interior")).build(
            {"map_id": map_id, "locations": locations})
        entrance_node_id = generated.pop("entrance_node_id", None) \
            or (generated["nodes"][0]["id"] if generated["nodes"] else None)

        record = {
            "map_id": map_id,
            "label": str(parsed.get("label", "")).strip() or f"Inside {node.get('name', node_id)}",
            "level_type": level.get("level_type", "interior"),
            "description": str(parsed.get("description", "")).strip(),
            "parent_map_id": parent_map_id,
            "anchor_node_id": node_id,
            "generator_id": level.get("generator_id", "interior"),
            "nodes": generated["nodes"],
            "edges": generated["edges"],
            "config": generated["config"],
            "schema": 2,
        }

        # Connections: the mandatory entrance plus any extra authored ways
        # out, resolved against the parent map's node names.
        from wbworldgen.worldgen import mapspace as _ms
        parent_nodes_by_name = {
            str(n.get("name", "")).strip().lower(): n
            for n in _ms.map_nodes(compiled, parent_map_id) if n.get("name")}
        child_by_name = {str(n.get("name", "")).strip().lower(): n
                         for n in generated["nodes"]}
        connections = []

        def _connection(kind, name, description, child_node_id, parent_node_id,
                        travel, requirements):
            return {
                "id": f"c_{hashlib.sha1(f'{map_id}/{len(connections)}/{child_node_id}'.encode()).hexdigest()[:8]}",
                "from": {"map_id": parent_map_id, "node_id": parent_node_id},
                "to": {"map_id": map_id, "node_id": child_node_id},
                "kind": kind or "entrance",
                "name": name or "",
                "description": description or "",
                "travel": travel,
                "bidirectional": True,
                "requirements": requirements or "",
                "hidden": False,
                "origin": "generated",
            }

        raw_connections = parsed.get("connections")
        if isinstance(raw_connections, list):
            for raw in raw_connections[:6]:
                if not isinstance(raw, dict):
                    continue
                child_ref = str(raw.get("at_location", "")).strip().lower()
                child_node = child_by_name.get(child_ref)
                if child_node is None and entrance_node_id:
                    child_node = next((n for n in generated["nodes"]
                                       if n["id"] == entrance_node_id), None)
                if child_node is None:
                    continue
                parent_ref = str(raw.get("to_parent_location", "")).strip().lower()
                parent_node = parent_nodes_by_name.get(parent_ref) or {"id": node_id}
                travel_raw = raw.get("travel", "instant")
                if isinstance(travel_raw, (int, float)) or (
                        isinstance(travel_raw, str) and travel_raw.strip().isdigit()):
                    travel = {"mode": "journey", "turns": max(1, int(travel_raw))}
                else:
                    travel = {"mode": "instant"}
                connections.append(_connection(
                    str(raw.get("kind", "")).strip(), str(raw.get("name", "")).strip(),
                    str(raw.get("description", "")).strip(), child_node["id"],
                    parent_node.get("id", node_id), travel,
                    str(raw.get("requirements", "")).strip()))

        anchored = any(
            c["from"]["node_id"] == node_id or c["to"]["node_id"] == node_id
            for c in connections)
        if not anchored:
            if entrance_node_id is None:
                raise ValueError(f"Map expansion for {node_id} has no entrance node")
            connections.insert(0, _connection(
                str(parsed.get("entrance_kind", "")).strip() or "entrance",
                str(parsed.get("entrance_name", "")).strip(),
                str(parsed.get("entrance_description", "")).strip(),
                entrance_node_id, node_id, {"mode": "instant"}, ""))

        return {"map": record, "connections": connections}

    def _mock_content(self, node: dict, levels: list, max_locations: int) -> dict:
        """Deterministic offline content — expansion runs at play time, so it
        must work without a live provider."""
        name = node.get("name", "") or node.get("id", "somewhere")
        count = min(4, max_locations)
        locations = []
        for i in range(1, count + 1):
            locations.append({
                "name": f"{name} Hall {i}" if i > 1 else f"{name} Gate",
                "type": "gate" if i == 1 else "hall",
                "description": f"Mock area {i} inside {name}.",
                "adjacent": [f"{name} Hall {i - 1}" if i - 1 > 1 else f"{name} Gate"] if i > 1 else [],
                "is_entrance": i == 1,
            })
        return {
            "label": f"Inside {name}",
            "level_type": levels[0].get("level_type", "interior"),
            "description": f"Mock interior of {name}: {count} connected areas.",
            "locations": locations,
            "entrance_kind": "gate",
            "entrance_name": f"The {name} Gate",
        }

    # --- live LLM call ------------------------------------------------------

    async def _live_expand(self, node: dict, context: dict, parent_map: dict,
                           levels: list, max_locations: int,
                           template_vocab: dict = None) -> dict:
        host = self._host
        enrichment = host._enrichment
        node_id = node.get("id", "")
        node_name = node.get("name", "Unnamed")
        node_type = node.get("type", "settlement")

        world = context.get("world", {})
        region = context.get("region", {})
        neighbors = [n.get("name") for n in context.get("neighbors", []) if n.get("name")]
        neighbors_line = f"- Nearby on the map: {', '.join(neighbors[:5])}\n" if neighbors else ""
        factions = ", ".join(region.get("factions", [])[:5])
        factions_line = f"- Local factions: {factions}\n" if factions else ""

        sub_noun = "rooms, halls, courts and notable places"
        if isinstance(template_vocab, dict) and template_vocab.get("site_sub_noun"):
            sub_noun = str(template_vocab["site_sub_noun"])

        levels_block = "\n".join(
            f"- {l.get('level_type')}: {l.get('guidance', l.get('label', ''))}"
            for l in levels)

        system = host._get_prompt(
            "map_expand_system",
            "You are a world-building AI designing one map of a larger world: the interior "
            "or sub-area of a single location, so a storyteller can set scenes inside it. "
            "Ground everything in the provided world and location context. Output ONLY valid JSON.",
        )
        user_msg = host._get_prompt(
            "map_expand_user",
            f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Parent map: {parent_map.get('label', '')} ({parent_map.get('level_type', 'world')})
Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
{factions_line}{neighbors_line}
Location to expand: {node_name} ({node_type})
Description: {node.get('description', '') or node.get('label_description', '')}

Choose the level_type for this new map from:
{levels_block}

Design 6-{max_locations} distinct locations ({sub_noun}). Exactly ONE location must have
"is_entrance": true — the way in from {node_name}'s surroundings (a gate, door, cave mouth,
docking bay...). Each location gets a name, a short type, a 1-2 sentence description, and
which other locations it directly adjoins (by name).
You MAY also add further connections out of this map in "connections": each states its kind,
a name, at_location (which of your locations it sits at), to_parent_location (an existing
location name on the parent map, or empty to link back to {node_name} itself), travel
("instant" or a number of turns for a longer crossing), and requirements (empty if open).

Output ONLY valid JSON:
{{"label": "...", "level_type": "...", "description": "2-3 sentences on how this place is laid out",
"entrance_kind": "gate|door|cave mouth|...", "entrance_name": "...", "entrance_description": "...",
"locations": [{{"name": "...", "type": "...", "description": "...", "adjacent": ["..."], "is_entrance": false}}, ...],
"connections": []}}""",
            node_name=node_name,
            node_type=node_type,
            node_description=node.get('description', ''),
            world_name=world.get('name', 'Unknown'),
            world_genre=world.get('genre', ''),
            world_tone=world.get('tone', ''),
            world_premise=world.get('premise', ''),
            max_locations=str(max_locations),
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        await enrichment._wait_for_backoff()
        async with host._enrichment_semaphore:
            try:
                return await json_retry_completion(
                    self._llm,
                    messages=messages,
                    model=self._llm.reader_model,
                    temperature=host._world_builder_temperature or 0.9,
                    inspector_ctx={"call_type": "world_build", "step": "map:expand"},
                    step_label=f"map:expand:{node_id}",
                    retry_attempts=host._json_retry_attempts,
                )
            except Exception as e:
                enrichment._note_rate_limit(e)
                logger.error("Map expansion failed for node %s: %s", node_id, e)
                raise
