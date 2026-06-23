"""Incremental node enrichment: labelling + description generation.

The ``EnrichmentEngine`` orchestrates one-node-at-a-time LLM enrichment over a
generated map, with importance ordering, transient-error retries and rate
limiting. It reads shared services (LLM, persistence, model config, prompt
templates) from a ``host`` object (the WorldBuilder facade).
"""

import asyncio
import logging
import re

from backend.engine.worldgen.compiler import compile_world
from backend.engine.worldgen.generation.llm import json_retry_completion
from backend.engine.worldgen.enrichment.context import (
    build_enrichment_context,
    collect_nodes_by_layer,
    postprocess_links,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini/gemini-2.5-flash"


class EnrichmentEngine:
    def __init__(self, host):
        self._host = host

    @property
    def _llm(self):
        return self._host._llm_service

    def _load_compiled(self, world_id: str) -> dict:
        world_data = self._host.load_world(world_id)
        return compile_world(world_data, getattr(self._host, "_steps", None))

    async def label_next(self, world_id: str, labeled_node_ids: list = None, layer_filter: str = None) -> dict:
        if not self._llm or self._llm.mode == "mock":
            raise RuntimeError("Enrichment requires an LLM service. The mock enrichment has been removed.")

        compiled = self._load_compiled(world_id)
        all_nodes, _ = collect_nodes_by_layer(compiled, layer_filter)
        all_nodes_full, layer_map_full = collect_nodes_by_layer(compiled)
        saved_labeled = {n.get("id") for n in all_nodes_full if n.get("name")}
        done_ids = saved_labeled | set(labeled_node_ids or [])

        unlabeled = sorted(
            [n for n in all_nodes if n.get("id") not in done_ids],
            key=lambda n: -n.get("importance", 0),
        )
        total_nodes = len(all_nodes_full)
        total_labeled = len(done_ids)

        per_layer = {}
        for lid, info in layer_map_full.items():
            lid_labeled = sum(
                1 for nid in done_ids
                if any(n.get("id") == nid and n.get("layer_id", "") == lid for n in all_nodes_full)
            )
            per_layer[lid] = {"done": lid_labeled, "total": info["total"]}

        if not unlabeled:
            return {"node_id": None, "label": None, "label_description": None, "layer_id": None,
                    "per_layer": per_layer, "total_labeled": total_labeled,
                    "total_nodes": total_nodes, "complete": True, "failed_node_ids": []}

        node = unlabeled[0]
        context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=False)
        name = snippet = None

        for attempt in range(3):
            try:
                async with self._host._enrichment_semaphore:
                    name, snippet = await self._live_label(node, context)
                break
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                logger.warning("Transient error labeling node %s (attempt %d): %s", node.get("id"), attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            except Exception as e:
                logger.error("Label generation failed for node %s: %s", node.get("id"), e)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

        if name is None:
            logger.error("Label generation exhausted retries for node %s, skipping", node.get("id"))
            return {"node_id": node.get("id"), "label": None, "label_description": None,
                    "layer_id": node.get("layer_id", ""),
                    "per_layer": per_layer, "total_labeled": total_labeled,
                    "total_nodes": total_nodes, "complete": False,
                    "failed_node_ids": [node.get("id")]}

        node_id = node.get("id")
        lid = node.get("layer_id", "")
        self._host._save_node_enrichment(world_id, node_id, "name", name)
        if snippet:
            self._host._save_node_enrichment(world_id, node_id, "label_description", snippet)
        self._host._flush_enrichment_cache(world_id)

        if lid in per_layer:
            per_layer[lid]["done"] = per_layer[lid]["done"] + 1

        return {"node_id": node_id, "label": name, "label_description": snippet, "layer_id": lid,
                "per_layer": per_layer, "total_labeled": total_labeled + 1,
                "total_nodes": total_nodes, "complete": len(unlabeled) <= 1, "failed_node_ids": []}

    async def describe_next(self, world_id: str, labeled_node_ids: list = None, layer_filter: str = None) -> dict:
        if not self._llm or self._llm.mode == "mock":
            raise RuntimeError("Enrichment requires an LLM service. The mock enrichment has been removed.")

        compiled = self._load_compiled(world_id)
        all_nodes, _ = collect_nodes_by_layer(compiled, layer_filter)
        all_nodes_full, layer_map_full = collect_nodes_by_layer(compiled)
        labeled = [n for n in all_nodes_full if n.get("name")]
        saved_described = {n.get("id") for n in labeled if n.get("description")}
        done_ids = saved_described | set(labeled_node_ids or [])

        undescribed = sorted(
            [n for n in all_nodes if n.get("id") not in done_ids and n.get("name")],
            key=lambda n: -n.get("importance", 0),
        )
        total_labeled_nodes = len(labeled)
        total_described = len(done_ids)

        per_layer = {}
        for lid, info in layer_map_full.items():
            lid_done = sum(1 for n in all_nodes_full if n.get("id") in done_ids and n.get("layer_id", "") == lid)
            per_layer[lid] = {"done": lid_done, "total": info["total"]}

        if not undescribed:
            return {"node_id": None, "description": None, "layer_id": None,
                    "per_layer": per_layer, "total_labeled": total_described,
                    "total_nodes": total_labeled_nodes, "complete": True, "failed_node_ids": []}

        node = undescribed[0]
        context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=True)
        desc_with_links = None

        for attempt in range(3):
            try:
                async with self._host._enrichment_semaphore:
                    desc_with_links = await self._live_description(node, context)
                break
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                logger.warning("Transient error describing node %s (attempt %d): %s", node.get("id"), attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            except Exception as e:
                logger.error("Description generation failed for node %s: %s", node.get("id"), e)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

        if desc_with_links is None:
            logger.error("Description generation exhausted retries for node %s, skipping", node.get("id"))
            return {"node_id": node.get("id"), "description": None,
                    "layer_id": node.get("layer_id", ""),
                    "per_layer": per_layer, "total_labeled": total_described,
                    "total_nodes": total_labeled_nodes, "complete": False,
                    "failed_node_ids": [node.get("id")]}

        desc = postprocess_links(desc_with_links, node, all_nodes)
        node_id = node.get("id")
        lid = node.get("layer_id", "")
        self._host._save_node_enrichment(world_id, node_id, "description", desc)
        self._host._flush_enrichment_cache(world_id)

        if lid in per_layer:
            per_layer[lid]["done"] = per_layer[lid].get("done", 0) + 1

        return {"node_id": node_id, "description": desc, "layer_id": lid,
                "per_layer": per_layer, "total_labeled": total_described + 1,
                "total_nodes": total_labeled_nodes, "complete": len(undescribed) <= 1, "failed_node_ids": []}

    # --- live LLM calls -----------------------------------------------------

    async def _live_label(self, node: dict, context: dict) -> tuple:
        node_type = node.get("type", "waypoint")
        node_id = node.get("id", "")
        importance = node.get("importance", 0)

        world = context.get("world", {})
        layer = context.get("layer", {})
        region = context.get("region", {})
        neighbors = context.get("neighbors", [])

        neighbor_names = [n.get("name", n.get("link_id", "?")) for n in neighbors[:5]]
        neighbor_str = ", ".join(neighbor_names) if neighbor_names else "none"

        region_factions = region.get("factions", [])
        region_landmarks = region.get("landmarks", [])
        factions_str = f"- Factions: {', '.join(region_factions)}\n" if region_factions else ""
        landmarks_str = f"- Notable landmarks: {', '.join(region_landmarks)}\n" if region_landmarks else ""

        host = self._host
        model = host._enrich_label_model or host._world_builder_model or (self._llm.module_fast_model if self._llm else _DEFAULT_MODEL)
        temperature = host._world_builder_temperature or 0.9

        system = host._get_prompt(
            "enrich_label_system",
            "You are a world-building AI. Generate a concise, evocative name and a one-line label description for a map node.",
        )
        user_msg = host._get_prompt(
            "enrich_label_user",
            f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
- Climate: {region.get('climate', '')}
{factions_str}{landmarks_str}
Node details:
- ID: {node_id}
- Type: {node_type}
- Importance: {importance}/10
- Layer: {layer.get('name', 'surface')} ({layer.get('type', 'surface')})
- Layer description: {layer.get('description', '')}
- Nearby nodes: {neighbor_str}

Generate a unique, fitting name for this {node_type} and a short one-line description (label_description).
Output ONLY valid JSON: {{"name": "...", "label_description": "..."}}""",
            world_name=world.get('name', 'Unknown'),
            world_genre=world.get('genre', ''),
            world_tone=world.get('tone', ''),
            world_premise=world.get('premise', ''),
            node_id=node_id,
            node_type=node_type,
            node_importance=str(importance),
            layer_name=layer.get('name', 'surface'),
            layer_type=layer.get('type', 'surface'),
            layer_description=layer.get('description', ''),
            neighbor_names=neighbor_str,
            region_name=region.get('name', 'unknown'),
            region_terrain=region.get('terrain', ''),
            region_climate=region.get('climate', ''),
            region_factions=factions_str,
            region_landmarks=landmarks_str,
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        try:
            result = await json_retry_completion(
                self._llm,
                messages=messages,
                model=model,
                temperature=temperature,
                inspector_ctx={"call_type": "world_build", "step": "enrich:label"},
                step_label=f"enrich:label:{node_id}",
                retry_attempts=host._json_retry_attempts,
            )
            return result.get("name", "Unknown"), result.get("label_description", "")
        except Exception as e:
            logger.error(f"Label generation failed for node {node_id}: {e}")
            raise

    async def _live_description(self, node: dict, context: dict) -> str:
        node_id = node.get("id", "")
        node_name = node.get("name", "Unnamed")
        node_type = node.get("type", "waypoint")
        label_description = node.get("label_description", "")

        world = context.get("world", {})
        layer = context.get("layer", {})
        region = context.get("region", {})
        neighbors = context.get("neighbors", [])

        labeled_neighbors = [n for n in neighbors if n.get("name")]
        neighbor_str = ", ".join(
            [f"{n.get('name', '?')} ({n.get('type', '?')}, link_id: {n.get('link_id', '?')})" for n in labeled_neighbors[:5]]
        ) or "none"

        host = self._host
        model = host._world_builder_model or (self._llm.reader_model if self._llm else _DEFAULT_MODEL)
        temperature = host._world_builder_temperature or 0.9

        system = host._get_prompt(
            "enrich_description_system",
            "You are a world-building AI. Write a short, atmospheric flavor description for a map location. Reference neighboring locations using their ${link_ID} syntax.",
        )
        user_msg = host._get_prompt(
            "enrich_description_user",
            f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
- Climate: {region.get('climate', '')}

Location: {node_name}
Label: {label_description}
Type: {node_type}
Layer: {layer.get('name', 'surface')} ({layer.get('type', 'surface')})
Layer description: {layer.get('description', '')}
Nearby locations: {neighbor_str}

Write a 1-3 sentence flavor description of this location. Reference neighbors using their link IDs like ${{link_n_0001}} or ${{link_a1b2}} (the same format used in the neighbor list above).
Output ONLY the description text, no JSON wrapper.""",
            world_name=world.get('name', 'Unknown'),
            world_genre=world.get('genre', ''),
            world_tone=world.get('tone', ''),
            world_premise=world.get('premise', ''),
            node_name=node_name,
            label_description=label_description,
            node_type=node_type,
            layer_name=layer.get('name', 'surface'),
            layer_type=layer.get('type', 'surface'),
            layer_description=layer.get('description', ''),
            neighbor_names=neighbor_str,
            region_name=region.get('name', 'unknown'),
            region_terrain=region.get('terrain', ''),
            region_climate=region.get('climate', ''),
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]

        temperature = float(temperature)
        for attempt in range(3):
            try:
                content = await self._llm.simple_completion(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    inspector_ctx={"call_type": "world_build", "step": f"enrich:description:{'retry' if attempt else 'initial'}"},
                )
                content = content.strip()
                content = re.sub(r'^```[a-zA-Z]*\s*', '', content)
                content = re.sub(r'\s*```$', '', content)
                content = content.strip()
                if len(content) >= 10:
                    return content
                logger.warning("Description too short for node %s (%d chars), retrying (attempt %d)", node_id, len(content), attempt + 1)
                temperature = min(temperature + 0.1, 1.0)
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                logger.warning("Transient error for description node %s (attempt %d): %s", node_id, attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    temperature = min(temperature + 0.1, 1.0)
                    continue
                raise
            except Exception:
                raise

        if label_description:
            return label_description
        return f"A notable {node_type} within {world.get('name', 'the world')}."
