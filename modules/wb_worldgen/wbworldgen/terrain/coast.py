"""Coastal & submarine smoothing.

A late terrain-shaping pass that softens everything at and below the waterline:
the sea floor is blurred so it reads as a smooth shelf instead of jagged noise,
and low coastal land is eased into gentle slopes rather than sharp sea cliffs.
Deep inland terrain is left completely untouched.

The blend weight is driven by elevation relative to ``sea_level``: cells at or
below sea level are fully smoothed, fading out through a band of height
``width`` above the waterline back to the unmodified terrain.
"""

import numpy as np
from scipy.ndimage import gaussian_filter


def smooth_coast(height: np.ndarray, sea_level: float, *,
                 width: float = 0.08, strength: float = 1.0,
                 sigma: float = 2.5) -> np.ndarray:
    """Blur terrain under and near water, blended by depth below the coast.

    Parameters
    ----------
    height:
        2-D height field (expected roughly in [0, 1]).
    sea_level:
        Absolute height of the waterline (same convention as ``derive_layers``).
    width:
        Height band *above* sea level that still receives (fading) smoothing.
        Larger values pull the smoothing further inland onto low coastal plains.
    strength:
        Maximum blend weight (0..1) applied to fully-submerged cells. 0 disables.
    sigma:
        Gaussian blur radius in cells. Scaled with resolution by the caller so
        the effect looks the same at any map size.

    Returns
    -------
    A new height array; the input is not modified.
    """
    if strength <= 0.0 or sigma <= 0.0:
        return height

    blurred = gaussian_filter(height, sigma=sigma, mode="nearest")

    # Weight 1 at/below sea level, fading to 0 at sea_level + width. Underwater
    # cells clip to 1 so the whole sea floor is smoothed uniformly; a smoothstep
    # gives a soft, non-banded transition back onto dry land.
    band = max(1e-6, width)
    w = np.clip((sea_level + band - height) / band, 0.0, 1.0)
    w = w * w * (3.0 - 2.0 * w)
    w *= float(np.clip(strength, 0.0, 1.0))

    return height * (1.0 - w) + blurred * w
