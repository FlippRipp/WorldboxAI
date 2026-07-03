from wbworldgen.worldgen.base import Step, register
from wbworldgen.worldgen.terrain_placement import ENVIRONMENT_TAGS, tag_descriptions

_GUIDANCE = """
The terrain_generation step output describes the ACTUAL generated geography for
each surface layer (biome mix, coastline/rivers/lakes, elevation). Author
landmarks that fit that terrain — do not invent a glacier on a layer that is all
desert, or a coral reef on a landlocked layer.

For every landmark set ``environment`` to ONE of these tags so the engine can
place it on a fitting cell of the map:
{tags}

Pick the tag whose terrain matches the landmark you are describing.
""".format(tags=tag_descriptions())


@register
class NaturalLandmarksStep(Step):
    id = "natural_landmarks"
    label = "Natural Landmarks"
    description = "Place notable natural features: mountains, forests, rivers, caverns, and other landmarks per region."
    after = "terrain_regions"
    guidance = _GUIDANCE
    schema = {
        "landmarks": {"type": "list", "label": "Landmarks", "rerollable": True, "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "region": {"type": "string", "label": "Region"},
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
