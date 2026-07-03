"""Diagnostic tool: detect rapid biome-colour changes (sharp seams) in a render.

The biome renderer blends biomes in *climate space* (see
``render._climate_blend_base``) so neighbouring biomes melt together using the
temperature/moisture fields. This tool measures, objectively, how sharp the
remaining biome boundaries are — so blend tuning can be judged by numbers instead
of by eye (the human/LLM eye is unreliable at spotting subtle pixel-scale seams).

It operates on the biome *hue field* (the climate-blend base, BEFORE relief and
canopy texture) so the signal is biome-boundary sharpness rather than hillshade or
forest mottle noise. Coastlines, rivers and lakes are legitimately sharp and are
masked out.

Usage:
    python tools/biome_sharpness.py [LAYERS_NPZ]
        LAYERS_NPZ : path to a terrain ``layers.npz`` (as saved by terrain_store).
                     If omitted, a small terrain is generated on the fly.

Outputs a percentile breakdown of adjacent-pixel colour deltas, the biome
transitions responsible for the sharpest seams, and a heatmap PNG next to the
input (red = sharp). Lower percentiles / fewer high-delta pixels = smoother.
"""
import os
import sys

import numpy as np
from scipy.ndimage import binary_dilation

# Allow running as `python tools/biome_sharpness.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.engine.terrain import render, biomes as bm  # noqa: E402

# Colour-delta (Euclidean RGB) above which a land boundary counts as a "seam".
SEAM_THRESHOLD = 40.0


def load_layers(path):
    """Load the arrays we need from a terrain ``layers.npz`` (or generate one)."""
    if path:
        z = np.load(path, allow_pickle=True)
        return {k: z[k] for k in z.files}, os.path.dirname(os.path.abspath(path))
    print("No layers.npz given — generating a small terrain (seed 11)...")
    from backend.engine.terrain import pipeline as P
    res = P.generate_terrain(P.TerrainParams(resolution=256, seed=11))
    return res.layers, os.getcwd()


def color_delta(base):
    """Per-pixel max Euclidean RGB delta to any 8-neighbour, plus that neighbour's
    offset (so the biome pair across the sharpest edge can be identified)."""
    H, W = base.shape[:2]
    grad = np.zeros((H, W))
    arg = np.zeros((H, W, 2), int)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            shifted = np.roll(np.roll(base, dy, 0), dx, 1)
            d = np.sqrt(((base - shifted) ** 2).sum(-1))
            upd = d > grad
            grad[upd] = d[upd]
            arg[upd] = (dy, dx)
    return grad, arg


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    layers, out_dir = load_layers(path)
    biome = np.asarray(layers["biome"]).astype(int)
    H, W = biome.shape
    mode = str(layers.get("biome_mode", "realistic"))
    if mode not in ("realistic", "fantasy"):
        mode = "realistic"
    names = {bid: n for bid, (n, _c, _t) in bm.biome_table(mode).items()}

    # Biome hue field exactly as the renderer builds it (climate blend + specials).
    blend = float(layers.get("biome_blend", 0.7))
    base = render._climate_blend_base(layers["temperature"], layers["moisture"],
                                      biome, mode, blend, int(layers.get("seed", 0)))
    grad, arg = color_delta(base)

    # Mask out legitimately-sharp water boundaries: ocean/ice/lakes/rivers and a
    # 2px dilation (the coastline itself is meant to be crisp).
    water = (biome == bm.OCEAN) | (biome == bm.ICE)
    if layers.get("lake_mask") is not None:
        water = water | np.asarray(layers["lake_mask"]).astype(bool)
    rf = layers.get("river_field")
    if rf is not None and np.asarray(rf).shape == biome.shape:
        water = water | (np.asarray(rf) > 0.05)
    elif layers.get("river_mask") is not None:
        water = water | np.asarray(layers["river_mask"]).astype(bool)
    land = ~binary_dilation(water, iterations=2)
    g = grad[land]
    n = g.size

    print("\n=== Biome boundary sharpness ===")
    print(f"  resolution {H}x{W}, palette '{mode}', biome_blend {blend:.2f}")
    print(f"  {n} land pixels analysed (water/coast/rivers excluded)\n")
    for p in (50, 90, 99, 99.9):
        print(f"  p{p:<5} colour-delta = {np.percentile(g, p):6.2f}")
    print(f"  max          = {g.max():6.2f}")
    for thr in (SEAM_THRESHOLD, 70, 100):
        c = int((g > thr).sum())
        print(f"  pixels > {thr:5.0f} delta : {c:6d}  ({100 * c / n:.3f}%)")

    # Rank the biome transitions causing the flagged seams.
    from collections import Counter
    flagged = land & (grad > SEAM_THRESHOLD)
    ys, xs = np.where(flagged)
    pairs = Counter()
    for y, x in zip(ys, xs):
        dy, dx = arg[y, x]
        a, b = int(biome[y, x]), int(biome[(y + dy) % H, (x + dx) % W])
        if a != b:
            pairs[tuple(sorted((a, b)))] += float(grad[y, x])
    print("\n  Sharpest biome transitions (summed delta over flagged pixels):")
    if not pairs:
        print("    (none above threshold — boundaries are smooth)")
    for (a, b), tot in pairs.most_common(10):
        print(f"    {names.get(a, a):>22} <-> {names.get(b, b):<22}  {tot:8.0f}")

    # Heatmap (red = sharp) for a visual cross-check.
    try:
        from PIL import Image
        hm = np.zeros((H, W, 3), np.uint8)
        hm[..., 0] = (np.clip(grad / 100.0, 0, 1) * land * 255).astype(np.uint8)
        dst = os.path.join(out_dir, "biome_sharpness_heat.png")
        Image.fromarray(hm).resize((max(512, W), max(512, H)),
                                   Image.NEAREST).save(dst)
        print(f"\n  Heatmap written: {dst}")
    except Exception as e:  # pragma: no cover - heatmap is best-effort
        print(f"\n  (heatmap skipped: {e})")


if __name__ == "__main__":
    main()
