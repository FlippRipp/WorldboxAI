"""Terrain-aware placement: where settlements, ports, strongholds and tagged
landmarks belong on a generated terrain raster.

Coordinates are produced in the abstract ``map_width × map_height`` space the
node-graph map already uses; internally we work in raster cells and convert with
``terrain_store.cell_at``.

``ENVIRONMENT_TAGS`` is the canonical set of landmark environment tags shared by
the ``natural_landmarks`` authoring prompt (so the AI emits valid tags) and the
placement logic (which turns a tag into a cell-suitability mask). Keep the two in
sync by sourcing both from here.
"""

import numpy as np
from scipy.ndimage import distance_transform_edt

from wbworldgen.terrain import biomes as _bm
from wbworldgen.worldgen import terrain_store as _ts


# Canonical landmark environment tags -> short authoring description. The
# placement criteria for each live in ``_tag_mask`` below.
ENVIRONMENT_TAGS = {
    "snowy_peak": "a snow/ice-capped high mountain",
    "rocky_summit": "bare rocky highland or crag",
    "deep_forest": "dense inland woodland",
    "jungle_depths": "tropical rainforest interior",
    "coastal_cliff": "steep ground at the sea's edge",
    "island": "isolated land surrounded by water",
    "river_bend": "beside a flowing river",
    "lake_shore": "on the edge of an inland lake",
    "marsh": "low, wet lowland near water",
    "desert_waste": "arid desert expanse",
    "grassy_plain": "open temperate grassland",
    "volcanic": "barren high rock (treated as volcanic)",
}


def tag_descriptions() -> str:
    """Bulleted tag list for prompt guidance."""
    return "\n".join(f"- {tag}: {desc}" for tag, desc in ENVIRONMENT_TAGS.items())


# --- suitability fields ----------------------------------------------------

def _norm(a: np.ndarray) -> np.ndarray:
    lo, hi = float(a.min()), float(a.max())
    return (a - lo) / (hi - lo) if hi - lo > 1e-9 else np.zeros_like(a)


def _fresh_water_proximity(layers: dict) -> np.ndarray:
    """[0,1] field, high near rivers/lakes, decaying inland."""
    res = layers["height"].shape[0]
    src = np.zeros((res, res), dtype=bool)
    for key in ("river_mask", "lake_mask"):
        m = layers.get(key)
        if m is not None:
            src |= np.asarray(m).astype(bool)
    if not src.any():
        return np.zeros((res, res), dtype=np.float64)
    dist = distance_transform_edt(~src)
    return np.exp(-(dist / max(1.0, res * 0.05)) ** 2)


def suitability_fields(layers: dict) -> dict:
    """Per-purpose [0,1] suitability rasters over land cells."""
    height = np.asarray(layers["height"], dtype=np.float64)
    slope = np.asarray(layers["slope"], dtype=np.float64)
    water = np.asarray(layers["water"]).astype(bool)
    biome = np.asarray(layers["biome"]).astype(int)
    sea = float(layers.get("sea_level", 0.4))
    land = ~water

    flat = 1.0 - _norm(slope)            # flatter = better to build
    fresh = _fresh_water_proximity(layers)
    coast_dist = distance_transform_edt(~water)  # cells from any water
    coastal = np.exp(-(coast_dist / 3.0) ** 2)   # ~within a few cells of shore

    # Height-based bare-rock and snow cover (replaces the old ROCK/SNOW biome
    # classes); shared with the renderer and roads so all three agree on where
    # the mountains are.
    rock_cover, snow_cover = _bm.alpine_cover(height, sea,
                                              layers.get("temperature"))
    rock_cover = np.where(land, rock_cover, 0.0)
    snow_cover = np.where(land, snow_cover, 0.0)

    # Temperate/habitable biomes earn a settlement bonus; harsh ground penalised.
    # Harsh ground is high rock/snow cover plus the still-classified ice/desert.
    habitable = np.isin(biome, [_bm.GRASSLAND, _bm.FOREST, _bm.SAVANNA,
                                _bm.TEMPERATE_RAINFOREST, _bm.SHRUBLAND]).astype(float)
    harsh = np.clip(rock_cover + snow_cover
                    + np.isin(biome, [_bm.ICE, _bm.DESERT]).astype(float),
                    0.0, 1.0)

    # land-elevation percentile for "high ground"
    land_h = height[land]
    hi_ref = float(np.quantile(land_h, 0.7)) if land.any() else sea + 0.3
    high_ground = _norm(np.clip(height - hi_ref, 0.0, None))

    city = (0.45 * flat + 0.3 * fresh + 0.15 * habitable + 0.1 * coastal
            - 0.4 * harsh)
    port = (0.5 * coastal + 0.3 * flat + 0.2 * fresh - 0.5 * harsh)
    stronghold = (0.55 * high_ground + 0.25 * (1.0 - flat) + 0.2 * habitable
                  - 0.3 * harsh)

    out = {}
    for name, fld in (("city", city), ("port", port), ("stronghold", stronghold)):
        fld = np.where(land, fld, -1e9)  # forbid water cells
        out[name] = fld
    out["_fresh"] = fresh
    out["_coastal"] = coastal
    out["_high_ground"] = high_ground
    out["_rock_cover"] = rock_cover
    out["_snow_cover"] = snow_cover
    return out


# --- weighted sampling ------------------------------------------------------

def sample_points(field: np.ndarray, n: int, res: int, map_width: float,
                  map_height: float, rng, min_sep_cells: float,
                  taken: list = None) -> list:
    """Pick ``n`` well-spaced high-suitability cells; return map-space (x,y).

    Greedy: rank candidate cells by score (with jitter), accept while enforcing a
    minimum separation. ``taken`` seeds already-occupied cell positions."""
    flat = field.ravel()
    # Consider only positively-scored cells; fall back to top cells otherwise.
    valid = np.where(flat > 0)[0]
    if valid.size < n:
        valid = np.argsort(flat)[::-1][:max(n * 50, 200)]
    scores = flat[valid] + rng.uniform(0, 1e-3, size=valid.size)
    order = valid[np.argsort(scores)[::-1]]

    chosen_cells = list(taken or [])
    result = []
    for idx in order:
        if len(result) >= n:
            break
        r, c = divmod(int(idx), res)
        if all((r - rr) ** 2 + (c - cc) ** 2 >= min_sep_cells ** 2
               for rr, cc in chosen_cells):
            chosen_cells.append((r, c))
            x = c / (res - 1) * map_width
            y = r / (res - 1) * map_height
            result.append((x, y, r, c))
    return result


# --- landmark tag -> cell ---------------------------------------------------

def _tag_mask(tag: str, layers: dict, fields: dict) -> np.ndarray:
    """A [0,1] suitability mask for an environment tag (0 where unsuitable)."""
    height = np.asarray(layers["height"], dtype=np.float64)
    biome = np.asarray(layers["biome"]).astype(int)
    water = np.asarray(layers["water"]).astype(bool)
    land = ~water
    high = fields["_high_ground"]
    coastal = fields["_coastal"]
    fresh = fields["_fresh"]

    def biome_in(ids):
        return np.isin(biome, ids).astype(float)

    if tag == "snowy_peak":
        # Snow-capped high ground (height-based cover), plus frozen sea margin.
        m = (fields["_snow_cover"] + biome_in([_bm.ICE])) * (0.5 + high)
    elif tag in ("rocky_summit", "volcanic"):
        m = fields["_rock_cover"] * (0.5 + high)
    elif tag == "deep_forest":
        m = biome_in([_bm.FOREST, _bm.TAIGA, _bm.TEMPERATE_RAINFOREST]) * (1.0 - coastal)
    elif tag == "jungle_depths":
        m = biome_in([_bm.JUNGLE]) * (1.0 - coastal)
    elif tag == "coastal_cliff":
        m = coastal * _norm(np.asarray(layers["slope"]))
    elif tag == "island":
        # small land far from the main mass: high coast proximity on all sides
        m = coastal * land
    elif tag == "river_bend":
        m = fresh * (np.asarray(layers.get("river_mask", np.zeros_like(height))).astype(float) * 3 + fresh)
    elif tag == "lake_shore":
        lm = layers.get("lake_mask")
        if lm is None:
            m = fresh
        else:
            dist = distance_transform_edt(~np.asarray(lm).astype(bool))
            m = np.exp(-(dist / 3.0) ** 2)
    elif tag == "marsh":
        m = fresh * (height < float(layers.get("sea_level", 0.4)) + 0.12)
    elif tag == "desert_waste":
        m = biome_in([_bm.DESERT])
    elif tag == "grassy_plain":
        m = biome_in([_bm.GRASSLAND, _bm.SAVANNA]) * (1.0 - _norm(np.asarray(layers["slope"])))
    else:
        m = np.where(land, 0.1, 0.0)  # unknown tag: anywhere on land

    return np.where(land, m, 0.0)


def place_landmark(tag: str, layers: dict, fields: dict, res: int,
                   map_width: float, map_height: float, rng, taken: list,
                   region_mask: np.ndarray = None) -> tuple:
    """Best free cell matching ``tag``; returns (x, y, r, c) or None.

    ``region_mask`` (a bool ``res x res`` array) restricts placement to a
    region's territory so an authored landmark stays inside its region.
    """
    mask = _tag_mask(tag or "", layers, fields)
    if region_mask is not None:
        # Keep only in-territory cells; a tiny floor lets sampling still find a
        # spot when no cell matches the tag well inside the territory.
        mask = np.where(region_mask, np.maximum(mask, 1e-3), 0.0)
    pts = sample_points(mask, 1, res, map_width, map_height, rng,
                        min_sep_cells=max(2.0, res * 0.03), taken=taken)
    return pts[0] if pts else None
