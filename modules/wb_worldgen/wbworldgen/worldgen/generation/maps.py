"""Procedural map generation — delegates to ``world_map`` (no LLM)."""

import logging

from wbworldgen.world_map import generate_map as _generate_map_static
from wbworldgen.world_map import generate_multilayer_map as _generate_multilayer_map
from wbworldgen.worldgen.compiler import build_compiled_for_map
from wbworldgen.worldgen import terrain_store as _ts
from wbworldgen.worldgen.persistence import WorldPersistence

logger = logging.getLogger(__name__)


class MapStepGenerator:
    """Generates the node-graph world map from prior step data."""

    def __init__(self, worlds_dir: str = "data/worlds"):
        self._persistence = WorldPersistence(worlds_dir)

    def _load_terrain(self, world_id: str, layer_id: str) -> dict:
        """Load persisted terrain rasters for a layer, or {} if none exist."""
        if not world_id:
            return {}
        try:
            out_dir = self._persistence.terrain_dir(world_id, layer_id or "main")
            return _ts.load_terrain(str(out_dir))
        except Exception as e:
            logger.warning("terrain load failed for %s/%s: %s", world_id, layer_id, e)
            return {}

    def generate(self, world_state: dict, config: dict = None) -> dict:
        compiled = build_compiled_for_map(world_state)
        world_id = compiled.get("world_id", "") or world_state.get("_draft_id", "")

        total_nodes = 100
        if config:
            total_nodes = config.get("total_nodes", 100)
        elif world_state.get("steps", {}).get("map_generation", {}).get("data", {}).get("total_nodes"):
            total_nodes = world_state["steps"]["map_generation"]["data"]["total_nodes"]
        total_nodes = max(30, min(500, int(total_nodes)))

        layer_design_data = world_state.get("steps", {}).get("layer_design", {}).get("data", {})
        if (
            isinstance(layer_design_data, dict)
            and layer_design_data.get("has_multiple_layers")
            and layer_design_data.get("layers")
        ):
            layer_specs = [s for s in layer_design_data.get("layers", []) if isinstance(s, dict)]
            if not layer_specs:
                logger.warning(
                    "layer_design has_multiple_layers=true but no valid layer dicts, "
                    "falling back to single-layer map"
                )
                terrain = self._load_terrain(world_id, "main")
                return _generate_map_static(compiled, total_nodes=total_nodes,
                                            terrain=terrain).to_dict()
            connections_spec = layer_design_data.get("connections", [])
            placement = layer_design_data.get("connection_placement", "edges")
            if placement not in ("edges", "central", "random", "scattered"):
                placement = "edges"
            # Per-layer terrain (only surface-like layers have rasters on disk).
            terrain_by_layer = {
                s.get("layer_id"): self._load_terrain(world_id, s.get("layer_id"))
                for s in layer_specs
            }
            return _generate_multilayer_map(
                compiled,
                layer_specs=layer_specs,
                connections_spec=connections_spec,
                total_nodes=total_nodes,
                connection_placement=placement,
                terrain_by_layer=terrain_by_layer,
            )

        terrain = self._load_terrain(world_id, "main")
        return _generate_map_static(compiled, total_nodes=total_nodes,
                                    terrain=terrain).to_dict()
