from wbworldgen.worldgen.base import Step, register, USES_MAP


@register
class MapGenerationStep(Step):
    id = "map_generation"
    label = "World Map"
    description = "Generate an organic node-graph map with settlements, landmarks, and waypoints connected by travel routes."
    after = "society_factions"
    uses = USES_MAP
    # A legacy fallback draws a procedural default when no hierarchy exists
    # (old-world replay); new builds are expected to design the structure
    # first, so the declared contract requires it.
    requires = ("hierarchy",)
    produces = ("maps",)
    schema = {
        "total_nodes": {"type": "number", "label": "Settlement Density", "min": 30, "max": 500, "default": 100},
    }
