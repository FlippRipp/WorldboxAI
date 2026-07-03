"""Render height/biome arrays to PNG bytes (top-down 2D).

Uses Pillow. Hillshade is a numpy normal-dot-light computation; the biome image
maps biome ids to colors and darkens by hillshade for relief.
"""

import io

import numpy as np

from wbworldgen.terrain import biomes as _bm
from wbworldgen.terrain import heightmap as _hm

RIVER_COLOR = np.array([60, 120, 200], dtype=np.float64)
# Lake colour ramp: shallow shore -> deep centre. Kept in the same blue family as
# the river and ocean (see _OCEAN_RAMP) so inland water reads as one cohesive
# palette rather than a separate teal; the shallow margin is just a lighter,
# slightly desaturated version of the river blue, not a green-tinted teal.
LAKE_SHALLOW = np.array([45, 95, 160], dtype=np.float64)
LAKE_DEEP = np.array([12, 42, 95], dtype=np.float64)
LAKE_COLOR = np.array([28, 72, 140], dtype=np.float64)  # fallback (no depth)
# River-field ramp (momentum model): same blue family as the lakes but lifted a
# notch lighter so flowing water reads as distinct from still lake water.
RIVER_SHALLOW = np.array([70, 130, 200], dtype=np.float64)
RIVER_DEEP = np.array([30, 75, 150], dtype=np.float64)


def _paint_lakes(rgb, lake_mask, shade, lake_depth):
    """Tint lake cells by water depth so they read as basins, not flat discs.

    Falls back to a single flat colour when no depth field is supplied."""
    if lake_mask is None:
        return rgb
    if lake_depth is not None:
        d = lake_depth[lake_mask]
        # Normalize depth per-render; sqrt makes shallow margins read clearly.
        dmax = float(d.max()) if d.size else 0.0
        t = np.sqrt(np.clip(d / dmax, 0.0, 1.0)) if dmax > 1e-9 else np.zeros_like(d)
        col = LAKE_SHALLOW * (1.0 - t)[:, None] + LAKE_DEEP * t[:, None]
        rgb[lake_mask] = col * (0.8 + 0.3 * shade[lake_mask])[:, None]
    else:
        rgb[lake_mask] = LAKE_COLOR * (0.8 + 0.3 * shade[lake_mask])[:, None]
    return rgb


def overlay_rivers(rgb: np.ndarray, paths, order: np.ndarray,
                   scale: int = 4, land_mask: np.ndarray = None) -> np.ndarray:
    """Paint smooth, anti-aliased, sub-pixel-thin rivers onto an RGB image.

    ``paths`` are pre-smoothed + meandered polylines in cell coordinates; width
    is set by **Horton-Strahler order** (order-1 headwater creeks are hairlines,
    high-order trunks are wider). Each path is stroked with rounded joints on a
    ``scale``x supersampled canvas, then box-downsampled — so a 1 super-pixel
    stroke becomes ~1/scale of a native pixel (thin) with anti-aliased edges.
    """
    if not paths or order is None:
        return rgb
    from PIL import Image, ImageDraw

    res = rgb.shape[0]
    # Adaptive supersampling: cap the canvas (~4096px) so high-res maps don't
    # allocate a giant buffer. Low-res maps still get the full 4x for AA.
    scale = max(1, min(scale, 4096 // res))
    H = res * scale
    canvas = Image.new("L", (H, H), 0)
    draw = ImageDraw.Draw(canvas)
    res1 = res - 1

    for p in paths:
        if len(p) < 2:
            continue
        # Strahler order at the downstream end sets the path's channel width.
        ex = min(res1, max(0, int(p[-1, 0])))
        ey = min(res1, max(0, int(p[-1, 1])))
        o = int(order[ey, ex])
        w_native = 0.3 + 0.4 * max(0, o - 1)   # order1 thin -> wider per order
        w_hi = max(1, int(round(w_native * scale)))
        pts = [(float(x) * scale, float(y) * scale) for x, y in p]
        draw.line(pts, fill=255, width=w_hi, joint="curve")

    # Box-average downsample -> fractional coverage (anti-aliasing). A thin
    # stroke covers only ~1/scale of a cell, so a gamma keeps it visible while
    # preserving the soft edge (width unchanged).
    alpha = (np.asarray(canvas, dtype=np.float64).reshape(res, scale, res, scale)
             .mean(axis=(1, 3)) / 255.0)
    alpha = np.clip(alpha ** 0.6, 0.0, 1.0)
    # Clip rivers to land: don't paint over ocean or lakes (paths run to the
    # mouth and through basins, but the water bodies should show through).
    if land_mask is not None:
        alpha = alpha * land_mask
    alpha = alpha[..., None]
    return rgb * (1.0 - alpha) + RIVER_COLOR * alpha

def overlay_river_field(rgb: np.ndarray, field: np.ndarray, land_mask=None,
                        density: float = 0.5, shade: np.ndarray = None) -> np.ndarray:
    """Paint rivers as a smooth, connected *water field* (SimpleHydrology-style).

    ``field`` is a normalized [0,1] flow magnitude (see ``build_rivers``'
    ``river_field``): high on trunks, fading to zero on headwaters. Rather than
    stroking vector polylines, we map flow to opacity so wide rivers read solid
    and tributaries taper off — naturally continuous, never ending mid-land.

    ``density`` lowers the visibility threshold (more creeks show). The alpha is
    Gaussian-blurred for anti-aliased edges, so the result is soft, not spotty.
    """
    if field is None:
        return rgb
    from scipy.ndimage import gaussian_filter

    # The field is already channelized (sheet flow sits at 0), so use a small
    # fixed floor just to drop the faintest fringe; density is applied upstream.
    t0 = 0.05
    # Opacity ramps from the threshold up; gamma keeps thin streams visible
    # while letting trunks reach full strength.
    alpha = np.clip((field - t0) / max(1e-6, 1.0 - t0), 0.0, 1.0) ** 0.7
    alpha = gaussian_filter(alpha, sigma=0.6)
    if land_mask is not None:
        alpha = alpha * land_mask

    # Tint by flow: small streams a lighter shallow blue, big trunks deep blue.
    t = np.clip(field, 0.0, 1.0)[..., None]
    color = RIVER_SHALLOW * (1.0 - t) + RIVER_DEEP * t
    if shade is not None:
        color = color * (0.75 + 0.4 * shade[..., None])

    a = alpha[..., None]
    return rgb * (1.0 - a) + color * a


def _overlay_water(rgb, river_field, paths, order, land_mask, density, shade):
    """Pick the river renderer: the continuous water field when available
    (momentum model), otherwise the vector-polyline strokes (droplet model)."""
    if river_field is not None:
        return overlay_river_field(rgb, river_field, land_mask=land_mask,
                                   density=density, shade=shade)
    return overlay_rivers(rgb, paths, order, land_mask=land_mask)


# RGB per biome id, built from the active palette (see biomes.py).
BIOME_COLORS = _bm.biome_colors("realistic")


# Canopy palette accents per tree-style: forests are tinted toward these and
# darkened a touch so woodland reads as denser/cooler than open biomes. Keyed by
# the ``tree`` style in biomes.py (None-style biomes get no canopy treatment).
# How strongly forest cells are pulled toward their canopy tint (scaled by the
# forest-density ``strength``). Higher = greener, deeper woodland; the climate
# blend lightens the forest base by mixing in grassland/savanna, so this pull is
# what restores saturated forest green.
_CANOPY_PULL = 0.5

_CANOPY_TINT = {
    "conifer":   (38, 84, 58),
    "broadleaf": (46, 108, 52),
    "jungle":    (32, 100, 44),
    "scrub":     (120, 130, 82),
    "enchanted": (60, 130, 140),
    "fungal":    (120, 80, 135),
}


def _forest_mask(biome: np.ndarray, mode: str) -> np.ndarray:
    """Boolean mask of forest-flagged biome cells for the active palette."""
    styles = _bm.tree_styles(mode)
    mask = np.zeros(biome.shape, dtype=bool)
    for bid in styles:
        mask |= (biome == bid)
    return mask


def apply_canopy_texture(rgb: np.ndarray, biome: np.ndarray, seed: int = 0,
                         strength: float = 0.5, mode: str = "realistic",
                         weight: np.ndarray = None) -> np.ndarray:
    """Render forests as a soft canopy *texture* (no individual trees).

    Forest cells are pulled toward a per-style canopy tint and mottled with a
    higher-frequency noise field, so woodland reads as a textured, slightly
    darker region rather than a flat color band. ``strength`` (0..1) scales both
    the tint pull and the mottle contrast; an optional per-cell ``weight`` (0..1)
    further fades the effect (used to suppress canopy high on mountains so the
    rocky elevation tint wins there).

    The per-style coverage is feathered with a light blur so the canopy green
    spreads softly into the ecotone around each forest rather than stopping on a
    hard cell boundary. This both restores the lush green that the climate blend
    otherwise dilutes (forest colour mixing with lighter grassland/savanna) and
    keeps wooded edges smooth.
    """
    from scipy.ndimage import gaussian_filter

    styles = _bm.tree_styles(mode)
    if not styles or strength <= 0:
        return rgb
    res = rgb.shape[0]
    # One mottle field reused for every forest type (cheap; visually fine).
    mottle = _hm.fbm(res, int(seed) + 0x7A, octaves=5, base_freq=max(8, res // 24))
    mottle = (mottle - 0.5)  # ~[-0.5, 0.5]
    w = 1.0 if weight is None else weight
    feather = max(0.8, res / 180.0)
    out = rgb.astype(np.float64)
    for bid, style_key in styles.items():
        mask = biome == bid
        if not mask.any():
            continue
        tint = np.array(_CANOPY_TINT.get(style_key, (52, 100, 56)), dtype=np.float64)
        # Feathered coverage: full inside the forest, tapering across its edge so
        # the green pull reaches into the surrounding ecotone (a soft halo) rather
        # than ending on a hard cell boundary.
        cov = np.maximum(mask.astype(np.float64),
                         gaussian_filter(mask.astype(np.float64), feather))
        cw = (_CANOPY_PULL * strength) * cov * w   # tint-pull weight (feathered)
        cw3 = cw[..., None]
        pulled = out * (1.0 - cw3) + tint * cw3
        bright = (1.0 + (0.45 * strength) * mottle * cov * w)[..., None]
        out = pulled * bright
    return out


def _biome_variation(shape, seed: int) -> np.ndarray:
    """Low-frequency [~0.92, 1.08] brightness field to break up flat biome fills."""
    res = shape[0]
    f = _hm.fbm(res, int(seed) + 0x2C, octaves=3, base_freq=4)
    return 0.92 + 0.16 * f


def _soften_biome_hue(base: np.ndarray, land: np.ndarray, strength: float,
                      seed: int) -> np.ndarray:
    """Aggressively blend the biome *hue* field: noise-warp the colors for ragged
    organic borders, then blur heavily. ``strength`` (0..1) scales both the warp
    amplitude and the blur radius. Restricted to land so coastlines stay crisp.
    """
    from scipy.ndimage import gaussian_filter

    res = base.shape[0]
    soft = base
    # Domain-warp the colour lookup (same idea as heightmap.base_heightmap): bend
    # sample positions by two low-freq noise fields so borders wander/interlock.
    # Where a warped sample lands on ocean, fall back to the original land colour
    # so the sea never bleeds inland.
    amp = strength * res * 0.05
    if amp > 0.5:
        wx = _hm.fbm(res, int(seed) + 0x5A1, octaves=4) - 0.5
        wy = _hm.fbm(res, int(seed) + 0x5A2, octaves=4) - 0.5
        coords = np.arange(res)
        gx, gy = np.meshgrid(coords, coords)
        sx = np.clip((gx + wx * amp).astype(int), 0, res - 1)
        sy = np.clip((gy + wy * amp).astype(int), 0, res - 1)
        sampled = soft[sy, sx]
        soft = np.where(land[sy, sx][..., None], sampled, base)
    # Heavy blur so regions melt into one another — land-normalized so only land
    # contributes (no ocean colour averaged into coastal land).
    sigma = max(0.8, strength * res / 40.0)
    landf = land.astype(np.float64)
    w = gaussian_filter(landf, sigma)
    denom = np.maximum(w, 1e-6)
    soft = np.dstack([gaussian_filter(soft[..., c] * landf, sigma) / denom
                      for c in range(3)])
    return np.where(land[..., None], soft, base)


def _soft_band_weights(values: np.ndarray, edges, half_width: float) -> list:
    """Soft membership weights over the bands separated by ``edges``.

    For N internal ``edges`` (so N+1 bands) returns N+1 [0,1] weight arrays that
    form a partition of unity: each boundary is a ``smoothstep`` ramp of
    half-width ``half_width`` instead of a hard cut, so a cell sitting near a
    threshold splits its membership between the two adjacent bands. Widening
    ``half_width`` widens the transition (and thus the rendered gradient)."""
    hw = max(1e-4, half_width)
    # Rising ramp across each boundary: r[i] ~ 0 below edge i, ~1 above it, so r[i]
    # is the soft fraction of the value that lies *above* edge i.
    ramps = []
    for e in edges:
        t = np.clip((values - (e - hw)) / (2.0 * hw), 0.0, 1.0)
        ramps.append(t * t * (3.0 - 2.0 * t))   # smoothstep
    # Band weights are the gaps between successive ramps. The lowest band holds the
    # mass below the first edge (1 - r[0]); each middle band holds r[i-1] - r[i];
    # the highest band holds r[-1]. (Getting this order wrong inverts the axis —
    # wettest cells would render with the driest band's colour.)
    weights = [1.0 - ramps[0]]
    for i in range(1, len(ramps)):
        weights.append(ramps[i - 1] - ramps[i])
    weights.append(ramps[-1])
    return weights


def _climate_grid_base(temp: np.ndarray, moisture: np.ndarray, mode: str,
                       blend: float, colors: np.ndarray = None) -> np.ndarray:
    """Interpolate the Whittaker colour grid by each cell's temp/moisture position
    (soft band weights), giving climate-space gradient ecotones. ``blend`` scales
    the band half-width. ``colors`` is the resolved (possibly overridden) palette
    array; falls back to the default palette when not supplied. Returns the raw
    climate colour before any spatial cap or special-biome compositing."""
    if colors is None:
        colors = _bm.biome_colors(mode)
    color_matrix = colors[np.array(_bm.WHITTAKER_GRID)]  # (4 temp, 4 moisture, 3)

    hw = 0.02 + 0.10 * float(np.clip(blend, 0.0, 1.0))
    tw = np.stack(_soft_band_weights(temp, _bm.TEMP_BANDS, hw), axis=-1)        # (H,W,4)
    mw = np.stack(_soft_band_weights(moisture, _bm.MOISTURE_BANDS, hw), axis=-1)  # (H,W,4)
    # Clamp >=0 and renormalize so weights are a clean partition of unity even if
    # neighbouring smoothstep ramps overlap at large half-widths.
    tw = np.clip(tw, 0.0, None); tw /= np.maximum(tw.sum(-1, keepdims=True), 1e-6)
    mw = np.clip(mw, 0.0, None); mw /= np.maximum(mw.sum(-1, keepdims=True), 1e-6)
    # base[y,x] = sum_t sum_m tw[y,x,t] * mw[y,x,m] * color_matrix[t,m]
    return np.einsum("yxt,yxm,tmc->yxc", tw, mw, color_matrix)


def _spatial_cap(base: np.ndarray, blendable: np.ndarray, sigma: float) -> np.ndarray:
    """Cap the *minimum* transition width in pixels with a light land-normalized
    blur. The climate blend gives gradient widths in *climate* units, so where
    temp/moisture change rapidly (coasts, river corridors) a boundary still
    compresses into a few pixels and reads as a hard seam; this smooths those
    without widening the broad gradients. Normalized over ``blendable`` so the
    coastline (composited later) stays crisp and ocean colour never bleeds inland."""
    from scipy.ndimage import gaussian_filter
    if sigma <= 0:
        return base
    bf = blendable.astype(np.float64)
    denom = np.maximum(gaussian_filter(bf, sigma), 1e-6)
    smoothed = np.dstack([gaussian_filter(base[..., c] * bf, sigma) / denom
                          for c in range(3)])
    return np.where(blendable[..., None], smoothed, base)


def _climate_blend_base(temp: np.ndarray, moisture: np.ndarray,
                        biome: np.ndarray, mode: str, blend: float,
                        seed: int, colors: np.ndarray = None) -> np.ndarray:
    """Smooth climate-space biome colour: interpolate across the Whittaker grid
    by each cell's temperature/moisture position instead of snapping to one id,
    then cap any residual pixel-scale seams with a light spatial blur.

    Transition width tracks the climate gradient (wide where climate changes
    slowly, narrow where it changes fast), giving gradient ecotones rather than
    crisp seams. ``blend`` (0..1) scales the band half-width. Non-climate special
    biomes (beach/snow/ice/rock + fantasy overlays) aren't on the grid, so they're
    composited back over the blended base with softened edges.
    """
    from scipy.ndimage import gaussian_filter

    if colors is None:
        colors = _bm.biome_colors(mode)
    base = _climate_grid_base(temp, moisture, mode, blend, colors)

    # Cap residual pixel-scale sharpness (steep-climate-gradient seams) before
    # compositing the crisp specials/coastline.
    blendable = ~((biome == _bm.OCEAN) | (biome == _bm.ICE))
    base = _spatial_cap(base, blendable, 0.6 + 1.4 * float(np.clip(blend, 0.0, 1.0)))

    # Composite special (non-climate) biomes back on top. Snow/beach get a softly
    # blurred alpha so their edges blend rather than snapping back to hard lines;
    # rock and the fantasy overlays read as crisper patches.
    # Soft-composited land specials: feather their edges so they don't reintroduce
    # the crisp seams the climate blend just removed. Snow caps, beaches, scattered
    # steep rock, and stamped fantasy patches all read better with organic borders.
    soft_ids = (_bm.SNOW, _bm.BEACH, _bm.ROCK, _bm.CORRUPTION, _bm.ARCANE_GROVE)
    # Water bodies stay hard so coastlines / ice margins remain crisp; their
    # discrete colour also feeds the downstream water tint correctly.
    hard_ids = (_bm.OCEAN, _bm.ICE)
    table = _bm.biome_table(mode)
    sigma = 0.6 + 1.2 * float(np.clip(blend, 0.0, 1.0))
    for bid in soft_ids + hard_ids:
        if bid not in table:
            continue
        mask = (biome == bid)
        if not mask.any():
            continue
        a = mask.astype(np.float64)
        if bid in soft_ids:
            # Keep full colour inside the cell (so a lone rock/snow cell stays
            # visible) but add a soft outward halo so the *edge* feathers instead
            # of snapping — max() of the hard mask and its blurred tail.
            a = np.maximum(a, gaussian_filter(a, sigma))
        base = base * (1.0 - a[..., None]) + np.array(colors[bid]) * a[..., None]
    return base


def mountain_exaggeration(mountain_strength: float) -> float:
    """Map the Mountain Height slider's 1..3 band to a 0..2 visual amplifier.

    The slider's 0..1 band drives the actual mountain *geometry* (see
    pipeline.generate_terrain); its 1..3 band adds no geometry — instead it
    returns here as a 0..2 factor that exaggerates how big mountains *read*
    (relief z-scale, hillshade contrast, rock/snow tinting and snow line).
    """
    return float(np.clip(mountain_strength - 1.0, 0.0, 2.0))


def hillshade(height: np.ndarray, azimuth: float = 315.0, altitude: float = 45.0,
              z_scale: float = 12.0, contrast: float = 1.5) -> np.ndarray:
    """Return a [0,1] shaded-relief array.

    ``z_scale`` is vertical exaggeration — higher makes slopes read more
    strongly. ``contrast`` expands the result around its midpoint so highlights
    brighten and shadows deepen for punchier relief.
    """
    gy, gx = np.gradient(height * z_scale)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    shaded = (np.sin(alt) * np.sin(slope)
              + np.cos(alt) * np.cos(slope) * np.cos(az - aspect))
    # Expand contrast around the mid-grey neutral so relief pops.
    shaded = 0.5 + (shaded - 0.5) * contrast
    return np.clip(shaded, 0.0, 1.0)


def _encode_png(rgb: np.ndarray) -> bytes:
    from PIL import Image
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def hillshade_png(height: np.ndarray, sea_level: float = 0.4,
                  order=None, paths=None, lake_mask=None, lake_depth=None,
                  z_scale: float = 12.0, river_field=None, density: float = 0.5,
                  hillshade_strength: float = 1.5) -> bytes:
    """Grayscale relief with a blue tint below sea level."""
    shade = hillshade(height, z_scale=z_scale, contrast=hillshade_strength)
    g = (shade * 255.0)
    rgb = np.stack([g, g, g], axis=-1)
    water = height < sea_level
    # Tint water blue.
    rgb[water] = np.stack([g * 0.3, g * 0.5, np.clip(g + 60, 0, 255)], axis=-1)[water]
    rgb = _paint_lakes(rgb, lake_mask, shade, lake_depth)
    land_mask = ~water if lake_mask is None else (~water & ~lake_mask)
    rgb = _overlay_water(rgb, river_field, paths, order, land_mask, density, shade)
    return _encode_png(rgb)


# Hypsometric ramp stops as (fraction-of-range, RGB). Below ``sea_level`` the
# fraction is remapped into the ocean stops, above into the land stops, so the
# coastline always lands exactly at the sea-level contour.
_OCEAN_RAMP = np.array([
    [8, 30, 70],      # deep ocean
    [20, 70, 130],    # ocean
    [60, 130, 180],   # shallow
], dtype=np.float64)
_LAND_RAMP = np.array([
    [70, 140, 70],    # lowland
    [150, 180, 90],   # plains
    [180, 150, 90],   # hills
    [130, 100, 70],   # mountain
    [120, 110, 105],  # high rock
    [245, 245, 250],  # snow peak
], dtype=np.float64)

# How much of the hypsometric height ramp to blend into *land* biome colour in
# ``biome_png`` (0 = pure biome colour, 1 = pure elevation tint). A small value
# lets elevation read as a green->tan->brown->snow gradient layered over the
# biome hue while keeping the biome clearly dominant. Tune here.
_LAND_ELEVATION_TINT = 0.22


def _ramp_lookup(t: np.ndarray, ramp: np.ndarray) -> np.ndarray:
    """Linear-interpolate an [0,1] field through an (N,3) colour ramp."""
    n = len(ramp) - 1
    pos = np.clip(t, 0.0, 1.0) * n
    i = np.clip(np.floor(pos).astype(int), 0, n - 1)
    f = (pos - i)[..., None]
    return ramp[i] * (1 - f) + ramp[i + 1] * f


def _hypsometric_tint(height: np.ndarray, sea_level: float) -> np.ndarray:
    """Per-cell hypsometric color: ocean-depth blues below ``sea_level``, land
    greens/browns/snow above, with the coastline pinned at the sea-level contour."""
    water = height < sea_level
    land = ~water
    rgb = np.empty(height.shape + (3,), dtype=np.float64)
    # Ocean: depth 0..sea_level remapped to 0..1 through the ocean ramp.
    rgb[water] = _ramp_lookup(height / max(1e-6, sea_level), _OCEAN_RAMP)[water]
    # Land: sea_level..1 remapped to 0..1 through the land ramp.
    span = max(1e-6, 1.0 - sea_level)
    rgb[land] = _ramp_lookup((height - sea_level) / span, _LAND_RAMP)[land]
    return rgb


def elevation_png(height: np.ndarray, sea_level: float = 0.4,
                  order=None, paths=None, lake_mask=None, lake_depth=None,
                  z_scale: float = 12.0, river_field=None, density: float = 0.5,
                  hillshade_strength: float = 1.5) -> bytes:
    """Hypsometric tint (sea-depth blues -> land greens/browns -> snow),
    modulated by hillshade for relief."""
    water = height < sea_level
    land = ~water
    rgb = _hypsometric_tint(height, sea_level)
    # Shade for relief; keep water flatter so depth tint stays readable.
    shade = hillshade(height, z_scale=z_scale, contrast=hillshade_strength)
    mult = np.where(water, 0.8 + 0.3 * shade, 0.5 + 0.65 * shade)[..., None]
    rgb = rgb * mult
    rgb = _paint_lakes(rgb, lake_mask, shade, lake_depth)
    land_mask = land if lake_mask is None else (land & ~lake_mask)
    rgb = _overlay_water(rgb, river_field, paths, order, land_mask, density, shade)
    return _encode_png(rgb)


def biome_png(biome: np.ndarray, height: np.ndarray, sea_level: float = 0.4,
              order=None, paths=None, lake_mask=None, lake_depth=None,
              z_scale: float = 12.0, river_field=None, density: float = 0.5,
              hillshade_strength: float = 1.5, biome_mode: str = "realistic",
              forests: bool = True, forest_density: float = 0.5,
              biome_blend: float = 0.7,
              temperature: np.ndarray = None, moisture: np.ndarray = None,
              seed: int = 0, mountain_exaggeration: float = 0.0,
              rock_line: float = _bm._ROCK_LINE, snow_line: float = _bm._SNOW_LINE,
              alpine_blend: float = _bm._ALPINE_BLEND,
              color_overrides: dict = None) -> bytes:
    """Stylized-atlas biome render: biome colour, softened boundaries, within-biome
    variation, a soft forest canopy texture, and strong relief shading, with ocean
    depth shading. Lakes and rivers overlaid last.

    ``biome_blend`` (0..1) controls how aggressively neighbouring biomes melt
    together. When ``temperature`` and ``moisture`` are supplied, biome colours are
    blended in *climate space* (interpolated across the Whittaker grid by each
    cell's temp/moisture), so transition width tracks the climate gradient; without
    them it falls back to the legacy noise-warp + spatial blur of discrete colours.

    High-elevation bare rock and snow are applied here as a *height-based fade*
    (``biomes.alpine_cover``): land keeps its underlying climate biome colour low
    down and melts toward rock and then snow with rising altitude, instead of the
    classifier snapping peaks to a flat rock/snow class. Forest canopy is faded
    out under that rock/snow so trees don't poke through the summits."""
    water = biome == _bm.OCEAN
    if _bm.ICE in _bm.biome_table(biome_mode):
        water = water | (biome == _bm.ICE)
    land = ~water

    # Resolve the palette once (with any live colour-editor overrides) so every
    # render path below uses the same colours.
    colors = _bm.biome_colors(biome_mode, color_overrides)

    # 1. Establish the biome *hue* field (before relief/elevation) so borders can
    #    melt together hard without smearing the crisp mountains added later.
    if biome_blend > 0 and temperature is not None and moisture is not None:
        # Climate-space blend: gradient ecotones driven by the temp/moisture maps.
        base = _climate_blend_base(temperature, moisture, biome, biome_mode,
                                   biome_blend, seed, colors)
    else:
        # Legacy path: discrete colour lookup, then noise-warp + heavy blur. Keep
        # coastlines crisp by compositing the soft result over land only.
        base = colors[biome]
        if biome_blend > 0:
            base = _soften_biome_hue(base, land, biome_blend, seed)

    # 2. Hypsometric depth/height tint. Water takes a strong depth tint so deep
    #    ocean reads darker/bluer than the shallows. Land takes a *light*
    #    hypsometric tint (``_LAND_ELEVATION_TINT``) layered over its biome
    #    colour so elevation reads as a green->tan->brown gradient at a glance,
    #    while the biome hue stays dominant. (The high end is taken over by the
    #    rock/snow fade in step 3.)
    tint = _hypsometric_tint(height, sea_level)
    k = np.where(water, 0.42, _LAND_ELEVATION_TINT)[..., None]
    rgb = base * (1.0 - k) + tint * k

    # 3. Height-based rock -> snow fade over land: melt the biome colour toward
    #    bare rock on the high flanks and then toward snow at the peaks, so the
    #    map shows "whatever biome is there -> rock -> snow" with altitude rather
    #    than a flat rock/snow class. ``tree_weight`` then suppresses the forest
    #    canopy under that cover so trees don't poke through the summits.
    rock_w, snow_w = _bm.alpine_cover(height, sea_level, temperature,
                                      mountain_exaggeration, rock_line=rock_line,
                                      snow_line=snow_line, blend=alpine_blend)
    rock_w = np.where(land, rock_w, 0.0)
    snow_w = np.where(land, snow_w, 0.0)
    wr = rock_w[..., None]
    rgb = rgb * (1.0 - wr) + colors[_bm.ROCK] * wr
    ws = snow_w[..., None]
    rgb = rgb * (1.0 - ws) + colors[_bm.SNOW] * ws
    tree_weight = 1.0 - np.clip(rock_w + snow_w, 0.0, 1.0)

    # 4. Forest canopy texture (replaces per-tree stamps), faded out high on the
    #    mountains by ``tree_weight`` so rock/snow reads clean at altitude.
    if forests:
        rgb = apply_canopy_texture(rgb, biome, seed=seed, strength=forest_density,
                                   mode=biome_mode, weight=tree_weight)

    # 5. Break up flat fills with a gentle low-frequency brightness variation.
    rgb = rgb * _biome_variation(height.shape, seed)[..., None]

    # 6. Strong, water-aware relief shading. Land uses a wide multiplier range
    #    (~0.4..1.2) so slopes pop in 3-D — sunlit faces brighten and shadowed
    #    faces deepen — making high ground easy to read. Water stays flatter so
    #    its depth tint remains legible.
    shade = hillshade(height, z_scale=z_scale, contrast=hillshade_strength)
    mult = np.where(water, 0.8 + 0.3 * shade, 0.4 + 0.8 * shade)[..., None]
    rgb = rgb * mult

    rgb = _paint_lakes(rgb, lake_mask, shade, lake_depth)
    land_mask = land if lake_mask is None else (land & ~lake_mask)
    rgb = _overlay_water(rgb, river_field, paths, order, land_mask, density, shade)
    return _encode_png(rgb)


def cave_png(layers: dict, z_scale: float = 14.0,
             hillshade_strength: float = 1.6, seed: int = 0,
             terrace_steps: int = 6, ssao_strength: float = 0.6) -> bytes:
    """Top-down underground render: cave-biome colours over the floor, solid rock
    walls darkened, with subterranean pools and rivers overlaid.

    Unlike ``biome_png`` (hypsometric land/sea tints, snow, forest canopy) the
    cave palette is flat per-biome. Depth is hardened two ways so the floor reads
    as rugged carved stone rather than a soft bevel:

      * **Terracing** quantises the floor into ``terrace_steps`` geological strata
        and darkens the band boundaries into contour ledges (drop-offs/cliffs);
      * **2D SSAO** darkens the floor near walls and in tight crevices (distance
        to the nearest rock), so deep chasms read black and wide caverns bright.

    Expects the cave ``layers`` from
    :func:`wbworldgen.terrain.caves.generate_cave_terrain`.
    """
    from scipy import ndimage

    biome = np.asarray(layers["biome"]).astype(int)
    floor = np.asarray(layers["height"], dtype=np.float64)
    open_mask = np.asarray(layers["open"]).astype(bool)
    rock = np.asarray(layers["rock"]).astype(bool)
    pools = layers.get("lake_mask")
    lake_depth = layers.get("lake_depth")
    rivers = layers.get("river_mask")
    res = floor.shape[0]

    rgb = _bm.biome_colors("cave")[biome].astype(np.float64)

    # --- Terracing: quantise the floor into strata and shade the banded field so
    #     slopes read as a stack of distinct ledges instead of a smooth ramp.
    steps = max(1, int(terrace_steps))
    terraced = np.floor(np.clip(floor, 0.0, 0.9999) * steps) / steps
    shade = hillshade(terraced, z_scale=z_scale, contrast=hillshade_strength)

    # Contour ledges: cells where the stratum index changes from a neighbour.
    band = np.floor(np.clip(floor, 0.0, 0.9999) * steps).astype(np.int32)
    edge = np.zeros((res, res), dtype=bool)
    edge[:, 1:] |= band[:, 1:] != band[:, :-1]
    edge[1:, :] |= band[1:, :] != band[:-1, :]

    # --- 2D SSAO: distance from each floor cell to the nearest wall. Cells deep
    #     in open space stay bright; cells hugging walls / in crevices darken.
    dist = ndimage.distance_transform_edt(open_mask)
    ao_radius = max(2.0, res * 0.012)
    ao = np.clip(dist / ao_radius, 0.0, 1.0) ** 0.6
    s = float(np.clip(ssao_strength, 0.0, 1.0))
    ao_mult = (1.0 - s) + s * ao

    # Relief from the floor, but flatten it under rock so walls stay uniformly
    # dark (a rock=1.0 ceiling otherwise produces harsh cliff shading everywhere).
    mult = np.where(open_mask, 0.55 + 0.6 * shade, 0.9)
    mult = mult * np.where(open_mask, ao_mult, 1.0)
    mult = mult * np.where(open_mask & edge, 0.68, 1.0)  # darken contour ledges
    rgb = rgb * mult[..., None]
    # Gentle low-frequency variation so big caverns aren't flat fills.
    rgb = rgb * _biome_variation(floor.shape, seed)[..., None]
    # Keep rock walls dark and solid regardless of the above.
    rgb[rock] = np.array(_bm.biome_table("cave")[_bm.ROCK_WALL][1], dtype=np.float64)

    # Standing pools: depth-graded blue (reuse the lake painter).
    if pools is not None:
        rgb = _paint_lakes(rgb, np.asarray(pools).astype(bool), shade, lake_depth)
    # Flowing rivers: thin bright channels.
    if rivers is not None:
        rm = np.asarray(rivers).astype(bool)
        rgb[rm] = (RIVER_COLOR * (0.7 + 0.5 * shade[rm])[:, None])
    return _encode_png(rgb)


# Temperature debug ramp: cold (blues) -> temperate (greens) -> hot (reds).
_TEMP_RAMP = np.array([
    [40, 60, 140],    # frigid
    [60, 150, 200],   # cold
    [120, 200, 150],  # cool
    [220, 220, 120],  # warm
    [220, 140, 70],   # hot
    [190, 60, 50],    # torrid
], dtype=np.float64)


def temperature_png(temperature: np.ndarray, height: np.ndarray,
                    sea_level: float = 0.4, z_scale: float = 12.0,
                    hillshade_strength: float = 1.5) -> bytes:
    """Temperature field as a cold->hot ramp, shaded for relief."""
    rgb = _ramp_lookup(temperature, _TEMP_RAMP)
    shade = hillshade(height, z_scale=z_scale, contrast=hillshade_strength)
    rgb = rgb * (0.6 + 0.55 * shade)[..., None]
    return _encode_png(rgb)


# Moisture debug ramp: arid (tan) -> semi-arid -> moist (green) -> wet (teal/blue).
_MOISTURE_RAMP = np.array([
    [200, 178, 120],  # arid
    [206, 200, 132],  # semi-arid
    [150, 196, 110],  # moist grassland
    [70, 172, 112],   # damp / forest
    [40, 152, 150],   # wet
    [30, 110, 175],   # saturated / rainforest
], dtype=np.float64)


def moisture_png(moisture: np.ndarray, height: np.ndarray,
                 sea_level: float = 0.4, water: np.ndarray = None,
                 z_scale: float = 12.0, hillshade_strength: float = 1.5) -> bytes:
    """Moisture field as an arid->wet ramp, shaded for relief.

    Open water (sea + lakes) is drawn as a flat deep blue: its moisture is pinned
    to the saturated end and isn't meaningful for biomes, so flattening it keeps
    the land moisture gradient — including the riparian halos along rivers —
    legible (mirrors how the temperature view stays focused on the land)."""
    rgb = _ramp_lookup(moisture, _MOISTURE_RAMP)
    shade = hillshade(height, z_scale=z_scale, contrast=hillshade_strength)
    rgb = rgb * (0.6 + 0.55 * shade)[..., None]
    wmask = (height < sea_level) if water is None else np.asarray(water, dtype=bool)
    rgb[wmask] = (np.array([30, 60, 110], dtype=np.float64)
                  * (0.6 + 0.55 * shade[wmask])[..., None])
    return _encode_png(rgb)
