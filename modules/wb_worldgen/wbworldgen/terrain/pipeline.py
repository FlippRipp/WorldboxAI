"""Terrain generation orchestrator.

``generate_terrain`` is the single entry point used by both the experimental
API and the (unregistered) pipeline step. It chains:

    base heightmap  ->  + noise mountain chains  ->  thermal erosion  ->
    hydraulic erosion  ->  derived layers (slope/water/moisture/biome)

and returns the arrays plus per-stage timing stats.
"""

import base64
from dataclasses import dataclass, field, asdict
import time
from typing import Optional

import numpy as np

from wbworldgen.terrain import heightmap as _hm
from wbworldgen.terrain import erosion as _er
from wbworldgen.terrain import momentum_erosion as _me
from wbworldgen.terrain import derive as _dv
from wbworldgen.terrain import rivers as _rv
from wbworldgen.terrain import lakes as _lk
from wbworldgen.terrain import craters as _cr
from wbworldgen.terrain import coast as _co


@dataclass
class TerrainParams:
    seed: int = -1                   # < 0 => pick a fresh random seed each run
    resolution: int = 512
    octaves: int = 6
    warp: float = 0.35
    island: float = 0.5
    mountain_strength: float = 0.85  # height of noise mountain chains
    mountain_coverage: float = 0.5   # how much of the land is mountainous
    mountain_sharpness: float = 2.5  # ridge sharpness (higher = thinner ridges)
    spline_ridges: int = 0            # 0 = disabled; 1–15 = number of arc chains
    spline_ridge_strength: float = 0.5  # height added along arc centerline
    spline_ridge_width: float = 0.12    # falloff half-width (fraction of map)
    spline_ridge_length: float = 1.0    # arc span (fraction of map diagonal)
    redistribution: float = 2.2      # power-law: >1 flattens lowlands, keeps peaks
    thermal_iterations: int = 20
    droplets: int = 60000
    erosion_strength: float = 1.0    # droplet erosion intensity (deeper valleys)
    erosion_backend: str = "auto"  # auto | numba | numpy
    # --- Hydrology model: which erosion drives the water ---
    hydrology_model: str = "momentum"  # momentum | droplet
    momentum_iterations: int = 60    # particle batches (momentum model)
    momentum_particles: int = 8000   # particles per batch (momentum model)
    momentum_transfer: float = 0.8   # flow-momentum steering -> meander strength
    discharge_alpha: float = 0.4     # blend rate of new flow into discharge map
    sea_level: float = 0.4
    rivers: bool = True              # compute & overlay river network
    river_density: float = 0.5       # 0..1, higher = more/smaller tributaries
    river_carve: float = 0.02        # incise channels into the heightmap
    river_meander: float = 1.2       # S-bend amplitude (0 = follow raw flow path)
    lakes: bool = True               # keep large depressions as lakes
    lake_min_area: int = 50          # min cells for a depression to become a lake
    lake_min_depth: float = 0.012    # min basin depth (height units) for a lake
    breach: bool = True              # carve drainage notches in shallow basins
    breach_max_depth: float = 0.05   # max basin depth that gets breached vs filled
    deltas: bool = True              # L-system distributary deltas at river mouths
    delta_min_order: int = 3         # min Strahler order of a mouth to form a delta
    delta_size: float = 1.0          # overall delta extent

    # --- Meteoric impact craters ---
    craters: bool = False
    crater_count: int = 5
    crater_min_radius: float = 0.02  # fraction of min(H,W)
    crater_max_radius: float = 0.08  # fraction of min(H,W)
    crater_depth: float = 0.15       # bowl depth in height units
    crater_rim_height: float = 0.05  # raised rim height in height units
    crater_ejecta_falloff: float = 3.0  # higher = tighter ejecta blanket
    crater_age: str = "ancient"      # "ancient" (pre-erosion) | "fresh" (post-erosion)

    # --- Coastal / submarine smoothing ---
    coastal_smooth: bool = False
    coastal_smooth_width: float = 0.08    # height band above sea level to ease
    coastal_smooth_strength: float = 1.0  # 0..1 blend weight underwater

    # --- Biomes & vegetation ---
    biome_mode: str = "realistic"    # realistic | fantasy palette/labels
    fantasy_overlay: bool = False    # stamp corruption / arcane-grove patches
    equator: float = 0.5             # latitude (0..1 row) that is hottest
    temp_band: float = 1.0           # width of the warm/temperate zone
    lapse: float = 0.6               # how strongly elevation cools temperature
    wind_dir: float = 270.0          # prevailing wind (deg, from); 270 = westerly
    humidity: float = 1.0            # overall rainfall / air moisture
    orographic: float = 1.0          # windward-vs-leeward rain-shadow contrast
    aridity: float = 0.0             # 0..1 dry-skew of the moisture field (desert extent)
    alpine_aridity: float = 0.6      # 0..1 how strongly high ground dries to rock
    rock_line: float = 0.45          # land-height fraction where bare rock starts
    snow_line: float = 0.72          # land-height fraction where snow starts
    alpine_blend: float = 0.23       # rock/snow fade width (low = hard border)
    river_moisture: float = 0.3      # riparian humidity rivers add to their banks
    forests: bool = True             # render forests as procedural tree stamps
    forest_density: float = 0.5      # 0..1 tree scatter density
    biome_blend: float = 0.7         # 0..1 how aggressively biomes melt together

    # --- Rendering ---
    relief: float = 16.0             # hillshade vertical exaggeration (z_scale)
    hillshade_strength: float = 1.5  # shaded-relief contrast (punchiness)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "TerrainParams":
        d = d or {}
        valid = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TerrainResult:
    params: TerrainParams
    layers: dict          # height/slope/water/moisture/biome arrays
    stats: dict = field(default_factory=dict)


def generate_terrain(params: TerrainParams, on_frame=None) -> TerrainResult:
    res = max(64, min(4096, int(params.resolution)))
    stats = {}

    # Work-in-progress streaming. When ``on_frame`` is supplied we render a
    # hillshade preview at stage boundaries and (throttled) inside the iterative
    # erosion loops, so a client can watch the world form. Preview frames render
    # at the selected resolution so the live view matches the final image;
    # downsampling only kicks in past ``_PREVIEW_MAX`` to keep huge maps (e.g.
    # 4096) cheap to stream. The sea level is estimated per-frame (the real sea
    # threshold is only known after erosion), so frames are previews, not the
    # final artifact. All of this is skipped entirely when on_frame is None.
    _PREVIEW_MAX = 1024  # cap preview dimension; below this, render full-res
    _step = max(1, res // _PREVIEW_MAX)  # downsample factor for preview frames

    # Erosion runs in *cell* units, so its effect is implicitly tied to grid
    # resolution. A fixed particle/droplet budget spread over 4x the cells (a
    # doubled resolution) carves each cell ~1/4 as much, and particles — moving
    # one cell per step for a fixed step count — traverse a smaller slice of the
    # map. The terrain therefore stays closer to the smooth base heightmap the
    # higher you go. Pin the look to a reference resolution and scale every
    # cell-tied erosion parameter by the linear ratio so the carved result is
    # consistent in *world* terms at any resolution:
    #   - particle / droplet COUNT      ~ scale^2  (constant per-area density)
    #   - max_steps, erosion radius      ~ scale    (same travel distance / width)
    #   - thermal talus / iterations     ~ 1/scale, scale (same world slope/diffusion)
    REF_RES = 1024.0
    res_scale = res / REF_RES
    stats["res_scale"] = round(res_scale, 3)

    # Mountain Height slider's 1..3 band (geometry caps at 1.0) returns here as a
    # 0..2 visual amplifier that exaggerates relief / shading / rock+snow tinting
    # so mountains read bigger without taller geometry. Mirror of
    # render.mountain_exaggeration (kept inline so this module's lazy render
    # import stays inside _emit).
    mtn_exagg = float(np.clip(params.mountain_strength - 1.0, 0.0, 2.0))
    relief_eff = params.relief * (1.0 + 0.6 * mtn_exagg)
    hillshade_eff = params.hillshade_strength * (1.0 + 0.15 * mtn_exagg)
    stats["mountain_exaggeration"] = round(mtn_exagg, 3)

    def _emit(stage, label, height, frac, done=0, total=0, sea=None):
        if on_frame is None:
            return
        from wbworldgen.terrain import render as _rd  # lazy: pulls in PIL
        small = np.ascontiguousarray(height[::_step, ::_step])
        # Use the predetermined sea level once it's known; before that, estimate.
        sea_v = sea if sea is not None else float(
            np.quantile(small, np.clip(params.sea_level, 0.0, 0.99)))
        png = _rd.hillshade_png(small, sea_v, z_scale=relief_eff,
                                hillshade_strength=hillshade_eff)
        on_frame({"stage": stage, "label": label, "frac": round(float(frac), 3),
                  "done": int(done), "total": int(total),
                  "image": base64.b64encode(png).decode("ascii")})

    def _throttle(stage, label, base, span, every, sea=None):
        """Build a progress_cb that emits a frame every ``every`` iterations
        (and always on the last), mapping loop progress into [base, base+span]."""
        def cb(done, total, height):
            if on_frame is None:
                return
            if done % every == 0 or done == total:
                _emit(stage, label, height, base + span * (done / max(1, total)),
                      done, total, sea=sea)
        return cb

    # A negative seed means "make a new random world each run". Resolve it to a
    # concrete value up front so every stage is consistent and we can report the
    # seed back for the user to copy and reuse.
    if int(params.seed) < 0:
        params.seed = int(np.random.default_rng().integers(0, 2**31 - 1))
    stats["seed"] = int(params.seed)

    t = time.time()
    base = _hm.base_heightmap(params.seed, res=res, octaves=params.octaves,
                              warp=params.warp, island=params.island)
    stats["heightmap_s"] = round(time.time() - t, 3)
    _emit("base", "Base heightmap", base, 0.05)

    t = time.time()
    # Noise-based mountain chains (no tectonics): masked, domain-warped ridged
    # noise added on top of the continents, scaled by a land factor so ranges
    # sit inland and fade out at the coast rather than forming sea cliffs.
    mtn = _hm.mountain_field(res, params.seed,
                             coverage=params.mountain_coverage,
                             sharpness=params.mountain_sharpness)
    land_factor = np.clip((base - 0.2) / 0.8, 0.0, 1.0)

    # Elevation redistribution (Red Blob Games): a power curve > 1 pushes the
    # abundant mid elevations down into lowlands/sea while leaving peaks high.
    # Apply it to the *base continents only*; if applied after the mountains are
    # stacked on it crushes their mid-slopes into thin spikes (so raising
    # mountain_strength gave skinnier, not bigger, mountains).
    base_shaped = base
    if abs(params.redistribution - 1.0) > 1e-3:
        base_shaped = np.clip(base, 0.0, 1.0) ** params.redistribution

    # Anchor the continental shelf in a fixed lower band and stack mountain
    # relief on top. The *geometric* mountain height is driven only by the
    # slider's 0..1 band (``geo_strength``); the 1..3 band is a visual amplifier
    # (see render.mountain_exaggeration) and adds no geometry. Peaks overshoot
    # ~1.15 in only the few cells where base and ridge are both high; we keep
    # just a floor clip (no top clamp) so the post-erosion normalize below
    # sharpens those into pointed peaks instead of clamping them into flat
    # plateaus at 1.0.
    geo_strength = min(params.mountain_strength, 1.0)
    height = 0.6 * base_shaped + geo_strength * mtn * land_factor * 0.55
    height = np.clip(height, 0.0, None)

    if params.spline_ridges > 0:
        spline_field = _hm._spline_ridge_field(
            res, params.seed,
            count=params.spline_ridges,
            width=params.spline_ridge_width,
            length=params.spline_ridge_length)
        # Add height only where noise mountains aren't already dominant,
        # so arcs fill gaps rather than double-stacking on existing peaks.
        fill_weight = np.clip(1.0 - mtn * 2.0, 0.0, 1.0)
        height += params.spline_ridge_strength * spline_field * fill_weight * land_factor
        height = np.clip(height, 0.0, None)

    stats["mountains_s"] = round(time.time() - t, 3)
    _emit("mountains", "Mountain chains", height, 0.1)

    if params.craters and params.crater_age == "ancient":
        t = time.time()
        height = _cr.apply_craters(
            height,
            count=params.crater_count,
            min_radius=params.crater_min_radius,
            max_radius=params.crater_max_radius,
            depth=params.crater_depth,
            rim_height=params.crater_rim_height,
            ejecta_falloff=params.crater_ejecta_falloff,
            seed=params.seed ^ 0xDEAD,
        )
        stats["craters_s"] = round(time.time() - t, 3)

    # Predetermine the sea level from the pre-erosion surface so the erosion
    # simulation knows what is submarine: below this line channels are never
    # carved — only sediment is deposited (seabed, shoals and deltas build up).
    # Defined from the base continents (not the mountainized field) so coastline
    # coverage stays stable regardless of mountain height; the same value is
    # carried through the post-erosion normalize rather than recomputed.
    base_ocean = base < float(np.quantile(base, np.clip(params.sea_level, 0.0, 0.99)))
    if base_ocean.any() and (~base_ocean).any():
        sea_abs = float(np.quantile(height[base_ocean], 0.99))
        sea_abs = min(sea_abs, float(np.quantile(height[~base_ocean], 0.02)))
    else:
        sea_abs = float(np.quantile(height, np.clip(params.sea_level, 0.0, 0.99)))

    thermal_iters = max(1, round(params.thermal_iterations * res_scale))
    thermal_talus = 0.004 / res_scale

    # Hydraulic erosion. Thermal talus smoothing is paired with each model:
    #   momentum -> particle erosion with a coupled discharge/momentum map, whose
    #     simulated flow is reused directly by the river stage (emergent meanders).
    #     Thermal is interleaved one pass at a time between batches so banks are
    #     smoothed as channels carve, instead of only up front.
    #   droplet  -> the classic capacity/slope droplet carver (flow recomputed
    #     later by the D-infinity hydrology in build_rivers), preceded by a single
    #     full thermal pre-smoothing pass as before.
    sim_discharge = None
    sim_momentum = None
    if params.hydrology_model == "momentum":
        t = time.time()
        # Keep the batch count fixed (so the discharge/momentum blend dynamics
        # are identical) and scale the per-batch particle count by area, plus
        # particle travel distance and channel width linearly.
        sim = _me.momentum_erode(
            height, iterations=params.momentum_iterations,
            particles=max(1, round(params.momentum_particles * res_scale * res_scale)),
            seed=params.seed,
            backend=params.erosion_backend, strength=params.erosion_strength,
            momentum_transfer=params.momentum_transfer,
            discharge_alpha=params.discharge_alpha, sea_level=sea_abs,
            max_steps=max(1, round(128 * res_scale)),
            radius=max(1, round(2 * res_scale)),
            thermal_iterations=thermal_iters, thermal_talus=thermal_talus,
            thermal_factor=0.5,
            progress_cb=_throttle("momentum", "Momentum erosion (rivers forming)",
                                  0.25, 0.55, every=3, sea=sea_abs))
        height = sim["height"]
        sim_discharge = sim["discharge"]
        sim_momentum = (sim["momentum_x"], sim["momentum_y"])
        stats["momentum_s"] = round(time.time() - t, 3)
        stats["thermal_interleaved"] = True
        stats["max_discharge"] = round(float(sim_discharge.max()), 2)
    else:
        t = time.time()
        height = _er.thermal_erode(
            height, iterations=thermal_iters, talus=thermal_talus,
            progress_cb=_throttle("thermal", "Thermal erosion", 0.12, 0.1,
                                  every=5, sea=sea_abs))
        stats["thermal_s"] = round(time.time() - t, 3)
        t = time.time()
        height = _er.hydraulic_erode(
            height,
            droplets=max(1, round(params.droplets * res_scale * res_scale)),
            seed=params.seed, backend=params.erosion_backend,
            strength=params.erosion_strength, sea_level=sea_abs,
            max_steps=max(1, round(64 * res_scale)),
            radius=max(1, round(2 * res_scale)))
        stats["hydraulic_s"] = round(time.time() - t, 3)
        _emit("hydraulic", "Droplet erosion", height, 0.8, sea=sea_abs)
    stats["hydrology_model"] = params.hydrology_model
    stats["erosion_backend"] = (
        "numpy" if params.erosion_backend == "numpy"
        else ("numba" if _er._HAS_NUMBA else "numpy")
    )

    # Normalize post-erosion to [0,1] for rendering / biome thresholds, carrying
    # the *predetermined* sea level (fixed above, before erosion) through the
    # same affine transform instead of recomputing it. The waterline the erosion
    # simulation respected stays the waterline.
    lo, hi = float(height.min()), float(height.max())
    if hi - lo > 1e-9:
        height = (height - lo) / (hi - lo)
        sea_abs = (sea_abs - lo) / (hi - lo)
    sea_abs = float(np.clip(sea_abs, 0.0, 1.0))

    # Coastal/submarine smoothing: blur the sea floor and ease low coastal land
    # into gentle slopes, blended by depth below the waterline. Runs before
    # fresh craters / lakes / rivers so those features read the smoothed coast.
    if params.coastal_smooth:
        t = time.time()
        height = _co.smooth_coast(
            height, sea_abs,
            width=params.coastal_smooth_width,
            strength=params.coastal_smooth_strength,
            sigma=max(1.0, res / 200.0),
        )
        stats["coastal_s"] = round(time.time() - t, 3)

    if params.craters and params.crater_age == "fresh":
        t = time.time()
        height = _cr.apply_craters(
            height,
            count=params.crater_count,
            min_radius=params.crater_min_radius,
            max_radius=params.crater_max_radius,
            depth=params.crater_depth,
            rim_height=params.crater_rim_height,
            ejecta_falloff=params.crater_ejecta_falloff,
            seed=params.seed ^ 0xDEAD,
        )
        stats["craters_s"] = round(time.time() - t, 3)
        # Re-run post-erosion normalization so fresh craters sit in the same
        # height space as the eroded terrain they punched into.
        lo, hi = float(height.min()), float(height.max())
        if hi - lo > 1e-9:
            height = (height - lo) / (hi - lo)

    # Lakes + selective breaching: classify depressions into lakes / breached
    # valleys / filled pits. ``route`` is the drain-guaranteed DEM for flow.
    lake_mask = None
    lake_depth = None
    route = None
    if params.lakes or params.breach:
        t = time.time()
        res_d = _lk.resolve_depressions(
            height, sea_abs,
            lake_min_area=params.lake_min_area,
            lake_min_depth=params.lake_min_depth if params.lakes else 1e9,
            breach=params.breach, breach_max_depth=params.breach_max_depth,
            seed=params.seed)
        height = res_d["terrain"]
        route = res_d["route"]
        lake_mask = res_d["lake_mask"]
        lake_depth = res_d["lake_depth"]
        stats["lakes_s"] = round(time.time() - t, 3)
        stats["lakes"] = res_d["n_lakes"]
        stats["breached"] = res_d["n_breached"]
        _emit("lakes", "Lakes & basins", height, 0.88)

    river = None
    if params.rivers:
        t = time.time()
        river = _rv.build_rivers(height, sea_abs,
                                 density=params.river_density,
                                 carve=params.river_carve,
                                 meander=params.river_meander,
                                 route_height=route,
                                 deltas=params.deltas,
                                 delta_min_order=params.delta_min_order,
                                 delta_size=params.delta_size,
                                 discharge=sim_discharge,
                                 momentum=sim_momentum)
        # carved channels + delta land deposition may have changed height
        height = river["height"]
        stats["rivers_s"] = round(time.time() - t, 3)
        stats["river_cells"] = int(river["river_mask"].sum())
        stats["max_strahler"] = river["max_order"]
        _emit("rivers", "River network", height, 0.97)

    t = time.time()
    layers = _dv.derive_layers(height, sea_level=sea_abs, lake_mask=lake_mask,
                               biome_mode=params.biome_mode,
                               fantasy_overlay=params.fantasy_overlay,
                               equator=params.equator, temp_band=params.temp_band,
                               lapse=params.lapse, wind_dir=params.wind_dir,
                               humidity=params.humidity, orographic=params.orographic,
                               aridity=params.aridity,
                               river_mask=(river["river_mask"] if river is not None else None),
                               river_field=(river.get("river_field") if river is not None else None),
                               river_moisture=params.river_moisture,
                               biome_blend=params.biome_blend,
                               mountain_exaggeration=mtn_exagg,
                               alpine_aridity=params.alpine_aridity, seed=params.seed)
    if lake_mask is not None:
        layers["lake_mask"] = lake_mask
        layers["lake_depth"] = lake_depth
    if river is not None:
        layers["acc"] = river["acc"]
        layers["order"] = river["order"]
        layers["river_mask"] = river["river_mask"]
        layers["river_paths"] = river["river_paths"]
        # Continuous water field — only the momentum model renders from it; the
        # droplet model keeps vector polylines (river_field stays absent for it).
        if params.hydrology_model == "momentum":
            layers["river_field"] = river["river_field"]
    stats["derive_s"] = round(time.time() - t, 3)
    stats["land_fraction"] = round(layers["land_fraction"], 3)
    stats["resolution"] = res

    return TerrainResult(params=params, layers=layers, stats=stats)


if __name__ == "__main__":  # pragma: no cover - dev smoke test
    r = generate_terrain(TerrainParams(seed=7, resolution=512))
    print("stats:", r.stats)
    assert np.isfinite(r.layers["height"]).all()
    try:
        from wbworldgen.terrain import render as _rd
        for name, data in (("hillshade", _rd.hillshade_png(r.layers["height"], r.params.sea_level)),
                           ("biome", _rd.biome_png(r.layers["biome"], r.layers["height"],
                                                    r.layers["sea_level"]))):
            with open(f"terrain_{name}.png", "wb") as f:
                f.write(data)
            print(f"wrote terrain_{name}.png ({len(data)} bytes)")
    except ImportError as e:
        print("render skipped:", e)
