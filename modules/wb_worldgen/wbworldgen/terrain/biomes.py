"""Biome registry: the single source of truth for biome ids, colors, names and
vegetation (tree) styles, shared by ``derive.py`` (classification) and
``render.py`` (coloring + forest stamps).

Classification is climate-based (temperature x moisture, a Whittaker model) and
produces a stable set of *climate* ids (0..13). A render *mode* then chooses how
those ids look:

  * ``realistic`` -> earthly palette + names (REALISTIC_BIOMES)
  * ``fantasy``   -> exotic remap of the same climate ids (FANTASY_BIOMES)

On top of either base, optional *overlay* patches stamp special ids
(``CORRUPTION``, ``ARCANE_GROVE``) that exist in both palettes.

Each biome entry carries a ``tree`` style key (or ``None``). Forest-flagged
biomes are rendered as scattered procedural tree stamps (see
``render.overlay_forests``); the style key selects the stamp shape/palette.
"""

import numpy as np

# --- Climate biome ids (output of the Whittaker classifier) ---------------
# Ids 0..6 are kept identical to the historical 7-biome scheme so any existing
# reader of the ``biome`` layer keeps working; 7..13 are appended.
OCEAN = 0
BEACH = 1
DESERT = 2
GRASSLAND = 3        # temperate grassland
FOREST = 4           # temperate (broadleaf) forest
ROCK = 5             # bare rock / steep
SNOW = 6             # permanent snow / high peaks
ICE = 7              # polar ice / frozen sea margin
TUNDRA = 8
TAIGA = 9            # boreal / coniferous forest
TEMPERATE_RAINFOREST = 10
SHRUBLAND = 11
SAVANNA = 12
JUNGLE = 13          # tropical rainforest
COLD_DESERT = 14     # cold/temperate arid (Gobi, Great Basin)

# --- Overlay ids (stamped on top of either base) --------------------------
CORRUPTION = 20
ARCANE_GROVE = 21

# --- Whittaker climate grid -----------------------------------------------
# Single source of truth for the temperature x moisture -> climate-biome table,
# shared by ``derive.classify_whittaker`` (discrete classification) and
# ``render._climate_blend_base`` (smooth climate-space colour blend) so the two
# can never drift apart. Rows are temperature bands (cold..hot), columns are
# moisture bands (dry..wet); ``TEMP_BANDS`` / ``MOISTURE_BANDS`` are the internal
# boundary values separating those bands.
WHITTAKER_GRID = (
    (COLD_DESERT, TUNDRA,    TAIGA,  TAIGA),                  # cold
    (COLD_DESERT, SHRUBLAND, TAIGA,  TAIGA),                  # cool
    (GRASSLAND,   FOREST,    FOREST, TEMPERATE_RAINFOREST),   # warm
    (DESERT,      SAVANNA,   JUNGLE, JUNGLE),                 # hot
)
TEMP_BANDS = (0.22, 0.42, 0.66)      # internal cold|cool|warm|hot boundaries
MOISTURE_BANDS = (0.25, 0.5, 0.75)   # internal dry|semi|moist|wet boundaries

# --- Underground / cave biome ids -----------------------------------------
# Kept in their own id range (30+) so they never collide with the surface
# climate ids above; the "cave" palette (see CAVE_BIOMES) is selected via the
# ``biome_mode="cave"`` path the same way realistic/fantasy are.
ROCK_WALL = 30          # solid rock — non-navigable cave wall
DRY_CAVERN = 31         # bare open cavern floor
MOSSY_GROTTO = 32       # damp, moss/mushroom-covered floor
UNDERGROUND_LAKE = 33   # flooded chamber / subterranean water
CRYSTAL_CAVERN = 34     # crystal-lined cavern
LAVA_TUBE = 35          # hot deep tube near magma
ICE_CAVE = 36           # frozen near-surface cave

_GREY = (120, 115, 110)

# id -> (name, (r,g,b), tree-style or None)
REALISTIC_BIOMES = {
    OCEAN:                ("Ocean",                 (40, 70, 120),   None),
    BEACH:                ("Beach",                 (222, 207, 150), None),
    DESERT:               ("Desert",                (204, 188, 139), None),
    GRASSLAND:            ("Grassland",             (135, 164, 110), None),
    FOREST:               ("Temperate Forest",      (70, 120, 70),   "broadleaf"),
    ROCK:                 ("Bare Rock",             (120, 115, 110), None),
    SNOW:                 ("Snow",                  (240, 240, 245), None),
    ICE:                  ("Polar Ice",             (215, 230, 240), None),
    TUNDRA:               ("Tundra",                (160, 165, 140), None),
    TAIGA:                ("Taiga",                 (60, 100, 80),   "conifer"),
    TEMPERATE_RAINFOREST: ("Temperate Rainforest",  (48, 105, 75),   "conifer"),
    SHRUBLAND:            ("Shrubland",             (167, 163, 117), "scrub"),
    SAVANNA:              ("Savanna",               (184, 174, 116), "scrub"),
    JUNGLE:               ("Tropical Rainforest",   (45, 115, 55),   "jungle"),
    COLD_DESERT:          ("Cold Desert",           (200, 195, 178), None),
    CORRUPTION:           ("Corrupted Waste",       (90, 60, 95),    None),
    ARCANE_GROVE:         ("Arcane Grove",          (70, 110, 130),  "enchanted"),
}

FANTASY_BIOMES = {
    OCEAN:                ("Abyssal Sea",           (35, 55, 110),   None),
    BEACH:                ("Pale Sands",            (225, 210, 175), None),
    DESERT:               ("Ashlands",              (150, 120, 110), None),
    GRASSLAND:            ("Bloom Heath",           (150, 165, 110), "scrub"),
    FOREST:               ("Enchanted Grove",       (70, 120, 110),  "enchanted"),
    ROCK:                 ("Crystal Fields",        (130, 130, 165), None),
    SNOW:                 ("Everfrost",             (235, 240, 250), None),
    ICE:                  ("Glacial Wastes",        (200, 225, 240), None),
    TUNDRA:               ("Frostbarrens",          (155, 165, 165), None),
    TAIGA:                ("Frostbound Pines",      (55, 95, 100),   "conifer"),
    TEMPERATE_RAINFOREST: ("Mistwood",              (50, 110, 95),   "enchanted"),
    SHRUBLAND:            ("Thornscrub",            (140, 130, 95),  "scrub"),
    SAVANNA:              ("Emberveldt",            (185, 145, 90),  "scrub"),
    JUNGLE:               ("Fungal Forest",         (95, 70, 120),   "fungal"),
    COLD_DESERT:          ("Bleak Barrens",         (165, 170, 168), None),
    CORRUPTION:           ("Corrupted Waste",       (75, 45, 85),    None),
    ARCANE_GROVE:         ("Arcane Grove",          (80, 140, 170),  "enchanted"),
}

# Underground palette. Climate ids are meaningless below ground, so caves use
# their own classifier (see ``cave_biomes.classify_caves``) over this dedicated
# id range. ``tree`` styles drive the canopy texture in render.py; caves keep
# them mostly None (only the mossy grotto gets a soft "fungal" tint).
CAVE_BIOMES = {
    ROCK_WALL:         ("Solid Rock",         (38, 36, 42),    None),
    DRY_CAVERN:        ("Dry Cavern",         (110, 100, 92),  None),
    MOSSY_GROTTO:      ("Mossy Grotto",       (78, 96, 70),    "fungal"),
    UNDERGROUND_LAKE:  ("Underground Lake",   (40, 80, 120),   None),
    CRYSTAL_CAVERN:    ("Crystal Cavern",     (120, 120, 170), None),
    LAVA_TUBE:         ("Lava Tube",          (150, 70, 45),   None),
    ICE_CAVE:          ("Ice Cave",           (180, 205, 220), None),
}

_TABLES = {"realistic": REALISTIC_BIOMES, "fantasy": FANTASY_BIOMES,
           "cave": CAVE_BIOMES}

# Highest id that needs a row in the color lookup array (across all palettes).
_MAX_ID = max(max(t) for t in _TABLES.values())


def biome_table(mode: str) -> dict:
    return _TABLES.get(mode, REALISTIC_BIOMES)


def biome_colors(mode: str = "realistic", overrides: dict = None) -> np.ndarray:
    """(_MAX_ID+1, 3) RGB lookup for the active palette; gaps -> neutral grey.

    ``overrides`` (optional) maps biome id -> ``(r, g, b)`` to replace specific
    biome colours without touching the source palette — used by the Terrain Lab's
    live colour editor. Keys may arrive as strings (JSON) and are coerced to int;
    ids outside the lookup range are ignored.
    """
    table = biome_table(mode)
    arr = np.tile(np.array(_GREY, dtype=np.float64), (_MAX_ID + 1, 1))
    for bid, (_name, color, _tree) in table.items():
        arr[bid] = color
    if overrides:
        for bid, color in overrides.items():
            try:
                idx = int(bid)
            except (TypeError, ValueError):
                continue
            if 0 <= idx <= _MAX_ID and color is not None:
                arr[idx] = color[:3]
    return arr


def tree_styles(mode: str = "realistic") -> dict:
    """id -> tree-style key, only for forest-flagged biomes in this palette."""
    return {bid: tree for bid, (_n, _c, tree) in biome_table(mode).items()
            if tree is not None}


# Default land-height fractions where bare rock and snow start fading in, and how
# wide that fade is (how fast the biome melts to rock/snow). These are the
# defaults for ``alpine_cover`` and the slider defaults in the Terrain Lab — keep
# the two in sync. Rock starts mid-upland, snow above it; both fully cover one
# ``blend`` width higher.
_ROCK_LINE = 0.45
_SNOW_LINE = 0.72
_ALPINE_BLEND = 0.23


def alpine_cover(height, sea_level: float, temperature=None,
                 exaggeration: float = 0.0, rock_line: float = _ROCK_LINE,
                 snow_line: float = _SNOW_LINE, blend: float = _ALPINE_BLEND):
    """Height-based bare-rock and snow cover, replacing the old ROCK/SNOW biome
    classes.

    Returns ``(rock_weight, snow_weight)`` arrays in [0,1]: how much each cell
    reads as bare rock and snow. Used by the renderer (to fade biome -> rock ->
    snow with altitude), by settlement placement (harsh high ground) and by road
    routing (expensive high ground), so all three agree on where the mountains
    are.

    Height is the primary driver. ``rock_line`` / ``snow_line`` are the land-height
    fractions (0..1 above sea level) where bare rock and snow begin, and ``blend``
    is the fade width — small = a hard border, large = a slow, gradual melt. A
    ``temperature`` field (if given) lowers both lines over cold ground and raises
    them over warm ground, so polar peaks whiten sooner than tropical ones.
    ``exaggeration`` is the Mountain-Height slider's 0..2 visual band, which lowers
    the lines so tall ranges read more rock/snow-capped without changing geometry.
    """
    h = np.asarray(height, dtype=np.float64)
    hf = np.clip((h - sea_level) / max(1e-6, 1.0 - sea_level), 0.0, 1.0)
    drop = 0.12 * float(np.clip(exaggeration, 0.0, 2.0))
    width = max(1e-3, float(blend))
    if temperature is not None:
        clim = (0.5 - np.clip(np.asarray(temperature, dtype=np.float64),
                              0.0, 1.0)) * 0.25
    else:
        clim = 0.0

    def _smoothstep(lo, hi):
        t = np.clip((hf - lo) / np.maximum(1e-6, hi - lo), 0.0, 1.0)
        return t * t * (3.0 - 2.0 * t)

    rock = _smoothstep(rock_line - drop + clim, rock_line + width - drop + clim)
    snow = _smoothstep(snow_line - drop + clim, snow_line + width - drop + clim)
    return rock, snow
