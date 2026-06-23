"""Backwards-compatible shim.

The world-generation system was refactored into the modular
``backend.engine.worldgen`` package. This module is kept as a thin re-export so
existing imports (``from backend.engine.world_builder import WorldBuilder,
PipelineStep``) and tests continue to work unchanged.

New code should import from ``backend.engine.worldgen`` directly.
"""

from backend.engine.worldgen import (  # noqa: F401
    PipelineStep,
    StepContext,
    StepResult,
    WorldBuilder,
    WorldState,
    compile_world,
    register_default_steps,
)

__all__ = [
    "WorldBuilder",
    "PipelineStep",
    "WorldState",
    "StepResult",
    "StepContext",
    "compile_world",
    "register_default_steps",
]
