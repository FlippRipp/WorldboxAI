"""Enrichment pass registry: the catalog of node/map LLM passes.

B1 of the worldgen architecture plan: label, describe and review stop being
hardcoded engine phases and become registered ``PassSpec`` entries the
engine schedules. A pass is a catalog entry (P1): id, label, a description
that doubles as the planner's selection text, a declared work unit and
contracts. Adding a pass = dropping a module in ``passes/`` that calls
``register_pass`` (P2); unknown ids fail loudly (P7).

The engine owns everything genuinely shared — pending computation (normal /
rework / scoped runs), importance ordering, concurrency, batching, retries,
flush cadence, progress events. A pass therefore does not select its own
work items; it declares two predicates (``is_done``, ``in_domain``) and the
engine derives the work queue and progress arithmetic from them uniformly.
(The plan sketched a per-pass ``selector`` callable; that conflicted with
the same decision's "engine keeps per-unit pending computation" and was
resolved in the engine's favor.)
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


def _always(node: dict) -> bool:
    return True


async def _noop_emit(evt: dict):
    return None


@dataclass
class RunState:
    """Shared per-run state handed to every pass invocation.

    ``all_nodes`` is the run's canonical node list (per-node copies tagged
    with map ids); the engine mutates it in place as work lands so later
    calls see fresh neighbor names without re-loading the world. ``emit``
    is the engine's safe event callback — passes may use it for custom
    events (review emits ``review_fix``); it never raises.
    """

    world_id: str
    compiled: dict
    all_nodes: list
    layer_map: dict
    rework: bool = False
    emit: Callable = _noop_emit


@dataclass(frozen=True)
class PassSpec:
    """One registered enrichment pass.

    Node passes (``unit="node"``): ``run(services, node, state)`` performs
    one unit of LLM work and returns a ``{field: value}`` dict — the engine
    stores truthy fields (enrichment store + compiled cache + the in-memory
    node) — or ``None`` on an already-logged failure. Map passes
    (``unit="map"``): ``run(services, map_record, state)`` handles one whole
    map, does its own storage, and returns a summary-contribution dict
    (numbers add, lists extend across maps) — zeros when the map was
    skipped, ``None`` only when nothing should be aggregated.
    """

    id: str
    label: str
    description: str  #: doubles as the planner-catalog selection text (B2)
    unit: str = "node"  #: what one work item is: "node" | "map"
    run: Callable = None
    #: Node passes: does this node already carry the pass's output?
    #: Also drives per-layer progress counts and map-completion triggers.
    is_done: Callable = None
    #: Node passes: is the node eligible at all (the progress denominator —
    #: describe only ever applies to named nodes).
    in_domain: Callable = _always
    #: Pass ids that must run before this one when several run in one call
    #: (phase="all" resolves to the topological order over these).
    after: tuple = ()
    #: ``{"on_map_complete": "<pass_id>"}`` — run this map pass over each
    #: map the named pass fully completes during a run, instead of as a
    #: standalone phase. Triggered passes are excluded from phase="all" but
    #: still runnable explicitly (phase="review").
    triggers: Optional[dict] = None
    #: Declared data contract (B3): world artifacts this pass consumes and
    #: contributes ("maps", "labels", ...). Hard needs only — the C1
    #: executor's dependency check reads these; ordering *within* enrichment
    #: stays ``after``'s business.
    requires: tuple = ()
    produces: tuple = ()
    #: May share one LLM call across several units (label batching).
    batchable: bool = False
    #: Batchable passes: ``run_batch(services, batch, state)`` returns
    #: ``(results, leftovers)`` — results maps node_id -> field updates,
    #: leftovers are nodes to re-run as single-node calls.
    run_batch: Callable = None
    #: Map stored field updates onto SSE node-event fields (label stores
    #: ``name`` but emits ``label``). Default: identity.
    event_fields: Callable = None
    #: Key for this pass's counter in run summaries. The built-ins keep the
    #: legacy names ("labeled", "described", "review"); default: the id.
    summary_key: str = None

    def __post_init__(self):
        if self.unit not in ("node", "map"):
            raise ValueError(f"PassSpec {self.id!r}: unit must be 'node' or 'map', got {self.unit!r}")
        if not self.id or not callable(self.run):
            raise ValueError(f"PassSpec {self.id!r}: id and run are required")
        if self.unit == "node" and not callable(self.is_done):
            raise ValueError(f"PassSpec {self.id!r}: node passes require is_done")
        if self.batchable and not callable(self.run_batch):
            raise ValueError(f"PassSpec {self.id!r}: batchable passes require run_batch")

    @property
    def summary_field(self) -> str:
        return self.summary_key or self.id


#: id -> PassSpec, in registration order (passes/__init__ imports the
#: built-ins label, describe, review in that order).
_PASS_REGISTRY: dict[str, PassSpec] = {}


def register_pass(spec: PassSpec) -> PassSpec:
    """Add a pass to the catalog. Duplicate ids fail loudly (P1)."""
    if spec.id in _PASS_REGISTRY:
        raise ValueError(f"Enrichment pass '{spec.id}' is already registered")
    _PASS_REGISTRY[spec.id] = spec
    return spec


def unregister_pass(pass_id: str):
    """Remove a pass (tests registering temporary passes clean up with this)."""
    _PASS_REGISTRY.pop(pass_id, None)


def get_pass(pass_id: str) -> PassSpec:
    spec = _PASS_REGISTRY.get(pass_id)
    if spec is None:
        raise ValueError(f"Unknown enrichment pass: {pass_id}")
    return spec


def registered_passes() -> list:
    """Every registered spec, registration order."""
    return list(_PASS_REGISTRY.values())


def node_passes() -> list:
    """Registered node-unit specs, registration order (summary key order)."""
    return [s for s in _PASS_REGISTRY.values() if s.unit == "node"]


def phase_pass_ids() -> list:
    """Every pass that runs as its own phase, in dependency order.

    Trigger-fired passes run at their trigger points instead and are
    excluded; ``after`` constraints naming passes outside this list are
    ignored for ordering. Topological sort, ties broken by registration
    order — phase="all" expands to exactly this list.
    """
    specs = [s for s in _PASS_REGISTRY.values() if not s.triggers]
    ids = [s.id for s in specs]
    idset = set(ids)
    waiting = {s.id: {d for d in s.after if d in idset} for s in specs}
    ordered = []
    while waiting:
        ready = [pid for pid in ids if pid in waiting and not (waiting[pid] & set(waiting))]
        if not ready:
            raise ValueError(f"Enrichment pass ordering cycle among: {sorted(waiting)}")
        for pid in ready:
            ordered.append(pid)
            del waiting[pid]
    return ordered


def resolve_phases(phase: str) -> list:
    """Map the ``phase=`` API onto pass specs: "all" = every phase pass in
    dependency order; otherwise one registered pass id. Unknown ids raise
    (P7)."""
    if phase == "all":
        return [get_pass(pid) for pid in phase_pass_ids()]
    return [get_pass(phase)]


def describe_passes() -> list[dict]:
    """Catalog slice of the registered passes (see worldgen/catalog.py): one
    self-describing entry per pass, registration order."""
    return [
        {"kind": "pass", "id": s.id, "label": s.label,
         "description": s.description, "unit": s.unit,
         "after": list(s.after),
         "triggers": dict(s.triggers) if s.triggers else None,
         "batchable": s.batchable,
         "requires": list(s.requires), "produces": list(s.produces)}
        for s in _PASS_REGISTRY.values()
    ]
