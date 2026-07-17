from wbworldgen.worldgen.base import Step, register


@register
class SocietyFactionsStep(Step):
    id = "society_factions"
    label = "Groups & Communities"
    description = ("Define the people: the organizations, communities, circles and powers "
                   "of this world, and the places they hold.")
    after = "natural_landmarks"
    schema = {
        "factions": {"type": "list", "label": "Groups", "rerollable": True, "item_schema": {
            "scope": {
                "type": "string",
                "label": "Map Scope",
                "description": "Which map this group calls home: empty for the main world map, or a parallel map's name from hierarchy_design.",
            },
            "name": {"type": "string", "label": "Group Name"},
            "type": {"type": "string", "label": "Group Type"},
            "description": {"type": "text", "label": "Description"},
            "settlements": {"type": "list", "label": "Settlements", "item_type": "string", "description": "Distinct named places this group holds or frequents — these become major map locations."},
            "significant_landmarks": {"type": "list", "label": "Significant Landmarks", "item_type": "string"},
        }},
    }
