"""River network extraction, meandering and carving.

Built on top of :mod:`hydrology` (Priority-Flood + D-infinity + Strahler):

    threshold accumulation -> river mask
    -> Strahler order (channel width)
    -> trace headwater->sea polylines along the dominant-receiver tree
    -> Menger-curvature meandering (amplifies real bends; slope/order aware)
    -> Chaikin smoothing

Menger meandering follows the geometric approach from the research: rather than
stamping a sine wave, it displaces each vertex along the outward normal of its
*existing* curvature, so meanders grow as a natural instability from the small
wiggles already in the routed path — straight mountain reaches stay straight,
gentle lowland reaches bloom into sweeping bends.
"""

import numpy as np

from wbworldgen.terrain import hydrology as _hy


def _trace_polylines(river_mask, primary, order):
    """Trace connected polylines headwater->sea along the primary-receiver tree.

    Returns a list of Nx2 (x, y) cell-coordinate arrays. Each channel edge is
    emitted once; tributaries stop when they merge into an already-traced trunk.
    """
    res = river_mask.shape[0]
    ys, xs = np.nonzero(river_mask)
    indeg = np.zeros((res, res), np.int32)
    for y, x in zip(ys, xs):
        r = int(primary[y, x])
        if r < 0:
            continue
        ry, rx = r // res, r % res
        if river_mask[ry, rx]:
            indeg[ry, rx] += 1

    visited = np.zeros((res, res), np.bool_)
    paths = []
    for y, x in zip(ys, xs):
        if indeg[y, x] != 0:
            continue  # start only at headwaters
        cy, cx = int(y), int(x)
        pts = [(cx + 0.5, cy + 0.5)]
        visited[cy, cx] = True
        steps = 0
        while True:
            r = int(primary[cy, cx])
            if r < 0:
                break
            ny, nx = r // res, r % res
            pts.append((nx + 0.5, ny + 0.5))
            if not river_mask[ny, nx]:
                break  # reached the sea
            if visited[ny, nx]:
                break  # merged into a trunk
            visited[ny, nx] = True
            cy, cx = ny, nx
            steps += 1
            if steps > 4 * res:
                break
        if len(pts) >= 2:
            paths.append(np.array(pts, dtype=np.float64))
    return paths


def _resample(pts, step):
    """Resample a polyline to roughly uniform spacing ``step`` (cells)."""
    if len(pts) < 3:
        return pts
    seg = np.diff(pts, axis=0)
    d = np.hypot(seg[:, 0], seg[:, 1])
    s = np.concatenate([[0.0], np.cumsum(d)])
    total = s[-1]
    if total < step * 2:
        return pts
    targets = np.arange(0.0, total, step)
    x = np.interp(targets, s, pts[:, 0])
    y = np.interp(targets, s, pts[:, 1])
    out = np.column_stack([x, y])
    out[-1] = pts[-1]  # keep the exact mouth/junction
    return out


def _meander(pts, order_grid, slope_norm, amp, res, seed, iterations=6):
    """Grow natural meanders à la the research's geometric method.

    Meandering is an instability: a tiny perpendicular *seed* perturbation is
    iteratively amplified along the outward normal of the path's local curvature
    (Menger curvature), with points reinserted as the path lengthens. Amplitude
    is scaled by discharge (Strahler order) and flatness, and tapered to zero at
    the endpoints so confluences/mouths stay anchored. Straight steep reaches
    barely move; gentle lowland reaches bloom into sweeping bends.
    """
    if len(pts) < 6 or amp <= 0:
        return pts
    res1 = res - 1
    rng = np.random.default_rng(seed)
    gain = amp * 0.6

    # Seed: small perpendicular noise so meanders have something to amplify.
    pts = pts.copy()
    seg = np.diff(pts, axis=0)
    tl = np.hypot(seg[:, 0], seg[:, 1])
    avg = max(0.5, float(tl.mean()))

    for it in range(iterations):
        n = len(pts)
        if n < 6:
            break
        a = pts[:-2]; b = pts[1:-1]; c = pts[2:]
        lac = np.hypot(c[:, 0] - a[:, 0], c[:, 1] - a[:, 1]) + 1e-9
        tx = (c[:, 0] - a[:, 0]) / lac
        ty = (c[:, 1] - a[:, 1]) / lac
        nx, ny = -ty, tx
        cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - b[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - b[:, 0])
        lab = np.hypot(b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]) + 1e-9
        lcb = np.hypot(c[:, 0] - b[:, 0], c[:, 1] - b[:, 1]) + 1e-9
        kappa = 2.0 * cross / (lab * lcb * lac)          # signed curvature

        bx = np.clip(b[:, 0].astype(int), 0, res1)
        by = np.clip(b[:, 1].astype(int), 0, res1)
        ordv = order_grid[by, bx].astype(np.float64)
        of = 0.35 + 0.65 * np.clip(ordv / 4.0, 0.0, 1.0)
        flat = 1.0 - 0.75 * np.clip(slope_norm[by, bx], 0.0, 1.0)
        # taper amplitude to 0 at the two ends
        idx = np.arange(1, n - 1)
        taper = np.clip(np.minimum(idx, n - 1 - idx) / (0.18 * n + 1e-9), 0.0, 1.0)

        # curvature amplification + a small seed floor on the first pass
        seedf = (rng.standard_normal(n - 2) * 0.5) if it == 0 else 0.0
        mag = gain * (np.tanh(np.abs(kappa) * 8.0) * np.sign(kappa) * avg + seedf)
        mag *= of * flat * taper

        pts = pts.copy()
        pts[1:-1, 0] += nx * mag
        pts[1:-1, 1] += ny * mag
        np.clip(pts[:, 0], 1, res1, out=pts[:, 0])
        np.clip(pts[:, 1], 1, res1, out=pts[:, 1])
        # Reinsert points where segments stretched, to keep resolution.
        pts = _resample(pts, avg)
    return pts


def _chaikin(pts, iters=2):
    pts = np.asarray(pts, dtype=np.float64)
    for _ in range(iters):
        if len(pts) < 3:
            break
        p = pts[:-1]
        q = pts[1:]
        inter = np.empty((2 * len(p), 2))
        inter[0::2] = 0.75 * p + 0.25 * q
        inter[1::2] = 0.25 * p + 0.75 * q
        pts = np.vstack([pts[0], inter, pts[-1]])
    return pts


def _rotate(v, ang):
    ca, sa = np.cos(ang), np.sin(ang)
    return np.array([v[0] * ca - v[1] * sa, v[0] * sa + v[1] * ca])


def _grow_delta(pos, d, dist, length, max_reach, gen, max_gen, rng,
                height, sea_level, res, out):
    """Recursive stochastic L-system branch growing seaward, splitting into
    thinner distributaries. Each emitted point carries its distance from the
    mouth (``dist``) so deposition can decay with distance. Growth stops at land,
    off-grid, or once it exceeds ``max_reach`` cells from the mouth — this hard
    cap is what keeps distributaries from streaking across open ocean."""
    if gen > max_gen or length < 2:
        return
    res1 = res - 1
    pts = [(pos[0], pos[1], dist)]
    cur = pos.copy()
    dd = d.copy()
    for _ in range(int(max(2, length))):
        dd = _rotate(dd, rng.normal(0.0, 0.12))   # gentle wander
        cur = cur + dd
        dist += 1.0
        ix, iy = int(cur[0]), int(cur[1])
        if ix < 0 or ix > res1 or iy < 0 or iy > res1:
            break
        if height[iy, ix] >= sea_level:
            break  # reached land — distributary ends
        if dist > max_reach:
            break  # too far from the mouth — sediment never reaches here
        pts.append((cur[0], cur[1], dist))
    if len(pts) < 2:
        return
    out.append(np.array(pts))
    # Branch into 2-3 thinner, shorter distributaries (bird-foot fan).
    nchild = 2 if rng.random() < 0.6 else 3
    spread = rng.uniform(0.35, 0.6)
    for k in range(nchild):
        ang = (k - (nchild - 1) / 2.0) * spread + rng.normal(0.0, 0.08)
        _grow_delta(cur, _rotate(dd, ang), dist, length * rng.uniform(0.6, 0.78),
                    max_reach, gen + 1, max_gen, rng, height, sea_level, res, out)


def _build_deltas(paths, order, height, sea_level, min_order=3, size=1.0,
                  max_gen=3, seed=0):
    """Build L-system distributary deltas at every sufficiently large river
    mouth. Returns extra polylines (in cell coords) to draw over the ocean."""
    res = height.shape[0]
    res1 = res - 1
    rng = np.random.default_rng(seed)
    deltas = []
    for p in paths:
        if len(p) < 4:
            continue
        cx = np.clip(p[:, 0].astype(int), 0, res1)
        cy = np.clip(p[:, 1].astype(int), 0, res1)
        mo = int(order[cy, cx].max())
        if mo < min_order:
            continue  # only big rivers form deltas
        # Coast crossing = first vertex that enters the sea.
        sea_idx = np.nonzero(height[cy, cx] < sea_level)[0]
        if len(sea_idx) == 0 or sea_idx[0] < 2:
            continue
        ci = int(sea_idx[0])
        mouth = p[ci].copy()
        d = p[ci] - p[ci - 2]
        nrm = np.hypot(d[0], d[1])
        if nrm < 1e-6:
            continue
        d = d / nrm
        base_len = size * res * 0.045 * (0.6 + 0.4 * min(mo, 7) / 7.0)
        # Hard cap on how far any distributary can reach from the mouth, so the
        # delta stays a compact lobe instead of growing fingers out to sea.
        max_reach = size * res * 0.07 * (0.6 + 0.4 * min(mo, 7) / 7.0)
        _grow_delta(mouth, d, 0.0, base_len, max_reach, 0, max_gen, rng,
                    height, sea_level, res, deltas)
    return deltas


def build_rivers(height, sea_level, density=0.5, carve=0.0, meander=1.2,
                 route_height=None, deltas=True, delta_min_order=3, delta_size=1.0,
                 discharge=None, momentum=None):
    """Full river build. Returns dict with mask, Strahler order, accumulation,
    smoothed polylines and (optionally) a carved height field.

    ``route_height`` is an optional drainage-guaranteed DEM (e.g. lake-resolved)
    to route flow over; if omitted the height field is filled here.

    ``discharge`` (and optional ``momentum``) come from the momentum-coupled
    erosion model. When supplied, the simulated discharge is used directly as
    the per-cell flow (``acc``) instead of recomputing D-infinity accumulation,
    and geometric meandering is skipped — the channels are already physically
    sinuous in the heightmap. The receiver tree (for tracing + Strahler) is still
    derived from the eroded DEM, which now follows those meanders.
    """
    res = height.shape[0]
    filled = route_height if route_height is not None else _hy.fill_depressions(height, sea_level)
    fa = _hy.flow_and_accumulation(filled, sea_level)
    primary = fa["primary"]

    use_sim = discharge is not None
    # Simulated discharge already encodes flow volume; otherwise use D-infinity.
    acc = np.asarray(discharge, dtype=np.float64) if use_sim else fa["acc"]

    land = height > sea_level
    thresh = max(6.0, (1.0 - np.clip(density, 0.0, 1.0)) * res * 0.6)
    if use_sim:
        # Discharge is in particle-volume units, not drainage-cell counts, so
        # scale the threshold to its own distribution (channels vs. sheet flow).
        refd = float(np.quantile(acc[land], 0.985)) if land.any() else 1.0
        thresh = max(1e-6, (1.0 - np.clip(density, 0.0, 1.0)) * refd)
    river_mask = (acc >= thresh) & land
    order = _hy.strahler_order(primary, river_mask, acc)

    # Continuous "water field" for rendering: a smooth, connected flow magnitude
    # in [0,1]. Built from the D-infinity accumulation on the depression-filled
    # DEM (fa["acc"]) — guaranteed to drain to the sea (Priority-Flood), so it
    # never dead-ends. The low end is anchored at the *channelization threshold*
    # (the drainage area where a channel begins) so sheet flow stays at 0 and
    # only real channels light up; above it, log-compressed so trunks read
    # bright/wide and headwaters faint. The area threshold uses the same
    # density->area rule as the droplet mask, so "River Density" feels consistent.
    area_thresh = max(6.0, (1.0 - np.clip(density, 0.0, 1.0)) * res * 0.6)
    facc = np.log1p(np.maximum(fa["acc"], 0.0))
    lo = float(np.log1p(area_thresh))
    ref = float(np.quantile(facc[land], 0.997)) if land.any() else float(facc.max() or 1.0)
    river_field = np.clip((facc - lo) / max(ref - lo, 1e-9), 0.0, 1.0)
    if use_sim:
        # Fuse the simulated channels, but only their strong cores (>0.9 quantile
        # of discharge) so the connected accumulation stays the backbone.
        from scipy.ndimage import gaussian_filter
        d = gaussian_filter(np.asarray(acc, dtype=np.float64), 1.0)
        dlo = float(np.quantile(d[land], 0.9)) if land.any() else 0.0
        dref = float(np.quantile(d[land], 0.999)) if land.any() else 1.0
        dfield = np.clip((d - dlo) / max(dref - dlo, 1e-9), 0.0, 1.0)
        river_field = np.maximum(river_field, dfield)
    river_field = river_field * land

    # Slope (normalized) so meanders widen on flat ground, tighten on steep.
    gy, gx = np.gradient(height)
    slope = np.hypot(gx, gy)
    ref = np.quantile(slope[land], 0.9) if land.any() else float(slope.max() or 1)
    slope_norm = slope / max(ref, 1e-6)

    raw = _trace_polylines(river_mask, primary, order)
    paths = []
    for k, p in enumerate(raw):
        p = _resample(p, step=1.5)
        # Geometric meandering only for the droplet model. With simulated flow
        # the channels already meander physically, so we just smooth the trace.
        if not use_sim:
            p = _meander(p, order, slope_norm, meander, res, seed=(k * 2654435761) & 0x7FFFFFFF)
        p = _chaikin(p, iters=2)
        paths.append(p)

    out_height = height
    if carve > 0:
        strength = np.zeros_like(height)
        oi = order.astype(np.float64)
        strength[river_mask] = np.clip(oi[river_mask] / 4.0, 0.15, 1.0)
        out_height = height - carve * strength

    # L-system distributary deltas at large river mouths. The branches grow
    # seaward; we deposit a narrow band of *land* (sediment) around them and
    # then draw the channels through it, producing a bird-foot delta.
    if deltas:
        dpaths = _build_deltas(paths, order, height, sea_level,
                               min_order=delta_min_order, size=delta_size,
                               seed=0)
        if dpaths:
            from scipy.ndimage import gaussian_filter
            # Deposit sediment as a *thickness* field that decays exponentially
            # with distance from the mouth (carried in column 2 of each branch).
            # Adding thickness to the seabed raises near-mouth cells above water
            # (emergent lobe) while far cells merely shoal and stay submerged —
            # so the delta fades into shallows instead of forming land tentacles.
            decay = max(4.0, 0.03 * res * delta_size)   # e-folding length (cells)
            max_thick = 0.05                              # cap at the mouth
            thick = np.zeros((res, res), dtype=np.float64)
            for br in dpaths:
                ix = np.clip(br[:, 0].astype(int), 0, res - 1)
                iy = np.clip(br[:, 1].astype(int), 0, res - 1)
                amp = max_thick * np.exp(-br[:, 2] / decay)
                np.maximum.at(thick, (iy, ix), amp)
            # Spread each splat into a small lobe and smooth the deposit.
            thick = gaussian_filter(thick, sigma=1.5)
            out_height = out_height.copy()
            sea_cells = out_height < sea_level
            # Raise the seabed by the deposited thickness (only under the sea).
            out_height[sea_cells] = np.minimum(
                out_height[sea_cells] + thick[sea_cells], sea_level + 0.006)
            paths.extend(_chaikin(d[:, :2], iters=1) for d in dpaths)

    return {
        "river_mask": river_mask, "order": order, "acc": acc,
        "river_paths": paths, "height": out_height, "river_field": river_field,
        "max_order": int(order.max()) if river_mask.any() else 0,
    }


if __name__ == "__main__":  # pragma: no cover
    import time
    from wbworldgen.terrain.pipeline import generate_terrain, TerrainParams
    r = generate_terrain(TerrainParams(seed=7, resolution=512))
    print("river stats:", {k: v for k, v in r.stats.items() if "river" in k or k.endswith("_s")})
