"""Modular world-generation package.

Public surface (kept stable for the API layer, CharacterBuilder and tests):

- ``WorldBuilder`` — facade/composition root (starts with an EMPTY pipeline).
- ``PipelineStep`` — legacy-compatible step descriptor.
- ``Step`` / ``register`` / ``build_default_steps`` — the modular step base.
- ``WorldState`` / ``StepResult`` / ``StepContext`` — generation state types.
- ``compile_world`` — pure world-compiler entry point.
- ``register_default_steps`` — register the 10 built-in steps into a builder.

Adding a step: drop a module in ``worldgen/steps/`` decorated with ``@register``
and import it from ``worldgen/steps/__init__.py``. Removing a step: delete the
module and its import. No dispatcher edits required.
"""

from backend.engine.worldgen.base import (
    Step,
    STEP_REGISTRY,
    USES_ENRICHMENT,
    USES_LLM,
    USES_MAP,
    USES_MOCK,
    build_default_steps,
    register,
)
from backend.engine.worldgen.compiler import compile_world
from backend.engine.worldgen.facade import WorldBuilder
from backend.engine.worldgen.types import (
    PipelineStep,
    StepContext,
    StepResult,
    WorldState,
)


def register_default_steps(world_builder: "WorldBuilder") -> "WorldBuilder":
    """Register the default pipeline steps into ``world_builder``.

    Importing ``backend.engine.worldgen.steps`` triggers ``@register`` on every
    built-in step class; we then instantiate and register each one in
    dependency-friendly declaration order.
    """
    import backend.engine.worldgen.steps  # noqa: F401  (registration side effect)

    for step in build_default_steps():
        world_builder.register_step(step)
    return world_builder


__all__ = [
    "WorldBuilder",
    "PipelineStep",
    "Step",
    "STEP_REGISTRY",
    "USES_ENRICHMENT",
    "USES_LLM",
    "USES_MAP",
    "USES_MOCK",
    "build_default_steps",
    "register",
    "register_default_steps",
    "compile_world",
    "StepContext",
    "StepResult",
    "WorldState",
]
