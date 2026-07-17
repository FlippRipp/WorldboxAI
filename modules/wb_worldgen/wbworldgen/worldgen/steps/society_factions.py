from wbworldgen.worldgen.base import Step, register


@register
class SocietyFactionsStep(Step):
    id = "society_factions"
    label = "Society & Factions"
    description = "Define the people: factions, settlements, and significant man-made landmarks for each region."
    after = "natural_landmarks"
    schema = {
        "factions": {"type": "list", "label": "Factions", "rerollable": True, "item_schema": {
            "scope": {
                "type": "string",
                "label": "Map Scope",
                "description": "Which map this faction calls home: empty for the main world map, or a parallel map's name from hierarchy_design.",
            },
            "name": {"type": "string", "label": "Faction Name"},
            "type": {"type": "string", "label": "Faction Type"},
            "description": {"type": "text", "label": "Description"},
            "settlements": {"type": "list", "label": "Settlements", "item_type": "string"},
            "significant_landmarks": {"type": "list", "label": "Significant Landmarks", "item_type": "string"},
        }},
    }
