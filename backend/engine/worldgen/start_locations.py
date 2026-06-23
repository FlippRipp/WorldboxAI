"""Start-location discovery + LLM-assisted selection for a saved world."""

import json
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)


def _find_node_region(node_id: str, compiled: dict) -> str:
    for region in compiled.get("map", {}).get("regions", []):
        if node_id in region.get("node_ids", []):
            return region.get("region_name", "")
    for node in compiled.get("map", {}).get("nodes", []):
        if node.get("id") == node_id:
            desc = node.get("description", "")
            for region_data in compiled.get("regions", {}).get("regions", []):
                region_name = region_data.get("name", "")
                if region_name and region_name.lower() in desc.lower():
                    return region_name
    return ""


def get_start_locations(compiled: dict) -> list[dict]:
    map_layers = compiled.get("map_layers", [])
    if map_layers:
        nodes = []
        for layer in map_layers:
            layer_map = layer.get("map", {})
            for node in layer_map.get("nodes", []):
                node["layer_id"] = layer.get("layer_id", "")
                node["layer_name"] = layer.get("name", "")
            nodes.extend(layer_map.get("nodes", []))
    else:
        nodes = compiled.get("map", {}).get("nodes", [])

    def build(node, default_type):
        c = {
            "node_id": node.get("id"),
            "name": node.get("name"),
            "type": node.get("type", default_type),
            "description": node.get("description", "")[:300],
            "region": _find_node_region(node.get("id"), compiled),
        }
        if node.get("layer_id"):
            c["layer_id"] = node.get("layer_id")
            c["layer_name"] = node.get("layer_name", "")
        return c

    candidates = [
        build(n, n.get("type"))
        for n in nodes
        if n.get("type") in ("settlement", "landmark") and n.get("name")
    ]
    if not candidates:
        candidates = [build(n, "location") for n in nodes if n.get("name")]
    return candidates


async def llm_pick_start_location(compiled: dict, candidates: list[dict], preference: str, llm) -> Optional[dict]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if not preference or preference.lower() == "random":
        return random.choice(candidates)

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
    user_msg = f"""World premise: {world_premise}

Player's starting location preference: "{preference}"

Available locations:
{candidates_summary}

Pick the single best matching location. Return JSON: {{"node_id": "...", "name": "...", "reason": "one sentence why"}}"""
    try:
        content = await llm.simple_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            model=llm.reader_model,
            response_format={"type": "json_object"},
            inspector_ctx={"call_type": "reader", "step": "world_build:pick_start"},
        )
        choice = json.loads(content)
        chosen_id = choice.get("node_id", "")
        for c in candidates:
            if c["node_id"] == chosen_id:
                c["reason"] = choice.get("reason", "")
                return c
        return candidates[0]
    except Exception as e:
        logger.error(f"LLM start location pick failed: {e}")
        return random.choice(candidates)
