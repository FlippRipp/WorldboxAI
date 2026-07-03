from wbworldgen.worldgen.base import Step, register, USES_MAP


@register
class MapGenerationStep(Step):
    id = "map_generation"
    label = "World Map"
    description = "Generate an organic node-graph map with settlements, landmarks, and waypoints connected by travel routes."
    after = "society_factions"
    uses = USES_MAP
    schema = {
        "total_nodes": {"type": "number", "label": "Settlement Density", "min": 30, "max": 500, "default": 100},
    }
