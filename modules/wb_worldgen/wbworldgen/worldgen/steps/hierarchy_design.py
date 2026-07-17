from wbworldgen.worldgen.base import Step, register

_GUIDANCE = """
The world is a hierarchy of maps. The template for this world declares which
map levels can exist (e.g. world -> interior, or solar system -> planet ->
interior); level names are descriptive text, not fixed categories — read them
and decide what they mean for THIS world.

parallel_maps declares maps that exist SIDE BY SIDE with the main map at the
top of the hierarchy — a D&D-style underworld beneath the surface world, a
shadow realm, the separate planets of a star system. Most worlds need none.
Each parallel map states how it connects to the main map (connection_kind,
e.g. cave_mouth, portal, spaceport, and roughly how many such crossings).

pregenerate lists the few named locations whose own sub-maps should be built
during world creation because the seed premise makes them central (the
story's starting city, the villain's fortress). Everything else is generated
during play when the story approaches it — keep this list SHORT (0-3).
"""


@register
class HierarchyDesignStep(Step):
    id = "hierarchy_design"
    label = "World Structure"
    description = "Decide the world's map hierarchy: parallel maps (underworlds, planets) and which locations deserve pre-built sub-maps."
    after = "lore"
    guidance = _GUIDANCE
    schema = {
        "notes": {
            "type": "text",
            "label": "Structure Notes",
            "description": "Your reading of how this world's map hierarchy fits the premise.",
        },
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
    }
