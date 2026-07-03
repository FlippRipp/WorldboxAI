"""Unregistered pipeline-step wrapper for terrain generation.

This documents the *eventual* shape of integrating terrain into the real
world-generation pipeline, but is intentionally **NOT** imported by
``wbworldgen.worldgen.steps.__init__`` and therefore never enters
``STEP_REGISTRY`` / ``build_default_steps()``. The live pipeline is unaffected.

When we are happy with the experimental output, promotion is:
  1. add ``from wbworldgen.terrain.step import TerrainHeightmapStep`` to
     ``steps/__init__`` and decorate the class with ``@register``;
  2. fill in ``contribute_to_compiled`` so downstream steps (e.g. map node /
     city placement) can read the height/slope/biome layers.
"""

import numpy as np

from wbworldgen.worldgen.base import Step
from wbworldgen.terrain.pipeline import TerrainParams, generate_terrain


# NOTE: deliberately NOT decorated with @register.
class TerrainHeightmapStep(Step):
    id = "terrain_heightmap"
    label = "Terrain Heightmap (experimental)"
    description = (
        "Procedural 2D terrain: fractal heightmap, noise-based mountain chains, "
        "thermal + hydraulic erosion. Produces height/slope/biome layers."
    )
    after = None
    uses = "map"  # pure procedural; no LLM
    schema = {
        "seed": {"type": "number", "label": "Seed", "default": 1},
        "resolution": {"type": "number", "label": "Resolution", "min": 64, "max": 1024, "default": 512},
        "mountain_strength": {"type": "number", "label": "Mountain Height", "min": 0, "max": 3.0, "default": 0.6},
        "mountain_coverage": {"type": "number", "label": "Mountain Coverage", "min": 0, "max": 1, "default": 0.5},
        "droplets": {"type": "number", "label": "Erosion Droplets", "min": 0, "max": 300000, "default": 60000},
        "sea_level": {"type": "number", "label": "Sea Level", "min": 0.0, "max": 1.0, "default": 0.4},
    }

    async def generate(self, ctx) -> dict:
        cfg = ctx.config or {}
        params = TerrainParams.from_dict(cfg)
        result = generate_terrain(params)
        # Step data must be JSON-serializable: store params + stats inline, and
        # leave the heavy arrays to the persistence layer / API (see the
        # experimental API which writes height.npy + preview PNGs to disk).
        layers = result.layers
        return {
            "params": params.to_dict(),
            "stats": result.stats,
            "summary": {
                "land_fraction": result.stats.get("land_fraction"),
                "biome_histogram": _biome_histogram(layers["biome"]),
            },
        }

    # def contribute_to_compiled(self, steps_data, compiled):
    #     """FUTURE: expose terrain layers so map/city placement can sample
    #     height, slope and biome when choosing node positions."""
    #     ...


def _biome_histogram(biome: np.ndarray) -> dict:
    ids, counts = np.unique(biome, return_counts=True)
    return {int(i): int(c) for i, c in zip(ids, counts)}
