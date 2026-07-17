"""Terrain generation step — builds raster terrain for surface-like layers.

Runs right after ``hierarchy_design`` so later authoring steps can
author regions/landmarks that fit the actual generated geography. Every layer is
a separate area, so each one gets its own heightmap/biome raster (reusing
``wbworldgen.terrain``) seeded distinctly; the arrays + rendered images are
persisted under the world's terrain directory and a small per-layer summary flows
into downstream prompts via the normal chain context.
"""

import asyncio
import logging
import zlib

from wbworldgen.terrain.pipeline import generate_terrain, TerrainParams
from wbworldgen.terrain.caves import generate_cave_terrain, CaveParams
from wbworldgen.worldgen import terrain_store as _ts
from wbworldgen.worldgen.base import Step, register, USES_MAP
from wbworldgen.worldgen.persistence import resolve_world_id

logger = logging.getLogger(__name__)

_TERRAIN_RESOLUTION = 1024


def _layer_specs(world_state: dict) -> list[dict]:
    """Resolve the list of layers to consider, single-layer worlds included."""
    ld = world_state.get("steps", {}).get("layer_design", {}).get("data", {})
    if isinstance(ld, dict) and ld.get("has_multiple_layers") and ld.get("layers"):
        return [s for s in ld["layers"] if isinstance(s, dict)]
    return [{"layer_id": "main", "name": "Overworld", "layer_type": "surface", "index": 0}]


def _layer_seed(world_id: str, index: int) -> int:
    return zlib.crc32(f"{world_id}:{index}".encode("utf-8")) & 0x7FFFFFFF


@register
class TerrainGenerationStep(Step):
    id = "terrain_generation"
    label = "Terrain"
    description = (
        "Generate the physical terrain (elevation, biomes, rivers) for each "
        "surface layer. Drives where settlements, landmarks and roads are placed."
    )
    after = "hierarchy_design"
    uses = USES_MAP  # no LLM; bespoke generate below
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
                None, _build_layer_terrain, world_id, spec, resolution, biome_mode, persistence)
            out_layers.append(entry)
            logger.info("terrain generated for layer %s (%s)", entry["layer_id"], world_id)

        return {"layers": out_layers, "world_id": world_id}


def _build_layer_terrain(world_id: str, spec: dict, resolution: int,
                         biome_mode: str, persistence) -> dict:
    """Generate + persist one layer's terrain. Pure CPU + disk on local data —
    safe to run in a worker thread."""
    ltype = str(spec.get("layer_type", "surface")).lower()
    lid = spec.get("layer_id") or "main"
    seed = _layer_seed(world_id, int(spec.get("index", 0)))
    # Underground layers get the bespoke cave generator (tunnels/caverns,
    # subterranean water, cave biomes); everything else gets surface
    # terrain. Both emit the same ``layers`` contract downstream.
    if ltype == "underground":
        result = generate_cave_terrain(CaveParams(seed=seed, resolution=resolution))
        summary_mode = "cave"
    else:
        result = generate_terrain(TerrainParams(seed=seed, resolution=resolution,
                                                biome_mode=biome_mode))
        summary_mode = biome_mode
    out_dir = persistence.terrain_dir(world_id, lid)
    meta = _ts.save_terrain(str(out_dir), result.layers, result.params)
    summary = _ts.build_terrain_summary(result.layers, summary_mode)
    return {
        "layer_id": lid,
        "name": spec.get("name", lid),
        "layer_type": ltype,
        "seed": seed,
        "resolution": resolution,
        "summary": _summary_text(summary),
        "summary_data": summary,
        **meta,
    }


def _summary_text(summary: dict) -> str:
    """Human/LLM-readable one-paragraph digest of a terrain summary."""
    if summary.get("underground"):
        biomes = ", ".join(
            f"{name} {pct}%" for name, pct in
            list(summary.get("biome_percent", {}).items())[:6]
        )
        feats = []
        if summary.get("has_rivers"):
            feats.append("underground rivers")
        if summary.get("has_lakes"):
            feats.append("subterranean lakes")
        return (
            "Underground cavern network. "
            f"Navigable cavern/tunnel coverage: "
            f"{round(summary.get('open_fraction', 0) * 100)}% (rest is solid rock). "
            f"Water: {', '.join(feats) or 'none notable'}. "
            f"Cave biome mix (% of open ground): {biomes}."
        )
    biomes = ", ".join(
        f"{name} {pct}%" for name, pct in list(summary.get("biome_percent", {}).items())[:6]
    )
    feats = []
    if summary.get("has_coastline"):
        feats.append("coastline")
    if summary.get("has_rivers"):
        feats.append("rivers")
    if summary.get("has_lakes"):
        feats.append("lakes")
    elev = summary.get("elevation", {})
    return (
        f"Climate: {summary.get('dominant_climate', 'temperate')}. "
        f"Land coverage: {round(summary.get('land_fraction', 0) * 100)}%. "
        f"Features: {', '.join(feats) or 'none notable'}. "
        f"Biome mix (% of land): {biomes}. "
        f"Elevation spread {elev.get('min')}–{elev.get('max')} (mean {elev.get('mean')})."
    )
