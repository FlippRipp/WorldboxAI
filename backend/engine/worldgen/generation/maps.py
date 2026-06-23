"""Procedural map generation — delegates to ``world_map`` (no LLM)."""

import logging

from backend.engine.world_map import generate_map as _generate_map_static
from backend.engine.world_map import generate_multilayer_map as _generate_multilayer_map
from backend.engine.worldgen.compiler import build_compiled_for_map

logger = logging.getLogger(__name__)


class MapStepGenerator:
    """Generates the node-graph world map from prior step data."""

    def generate(self, world_state: dict, config: dict = None) -> dict:
        compiled = build_compiled_for_map(world_state)

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
                return _generate_map_static(compiled, total_nodes=total_nodes).to_dict()
            connections_spec = layer_design_data.get("connections", [])
            return _generate_multilayer_map(
                compiled,
                layer_specs=layer_specs,
                connections_spec=connections_spec,
                total_nodes=total_nodes,
            )

        return _generate_map_static(compiled, total_nodes=total_nodes).to_dict()
