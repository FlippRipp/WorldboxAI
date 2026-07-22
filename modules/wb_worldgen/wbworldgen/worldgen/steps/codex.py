"""Codex step — the world's lorebook: how its systems actually work.

The ``lore`` step establishes identity and history; this step establishes
*reference*: magic, species, technology, items, faiths, customs — the
in-world encyclopedia a storyteller consults mid-scene. The domain list is
itself an AI decision (the ``hierarchy_design`` precedent): the model
declares which domains THIS world needs — a mythic world declares magic and
species, a slice-of-life city maybe only institutions, a mundane world
honestly declares none. A declared domain with no entries is a blocking
lint finding (``codex_domain_empty`` — worldgen/codex.py), which is what
turns the declaration into a checkable obligation.

Entries are written to stand alone: at play time each entry is embedded
individually into the save's semantic world index and retrieved when the
scene resembles it. Always-on truths belong in world_rules (ambient in
every storyteller turn); the codex is the retrieved depth behind them.
"""

from wbworldgen.worldgen.base import Step, register

_GUIDANCE = """
Declare ONLY the domains this world actually needs — the domains are a design
decision, not a template to fill. A world of wild magic needs a magic domain;
a modern realist drama may need none at all (empty lists are legal and
honest). Every domain you declare MUST have at least one entry; an empty
domain is a contract violation.

Each entry is a self-contained encyclopedia article, retrieved individually
at play time: write it so it makes sense alone, without the other entries.
The summary is the one-breath version; details carries the depth — concrete
mechanics, names, costs, exceptions, the texture a storyteller can narrate
from. Avoid vague mood ("magic is mysterious"); write how things WORK.

Division of labor: fundamental always-on truths ("magic always costs a
memory") belong in the World Rules step, which every storytelling call sees.
The codex holds the depth BEHIND those rules — do not duplicate the rules,
deepen them.

Set subject to the name of ONE specific place (a map or named location) when
an entry belongs to it alone — a scoped entry reaches only that place's
content. Leave subject empty for world-wide reference.
"""


@register
class CodexStep(Step):
    id = "codex"
    label = "Codex"
    description = ("The world's lorebook: how its systems work — magic, "
                   "species, technology, items, faiths, customs — as "
                   "self-contained reference entries under world-specific "
                   "domains the step itself declares.")
    after = "lore"
    produces = ("codex",)
    guidance = _GUIDANCE
    schema = {
        "domains": {
            "type": "list", "label": "Domains",
            "description": ("The lore domains THIS world needs (magic, species, "
                            "technology, items, religion, customs, ...) — only "
                            "what the premise calls for; empty for worlds "
                            "without such systems."),
            "item_schema": {
                "name": {"type": "string", "label": "Domain"},
                "reason": {"type": "string", "label": "Why this world needs it"},
            },
        },
        "entries": {
            "type": "list", "label": "Entries", "rerollable": True,
            "item_schema": {
                "domain": {"type": "string", "label": "Domain",
                           "description": "One of the declared domain names."},
                "name": {"type": "string", "label": "Name"},
                "summary": {"type": "string", "label": "Summary",
                            "description": "The one-breath version of this entry."},
                "details": {"type": "text", "label": "Details",
                            "description": "The full reference: concrete mechanics, names, costs, exceptions."},
                "subject": {"type": "string", "label": "Subject",
                            "description": ("Name of the ONE place this entry belongs to "
                                            "(a map or named location); empty = world-wide.")},
            },
        },
    }

    def contribute_to_compiled(self, steps_data: dict, compiled: dict):
        data = (steps_data.get("codex") or {}).get("data")
        if isinstance(data, dict) and (data.get("domains") or data.get("entries")):
            compiled["codex"] = {
                "domains": data.get("domains") or [],
                "entries": data.get("entries") or [],
            }
