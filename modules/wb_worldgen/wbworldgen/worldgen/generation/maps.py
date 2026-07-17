"""Procedural map generation — delegates to ``world_map`` (no LLM)."""

import logging

import re as _re

from wbworldgen.world_map import bind_named_locations as _bind_named_locations
from wbworldgen.world_map import generate_map as _generate_map_static
from wbworldgen.world_map import generate_multilayer_map as _generate_multilayer_map
from wbworldgen.worldgen.compiler import build_compiled_for_map, collect_scope_content
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

    def generate(self, world_state: dict, config: dict = None,
                 generator_id: str = "world_map") -> dict:
        compiled = build_compiled_for_map(world_state)
        world_id = compiled.get("world_id", "") or world_state.get("_draft_id", "")

        step_data = world_state.get("steps", {}).get("map_generation", {}).get("data", {})
        total_nodes = 100
        if config:
            total_nodes = config.get("total_nodes", 100)
        elif step_data.get("total_nodes"):
            total_nodes = step_data["total_nodes"]
        total_nodes = max(30, min(500, int(total_nodes)))

        steps_data = world_state.get("steps", {})
        scope_content = collect_scope_content(steps_data)

        def _bind_scope(nodes, scope_label):
            content = scope_content.get(scope_label)
            if content and content.get("named_locations"):
                _bind_named_locations(nodes, content["named_locations"])

        # hierarchy_design parallel maps become sibling maps of the root; the
        # output keeps the legacy multilayer shape, which compile_world
        # migrates into world_format 2 (maps + connections).
        hierarchy_data = steps_data.get("hierarchy_design", {}).get("data", {})
        parallel_maps = [p for p in (hierarchy_data.get("parallel_maps") or [])
                         if isinstance(p, dict) and p.get("label")]

        if generator_id and generator_id != "world_map":
            if parallel_maps:
                logger.warning(
                    "generator %s does not support parallel maps yet; "
                    "falling back to world_map multilayer", generator_id)
            else:
                from wbworldgen.worldgen.generation.registry import get_generator
                seed = (config or {}).get("seed") or step_data.get("seed") or None
                result = get_generator(generator_id).build({
                    "compiled_world": compiled,
                    "total_nodes": total_nodes,
                    "seed": seed,
                    "id_prefix": "",
                })
                _bind_scope(result.get("nodes", []), "")
                return result

        if parallel_maps:
            layer_specs = [{"layer_id": "root", "name": "", "layer_type": "world", "index": 0}]
            connections_spec = []
            for i, pm in enumerate(parallel_maps):
                lid = _re.sub(r"[^a-z0-9]+", "_", str(pm["label"]).lower()).strip("_") or f"parallel_{i + 1}"
                layer_specs.append({
                    "layer_id": lid,
                    "name": pm.get("label", lid),
                    "layer_type": pm.get("level_type", "world") or "world",
                    "description": pm.get("description", ""),
                    "index": i + 1,
                })
                try:
                    count = max(1, min(6, int(pm.get("connection_count") or 2)))
                except (TypeError, ValueError):
                    count = 2
                connections_spec.append({
                    "from_layer": "root",
                    "to_layer": lid,
                    "connection_type": pm.get("connection_kind", "passage") or "passage",
                    "description": pm.get("description", ""),
                    "count_hint": count,
                })
            terrain_by_layer = {"root": self._load_terrain(world_id, "main")}
            result = _generate_multilayer_map(
                compiled,
                layer_specs=layer_specs,
                connections_spec=connections_spec,
                total_nodes=total_nodes,
                connection_placement="edges",
                terrain_by_layer=terrain_by_layer,
            )
            for layer in result.get("layers", []):
                label = layer.get("name", "")
                scope_label = "" if layer.get("layer_id") == "root" else label
                _bind_scope(layer.get("map", {}).get("nodes", []), scope_label)
            return result

        layer_design_data = steps_data.get("layer_design", {}).get("data", {})
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
        result = _generate_map_static(compiled, total_nodes=total_nodes,
                                      terrain=terrain).to_dict()
        _bind_scope(result.get("nodes", []), "")
        return result
