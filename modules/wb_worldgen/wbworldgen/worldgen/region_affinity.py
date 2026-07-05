"""Terrain-matched region territories.

The ``terrain_regions`` step authors regions as free-text (``name``/``terrain``/
``climate``) with no coordinates. To keep regions from coming out fragmented we
carve the map into contiguous *territories* BEFORE deciding node membership:

  1. ``region_affinity_field`` turns a region's terrain/climate text into a
     ``[0,1]`` per-cell suitability raster (how well each land cell matches the
     region's described geography), reusing the suitability internals from
     ``terrain_placement.suitability_fields``.
  2. ``partition_regions`` seeds each region on its best-matching terrain and
     grows contiguous territories via a terrain-biased multi-source Dijkstra
     (priority flood) over land cells, returning a coarse ``region_of_cell``
     grid. Authored locations are then placed *within* their territory and
     filler nodes inherit the region of the cell they fall in — contiguous by
     construction.

Everything works in the abstract ``map_width x map_height`` space via
``terrain_store.cell_at``; the partition raster is computed at a coarse
resolution (``PARTITION_RES``) since territory boundaries don't need the full
1024^2 precision.
"""

import heapq
import re

import numpy as np

from wbworldgen.terrain import biomes as _bm


# Territory partition works on a downsampled grid — boundaries are smooth bands,
# not pixel-precise, so a coarse grid keeps the Dijkstra flood cheap.
PARTITION_RES = 256

# Keyword -> which affinity contributions to add. Each entry lists biome ids
# and/or named suitability-field channels ("high", "coast", "fresh", "rock",
# "snow", "low", "warm", "cold"). Matched as whole words against the combined
# lower-cased terrain + climate text.
_KEYWORD_RULES = {
    # forests / woodland
    r"forest|wood|woodland|timber": {"biomes": [_bm.FOREST, _bm.TEMPERATE_RAINFOREST]},
    r"taiga|boreal|coniferous|pine|conifer": {"biomes": [_bm.TAIGA]},
    r"jungle|rainforest|tropical": {"biomes": [_bm.JUNGLE]},
    # arid
    r"desert|arid|dune|sand|barren|waste": {"biomes": [_bm.DESERT, _bm.COLD_DESERT]},
    # mountains / high ground
    r"mountain|peak|highland|alpine|crag|summit|ridge|cliff": {
        "channels": ["high", "rock"]},
    r"volcan|obsidian|lava|ash": {"channels": ["rock", "high"]},
    # cold
    r"snow|glacier|ice|frozen|frost|arctic|polar|frigid": {
        "biomes": [_bm.ICE, _bm.TUNDRA], "channels": ["snow", "cold"]},
    r"tundra": {"biomes": [_bm.TUNDRA], "channels": ["cold"]},
    # coast / water
    r"coast|shore|sea|ocean|beach|port|harbor|harbour|maritime|tidal": {
        "channels": ["coast"]},
    r"river|riverine|delta|estuar": {"channels": ["fresh"]},
    r"lake|lakes|lacustrine": {"channels": ["fresh"]},
    r"swamp|marsh|bog|fen|wetland|mire|mangrove": {"channels": ["fresh", "low"]},
    # open land
    r"plain|grass|grassland|meadow|steppe|prairie|pasture|field": {
        "biomes": [_bm.GRASSLAND]},
    r"savanna|savannah|scrub|shrub|shrubland|heath": {
        "biomes": [_bm.SAVANNA, _bm.SHRUBLAND]},
    # climate-only nudges
    r"temperate|mild": {"channels": ["warm"]},
    r"warm|hot|sweltering|scorching|humid|sultry": {"channels": ["warm"]},
    r"cold|cool|chilly|bleak": {"channels": ["cold"]},
}


def _downsample(a, res: int):
    """Nearest-neighbour resample of a raster to ``res x res``."""
    a = np.asarray(a)
    src = a.shape[0]
    if src == res:
        return a
    idx = np.clip((np.arange(res) / (res - 1) * (src - 1)).round().astype(int), 0, src - 1)
    return a[np.ix_(idx, idx)]


def _norm01(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float64)
    lo, hi = float(a.min()), float(a.max())
    return (a - lo) / (hi - lo) if hi - lo > 1e-9 else np.zeros_like(a)


def region_affinity_field(region: dict, layers: dict, fields: dict,
                          res: int = PARTITION_RES) -> np.ndarray:
    """``[0,1]`` land-masked raster: how well each cell matches this region.

    Parses ``region['terrain'] + region['climate']`` keywords into biome/channel
    contributions. Returns a uniform positive land field when nothing matches, so
    the region still earns a territory via pure spatial spread.
    """
    biome = _downsample(np.asarray(layers["biome"]).astype(int), res)
    water = _downsample(np.asarray(layers["water"]).astype(bool), res)
    land = ~water

    ch = {
        "high": _downsample(fields["_high_ground"], res),
        "coast": _downsample(fields["_coastal"], res),
        "fresh": _downsample(fields["_fresh"], res),
        "rock": _downsample(fields["_rock_cover"], res),
        "snow": _downsample(fields["_snow_cover"], res),
    }
    temp = layers.get("temperature")
    if temp is not None:
        t = _downsample(np.asarray(temp, dtype=np.float64), res)
        ch["warm"] = _norm01(t)
        ch["cold"] = 1.0 - _norm01(t)
        height = _downsample(np.asarray(layers["height"], dtype=np.float64), res)
        sea = float(layers.get("sea_level", 0.4))
        ch["low"] = _norm01(np.clip(sea + 0.15 - height, 0.0, None))
    else:
        for k in ("warm", "cold", "low"):
            ch[k] = np.zeros((res, res), dtype=np.float64)

    text = f"{region.get('terrain', '')} {region.get('climate', '')}".lower()

    acc = np.zeros((res, res), dtype=np.float64)
    matched = False
    for pattern, rule in _KEYWORD_RULES.items():
        if not re.search(pattern, text):
            continue
        matched = True
        for bid in rule.get("biomes", []):
            acc += (biome == bid).astype(np.float64)
        for name in rule.get("channels", []):
            acc += ch[name]

    if not matched:
        # No recognizable geography: uniform preference over land so the region
        # is placed purely by spatial spread.
        return np.where(land, 0.5, 0.0)

    acc = _norm01(acc)
    return np.where(land, acc, 0.0)


def _pick_seeds(affinities: list, land: np.ndarray, res: int, rng) -> list:
    """One seed cell per region, best-affinity-first with a spacing constraint.

    Regions with the most distinctive terrain (highest peak affinity) claim their
    spot first. Every region is guaranteed a distinct seed cell.
    """
    order = sorted(range(len(affinities)),
                   key=lambda i: float(affinities[i].max()), reverse=True)
    min_sep = max(2.0, res * 0.12)
    seeds: dict[int, tuple] = {}
    taken: list[tuple] = []
    for ridx in order:
        aff = affinities[ridx]
        # Rank land cells by affinity (tiny jitter breaks ties deterministically).
        flat = aff.ravel() + rng.uniform(0, 1e-6, size=aff.size)
        flat = np.where(land.ravel(), flat, -1.0)
        for cell in np.argsort(flat)[::-1]:
            if flat[cell] < 0:
                break  # only water left
            r, c = divmod(int(cell), res)
            if all((r - rr) ** 2 + (c - cc) ** 2 >= min_sep ** 2 for rr, cc in taken):
                seeds[ridx] = (r, c)
                taken.append((r, c))
                break
        if ridx not in seeds:
            # Spacing left nowhere — accept the best remaining land cell.
            best = int(np.argmax(flat))
            seeds[ridx] = divmod(best, res)
            taken.append(seeds[ridx])
    return [seeds[i] for i in range(len(affinities))]


def partition_regions(region_data: list, layers: dict, fields: dict,
                      map_width: float, map_height: float, rng,
                      res: int = PARTITION_RES) -> np.ndarray:
    """Carve the map into contiguous region territories.

    Returns an ``res x res`` int grid (``region_of_cell``): the region index that
    owns each land cell, or ``-1`` on water. Territories are grown by a
    terrain-biased multi-source Dijkstra so each bulges toward the terrain that
    matches its description while staying a single connected blob.
    """
    n_regions = len(region_data)
    water = _downsample(np.asarray(layers["water"]).astype(bool), res)
    land = ~water
    out = np.full((res, res), -1, dtype=np.int32)
    if n_regions == 0 or not land.any():
        return out

    affinities = [region_affinity_field(r, layers, fields, res) for r in region_data]
    seeds = _pick_seeds(affinities, land, res, rng)

    # Dijkstra where the step cost into a land cell is cheaper the better that
    # cell matches the advancing region — cost = base * (1 + w*(1 - affinity)).
    W = 6.0
    cost = np.full((res, res), np.inf, dtype=np.float64)
    heap: list[tuple] = []
    for ridx, (sr, sc) in enumerate(seeds):
        if cost[sr, sc] > 0:
            cost[sr, sc] = 0.0
            out[sr, sc] = ridx
            heapq.heappush(heap, (0.0, sr, sc, ridx))

    while heap:
        d, r, c, ridx = heapq.heappop(heap)
        if d > cost[r, c]:
            continue
        aff = affinities[ridx]
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            if nr < 0 or nr >= res or nc < 0 or nc >= res or not land[nr, nc]:
                continue
            step = 1.0 + W * (1.0 - float(aff[nr, nc]))
            nd = d + step
            if nd < cost[nr, nc]:
                cost[nr, nc] = nd
                out[nr, nc] = ridx
                heapq.heappush(heap, (nd, nr, nc, ridx))

    return out


def upsample_region_grid(region_of_cell: np.ndarray, res: int) -> np.ndarray:
    """Nearest-neighbour upsample the coarse territory grid to ``res x res``.

    Used to mask full-resolution suitability rasters to a region's territory.
    """
    src = region_of_cell.shape[0]
    if src == res:
        return region_of_cell
    idx = np.clip((np.arange(res) / (res - 1) * (src - 1)).round().astype(int), 0, src - 1)
    return region_of_cell[np.ix_(idx, idx)]


def region_at(region_of_cell: np.ndarray, x: float, y: float,
              map_width: float, map_height: float) -> int:
    """Region index owning the territory cell at map-space ``(x, y)``.

    Falls back to the nearest owned cell when the exact cell is unassigned
    (e.g. a node placed on a thin water pixel).
    """
    res = region_of_cell.shape[0]
    col = int(np.clip(round(x / max(1e-6, map_width) * (res - 1)), 0, res - 1))
    row = int(np.clip(round(y / max(1e-6, map_height) * (res - 1)), 0, res - 1))
    if region_of_cell[row, col] >= 0:
        return int(region_of_cell[row, col])
    owned = np.argwhere(region_of_cell >= 0)
    if owned.size == 0:
        return -1
    d2 = (owned[:, 0] - row) ** 2 + (owned[:, 1] - col) ** 2
    nr, nc = owned[int(np.argmin(d2))]
    return int(region_of_cell[nr, nc])
