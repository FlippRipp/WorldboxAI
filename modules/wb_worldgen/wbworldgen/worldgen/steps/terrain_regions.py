from wbworldgen.worldgen.base import Step, register


@register
class TerrainRegionsStep(Step):
    id = "terrain_regions"
    label = "Terrain & Regions"
    description = "Define the physical geography: regions with terrain, climate, and natural features per layer."
    after = "terrain_generation"
    guidance = (
        "The terrain_generation step output describes the ACTUAL generated "
        "geography per surface layer (biome mix, coastline/rivers/lakes, climate, "
        "elevation spread). Make each region's terrain and climate consistent with "
        "that summary — e.g. if the layer is mostly desert with a coastline, the "
        "regions should reflect arid interior + coastal zones rather than lush "
        "forests everywhere."
    )
    schema = {
        "regions": {"type": "list", "label": "Regions", "rerollable": True, "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "name": {"type": "string", "label": "Region Name"},
            "terrain": {"type": "text", "label": "Terrain"},
            "climate": {"type": "string", "label": "Climate"},
            "description": {"type": "text", "label": "Description"},
        }},
    }
