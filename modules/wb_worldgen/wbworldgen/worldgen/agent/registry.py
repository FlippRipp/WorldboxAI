"""The toolbox registry: the fourth capability catalog (C1 of the worldgen
architecture plan).

A *tool* is the agent's unit of action: a catalog entry (P1) with an id, a
label, a description that doubles as the agent's prompt text, a declared
parameter schema, and an async ``invoke``. Adding a tool = dropping a module
in ``tools/`` that calls ``register_tool`` (P2); unknown ids and undeclared
or ill-typed arguments are rejected loudly (P7) with messages shaped for the
agent to read — the C2 harness feeds a ``ToolError`` back to the agent as
the observation it must react to.

Every v1 tool wraps a surface the app already trusts (``generate_step``,
``enrich_run``, the enrichment store, save-step, ``design.py``, the compiled
cache) — the toolbox adds validation and preconditions, never a second
execution path (P5).
"""

from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(Exception):
    """A rejected tool action (unknown tool, bad arguments, unmet
    preconditions, invalid targets). The message is the agent-facing
    observation: name what was wrong AND what to do instead."""


@dataclass
class ToolContext:
    """What a tool invocation runs against: the WorldBuilder facade and the
    world being built. The C2 harness holds one per build."""

    builder: Any
    world_id: str
    #: Optional async event sink long-running tools thread into their inner
    #: runs (enrichment node/phase events stream to the build observer;
    #: they never enter the persisted action log).
    on_event: Any = None


@dataclass(frozen=True)
class ToolSpec:
    """One registered agent tool.

    ``params`` maps argument names to specs::

        {"type": "string"|"integer"|"number"|"boolean"|"list"|"object",
         "description": str,      # rendered into the agent's catalog
         "required": bool,        # default False
         "enum": [...],           # optional allowed values
         "min"/"max": int,        # optional integer/number bounds
         "item_type": str}        # optional element type for lists

    ``invoke`` is ``async (ctx, **args) -> dict``; validated arguments are
    passed as keywords, so optional parameters take their Python defaults.
    ``mutates`` marks tools that change the world (the catalog shows it; the
    C2 harness logs and budgets writes differently from reads).
    """

    id: str
    label: str
    description: str  #: doubles as the agent's catalog/prompt text (P1)
    invoke: Callable = None
    params: dict = field(default_factory=dict)
    mutates: bool = False

    def __post_init__(self):
        if not self.id or not callable(self.invoke):
            raise ValueError(f"ToolSpec {self.id!r}: id and invoke are required")


#: id -> ToolSpec, registration order (tools/__init__ imports the built-ins).
_TOOL_REGISTRY: dict = {}


def register_tool(spec: ToolSpec) -> ToolSpec:
    """Add a tool to the catalog. Duplicate ids fail loudly (P1)."""
    if spec.id in _TOOL_REGISTRY:
        raise ValueError(f"Agent tool '{spec.id}' is already registered")
    _TOOL_REGISTRY[spec.id] = spec
    return spec


def unregister_tool(tool_id: str):
    """Remove a tool (tests registering temporary tools clean up with this)."""
    _TOOL_REGISTRY.pop(tool_id, None)


def get_tool(tool_id: str) -> ToolSpec:
    _ensure_builtins()
    spec = _TOOL_REGISTRY.get(tool_id)
    if spec is None:
        raise ToolError(
            f"Unknown tool '{tool_id}'. Registered tools: "
            f"{', '.join(registered_tool_ids())}")
    return spec


def registered_tool_ids() -> list:
    _ensure_builtins()
    return list(_TOOL_REGISTRY)


def registered_tools() -> list:
    """Every registered spec, registration order."""
    _ensure_builtins()
    return list(_TOOL_REGISTRY.values())


def _ensure_builtins():
    # Same lazy-import idiom as register_default_steps / the catalog: the
    # built-in tools live in a package nothing imports at module load.
    import wbworldgen.worldgen.agent.tools  # noqa: F401


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_args(spec: ToolSpec, args: dict) -> list:
    """Validate an argument dict against a tool's declared parameters.
    Returns human-readable problems (empty = valid); never raises."""
    if not isinstance(args, dict):
        return [f"arguments must be a JSON object, got {type(args).__name__}"]
    params = spec.params or {}
    problems = []
    for name in args:
        if name not in params:
            problems.append(
                f"unknown argument '{name}' (accepted: {', '.join(params) or 'none'})")
    for name, p in params.items():
        if name not in args:
            if p.get("required"):
                problems.append(f"missing required argument '{name}'")
            continue
        value = args[name]
        expected = p.get("type", "string")
        check = _TYPE_CHECKS.get(expected)
        if check is not None and not check(value):
            problems.append(
                f"argument '{name}' must be a {expected}, got "
                f"{type(value).__name__} ({value!r})")
            continue
        if p.get("enum") is not None and value not in p["enum"]:
            problems.append(
                f"argument '{name}' must be one of {p['enum']}, got {value!r}")
        if expected in ("integer", "number"):
            if p.get("min") is not None and value < p["min"]:
                problems.append(f"argument '{name}' must be >= {p['min']}, got {value!r}")
            if p.get("max") is not None and value > p["max"]:
                problems.append(f"argument '{name}' must be <= {p['max']}, got {value!r}")
        if expected == "list" and p.get("item_type"):
            item_check = _TYPE_CHECKS.get(p["item_type"])
            if item_check is not None:
                bad = [v for v in value if not item_check(v)]
                if bad:
                    problems.append(
                        f"argument '{name}' must be a list of {p['item_type']}s; "
                        f"invalid entries: {bad!r}")
    return problems


async def invoke_tool(ctx: ToolContext, tool_id: str, args: dict = None) -> dict:
    """Look up, validate and run one tool action. Raises ``ToolError`` for
    everything the agent can correct (unknown tool, bad args, and whatever
    the tool itself rejects); harness-level faults (no such world, no LLM
    wired) propagate as their own exception types."""
    _ensure_builtins()
    spec = get_tool(tool_id)
    problems = validate_args(spec, args or {})
    if problems:
        raise ToolError(f"{tool_id}: " + "; ".join(problems))
    return await spec.invoke(ctx, **(args or {}))


def describe_tools() -> list:
    """Catalog slice of the registered tools (see worldgen/catalog.py): one
    self-describing entry per tool, registration order."""
    return [
        {"kind": "tool", "id": s.id, "label": s.label,
         "description": s.description, "mutates": s.mutates,
         "params": {name: dict(p) for name, p in (s.params or {}).items()}}
        for s in registered_tools()
    ]
