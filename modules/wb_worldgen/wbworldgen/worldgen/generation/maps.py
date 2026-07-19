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

    def _generate_with_parallel(self, compiled: dict, world_state: dict,
                                root_generator: str, parallel_maps: list,
                                total_nodes: int, seed=None) -> dict:
        """Parallel planes for a non-world_map root: each layer is built by
        its own generator (the root's, or the level matching the plane's
        level_type), then the planes are joined by crossings picked near the
        map borders. Output keeps the legacy multilayer shape ({layers,
        connections}) that compile_world already migrates into world_format 2
        maps + connections — same as the world_map multilayer path."""
        from wbworldgen.worldgen.generation.registry import GENERATOR_REGISTRY, get_generator
        from wbworldgen.worldgen.design import designed_levels

        levels = designed_levels(world_state)
        gen_by_level = {l.get("level_type"): l.get("generator_id") for l in levels}

        def _implemented(gid):
            spec = GENERATOR_REGISTRY.get(gid or "")
            return gid if spec is not None and spec.build is not None else "world_map"

        layer_specs = [{"layer_id": "root", "name": "",
                        "layer_type": (levels[0].get("level_type") if levels else "world") or "world",
                        "description": "", "index": 0,
                        "generator_id": _implemented(root_generator)}]
        connections_spec = []
        for i, pm in enumerate(parallel_maps):
            lid = _re.sub(r"[^a-z0-9]+", "_", str(pm["label"]).lower()).strip("_") or f"parallel_{i + 1}"
            layer_specs.append({
                "layer_id": lid,
                "name": pm.get("label", lid),
                "layer_type": pm.get("level_type", "world") or "world",
                "description": pm.get("description", ""),
                "index": i + 1,
                "generator_id": _implemented(gen_by_level.get(pm.get("level_type"))),
            })
            try:
                count = max(1, min(6, int(pm.get("connection_count") or 2)))
            except (TypeError, ValueError):
                count = 2
            connections_spec.append({
                "from_layer": "root", "to_layer": lid,
                "connection_type": pm.get("connection_kind", "passage") or "passage",
                "description": pm.get("description", ""),
                "count_hint": count,
            })

        nodes_per_layer = max(20, int(total_nodes) // max(1, len(layer_specs)))
        maps_by_layer: dict[str, dict] = {}
        for spec in layer_specs:
            lid = spec["layer_id"]
            layer_seed = (int(seed) + spec["index"] * 1000) if seed else None
            built = get_generator(spec["generator_id"]).build({
                "compiled_world": compiled,
                "total_nodes": nodes_per_layer,
                "seed": layer_seed,
                "id_prefix": f"{lid}_",
            })
            built["layer_id"] = lid
            maps_by_layer[lid] = built

        def _border_picks(map_dict, count, taken):
            cfg = map_dict.get("config", {}) or {}
            width = float(cfg.get("map_width", 1000) or 1000)
            height = float(cfg.get("map_height", 1000) or 1000)
            nodes = [n for n in map_dict.get("nodes", []) if n.get("id") not in taken]
            nodes.sort(key=lambda n: min(n.get("x", 0), n.get("y", 0),
                                         width - n.get("x", 0), height - n.get("y", 0)))
            pool = nodes[:max(count * 4, count)]
            stride = max(1, len(pool) // max(1, count))
            return [pool[i * stride] for i in range(count) if i * stride < len(pool)]

        layer_connections = []
        taken: dict[str, set] = {lid: set() for lid in maps_by_layer}
        counter = 0
        for cs in connections_spec:
            from_map = maps_by_layer.get(cs["from_layer"])
            to_map = maps_by_layer.get(cs["to_layer"])
            if not from_map or not to_map:
                continue
            count = cs["count_hint"]
            from_nodes = _border_picks(from_map, count, taken[cs["from_layer"]])
            to_nodes = _border_picks(to_map, count, taken[cs["to_layer"]])
            for fn, tn in zip(from_nodes, to_nodes):
                lc_id = f"lc_{counter:04d}"
                for node in (fn, tn):
                    node["type"] = cs["connection_type"]
                    node["importance"] = max(node.get("importance", 0) or 0, 4)
                    node["interlayer_connection_id"] = lc_id
                taken[cs["from_layer"]].add(fn["id"])
                taken[cs["to_layer"]].add(tn["id"])
                layer_connections.append({
                    "id": lc_id,
                    "from_layer_id": cs["from_layer"], "from_node_id": fn["id"],
                    "to_layer_id": cs["to_layer"], "to_node_id": tn["id"],
                    "connection_type": cs["connection_type"],
                    "name": f"{cs['connection_type'].replace('_', ' ').title()} #{counter + 1}",
                    "description": cs["description"],
                    "bidirectional": True,
                })
                counter += 1

        return {
            "layers": [{
                "layer_id": s["layer_id"],
                "name": s["name"],
                "description": s["description"],
                "layer_type": s["layer_type"],
                "index": s["index"],
                "map": maps_by_layer[s["layer_id"]],
            } for s in layer_specs],
            "connections": layer_connections,
            "config": {
                "total_nodes": total_nodes,
                "generated_from": compiled.get("generated_from", ""),
            },
        }

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

        def _bind_scope(nodes, scope_label, edges=None):
            content = scope_content.get(scope_label)
            if content and content.get("named_locations"):
                _bind_named_locations(nodes, content["named_locations"], edges)

        # hierarchy_design parallel maps become sibling maps of the root; the
        # output keeps the legacy multilayer shape, which compile_world
        # migrates into world_format 2 (maps + connections).
        hierarchy_data = steps_data.get("hierarchy_design", {}).get("data", {})
        parallel_maps = [p for p in (hierarchy_data.get("parallel_maps") or [])
                         if isinstance(p, dict) and p.get("label")]

        if generator_id and generator_id != "world_map":
            seed = (config or {}).get("seed") or step_data.get("seed") or None
            if parallel_maps:
                result = self._generate_with_parallel(
                    compiled, world_state, generator_id, parallel_maps,
                    total_nodes, seed)
                for layer in result.get("layers", []):
                    scope_label = "" if layer.get("layer_id") == "root" else layer.get("name", "")
                    lmap = layer.get("map", {})
                    _bind_scope(lmap.get("nodes", []), scope_label, lmap.get("edges"))
                return result
            from wbworldgen.worldgen.generation.registry import get_generator
            result = get_generator(generator_id).build({
                "compiled_world": compiled,
                "total_nodes": total_nodes,
                "seed": seed,
                "id_prefix": "",
            })
            _bind_scope(result.get("nodes", []), "", result.get("edges"))
            return result

        if parallel_maps:
            from wbworldgen.worldgen.design import designed_levels
            levels = designed_levels(world_state)
            root_layer_type = (levels[0].get("level_type") if levels else "world") or "world"
            layer_specs = [{"layer_id": "root", "name": "",
                            "layer_type": root_layer_type, "index": 0}]
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
                lmap = layer.get("map", {})
                _bind_scope(lmap.get("nodes", []), scope_label, lmap.get("edges"))
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
        _bind_scope(result.get("nodes", []), "", result.get("edges"))
        return result
