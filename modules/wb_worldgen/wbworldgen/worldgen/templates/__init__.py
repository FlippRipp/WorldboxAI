"""World templates: data-driven genre/scale presets for world generation.

A template never touches the step registry — it is applied at generation time
as a view over the registered steps: which steps run (``skip_steps``), what the
prompts say (``system_framing`` + per-step ``overrides``), what the schema form
shows (``schema_patch``), and what values are pinned into the generated data
regardless of the form (``pinned_values``).

The compiled world's shape is genre-independent by design: a template that
removes a field from the *form* (e.g. sci-fi hiding ``magic_level``) must pin a
value for it instead, so every downstream reader of the compiled dict keeps
seeing the full ``rules``/``lore`` contract.

Shipped templates live in this package as JSON; user templates go in
``data/world_templates/*.json`` (same shape, user wins on id collision). The
``ai_default`` template is the implicit default and deliberately empty — the
step classes' own guidance/schema are genre-neutral, and the ``world_form``
step's per-world directives (not the template) decide what each step covers.
Genre presets like ``overworld_fantasy`` are optional hints layered on top.
"""

import copy
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_ID = "ai_default"

#: The system-prompt framing used when a template doesn't override it — must
#: stay byte-identical to the historical hardcoded line.
DEFAULT_SYSTEM_FRAMING = "You are a world building AI for a tabletop roleplaying game."

_PACKAGE_DIR = Path(__file__).parent
_USER_DIR = Path("data/world_templates")


@dataclass
class WorldTemplate:
    id: str
    label: str = ""
    description: str = ""
    #: First line of every generation system prompt. None = default framing.
    system_framing: str = None
    #: Step ids that do not run for this template (never enrichment steps).
    skip_steps: list = field(default_factory=list)
    #: Per-step prompt/schema overrides:
    #: {step_id: {label, description, guidance, schema_patch: {remove, add, modify}}}
    overrides: dict = field(default_factory=dict)
    #: Values merged over the generated data of a step, guaranteeing contract
    #: keys removed from the form still exist in the output.
    pinned_values: dict = field(default_factory=dict)
    #: Vocabulary consumed outside step generation (enrichment/site prompts):
    #: {connection_looks: {type: look}, site_sub_noun: str, ...}
    vocabulary: dict = field(default_factory=dict)
    #: Map-generation defaults: {"default_total_nodes": int}
    map: dict = field(default_factory=dict)
    #: Ordered hierarchy levels (free text) top to bottom:
    #: [{level_type, label, generator_id, guidance, nestable?}]. Empty = the
    #: default [world, interior] pair.
    levels: list = field(default_factory=list)

    def resolved_levels(self) -> list:
        if self.levels:
            return [dict(l) for l in self.levels]
        from wbworldgen.worldgen.migrate import DEFAULT_LEVELS
        return [dict(l) for l in DEFAULT_LEVELS]

    def resolved_system_framing(self) -> str:
        return self.system_framing or DEFAULT_SYSTEM_FRAMING

    def step_override(self, step_id: str) -> dict:
        override = self.overrides.get(step_id)
        return override if isinstance(override, dict) else {}

    def apply_to_step(self, step):
        """A lightweight effective view of a registered step under this
        template: same object contract, patched schema/guidance/labels. The
        registered step instance is never mutated."""
        override = self.step_override(step.id)
        patch = override.get("schema_patch")
        if not override and not patch:
            return step
        effective = copy.copy(step)
        if override.get("label"):
            effective.label = override["label"]
        if override.get("description"):
            effective.description = override["description"]
        if override.get("guidance") is not None:
            effective.guidance = override["guidance"]
        if isinstance(patch, dict):
            effective.schema = apply_schema_patch(step.schema, patch)
        return effective

    def default_total_nodes(self):
        value = self.map.get("default_total_nodes") if isinstance(self.map, dict) else None
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None


def apply_schema_patch(schema: dict, patch: dict) -> dict:
    """Apply a {remove: [...], add: {...}, modify: {field: {...}}} patch to a
    deep copy of a step schema. Unknown fields are ignored."""
    patched = copy.deepcopy(schema or {})
    for key in patch.get("remove", []) or []:
        patched.pop(key, None)
    for key, spec in (patch.get("add") or {}).items():
        if isinstance(spec, dict):
            patched[key] = copy.deepcopy(spec)
    for key, changes in (patch.get("modify") or {}).items():
        if key in patched and isinstance(changes, dict) and isinstance(patched[key], dict):
            patched[key].update(copy.deepcopy(changes))
    return patched


def _template_from_dict(raw: dict) -> WorldTemplate:
    return WorldTemplate(
        id=str(raw.get("id", "")),
        label=str(raw.get("label", "")),
        description=str(raw.get("description", "")),
        system_framing=raw.get("system_framing") or None,
        skip_steps=list(raw.get("skip_steps") or []),
        overrides=dict(raw.get("overrides") or {}),
        pinned_values=dict(raw.get("pinned_values") or {}),
        vocabulary=dict(raw.get("vocabulary") or {}),
        map=dict(raw.get("map") or {}),
        levels=list(raw.get("levels") or []),
    )


def load_templates() -> dict:
    """{template_id: WorldTemplate} — shipped package templates overlaid by
    user templates from data/world_templates (user wins on id collision)."""
    templates: dict[str, WorldTemplate] = {}
    for directory in (_PACKAGE_DIR, _USER_DIR):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("skipping unreadable world template %s: %s", path, e)
                continue
            template = _template_from_dict(raw)
            if not template.id:
                logger.warning("skipping world template without id: %s", path)
                continue
            templates[template.id] = template
    if DEFAULT_TEMPLATE_ID not in templates:
        templates[DEFAULT_TEMPLATE_ID] = WorldTemplate(
            id=DEFAULT_TEMPLATE_ID, label="Let the AI decide",
            description="The AI reads your prompt and shapes the world to fit (default).")
    return templates


def get_template(templates: dict, template_id: str) -> WorldTemplate:
    """Resolve a template id defensively: unknown/absent ids fall back to the
    default so old worlds and callers keep working unchanged."""
    if template_id and template_id in templates:
        return templates[template_id]
    return templates[DEFAULT_TEMPLATE_ID]
