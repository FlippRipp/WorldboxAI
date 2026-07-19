"""World Design step — the AI's reading of what this world is.

The first pipeline step. One single-purpose, full-attention LLM call reads the
seed prompt (plus any scenario grounding) and decides the shape of the whole
generation run: what kind of world this is, whether it needs procedural
terrain at all (``map_style``), which optional steps add nothing here
(``skip_steps``), and — for every step that does run — a per-world directive
for what it should cover (``step_directives``). A modern Tokyo slice-of-life
world gets an abstract city map and "clubs and workplaces" instead of a
fantasy overworld with kingdoms; the AI, not the template, makes that call.

Downstream, ``design.dynamic_skips``/``design.coverage_directive`` (the
worldgen design module — the one query surface over this step's output) are
the pure read seams: the facade merges the skips into the effective step
order and injects each directive into that step's prompt. Worlds with no
world_form data (old worlds, seeded worlds) get an empty skip set and empty
directives — full back-compat.
"""

import copy

from wbworldgen.worldgen.base import Step, register
from wbworldgen.worldgen.design import AI_SKIPPABLE

#: Never presented in the step catalog: the step itself, and the engine-driven
#: enrichment passes the AI has no say over.
_CATALOG_EXCLUDED = {"world_form", "node_labeling", "node_descriptions"}

_GUIDANCE = """
You are shaping the generation pipeline itself, not writing world content yet.
Read the seed prompt closely and answer: what kind of world does it actually
ask for? A mythic fantasy overworld, a single modern city, a space station, a
quiet neighborhood? Then:

- map_style: pick "terrain" only when the world genuinely spans natural
  geography (continents, wilderness, biomes). Pick "city" when the whole
  playable world is ONE city, town or urban district — the map becomes a
  real street network: avenues, blocks, districts, venues. Pick "abstract"
  for stations, interiors, ships and other intimate non-urban settings —
  the map becomes a clean graph of places with no procedural landscape.
- skip_steps: rarely needed. Prefer a "keep this minimal" directive over
  skipping — even a slice-of-life story benefits from a few notable places and
  social circles.
- step_directives: write one directive for EVERY step in the catalog below.
  Phrase each in this world's own terms: a modern city's "Origins" is its
  founding and recent history, not a creation myth; its "Groups" are
  workplaces, clubs and friend circles, not armies; its "Notable Features"
  are stations, parks and landmark buildings, not enchanted forests. For a
  mythic world, lean the other way. The directives you write here steer every
  later generation call.
"""


def normalize_world_form(data, known_step_ids) -> dict:
    """Clamp LLM output to the engine contract: a valid map_style, skips from
    the allowlist only, directives only for known steps. Worst case (junk
    output) degrades to today's behavior: terrain, nothing skipped."""
    if not isinstance(data, dict):
        data = {}
    known = set(known_step_ids or [])
    map_style = data.get("map_style")
    if map_style not in ("terrain", "abstract", "city"):
        map_style = "terrain"
    skip_steps = []
    for sid in data.get("skip_steps") or []:
        if sid in AI_SKIPPABLE and sid not in skip_steps:
            skip_steps.append(sid)
    directives = []
    for entry in data.get("step_directives") or []:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("step_id")
        text = str(entry.get("directive") or "").strip()
        if sid in known and sid not in _CATALOG_EXCLUDED and text:
            directives.append({"step_id": sid, "directive": text})
    return {
        "world_kind": str(data.get("world_kind") or "").strip(),
        "map_style": map_style,
        "skip_steps": skip_steps,
        "step_directives": directives,
    }


@register
class WorldFormStep(Step):
    id = "world_form"
    label = "World Design"
    description = (
        "Read the seed prompt and decide the shape of this world: what kind of "
        "world it is, whether it needs physical terrain, and what each later "
        "step should cover for THIS specific world."
    )
    after = None
    guidance = _GUIDANCE
    schema = {
        "world_kind": {
            "type": "text", "label": "World Kind",
            "description": "One or two sentences: what kind of world this is — genre, era, scale, tone.",
        },
        "map_style": {
            "type": "select", "label": "Map Style", "options": ["terrain", "abstract", "city"],
            "description": ("terrain = generate physical geography (continents, biomes, rivers) and "
                            "place locations on it — for overworlds and wilderness. city = the world "
                            "is one city: a generated street network of avenues, blocks, districts "
                            "and venues. abstract = no procedural terrain; the map is a clean graph "
                            "of places — for stations, interiors, intimate settings."),
        },
        "skip_steps": {
            "type": "list", "label": "Steps to Skip", "item_type": "string",
            "description": ("Ids of optional steps that add nothing for this world (rarely needed — "
                            "prefer a 'keep this minimal' directive). Only natural_landmarks and "
                            "society_factions may be listed; terrain is controlled by map_style."),
        },
        "step_directives": {
            "type": "list", "label": "Step Directives", "rerollable": True,
            "item_schema": {
                "step_id": {"type": "string", "label": "Step"},
                "directive": {"type": "text", "label": "What this step should cover for this world"},
            },
        },
    }

    def _catalog(self, services, world_state) -> tuple[str, list]:
        """Readable catalog of the steps this design pass governs, plus their
        ids for output normalization."""
        lines, ids = [], []
        for sid in services.ordered_ids_for(world_state):
            if sid in _CATALOG_EXCLUDED:
                continue
            step = services._steps[sid]
            note = ""
            if sid == "terrain_generation":
                note = " [runs only when map_style is terrain]"
            elif sid in AI_SKIPPABLE:
                note = " [skippable]"
            lines.append(f"- {sid}: {step.label} — {step.description}{note}")
            ids.append(sid)
        return "\n".join(lines), ids

    async def generate(self, ctx) -> dict:
        services = ctx.services
        catalog, known_ids = self._catalog(services, ctx.world_state)

        # Custom-generate steps bypass the facade's mock branch, so handle
        # mock mode here (precedent: terrain_generation does its own work).
        llm = services._llm_service
        if ctx.force_mock or llm is None or getattr(llm, "mode", "mock") == "mock":
            from wbworldgen.worldgen.fixtures.mock_data import mock_world_form
            return normalize_world_form(mock_world_form(ctx.user_prompt, ctx.user_note), known_ids)

        effective = copy.copy(self)
        effective.guidance = (
            f"{effective.guidance}\n\nStep catalog (write one directive per step, "
            f"keyed by the id before the colon):\n{catalog}"
        )
        data = await services._llm_gen.generate(effective, {}, ctx.user_prompt, ctx.user_note)
        return normalize_world_form(data, known_ids)

    def contribute_to_compiled(self, steps_data: dict, compiled: dict):
        data = (steps_data.get("world_form") or {}).get("data")
        if isinstance(data, dict) and data:
            compiled["world_design"] = {
                "world_kind": data.get("world_kind", ""),
                "map_style": data.get("map_style", "terrain"),
            }
