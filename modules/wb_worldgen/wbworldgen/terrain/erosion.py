"""Erosion: thermal (vectorized numpy) + hydraulic (numba droplet / numpy grid).

Two public functions:

- :func:`thermal_erode` — talus-angle smoothing. Where a slope to a neighbour
  exceeds the talus threshold, material slides downhill. Fully vectorized.

- :func:`hydraulic_erode` — the dendritic-valley carver. Behind one signature
  there are two backends selected by ``_HAS_NUMBA``:
    * numba: the classic droplet model (Beyer / Lague). A droplet flows
      downhill, picking up sediment on steep descents and depositing it in
      pits, carving valleys. ``@njit`` makes the per-droplet loop near-C.
    * numpy fallback: a grid-based approximation that nudges each cell's height
      toward the average of its downhill neighbours, scaled by slope. Cruder,
      no droplets, but vectorized and dependency-free.

Force the fallback for testing by setting ``erosion._HAS_NUMBA = False`` before
calling, or pass ``backend="numpy"``.
"""

import numpy as np

try:
    from numba import njit, prange
    _HAS_NUMBA = True
except ImportError:  # pragma: no cover - exercised only without numba
    _HAS_NUMBA = False

    def njit(*args, **kwargs):  # no-op decorator so module still imports
        def wrap(fn):
            return fn
        return wrap if args and callable(args[0]) is False else (args[0] if args else wrap)

    def prange(*a):  # type: ignore
        return range(*a)


# --------------------------------------------------------------------------
# Thermal erosion (vectorized)
# --------------------------------------------------------------------------

def thermal_erode(height: np.ndarray, iterations: int = 20,
                  talus: float = 0.004, factor: float = 0.5,
                  progress_cb=None) -> np.ndarray:
    """Smooth slopes steeper than ``talus`` by moving material downhill.

    Each iteration computes the height difference to the 4 neighbours and moves
    a ``factor`` fraction of the excess (above talus) from high to low cells.

    ``progress_cb(done, total, height)`` is called after each iteration so a
    caller can stream the smoothing in progress.
    """
    h = height.astype(np.float64).copy()
    total = max(0, iterations)
    for i in range(total):
        _thermal_pass(h, talus, factor)
        if progress_cb is not None:
            progress_cb(i + 1, total, h)
    return h


def _thermal_pass(h: np.ndarray, talus: float, factor: float) -> np.ndarray:
    """One talus-diffusion pass, mutating ``h`` in place (and returning it).

    Computes the height difference to the 4 neighbours and moves a ``factor``
    fraction of the excess (above ``talus``) from high to low cells. Factored out
    of :func:`thermal_erode` so a single pass can be interleaved one step at a
    time with another erosion loop (see :mod:`momentum_erosion`)."""
    delta = np.zeros_like(h)
    for axis, shift in ((0, 1), (0, -1), (1, 1), (1, -1)):
        neigh = np.roll(h, shift, axis=axis)
        diff = h - neigh
        excess = np.maximum(0.0, diff - talus)
        move = excess * factor * 0.25  # split across 4 neighbours
        delta -= move
        delta += np.roll(move, -shift, axis=axis)
    h += delta
    return h


# --------------------------------------------------------------------------
# Hydraulic erosion — numba droplet kernel
# --------------------------------------------------------------------------

@njit(cache=True)
def _droplet_kernel(h, num_droplets, seed, inertia, capacity, deposition,
                    erosion, evaporation, gravity, max_steps, radius, sea_level):
    res = h.shape[0]
    np.random.seed(seed)
    for _ in range(num_droplets):
        # Random start position (float coords).
        px = np.random.random() * (res - 1)
        py = np.random.random() * (res - 1)
        dx = 0.0
        dy = 0.0
        speed = 1.0
        water = 1.0
        sediment = 0.0

        for _step in range(max_steps):
            xi = int(px)
            yi = int(py)
            if xi < 0 or xi >= res - 1 or yi < 0 or yi >= res - 1:
                break
            fx = px - xi
            fy = py - yi

            # Heights of the cell corners.
            h00 = h[yi, xi]
            h10 = h[yi, xi + 1]
            h01 = h[yi + 1, xi]
            h11 = h[yi + 1, xi + 1]

            # Bilinear gradient.
            gx = (h10 - h00) * (1 - fy) + (h11 - h01) * fy
            gy = (h01 - h00) * (1 - fx) + (h11 - h10) * fx

            # Update direction with inertia.
            dx = dx * inertia - gx * (1 - inertia)
            dy = dy * inertia - gy * (1 - inertia)
            mag = (dx * dx + dy * dy) ** 0.5
            if mag < 1e-9:
                # No clear downhill: nudge randomly to avoid getting stuck.
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
            if nxi < 0 or nxi >= res - 1 or nyi < 0 or nyi >= res - 1:
                break

            # Height at old and new positions (bilinear).
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

            # Sediment carrying capacity (more when steep + fast + much water).
            cap = max(-dh, 0.0) * speed * water * capacity
            if cap < 1e-9:
                cap = 1e-9

            # Below the waterline we never carve — only deposit sediment, so
            # underwater terrain is built up (deltas/shoals), never incised.
            underwater = old_h < sea_level
            if underwater or sediment > cap or dh > 0:
                # Deposit: if going uphill (dh>0) fill the pit, else drop excess.
                if dh > 0:
                    drop = min(sediment, dh)
                elif underwater:
                    drop = sediment * deposition
                else:
                    drop = (sediment - cap) * deposition
                sediment -= drop
                # Deposit at the four corners of the old cell (bilinear).
                h[yi, xi] += drop * (1 - fx) * (1 - fy)
                h[yi, xi + 1] += drop * fx * (1 - fy)
                h[yi + 1, xi] += drop * (1 - fx) * fy
                h[yi + 1, xi + 1] += drop * fx * fy
            else:
                # Erode: take sediment, but never more than the descent dh.
                take = min((cap - sediment) * erosion, -dh)
                # Distribute erosion over a small radius around the old cell.
                _erode_radius(h, xi, yi, take, radius, res)
                sediment += take

            speed = (max(0.0, speed * speed + dh * gravity)) ** 0.5
            water *= (1 - evaporation)
            px = nx
            py = ny
            if water < 1e-4:
                break
    return h


@njit(cache=True)
def _erode_radius(h, cx, cy, amount, radius, res):
    """Remove ``amount`` of material spread over a disc of given radius."""
    if radius <= 0:
        h[cy, cx] -= amount
        return
    total_w = 0.0
    for yy in range(cy - radius, cy + radius + 1):
        for xx in range(cx - radius, cx + radius + 1):
            if 0 <= xx < res and 0 <= yy < res:
                d2 = (xx - cx) ** 2 + (yy - cy) ** 2
                w = max(0.0, radius - d2 ** 0.5)
                total_w += w
    if total_w <= 0:
        h[cy, cx] -= amount
        return
    for yy in range(cy - radius, cy + radius + 1):
        for xx in range(cx - radius, cx + radius + 1):
            if 0 <= xx < res and 0 <= yy < res:
                d2 = (xx - cx) ** 2 + (yy - cy) ** 2
                w = max(0.0, radius - d2 ** 0.5)
                if w > 0:
                    h[yy, xx] -= amount * (w / total_w)


def _hydraulic_numpy(height: np.ndarray, iterations: int, strength: float,
                     sea_level: float = -1e30) -> np.ndarray:
    """Grid-based hydraulic fallback (no droplets).

    Crude approximation: repeatedly move a slope-scaled fraction of each cell's
    height toward its lowest neighbour, carving channels along steepest descent.
    Cells below ``sea_level`` are never carved — only redeposited onto.
    """
    h = height.astype(np.float64).copy()
    for _ in range(max(0, iterations)):
        lowest = h.copy()
        flow = np.zeros_like(h)
        for axis, shift in ((0, 1), (0, -1), (1, 1), (1, -1)):
            neigh = np.roll(h, shift, axis=axis)
            diff = h - neigh
            down = np.maximum(0.0, diff)
            flow += down
            lowest = np.minimum(lowest, neigh)
        # Carve proportional to local relief (how far above the lowest neighbour).
        carve = (h - lowest) * strength * 0.1
        carve[h < sea_level] = 0.0   # no incision below the waterline
        h -= carve
        # Deposit a fraction of carved material smoothed back (mass-ish balance).
        h += np.roll(carve, 1, axis=0) * 0.25 + np.roll(carve, 1, axis=1) * 0.25
    return h


def hydraulic_erode(height: np.ndarray, droplets: int = 60000, seed: int = 0,
                    backend: str = "auto", strength: float = 1.0,
                    sea_level: float = -1e30, **kwargs) -> np.ndarray:
    """Hydraulic erosion. Uses the numba droplet kernel when available.

    Args:
        height: input height field (any scale; output keeps the same scale).
        droplets: number of droplets (numba path) — ignored by numpy fallback.
        seed: RNG seed for droplet starts.
        backend: "auto" | "numba" | "numpy".
        strength: overall erosion intensity multiplier. Scales how much
            sediment each droplet can carry (capacity) and how aggressively it
            cuts (erosion rate), so higher = deeper, more pronounced valleys.
        kwargs: droplet tuning (inertia, capacity, deposition, erosion,
            evaporation, gravity, max_steps, radius) or numpy fallback
            (iterations).
    """
    use_numba = _HAS_NUMBA if backend == "auto" else (backend == "numba")
    if backend == "numba" and not _HAS_NUMBA:
        raise RuntimeError("numba backend requested but numba is not installed")
    strength = max(0.0, float(strength))

    if use_numba:
        h = np.ascontiguousarray(height, dtype=np.float64)
        params = dict(inertia=0.05, capacity=4.0, deposition=0.3, erosion=0.3,
                      evaporation=0.02, gravity=10.0, max_steps=64, radius=2)
        params.update({k: v for k, v in kwargs.items() if k in params})
        # Strength scales carrying capacity and cut rate (erosion clamped <1).
        capacity = params["capacity"] * strength
        erosion = min(0.95, params["erosion"] * strength)
        return _droplet_kernel(h.copy(), int(droplets), int(seed) & 0x7FFFFFFF,
                               params["inertia"], capacity,
                               params["deposition"], erosion,
                               params["evaporation"], params["gravity"],
                               int(params["max_steps"]), int(params["radius"]),
                               float(sea_level))

    iterations = int(kwargs.get("iterations", 40))
    return _hydraulic_numpy(height, iterations, 0.5 * strength, float(sea_level))


if __name__ == "__main__":  # pragma: no cover - dev smoke test
    import time
    import importlib.util
    spec = importlib.util.spec_from_file_location("hm", "backend/engine/terrain/heightmap.py")
    hm = importlib.util.module_from_spec(spec); spec.loader.exec_module(hm)
    h0 = hm.base_heightmap(seed=7, res=512)
    print("numba available:", _HAS_NUMBA)
    t = time.time(); h1 = thermal_erode(h0, iterations=20); print("thermal %.3fs" % (time.time() - t))
    t = time.time(); h2 = hydraulic_erode(h1, droplets=80000, seed=1); print("hydraulic(numba) %.3fs" % (time.time() - t))
    t = time.time(); h3 = hydraulic_erode(h1, backend="numpy"); print("hydraulic(numpy) %.3fs" % (time.time() - t))
    print("finite:", np.isfinite(h2).all(), np.isfinite(h3).all())
