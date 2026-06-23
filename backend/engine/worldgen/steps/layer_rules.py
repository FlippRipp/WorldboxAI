from backend.engine.worldgen.base import Step, register

_GUIDANCE = """
Review the world_rules.custom_rules from the world_rules step. Rules that apply to the
entire world should stay in world_rules. Rules that only apply to a specific layer should
be moved to layer_rules[layer_id].rules.

Then generate new unique rules for each layer based on its type and description.
Layer rules should describe practical, day-to-day truths specific to that layer.

Example: if layer_design has "overworld" and "underground", and world_rules has
"Sunlight burns the skin", that should move to overworld's layer_rules.
If world_rules has "Oaths are magically binding everywhere", that stays in world_rules."""


@register
class LayerRulesStep(Step):
    id = "layer_rules"
    label = "Layer Rules"
    description = "Review world rules and create unique rules for each layer. Move any world rules that only apply to a specific layer."
    after = "layer_design"
    guidance = _GUIDANCE
    schema = {
        "layer_rules": {"type": "list", "label": "Per-Layer Rules", "item_schema": {
            "layer_id": {"type": "string", "label": "Layer ID"},
            "name": {"type": "string", "label": "Layer Name"},
            "rules": {"type": "list", "label": "Rules", "item_type": "string"},
        }},
        "world_rules": {"type": "list", "label": "Remaining World Rules", "item_type": "string", "description": "Rules that apply globally. Layer-specific rules moved to layer_rules."},
    }
