"""Momentum-coupled particle hydraulic erosion (emergent meandering rivers).

A reimplementation, in numpy/numba, of the particle model from Nick McDonald's
SimpleHydrology (MIT) and the accompanying article:

    https://nickmcd.me/2023/12/12/meandering-rivers-in-particle-based-hydraulic-erosion-simulations/
    https://github.com/weigert/SimpleHydrology

No source is copied — only the algorithm is reproduced.

What makes this different from the classic droplet model in :mod:`erosion`:
each grid cell carries, in addition to its height, an exponentially-averaged
**discharge** (flow volume passing through) and **momentum** (flow direction *
volume). Descending particles both *read* the stored momentum (it steers them,
imparting a centrifugal push toward outer banks) and *write* to it (their volume
and velocity accumulate into per-cell track buffers, blended in after each
batch). Because streams thus influence one another, meanders, oxbow cut-offs and
braids **emerge in the heightmap itself** rather than being drawn on afterward.

The kernel mirrors the structure of :mod:`erosion`: an ``@njit`` particle batch
loop with a dependency-free numpy fallback selected by ``_HAS_NUMBA``.

Public entry point: :func:`momentum_erode` returns a dict with the eroded
``height`` plus the ``discharge`` and ``momentum_x`` / ``momentum_y`` fields, so
the river stage can reuse the simulated flow instead of recomputing it.
"""

import numpy as np

try:
    from numba import njit
    _HAS_NUMBA = True
except ImportError:  # pragma: no cover - exercised only without numba
    _HAS_NUMBA = False

    def njit(*args, **kwargs):  # no-op decorator so the module still imports
        def wrap(fn):
            return fn
        return wrap if args and callable(args[0]) is False else (args[0] if args else wrap)


# --------------------------------------------------------------------------
# Numba particle-batch kernel
# --------------------------------------------------------------------------

@njit(cache=True)
def _erode_radius(h, cx, cy, amount, radius, res, sea_level):
    """Remove ``amount`` of material spread over a disc of given radius, but
    never from cells below ``sea_level`` — underwater terrain is only deposited
    onto, never carved, even by the spread of a nearby coastal cut.

    Spreading erosion (rather than digging a single cell) is essential for
    stability: point erosion combined with bilinear deposition creates a
    grid-scale spike oscillation that amplifies without bound."""
    if radius <= 0:
        if h[cy, cx] >= sea_level:
            h[cy, cx] -= amount
        return
    total_w = 0.0
    for yy in range(cy - radius, cy + radius + 1):
        for xx in range(cx - radius, cx + radius + 1):
            if 0 <= xx < res and 0 <= yy < res and h[yy, xx] >= sea_level:
                w = max(0.0, radius - ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5)
                total_w += w
    if total_w <= 0:
        return  # nothing above water nearby — skip this erosion entirely
    for yy in range(cy - radius, cy + radius + 1):
        for xx in range(cx - radius, cx + radius + 1):
            if 0 <= xx < res and 0 <= yy < res and h[yy, xx] >= sea_level:
                w = max(0.0, radius - ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5)
                if w > 0:
                    h[yy, xx] -= amount * (w / total_w)


@njit(cache=True)
def _momentum_batch(h, discharge, momx, momy, num_particles, seed,
                    inertia, capacity, deposition, erosion, evaporation,
                    gravity, momentum_transfer, min_water, max_steps, radius,
                    sea_level):
    """Run one batch of particles, writing erosion into ``h`` and accumulating
    flow into the track buffers (returned). Reads the *current* discharge /
    momentum maps to steer particles (the coupling that produces meanders).

    Physics follows the stable droplet scheme (scalar ``speed`` + a unit
    direction updated with inertia, as in :mod:`erosion`); momentum enters as a
    *steering* term blended into the direction and as a discharge multiplier on
    carrying capacity, so high-flow channels self-deepen and deflect later
    particles toward their outer banks — the origin of the meanders.
    """
    res = h.shape[0]
    res1 = res - 1
    np.random.seed(seed)

    d_track = np.zeros((res, res), np.float64)
    mx_track = np.zeros((res, res), np.float64)
    my_track = np.zeros((res, res), np.float64)

    for _ in range(num_particles):
        px = np.random.random() * res1
        py = np.random.random() * res1
        dx = 0.0
        dy = 0.0
        speed = 1.0
        water = 1.0
        sediment = 0.0

        for _step in range(max_steps):
            xi = int(px)
            yi = int(py)
            if xi < 0 or xi >= res1 or yi < 0 or yi >= res1:
                break
            fx = px - xi
            fy = py - yi

            h00 = h[yi, xi]
            h10 = h[yi, xi + 1]
            h01 = h[yi + 1, xi]
            h11 = h[yi + 1, xi + 1]
            gx = (h10 - h00) * (1 - fy) + (h11 - h01) * fy
            gy = (h01 - h00) * (1 - fx) + (h11 - h10) * fx

            # Direction update with inertia (downhill), like the droplet model.
            dx = dx * inertia - gx * (1 - inertia)
            dy = dy * inertia - gy * (1 - inertia)

            # Momentum steering: bend the direction toward the stored stream
            # momentum at this cell, weighted by discharge / (water + discharge)
            # so it only matters inside established channels. This deflection is
            # what carves outer banks and grows meanders.
            disc = discharge[yi, xi]
            if disc > 0.0:
                fmx = momx[yi, xi]
                fmy = momy[yi, xi]
                fmag = (fmx * fmx + fmy * fmy) ** 0.5
                if fmag > 1e-9:
                    w = momentum_transfer * disc / (water + disc)
                    dx += w * fmx / fmag
                    dy += w * fmy / fmag

            mag = (dx * dx + dy * dy) ** 0.5
            if mag < 1e-9:
                ang = np.random.random() * 6.2831853
                dx = np.cos(ang)
                dy = np.sin(ang)
                mag = 1.0
            dx /= mag
            dy /= mag

            nx = px + dx
            ny = py + dy
            nxi = int(nx)
            nyi = int(ny)
            if nxi < 0 or nxi >= res1 or nyi < 0 or nyi >= res1:
                break

            old_h = (h00 * (1 - fx) * (1 - fy) + h10 * fx * (1 - fy)
                     + h01 * (1 - fx) * fy + h11 * fx * fy)
            nfx = nx - nxi
            nfy = ny - nyi
            nh00 = h[nyi, nxi]
            nh10 = h[nyi, nxi + 1]
            nh01 = h[nyi + 1, nxi]
            nh11 = h[nyi + 1, nxi + 1]
            new_h = (nh00 * (1 - nfx) * (1 - nfy) + nh10 * nfx * (1 - nfy)
                     + nh01 * (1 - nfx) * nfy + nh11 * nfx * nfy)
            dh = new_h - old_h  # negative going downhill

            # Record flow through the old cell (volume + momentum) for blending.
            d_track[yi, xi] += water
            mx_track[yi, xi] += water * dx
            my_track[yi, xi] += water * dy

            # Carrying capacity (droplet model). Erosion magnitude is kept
            # bounded like the classic kernel — the meandering comes from the
            # momentum *steering* above, not from amplifying the cut rate by
            # discharge (that feeds back into a runaway incision instability).
            cap = max(-dh, 0.0) * speed * water * capacity
            if cap < 1e-9:
                cap = 1e-9

            # Below the waterline we never carve channels — only lay down
            # sediment (building seabed, shoals and deltas). On land it's the
            # usual capacity rule: deposit when overloaded or climbing, else cut.
            underwater = old_h < sea_level
            if underwater or sediment > cap or dh > 0:
                if dh > 0:
                    drop = min(sediment, dh)
                elif underwater:
                    drop = sediment * deposition
                else:
                    drop = (sediment - cap) * deposition
                sediment -= drop
                h[yi, xi] += drop * (1 - fx) * (1 - fy)
                h[yi, xi + 1] += drop * fx * (1 - fy)
                h[yi + 1, xi] += drop * (1 - fx) * fy
                h[yi + 1, xi + 1] += drop * fx * fy
            else:
                take = min((cap - sediment) * erosion, -dh)
                _erode_radius(h, xi, yi, take, radius, res, sea_level)
                sediment += take

            speed = (max(0.0, speed * speed + dh * gravity)) ** 0.5
            water *= (1 - evaporation)
            px = nx
            py = ny
            if water < min_water:
                break

    return d_track, mx_track, my_track


def _momentum_numpy(h, iterations, strength, sea_level=-1e30):
    """Dependency-free fallback: discharge-weighted slope diffusion.

    Without numba we can't afford per-particle tracking, so we approximate the
    effect — accumulate a steady-state discharge by repeatedly routing each
    cell's water to its lowest neighbour, then carve proportionally to discharge
    so big channels cut deeper. No true momentum, hence no real meanders, but it
    keeps the module usable and the return shape identical. Cells below
    ``sea_level`` are never carved — only the redeposited material lands there."""
    res = h.shape[0]
    h = h.astype(np.float64).copy()
    discharge = np.ones((res, res), np.float64)
    for _ in range(max(0, iterations)):
        lowest = h.copy()
        flow = np.zeros_like(h)
        for axis, shift in ((0, 1), (0, -1), (1, 1), (1, -1)):
            neigh = np.roll(h, shift, axis=axis)
            down = np.maximum(0.0, h - neigh)
            flow += down
            lowest = np.minimum(lowest, neigh)
        discharge = 0.7 * discharge + 0.3 * (1.0 + flow * res)
        carve = (h - lowest) * strength * 0.08 * np.clip(discharge / discharge.mean(), 0.2, 4.0)
        carve[h < sea_level] = 0.0   # no incision below the waterline
        h -= carve
        h += np.roll(carve, 1, axis=0) * 0.25 + np.roll(carve, 1, axis=1) * 0.25
    # No directional momentum in the fallback.
    return {"height": h, "discharge": discharge,
            "momentum_x": np.zeros((res, res)), "momentum_y": np.zeros((res, res))}


def momentum_erode(height, iterations=60, particles=8000, seed=0,
                   backend="auto", strength=1.0, sea_level=-1e30,
                   progress_cb=None, **kwargs):
    """Momentum-coupled particle erosion.

    Args:
        height: input height field (any scale; output keeps the same scale).
        iterations: number of particle batches. Discharge / momentum maps are
            blended after each batch, so more batches = more developed channels.
        particles: particles per batch (numba path).
        seed: RNG seed.
        backend: "auto" | "numba" | "numpy".
        strength: overall erosion intensity multiplier (scales deposition_rate).
        kwargs: physics tuning — dt, density, evaporation, deposition_rate,
            momentum_transfer, discharge_alpha, friction, min_volume, max_steps,
            or (numpy fallback) iterations. Also accepts interleaved thermal
            smoothing (numba path): ``thermal_iterations`` (total talus passes to
            spread across the batches, default 0 = off), ``thermal_talus`` and
            ``thermal_factor``. Running thermal one step at a time between batches
            smooths each river bank as it is carved, rather than only before the
            first batch.

    Returns:
        dict with ``height``, ``discharge``, ``momentum_x``, ``momentum_y``.
    """
    use_numba = _HAS_NUMBA if backend == "auto" else (backend == "numba")
    if backend == "numba" and not _HAS_NUMBA:
        raise RuntimeError("numba backend requested but numba is not installed")
    strength = max(0.0, float(strength))

    if not use_numba:
        return _momentum_numpy(height, int(kwargs.get("iterations", iterations)),
                               0.5 * strength, sea_level=float(sea_level))

    p = dict(inertia=0.1, capacity=4.0, deposition=0.3, erosion=0.3,
             evaporation=0.02, gravity=10.0, momentum_transfer=0.8,
             discharge_alpha=0.4, min_water=1e-3, max_steps=128, radius=2)
    p.update({k: v for k, v in kwargs.items() if k in p})

    h = np.ascontiguousarray(height, dtype=np.float64).copy()
    res = h.shape[0]
    discharge = np.zeros((res, res), np.float64)
    momx = np.zeros((res, res), np.float64)
    momy = np.zeros((res, res), np.float64)
    alpha = float(p["discharge_alpha"])
    # Strength scales carrying capacity and cut rate (erosion clamped < 1).
    capacity = p["capacity"] * strength
    erosion = min(0.95, p["erosion"] * strength)

    base_seed = int(seed) & 0x7FFFFFFF
    total = max(1, int(iterations))

    # Interleaved thermal smoothing: spread ``thermal_iterations`` talus passes
    # evenly across the batches (≈ thermal_iterations / total per batch, carried
    # in a float accumulator) so each river bank is smoothed as it is carved
    # instead of only before the first batch. The total budget is conserved.
    thermal_iterations = int(kwargs.get("thermal_iterations", 0))
    thermal_talus = float(kwargs.get("thermal_talus", 0.004))
    thermal_factor = float(kwargs.get("thermal_factor", 0.5))
    thermal_per = thermal_iterations / total if thermal_iterations else 0.0
    thermal_acc = 0.0
    if thermal_iterations:
        from wbworldgen.terrain.erosion import _thermal_pass

    for it in range(total):
        d_track, mx_track, my_track = _momentum_batch(
            h, discharge, momx, momy, int(particles),
            (base_seed + it) & 0x7FFFFFFF,
            p["inertia"], capacity, p["deposition"], erosion,
            p["evaporation"], p["gravity"], p["momentum_transfer"],
            p["min_water"], int(p["max_steps"]), int(p["radius"]),
            float(sea_level))
        # Exponential blend of the new batch's flow into the stored maps.
        discharge = (1.0 - alpha) * discharge + alpha * d_track
        momx = (1.0 - alpha) * momx + alpha * mx_track
        momy = (1.0 - alpha) * momy + alpha * my_track
        if thermal_iterations:
            thermal_acc += thermal_per
            steps = round(thermal_acc) if it == total - 1 else int(thermal_acc)
            thermal_acc -= steps
            for _ in range(steps):
                _thermal_pass(h, thermal_talus, thermal_factor)
        # Work-in-progress hook: lets the pipeline stream the channels forming.
        if progress_cb is not None:
            progress_cb(it + 1, total, h)

    return {"height": h, "discharge": discharge,
            "momentum_x": momx, "momentum_y": momy}


if __name__ == "__main__":  # pragma: no cover - dev smoke test
    import time
    import importlib.util
    spec = importlib.util.spec_from_file_location("hm", "backend/engine/terrain/heightmap.py")
    hm = importlib.util.module_from_spec(spec); spec.loader.exec_module(hm)
    h0 = hm.base_heightmap(seed=7, res=256)
    print("numba available:", _HAS_NUMBA)
    t = time.time(); out = momentum_erode(h0, iterations=80, particles=6000, seed=1)
    print("momentum %.3fs" % (time.time() - t))
    print("finite:", np.isfinite(out["height"]).all(),
          "discharge>=0:", (out["discharge"] >= 0).all(),
          "max discharge:", round(float(out["discharge"].max()), 2))
