"""Terrain generation step — builds the root map's raster terrain.

Runs right after ``hierarchy_design`` so later authoring steps can author
regions/landmarks that fit the actual generated geography. For every world
built today this step produces exactly one raster stack: the root map's,
keyed ``main`` (the id ``map_generation`` and the terrain-image routes
load). Terrain-flagged child maps — a planet opened from a star system —
get their rasters from the map-expansion engine when the child map is
built (``terrain_build.build_layer_terrain``), never from this step, and
parallel maps are currently placed without rasters. The multi-layer branch
in ``_layer_specs`` serves only pre-hierarchy worlds carrying data from
the deprecated ``layer_design`` step. The arrays + rendered images are
persisted under the world's terrain directory and a per-layer summary
flows into downstream prompts via the normal chain context.
"""

import asyncio
import logging

from wbworldgen.worldgen.base import Step, register, USES_MAP
from wbworldgen.worldgen.persistence import resolve_world_id
from wbworldgen.worldgen.terrain_build import build_layer_terrain

logger = logging.getLogger(__name__)

_TERRAIN_RESOLUTION = 1024


def _layer_specs(world_state: dict) -> list[dict]:
    """The layer list to rasterize. Modern worlds always take the fallback:
    the single root entry keyed ``main``. The multi-layer branch reads the
    deprecated ``layer_design`` step and is reachable only when re-running
    terrain on a pre-hierarchy world that still carries its data —
    hierarchy_design's parallel/child maps are deliberately not rasterized
    here (terrain-flagged children get expansion-time terrain instead)."""
    ld = world_state.get("steps", {}).get("layer_design", {}).get("data", {})
    if isinstance(ld, dict) and ld.get("has_multiple_layers") and ld.get("layers"):
        return [s for s in ld["layers"] if isinstance(s, dict)]
    return [{"layer_id": "main", "name": "Overworld", "layer_type": "surface", "index": 0}]


@register
class TerrainGenerationStep(Step):
    id = "terrain_generation"
    label = "Terrain"
    description = (
        "Generate the physical terrain raster (elevation, biomes, rivers) for "
        "the world's ROOT map. Drives where settlements, landmarks and roads "
        "are placed. Never produces per-planet or per-parallel-map layers — "
        "terrain-flagged child maps get their own rasters at expansion time, "
        "when the child map itself is built; re-running this step cannot add "
        "layers."
    )
    after = "hierarchy_design"
    uses = USES_MAP  # no LLM; bespoke generate below
    requires = ("hierarchy",)
    produces = ("terrain",)
    schema = {
        "layers": {"type": "list", "label": "Terrain Layers", "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "name": {"type": "string", "label": "Layer Name"},
            "summary": {"type": "text", "label": "Terrain Summary"},
        }},
    }

    def context_view(self, data):
        """Downstream prompts only need the readable per-layer ``summary`` —
        the structured ``summary_data`` (biome histograms) duplicates it and
        the image filenames mean nothing to an LLM."""
        if not isinstance(data, dict):
            return data
        layers = [
            {k: v for k, v in tl.items() if k not in ("summary_data", "images")}
            if isinstance(tl, dict) else tl
            for tl in data.get("layers", [])
        ]
        return {**data, "layers": layers}

    async def generate(self, ctx) -> dict:
        world_state = ctx.world_state
        services = ctx.services
        world_id = resolve_world_id(world_state)
        # Pin the id so the subsequent draft save writes to the same directory.
        world_state["_draft_id"] = world_id
        persistence = services._persistence

        config = ctx.config or {}
        resolution = int(config.get("resolution", _TERRAIN_RESOLUTION))
        biome_mode = config.get("biome_mode", "realistic")

        loop = asyncio.get_running_loop()
        out_layers = []
        # Layers stay sequential (each raster pipeline peaks at several 1024²
        # arrays); the executor offload is about keeping the event loop — and
        # with it the API/chat — responsive during the CPU-heavy generation.
        for spec in _layer_specs(world_state):
            entry = await loop.run_in_executor(
                None, build_layer_terrain, world_id, spec, resolution, biome_mode, persistence)
            out_layers.append(entry)
            logger.info("terrain generated for layer %s (%s)", entry["layer_id"], world_id)

        return {"layers": out_layers, "world_id": world_id}
