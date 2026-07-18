import copy

from wbworldgen.worldgen.base import Step, register
from wbworldgen.worldgen.terrain_placement import ENVIRONMENT_TAGS, tag_descriptions

_SCOPE_NOTE = """
Set ``scope`` to the parallel map's name (from hierarchy_design) a feature
belongs to, or leave it empty for the main map.
"""

_GUIDANCE_TERRAIN = """
The terrain_generation step's output describes the ACTUAL generated geography
for each surface layer (biome mix, coastline/rivers/lakes, elevation). Author
features that fit that terrain — do not invent a glacier on a map that is all
desert, or a coral reef on a landlocked map.
{scope}
For every feature set ``environment`` to ONE of these tags so the engine can
place it on a fitting cell of the map:
{tags}

Pick the tag whose terrain matches the feature you are describing.
""".format(scope=_SCOPE_NOTE, tags=tag_descriptions())

_GUIDANCE_ABSTRACT = """
This world has no generated natural terrain — features are notable places in
their own right (districts, venues, striking locations), placed on the map by
importance rather than geography. Author whatever fits this world's design and
lore.
{scope}""".format(scope=_SCOPE_NOTE)


@register
class NaturalLandmarksStep(Step):
    id = "natural_landmarks"
    label = "Notable Features"
    description = ("Place the world's notable physical features and places: natural landmarks, "
                   "districts, waterfronts, striking locations — whatever fits this world.")
    after = "terrain_generation"
    guidance = _GUIDANCE_TERRAIN
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
                # Hidden by the frontend when no landmark carries it (non-terrain worlds).
                "conditional": True,
            },
            "description": {"type": "text", "label": "Description"},
        }},
    }

    def view_for(self, world_state: dict) -> Step:
        """The ``environment`` tag exists solely for terrain-aware placement
        (terrain_placement turns it into a cell-suitability mask), which only
        runs on worlds whose creation generates terrain rasters. On abstract
        and city worlds the tag would be authored and then never read — so
        drop the field and its guidance there."""
        from wbworldgen.worldgen.steps.world_form import dynamic_skips
        if "terrain_generation" not in dynamic_skips(world_state):
            return self
        view = copy.copy(self)
        view.schema = copy.deepcopy(self.schema)
        view.schema["landmarks"]["item_schema"].pop("environment", None)
        view.guidance = _GUIDANCE_ABSTRACT
        return view
