"""Compiled-world cache shared by the generation engines.

Size 1: the actively-worked world. Skips the full world re-read + compile +
terrain npz decompress per node call during enrichment/expansion runs.
Entries are mutated in place as enrichment lands (``update_node``) and must
be dropped (``invalidate``) whenever the world's step files are written by
anything else — the facade's save paths do this explicitly.

Owned by the WorldBuilder facade; engines receive it via
``GenServices.compiled``. Extracted from ``EnrichmentEngine`` so the
invalidation contract is visible instead of buried in one engine.
"""

import logging
from typing import Any, Callable

from wbworldgen.worldgen.compiler import compile_world

logger = logging.getLogger(__name__)


class CompiledWorldCache:
    def __init__(self, load_world: Callable[[str], dict], steps: dict = None,
                 terrain_store: Any = None):
        #: ``load_world`` is called late (never captured results) so callers
        #: that rebind the facade's loader are always seen.
        self._load_world = load_world
        self._steps = steps
        self._terrain_store = terrain_store
        self._cache: dict = {}

    def __contains__(self, world_id: str) -> bool:
        return world_id in self._cache

    def load(self, world_id: str) -> dict:
        """The compiled world, cached; terrain rasters attached best-effort."""
        compiled = self._cache.get(world_id)
        if compiled is None:
            world_data = self._load_world(world_id)
            compiled = compile_world(world_data, self._steps)
            tg = world_data.get("steps", {}).get("terrain_generation", {}).get("data", {})
            compiled["_terrain_meta"] = tg.get("layers", []) if isinstance(tg, dict) else []
            self._cache.clear()
            self._cache[world_id] = compiled
        self._ensure_terrain(world_id, compiled)
        return compiled

    def invalidate(self, world_id: str = None):
        """Drop cached compiled state (after the world's step files were
        written by something other than enrichment, or the world was
        deleted)."""
        if world_id is None:
            self._cache.clear()
        else:
            self._cache.pop(world_id, None)

    def release_terrain(self, world_id: str):
        """Free the decompressed terrain rasters (tens of MB) while keeping
        the cheap compiled JSON cached; they lazily re-attach on the next
        ``load``."""
        compiled = self._cache.get(world_id)
        if compiled is not None:
            compiled.pop("_terrain_layers", None)

    def node_index(self, compiled: dict) -> dict:
        """Lazily-built {node_id: node dict} index over the compiled world's
        own node dicts (not the per-call copies handed to prompts)."""
        index = compiled.get("_node_by_id")
        if index is None:
            from wbworldgen.worldgen import mapspace as _ms
            index = {n.get("id"): n for n in _ms.all_nodes(compiled)}
            compiled["_node_by_id"] = index
        return index

    def get_node(self, world_id: str, node_id: str) -> dict | None:
        """Current state of one map node (post-enrichment fields included)."""
        compiled = self.load(world_id)
        return self.node_index(compiled).get(node_id)

    def update_node(self, compiled: dict, node_id: str, field: str, value: str):
        """Mirror an enrichment write onto the compiled world's own node
        dicts so the cached compiled state stays truthful across calls/runs
        (the node lists handed to prompts are per-call copies)."""
        node = self.node_index(compiled).get(node_id)
        if node is not None:
            node[field] = value

    def _ensure_terrain(self, world_id: str, compiled: dict):
        """Load persisted terrain rasters per layer so enrichment context can
        sample biome/elevation at each node's coordinate. Best-effort; no-op
        when already attached or no terrain store is wired."""
        if "_terrain_layers" in compiled:
            return
        if self._terrain_store is None:
            return
        try:
            from wbworldgen.worldgen import terrain_store as _ts
            terrain_by_layer = {}
            for tl in compiled.get("_terrain_meta", []):
                lid = tl.get("layer_id", "main")
                out_dir = self._terrain_store.terrain_dir(world_id, lid)
                layers = _ts.load_terrain(str(out_dir))
                if layers:
                    # Keyed by the terrain step's layer id; nodes are tagged
                    # with their map's legacy_layer_id, and the single-entry
                    # fallback in _terrain_for_node covers any mismatch.
                    terrain_by_layer[lid] = layers
            # Terrain-flagged child maps (a planet opened from a star system)
            # carry a terrain marker in their config, keyed by their map id —
            # the same id their nodes are tagged with.
            from wbworldgen.worldgen import mapspace as _ms
            for mid, m in _ms.maps_by_id(compiled).items():
                if mid in terrain_by_layer or not (m.get("config") or {}).get("terrain"):
                    continue
                layers = _ts.load_terrain(
                    str(self._terrain_store.terrain_dir(world_id, mid)))
                if layers:
                    terrain_by_layer[mid] = layers
            if terrain_by_layer:
                compiled["_terrain_layers"] = terrain_by_layer
        except Exception as e:
            logger.warning("attach terrain for enrichment failed (%s): %s", world_id, e)
