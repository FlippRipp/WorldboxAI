from backend.engine.worldgen.base import Step, register

_GUIDANCE = """
count_hint is a small integer (1-5) indicating roughly how many physical locations
connect two layers. For example, count_hint: 3 means ~3 entrances between the
overworld and underground. Use small numbers — 2000 is nonsensical, typical values are 1-4."""


@register
class LayerDesignStep(Step):
    id = "layer_design"
    label = "World Layers"
    description = "Determine if the world has multiple layers (surface, underground, sky, ocean) and how they connect."
    after = "lore"
    guidance = _GUIDANCE
    schema = {
        "has_multiple_layers": {"type": "boolean", "label": "Has Multiple Layers"},
        "layers": {"type": "list", "label": "Layers", "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "name": {"type": "string", "label": "Layer Name"},
            "layer_type": {"type": "string", "label": "Layer Type"},
            "description": {"type": "text", "label": "Description"},
            "index": {"type": "number", "label": "Order Index"},
        }},
        "connections": {"type": "list", "label": "Inter-layer Connections", "item_schema": {
            "from_layer": {"type": "string", "label": "From Layer"},
            "to_layer": {"type": "string", "label": "To Layer"},
            "connection_type": {"type": "string", "label": "Connection Type"},
            "description": {"type": "text", "label": "Description"},
            "count_hint": {"type": "number", "label": "Connection Points", "min": 1, "max": 6, "default": 2, "description": "How many locations link these layers (e.g., 3 dungeon entrances). Typically 1-4."},
        }},
    }
