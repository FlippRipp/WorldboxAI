"""World Structure step — the AI designs the world's map hierarchy.

Templates used to declare the ordered map levels (world -> interior, ...);
this step makes the world's own design authoritative: one LLM call, running
after rules and lore, authors the levels top to bottom and binds each to a
registered map generator by reading the generator catalog. Junk output
degrades to the default [world, interior] pair, so the worst case behaves
exactly like the pre-design era. Non-root levels only take effect at
play-time expansion (interiors today; wider scales arrive with the unified
generation rework — see docs/design/ai_world_structure_plan.md).

``design.designed_levels`` (the worldgen design module — the one query
surface over this step's output) and ``contribute_to_compiled`` are the read
seams: the facade resolves a world's levels through the former (falling back
to the default [world, interior] pair for old worlds), and the compiler
picks up the AI-authored vocabulary through the latter, filling the same
``template_vocab`` seam template-era worlds' snapshots still use.
"""

import copy

from wbworldgen.worldgen import design as _design
from wbworldgen.worldgen.base import Step, register

#: Where an unknown or unimplemented generator id lands — decision 6: the
#: existing world_map is the abstract fallback (without terrain it draws a
#: clean node graph).
FALLBACK_GENERATOR = "world_map"

#: The deepest level must always be enterable during play.
_INTERIOR_LEVEL = {
    "level_type": "interior", "label": "Interior", "generator_id": "interior",
    "nestable": True,
    "guidance": "Rooms, halls and courts of one building, complex or vessel.",
}

#: Hard cap on authored levels — matches the play-time expansion MAX_DEPTH.
MAX_LEVELS = 6

_GUIDANCE = """
The world's map structure is designed HERE, top to bottom. levels is an
ordered list from the LARGEST scale down to the SMALLEST — the root map of
the world first, interiors last. Design the structure THIS world actually
needs, in its own terms: a lone fantasy overworld is [world -> interior]; a
single city is [city -> interior]; a star empire might be [star_system ->
planet -> city -> interior]. 2-4 levels is typical. During play, a location
on one map can open into a map of any LOWER level — a vast capital on the
world map opens as a city, a lone tavern straight as an interior.

For each level: level_type is a short lowercase id (world, planet, city,
interior...); label is its display name; guidance is 1-2 sentences telling a
later AI what ONE map of this level contains, written in this world's own
voice; generator_id picks which registered map generator draws maps of this
level — read the catalog below and choose the best fit for the scale, and
when nothing fits use world_map (without terrain it degrades to a clean
abstract node graph); terrain is "yes" ONLY when one map of this level spans
natural geography (a planet's surface, a continent-scale realm) and should
get generated elevation, biomes and rivers — "no" for star systems, cities,
interiors and abstract planes. The LAST level must use the interior
generator so locations can always open into enterable rooms.

parallel_maps declares maps that exist SIDE BY SIDE with the root map at the
very top of the hierarchy, joined by a handful of crossings — a D&D
underworld beneath the surface world, a mirror shadow realm. This is an
exceptional structure: use it ONLY when the premise absolutely requires a
coequal plane. It is NOT for places contained in the world — planets,
cities, stations and realms-within-realms belong in levels, as locations
that open into deeper maps. Most worlds need NONE.

pregenerate lists the few named locations whose own sub-maps should be built
during world creation because the seed premise makes them central (the
story's starting city, the villain's fortress). Everything else is generated
during play when the story approaches it — keep this list SHORT (0-3).

site_sub_noun and connection_looks tune play-time prompts: site_sub_noun is
what this world calls the parts inside a location ("rooms, halls and
courts", "decks, domes and installations"); connection_looks describe how
each kind of crossing between maps reads in prose (kind "cave_mouth" ->
"a cave mouth yawning into the dark").
"""


def normalize_hierarchy_design(data, implemented_generator_ids,
                               map_style: str = "",
                               fallback_levels: list = None) -> dict:
    """Clamp LLM output to the engine contract: deduped slug level types,
    generator ids from the implemented registry only (unknown ids land on the
    abstract fallback), an interior level guaranteed at the bottom, and a
    root generator aligned with the world design's "city" map style. Worst
    case (junk output) degrades to ``fallback_levels`` (caller-provided) or
    the default [world, interior] pair."""
    if not isinstance(data, dict):
        data = {}
    implemented = set(implemented_generator_ids or [])

    levels = []
    seen_types = set()
    for raw in data.get("levels") or []:
        if not isinstance(raw, dict):
            continue
        level_type = "_".join(str(raw.get("level_type") or "").strip().lower().split())
        if not level_type or level_type in seen_types:
            continue
        seen_types.add(level_type)
        generator_id = str(raw.get("generator_id") or "").strip()
        if generator_id not in implemented:
            generator_id = FALLBACK_GENERATOR
        level = {
            "level_type": level_type,
            "label": str(raw.get("label") or "").strip() or level_type.replace("_", " ").title(),
            "guidance": str(raw.get("guidance") or "").strip(),
            "generator_id": generator_id,
        }
        if generator_id == "interior":
            level["nestable"] = True
        # Terrain rasters are a world_map capability; the flag is meaningless
        # (and dropped) on other generators.
        terrain = raw.get("terrain")
        if generator_id == "world_map" and (
                terrain is True or str(terrain or "").strip().lower() in ("yes", "y", "true", "1")):
            level["terrain"] = True
        levels.append(level)
    levels = levels[:MAX_LEVELS]

    if not levels:
        if fallback_levels:
            levels = [dict(l) for l in fallback_levels]
        else:
            from wbworldgen.worldgen.migrate import DEFAULT_LEVELS
            levels = [dict(l) for l in DEFAULT_LEVELS]
    if all(l.get("generator_id") != "interior" for l in levels):
        levels = levels[:MAX_LEVELS - 1] + [dict(_INTERIOR_LEVEL)]
    if (map_style == "city" and "city_roadnet" in implemented
            and levels[0].get("generator_id") != "city_roadnet"):
        # The (player-reviewed) world design declared the whole world one
        # city — the root map is a street network, whatever the LLM said
        # (and whatever fallback the junk case landed on).
        levels[0]["generator_id"] = "city_roadnet"

    parallel_maps = []
    for raw in data.get("parallel_maps") or []:
        if not isinstance(raw, dict) or not str(raw.get("label") or "").strip():
            continue
        parallel_maps.append({
            "label": str(raw.get("label") or "").strip(),
            "level_type": str(raw.get("level_type") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
            "connection_kind": str(raw.get("connection_kind") or "").strip(),
            "connection_count": raw.get("connection_count"),
        })

    pregenerate = []
    for raw in data.get("pregenerate") or []:
        if not isinstance(raw, dict) or not str(raw.get("location_name") or "").strip():
            continue
        pregenerate.append({
            "location_name": str(raw.get("location_name") or "").strip(),
            "level_type": str(raw.get("level_type") or "").strip(),
            "reason": str(raw.get("reason") or "").strip(),
        })

    connection_looks = []
    seen_kinds = set()
    for raw in data.get("connection_looks") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        look = str(raw.get("look") or "").strip()
        if kind and look and kind not in seen_kinds:
            seen_kinds.add(kind)
            connection_looks.append({"kind": kind, "look": look})

    return {
        "notes": str(data.get("notes") or "").strip(),
        "levels": levels,
        "parallel_maps": parallel_maps,
        "pregenerate": pregenerate,
        "site_sub_noun": str(data.get("site_sub_noun") or "").strip(),
        "connection_looks": connection_looks,
    }


@register
class HierarchyDesignStep(Step):
    id = "hierarchy_design"
    label = "World Structure"
    description = ("Design the world's map hierarchy: its levels from largest to "
                   "smallest scale, which map generator draws each, plus parallel "
                   "planes and pre-built sub-maps.")
    after = "lore"
    produces = ("hierarchy",)
    guidance = _GUIDANCE
    schema = {
        "notes": {
            "type": "text",
            "label": "Structure Notes",
            "description": "Your reading of how this world's map hierarchy fits the premise.",
        },
        "levels": {"type": "list", "label": "Map Levels", "rerollable": True, "item_schema": {
            "level_type": {"type": "string", "label": "Level Type"},
            "label": {"type": "string", "label": "Name"},
            "guidance": {"type": "text", "label": "What one map of this level contains"},
            "generator_id": {"type": "string", "label": "Map Generator"},
            "terrain": {"type": "string", "label": "Physical terrain (yes/no)"},
        }},
        "parallel_maps": {"type": "list", "label": "Parallel Maps", "rerollable": True, "item_schema": {
            "label": {"type": "string", "label": "Name"},
            "level_type": {"type": "string", "label": "Level Type"},
            "description": {"type": "text", "label": "Description"},
            "connection_kind": {
                "type": "string",
                "label": "Connection Kind",
                "description": "How it links to the main map (cave_mouth, portal, spaceport...).",
            },
            "connection_count": {
                "type": "number",
                "label": "Connections",
                "description": "How many crossings link it to the main map (1-6).",
            },
        }},
        "pregenerate": {"type": "list", "label": "Pre-built Sub-maps", "rerollable": True, "item_schema": {
            "location_name": {"type": "string", "label": "Location"},
            "level_type": {"type": "string", "label": "Level Type"},
            "reason": {"type": "string", "label": "Why Upfront"},
        }},
        "site_sub_noun": {
            "type": "text",
            "label": "Sub-location Noun",
            "description": ("What this world calls the parts inside a location, e.g. "
                            "\"rooms, halls and courts\" or \"decks, domes and installations\"."),
        },
        "connection_looks": {"type": "list", "label": "Connection Looks", "rerollable": True, "item_schema": {
            "kind": {"type": "string", "label": "Kind"},
            "look": {"type": "text", "label": "How it reads in prose"},
        }},
    }

    async def generate(self, ctx) -> dict:
        services = ctx.services
        from wbworldgen.worldgen.generation.registry import list_generators
        implemented = [g for g in list_generators() if g["implemented"]]
        implemented_ids = [g["id"] for g in implemented]
        map_style = _design.map_style(ctx.world_state)

        # Custom-generate steps bypass the facade's mock branch, so handle
        # mock mode here (precedent: world_form).
        llm = services._llm_service
        if ctx.force_mock or llm is None or getattr(llm, "mode", "mock") == "mock":
            from wbworldgen.worldgen.fixtures.mock_data import mock_hierarchy_design
            return normalize_hierarchy_design(
                mock_hierarchy_design(ctx.user_prompt, ctx.user_note),
                implemented_ids, map_style)

        catalog = "\n".join(
            f"- {g['id']}: {g['label']} — {g['description']}" for g in implemented)
        style_note = ""
        if map_style:
            aligned = _design.aligned_root_generator(map_style)
            style_note = (
                f"\n\nThe World Design step chose map_style \"{map_style}\" for the root map"
                + (f" — the FIRST level's generator_id must be {aligned}." if aligned
                   else " — no procedural terrain; give the FIRST level the generator that "
                        "best fits its scale."))
        # Subject-scoped ideation notes (C5/N3): the structure this step
        # designs is what note subjects later bind to per-map, so it alone
        # sees every subject note in full.
        from wbworldgen.worldgen import notes as _notes
        notes_block = ""
        subjects = _notes.subject_notes(ctx.world_state)
        if subjects:
            notes_block = (
                "\n\nThe world's creator agreed on design notes about "
                "specific places. The world must contain every subject, "
                "named so it is recognizable. Where a subject is a "
                "realm-, planet- or map-scale place, design the structure "
                "so its own map exists (a level or parallel map whose "
                "label matches the subject); smaller subjects become named "
                "locations on maps later — do not force those into "
                "levels:\n"
                + "\n".join(f"- {n['subject']}: {n['text']}" for n in subjects))
        effective = copy.copy(self)
        effective.guidance = (
            f"{effective.guidance}\n\nMap generator catalog (generator_id must be one of "
            f"these ids):\n{catalog}{style_note}{notes_block}"
        )
        context = services._build_chain_context(ctx.world_state, self.id)
        data = await services._llm_gen.generate(
            effective, context, ctx.user_prompt, ctx.user_note,
            system_framing=services.system_framing_for(ctx.world_state),
            coverage_directive=_design.coverage_directive(ctx.world_state, self.id))
        return normalize_hierarchy_design(data, implemented_ids, map_style)

    def contribute_to_compiled(self, steps_data: dict, compiled: dict):
        # AI-authored world vocabulary fills the same seam template-era
        # worlds' snapshots use; an existing snapshot (old worlds) wins so
        # their prompts never change under them.
        if compiled.get("template_vocab"):
            return
        data = (steps_data.get("hierarchy_design") or {}).get("data")
        if not isinstance(data, dict):
            return
        vocab = {}
        noun = str(data.get("site_sub_noun") or "").strip()
        if noun:
            vocab["site_sub_noun"] = noun
        looks = {}
        for entry in data.get("connection_looks") or []:
            if isinstance(entry, dict):
                kind = str(entry.get("kind") or "").strip()
                look = str(entry.get("look") or "").strip()
                if kind and look:
                    looks[kind] = look
        if looks:
            vocab["connection_looks"] = looks
        if vocab:
            compiled["template_vocab"] = vocab
