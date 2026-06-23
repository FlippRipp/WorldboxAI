from backend.engine.worldgen.base import Step, register


@register
class SocietyFactionsStep(Step):
    id = "society_factions"
    label = "Society & Factions"
    description = "Define the people: factions, settlements, and significant man-made landmarks for each region."
    after = "natural_landmarks"
    schema = {
        "factions": {"type": "list", "label": "Factions", "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "region": {"type": "string", "label": "Region"},
            "name": {"type": "string", "label": "Faction Name"},
            "type": {"type": "string", "label": "Faction Type"},
            "description": {"type": "text", "label": "Description"},
            "settlements": {"type": "list", "label": "Settlements", "item_type": "string"},
            "significant_landmarks": {"type": "list", "label": "Significant Landmarks", "item_type": "string"},
        }},
    }
