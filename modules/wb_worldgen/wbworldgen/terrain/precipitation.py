"""Wind-driven orographic precipitation.

The old moisture proxy was a Gaussian decay of distance-to-coast, which produced
unnatural concentric bands hugging the shoreline. This module replaces it with a
physically-motivated advected-moisture sweep (the approach common to modern
fantasy map generators):

    an air mass enters from the prevailing-wind edge carrying humidity, gains
    humidity over water (evaporation, stronger over warm seas), and drops rain as
    it is forced up over rising terrain (orographic lift). Humidity depletes as it
    rains, so the leeward side of a range is left in a dry rain shadow.

The result is a [0,1] precipitation field that ``derive.classify_whittaker`` uses
in place of the coast-distance moisture proxy.
"""

import numpy as np
from scipy.ndimage import gaussian_filter

from wbworldgen.terrain import heightmap as _hm


# Map a wind direction (degrees, meteorological: the direction the wind blows
# *from*) to the array transform that puts the upwind edge at row 0, so a single
# top-to-bottom sweep kernel handles all four cardinal cases. ``inverse`` undoes
# the transform on the result.
def _orient(arr: np.ndarray, wind_dir: float):
    d = wind_dir % 360.0
    if 315 <= d or d < 45:          # wind from North -> sweep top->bottom
        return arr, lambda a: a
    if 45 <= d < 135:               # wind from East -> sweep right->left
        return np.rot90(arr, k=1), lambda a: np.rot90(a, k=-1)
    if 135 <= d < 225:              # wind from South -> sweep bottom->top
        return np.flipud(arr), lambda a: np.flipud(a)
    return np.rot90(arr, k=-1), lambda a: np.rot90(a, k=1)  # from West


def precipitation_map(height: np.ndarray, water: np.ndarray, temp: np.ndarray,
                      sea_level: float = 0.4, wind_dir: float = 270.0,
                      humidity: float = 1.0, orographic: float = 1.0,
                      aridity: float = 0.0, seed: int = 0) -> np.ndarray:
    """Return a [0,1] precipitation field from an orographic moisture sweep.

    ``wind_dir`` is the prevailing wind in degrees (direction the wind blows
    *from*: 270 = westerly). ``humidity`` scales the moisture the air can carry
    (overall wetness). ``orographic`` scales how aggressively rising terrain
    wrings rain out (range contrast between windward and leeward). ``aridity``
    (0..1) skews the rank-normalized distribution drier so genuinely arid worlds
    are possible (see the redistribution note below).
    """
    h = height.astype(np.float64)
    oriented, inverse = _orient(h, wind_dir)
    w_or, _ = _orient(water.astype(bool), wind_dir)
    t_or, _ = _orient(temp.astype(np.float64), wind_dir)

    rows, cols = oriented.shape
    rain = np.zeros_like(oriented)
    # Air enters the upwind edge already moist.
    air = np.full(cols, humidity, dtype=np.float64)
    cap = max(1e-3, humidity)
    floor = 0.05 * humidity          # interiors never go bone-dry
    prev_elev = np.clip(oriented[0] - sea_level, 0.0, None)

    for r in range(rows):
        is_water = w_or[r]
        elev = np.clip(oriented[r] - sea_level, 0.0, None)

        # Over water: evaporate toward the cap, warm seas evaporate faster.
        evap = (0.10 + 0.20 * t_or[r]) * (cap - air)
        air = np.where(is_water, air + evap, air)

        # Over land: a base fraction always falls, plus orographic rain
        # proportional to the upslope gain along the wind direction. The base
        # fraction is kept low so air travels deep inland (interiors stay
        # semi-arid, not bone-dry desert) and the rain shadow is driven mostly
        # by the orographic term.
        d_elev = np.clip(elev - prev_elev, 0.0, None)
        fall = (0.012 + orographic * 8.0 * d_elev) * air
        fall = np.minimum(fall, np.clip(air - floor, 0.0, None))
        fall = np.where(is_water, 0.0, fall)

        air = air - fall
        rain[r] = np.where(is_water, air, fall)  # show carried humidity over sea
        prev_elev = elev

    precip = inverse(rain)
    precip = gaussian_filter(precip, sigma=2.5)

    # Break up any residual streaking along the wind lanes with multi-scale FBM,
    # mirroring the perturbation pattern used elsewhere in derive.py.
    noise = (_hm.fbm(h.shape[0], int(seed) + 6611, octaves=5, base_freq=5)
             + 0.5 * _hm.fbm(h.shape[0], int(seed) + 6612, octaves=4, base_freq=9))
    precip = precip + (noise / 1.5 - 0.5) * 0.20 * precip.std()

    # Rank-normalize (histogram equalization) over *land only* so the raw,
    # heavily right-skewed rain totals spread evenly across [0,1]. A plain
    # min-max stretch leaves the bulk of land bunched in the arid tail
    # (everything reads as desert); ranking preserves the windward-vs-leeward
    # *ordering* that matters visually while giving a balanced spread across the
    # moisture bands. (Same redistribution trick the heightmap uses for
    # elevation.) Water cells are irrelevant to biomes (overridden to ocean/ice)
    # so they get the wet end and are excluded from the land ranking.
    #
    # The flip side of a balanced spread is that ~25% of land sits in each
    # moisture band *no matter how arid the world is*, capping desert extent. The
    # ``aridity`` lever below reintroduces a controllable dry skew on top of the
    # ranked field so an intentionally arid planet is possible again.
    out = np.ones(h.shape, dtype=np.float64)
    land = ~water.astype(bool)
    vals = precip[land]
    if vals.size:
        order = np.argsort(vals, kind="stable")
        ranked = np.empty_like(vals)
        ranked[order] = np.linspace(0.0, 1.0, vals.size)
        # Ranking flattens the distribution to uniform [0,1], which would make
        # the overall ``humidity`` scalar a no-op. Re-apply it as a gamma curve:
        # humidity > 1 lifts the whole field wetter (more forest/rainforest),
        # < 1 pushes it drier (more grass/shrub/desert); == 1 is the identity.
        # ``aridity`` folds a dedicated dry skew into the same exponent (decoupled
        # from humidity): 0 keeps the balanced spread, toward 1 pushes most land
        # into the dry bands so deserts genuinely dominate.
        gamma = 1.0 / max(0.2, humidity)
        gamma *= (1.0 + 3.0 * float(np.clip(aridity, 0.0, 1.0)))
        out[land] = ranked ** gamma
    return out
