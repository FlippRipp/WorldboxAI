from wbworldgen.worldgen.base import Step, register


@register
class SocietyFactionsStep(Step):
    id = "society_factions"
    label = "Groups & Communities"
    description = ("Define the people: the organizations, communities, circles and powers "
                   "of this world, and the places they hold.")
    after = "natural_landmarks"
    guidance = (
        "Set region to the name of the region or district (from Terrain & "
        "Regions) where the group is based, so its places land in the right "
        "part of the map. A group's significant landmarks are placed NEAR its "
        "first settlement — list only places that stand on the map in their "
        "own right. A room or space INSIDE another place (an office inside a "
        "school, a shrine room inside a temple) belongs in the Notable "
        "Features step instead, as a feature with part_of and relation "
        "'inside'."
    )
    schema = {
        "factions": {"type": "list", "label": "Groups", "rerollable": True, "item_schema": {
            "scope": {
                "type": "string",
                "label": "Map Scope",
                "description": "Which map this group calls home: empty for the main world map, or a parallel map's name from hierarchy_design.",
            },
            "region": {
                "type": "string",
                "label": "Region",
                "description": "Name of one of the Notable Features areas where this group is based; empty if none fits.",
            },
            "name": {"type": "string", "label": "Group Name"},
            "type": {"type": "string", "label": "Group Type"},
            "description": {"type": "text", "label": "Description"},
            "settlements": {"type": "list", "label": "Settlements", "item_type": "string", "description": "Distinct named places this group holds or frequents — these become major map locations."},
            "significant_landmarks": {"type": "list", "label": "Significant Landmarks", "item_type": "string", "description": "Standalone places tied to this group — placed near the group's first settlement."},
        }},
    }
