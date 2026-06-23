from backend.engine.worldgen.base import Step, register


@register
class LoreStep(Step):
    id = "lore"
    label = "Overarching Lore"
    description = "Build the world's creation myth, history, central conflict, and key eras."
    after = "world_rules"
    schema = {
        "world_name": {"type": "string", "label": "World Name"},
        "premise": {"type": "text", "label": "Premise"},
        "creation_myth": {"type": "text", "label": "Creation Myth"},
        "historical_eras": {"type": "list", "label": "Historical Eras", "item_schema": {"name": "string", "duration": "string", "summary": "string"}},
        "central_conflict": {"type": "text", "label": "Central Conflict"},
    }
