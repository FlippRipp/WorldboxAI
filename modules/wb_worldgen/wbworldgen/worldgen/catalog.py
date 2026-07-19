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

from wbworldgen.worldgen.base import describe_steps
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


def _step_notes(entry: dict) -> str:
    notes = []
    if entry.get("after"):
        notes.append(f"after {entry['after']}")
    if entry.get("uses") and entry["uses"] != "llm":
        notes.append(f"engine: {entry['uses']}")
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
