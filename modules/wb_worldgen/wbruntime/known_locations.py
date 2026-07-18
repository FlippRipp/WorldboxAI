"""Auto-reveal pass: which places does the character already know?

Runs once per story via ``on_intro_complete``, right after the opening message
is generated, and on demand via the ``/recall`` command in already-started
stories. Every detailed node (name + description) still hidden by fog of war
is sent to the LLM in batches together with the character (name, race,
backstory), the start location, the world premise and the opening scene; the
LLM answers with the ids of the locations the character would plausibly
already know about, and those join ``revealed_node_ids``. Nodes without
details yet can't be "known" — they stay hidden until the story reaches them.
"""

import asyncio
import json
import logging

from .worldspace import ensure_v2, maps_by_id, node_index

logger = logging.getLogger(__name__)

BATCH_SIZE = 30

_SYSTEM = (
    "You are the memory of a player character in a text RPG. Given the character "
    "and a list of places in their world, decide which places the character would "
    "already know about at the moment their story begins.\n"
    "The character KNOWS places that are famous or widely talked about (major "
    "settlements, renowned landmarks), places in or near their home region, places "
    "tied to their backstory (origin, travels, trade), and whatever is common "
    "knowledge to any local.\n"
    "The character does NOT know places whose description marks them as secret, "
    "hidden, lost or undiscovered, nor minor spots far from anything the character "
    "has a link to. Knowing OF a place is enough — they need not have visited it.\n"
    "When unsure, lean on importance (0-10): high importance means widely known.\n"
    "Output only valid JSON."
)


def known_location_candidates(world_data: dict, revealed: set) -> list[dict]:
    """Detailed, still-hidden nodes with the context the LLM needs to judge."""
    out = []
    for map_id, m in maps_by_id(world_data).items():
        map_label = m.get("label") or map_id
        for node in m.get("nodes", []):
            nid = node.get("id")
            if not nid or nid in revealed:
                continue
            if not node.get("name") or not node.get("description"):
                continue
            out.append({
                "id": nid,
                "name": node["name"],
                "type": node.get("type", "location"),
                "region": node.get("region", ""),
                "importance": node.get("importance"),
                "map_label": map_label,
                "description": node["description"],
            })
    return out


def _context_header(state: dict, world_data: dict) -> str:
    parts = []

    char = (state.get("characters") or {}).get("default_player") or {}
    rpg = (state.get("module_data") or {}).get("wb_core_rpg") or {}
    char_lines = []
    if char.get("name"):
        char_lines.append(f"Name: {char['name']}")
    if char.get("race"):
        char_lines.append(f"Race: {char['race']}")
    if rpg.get("backstory"):
        char_lines.append(f"Backstory: {rpg['backstory']}")
    if char_lines:
        parts.append("<character>\n" + "\n".join(char_lines) + "\n</character>")

    start = node_index(world_data).get(state.get("player_location_node_id") or "")
    if start and start.get("name"):
        region = f" in {start['region']}" if start.get("region") else ""
        parts.append(f"The story begins at: {start['name']}{region}.")

    lore = world_data.get("lore", {})
    if lore.get("world_name"):
        parts.append(f"World: {lore['world_name']}")
    if lore.get("premise"):
        parts.append(f"World premise: {lore['premise']}")

    opening = next(iter(state.get("history") or []), "")
    if opening:
        parts.append("<opening_scene>\n" + opening + "\n</opening_scene>")

    return "\n\n".join(parts)


async def _decide_batch(llm, header: str, batch: list[dict]) -> list[str]:
    lines = []
    for c in batch:
        imp = c.get("importance")
        imp_txt = f", importance {imp}/10" if isinstance(imp, (int, float)) else ""
        region_txt = f", {c['region']}" if c.get("region") else ""
        lines.append(
            f"- {c['id']}: {c['name']} ({c['type']}{region_txt}{imp_txt}, "
            f"on map {c['map_label']}) — {c['description']}"
        )
    user = (
        header
        + "\n\nLocations the character might know about:\n" + "\n".join(lines)
        + '\n\nReturn JSON: {"known_node_ids": ["id", ...]} listing ONLY ids from '
        "the list above that the character already knows about. An empty list is "
        "a valid answer."
    )
    try:
        content = await llm.simple_completion(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            model=llm.reader_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "reader", "step": "intro:known_locations"},
        )
        ids = json.loads(content).get("known_node_ids", [])
        valid = {c["id"] for c in batch}
        return [nid for nid in ids if isinstance(nid, str) and nid in valid]
    except Exception as e:
        logger.warning("Known-locations batch failed (%d nodes): %s", len(batch), e)
        return []


def _concurrency(host) -> int:
    try:
        if host._services is not None and host._services.get("settings") is not None:
            return max(1, int(host._services["settings"].get("world.enrichment_concurrency")))
    except Exception:
        pass
    return 3


async def reveal_known_locations(host, state: dict, sdk) -> dict:
    """Run the pass. Returns ``{}`` when nothing changed, else the full
    updated ``revealed_node_ids`` plus ``newly_known_node_ids`` for callers
    that want to report what was added (the ``/recall`` command)."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    engine = host._services.get("engine") if host._services else None
    llm = getattr(engine, "llm", None)
    if llm is None or getattr(llm, "mode", "mock") == "mock":
        return {}
    ensure_v2(state)
    revealed = list(dict.fromkeys(state.get("revealed_node_ids", [])))
    candidates = known_location_candidates(world_data, set(revealed))
    if not candidates:
        return {}

    if sdk is not None:
        await sdk.ui.emit_status(
            "known_locations", "Recalling the places your character knows…")

    header = _context_header(state, world_data)
    batches = [candidates[i:i + BATCH_SIZE]
               for i in range(0, len(candidates), BATCH_SIZE)]
    sem = asyncio.Semaphore(_concurrency(host))

    async def bounded(batch):
        async with sem:
            return await _decide_batch(llm, header, batch)

    results = await asyncio.gather(*(bounded(b) for b in batches))
    known = [nid for batch_ids in results for nid in batch_ids]
    if not known:
        return {}
    logger.info("Known-locations pass revealed %d of %d candidate nodes",
                len(known), len(candidates))
    return {"revealed_node_ids": revealed + known, "newly_known_node_ids": known}
