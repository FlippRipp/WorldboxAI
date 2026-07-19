"""Build + persist one map layer's terrain rasters.

The bridge between the self-contained ``wbworldgen.terrain`` package (the
raster pipelines) and the worldgen persistence layout: one call generates a
layer's terrain — the cave generator for underground layers, surface
terrain otherwise — writes the arrays and preview images under the world's
terrain directory, and returns the JSON-safe per-layer entry downstream
prompts and the map screen consume.

Shared by the terrain_generation pipeline step (creation-time rasters) and
the map-expansion engine (terrain-flagged child maps like a planet opened
from a star system), so it lives in core rather than inside a step module.
Pure CPU + disk on local data — safe to run in a worker thread.
"""

import zlib

from wbworldgen.terrain.pipeline import generate_terrain, TerrainParams
from wbworldgen.terrain.caves import generate_cave_terrain, CaveParams
from wbworldgen.worldgen import terrain_store as _ts


def layer_seed(world_id: str, index: int) -> int:
    return zlib.crc32(f"{world_id}:{index}".encode("utf-8")) & 0x7FFFFFFF


def build_layer_terrain(world_id: str, spec: dict, resolution: int,
                        biome_mode: str, persistence) -> dict:
    """Generate + persist one layer's terrain; returns the layer entry
    (seed, resolution, summary text/data, image metadata). ``persistence``
    needs only ``terrain_dir(world_id, layer_id)``."""
    ltype = str(spec.get("layer_type", "surface")).lower()
    lid = spec.get("layer_id") or "main"
    seed = layer_seed(world_id, int(spec.get("index", 0)))
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
        "summary": summary_text(summary),
        "summary_data": summary,
        **meta,
    }


def summary_text(summary: dict) -> str:
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
