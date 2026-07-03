"""Base heightmap: fractal value-noise with domain warping.

Pure numpy, deterministic from a seed. We use a self-contained value-noise
implementation (no external noise lib) so the only dependency is numpy. The
noise is smooth (cosine-interpolated lattice) and summed across octaves (FBM)
to produce organic, curvy terrain. Domain warping bends the field so ridges and
valleys meander instead of looking like axis-aligned blobs.
"""

import numpy as np


def _value_noise_grid(res: int, freq: int, rng: np.random.Generator) -> np.ndarray:
    """One octave of smooth value noise sampled on a ``res`` x ``res`` grid.

    A ``(freq+1) x (freq+1)`` lattice of random values is smoothly interpolated
    (smoothstep) up to the full resolution.
    """
    lattice = rng.random((freq + 1, freq + 1)).astype(np.float64)

    # Sample coordinates in lattice space.
    coords = np.linspace(0, freq, res, endpoint=False)
    i = np.floor(coords).astype(int)
    f = coords - i
    # smoothstep weights for a smooth (C1) interpolation.
    w = f * f * (3.0 - 2.0 * f)

    i0 = i
    i1 = np.minimum(i + 1, freq)
    wx = w[None, :]
    wy = w[:, None]

    # Gather the four lattice corners for every output cell via broadcasting.
    c00 = lattice[np.ix_(i0, i0)]
    c10 = lattice[np.ix_(i0, i1)]
    c01 = lattice[np.ix_(i1, i0)]
    c11 = lattice[np.ix_(i1, i1)]

    top = c00 * (1 - wx) + c10 * wx
    bot = c01 * (1 - wx) + c11 * wx
    return top * (1 - wy) + bot * wy


def _perlin_grid(res: int, freq: int, rng: np.random.Generator) -> np.ndarray:
    """One octave of gradient (Perlin) noise, ~[-1, 1].

    Random unit gradient per lattice point; the value at each sample is the
    quintic-faded bilinear blend of the four corner gradient dot-products.
    Unlike value noise, gradient noise has sharp zero-crossings, which is what
    gives ridged-multifractal terrain crisp ridge-and-valley creases.
    """
    ang = rng.random((freq + 1, freq + 1)) * (2.0 * np.pi)
    gx = np.cos(ang)
    gy = np.sin(ang)

    coords = np.linspace(0, freq, res, endpoint=False)
    i = np.floor(coords).astype(int)
    f = coords - i
    i0 = i
    i1 = np.minimum(i + 1, freq)
    u = f * f * f * (f * (f * 6 - 15) + 10)  # quintic fade

    g00x = gx[np.ix_(i0, i0)]; g00y = gy[np.ix_(i0, i0)]
    g10x = gx[np.ix_(i0, i1)]; g10y = gy[np.ix_(i0, i1)]
    g01x = gx[np.ix_(i1, i0)]; g01y = gy[np.ix_(i1, i0)]
    g11x = gx[np.ix_(i1, i1)]; g11y = gy[np.ix_(i1, i1)]

    dx0 = f[None, :]; dx1 = (f - 1)[None, :]
    dy0 = f[:, None]; dy1 = (f - 1)[:, None]

    n00 = g00x * dx0 + g00y * dy0
    n10 = g10x * dx1 + g10y * dy0
    n01 = g01x * dx0 + g01y * dy1
    n11 = g11x * dx1 + g11y * dy1

    ux = u[None, :]
    vy = u[:, None]
    nx0 = n00 * (1 - ux) + n10 * ux
    nx1 = n01 * (1 - ux) + n11 * ux
    val = nx0 * (1 - vy) + nx1 * vy
    return val * 1.4142135623730951  # scale toward [-1, 1]


def fbm(res: int, seed: int, octaves: int = 6, base_freq: int = 4,
        lacunarity: float = 2.0, gain: float = 0.5) -> np.ndarray:
    """Fractal Brownian motion: sum of value-noise octaves, normalized to [0,1]."""
    rng = np.random.default_rng(seed)
    out = np.zeros((res, res), dtype=np.float64)
    amp = 1.0
    freq = base_freq
    total_amp = 0.0
    for _ in range(max(1, octaves)):
        # Each octave gets a distinct sub-stream so octaves are uncorrelated.
        sub = np.random.default_rng(rng.integers(0, 2**31 - 1))
        out += amp * _value_noise_grid(res, freq, sub)
        total_amp += amp
        amp *= gain
        freq = max(1, int(round(freq * lacunarity)))
    out /= total_amp
    return out


def ridged_fbm(res: int, seed: int, octaves: int = 6, base_freq: int = 6,
               sharpness: float = 2.0) -> np.ndarray:
    """Ridged multifractal-ish noise in [0,1].

    Folds FBM around its midpoint (``1 - |2n-1|``) to create sharp ridge lines
    rather than smooth blobs, then raises to ``sharpness`` to thin the ridges.
    Used to give uplifted tectonic belts believable mountain ridgelines.
    """
    n = fbm(res, seed, octaves=octaves, base_freq=base_freq)
    ridges = 1.0 - np.abs(2.0 * n - 1.0)
    return np.clip(ridges, 0.0, 1.0) ** max(0.1, sharpness)


def ridged_multifractal(res: int, seed: int, octaves: int = 7, base_freq: int = 5,
                        lacunarity: float = 2.0, gain: float = 2.0,
                        offset: float = 1.0, h: float = 0.9) -> np.ndarray:
    """Musgrave ridged multifractal noise in [0,1].

    Unlike a plain ridged-FBM (which folds the *summed* octaves once), this
    folds and squares *each* octave (``(offset-|n|)^2``) and weights every
    octave by the accumulated signal of the lower ones. That feedback makes
    already-high ridges accumulate rugged fine detail while valleys stay
    smooth — producing meaningful ridge-and-valley alpine structure rather than
    uniform soft bumps.

    Ref: Musgrave, "Procedural Fractal Terrains" (RidgedMultifractal).
    """
    rng = np.random.default_rng(seed)
    result = np.zeros((res, res), dtype=np.float64)
    weight = np.ones((res, res), dtype=np.float64)
    freq = float(base_freq)
    norm = 0.0
    for _ in range(max(1, octaves)):
        sub = np.random.default_rng(rng.integers(0, 2**31 - 1))
        n = _perlin_grid(res, max(1, int(round(freq))), sub)
        signal = offset - np.abs(n)        # ridge where |n| ~ 0 (sharp crease)
        signal *= signal                   # sharpen the ridge crest
        signal *= weight                   # detail rides on previous octaves
        weight = np.clip(signal * gain, 0.0, 1.0)
        a = freq ** (-h)                    # spectral amplitude (1/f^h)
        result += signal * a
        norm += a
        freq *= lacunarity
    result /= max(norm, 1e-9)
    lo, hi = float(result.min()), float(result.max())
    if hi - lo > 1e-9:
        result = (result - lo) / (hi - lo)
    return result


def mountain_field(res: int, seed: int, coverage: float = 0.5,
                   sharpness: float = 2.0, warp: float = 0.4) -> np.ndarray:
    """Noise-based mountain chains in [0,1] (no tectonics).

    Combines three ideas used by most procedural-terrain generators:
      * ridged noise gives sharp linear *ridgelines* (chains, not blobs);
      * domain warping bends those chains so they meander;
      * a low-frequency mask clusters ranges into belts instead of covering the
        whole map, with ``coverage`` setting how much terrain is mountainous.
    """
    ridges = ridged_multifractal(res, seed + 733, octaves=8, base_freq=6)
    # ``sharpness`` thins (>1) or broadens (<1) the ridges.
    ridges = np.clip(ridges, 0.0, 1.0) ** max(0.1, sharpness)

    if warp > 0:
        # Bend the ridgelines into sinuous chains.
        wx = fbm(res, seed + 501, octaves=3) - 0.5
        wy = fbm(res, seed + 502, octaves=3) - 0.5
        coords = np.arange(res)
        gx, gy = np.meshgrid(coords, coords)
        amp = warp * res * 0.12
        sx = np.clip((gx + wx * amp).astype(int), 0, res - 1)
        sy = np.clip((gy + wy * amp).astype(int), 0, res - 1)
        ridges = ridges[sy, sx]

    # Clustering mask: smoothstep over a low-freq field so ranges occur in
    # belts/regions. Higher coverage -> more of the map is mountainous.
    region = fbm(res, seed + 822, octaves=3, base_freq=3)
    lo = np.clip(1.0 - coverage - 0.15, 0.0, 1.0)
    hi = np.clip(1.0 - coverage + 0.15, 0.0, 1.0)
    mask = np.clip((region - lo) / max(1e-6, hi - lo), 0.0, 1.0)
    mask = mask * mask * (3.0 - 2.0 * mask)  # smoothstep
    return ridges * mask


def _radial_falloff(res: int, strength: float) -> np.ndarray:
    """Smooth bowl that pushes the map edges toward sea (continents/islands)."""
    lin = np.linspace(-1.0, 1.0, res)
    gx, gy = np.meshgrid(lin, lin)
    dist = np.sqrt(gx * gx + gy * gy) / np.sqrt(2.0)  # 0 center -> 1 corner
    falloff = np.clip(1.0 - dist, 0.0, 1.0)
    falloff = falloff ** max(0.1, strength)
    return falloff


def base_heightmap(seed: int, res: int = 512, octaves: int = 6,
                   warp: float = 0.35, island: float = 0.0) -> np.ndarray:
    """Generate the base height field in [0,1].

    Args:
        seed: deterministic seed.
        res: grid resolution (res x res).
        octaves: FBM octave count (more = finer detail).
        warp: domain-warp strength (0 = none). Bends the field organically.
        island: 0 = continent-to-edge; >0 applies a radial falloff so land
            concentrates centrally (1.0 ~ pronounced island).
    """
    height = fbm(res, seed, octaves=octaves)

    if warp > 0:
        # Domain warp: offset sample positions by two extra low-freq noise fields.
        wx = fbm(res, seed + 101, octaves=4) - 0.5
        wy = fbm(res, seed + 202, octaves=4) - 0.5
        coords = np.arange(res)
        gx, gy = np.meshgrid(coords, coords)
        amp = warp * res * 0.15
        sx = np.clip((gx + wx * amp).astype(int), 0, res - 1)
        sy = np.clip((gy + wy * amp).astype(int), 0, res - 1)
        height = height[sy, sx]

    if island > 0:
        height = height * _radial_falloff(res, island)

    # Normalize to full [0,1].
    lo, hi = float(height.min()), float(height.max())
    if hi - lo > 1e-9:
        height = (height - lo) / (hi - lo)
    return height.astype(np.float64)


def _spline_ridge_field(res: int, seed: int, count: int = 1,
                        width: float = 0.12, length: float = 1.0) -> np.ndarray:
    """Generate a [0,1] height field of meandering mountain arcs across the map.

    Each arc walks from a start point along a heading for ``length`` of the map
    diagonal, with the centerline pushed sideways by a sine wave plus a random
    walk so it genuinely snakes (rather than bowing once). The field value at
    each cell is a smooth falloff from the nearest arc centerline, so splines
    add a ridge-shaped bump that tapers off with distance. Multiple arcs are
    combined via max.
    """
    rng = np.random.default_rng(seed + 9137)
    half_w = max(1.0, width * res)
    diag = res * 1.4142135623730951
    total_len = float(np.clip(length, 0.1, 10.0)) * diag

    coords = np.arange(res, dtype=np.float64)
    gx, gy = np.meshgrid(coords, coords)
    field = np.zeros((res, res), dtype=np.float64)

    for _ in range(max(1, count)):
        # Random heading and its perpendicular (the meander axis).
        ang = float(rng.uniform(0.0, 2.0 * np.pi))
        dirx, diry = np.cos(ang), np.sin(ang)
        perpx, perpy = -diry, dirx

        # Centre the arc on the map, then jitter, so it tends to stay on-screen.
        sx = res * 0.5 - dirx * total_len * 0.5 + float(rng.uniform(-0.2, 0.2)) * res
        sy = res * 0.5 - diry * total_len * 0.5 + float(rng.uniform(-0.2, 0.2)) * res

        # Meander: a sine sweep (1–3 lobes) plus a slow random walk for irregularity.
        amp = float(rng.uniform(0.08, 0.18)) * res
        lobes = float(rng.uniform(1.0, 3.0))
        phase = float(rng.uniform(0.0, 2.0 * np.pi))

        n_seg = 24
        walk = 0.0
        pts = []
        for i in range(n_seg + 1):
            t = i / n_seg
            bx = sx + dirx * total_len * t
            by = sy + diry * total_len * t
            walk += float(rng.uniform(-0.04, 0.04)) * res
            off = amp * np.sin(phase + lobes * 2.0 * np.pi * t) + walk
            pts.append(np.array([bx + perpx * off, by + perpy * off]))

        # Chaikin subdivision to round the polyline into a smooth curve.
        for _ in range(2):
            refined = [pts[0]]
            for i in range(len(pts) - 1):
                q = 0.75 * pts[i] + 0.25 * pts[i + 1]
                r = 0.25 * pts[i] + 0.75 * pts[i + 1]
                refined.extend([q, r])
            refined.append(pts[-1])
            pts = refined

        # Distance field: min squared distance to any segment in the polyline.
        dist2 = np.full((res, res), np.inf, dtype=np.float64)
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            abx, aby = bx - ax, by - ay
            ab2 = abx * abx + aby * aby
            if ab2 < 1e-9:
                continue
            t = np.clip(((gx - ax) * abx + (gy - ay) * aby) / ab2, 0.0, 1.0)
            cx = ax + t * abx
            cy = ay + t * aby
            d2 = (gx - cx) ** 2 + (gy - cy) ** 2
            dist2 = np.minimum(dist2, d2)

        dist = np.sqrt(dist2)
        # Smooth quadratic falloff from centerline.
        arc = np.clip(1.0 - dist / half_w, 0.0, 1.0) ** 2
        field = np.maximum(field, arc)

    return field


if __name__ == "__main__":  # pragma: no cover - dev smoke test
    import sys
    h = base_heightmap(seed=7, res=256, island=0.6)
    print("shape", h.shape, "min", h.min(), "max", h.max(), "mean", h.mean())
    try:
        from PIL import Image
        img = (np.clip(h, 0, 1) * 255).astype(np.uint8)
        Image.fromarray(img, mode="L").save("heightmap_smoke.png")
        print("wrote heightmap_smoke.png")
    except ImportError:
        print("PIL not available; skipped PNG dump", file=sys.stderr)
