"""Underground cave-terrain generator.

A parallel to :func:`wbworldgen.terrain.pipeline.generate_terrain` that emits
the **same ``layers`` dict contract** (so persistence, terrain-aware placement,
summaries and rendering all work unchanged) but for 2D top-down *underground*
maps: a mix of tunnels and deliberately placed caverns, subterranean water
(pools/lakes + flowing rivers) and underground biomes.

Pipeline (the "remake" — intentional rooms + fault-guided winding tunnels):

    Poisson-disk POI placement (major caverns spread without clustering)
      -> Delaunay web -> MST + retained loop edges (connected, not linear)
      -> cellular-automata carved rooms around each POI (organic, not blobs)
      -> fault-guided, width-varied least-cost tunnels along the graph edges
      -> domain warp + CA smoothing -> single-component connectivity
      -> light wall erosion (round seams / widen pinch points)
      -> floor heightmap -> water table (pools) + D8 flow rivers
      -> underground biome classification -> assembled layers

What's reused from the surface stack: the value/Perlin noise + FBM and the
ridged multifractal "fault" field (:mod:`heightmap`), the 8-connected least-cost
Dijkstra used for surface roads (:mod:`wbworldgen.worldgen.roads`), scipy
Delaunay/labeling/distance transforms, and the ``layers`` schema + cave biome
palette. Water is built directly here rather than via the surface
``lakes``/``rivers`` modules: those route to a global sea over an open DEM,
whereas a cave floor is bounded by solid rock walls, so a wall-bounded DEM would
simply flood every enclosed chamber. A wall-aware water table + D8 accumulation
over the open cells is both simpler and correct underground.
"""

from dataclasses import dataclass, asdict
import time
from typing import Optional

import numpy as np
from scipy import ndimage
from scipy.spatial import Delaunay

from wbworldgen.terrain import heightmap as _hm
from wbworldgen.terrain import cave_biomes as _cb
from wbworldgen.terrain.pipeline import TerrainResult


@dataclass
class CaveParams:
    seed: int = -1                   # < 0 => fresh random seed each run
    resolution: int = 384
    cavern_density: float = 0.22     # 0..1 fraction of the map that opens into caverns
    cavern_size: float = 0.5         # 0..1 room scale (bigger rooms, wider spacing)
    tunnel_width: float = 0.5        # 0..1 corridor thickness
    tunnel_windiness: float = 0.5    # 0..1 how much tunnels wander through soft rock
    extra_tunnels: float = 0.4       # 0..1 fraction of Delaunay loop edges kept beyond the MST
    ca_iterations: int = 4           # cellular-automata smoothing passes
    fault_strength: float = 0.6      # 0..1 how strongly tunnels snap to tectonic fault lines
    erosion_amount: float = 0.4      # 0..1 wall erosion that rounds seams / widens pinch points
    water_level: float = 0.28        # 0..1 floor height below which pools form
    lava_amount: float = 0.5         # 0..1 abundance of lava tubes
    crystal_amount: float = 0.5      # 0..1 abundance of crystal caverns
    ice_amount: float = 0.3          # 0..1 abundance of ice caves
    biome_blend: float = 0.6         # 0..1 ecotone width for biome borders
    biome_mode: str = "cave"         # palette selector (terrain_store / render)
    is_cave: bool = True             # marks this as a cave layer for save/render
    # Rendering knobs read by terrain_store/render (kept name-compatible).
    relief: float = 14.0
    hillshade_strength: float = 1.6
    river_density: float = 0.5
    terrace_steps: int = 6           # geological strata count for the cave render
    ssao_strength: float = 0.6       # 0..1 ambient-occlusion depth in the cave render

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "CaveParams":
        d = d or {}
        valid = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def to_dict(self) -> dict:
        return asdict(self)


# --- macro structure: POIs + connectivity graph ----------------------------

def _poisson_disk(res: int, min_dist: float, seed: int, k: int = 30) -> list:
    """Bridson Poisson-disk sampling -> list of ``(y, x)`` POI centres.

    Distributes major cavern nodes so they're spaced at least ``min_dist`` apart
    without clustering or grid-aligning — the natural-looking macro layout the
    rooms and tunnels are built around.
    """
    min_dist = max(2.0, float(min_dist))
    rng = np.random.default_rng(seed + 777)
    cell = min_dist / np.sqrt(2.0)
    gw = int(np.ceil(res / cell))
    grid = np.full((gw, gw), -1, dtype=np.int64)
    samples: list = []
    active: list = []

    def _grid_xy(p):
        return int(p[1] / cell), int(p[0] / cell)  # (gx, gy)

    p0 = (float(rng.uniform(0, res)), float(rng.uniform(0, res)))
    samples.append(p0)
    active.append(0)
    gx0, gy0 = _grid_xy(p0)
    grid[gy0, gx0] = 0

    while active:
        ai = int(rng.integers(0, len(active)))
        idx = active[ai]
        py, px = samples[idx]
        placed = False
        for _ in range(k):
            ang = float(rng.uniform(0, 2.0 * np.pi))
            rad = float(rng.uniform(min_dist, 2.0 * min_dist))
            ny = py + rad * np.sin(ang)
            nx = px + rad * np.cos(ang)
            if not (0 <= ny < res and 0 <= nx < res):
                continue
            gx, gy = int(nx / cell), int(ny / cell)
            ok = True
            for yy in range(max(0, gy - 2), min(gw, gy + 3)):
                for xx in range(max(0, gx - 2), min(gw, gx + 3)):
                    j = grid[yy, xx]
                    if j >= 0:
                        sy, sx = samples[j]
                        if (sy - ny) ** 2 + (sx - nx) ** 2 < min_dist * min_dist:
                            ok = False
                            break
                if not ok:
                    break
            if ok:
                samples.append((ny, nx))
                grid[gy, gx] = len(samples) - 1
                active.append(len(samples) - 1)
                placed = True
                break
        if not placed:
            active.pop(ai)

    return [(int(round(y)), int(round(x))) for (y, x) in samples]


def _graph_edges(nodes: list, extra: float) -> list:
    """Delaunay web -> MST (guaranteed connectivity) + a fraction of the shortest
    leftover Delaunay edges as loops, so the network isn't a perfect tree."""
    n = len(nodes)
    if n < 2:
        return []
    if n < 3:
        return [(0, 1)]
    pts = np.array([(x, y) for (y, x) in nodes], dtype=np.float64)
    try:
        tri = Delaunay(pts)
    except Exception:
        return _mst_edges(nodes)
    web = set()
    for simplex in tri.simplices:
        for a, b in ((simplex[0], simplex[1]),
                     (simplex[1], simplex[2]),
                     (simplex[2], simplex[0])):
            web.add((int(min(a, b)), int(max(a, b))))

    mst = set((int(min(a, b)), int(max(a, b))) for a, b in _mst_edges(nodes))
    leftover = [e for e in web if e not in mst]
    # Shortest leftover edges make the most natural-looking loops.
    leftover.sort(key=lambda e: (pts[e[0]][0] - pts[e[1]][0]) ** 2
                  + (pts[e[0]][1] - pts[e[1]][1]) ** 2)
    n_extra = int(round(float(np.clip(extra, 0.0, 1.0)) * len(leftover)))
    return list(mst) + leftover[:n_extra]


def _mst_edges(nodes: list) -> list:
    """Euclidean minimum spanning tree edge list (Prim's) over (y,x) nodes."""
    n = len(nodes)
    if n < 2:
        return []
    pts = np.array(nodes, dtype=np.float64)
    in_tree = np.zeros(n, dtype=bool)
    in_tree[0] = True
    best = np.sum((pts - pts[0]) ** 2, axis=1)
    parent = np.zeros(n, dtype=int)
    edges = []
    for _ in range(n - 1):
        best_masked = np.where(in_tree, np.inf, best)
        j = int(np.argmin(best_masked))
        edges.append((int(parent[j]), j))
        in_tree[j] = True
        d = np.sum((pts - pts[j]) ** 2, axis=1)
        upd = d < best
        best = np.where(upd, d, best)
        parent = np.where(upd, j, parent)
    return edges


# --- carving caverns (cellular automata) -----------------------------------

def _room_shape(rng: np.random.Generator) -> tuple:
    """Draw a single cavern archetype -> ``(scale, aspect, lump)``.

    One of: a long wide fracture/crack, a major lumpy hall/lake chamber, or a
    smaller irregular grotto. Used both for standalone rooms and as the lobes of
    a composite cavern.
    """
    roll = rng.random()
    if roll < 0.18:            # long, wide fracture/crack
        return (float(rng.uniform(0.9, 1.5)), float(rng.uniform(2.5, 6.0)),
                float(rng.uniform(0.2, 0.5)))
    if roll < 0.42:            # major lumpy hall / lake chamber
        return (float(rng.uniform(1.6, 2.5)), float(rng.uniform(1.0, 2.2)),
                float(rng.uniform(0.5, 0.9)))
    return (float(rng.uniform(0.6, 1.2)), float(rng.uniform(1.0, 2.4)),
            float(rng.uniform(0.4, 0.9)))      # smaller irregular grotto


def _carve_room(caverns: np.ndarray, cy: int, cx: int, half: int,
                seed: int, iters: int = 4, aspect: float = 1.0,
                theta: float = 0.0, lump: float = 0.6) -> None:
    """Carve one organic cavern around a POI with cellular automata.

    The chamber is a *rotated, anisotropic, noise-lumped* distance field rather
    than a circle, so shape varies widely:

      * ``aspect`` (>1) elongates the room into the ``theta`` direction — high
        values give long wide cracks/fractures; ~1 gives a compact room;
      * ``lump`` warps the rim with low-frequency noise so the outline is lobed
        and irregular instead of a smooth ellipse.

    Floor probability falls off from a solid open core to a fuzzy ragged rim;
    the ``>=5 of 9`` majority rule then smooths it. Writes (OR-merges) the room.
    """
    res = caverns.shape[0]
    ax = max(1.0, half * np.sqrt(max(1.0, aspect)))   # long semi-axis
    ay = max(1.0, half / np.sqrt(max(1.0, aspect)))   # short semi-axis
    ext = int(np.ceil(max(ax, ay) * 1.2)) + 2
    y0, y1 = max(0, cy - ext), min(res, cy + ext + 1)
    x0, x1 = max(0, cx - ext), min(res, cx + ext + 1)
    if y1 - y0 < 3 or x1 - x0 < 3:
        caverns[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 2] = True
        return
    rng = np.random.default_rng(seed)
    yy, xx = np.ogrid[y0 - cy:y1 - cy, x0 - cx:x1 - cx]
    yy = np.broadcast_to(yy, (y1 - y0, x1 - x0)).astype(np.float64)
    xx = np.broadcast_to(xx, (y1 - y0, x1 - x0)).astype(np.float64)
    ca, sa = np.cos(theta), np.sin(theta)
    rx = xx * ca + yy * sa            # coordinate along the long axis
    ry = -xx * sa + yy * ca           # coordinate along the short axis
    nd2 = (rx / ax) ** 2 + (ry / ay) ** 2   # 1.0 at the nominal elliptical rim

    # Lumpiness: bend the rim in/out with a low-freq noise patch (no effect at the
    # core, so the chamber body stays solid while its outline goes ragged/lobed).
    if lump > 1e-3:
        h, w = nd2.shape
        nz = _hm.fbm(max(8, h, w), seed + 5, octaves=3, base_freq=3)[:h, :w] - 0.5
        nd2 = np.clip(nd2 * (1.0 + 1.3 * float(np.clip(lump, 0.0, 1.0)) * nz), 0.0, None)

    p = np.clip(0.95 - 0.85 * nd2, 0.0, 0.97)
    sub = rng.random(nd2.shape) < p

    k = np.ones((3, 3), dtype=np.int32)
    for _ in range(max(1, iters)):
        nb = ndimage.convolve(sub.astype(np.int32), k, mode="constant", cval=0)
        sub = nb >= 5
    sub = sub | (nd2 < 0.22)          # guarantee an open core after smoothing
    sub = sub & (nd2 < 1.1)           # trim stray outliers but keep lobes
    caverns[y0:y1, x0:x1] |= sub


# --- tunnels (fault-guided, width-varied least-cost routes) ----------------

def _disk(radius: int) -> np.ndarray:
    r = max(1, int(radius))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def _fault_field(res: int, seed: int) -> np.ndarray:
    """[0,1] ridged-multifractal field: sharp intersecting tectonic fault lines
    (high = a fault/weak-rock crease tunnels prefer to follow)."""
    return _hm.ridged_multifractal(res, seed + 6101, octaves=6, base_freq=4)


def _route_tunnels(res: int, nodes: list, edges: list, fault: np.ndarray,
                   seed: int, windiness: float, width: float,
                   fault_strength: float) -> np.ndarray:
    """Carve corridors along the graph edges with fault-guided least-cost routes.

    Routes run on a downsampled cost grid (like surface roads) so Dijkstra is
    cheap. Cost favours soft rock and snaps toward fault lines; the resulting
    thin path is upscaled and given a **spatially varying width** (via a low-freq
    noise field) so corridors taper and bulge instead of reading as uniform
    worms.
    """
    from wbworldgen.worldgen import roads as _roads

    if not nodes or not edges:
        return np.zeros((res, res), dtype=bool)

    route_res = min(res, 160)
    step = max(1, res // route_res)
    rr = res // step

    hardness = _hm.fbm(rr, seed + 41, octaves=4, base_freq=8)
    cost = 1.0 + (3.0 + 9.0 * float(np.clip(windiness, 0.0, 1.0))) * hardness
    # Snap toward fault lines: high fault => low cost => tunnels follow the crease.
    weak = fault[::step, ::step][:rr, :rr]
    cost = cost * np.clip(1.0 - 0.75 * float(np.clip(fault_strength, 0.0, 1.0)) * weak,
                          0.12, 1.0)

    rnodes = []
    for (cy, cx) in nodes:
        ry = int(np.clip(round(cy / step), 0, rr - 1))
        rx = int(np.clip(round(cx / step), 0, rr - 1))
        rnodes.append((ry, rx))

    line = np.zeros((rr, rr), dtype=bool)
    for a, b in edges:
        if a >= len(rnodes) or b >= len(rnodes) or a == b:
            continue
        _dist, prev = _roads._dijkstra(cost, rnodes[a])
        path = _roads._reconstruct(prev, rnodes[a], rnodes[b], rr)
        for (yy, xx) in path:
            line[yy, xx] = True

    # Upscale the thin centreline to full res, then grow it to a varying radius
    # with a distance transform (kills the nearest-neighbour staircase too).
    full_line = np.repeat(np.repeat(line, step, axis=0), step, axis=1)
    if full_line.shape != (res, res):
        pad = np.zeros((res, res), dtype=bool)
        pad[:full_line.shape[0], :full_line.shape[1]] = full_line[:res, :res]
        full_line = pad
    if not full_line.any():
        return np.zeros((res, res), dtype=bool)

    dist = ndimage.distance_transform_edt(~full_line)
    base = 2.0 + 5.0 * float(np.clip(width, 0.0, 1.0))    # mean radius in px
    wnoise = _hm.fbm(res, seed + 71, octaves=4, base_freq=10)
    width_field = base * (0.45 + 1.1 * wnoise)            # ~0.45x .. 1.55x: taper + bulge
    return dist <= width_field


# --- smoothing / connectivity / erosion ------------------------------------

def _cellular_smooth(open_mask: np.ndarray, iterations: int) -> np.ndarray:
    """Conway-style majority smoothing: a cell is open if >=5 of its 3x3
    neighbourhood (incl. itself) is open. Rounds off walls and removes speckle."""
    k = np.ones((3, 3), dtype=np.int32)
    m = open_mask.copy()
    for _ in range(max(0, iterations)):
        nb = ndimage.convolve(m.astype(np.int32), k, mode="constant", cval=0)
        m = nb >= 5
    return m


def _warp_mask(mask: np.ndarray, seed: int, amp_frac: float = 0.05) -> np.ndarray:
    """Domain-warp a boolean field: bend sample coordinates by two low-freq noise
    fields so caverns and tunnels look stretched/sheared by tectonic pressure."""
    res = mask.shape[0]
    wx = _hm.fbm(res, seed + 301, octaves=4) - 0.5
    wy = _hm.fbm(res, seed + 302, octaves=4) - 0.5
    coords = np.arange(res)
    gx, gy = np.meshgrid(coords, coords)
    amp = amp_frac * res
    sx = np.clip((gx + wx * amp).astype(int), 0, res - 1)
    sy = np.clip((gy + wy * amp).astype(int), 0, res - 1)
    return mask[sy, sx]


def _erode_walls(open_mask: np.ndarray, amount: float, seed: int,
                 iters: int = 2) -> np.ndarray:
    """Light erosion that rounds seams and widens pinch points.

    Rock cells that protrude into open space (>=5 open neighbours) are dissolved
    with a probability scaled by ``amount``. Because erosion only ever *adds*
    open cells, it can never sever the network — connectivity is preserved.
    """
    if amount <= 1e-3:
        return open_mask
    rng = np.random.default_rng(seed + 555)
    k = np.ones((3, 3), dtype=np.int32)
    m = open_mask.copy()
    for _ in range(max(1, iters)):
        opencount = ndimage.convolve(m.astype(np.int32), k, mode="constant", cval=0)
        cand = (~m) & (opencount >= 5)
        roll = rng.random(m.shape) < (0.35 + 0.55 * float(np.clip(amount, 0.0, 1.0)))
        m = m | (cand & roll)
    return m


def _enforce_connectivity(open_mask: np.ndarray) -> np.ndarray:
    """Keep all chambers but guarantee one network: carve a straight corridor
    from every isolated component to the nearest cell of the largest one."""
    lbl, n = ndimage.label(open_mask)
    if n <= 1:
        return open_mask
    sizes = ndimage.sum(np.ones_like(lbl), lbl, range(1, n + 1))
    main = int(np.argmax(sizes)) + 1
    main_mask = lbl == main
    _d, (iy, ix) = ndimage.distance_transform_edt(~main_mask, return_indices=True)
    out = open_mask.copy()
    for comp in range(1, n + 1):
        if comp == main:
            continue
        ys, xs = np.nonzero(lbl == comp)
        ci = int(np.argmin(_d[ys, xs]))
        y0, x0 = int(ys[ci]), int(xs[ci])
        y1, x1 = int(iy[y0, x0]), int(ix[y0, x0])
        _carve_line(out, y0, x0, y1, x1)
        main_mask = main_mask | (lbl == comp)
    return out


def _carve_line(mask: np.ndarray, y0, x0, y1, x1, width: int = 1):
    """Bresenham corridor of half-width ``width`` carved open into ``mask``."""
    dx = abs(x1 - x0); dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    x, y = x0, y0
    res = mask.shape[0]
    while True:
        y0c, y1c = max(0, y - width), min(res, y + width + 1)
        x0c, x1c = max(0, x - width), min(res, x + width + 1)
        mask[y0c:y1c, x0c:x1c] = True
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy; x += sx
        if e2 < dx:
            err += dx; y += sy


# --- water -----------------------------------------------------------------

def _flow_rivers(floor: np.ndarray, open_mask: np.ndarray, pools: np.ndarray,
                 density: float) -> np.ndarray:
    """D8 flow accumulation over open cells -> river channel mask.

    Each open cell drains to its lowest lower open neighbour; accumulating in
    descending-height order yields drainage. High-accumulation, non-pooled cells
    become flowing underground rivers feeding the pools.
    """
    res = floor.shape[0]
    big = floor.copy()
    big[~open_mask] = np.inf  # rock never participates in flow
    flat = big.ravel()
    receiver = np.full(res * res, -1, dtype=np.int64)
    ys, xs = np.nonzero(open_mask)
    neigh = ((-1, 0), (1, 0), (0, -1), (0, 1),
             (-1, -1), (-1, 1), (1, -1), (1, 1))
    for y, x in zip(ys, xs):
        h0 = big[y, x]
        best_h = h0
        best = -1
        for dy, dx in neigh:
            ny, nx = y + dy, x + dx
            if 0 <= ny < res and 0 <= nx < res and big[ny, nx] < best_h:
                best_h = big[ny, nx]
                best = ny * res + nx
        receiver[y * res + x] = best

    acc = np.zeros(res * res, dtype=np.float64)
    idx = np.argsort(flat)[::-1]  # process highest first so flow runs downhill
    acc_open = open_mask.ravel()
    for u in idx:
        if not acc_open[u]:
            continue
        acc[u] += 1.0
        r = receiver[u]
        if r >= 0:
            acc[r] += acc[u]
    acc2d = acc.reshape(res, res)
    if acc2d.max() <= 0:
        return np.zeros((res, res), dtype=bool)
    q = 1.0 - (0.02 + 0.06 * float(np.clip(density, 0.0, 1.0)))
    thr = float(np.quantile(acc2d[open_mask], np.clip(q, 0.5, 0.995)))
    rivers = open_mask & (acc2d > thr) & ~pools
    return rivers


# --- main entry ------------------------------------------------------------

def generate_cave_terrain(params: CaveParams) -> TerrainResult:
    res = max(64, min(2048, int(params.resolution)))
    stats = {}
    if int(params.seed) < 0:
        params.seed = int(np.random.default_rng().integers(0, 2**31 - 1))
    seed = int(params.seed)
    stats["seed"] = seed

    density = float(np.clip(params.cavern_density, 0.05, 0.9))
    size = float(np.clip(params.cavern_size, 0.0, 1.0))

    # --- Phase 1: macro structure + carved rooms ---------------------------
    t = time.time()
    fault = _fault_field(res, seed)
    # Fewer, larger, well-spaced caverns. Bigger rooms => wider spacing; denser
    # maps => closer spacing. Spacing scales with room size so big halls don't
    # overlap into mush.
    min_dist = res * (0.11 + 0.16 * size) / np.sqrt(0.4 + density)
    nodes = _poisson_disk(res, min_dist, seed)
    if len(nodes) < 2:  # tiny maps: fall back to two anchors
        nodes = [(res // 3, res // 3), (2 * res // 3, 2 * res // 3)]
    edges = _graph_edges(nodes, params.extra_tunnels)

    # Shape + size variety: each POI draws an archetype so the network reads as
    # distinct zones — long wide cracks, big lumpy halls, irregular grottos —
    # rather than identical round bubbles. ~22% are *composite* caverns: two or
    # three overlapping lobes of different archetypes (e.g. a crack bulging into a
    # lumpy hall) merged into one irregular chamber.
    caverns = np.zeros((res, res), dtype=bool)
    base_half = max(3, int(res * (0.030 + 0.060 * size)))
    n_composite = 0
    for i, (cy, cx) in enumerate(nodes):
        rs = np.random.default_rng(seed + 17 * i + 3)
        composite = rs.random() < 0.22
        n_lobes = 1 if not composite else (2 if rs.random() < 0.7 else 3)
        if composite:
            n_composite += 1
        for j in range(n_lobes):
            scale, aspect, lump = _room_shape(rs)
            half = max(3, int(round(base_half * scale)))
            if j == 0:
                ly, lx = cy, cx
            else:
                # Offset lobes overlap the primary so they fuse into one chamber.
                ang = float(rs.uniform(0.0, 2.0 * np.pi))
                d = base_half * float(rs.uniform(0.5, 1.2))
                ly = int(cy + d * np.sin(ang))
                lx = int(cx + d * np.cos(ang))
            _carve_room(caverns, ly, lx, half, seed=seed + 17 * i + 3 + 7 * j,
                        aspect=aspect, theta=float(rs.uniform(0.0, np.pi)),
                        lump=lump)
    stats["pois"] = len(nodes)
    stats["composite_caverns"] = n_composite

    tunnels = _route_tunnels(res, nodes, edges, fault, seed,
                             params.tunnel_windiness, params.tunnel_width,
                             params.fault_strength)
    open_mask = caverns | tunnels
    stats["carve_s"] = round(time.time() - t, 3)

    # --- Phase 2: naturalize (warp + smooth + erode) -----------------------
    t = time.time()
    open_mask = _warp_mask(open_mask, seed)
    open_mask = _cellular_smooth(open_mask, params.ca_iterations)
    # Morphological opening removes the 1px filaments/speckle the warp + CA leave
    # behind (tunnels are >=2px so they survive); connectivity is re-stitched
    # afterwards so trimmed rooms stay on one network.
    open_mask = ndimage.binary_opening(open_mask, structure=_disk(1))
    open_mask = _erode_walls(open_mask, params.erosion_amount, seed)
    open_mask = _cellular_smooth(open_mask, 1)
    open_mask = _enforce_connectivity(open_mask)
    rock = ~open_mask
    stats["open_fraction"] = round(float(open_mask.mean()), 3)
    stats["smooth_s"] = round(time.time() - t, 3)

    # Floor heightmap: vary only over open cells (normalized [0,1]); rock reads as
    # a high ceiling so wall edges render as cliffs and never collect water.
    t = time.time()
    raw = _hm.fbm(res, seed + 51, octaves=5, base_freq=6)
    floor = np.ones((res, res), dtype=np.float64)
    if open_mask.any():
        ov = raw[open_mask]
        lo, hi = float(ov.min()), float(ov.max())
        norm = (raw - lo) / (hi - lo) if hi - lo > 1e-9 else np.zeros_like(raw)
        floor = np.where(open_mask, np.clip(norm, 0.0, 1.0), 1.0)

    # Water table: pools where the open floor dips below water_level.
    wl = float(np.clip(params.water_level, 0.0, 0.95))
    pools = open_mask & (floor < wl)
    lake_depth = np.where(pools, wl - floor, 0.0)
    rivers = _flow_rivers(floor, open_mask, pools, params.river_density)
    flooded = pools | rivers
    stats["water_s"] = round(time.time() - t, 3)
    stats["pool_cells"] = int(pools.sum())
    stats["river_cells"] = int(rivers.sum())

    # Moisture: proximity to any standing/flowing water.
    if flooded.any():
        dist = ndimage.distance_transform_edt(~flooded)
        moisture = np.exp(-(dist / max(1.0, res * 0.014)) ** 2)
    else:
        moisture = np.zeros((res, res), dtype=np.float64)

    biome = _cb.classify_caves(open_mask, floor, moisture, flooded, seed=seed,
                               lava_amount=params.lava_amount,
                               crystal_amount=params.crystal_amount,
                               ice_amount=params.ice_amount,
                               biome_blend=params.biome_blend,
                               fault=fault)

    gy, gx = np.gradient(floor)
    slope = np.hypot(gx, gy)

    # ``water`` (for placement) = walls OR standing water, so ``land = ~water``
    # collapses to the walkable, dry cave floor that nodes should sit on.
    placement_water = rock | flooded
    layers = {
        "height": floor,
        "slope": slope,
        "water": placement_water,
        "moisture": moisture,
        "biome": biome,
        "biome_mode": "cave",
        "sea_level": wl,
        "land_fraction": float(open_mask.mean()),
        # Cave-specific masks (persisted + used by the cave renderer).
        "open": open_mask,
        "rock": rock,
        "flooded": flooded,
        "lake_mask": pools,
        "lake_depth": lake_depth,
        "river_mask": rivers,
    }
    stats["resolution"] = res
    return TerrainResult(params=params, layers=layers, stats=stats)


if __name__ == "__main__":  # pragma: no cover - dev smoke test
    import os
    out = os.environ.get("CAVE_OUT", ".")
    for sd in (7, 21, 99):
        for rez in (512, 1024):
            r = generate_cave_terrain(CaveParams(seed=sd, resolution=rez))
            L = r.layers
            assert np.isfinite(L["height"]).all()
            n_comp = ndimage.label(L["open"])[1]
            of = r.stats["open_fraction"]
            print(f"seed={sd} res={rez} open_frac={of} pois={r.stats['pois']} "
                  f"components={n_comp} carve={r.stats['carve_s']}s "
                  f"smooth={r.stats['smooth_s']}s water={r.stats['water_s']}s")
            assert n_comp == 1, f"expected single component, got {n_comp}"
            assert 0.08 <= of <= 0.5, f"open fraction {of} out of range"
            try:
                from wbworldgen.terrain import render as _rd
                png = _rd.cave_png(L, z_scale=r.params.relief,
                                   hillshade_strength=r.params.hillshade_strength,
                                   seed=sd, terrace_steps=r.params.terrace_steps,
                                   ssao_strength=r.params.ssao_strength)
                path = os.path.join(out, f"cave_s{sd}_r{rez}.png")
                with open(path, "wb") as f:
                    f.write(png)
                print("  wrote", path)
            except Exception as e:
                print("  render skipped:", e)
