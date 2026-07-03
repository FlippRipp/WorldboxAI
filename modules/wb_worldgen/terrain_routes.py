"""Experimental terrain-generation API (kept separate from world gen).

Endpoints:
  POST /api/terrain/generate          -> run generate_terrain, persist artifacts
  GET  /api/terrain/{run_id}/{image}  -> serve a stored preview PNG

Artifacts live under ``data/terrain_experiments/<run_id>/``:
  height.npy, hillshade.png, biome.png, params.json
"""

import asyncio
import json
import os
import pickle
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import numpy as np

from wbworldgen.terrain.pipeline import TerrainParams, generate_terrain
from wbworldgen.terrain.caves import CaveParams, generate_cave_terrain
from wbworldgen.terrain import render as _render
from wbworldgen.terrain import derive as _derive
from wbworldgen.terrain import biomes as _biomes

router = APIRouter(prefix="/api/terrain", tags=["terrain"])

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
_EXP_DIR = os.path.join(_BASE, "terrain_experiments")

_IMAGES = {"hillshade": "hillshade.png", "biome": "biome.png",
           "elevation": "elevation.png", "temperature": "temperature.png",
           "moisture": "moisture.png", "cave": "cave.png"}


class TerrainGenerateRequest(BaseModel):
    seed: int = -1
    resolution: int = 512
    octaves: int = 6
    warp: float = 0.35
    island: float = 0.5
    mountain_strength: float = 0.85
    mountain_coverage: float = 0.5
    mountain_sharpness: float = 2.5
    spline_ridges: int = 0
    spline_ridge_strength: float = 0.5
    spline_ridge_width: float = 0.12
    spline_ridge_length: float = 1.0
    redistribution: float = 2.2
    thermal_iterations: int = 20
    droplets: int = 60000
    erosion_strength: float = 1.0
    erosion_backend: str = "auto"
    hydrology_model: str = "momentum"
    momentum_iterations: int = 60
    momentum_particles: int = 8000
    momentum_transfer: float = 0.8
    discharge_alpha: float = 0.4
    sea_level: float = 0.4
    rivers: bool = True
    river_density: float = 0.5
    river_carve: float = 0.02
    river_meander: float = 1.2
    lakes: bool = True
    lake_min_area: int = 50
    lake_min_depth: float = 0.012
    breach: bool = True
    breach_max_depth: float = 0.05
    deltas: bool = True
    delta_min_order: int = 3
    delta_size: float = 1.0
    craters: bool = False
    crater_count: int = 5
    crater_min_radius: float = 0.02
    crater_max_radius: float = 0.08
    crater_depth: float = 0.15
    crater_rim_height: float = 0.05
    crater_ejecta_falloff: float = 3.0
    crater_age: str = "ancient"
    coastal_smooth: bool = False
    coastal_smooth_width: float = 0.08
    coastal_smooth_strength: float = 1.0
    biome_mode: str = "realistic"
    fantasy_overlay: bool = False
    equator: float = 0.5
    temp_band: float = 1.0
    lapse: float = 0.6
    wind_dir: float = 270.0
    humidity: float = 1.0
    orographic: float = 1.0
    aridity: float = 0.0
    alpine_aridity: float = 0.6
    rock_line: float = 0.45
    snow_line: float = 0.72
    alpine_blend: float = 0.23
    river_moisture: float = 0.3
    forests: bool = True
    forest_density: float = 0.5
    biome_blend: float = 0.7
    relief: float = 16.0
    hillshade_strength: float = 1.5
    # Live colour-editor overrides: biome id -> [r, g, b]. Applied on top of the
    # selected palette at render time (does not touch the source palette).
    colors: Optional[dict] = None


class BiomeRederiveRequest(BaseModel):
    """Biome/climate + colour subset for the fast re-derive path: recompute biomes
    on an already-eroded terrain (no heightmap/erosion/rivers) from a stored run."""
    biome_mode: str = "realistic"
    fantasy_overlay: bool = False
    biome_blend: float = 0.7
    equator: float = 0.5
    temp_band: float = 1.0
    lapse: float = 0.6
    wind_dir: float = 270.0
    humidity: float = 1.0
    orographic: float = 1.0
    aridity: float = 0.0
    alpine_aridity: float = 0.6
    rock_line: float = 0.45
    snow_line: float = 0.72
    alpine_blend: float = 0.23
    river_moisture: float = 0.3
    forests: bool = True
    forest_density: float = 0.5
    relief: float = 16.0
    hillshade_strength: float = 1.5
    seed: int = 0
    colors: Optional[dict] = None


class CaveGenerateRequest(BaseModel):
    """Underground/cave generation params (mirrors ``caves.CaveParams``)."""
    seed: int = -1
    resolution: int = 384
    cavern_density: float = 0.22
    cavern_size: float = 0.5
    tunnel_width: float = 0.5
    tunnel_windiness: float = 0.5
    extra_tunnels: float = 0.4
    ca_iterations: int = 4
    fault_strength: float = 0.6
    erosion_amount: float = 0.4
    water_level: float = 0.28
    lava_amount: float = 0.5
    crystal_amount: float = 0.5
    ice_amount: float = 0.3
    biome_blend: float = 0.6
    river_density: float = 0.5
    relief: float = 14.0
    hillshade_strength: float = 1.6
    terrace_steps: int = 6
    ssao_strength: float = 0.6


def _run_and_persist(params: TerrainParams, on_frame=None,
                     color_overrides: dict = None) -> dict:
    result = generate_terrain(params, on_frame=on_frame)
    layers = result.layers
    height = layers["height"]
    # Effective absolute sea threshold (sea_level is a target ocean fraction).
    sea = layers["sea_level"]
    order = layers.get("order")
    paths = layers.get("river_paths")
    lake_mask = layers.get("lake_mask")
    lake_depth = layers.get("lake_depth")
    river_field = layers.get("river_field")  # momentum model: continuous water

    run_id = uuid.uuid4().hex[:12]
    out_dir = os.path.join(_EXP_DIR, run_id)
    os.makedirs(out_dir, exist_ok=True)

    # Mountain Height slider's 1..3 band amplifies how big mountains read
    # (relief / shading / rock+snow tint) without adding geometry.
    ex = _render.mountain_exaggeration(params.mountain_strength)
    z = params.relief * (1.0 + 0.6 * ex)
    hs = params.hillshade_strength * (1.0 + 0.15 * ex)
    rf, dens = river_field, params.river_density
    np.save(os.path.join(out_dir, "height.npy"), height.astype(np.float32))
    with open(os.path.join(out_dir, "hillshade.png"), "wb") as f:
        f.write(_render.hillshade_png(height, sea, order, paths, lake_mask, lake_depth,
                                      z_scale=z, river_field=rf, density=dens, hillshade_strength=hs))
    with open(os.path.join(out_dir, "biome.png"), "wb") as f:
        f.write(_render.biome_png(layers["biome"], height, sea, order, paths, lake_mask, lake_depth,
                                  z_scale=z, river_field=rf, density=dens, hillshade_strength=hs,
                                  biome_mode=params.biome_mode, forests=params.forests,
                                  forest_density=params.forest_density,
                                  biome_blend=params.biome_blend,
                                  temperature=layers.get("temperature"),
                                  moisture=layers.get("moisture"), seed=params.seed,
                                  mountain_exaggeration=ex,
                                  rock_line=params.rock_line, snow_line=params.snow_line,
                                  alpine_blend=params.alpine_blend,
                                  color_overrides=color_overrides))
    with open(os.path.join(out_dir, "elevation.png"), "wb") as f:
        f.write(_render.elevation_png(height, sea, order, paths, lake_mask, lake_depth,
                                      z_scale=z, river_field=rf, density=dens, hillshade_strength=hs))
    with open(os.path.join(out_dir, "temperature.png"), "wb") as f:
        f.write(_render.temperature_png(layers["temperature"], height, sea,
                                        z_scale=z, hillshade_strength=hs))
    with open(os.path.join(out_dir, "moisture.png"), "wb") as f:
        f.write(_render.moisture_png(layers["moisture"], height, sea,
                                     water=layers.get("water"),
                                     z_scale=z, hillshade_strength=hs))
    with open(os.path.join(out_dir, "params.json"), "w", encoding="utf-8") as f:
        json.dump({"params": params.to_dict(), "stats": result.stats}, f, indent=2)

    # Freeze the post-erosion terrain state so biomes can be re-derived later
    # without re-running the expensive heightmap/erosion/river stages (see the
    # POST /{run_id}/biomes endpoint). mountain_strength lets us recompute the
    # exaggeration factor exactly as the pipeline does.
    with open(os.path.join(out_dir, "terrain.pkl"), "wb") as f:
        pickle.dump({
            "height": height.astype(np.float32),
            "sea_level": sea,
            "lake_mask": lake_mask,
            "lake_depth": lake_depth,
            "river_mask": layers.get("river_mask"),
            "river_field": river_field,
            "order": order,
            "river_paths": paths,
            "mountain_strength": params.mountain_strength,
        }, f)

    return {
        "run_id": run_id,
        "stats": result.stats,
        "params": params.to_dict(),
        "images": {
            "hillshade": f"/api/terrain/{run_id}/hillshade",
            "biome": f"/api/terrain/{run_id}/biome",
            "elevation": f"/api/terrain/{run_id}/elevation",
            "temperature": f"/api/terrain/{run_id}/temperature",
            "moisture": f"/api/terrain/{run_id}/moisture",
        },
    }


@router.post("/generate")
async def generate(req: TerrainGenerateRequest):
    params = TerrainParams.from_dict(req.model_dump())
    try:
        return await run_in_threadpool(_run_and_persist, params,
                                       color_overrides=req.colors)
    except Exception as e:  # surface generation failures clearly to the lab UI
        raise HTTPException(status_code=500, detail=f"terrain generation failed: {e}")


@router.post("/generate/stream")
async def generate_stream(req: TerrainGenerateRequest):
    """Same as /generate but streams work-in-progress preview frames as
    Server-Sent Events while the terrain is built. Each ``data:`` line is a JSON
    object: ``{type:"frame", stage, label, frac, image(base64 png), ...}`` during
    generation, then a terminal ``{type:"done", ...}`` carrying the same payload
    as /generate (run_id + full-resolution image URLs), or ``{type:"error"}``."""
    params = TerrainParams.from_dict(req.model_dump())
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_frame(frame: dict):
        # Called from the worker thread — hand the frame to the event loop.
        loop.call_soon_threadsafe(queue.put_nowait, {"type": "frame", **frame})

    def work():
        try:
            payload = _run_and_persist(params, on_frame=on_frame,
                                       color_overrides=req.colors)
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "done", **payload})
        except Exception as e:  # surface failures to the lab UI as a stream event
            loop.call_soon_threadsafe(queue.put_nowait,
                                      {"type": "error", "detail": str(e)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)  # end sentinel

    loop.run_in_executor(None, work)

    async def event_stream():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield "data: " + json.dumps(item) + "\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _run_cave_and_persist(params: CaveParams) -> dict:
    """Generate an underground/cave map, render it, and persist under a run id.

    Mirrors :func:`_run_and_persist` for the surface, but caves carve fast and
    have no erosion stages, so there's no streaming or frozen-state re-derive —
    just the single top-down ``cave.png``."""
    result = generate_cave_terrain(params)
    layers = result.layers

    run_id = uuid.uuid4().hex[:12]
    out_dir = os.path.join(_EXP_DIR, run_id)
    os.makedirs(out_dir, exist_ok=True)

    seed = int(result.params.seed)
    with open(os.path.join(out_dir, "cave.png"), "wb") as f:
        f.write(_render.cave_png(layers, z_scale=params.relief,
                                 hillshade_strength=params.hillshade_strength,
                                 seed=seed, terrace_steps=params.terrace_steps,
                                 ssao_strength=params.ssao_strength))
    with open(os.path.join(out_dir, "params.json"), "w", encoding="utf-8") as f:
        json.dump({"params": result.params.to_dict(), "stats": result.stats},
                  f, indent=2)

    return {
        "run_id": run_id,
        "stats": result.stats,
        "params": result.params.to_dict(),
        "images": {"cave": f"/api/terrain/{run_id}/cave"},
    }


@router.post("/cave/generate")
async def generate_cave(req: CaveGenerateRequest):
    params = CaveParams.from_dict(req.model_dump())
    try:
        return await run_in_threadpool(_run_cave_and_persist, params)
    except Exception as e:  # surface cave-generation failures clearly to the lab UI
        raise HTTPException(status_code=500, detail=f"cave generation failed: {e}")


def _rederive_biomes(run_id: str, req: BiomeRederiveRequest) -> dict:
    """Recompute biomes (+ climate + colours) on an already-eroded terrain stored
    under ``run_id`` and re-render only the biome/temperature/moisture PNGs. Reuses
    the frozen ``terrain.pkl`` so none of the expensive erosion/river work re-runs."""
    safe_id = "".join(c for c in run_id if c.isalnum())
    out_dir = os.path.join(_EXP_DIR, safe_id)
    frozen_path = os.path.join(out_dir, "terrain.pkl")
    if not os.path.isfile(frozen_path):
        raise FileNotFoundError("no stored terrain for this run — run Generate first")
    with open(frozen_path, "rb") as f:
        st = pickle.load(f)

    height = np.asarray(st["height"], dtype=np.float64)
    sea = float(st["sea_level"])
    lake_mask = st.get("lake_mask")
    lake_depth = st.get("lake_depth")
    river_mask = st.get("river_mask")
    river_field = st.get("river_field")
    order = st.get("order")
    paths = st.get("river_paths")

    # Match the pipeline's visual mountain exaggeration and the render scaling
    # applied in _run_and_persist.
    ex = _render.mountain_exaggeration(float(st.get("mountain_strength", 0.85)))
    z = req.relief * (1.0 + 0.6 * ex)
    hs = req.hillshade_strength * (1.0 + 0.15 * ex)

    # River-field overlay density isn't a biome param; pull it from the saved run.
    density = 0.5
    try:
        with open(os.path.join(out_dir, "params.json"), encoding="utf-8") as f:
            density = float(json.load(f).get("params", {}).get("river_density", 0.5))
    except (OSError, ValueError, KeyError):
        pass

    layers = _derive.derive_layers(
        height, sea_level=sea, lake_mask=lake_mask, biome_mode=req.biome_mode,
        fantasy_overlay=req.fantasy_overlay, equator=req.equator,
        temp_band=req.temp_band, lapse=req.lapse, wind_dir=req.wind_dir,
        humidity=req.humidity, orographic=req.orographic, aridity=req.aridity,
        river_mask=river_mask,
        river_field=river_field, river_moisture=req.river_moisture,
        biome_blend=req.biome_blend, mountain_exaggeration=ex,
        alpine_aridity=req.alpine_aridity, seed=req.seed)

    with open(os.path.join(out_dir, "biome.png"), "wb") as f:
        f.write(_render.biome_png(layers["biome"], height, sea, order, paths, lake_mask,
                                  lake_depth, z_scale=z, river_field=river_field,
                                  density=density, hillshade_strength=hs,
                                  biome_mode=req.biome_mode, forests=req.forests,
                                  forest_density=req.forest_density,
                                  biome_blend=req.biome_blend,
                                  temperature=layers.get("temperature"),
                                  moisture=layers.get("moisture"), seed=req.seed,
                                  mountain_exaggeration=ex,
                                  rock_line=req.rock_line, snow_line=req.snow_line,
                                  alpine_blend=req.alpine_blend,
                                  color_overrides=req.colors))
    with open(os.path.join(out_dir, "temperature.png"), "wb") as f:
        f.write(_render.temperature_png(layers["temperature"], height, sea,
                                        z_scale=z, hillshade_strength=hs))
    with open(os.path.join(out_dir, "moisture.png"), "wb") as f:
        f.write(_render.moisture_png(layers["moisture"], height, sea,
                                     water=layers.get("water"),
                                     z_scale=z, hillshade_strength=hs))

    # New cache-bust token: the run_id is unchanged but the images just changed.
    rev = uuid.uuid4().hex[:12]
    return {
        "run_id": safe_id,
        "rev": rev,
        "land_fraction": round(float(layers["land_fraction"]), 3),
        "images": {
            "biome": f"/api/terrain/{safe_id}/biome",
            "temperature": f"/api/terrain/{safe_id}/temperature",
            "moisture": f"/api/terrain/{safe_id}/moisture",
        },
    }


@router.post("/{run_id}/biomes")
async def rederive_biomes(run_id: str, req: BiomeRederiveRequest):
    """Fast biome-only re-derive: reuse the stored eroded terrain, recompute biomes
    + climate + colours, re-render the biome/temperature/moisture views."""
    try:
        return await run_in_threadpool(_rederive_biomes, run_id, req)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # surface re-derive failures clearly to the lab UI
        raise HTTPException(status_code=500, detail=f"biome re-derive failed: {e}")


@router.get("/palette")
async def get_palette():
    """Editable biome palettes (realistic + fantasy) for the lab's colour editor:
    each entry is ``{id, name, color:[r,g,b]}`` straight from the source palette."""
    def _entries(mode: str):
        return [{"id": bid, "name": name, "color": list(color)}
                for bid, (name, color, _tree) in _biomes.biome_table(mode).items()]
    return {"realistic": _entries("realistic"), "fantasy": _entries("fantasy")}


@router.get("/{run_id}/{image}")
async def get_image(run_id: str, image: str):
    fname = _IMAGES.get(image)
    if not fname:
        raise HTTPException(status_code=404, detail="unknown image")
    # Guard against path traversal via run_id.
    safe_id = "".join(c for c in run_id if c.isalnum())
    path = os.path.join(_EXP_DIR, safe_id, fname)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-cache"})
