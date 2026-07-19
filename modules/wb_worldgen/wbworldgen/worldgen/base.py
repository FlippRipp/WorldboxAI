"""Step base class + a global registry for self-registering step modules.

A *step* is a free-standing unit: its own module declares a ``Step`` subclass
with metadata (id/label/description/after), a ``schema`` (which drives both the
LLM prompt and the frontend form) and optional ``guidance``. Declarative steps
need nothing else — the pipeline dispatches generation by strategy. A step may
optionally override ``async generate(self, ctx)`` for bespoke generation and
``contribute_to_compiled(steps_data, compiled)`` to fold custom data into the
compiled world.

Adding a step = drop a module in ``steps/`` and decorate it with ``@register``.
Removing a step = delete the module. No shared dispatcher edits required.
"""

import copy
from typing import Any, Optional

#: Strategy hints used by the orchestrator to pick a default generator.
USES_LLM = "llm"
USES_MAP = "map"
USES_ENRICHMENT = "enrichment"
USES_MOCK = "mock"


class Step:
    id: str = ""
    label: str = ""
    description: str = ""
    after: Optional[str] = None
    schema: dict[str, Any] = {}
    #: Declared generation-config contract: the keys ``generate`` actually
    #: consumes (``schema`` above describes the step's OUTPUT fields — the
    #: two are different things). Same per-key spec shape as agent tool
    #: params ({"type", "description", "min"/"max", "enum", ...}); an empty
    #: dict means the step takes no config. The run_step tool validates
    #: against this loudly (P7 at the config layer — the Ecstasy Veil live
    #: run lost a finished root map to an invented, silently-ignored
    #: config), and the catalog renders it so the agent picks real keys.
    config_schema: dict[str, Any] = {}
    guidance: str = ""
    uses: str = USES_LLM
    #: Declared data contract (B3): names of the world artifacts this step
    #: consumes and contributes ("hierarchy", "maps", "labels", ...).
    #: ``requires`` lists only HARD needs — artifacts without which the step
    #: is meaningless — never soft prompt-context reads, so a dependency
    #: check can run against any effective (post-dynamic-skips) step list
    #: today's wizard legitimately produces. Execution order stays ``after``'s
    #: business; the checker (worldgen/catalog.py) is the C1 executor's.
    requires: tuple = ()
    produces: tuple = ()
    #: Legacy-compatible field; a real callable override shadows this.
    generate = None

    def __init__(self):
        # Per-instance copies so runtime schema extension (module hooks) does
        # not mutate the class-level definition shared across instances.
        self.id = type(self).id
        self.label = type(self).label
        self.description = type(self).description
        self.after = type(self).after
        self.schema = copy.deepcopy(type(self).schema)
        self.config_schema = copy.deepcopy(type(self).config_schema)
        self.guidance = type(self).guidance
        self.uses = type(self).uses
        self.requires = tuple(type(self).requires)
        self.produces = tuple(type(self).produces)

    def to_frontend(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "after": self.after,
            "schema": self.schema,
        }

    def context_view(self, data):
        """The view of this step's data that downstream steps see in their
        prompts. Override to trim payloads that only exist for the UI or for
        procedural steps (raster metadata, structured summaries, ...)."""
        return data

    def view_for(self, world_state: dict) -> "Step":
        """The step as it applies to *this* world. Override to return a clone
        with a trimmed schema/guidance when a field only makes sense under
        certain world designs (the class-level schema stays the maximal one
        the frontend sees). The orchestrator applies this view to generation
        and per-item rerolls; the default is the step itself."""
        return self


#: Ordered list of registered step classes (registration order preserved).
STEP_REGISTRY: list[type] = []


def register(cls):
    """Class decorator that adds a Step subclass to the global registry."""
    if cls not in STEP_REGISTRY:
        STEP_REGISTRY.append(cls)
    return cls


def describe_steps() -> list[dict]:
    """Catalog slice of the registered steps (see worldgen/catalog.py): one
    self-describing entry per step, registration order. Callers that need
    the built-ins present must import ``wbworldgen.worldgen.steps`` first
    (the catalog does)."""
    return [
        {"kind": "step", "id": cls.id, "label": cls.label,
         "description": cls.description, "after": cls.after, "uses": cls.uses,
         "requires": list(cls.requires), "produces": list(cls.produces),
         "config": {name: dict(spec)
                    for name, spec in (cls.config_schema or {}).items()}}
        for cls in STEP_REGISTRY
    ]


def build_default_steps() -> list:
    """Instantiate every registered step class (importing the steps package
    triggers registration)."""
    return [cls() for cls in STEP_REGISTRY]
