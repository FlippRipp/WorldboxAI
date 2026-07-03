"""Terrain generation step — builds raster terrain for surface-like layers.

Runs right after ``layer_design`` (and before ``terrain_regions``) so the AI can
author regions/landmarks that fit the actual generated geography. Every layer is
a separate area, so each one gets its own heightmap/biome raster (reusing
``wbworldgen.terrain``) seeded distinctly; the arrays + rendered images are
persisted under the world's terrain directory and a small per-layer summary flows
into downstream prompts via the normal chain context.
"""

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
    after = "layer_rules"
    uses = USES_MAP  # no LLM; bespoke generate below
    schema = {
        "layers": {"type": "list", "label": "Terrain Layers", "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "name": {"type": "string", "label": "Layer Name"},
            "summary": {"type": "text", "label": "Terrain Summary"},
        }},
    }

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

        out_layers = []
        for spec in _layer_specs(world_state):
            ltype = str(spec.get("layer_type", "surface")).lower()
            lid = spec.get("layer_id") or "main"
            seed = _layer_seed(world_id, int(spec.get("index", 0)))
            # Underground layers get the bespoke cave generator (tunnels/caverns,
            # subterranean water, cave biomes); everything else gets surface
            # terrain. Both emit the same ``layers`` contract downstream.
            is_cave = ltype == "underground"
            if is_cave:
                params = CaveParams(seed=seed, resolution=resolution)
                result = generate_cave_terrain(params)
                summary_mode = "cave"
            else:
                params = TerrainParams(seed=seed, resolution=resolution,
                                       biome_mode=biome_mode)
                result = generate_terrain(params)
                summary_mode = biome_mode
            out_dir = persistence.terrain_dir(world_id, lid)
            meta = _ts.save_terrain(str(out_dir), result.layers, result.params)
            summary = _ts.build_terrain_summary(result.layers, summary_mode)
            out_layers.append({
                "layer_id": lid,
                "name": spec.get("name", lid),
                "layer_type": ltype,
                "seed": seed,
                "resolution": resolution,
                "summary": _summary_text(summary),
                "summary_data": summary,
                **meta,
            })
            logger.info("terrain generated for layer %s (%s)", lid, world_id)

        return {"layers": out_layers, "world_id": world_id}


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
