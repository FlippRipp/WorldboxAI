"""Derive secondary layers from a final height field.

These are the layers a future pipeline step would expose to downstream
decisions (e.g. city placement): slope, water mask, a cheap moisture proxy, a
temperature field and a discrete biome classification.

Biomes are classified with a Whittaker model (temperature x moisture); the
discrete ids and their colors/names live in ``biomes.py`` so render and derive
share one source of truth. A ``biome_mode`` selects the realistic or fantasy
palette (the climate classification is identical; only the labelling differs),
and an optional fantasy overlay stamps corruption / arcane-grove patches.
"""

import numpy as np
from scipy.ndimage import distance_transform_edt

from wbworldgen.terrain import heightmap as _hm
from wbworldgen.terrain import biomes as _bm
from wbworldgen.terrain.biomes import WHITTAKER_GRID, TEMP_BANDS, MOISTURE_BANDS
from wbworldgen.terrain.precipitation import precipitation_map
# Re-export biome id constants for backward compatibility with older callers.
from wbworldgen.terrain.biomes import (  # noqa: F401
    OCEAN, BEACH, DESERT, GRASSLAND, FOREST, ROCK, SNOW, ICE, TUNDRA, TAIGA,
    TEMPERATE_RAINFOREST, SHRUBLAND, SAVANNA, JUNGLE, COLD_DESERT, CORRUPTION,
    ARCANE_GROVE,
)


def slope_map(height: np.ndarray) -> np.ndarray:
    gy, gx = np.gradient(height)
    return np.hypot(gx, gy)


def temperature_map(height: np.ndarray, sea_level: float = 0.4,
                    equator: float = 0.5, band: float = 1.0,
                    lapse: float = 0.6, seed: int = 0) -> np.ndarray:
    """A [0,1] temperature field: warm at the equator, cold at the poles and on
    high ground.

    ``equator`` (0..1) is the row, as a fraction of map height, that is hottest;
    temperature falls off toward both the top and bottom edge. ``band`` widens
    (>1) or tightens (<1) the warm/temperate zone. ``lapse`` cools high
    elevations (an environmental lapse rate applied to land above ``sea_level``).
    A little low-frequency noise keeps the isotherms from being perfectly flat.
    """
    h = height.shape[0]
    rows = np.linspace(0.0, 1.0, height.shape[0])[:, None]
    # Distance from the equator latitude, normalized so the poles read ~1.
    lat = np.abs(rows - equator) / max(1e-6, max(equator, 1.0 - equator))
    base = np.clip(1.0 - (lat / max(0.2, band)) ** 1.5, 0.0, 1.0)
    base = np.broadcast_to(base, height.shape).copy()

    # Elevation lapse: cool land that rises above the waterline.
    land_elev = np.clip(height - sea_level, 0.0, None)
    base -= lapse * land_elev

    # Multi-scale jitter so climate belts waver and interlock instead of forming
    # flat horizontal stripes (broad meander + finer ragged edge).
    jitter = (_hm.fbm(height.shape[0], int(seed) + 4242, octaves=3, base_freq=3)
              + 0.5 * _hm.fbm(height.shape[0], int(seed) + 4243, octaves=4, base_freq=7))
    base += (jitter / 1.5 - 0.5) * 0.24

    return np.clip(base, 0.0, 1.0)


def classify_whittaker(elev: np.ndarray, temp: np.ndarray, moisture: np.ndarray,
                       slope: np.ndarray, water: np.ndarray,
                       sea_level: float, biome_blend: float = 0.7,
                       mountain_exaggeration: float = 0.0,
                       seed: int = 0) -> np.ndarray:
    """Vectorized Whittaker classification -> climate biome ids.

    Order matters: later writes win, so broad climate zones are laid down first
    and physical overrides (steep rock, cold-high snow, frozen sea) last.

    ``biome_blend`` (0..1) dithers the temperature/moisture band thresholds with
    low-amplitude noise so neighbouring biomes interlock into natural ecotones
    instead of snapping along straight threshold lines.
    """
    biome = np.full(elev.shape, GRASSLAND, dtype=np.int32)
    land = ~water
    coast = land & (elev < sea_level + 0.001)

    # Jitter the band inputs so threshold crossings wander cell-to-cell. A small
    # FBM offset (zero-mean) on both axes turns the straight Whittaker grid lines
    # into ragged, interlocking borders; biome_blend scales the ecotone width.
    if biome_blend > 1e-3:
        amp = 0.06 * float(np.clip(biome_blend, 0.0, 1.0))
        t_jit = (_hm.fbm(elev.shape[0], int(seed) + 5511, octaves=4, base_freq=6) - 0.5)
        m_jit = (_hm.fbm(elev.shape[0], int(seed) + 5512, octaves=4, base_freq=6) - 0.5)
        temp = np.clip(temp + amp * 2.0 * t_jit, 0.0, 1.0)
        moisture = np.clip(moisture + amp * 2.0 * m_jit, 0.0, 1.0)

    # Temperature x moisture lookup over land, driven by the shared Whittaker grid
    # (see biomes.WHITTAKER_GRID) so render's climate-space colour blend uses the
    # exact same table. ``_band_masks`` splits a [0,1] field into ordered low..high
    # band masks at the given internal boundaries: e.g. temp -> cold|cool|warm|hot.
    def _band_masks(values, edges):
        masks = []
        lo = -np.inf
        for hi in (*edges, np.inf):
            masks.append((values >= lo) & (values < hi))
            lo = hi
        return masks

    temp_masks = _band_masks(temp, TEMP_BANDS)            # cold, cool, warm, hot
    moist_masks = _band_masks(moisture, MOISTURE_BANDS)   # dry, semi, moist, wet
    for t, tmask in enumerate(temp_masks):
        for m, mmask in enumerate(moist_masks):
            biome[land & tmask & mmask] = WHITTAKER_GRID[t][m]

    # Beaches sit just above the waterline (override climate at the shore).
    biome[coast] = BEACH

    # NOTE: high-mountain bare rock and snow are no longer baked into the biome
    # map here. The underlying climate biome (forest, grassland, tundra, ...) is
    # kept all the way to the peaks, and rock/snow are applied as a *height-based
    # fade* downstream by ``biomes.alpine_cover`` — for colour in render.biome_png
    # and for harsh/expensive high ground in terrain_placement and roads. This is
    # what lets the map fade "whatever biome is there -> rock -> snow" with
    # altitude instead of snapping to a flat rock/snow class. (``slope`` and
    # ``mountain_exaggeration`` are retained on the signature for callers but are
    # no longer consumed here.)

    # Water: frozen sea (ICE) at the coldest latitudes, open ocean elsewhere.
    biome[water] = OCEAN
    biome[water & (temp < 0.12)] = ICE
    return biome


def _apply_fantasy_overlay(biome: np.ndarray, water: np.ndarray,
                           seed: int) -> np.ndarray:
    """Stamp corruption / arcane-grove patches over land via low-freq masks."""
    land = ~water
    res = biome.shape[0]
    corrupt = _hm.fbm(res, int(seed) + 9001, octaves=4, base_freq=3)
    arcane = _hm.fbm(res, int(seed) + 9002, octaves=4, base_freq=3)
    biome[land & (corrupt > 0.78)] = CORRUPTION
    biome[land & (arcane > 0.80)] = ARCANE_GROVE
    return biome


def derive_layers(height: np.ndarray, sea_level: float = 0.4,
                  lake_mask: np.ndarray = None, biome_mode: str = "realistic",
                  fantasy_overlay: bool = False, equator: float = 0.5,
                  temp_band: float = 1.0, lapse: float = 0.6,
                  wind_dir: float = 270.0, humidity: float = 1.0,
                  orographic: float = 1.0, aridity: float = 0.0,
                  river_mask: np.ndarray = None,
                  river_field: np.ndarray = None, river_moisture: float = 0.3,
                  biome_blend: float = 0.7, mountain_exaggeration: float = 0.0,
                  alpine_aridity: float = 0.6, seed: int = 0) -> dict:
    """Compute slope / water / moisture / temperature / biome from a [0,1]-ish
    height field.

    ``sea_level`` is a quantile-independent absolute cutoff in height units.
    ``lake_mask`` (optional) marks inland lake cells, treated as water for
    moisture and biome. ``river_mask`` / ``river_field`` (optional) feed the
    river network into the moisture map so flowing water humidifies its banks
    (``river_moisture`` scales the riparian lift). ``biome_mode`` ("realistic" |
    "fantasy") only selects the palette/labels downstream; the climate
    classification is identical. ``fantasy_overlay`` stamps exotic patches on top
    of either base.
    """
    h = height.astype(np.float64)
    # Normalize defensively so sea_level is meaningful.
    lo, hi = float(h.min()), float(h.max())
    if hi - lo > 1e-9:
        h = (h - lo) / (hi - lo)

    sea = h < sea_level
    water = sea if lake_mask is None else (sea | lake_mask)
    slope = slope_map(h)

    # Temperature first: the precipitation sweep uses it (warm seas evaporate
    # more) and the classifier's snow line is driven off it. The Mountain Height
    # slider's visual band cools high ground harder (stronger lapse) so the snow
    # line descends and tall ranges read bigger — without changing geometry.
    lapse_eff = lapse * (1.0 + 1.5 * mountain_exaggeration)
    temperature = temperature_map(h, sea_level=sea_level, equator=equator,
                                  band=temp_band, lapse=lapse_eff, seed=seed)

    # Moisture from a wind-driven orographic precipitation sweep (rain shadow,
    # windward uplift) instead of the old coast-distance proxy. Blend in a thin
    # coastal-humidity term so immediate shorelines stay reliably moist.
    precip = precipitation_map(h, water, temperature, sea_level=sea_level,
                               wind_dir=wind_dir, humidity=humidity,
                               orographic=orographic, aridity=aridity, seed=seed)
    # Standing-water humidity: the sea and inland lakes (both already folded into
    # ``water``) raise moisture in a broad coastal band via a distance falloff.
    dist_to_water = distance_transform_edt(~water)
    coast_moist = np.exp(-(dist_to_water / max(1.0, h.shape[0] * 0.04)) ** 2)
    moisture = 0.8 * precip + 0.2 * coast_moist

    # Riparian humidity: flowing rivers wet their banks, raising local moisture in
    # a tighter corridor than open coasts (gallery forests, green valleys threading
    # through drier land). Big trunks humidify a wider band than headwater creeks,
    # so prefer the continuous flow field (``river_field``) and fall back to the
    # binary channel mask. The lift is proportional to (1 - moisture) so it eases
    # toward saturation without overshooting already-wet ground.
    if river_moisture > 0.0:
        rsource = None
        if river_field is not None:
            rsource = np.asarray(river_field, dtype=np.float64) > 0.05
        elif river_mask is not None:
            rsource = np.asarray(river_mask, dtype=bool)
        if rsource is not None and rsource.any():
            dist_to_river = distance_transform_edt(~rsource)
            riparian = np.exp(-(dist_to_river / max(1.0, h.shape[0] * 0.02)) ** 2)
            moisture = moisture + river_moisture * riparian * (1.0 - moisture)

    # Alpine aridity: high ground sits above most weather and in the rain shadow
    # of its own windward slopes, so moisture falls off with altitude. This is
    # what makes high peaks barren rock with only patchy snow (instead of a flat
    # snow cap), letting the biome classifier carry that look without a colour
    # tint. Applied before the final clip so the dried field feeds the classifier.
    if alpine_aridity > 0.0:
        hf = np.clip((h - sea_level) / max(1e-6, 1.0 - sea_level), 0.0, 1.0)
        onset = 0.35                          # aridity begins ~35% up the land range
        dry = np.clip((hf - onset) / (1.0 - onset), 0.0, 1.0) ** 1.5  # accelerate to peaks
        moisture = moisture * (1.0 - alpine_aridity * dry)

    moisture = np.clip(moisture, 0.0, 1.0)

    biome = classify_whittaker(h, temperature, moisture, slope, water, sea_level,
                               biome_blend=biome_blend,
                               mountain_exaggeration=mountain_exaggeration,
                               seed=seed)
    if fantasy_overlay:
        biome = _apply_fantasy_overlay(biome, water, seed)

    land = ~water
    return {
        "height": h,
        "slope": slope,
        "water": water,
        "moisture": moisture,
        "temperature": temperature,
        "biome": biome,
        "biome_mode": biome_mode,
        "sea_level": sea_level,
        "land_fraction": float(land.mean()),
    }
