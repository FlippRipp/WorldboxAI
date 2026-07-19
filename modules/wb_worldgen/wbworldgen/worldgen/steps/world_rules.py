from wbworldgen.worldgen.base import Step, register

_GUIDANCE = """
Each custom_rule is a practical statement about how the world and its inhabitants operate day-to-day.
Think: what would an inhabitant of this world take for granted as "just the way things are"?
Match the rules to the world's genre — they can be fantastical, mundane, or anywhere in between.

Good examples:
- "Monsters aggressively hunt humans and will track them for miles"
- "No merchant will trade with strangers without a blood-sealed contract"
- "Everyone commutes by the last train home — missing it strands you in the city overnight"
- "Neighbors know each other's business; nothing stays private on this street for long"
- "Every ship burn is logged with traffic control — moving quietly means moving slowly"

Avoid vague philosophy: "Magic is dangerous", "Nature is hostile", "Society is oppressive".
Avoid game mechanics: "Players get +1", "Damage is doubled at night"."""


@register
class WorldRulesStep(Step):
    id = "world_rules"
    label = "World Rules"
    description = "Define the genre, tone, era, and practical world constraints that shape daily life."
    after = "world_form"
    produces = ("rules",)
    guidance = _GUIDANCE
    schema = {
        "genre": {"type": "string", "label": "Genre"},
        "tone": {"type": "string", "label": "Tone"},
        "magic_level": {"type": "select", "label": "Magic Level", "options": ["none", "rare", "common", "ubiquitous"], "description": "How supernatural this world is. Use 'none' for realistic and modern settings."},
        "tech_era": {"type": "string", "label": "Technology Era"},
        "lethality": {"type": "slider", "label": "Lethality", "min": 1, "max": 10},
        "custom_rules": {"type": "list", "label": "World Constraints", "item_type": "string", "rerollable": True, "description": "Fundamental truths about how the world works — not game mechanics."},
    }
