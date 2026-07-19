"""One capability catalog over the three registries (B2 of the worldgen
architecture plan).

Steps (pipeline stages), map generators and enrichment passes each keep
their own registry; this module renders them as one self-describing catalog
— the document Arc C's planner (or a human, or an endpoint) reads whole.
Every entry is a capability in the P1 sense: kind, id, label, a description
that doubles as the planner's selection text, and its declared contracts.
The structured (``capability_catalog``) and human-readable
(``render_catalog_markdown``) forms carry the same content.

Module-contributed hooks (``HOOK_NAMES``) are deliberately *not* in the
catalog — whether other modules may register capabilities directly is Arc C
open question 4.
"""

from wbworldgen.worldgen.base import STEP_REGISTRY, describe_steps
from wbworldgen.worldgen.enrichment.registry import describe_passes
from wbworldgen.worldgen.generation.registry import describe_generators


def capability_catalog() -> dict:
    """The combined catalog: every registered capability, self-described.

    ``{"steps": [...], "generators": [...], "passes": [...], "tools": [...]}``
    — each entry carries its ``kind`` too, so the lists can be flattened
    without losing anything. ``tools`` is the fourth catalog (C1): the agent's
    action surface over the other three.
    """
    # Registration side effect, same idiom as register_default_steps: the
    # built-in steps live in a package nothing imports at module load.
    # Generators and passes register when their registries import. The agent
    # toolbox import is function-local both for the same reason and because
    # its read_catalog tool imports this module back.
    import wbworldgen.worldgen.steps  # noqa: F401
    import wbworldgen.worldgen.enrichment.passes  # noqa: F401
    from wbworldgen.worldgen.agent.registry import describe_tools

    return {
        "steps": describe_steps(),
        "generators": describe_generators(),
        "passes": describe_passes(),
        "tools": describe_tools(),
    }


def _data_notes(entry: dict) -> list:
    notes = []
    if entry.get("requires"):
        notes.append(f"requires {'+'.join(entry['requires'])}")
    if entry.get("produces"):
        notes.append(f"produces {'+'.join(entry['produces'])}")
    return notes


def _step_notes(entry: dict) -> str:
    notes = []
    if entry.get("after"):
        notes.append(f"after {entry['after']}")
    if entry.get("uses") and entry["uses"] != "llm":
        notes.append(f"engine: {entry['uses']}")
    notes.extend(_data_notes(entry))
    return f" [{', '.join(notes)}]" if notes else ""


def _generator_notes(entry: dict) -> str:
    notes = []
    if not entry.get("implemented", True):
        notes.append("reserved, not implemented")
    if entry.get("needs_llm_content"):
        notes.append("needs authored content")
    return f" [{', '.join(notes)}]" if notes else ""


def _pass_notes(entry: dict) -> str:
    notes = [f"per {entry.get('unit', 'node')}"]
    if entry.get("after"):
        notes.append(f"after {', '.join(entry['after'])}")
    triggers = entry.get("triggers") or {}
    if triggers.get("on_map_complete"):
        notes.append(f"auto-runs when {triggers['on_map_complete']} completes a map")
    if entry.get("batchable"):
        notes.append("batchable")
    notes.extend(_data_notes(entry))
    return f" [{', '.join(notes)}]"


_SECTIONS = (
    ("Steps", "steps",
     "Pipeline stages, in dependency order.", _step_notes),
    ("Map generators", "generators",
     "Ways to draw one map of the hierarchy.", _generator_notes),
    ("Enrichment passes", "passes",
     "Node- and map-level LLM work over generated maps.", _pass_notes),
)


def _tool_lines(entry: dict) -> list:
    """One tool as markdown: the headline plus one indented line per
    argument (the agent picks arguments from these)."""
    marker = "mutates world" if entry.get("mutates") else "read-only"
    lines = [f"- **{entry['id']}** ({entry['label']}) [{marker}]: "
             f"{entry['description']}"]
    for name, p in (entry.get("params") or {}).items():
        bits = [p.get("type", "string")]
        if p.get("required"):
            bits.append("required")
        if p.get("enum") is not None:
            bits.append(f"one of {p['enum']}")
        desc = f" — {p['description']}" if p.get("description") else ""
        lines.append(f"  - `{name}` ({', '.join(bits)}){desc}")
    return lines


def render_catalog_markdown(catalog: dict = None) -> str:
    """The catalog as a markdown document — what the build agent (or a
    human) reads to choose capabilities and actions."""
    cat = catalog if catalog is not None else capability_catalog()
    lines = ["# Build capabilities", ""]
    for title, key, blurb, notes_fn in _SECTIONS:
        lines.append(f"## {title}")
        lines.append(f"_{blurb}_")
        lines.append("")
        for entry in cat.get(key, []):
            lines.append(
                f"- **{entry['id']}** ({entry['label']}){notes_fn(entry)}: "
                f"{entry['description']}")
        lines.append("")
    if cat.get("tools"):
        lines.append("## Agent tools")
        lines.append("_The agent's action surface: every action a build "
                     "agent may take, with its arguments (C1)._")
        lines.append("")
        for entry in cat["tools"]:
            lines.extend(_tool_lines(entry))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def check_data_dependencies(items: list, steps: dict = None) -> list:
    """Validate an ordered capability list against the declared data
    contracts (B3): every item's ``requires`` must name artifacts some
    earlier item ``produces``.

    ``items``: ``[{"kind": "step"|"pass", "id": ...}, ...]`` in execution
    order — the shape of an effective (post-``dynamic_skips``) pipeline or a
    C1 plan's item list. ``steps`` optionally maps step ids to step objects
    (a builder's registered instances, module-contributed steps included);
    when omitted, the built-in step classes are used. Pass ids always
    resolve against the pass registry.

    This checker belongs to the *executor*, not the sorter: ``resolve_order``
    keeps its behavior and ``after`` chains keep ordering execution — this
    function only answers "is every data need met by the items actually in
    the list". Returns a list of human-readable problems (empty = valid);
    an item referencing an unknown capability raises ``ValueError`` (P1/P7).
    """
    from wbworldgen.worldgen.enrichment.registry import get_pass

    if steps is None:
        import wbworldgen.worldgen.steps  # noqa: F401 — registration side effect
        steps = {cls.id: cls for cls in STEP_REGISTRY}

    problems = []
    available = set()
    for item in items:
        kind = item.get("kind")
        item_id = item.get("id")
        if kind == "step":
            cap = steps.get(item_id)
            if cap is None:
                raise ValueError(f"Unknown step capability: {item_id}")
        elif kind == "pass":
            cap = get_pass(item_id)  # raises on unknown ids
        else:
            raise ValueError(f"Unknown capability kind: {kind!r} (item {item_id!r})")
        for needed in getattr(cap, "requires", ()) or ():
            if needed not in available:
                problems.append(
                    f"{kind}:{item_id} requires '{needed}', which nothing "
                    f"earlier in the list produces")
        available.update(getattr(cap, "produces", ()) or ())
    return problems


def produced_artifacts(world_state: dict, compiled: dict = None,
                       steps: dict = None) -> set:
    """The artifact names an existing world's content already provides — the
    executor-side companion of ``check_data_dependencies``. In agent mode
    there is no plan list to walk: a per-action precondition check (C1, P7)
    diffs a capability's ``requires`` against this set.

    Step artifacts count when the step's entry carries non-empty data
    (``approved`` is a wizard-workflow flag, not a data signal; seeded
    worlds hold empty ``{}`` placeholders that rightly do not count). Pass
    artifacts count when at least one compiled node already carries the
    pass's output (partial coverage still is the artifact — floor-limited
    enrichment is the default); they need ``compiled``. ``steps`` overrides
    the built-in step classes with a builder's registered instances,
    module-contributed steps included.
    """
    from wbworldgen.worldgen.enrichment.registry import registered_passes

    if steps is None:
        import wbworldgen.worldgen.steps  # noqa: F401 — registration side effect
        steps = {cls.id: cls for cls in STEP_REGISTRY}

    produced = set()
    steps_state = (world_state or {}).get("steps", {})
    for step_id, cap in steps.items():
        entry = steps_state.get(step_id) or {}
        if entry.get("data"):
            produced.update(getattr(cap, "produces", ()) or ())

    if compiled is not None:
        from wbworldgen.worldgen import mapspace as _ms
        import wbworldgen.worldgen.enrichment.passes  # noqa: F401 — registration
        nodes = _ms.all_nodes(compiled)
        for spec in registered_passes():
            if spec.unit != "node" or not spec.produces:
                continue
            if any(spec.in_domain(n) and spec.is_done(n) for n in nodes):
                produced.update(spec.produces)
    return produced
