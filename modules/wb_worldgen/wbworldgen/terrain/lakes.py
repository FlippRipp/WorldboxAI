"""Lake generation + selective depression breaching / carving.

Priority-Flood tells us exactly which cells sit inside a depression (it raises
them to a spill level). Instead of silently filling them all, we classify each
depression and apply the research's hybrid policy:

  * **Lake**  — large/deep basins are kept as flat water at the spill level
                (a ``lake_mask``); rivers flow in and the overflow leaves at the
                spill point.
  * **Breach**— shallower basins are *carved*: a notch is cut through the rim
                from the basin floor to a lower outside cell, so the basin
                drains as a natural valley (interior slope preserved) instead of
                ponding.
  * **Fill**  — everything else (tiny noise pits) is left filled as terrain.

Returns the terrain to render, a routing DEM guaranteed to drain (for the
hydrology pass), and the lake mask.
"""

import numpy as np
from scipy import ndimage

from wbworldgen.terrain.hydrology import fill_depressions


def _carve_line(terrain, y0, x0, y1, x1, h_start, h_end):
    """Carve a descending trench between two points (Bresenham), lowering only
    cells that are higher than the ramp so we cut a channel, not a wall."""
    pts = []
    dx = abs(x1 - x0); dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    x, y = x0, y0
    while True:
        pts.append((y, x))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy; x += sx
        if e2 < dx:
            err += dx; y += sy
    n = len(pts)
    for k, (yy, xx) in enumerate(pts):
        ramp = h_start + (h_end - h_start) * (k / max(1, n - 1))
        if terrain[yy, xx] > ramp:
            terrain[yy, xx] = ramp


def _shore_noise(res, seed, amp):
    """Coherent [0, amp] noise field used to roughen lake shorelines so they
    don't read as perfectly smooth iso-contour blobs."""
    rng = np.random.default_rng(seed)
    low = max(4, res // 16)
    coarse = rng.random((low, low))
    field = ndimage.zoom(coarse, res / low, order=1)[:res, :res]
    field = (field - field.min()) / max(1e-9, float(np.ptp(field)))
    return field * amp


def resolve_depressions(height, sea_level, lake_min_area=50, lake_min_depth=0.012,
                        breach=True, breach_max_depth=0.05, seed=0):
    """Classify depressions into lakes / breached valleys / filled pits.

    Returns dict: ``terrain`` (render/biome height), ``route`` (drain-guaranteed
    DEM for flow routing), ``lake_mask``, ``lake_depth`` (water depth at each
    lake cell, for depth-graded colouring), and stats.
    """
    res = height.shape[0]
    # True flat fill (no epsilon) reveals depressions and their spill levels.
    filled = fill_depressions(height, sea_level, eps=0.0)
    depth = filled - height
    # Roughen the shoreline: nibble away shallow rim cells by a coherent noise
    # threshold so the boundary meanders instead of tracing a clean contour.
    # Deep interior cells (depth >> noise) are untouched, so only the edge moves.
    shore = _shore_noise(res, seed, amp=max(1e-4, 0.4 * lake_min_depth))
    submerged = (depth > shore) & (depth > 1e-4) & (height > sea_level)

    lbl, ncomp = ndimage.label(submerged, structure=np.ones((3, 3)))
    lake_mask = np.zeros((res, res), dtype=bool)
    terrain = height.copy()
    n_lakes = n_breached = n_filled = 0

    if ncomp:
        objs = ndimage.find_objects(lbl)
        for cid in range(1, ncomp + 1):
            sl = objs[cid - 1]
            if sl is None:
                continue
            comp = lbl[sl] == cid
            area = int(comp.sum())
            sub_depth = depth[sl][comp]
            maxd = float(sub_depth.max())
            spill = float(filled[sl][comp][0])  # constant within a depression

            if area >= lake_min_area and maxd >= lake_min_depth:
                gmask = np.zeros((res, res), bool)
                gmask[sl] = comp
                lake_mask |= gmask
                n_lakes += 1
            elif breach and maxd <= breach_max_depth:
                _breach(terrain, height, filled, lbl, cid, sl, spill, res)
                n_breached += 1
            else:
                n_filled += 1  # leave filled-as-terrain below

    # Routing DEM: lakes flat at spill, then guarantee drainage everywhere.
    route = terrain.copy()
    route[lake_mask] = filled[lake_mask]
    route = fill_depressions(route, sea_level, eps=1e-6)

    # Water depth at each lake cell (spill level minus the original floor), for
    # depth-graded rendering. Zero outside lakes.
    lake_depth = np.where(lake_mask, np.maximum(0.0, filled - height), 0.0)

    return {
        "terrain": terrain, "route": route, "lake_mask": lake_mask,
        "lake_depth": lake_depth,
        "n_lakes": n_lakes, "n_breached": n_breached, "n_filled": n_filled,
    }


def _breach(terrain, height, filled, lbl, cid, sl, spill, res):
    """Carve a drainage notch from a basin's pit to its lowest outside outlet."""
    ys0, xs0 = sl[0].start, sl[1].start
    comp = lbl[sl] == cid
    ly, lx = np.nonzero(comp)
    gy = ly + ys0
    gx = lx + xs0

    # Pit = lowest original cell in the basin.
    pit = int(np.argmin(height[gy, gx]))
    py, px = int(gy[pit]), int(gx[pit])

    # Outlet = boundary cell whose outside neighbour is lowest (the spill saddle).
    best = None
    for yy, xx in zip(gy, gx):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                ny, nx = yy + dy, xx + dx
                if 0 <= ny < res and 0 <= nx < res and lbl[ny, nx] != cid:
                    ho = height[ny, nx]
                    if ho < spill and (best is None or ho < best[0]):
                        best = (ho, ny, nx)
    if best is None:
        return
    ho, oy, ox = best
    # Carve pit -> outlet descending from the pit elevation to just below the
    # outside outlet, so the basin drains through the notch.
    _carve_line(terrain, py, px, oy, ox, float(height[py, px]), float(ho) - 1e-3)
