import copy

from wbworldgen.worldgen.base import Step, register


def brief_rules(world_state: dict) -> list[str]:
    """The ideation brief's co-authored rules, cleaned; [] without a brief.
    Shared with the agent toolbox: patch_step guards these from being
    dropped out of custom_rules."""
    brief = world_state.get("brief")
    if not isinstance(brief, dict):
        return []
    return [str(r).strip() for r in (brief.get("rules") or []) if str(r).strip()]

#: What makes a good world rule — the one doctrine shared by this step's
#: generation guidance and the ideation conversation (C4), so the rules the
#: player co-authors in chat and the rules this step generates are held to
#: the same standard.
RULES_DOCTRINE = """\
A world rule is a practical statement about how the world and its inhabitants operate day-to-day.
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

_GUIDANCE = "\nEach custom_rule is one world rule. " + RULES_DOCTRINE


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

    async def generate(self, ctx) -> dict:
        """The declarative generation, plus the ideation brief's co-authored
        rules as fixed input (C4/D4): they are injected into the generation
        prompt and enforced verbatim at the head of custom_rules — the step
        expands them, every regeneration keeps honoring them, and downstream
        consumers keep their contract (the rules land in custom_rules, the
        schema is untouched). Without a brief this IS the declarative path,
        unchanged."""
        agreed = brief_rules(ctx.world_state)
        step = self
        if agreed:
            step = copy.copy(self)
            step.guidance = (
                f"{self.guidance}\n\n"
                "The player and the AI already agreed on these world rules "
                "during ideation. They are fixed input, not suggestions: "
                "include EVERY one of them in custom_rules verbatim (word for "
                "word, first, in this order), then add further rules that "
                "extend and deepen them without contradicting any:\n"
                + "\n".join(f"- {r}" for r in agreed))
        data = await ctx.services.generate_declarative(
            step, ctx.world_state, ctx.user_prompt, ctx.user_note,
            force_mock=ctx.force_mock)
        if agreed and isinstance(data, dict):
            existing = data.get("custom_rules")
            existing = ([str(r).strip() for r in existing if str(r).strip()]
                        if isinstance(existing, list) else [])
            # Verbatim enforcement: the co-authored rules lead, in their
            # agreed order; the model's output only extends them.
            data["custom_rules"] = agreed + [r for r in existing if r not in agreed]
        return data
