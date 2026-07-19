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
    # The PROCEDURAL root generator's one knob. Authored roots (abstract /
    # interior-style worlds) ignore it by design — the author reads the map
    # directive and picks its own count — so run_step rejects it for those
    # worlds with a pointer to the steering note instead of silently
    # dropping it (P7 at the config layer).
    config_schema = {
        "total_nodes": {
            "type": "integer", "min": 30, "max": 500,
            "description": "Node count for the procedural root generator "
                           "(default 100). Authored roots (abstract/interior "
                           "worlds) choose their own count — steer those "
                           "with the note instead.",
        },
    }
