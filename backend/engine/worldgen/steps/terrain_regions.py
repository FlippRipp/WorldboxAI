from backend.engine.worldgen.base import Step, register


@register
class TerrainRegionsStep(Step):
    id = "terrain_regions"
    label = "Terrain & Regions"
    description = "Define the physical geography: regions with terrain, climate, and natural features per layer."
    after = "layer_rules"
    schema = {
        "regions": {"type": "list", "label": "Regions", "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "name": {"type": "string", "label": "Region Name"},
            "terrain": {"type": "text", "label": "Terrain"},
            "climate": {"type": "string", "label": "Climate"},
            "description": {"type": "text", "label": "Description"},
        }},
    }
