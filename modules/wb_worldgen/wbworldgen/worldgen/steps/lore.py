from wbworldgen.worldgen.base import Step, register


@register
class LoreStep(Step):
    id = "lore"
    label = "Background & History"
    description = ("Establish the world's identity: its name, premise, origins, "
                   "defining periods of its past, and the central tension of the present.")
    after = "world_rules"
    schema = {
        "world_name": {"type": "string", "label": "World Name"},
        "premise": {"type": "text", "label": "Premise"},
        "creation_myth": {"type": "text", "label": "Origins", "description": "How this setting came to be — a creation myth, a founding story, or plain recent history, whichever fits the world."},
        "historical_eras": {"type": "list", "label": "Eras & Periods", "rerollable": True, "item_schema": {"name": "string", "duration": "string", "summary": "string"}},
        "central_conflict": {"type": "text", "label": "Central Tension", "description": "The defining tension of the present — a war, a rivalry, a social pressure, a quiet longing. Scale it to the world."},
    }
