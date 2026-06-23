from backend.engine.worldgen.base import Step, register


@register
class NaturalLandmarksStep(Step):
    id = "natural_landmarks"
    label = "Natural Landmarks"
    description = "Place notable natural features: mountains, forests, rivers, caverns, and other landmarks per region."
    after = "terrain_regions"
    schema = {
        "landmarks": {"type": "list", "label": "Landmarks", "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "region": {"type": "string", "label": "Region"},
            "name": {"type": "string", "label": "Name"},
            "type": {"type": "string", "label": "Landmark Type"},
            "description": {"type": "text", "label": "Description"},
        }},
    }
