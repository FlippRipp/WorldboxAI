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

    ``{"steps": [...], "generators": [...], "passes": [...]}`` — each entry
    carries its ``kind`` too, so the three lists can be flattened without
    losing anything.
    """
    # Registration side effect, same idiom as register_default_steps: the
    # built-in steps live in a package nothing imports at module load.
    # Generators and passes register when their registries import.
    import wbworldgen.worldgen.steps  # noqa: F401
    import wbworldgen.worldgen.enrichment.passes  # noqa: F401

    return {
        "steps": describe_steps(),
        "generators": describe_generators(),
        "passes": describe_passes(),
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


def render_catalog_markdown(catalog: dict = None) -> str:
    """The catalog as a markdown document — what a planning LLM (or a
    human) reads to choose capabilities."""
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
