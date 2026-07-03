"""Storage, summarization and sampling of per-layer terrain rasters.

The terrain raster generator (``wbworldgen.terrain``) produces large numpy
layers that are far too heavy to live inside the step JSON. This module persists
the subset needed downstream (placement, roads, enrichment) as a compressed
``.npz`` plus rendered ``biome.png`` / ``hillshade.png`` images, and provides:

  * ``build_terrain_summary`` — a small JSON-friendly digest (biome histogram,
    coastline/river/lake presence, elevation spread, dominant climate) fed into
    the AI authoring prompts via the normal chain context.
  * ``sample_terrain`` — look up biome/elevation/features at a node's map
    coordinate, used to give the enricher terrain awareness.

Coordinates everywhere downstream stay in the abstract ``map_width × map_height``
space; ``cell_at`` converts a map coordinate to a raster (row, col).
"""

import logging
import numpy as np

from wbworldgen.terrain import biomes as _bm
from wbworldgen.terrain import render as _render

logger = logging.getLogger(__name__)

# The array layers we keep on disk — everything placement / roads / sampling
# needs. (The full erosion/discharge fields are not persisted.)
_PERSIST_KEYS = (
    "height", "slope", "water", "biome", "moisture", "temperature",
    "river_mask", "lake_mask",
    # Cave layers only (absent keys are skipped for surface terrain).
    "open", "rock", "flooded",
)


def save_terrain(out_dir, layers: dict, params, map_width: float = 1000.0,
                 map_height: float = 1000.0) -> dict:
    """Persist terrain arrays + rendered images into ``out_dir``.

    Returns a small metadata dict (resolution, sea_level, image filenames, map
    dims) suitable for embedding in the step JSON.
    """
    import os

    res = int(layers["height"].shape[0])
    arrays = {}
    for k in _PERSIST_KEYS:
        v = layers.get(k)
        if v is None:
            continue
        if v.dtype == bool:
            arrays[k] = v
        elif np.issubdtype(v.dtype, np.integer):
            arrays[k] = v.astype(np.int16)
        else:
            arrays[k] = v.astype(np.float32)
    np.savez_compressed(os.path.join(out_dir, "layers.npz"),
                        sea_level=np.float32(layers["sea_level"]), **arrays)

    # Rendered images (mirrors backend/api/terrain_routes.py rendering).
    height = layers["height"]
    sea = layers["sea_level"]
    order = layers.get("order")
    paths = layers.get("river_paths")
    lake_mask = layers.get("lake_mask")
    lake_depth = layers.get("lake_depth")
    rf = layers.get("river_field")
    # Mountain Height slider's 1..3 band amplifies how big mountains read
    # (relief / shading / rock+snow tint) without adding geometry.
    ex = _render.mountain_exaggeration(getattr(params, "mountain_strength", 0.85))
    z = getattr(params, "relief", 12.0) * (1.0 + 0.6 * ex)
    hs = getattr(params, "hillshade_strength", 1.5) * (1.0 + 0.15 * ex)
    dens = getattr(params, "river_density", 0.5)
    # Underground layers use the flat cave palette + wall shading instead of the
    # hypsometric/forest surface render. The same image backs both "biome" and
    # "hillshade" so the existing two-image API keeps working.
    if getattr(params, "is_cave", False):
        try:
            png = _render.cave_png(
                layers, z_scale=z, hillshade_strength=hs,
                seed=getattr(params, "seed", 0),
                terrace_steps=getattr(params, "terrace_steps", 6),
                ssao_strength=getattr(params, "ssao_strength", 0.6))
            for name in ("biome.png", "hillshade.png"):
                with open(os.path.join(out_dir, name), "wb") as f:
                    f.write(png)
        except Exception as e:  # best-effort; arrays remain the source of truth
            logger.warning("cave image render failed: %s", e)
        return {
            "resolution": res,
            "sea_level": round(float(sea), 4),
            "map_width": map_width,
            "map_height": map_height,
            "images": {"biome": "biome.png", "hillshade": "hillshade.png"},
        }
    try:
        with open(os.path.join(out_dir, "biome.png"), "wb") as f:
            f.write(_render.biome_png(
                layers["biome"], height, sea, order, paths, lake_mask, lake_depth,
                z_scale=z, river_field=rf, density=dens, hillshade_strength=hs,
                biome_mode=getattr(params, "biome_mode", "realistic"),
                forests=getattr(params, "forests", True),
                forest_density=getattr(params, "forest_density", 0.5),
                biome_blend=getattr(params, "biome_blend", 0.7),
                temperature=layers.get("temperature"),
                moisture=layers.get("moisture"),
                seed=getattr(params, "seed", 0),
                mountain_exaggeration=ex,
                rock_line=getattr(params, "rock_line", 0.45),
                snow_line=getattr(params, "snow_line", 0.72),
                alpine_blend=getattr(params, "alpine_blend", 0.23)))
        with open(os.path.join(out_dir, "hillshade.png"), "wb") as f:
            f.write(_render.hillshade_png(
                height, sea, order, paths, lake_mask, lake_depth,
                z_scale=z, river_field=rf, density=dens, hillshade_strength=hs))
    except Exception as e:  # rendering is best-effort; arrays are the source of truth
        logger.warning("terrain image render failed: %s", e)

    return {
        "resolution": res,
        "sea_level": round(float(sea), 4),
        "map_width": map_width,
        "map_height": map_height,
        "images": {"biome": "biome.png", "hillshade": "hillshade.png"},
    }


def load_terrain(out_dir) -> dict:
    """Load the persisted arrays back into a layers-like dict (or {} if absent)."""
    import os
    path = os.path.join(out_dir, "layers.npz")
    if not os.path.exists(path):
        return {}
    with np.load(path) as data:
        out = {k: data[k] for k in data.files}
    if "sea_level" in out:
        out["sea_level"] = float(out["sea_level"])
    return out


def _build_cave_summary(layers: dict) -> dict:
    """Cave-specific digest: biome mix over the open floor, water presence and how
    much of the map is navigable cavern vs solid rock (no climate/coastline)."""
    biome = np.asarray(layers["biome"]).astype(int)
    open_mask = np.asarray(layers.get("open", ~np.asarray(layers["water"]))).astype(bool)
    table = _bm.biome_table("cave")
    open_count = int(open_mask.sum()) or 1
    ids, counts = np.unique(biome[open_mask], return_counts=True)
    pct = {}
    for i, c in sorted(zip(ids, counts), key=lambda t: -t[1]):
        if int(i) == _bm.ROCK_WALL:
            continue  # walls aren't a "biome" worth listing
        name = table.get(int(i), (f"biome_{int(i)}",))[0]
        pct[name] = round(100.0 * c / open_count, 1)
    has_rivers = bool(layers.get("river_mask") is not None
                      and np.asarray(layers["river_mask"]).any())
    has_lakes = bool(layers.get("lake_mask") is not None
                     and np.asarray(layers["lake_mask"]).any())
    return {
        "underground": True,
        "biome_percent": pct,
        "dominant_biomes": list(pct.keys())[:4],
        "has_coastline": False,
        "has_rivers": has_rivers,
        "has_lakes": has_lakes,
        "land_fraction": round(float(open_mask.mean()), 3),  # = open/cavern fraction
        "open_fraction": round(float(open_mask.mean()), 3),
        "dominant_climate": "subterranean",
    }


def build_terrain_summary(layers: dict, biome_mode: str = "realistic") -> dict:
    """A compact, JSON-friendly digest of a terrain raster for AI prompts."""
    if biome_mode == "cave" or layers.get("open") is not None:
        return _build_cave_summary(layers)
    biome = layers["biome"]
    water = layers["water"]
    height = layers["height"]
    temp = layers.get("temperature")
    table = _bm.biome_table(biome_mode)

    land = ~water.astype(bool)
    land_count = int(land.sum()) or 1
    ids, counts = np.unique(biome[land], return_counts=True)
    pct = {}
    for i, c in sorted(zip(ids, counts), key=lambda t: -t[1]):
        name = table.get(int(i), (f"biome_{int(i)}",))[0]
        pct[name] = round(100.0 * c / land_count, 1)

    has_rivers = bool(layers.get("river_mask") is not None
                      and np.asarray(layers["river_mask"]).any())
    has_lakes = bool(layers.get("lake_mask") is not None
                     and np.asarray(layers["lake_mask"]).any())
    # Coastline present if there is both land and water.
    has_coast = bool(land.any() and water.any())

    climate = "temperate"
    if temp is not None and land.any():
        t = float(np.mean(temp[land]))
        climate = ("frigid" if t < 0.22 else "cool" if t < 0.42
                   else "temperate" if t < 0.66 else "hot")

    land_h = height[land] if land.any() else height.ravel()
    return {
        "biome_percent": pct,
        "dominant_biomes": list(pct.keys())[:4],
        "has_coastline": has_coast,
        "has_rivers": has_rivers,
        "has_lakes": has_lakes,
        "land_fraction": round(float(land.mean()), 3),
        "dominant_climate": climate,
        "elevation": {
            "min": round(float(land_h.min()), 3),
            "max": round(float(land_h.max()), 3),
            "mean": round(float(land_h.mean()), 3),
        },
    }


def cell_at(x: float, y: float, res: int, map_width: float, map_height: float):
    """Map-space coordinate -> raster (row, col), clamped to bounds."""
    col = int(np.clip(round(x / max(1e-6, map_width) * (res - 1)), 0, res - 1))
    row = int(np.clip(round(y / max(1e-6, map_height) * (res - 1)), 0, res - 1))
    return row, col


def sample_terrain(layers: dict, x: float, y: float, map_width: float = 1000.0,
                   map_height: float = 1000.0, biome_mode: str = "realistic") -> dict:
    """Biome/elevation/feature lookup at a node's map coordinate."""
    if not layers or "biome" not in layers:
        return {}
    biome = layers["biome"]
    res = int(biome.shape[0])
    row, col = cell_at(x, y, res, map_width, map_height)
    table = _bm.biome_table(biome_mode)
    bid = int(biome[row, col])
    height = layers["height"]
    sea = float(layers.get("sea_level", 0.4))
    h = float(height[row, col])

    # Distance (in cells) to nearest river/lake/coast for "near water" flavor.
    near = []
    rm = layers.get("river_mask")
    lm = layers.get("lake_mask")
    if rm is not None and _within(rm, row, col, 3):
        near.append("river")
    if lm is not None and _within(lm, row, col, 3):
        near.append("lake")
    water = layers.get("water")
    if water is not None and _within(water, row, col, 2) and not bool(water[row, col]):
        near.append("coast")

    elev_band = ("underwater" if h < sea else "lowland" if h < sea + 0.2
                 else "highland" if h < 0.8 else "mountain")
    return {
        "biome": table.get(bid, (f"biome_{bid}",))[0],
        "elevation_band": elev_band,
        "near_water": near,
    }


def _within(mask, row, col, radius) -> bool:
    mask = np.asarray(mask)
    r0, r1 = max(0, row - radius), min(mask.shape[0], row + radius + 1)
    c0, c1 = max(0, col - radius), min(mask.shape[1], col + radius + 1)
    return bool(mask[r0:r1, c0:c1].any())
