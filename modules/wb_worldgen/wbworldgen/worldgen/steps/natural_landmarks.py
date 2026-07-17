from wbworldgen.worldgen.base import Step, register
from wbworldgen.worldgen.terrain_placement import ENVIRONMENT_TAGS, tag_descriptions

_GUIDANCE = """
When a terrain_generation step ran, its output describes the ACTUAL generated
geography for each surface layer (biome mix, coastline/rivers/lakes,
elevation). Author features that fit that terrain — do not invent a glacier on
a map that is all desert, or a coral reef on a landlocked map. On worlds
without generated terrain the environment tag only loosely guides placement —
pick the nearest match. Set ``scope`` to the parallel map's name (from
hierarchy_design) a feature belongs to, or leave it empty for the main map.

For every feature set ``environment`` to ONE of these tags so the engine can
place it on a fitting cell of the map:
{tags}

Pick the tag whose terrain matches the feature you are describing.
""".format(tags=tag_descriptions())


@register
class NaturalLandmarksStep(Step):
    id = "natural_landmarks"
    label = "Notable Features"
    description = ("Place the world's notable physical features and places: natural landmarks, "
                   "districts, waterfronts, striking locations — whatever fits this world.")
    after = "terrain_generation"
    guidance = _GUIDANCE
    schema = {
        "landmarks": {"type": "list", "label": "Landmarks", "rerollable": True, "item_schema": {
            "scope": {
                "type": "string",
                "label": "Map Scope",
                "description": "Which map this belongs to: empty for the main world map, or a parallel map's name from hierarchy_design.",
            },
            "name": {"type": "string", "label": "Name"},
            "type": {"type": "string", "label": "Landmark Type"},
            "environment": {
                "type": "select",
                "label": "Environment",
                "options": list(ENVIRONMENT_TAGS.keys()),
                "description": "Terrain type the engine places this landmark on.",
            },
            "description": {"type": "text", "label": "Description"},
        }},
    }
