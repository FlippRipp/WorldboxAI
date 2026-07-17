"""Incremental node enrichment: labelling + description generation.

The ``EnrichmentEngine`` orchestrates one-node-at-a-time LLM enrichment over a
generated map, with importance ordering, transient-error retries and rate
limiting. It reads shared services (LLM, persistence, model config, prompt
templates) from a ``host`` object (the WorldBuilder facade).
"""

import asyncio
import logging
import re
import time

from wbworldgen.worldgen.compiler import compile_world
from wbworldgen.worldgen.generation.llm import json_retry_completion
from wbworldgen.worldgen.enrichment.context import (
    build_enrichment_context,
    collect_nodes_by_layer,
    postprocess_links,
)

logger = logging.getLogger(__name__)


def _terrain_line(terrain: dict) -> str:
    """One-line terrain fact for the enrichment prompt (empty when unknown)."""
    if not terrain or not terrain.get("biome"):
        return ""
    parts = [f"- Local terrain: {terrain['biome']}"]
    if terrain.get("elevation_band"):
        parts.append(f"({terrain['elevation_band']}")
        near = terrain.get("near_water") or []
        parts[-1] += f", near {', '.join(near)})" if near else ")"
    return " ".join(parts)


# What each inter-layer connection type physically looks like, so generated
# names/descriptions match the kind of passage it actually is.
_CONNECTION_LOOK = {
    "dungeon_entrance": "a dungeon entrance — a dark doorway or descent leading underground",
    "cave_entrance": "a cave mouth opening into the earth",
    "cave_mouth": "a cave mouth opening into the earth",
    "port": "a harbor where ships dock and put to sea",
    "portal": "a magical portal or arcane gateway",
    "rift": "a glowing rift or tear in reality",
    "staircase": "a great staircase linking one level to another",
    "bridge": "a bridge spanning across to another area",
}


def _connection_block(connection: dict, vocab: dict = None) -> str:
    """Multi-line note describing the inter-layer connection a node represents,
    so the LLM names/describes it as the right kind of passage. Empty when the
    node is not a layer connection. A world template's vocabulary may add or
    override connection looks (e.g. spaceport/jump_gate for sci-fi)."""
    if not connection:
        return ""
    ctype = connection.get("type", "passage")
    looks = _CONNECTION_LOOK
    if isinstance(vocab, dict) and isinstance(vocab.get("connection_looks"), dict):
        looks = {**_CONNECTION_LOOK, **vocab["connection_looks"]}
    look = looks.get(ctype, f"a {ctype.replace('_', ' ')}")
    parts = [f"This location is a LAYER CONNECTION ({ctype}): {look}."]
    if connection.get("target_layer_id"):
        parts.append(f"It leads to the '{connection['target_layer_id']}' layer.")
    if connection.get("description"):
        parts.append(f"Connection details: {connection['description']}")
    parts.append("Name and describe it as this kind of passage.")
    return " ".join(parts)


def _strip_leading_the(name: str) -> str:
    """Drop a leading 'The ' so generated names don't all start the same way."""
    if not name:
        return name
    stripped = re.sub(r'^\s*[Tt]he\s+', '', name).strip()
    return stripped or name.strip()


# Substrings that identify a provider rate-limit error; when one is seen all
# in-flight enrichment workers back off together instead of hammering the API.
_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "quota",
                       "resource_exhausted", "too many requests")


class EnrichmentEngine:
    def __init__(self, host):
        self._host = host
        self._cancel_flags: set = set()
        self._backoff_until: float = 0.0
        # Compiled-world cache (size 1: the actively-enriched world). Skips the
        # full world re-read + compile + terrain npz decompress per node call.
        # Entries are mutated in place as enrichment lands and dropped whenever
        # the world's step files are written by anything else.
        self._compiled_cache: dict = {}

    @property
    def _llm(self):
        return self._host._llm_service

    # --- shared throttling ---------------------------------------------------

    def _note_rate_limit(self, exc) -> bool:
        msg = str(exc).lower()
        if any(marker in msg for marker in _RATE_LIMIT_MARKERS):
            self._backoff_until = max(self._backoff_until, time.monotonic() + 5.0)
            return True
        return False

    async def _wait_for_backoff(self):
        while True:
            delay = self._backoff_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 5.0))

    def _load_compiled(self, world_id: str) -> dict:
        compiled = self._compiled_cache.get(world_id)
        if compiled is None:
            world_data = self._host.load_world(world_id)
            compiled = compile_world(world_data, getattr(self._host, "_steps", None))
            tg = world_data.get("steps", {}).get("terrain_generation", {}).get("data", {})
            compiled["_terrain_meta"] = tg.get("layers", []) if isinstance(tg, dict) else []
            self._compiled_cache.clear()
            self._compiled_cache[world_id] = compiled
        self._ensure_terrain(world_id, compiled)
        return compiled

    def invalidate_compiled(self, world_id: str = None):
        """Drop cached compiled state (after the world's step files were written
        by something other than enrichment, or the world was deleted)."""
        if world_id is None:
            self._compiled_cache.clear()
        else:
            self._compiled_cache.pop(world_id, None)

    def release_terrain(self, world_id: str):
        """Free the decompressed terrain rasters (tens of MB) while keeping the
        cheap compiled JSON cached; they lazily re-attach on the next call."""
        compiled = self._compiled_cache.get(world_id)
        if compiled is not None:
            compiled.pop("_terrain_layers", None)

    def _node_index(self, compiled: dict) -> dict:
        """Lazily-built {node_id: node dict} index over the compiled world's own
        node dicts (not the per-call copies handed to prompts)."""
        index = compiled.get("_node_by_id")
        if index is None:
            from wbworldgen.worldgen import mapspace as _ms
            index = {n.get("id"): n for n in _ms.all_nodes(compiled)}
            compiled["_node_by_id"] = index
        return index

    def get_node(self, world_id: str, node_id: str) -> dict | None:
        """Current state of one map node (post-enrichment fields included)."""
        compiled = self._load_compiled(world_id)
        return self._node_index(compiled).get(node_id)

    def _update_cached_node(self, compiled: dict, node_id: str, field: str, value: str):
        """Mirror an enrichment write onto the compiled world's own node dicts
        so the cached compiled state stays truthful across calls/runs (the node
        lists handed to prompts are per-call copies)."""
        node = self._node_index(compiled).get(node_id)
        if node is not None:
            node[field] = value

    def _ensure_terrain(self, world_id: str, compiled: dict):
        """Load persisted terrain rasters per layer so enrichment context can
        sample biome/elevation at each node's coordinate. Best-effort; no-op
        when already attached."""
        if "_terrain_layers" in compiled:
            return
        try:
            from wbworldgen.worldgen import terrain_store as _ts
            persistence = getattr(self._host, "_persistence", None)
            if persistence is None:
                return
            terrain_by_layer = {}
            for tl in compiled.get("_terrain_meta", []):
                lid = tl.get("layer_id", "main")
                out_dir = persistence.terrain_dir(world_id, lid)
                layers = _ts.load_terrain(str(out_dir))
                if layers:
                    # Keyed by the terrain step's layer id; nodes are tagged
                    # with their map's legacy_layer_id, and the single-entry
                    # fallback in _terrain_for_node covers any mismatch.
                    terrain_by_layer[lid] = layers
            if terrain_by_layer:
                compiled["_terrain_layers"] = terrain_by_layer
        except Exception as e:
            logger.warning("attach terrain for enrichment failed (%s): %s", world_id, e)

    async def label_next(self, world_id: str, labeled_node_ids: list = None, layer_filter: str = None, rework: bool = False) -> dict:
        if not self._llm or self._llm.mode == "mock":
            raise RuntimeError("Enrichment requires an LLM service. The mock enrichment has been removed.")

        compiled = self._load_compiled(world_id)
        all_nodes, _ = collect_nodes_by_layer(compiled, layer_filter)
        all_nodes_full, layer_map_full = collect_nodes_by_layer(compiled)
        session_done = set(labeled_node_ids or [])

        if rework:
            # Rework pass: revisit nodes that already have a name, regenerating
            # the label with current context instead of skipping them.
            named = [n for n in all_nodes_full if n.get("name")]
            done_ids = session_done
            unlabeled = sorted(
                [n for n in all_nodes if n.get("id") not in done_ids and n.get("name")],
                key=lambda n: -n.get("importance", 0),
            )
            total_nodes = len(named)
        else:
            saved_labeled = {n.get("id") for n in all_nodes_full if n.get("name")}
            done_ids = saved_labeled | session_done
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
                if any(n.get("id") == nid and n.get("map_id", n.get("layer_id", "")) == lid for n in all_nodes_full)
            )
            per_layer[lid] = {"done": lid_labeled, "total": info["total"]}

        if not unlabeled:
            return {"node_id": None, "label": None, "label_description": None, "layer_id": None,
                    "per_layer": per_layer, "total_labeled": total_labeled,
                    "total_nodes": total_nodes, "complete": True, "failed_node_ids": []}

        node = unlabeled[0]
        context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=False)
        name, snippet = await self._label_with_retries(node, context)

        if name is None:
            return {"node_id": node.get("id"), "label": None, "label_description": None,
                    "layer_id": node.get("map_id", node.get("layer_id", "")),
                    "per_layer": per_layer, "total_labeled": total_labeled,
                    "total_nodes": total_nodes, "complete": False,
                    "failed_node_ids": [node.get("id")]}

        node_id = node.get("id")
        lid = node.get("map_id", node.get("layer_id", ""))
        self._host._save_node_enrichment(world_id, node_id, "name", name)
        self._update_cached_node(compiled, node_id, "name", name)
        if snippet:
            self._host._save_node_enrichment(world_id, node_id, "label_description", snippet)
            self._update_cached_node(compiled, node_id, "label_description", snippet)
        self._host._flush_enrichment_cache(world_id)

        if lid in per_layer:
            per_layer[lid]["done"] = per_layer[lid]["done"] + 1

        return {"node_id": node_id, "label": name, "label_description": snippet, "layer_id": lid,
                "per_layer": per_layer, "total_labeled": total_labeled + 1,
                "total_nodes": total_nodes, "complete": len(unlabeled) <= 1, "failed_node_ids": []}

    async def describe_next(
        self,
        world_id: str,
        labeled_node_ids: list = None,
        layer_filter: str = None,
        rework: bool = False,
    ) -> dict:
        if not self._llm or self._llm.mode == "mock":
            raise RuntimeError("Enrichment requires an LLM service. The mock enrichment has been removed.")

        compiled = self._load_compiled(world_id)
        all_nodes, _ = collect_nodes_by_layer(compiled, layer_filter)
        all_nodes_full, layer_map_full = collect_nodes_by_layer(compiled)
        labeled = [n for n in all_nodes_full if n.get("name")]
        session_done = set(labeled_node_ids or [])

        if rework:
            # Rework pass: revisit nodes that already have a description (including
            # ones from earlier, possibly stale/placeholder generations) instead of
            # skipping them, regenerating with full neighbor context.
            pool = [n for n in labeled if n.get("description")]
            done_ids = session_done
            undescribed = sorted(
                [n for n in all_nodes if n.get("id") not in done_ids and n.get("name") and n.get("description")],
                key=lambda n: -n.get("importance", 0),
            )
            total_labeled_nodes = len(pool)
        else:
            saved_described = {n.get("id") for n in labeled if n.get("description")}
            done_ids = saved_described | session_done
            undescribed = sorted(
                [n for n in all_nodes if n.get("id") not in done_ids and n.get("name")],
                key=lambda n: -n.get("importance", 0),
            )
            total_labeled_nodes = len(labeled)
        total_described = len(done_ids)

        per_layer = {}
        for lid, info in layer_map_full.items():
            lid_done = sum(1 for n in all_nodes_full if n.get("id") in done_ids and n.get("map_id", n.get("layer_id", "")) == lid)
            per_layer[lid] = {"done": lid_done, "total": info["total"]}

        if not undescribed:
            return {"node_id": None, "description": None, "layer_id": None,
                    "per_layer": per_layer, "total_labeled": total_described,
                    "total_nodes": total_labeled_nodes, "complete": True, "failed_node_ids": []}

        node = undescribed[0]
        context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=True)
        existing_description = node.get("description", "") if rework else ""
        desc_with_links = await self._describe_with_retries(node, context, existing_description)

        if desc_with_links is None:
            return {"node_id": node.get("id"), "description": None,
                    "layer_id": node.get("map_id", node.get("layer_id", "")),
                    "per_layer": per_layer, "total_labeled": total_described,
                    "total_nodes": total_labeled_nodes, "complete": False,
                    "failed_node_ids": [node.get("id")]}

        desc = postprocess_links(desc_with_links, node, all_nodes)
        node_id = node.get("id")
        lid = node.get("map_id", node.get("layer_id", ""))
        self._host._save_node_enrichment(world_id, node_id, "description", desc)
        self._update_cached_node(compiled, node_id, "description", desc)
        self._host._flush_enrichment_cache(world_id)

        if lid in per_layer:
            per_layer[lid]["done"] = per_layer[lid].get("done", 0) + 1

        return {"node_id": node_id, "description": desc, "layer_id": lid,
                "per_layer": per_layer, "total_labeled": total_described + 1,
                "total_nodes": total_labeled_nodes, "complete": len(undescribed) <= 1, "failed_node_ids": []}

    # --- retry wrappers -------------------------------------------------------

    async def _label_with_retries(self, node, context) -> tuple:
        """Label one node with transient-error retries. (None, None) on failure."""
        for attempt in range(3):
            try:
                await self._wait_for_backoff()
                async with self._host._enrichment_semaphore:
                    return await self._live_label(node, context)
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                logger.warning("Transient error labeling node %s (attempt %d): %s", node.get("id"), attempt + 1, e)
            except Exception as e:
                self._note_rate_limit(e)
                logger.error("Label generation failed for node %s: %s", node.get("id"), e)
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        logger.error("Label generation exhausted retries for node %s, skipping", node.get("id"))
        return None, None

    async def _describe_with_retries(self, node, context, existing_description: str = ""):
        """Describe one node with transient-error retries. None on failure."""
        for attempt in range(3):
            try:
                await self._wait_for_backoff()
                async with self._host._enrichment_semaphore:
                    return await self._live_description(node, context, existing_description=existing_description)
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                logger.warning("Transient error describing node %s (attempt %d): %s", node.get("id"), attempt + 1, e)
            except Exception as e:
                self._note_rate_limit(e)
                logger.error("Description generation failed for node %s: %s", node.get("id"), e)
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        logger.error("Description generation exhausted retries for node %s, skipping", node.get("id"))
        return None

    # --- batched labeling -----------------------------------------------------

    async def _run_label_batch(self, batch: list, all_nodes: list, compiled: dict,
                               used_names: list, _depth: int = 0) -> tuple:
        """One batched labeling call. Returns (results, leftovers): results maps
        node_id -> (name, snippet) for entries that validated; leftovers are
        nodes to re-run as single-node calls (missing/invalid/duplicate names,
        or the whole batch when the call itself kept failing)."""
        if len(batch) == 1:
            # Degenerate batch: the single-node path has the better retry story.
            return {}, list(batch)
        contexts = {
            n.get("id"): build_enrichment_context(n, all_nodes, compiled, include_descriptions=False)
            for n in batch
        }
        try:
            await self._wait_for_backoff()
            async with self._host._enrichment_semaphore:
                parsed = await self._live_label_batch(batch, contexts, used_names)
        except Exception as e:
            self._note_rate_limit(e)
            if _depth == 0 and len(batch) >= 4:
                logger.warning("Batch labeling failed (%d nodes), bisecting: %s", len(batch), e)
                mid = len(batch) // 2
                res_a, left_a = await self._run_label_batch(batch[:mid], all_nodes, compiled, used_names, _depth=1)
                res_b, left_b = await self._run_label_batch(batch[mid:], all_nodes, compiled, used_names, _depth=1)
                res_a.update(res_b)
                return res_a, left_a + left_b
            logger.warning("Batch labeling failed (%d nodes), falling back to single calls: %s", len(batch), e)
            return {}, list(batch)

        entries = parsed.get("nodes") if isinstance(parsed, dict) else None
        by_id = {}
        for entry in (entries if isinstance(entries, list) else []):
            if isinstance(entry, dict) and entry.get("id") is not None:
                by_id[str(entry["id"])] = entry

        results = {}
        leftovers = []
        seen = {str(n).strip().lower() for n in used_names if n}
        for node in batch:
            node_id = node.get("id")
            entry = by_id.get(str(node_id))
            name = _strip_leading_the(str((entry or {}).get("name") or "")).strip()
            if not name or name.lower() in seen:
                leftovers.append(node)
                continue
            seen.add(name.lower())
            results[node_id] = (name, str((entry or {}).get("label_description") or ""))
        return results, leftovers

    async def _live_label_batch(self, batch: list, contexts: dict, used_names: list) -> dict:
        host = self._host
        model = self._llm.module_fast_model or self._llm.reader_model
        temperature = host._world_builder_temperature or 0.9

        # Same world for every node in the batch.
        world = contexts.get(batch[0].get("id"), {}).get("world", {})

        lines = []
        for i, node in enumerate(batch, 1):
            ctx = contexts.get(node.get("id"), {})
            region = ctx.get("region", {})
            layer = ctx.get("layer", {})
            neighbor_names = [n.get("name") for n in ctx.get("neighbors", [])[:4] if n.get("name")]
            parts = [
                f"{i}. id: {node.get('id')}",
                f"type: {node.get('type', 'waypoint')}",
                f"importance: {node.get('importance', 0)}/10",
            ]
            if region.get("name"):
                parts.append(f"region: {region.get('name')} ({region.get('terrain', '')}, {region.get('climate', '')})")
            if layer.get("name"):
                parts.append(f"layer: {layer.get('name')} ({layer.get('type', 'surface')})")
            terrain = ctx.get("terrain", {})
            if terrain.get("biome"):
                parts.append(f"terrain: {terrain['biome']}")
            if neighbor_names:
                parts.append(f"near: {', '.join(neighbor_names)}")
            connection = ctx.get("connection", {})
            if connection:
                parts.append(f"NOTE: {_connection_block(connection, ctx.get('vocab'))}")
            lines.append(" | ".join(parts))
        nodes_block = "\n".join(lines)

        avoid = [str(n) for n in used_names if n][-40:]
        avoid_block = (
            "Already-used names (do NOT reuse or lightly vary these):\n" + ", ".join(avoid) + "\n\n"
        ) if avoid else ""

        system = host._get_prompt(
            "enrich_label_batch_system",
            "You are a world-building AI. Name several map locations at once. Give each a concise, "
            "evocative name and a one-line label description. Names must be distinct from each other "
            "and from the already-used names; vary naming styles across the batch. Never begin a name "
            "with the word \"The\".",
        )
        user_msg = host._get_prompt(
            "enrich_label_batch_user",
            f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

{avoid_block}Locations to name:
{nodes_block}

Generate a unique, fitting name and a short one-line label_description for EVERY location above.
Output ONLY valid JSON: {{"nodes": [{{"id": "...", "name": "...", "label_description": "..."}}, ...]}} with exactly {len(batch)} entries whose ids match the list.""",
            world_name=world.get('name', 'Unknown'),
            world_genre=world.get('genre', ''),
            world_tone=world.get('tone', ''),
            world_premise=world.get('premise', ''),
            nodes_block=nodes_block,
            used_names=", ".join(avoid),
            batch_size=str(len(batch)),
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        return await json_retry_completion(
            self._llm,
            messages=messages,
            model=model,
            temperature=temperature,
            inspector_ctx={"call_type": "world_build", "step": "enrich:label_batch"},
            step_label=f"enrich:label_batch:{len(batch)}",
            retry_attempts=host._json_retry_attempts,
        )

    # --- batch run ------------------------------------------------------------

    def cancel(self, world_id: str):
        """Request cancellation of an in-flight run for this world (checked
        between nodes; already-saved results are kept and flushed)."""
        self._cancel_flags.add(world_id)

    def _pending_for_phase(self, all_nodes: list, layer_map: dict, phase: str,
                           layer_filter: str, rework: bool,
                           importance_floor: int = None,
                           node_ids: list = None) -> tuple:
        """Work queue + progress baseline for one run phase.

        Returns (pending, per_layer, done, total) where pending is importance-
        sorted, per_layer mirrors the shape the frontend progress bars consume,
        and done/total match the legacy *_next counters for that phase.

        ``node_ids`` narrows the run to an explicit target set (play-time
        backfill of specific nodes) and takes precedence over
        ``importance_floor``, which narrows it to major locations
        (importance >= floor). When either is set, done/total and the per-layer
        counters are scoped to the targeted nodes so progress reads as complete
        when the targeted work is done."""
        in_scope = [n for n in all_nodes
                    if not layer_filter
                    or n.get("map_id", n.get("layer_id", "")) == layer_filter]
        scoped = node_ids is not None or importance_floor is not None
        if node_ids is not None:
            wanted = {str(nid) for nid in node_ids}
            in_scope = [n for n in in_scope if str(n.get("id")) in wanted]
        elif importance_floor is not None:
            in_scope = [n for n in in_scope if n.get("importance", 0) >= importance_floor]

        if phase == "label":
            if rework:
                pending = [n for n in in_scope if n.get("name")]
                total = len(pending)
                done = 0
            elif scoped:
                pending = [n for n in in_scope if not n.get("name")]
                total = len(in_scope)
                done = sum(1 for n in in_scope if n.get("name"))
            else:
                pending = [n for n in in_scope if not n.get("name")]
                total = sum(info["total"] for info in layer_map.values())
                done = sum(1 for n in all_nodes if n.get("name"))
            done_field = "name"
        else:
            if rework:
                pending = [n for n in in_scope if n.get("name") and n.get("description")]
                total = len(pending)
                done = 0
            elif scoped:
                pending = [n for n in in_scope if n.get("name") and not n.get("description")]
                total = sum(1 for n in in_scope if n.get("name"))
                done = sum(1 for n in in_scope if n.get("description"))
            else:
                pending = [n for n in in_scope if n.get("name") and not n.get("description")]
                total = sum(1 for n in all_nodes if n.get("name"))
                done = sum(1 for n in all_nodes if n.get("description"))
            done_field = "description"

        count_pool = in_scope if scoped else all_nodes
        per_layer = {}

        def _map_key(n):
            return n.get("map_id", n.get("layer_id", "")) or "main"

        for lid, info in layer_map.items():
            lid_done = 0 if rework else sum(
                1 for n in count_pool
                if _map_key(n) == (lid or "main") and n.get(done_field)
            )
            lid_total = info["total"] if not scoped else sum(
                1 for n in in_scope if _map_key(n) == (lid or "main")
            )
            per_layer[lid] = {"done": lid_done, "total": lid_total}

        pending.sort(key=lambda n: -n.get("importance", 0))
        return pending, per_layer, done, total

    async def run(self, world_id: str, phase: str = "all", count: int = None,
                  layer_filter: str = None, rework: bool = False,
                  exclude_node_ids: list = None, concurrency: int = 3,
                  batch_size: int = 8, on_event=None,
                  importance_floor: int = None, node_ids: list = None) -> dict:
        """Enrich many nodes in one server-driven run with bounded concurrency.

        ``phase`` is "label", "describe" or "all" (label to completion, then
        describe). The compiled world + terrain rasters are loaded once for the
        whole run instead of per node. Progress is reported through ``on_event``
        (async callable) as {"type": "node"|"failed"|"phase"|"done", ...} dicts.
        Results are write-cached per node and flushed to disk every few nodes,
        at phase end and on cancellation.

        ``importance_floor`` limits the run to major locations
        (importance >= floor); ``node_ids`` limits it to an explicit target set
        and wins over the floor. See ``_pending_for_phase``.
        """
        if not self._llm or self._llm.mode == "mock":
            raise RuntimeError("Enrichment requires an LLM service. The mock enrichment has been removed.")
        if phase not in ("label", "describe", "all"):
            raise ValueError(f"Unknown enrichment phase: {phase}")

        async def emit(evt: dict):
            if on_event is None:
                return
            try:
                await on_event(evt)
            except Exception:
                logger.warning("enrichment run event callback failed", exc_info=True)

        concurrency = max(1, int(concurrency))
        self._cancel_flags.discard(world_id)
        compiled = self._load_compiled(world_id)
        # One canonical node list for the whole run: node dicts are mutated in
        # memory as labels land so the describe phase sees fresh neighbor names
        # without re-loading the world from disk.
        all_nodes, layer_map = collect_nodes_by_layer(compiled)

        summary = {"labeled": 0, "described": 0, "failed_node_ids": [], "cancelled": False}
        flush_pending = 0

        try:
            for ph in (("label", "describe") if phase == "all" else (phase,)):
                pending, per_layer, done, total = self._pending_for_phase(
                    all_nodes, layer_map, ph, layer_filter, rework,
                    importance_floor=importance_floor, node_ids=node_ids)
                if exclude_node_ids:
                    skip = set(exclude_node_ids)
                    pending = [n for n in pending if n.get("id") not in skip]
                if count is not None:
                    pending = pending[:max(0, int(count))]
                await emit({"type": "phase", "phase": ph, "pending": len(pending),
                            "total_labeled": done, "total_nodes": total,
                            "per_layer": per_layer})
                if not pending:
                    continue

                queue = asyncio.Queue()
                if ph == "label" and batch_size > 1:
                    # Batched labeling: several nodes per LLM call. Invalid or
                    # duplicate entries get re-queued as single nodes.
                    for i in range(0, len(pending), batch_size):
                        queue.put_nowait(pending[i:i + batch_size])
                else:
                    for n in pending:
                        queue.put_nowait(n)

                # Names already on the map + assigned during this run; recent
                # ones feed batch prompts as a "do not reuse" list.
                used_names = [n["name"] for n in all_nodes if n.get("name")]

                def progress_snapshot():
                    # Copy the shared counters: events sit in the SSE queue while
                    # other workers keep mutating per_layer.
                    return {"total_labeled": done, "total_nodes": total,
                            "per_layer": {lid: dict(v) for lid, v in per_layer.items()}}

                async def record_result(node, event_fields: dict):
                    nonlocal done, flush_pending
                    done += 1
                    lid = node.get("map_id", node.get("layer_id", ""))
                    layer_key = lid if lid in per_layer else "main"
                    if layer_key in per_layer:
                        per_layer[layer_key]["done"] += 1
                    flush_pending += 1
                    if flush_pending >= 10:
                        flush_pending = 0
                        self._host._flush_enrichment_cache(world_id)
                    await emit({"type": "node", "phase": ph, "node_id": node.get("id"),
                                "layer_id": lid, **event_fields, **progress_snapshot()})

                async def record_failure(node):
                    summary["failed_node_ids"].append(node.get("id"))
                    await emit({"type": "failed", "phase": ph, "node_id": node.get("id"),
                                "layer_id": node.get("layer_id", ""), **progress_snapshot()})

                def store_label(node, name, snippet):
                    node_id = node.get("id")
                    self._host._save_node_enrichment(world_id, node_id, "name", name)
                    self._update_cached_node(compiled, node_id, "name", name)
                    node["name"] = name
                    used_names.append(name)
                    if snippet:
                        self._host._save_node_enrichment(world_id, node_id, "label_description", snippet)
                        self._update_cached_node(compiled, node_id, "label_description", snippet)
                        node["label_description"] = snippet
                    summary["labeled"] += 1

                async def worker():
                    while world_id not in self._cancel_flags:
                        try:
                            item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return

                        if isinstance(item, list):
                            results, leftovers = await self._run_label_batch(
                                item, all_nodes, compiled, used_names)
                            for node in item:
                                got = results.get(node.get("id"))
                                if got is None:
                                    continue
                                name, snippet = got
                                store_label(node, name, snippet)
                                await record_result(node, {"label": name, "label_description": snippet})
                            for node in leftovers:
                                queue.put_nowait(node)
                            continue

                        node = item
                        if ph == "label":
                            context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=False)
                            name, snippet = await self._label_with_retries(node, context)
                            if name is None:
                                await record_failure(node)
                                continue
                            store_label(node, name, snippet)
                            await record_result(node, {"label": name, "label_description": snippet})
                        else:
                            context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=True)
                            existing = node.get("description", "") if rework else ""
                            desc_with_links = await self._describe_with_retries(node, context, existing)
                            if desc_with_links is None:
                                await record_failure(node)
                                continue
                            desc = postprocess_links(desc_with_links, node, all_nodes)
                            node_id = node.get("id")
                            self._host._save_node_enrichment(world_id, node_id, "description", desc)
                            self._update_cached_node(compiled, node_id, "description", desc)
                            node["description"] = desc
                            summary["described"] += 1
                            await record_result(node, {"description": desc})

                await asyncio.gather(*(worker() for _ in range(min(concurrency, queue.qsize()))))
                self._host._flush_enrichment_cache(world_id)
                flush_pending = 0
                if world_id in self._cancel_flags:
                    summary["cancelled"] = True
                    break
        finally:
            if flush_pending:
                self._host._flush_enrichment_cache(world_id)
            self._cancel_flags.discard(world_id)
            # Keep the cheap compiled JSON cached but free the decompressed
            # rasters (tens of MB — matters on Termux).
            self.release_terrain(world_id)

        await emit({"type": "done", **summary})
        return summary

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
        terrain_str = _terrain_line(context.get("terrain", {}))

        host = self._host
        model = self._llm.module_fast_model or self._llm.reader_model
        temperature = host._world_builder_temperature or 0.9

        system = host._get_prompt(
            "enrich_label_system",
            "You are a world-building AI. Generate a concise, evocative name and a one-line label description for a map node.",
        )
        guidance = ["Do not begin the name with the word \"The\"."]
        connection_str = _connection_block(context.get("connection", {}), context.get("vocab"))
        if connection_str:
            guidance.append(connection_str)
        system = system + "\n\n" + "\n".join(guidance)
        user_msg = host._get_prompt(
            "enrich_label_user",
            f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
- Climate: {region.get('climate', '')}
{factions_str}{landmarks_str}{terrain_str}
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
            node_biome=context.get("terrain", {}).get("biome", ""),
            node_elevation=context.get("terrain", {}).get("elevation_band", ""),
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
            return _strip_leading_the(result.get("name", "Unknown")), result.get("label_description", "")
        except Exception as e:
            logger.error(f"Label generation failed for node {node_id}: {e}")
            raise

    async def _live_description(self, node: dict, context: dict, existing_description: str = "") -> str:
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
        model = self._llm.reader_model
        temperature = host._world_builder_temperature or 0.9

        if existing_description:
            system_fallback = (
                "You are a world-building AI. Revise and enrich an existing flavor description for a "
                "map location using fresh context about its neighbors. Preserve any still-fitting "
                "details from the original but deepen it with the new context. Reference neighboring "
                "locations using their ${link_ID} syntax."
            )
            rework_block = f"\nExisting description (revise/update, don't just repeat): {existing_description}\n"
            instruction = (
                "Rewrite this into an updated 1-3 sentence flavor description of this location, weaving in "
                "the nearby locations listed above. Reference neighbors using their link IDs like "
                "${link_n_0001} or ${link_a1b2} (the same format used in the neighbor list above)."
            )
        else:
            system_fallback = "You are a world-building AI. Write a short, atmospheric flavor description for a map location. Reference neighboring locations using their ${link_ID} syntax."
            rework_block = ""
            instruction = (
                "Write a 1-3 sentence flavor description of this location. Reference neighbors using "
                "their link IDs like ${link_n_0001} or ${link_a1b2} (the same format used in the neighbor list above)."
            )

        system = host._get_prompt("enrich_description_system", system_fallback)
        connection_str = _connection_block(context.get("connection", {}), context.get("vocab"))
        if connection_str:
            system = system + "\n\n" + connection_str
        user_msg = host._get_prompt(
            "enrich_description_user",
            f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
- Climate: {region.get('climate', '')}
{_terrain_line(context.get('terrain', {}))}
Location: {node_name}
Label: {label_description}
Type: {node_type}
Layer: {layer.get('name', 'surface')} ({layer.get('type', 'surface')})
Layer description: {layer.get('description', '')}
Nearby locations: {neighbor_str}
{rework_block}
{instruction}
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
            existing_description=existing_description,
            node_biome=context.get("terrain", {}).get("biome", ""),
            node_elevation=context.get("terrain", {}).get("elevation_band", ""),
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
