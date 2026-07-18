import copy

from wbworldgen.worldgen.base import Step, register
from wbworldgen.worldgen.terrain_placement import ENVIRONMENT_TAGS, tag_descriptions

_PLACEMENT_NOTE = """
First define ``areas``: 3-6 named areas that divide the main map — regions of
a wilderness, districts and quarters of a city, sectors of a station. The
engine carves the map into these territories and places everything inside
them, so give each a distinct character.

Set ``scope`` to the parallel map's name (from hierarchy_design) a feature
belongs to, or leave it empty for the main map. Set ``region`` to the name of
one of your areas — the engine places the feature inside that area's
territory; leave it empty only when no area fits.

Features that belong together must SAY so — the engine cannot guess that two
places are related, and unrelated placement scatters them across the map:
- ``part_of`` names another feature (or a group's settlement) this one belongs
  to. The engine then keeps them together instead of placing this feature
  independently.
- ``relation`` says how: "adjacent" puts it on the map right beside its parent
  (a shrine at the castle gates); "inside" makes it a room or space WITHIN the
  parent — it will not appear on this map at all, but is guaranteed to exist
  inside the parent when the parent is explored (a council office inside a
  school, a vault inside a bank).
"""

_GUIDANCE_TERRAIN = """
The terrain_generation step's output describes the ACTUAL generated geography
for each surface layer (biome mix, coastline/rivers/lakes, elevation). Author
features that fit that terrain — do not invent a glacier on a map that is all
desert, or a coral reef on a landlocked map.
{placement}
For every feature set ``environment`` to ONE of these tags so the engine can
place it on a fitting cell of the map:
{tags}

Pick the tag whose terrain matches the feature you are describing.
""".format(placement=_PLACEMENT_NOTE, tags=tag_descriptions())

_GUIDANCE_ABSTRACT = """
This world has no generated natural terrain — features are notable places in
their own right (districts, venues, striking locations), placed on the map by
importance rather than geography. Author whatever fits this world's design and
lore.
{placement}""".format(placement=_PLACEMENT_NOTE)


@register
class NaturalLandmarksStep(Step):
    id = "natural_landmarks"
    label = "Notable Features"
    description = ("Place the world's notable physical features and places: natural landmarks, "
                   "districts, waterfronts, striking locations — whatever fits this world.")
    after = "terrain_generation"
    guidance = _GUIDANCE_TERRAIN
    schema = {
        "areas": {"type": "list", "label": "Areas", "rerollable": True, "item_schema": {
            "name": {"type": "string", "label": "Area Name"},
            "terrain": {
                "type": "string",
                "label": "Character",
                "description": "Dominant terrain or character of this area (used to carve its territory on the map).",
            },
            "description": {"type": "text", "label": "Description"},
        }},
        "landmarks": {"type": "list", "label": "Landmarks", "rerollable": True, "item_schema": {
            "scope": {
                "type": "string",
                "label": "Map Scope",
                "description": "Which map this belongs to: empty for the main world map, or a parallel map's name from hierarchy_design.",
            },
            "region": {
                "type": "string",
                "label": "Region",
                "description": "Name of one of this step's areas the feature sits in; empty if none fits.",
            },
            "name": {"type": "string", "label": "Name"},
            "type": {"type": "string", "label": "Landmark Type"},
            "part_of": {
                "type": "string",
                "label": "Part Of",
                "description": "Name of another authored place this feature belongs to; empty for a standalone place.",
                "conditional": True,
            },
            "relation": {
                "type": "select",
                "label": "Relation",
                "options": ["adjacent", "inside"],
                "description": "With part_of: 'adjacent' places it right beside its parent on the map; 'inside' makes it a room within the parent, revealed when the parent is explored.",
                "conditional": True,
            },
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
