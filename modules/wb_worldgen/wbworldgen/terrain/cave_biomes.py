"""Underground biome classification.

The surface Whittaker model (temperature x moisture, latitude bands) is
meaningless below ground, so caves get their own classifier over the dedicated
cave id range in :mod:`biomes`. It mirrors the *structure* of
``derive.classify_whittaker`` — lay down a default, then let later, more
specific writes win — but the inputs are cave-appropriate:

  * **water proximity** (moisture) -> mossy grotto near water, flooded chambers;
  * a low-frequency **heat** field, gated by depth/dryness -> lava tubes;
  * low-frequency **mineral** noise -> crystal caverns;
  * a low-frequency **chill** field near the surface -> ice caves.

Everything is masked to the open (navigable) cells; rock walls keep the solid
``ROCK_WALL`` id so downstream code can tell floor from wall.
"""

import numpy as np

from wbworldgen.terrain import heightmap as _hm
from wbworldgen.terrain import biomes as _bm


def classify_caves(open_mask: np.ndarray, floor: np.ndarray,
                   moisture: np.ndarray, flooded: np.ndarray,
                   seed: int = 0, lava_amount: float = 0.5,
                   crystal_amount: float = 0.5, ice_amount: float = 0.3,
                   biome_blend: float = 0.6,
                   fault: np.ndarray = None) -> np.ndarray:
    """Classify each open cell into a cave biome id; rock stays ``ROCK_WALL``.

    Args:
        open_mask: boolean, True on navigable cave floor.
        floor: [0,1] floor-elevation field (low = deep basins / sumps).
        moisture: [0,1] proximity-to-water field (1 = adjacent to water).
        flooded: boolean, True where standing water sits (lakes + river cells).
        lava_amount / crystal_amount / ice_amount: 0..1 abundances of the
            respective special biomes.
        biome_blend: 0..1 dithers the field thresholds with low-freq noise so the
            biome borders interlock instead of snapping along contour lines.
        fault: optional [0,1] tectonic fault field; where it creases (high), the
            mineral field is lifted so crystal veins run along the faults and
            lava tubes favour the same structural weaknesses.
    """
    res = open_mask.shape[0]
    biome = np.full(open_mask.shape, _bm.ROCK_WALL, dtype=np.int32)
    floor = np.clip(np.asarray(floor, dtype=np.float64), 0.0, 1.0)
    moisture = np.clip(np.asarray(moisture, dtype=np.float64), 0.0, 1.0)

    # Low-frequency fields that drive the special biomes. Distinct sub-seeds so
    # they're uncorrelated; heat clusters deep, chill near the "surface".
    heat = _hm.fbm(res, int(seed) + 7001, octaves=3, base_freq=3)
    mineral = _hm.fbm(res, int(seed) + 7002, octaves=4, base_freq=5)
    chill = _hm.fbm(res, int(seed) + 7003, octaves=3, base_freq=3)

    # Tectonic faults concentrate mineralisation and magma pathways: lift the
    # mineral/heat fields along the fault creases so crystals and lava tubes run
    # as veins along structural weaknesses rather than as isolated round patches.
    if fault is not None:
        f = np.clip(np.asarray(fault, dtype=np.float64), 0.0, 1.0)
        mineral = np.clip(mineral + 0.35 * f, 0.0, 1.0)
        heat = np.clip(heat + 0.25 * f, 0.0, 1.0)

    # Dither thresholds for ragged ecotones (same idea as the surface blend).
    if biome_blend > 1e-3:
        amp = 0.08 * float(np.clip(biome_blend, 0.0, 1.0))
        jit = _hm.fbm(res, int(seed) + 7004, octaves=4, base_freq=6) - 0.5
        moisture = np.clip(moisture + amp * 2.0 * jit, 0.0, 1.0)

    op = open_mask

    def put(mask, bid):
        biome[op & mask] = bid

    # 1. Default open floor is a dry cavern.
    put(np.ones_like(op), _bm.DRY_CAVERN)

    # 2. Damp ground near water becomes mossy/mushroom grotto.
    put(moisture > 0.5, _bm.MOSSY_GROTTO)

    # 3. Crystal caverns where the mineral field peaks (scaled by abundance).
    crystal_thr = 1.0 - 0.45 * float(np.clip(crystal_amount, 0.0, 1.0))
    put(mineral > crystal_thr, _bm.CRYSTAL_CAVERN)

    # 4. Lava tubes: hot field, in the deeper + drier parts of the cave system.
    lava_thr = 1.0 - 0.45 * float(np.clip(lava_amount, 0.0, 1.0))
    put((heat > lava_thr) & (floor < 0.6) & (moisture < 0.55), _bm.LAVA_TUBE)

    # 5. Ice caves: cold pockets away from standing water.
    ice_thr = 1.0 - 0.45 * float(np.clip(ice_amount, 0.0, 1.0))
    put((chill > ice_thr) & (moisture < 0.6), _bm.ICE_CAVE)

    # 6. Standing water wins last so subterranean lakes/rivers always read.
    put(np.asarray(flooded, dtype=bool), _bm.UNDERGROUND_LAKE)

    return biome
