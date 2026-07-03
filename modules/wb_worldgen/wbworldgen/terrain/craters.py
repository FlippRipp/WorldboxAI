"""Meteoric impact crater simulation.

``apply_craters`` stamps physically-plausible impact profiles onto a heightmap:

    bowl depression  +  raised rim (asymmetric Gaussian)  +  ejecta blanket
    +  central rebound peak (large craters only)

Profiles are added as signed height deltas so they interact correctly with any
existing terrain. Call before erosion for ancient craters (weathered rims,
filled ejecta); call after erosion but before lake resolution for fresh craters
(sharp rims, ejecta blanket intact, bowls may fill with water).
"""

import numpy as np


def _crater_delta(
    r_norm: np.ndarray,
    depth: float,
    rim_height: float,
    ejecta_falloff: float,
    central_peak: float = 0.0,
) -> np.ndarray:
    """Signed height delta at normalised distance *r_norm* = dist / radius.

    The rim uses an asymmetric Gaussian: steep inner wall (k=8), outer falloff
    controlled by *ejecta_falloff*.  Higher falloff = tighter ejecta blanket
    (fresh impact); lower = wide, eroded spread.
    """
    dr = r_norm - 1.0

    bowl = np.where(r_norm < 1.0, -depth * (1.0 - r_norm ** 2), 0.0)

    # Asymmetric Gaussian rim: tight on the interior, user-set on the exterior
    k = np.where(dr < 0.0, 8.0, ejecta_falloff)
    rim = rim_height * np.exp(-k * dr ** 2)

    peak = (central_peak * np.exp(-12.0 * r_norm ** 2)) if central_peak > 0.0 else 0.0

    return bowl + rim + peak


def apply_craters(
    height: np.ndarray,
    *,
    count: int = 5,
    min_radius: float = 0.02,
    max_radius: float = 0.08,
    depth: float = 0.15,
    rim_height: float = 0.05,
    ejecta_falloff: float = 3.0,
    seed: int = 0,
) -> np.ndarray:
    """Stamp *count* impact craters onto *height* and return the modified copy.

    Parameters
    ----------
    height:
        2-D float array in [0, 1].
    count:
        Number of craters.  Craters can overlap; later craters overwrite earlier
        ones where deltas conflict (additive, then clipped to [0, 1]).
    min_radius / max_radius:
        Crater radius bounds as a fraction of ``min(H, W)``.  E.g. 0.05 = 5 %
        of the shorter dimension.
    depth:
        Bowl depth in height units (0–1).  The centre of the crater is depressed
        by at most this amount relative to its rim.
    rim_height:
        Maximum raised-rim height in height units.  The rim sits at ``r == R``.
    ejecta_falloff:
        Controls how quickly the ejecta blanket thins beyond the rim.  Higher
        values produce a tight blanket (fresh impact); lower values spread it
        wide (ancient, eroded).  Typical range 1 – 8.
    seed:
        RNG seed for placement and sizing.
    """
    rng = np.random.default_rng(seed)
    H, W = height.shape
    size = min(H, W)

    yy, xx = np.mgrid[0:H, 0:W]
    result = height.copy()

    # Sort radii largest-first so small craters punch through large ejecta.
    radii_frac = np.sort(rng.uniform(min_radius, max_radius, count))[::-1]

    for r_frac in radii_frac:
        radius = max(4.0, r_frac * size)

        # Place craters with a small inset so the rim stays on the map.
        margin_x = radius / W
        margin_y = radius / H
        cx = rng.uniform(margin_x, 1.0 - margin_x) * W
        cy = rng.uniform(margin_y, 1.0 - margin_y) * H

        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        r_norm = dist / radius

        # Central rebound peak appears in larger craters (complex craters).
        central_peak = 0.35 * depth if r_frac > 0.06 else 0.0

        # Only compute within 4 × radius (ejecta is negligible beyond that).
        mask = r_norm < 4.0
        delta = np.zeros_like(result)
        delta[mask] = _crater_delta(
            r_norm[mask], depth, rim_height, ejecta_falloff, central_peak
        )
        result += delta

    return np.clip(result, 0.0, 1.0)
