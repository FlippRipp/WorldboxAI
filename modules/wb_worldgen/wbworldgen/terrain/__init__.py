"""Experimental 2D terrain generation (heightmap -> mountains -> erosion).

This package is deliberately kept **separate** from ``wbworldgen.worldgen``.
It is reached only through the experimental "Experimental World Visualization"
menu / API and is NOT registered in the default world-generation pipeline. The
intent is to tune the procedural terrain in isolation, then later promote it to
a real pipeline step (the unregistered ``step.TerrainHeightmapStep`` documents
that future shape).

Public entry point: :func:`pipeline.generate_terrain`.
"""

from wbworldgen.terrain.pipeline import TerrainParams, TerrainResult, generate_terrain

__all__ = ["TerrainParams", "TerrainResult", "generate_terrain"]
