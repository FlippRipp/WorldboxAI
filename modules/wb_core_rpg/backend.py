"""Core RPG System -- stats, skills, XP, leveling, action judgement, HP."""
import asyncio
import math
import os
import random
import json
import re
from dataclasses import dataclass, field


STAT_NAMES = ["power", "agility", "vitality", "intelligence", "spirit", "charm"]

OLD_STAT_MAP = {
    "strength": "power",
    "dexterity": "agility",
    "constitution": "vitality",
    "wisdom": "spirit",
    "charisma": "charm",
    "intelligence": "intelligence",
}

STAT_DISPLAY = {
    "power": "Power",
    "agility": "Agility",
    "vitality": "Vitality",
    "intelligence": "Intelligence",
    "spirit": "Spirit",
    "charm": "Charm",
}

DEFAULT_STAT_TIERS = [
    {"min": 1, "max": 4, "label": "Severely Impaired"},
    {"min": 5, "max": 8, "label": "Below Average"},
    {"min": 9, "max": 12, "label": "Average Human"},
    {"min": 13, "max": 16, "label": "Above Average / Trained"},
    {"min": 17, "max": 20, "label": "Expert / Peak Human"},
    {"min": 21, "max": 25, "label": "Superhuman"},
    {"min": 26, "max": 30, "label": "Legendary / Demigod"},
]


def _tier_for(value: int, tier_list: list[dict]) -> str:
    for tier in tier_list:
        if tier["min"] <= value <= tier["max"]:
            return tier["label"]
    return "Unknown"


SKILL_ACTION_KEYWORDS = {
    "power": ["lift", "push", "break", "smash", "force", "throw", "carry", "shoulder", "crush", "wrestle"],
    "agility": ["dodge", "aim", "sneak", "pick", "lock", "balance", "juggle", "catch", "slip", "leap", "vault"],
    "vitality": ["endure", "resist", "survive", "withstand", "hold breath", "tolerate", "run", "march", "tank"],
    "intelligence": ["recall", "identify", "solve", "deduce", "analyze", "examine", "translate", "calculate", "decipher", "study"],
    "spirit": ["sense", "notice", "perceive", "track", "meditate", "heal", "diagnose", "insight", "focus", "commune"],
    "charm": ["persuade", "intimidate", "deceive", "bluff", "bargain", "negotiate", "perform", "inspire", "seduce", "befriend"],
}

# Each action_rating_strictness value maps to a difficulty tier:
# (label, judge guidance, no_and_max, fail_max, success_min). The prompt gets
# ONLY the chosen tier, never the 1-10 scale. Feasibility scores resolve into
# four outcomes (the DM's "no and / no / yes but / yes"):
#   1..no_and_max               fails, and the situation worsens
#   no_and_max+1..fail_max      fails
#   fail_max+1..success_min-1   partial success at a cost
#   success_min..10             success
# Higher tiers widen the failure side (Brutal fails everything up to 5).
# Either failure band may be empty: no_and_max of 0 means the tier never
# worsens a failure (Power Fantasy); no_and_max == fail_max means every
# failure worsens the situation (Brutal). The same thresholds drive the
# storyteller ruling and success-conditioned XP.
STRICTNESS_TIERS = {
    1: ("Power Fantasy", "the player is the unstoppable protagonist of this story. Practically anything they attempt succeeds, and succeeds with flair; reserve outright failure for the truly impossible. Never rate a merely unlikely attempt as an outright failure.", 0, 1, 5),
    2: ("Cinematic", "rule-of-cool cinema. Lean very generous - style, momentum, and daring carry attempts that mundane logic would question; when torn between two scores, pick the higher. Never rate a merely unlikely attempt as an outright failure.", 1, 2, 6),
    3: ("Heroic", "the player is the hero. Favor them in any doubt; success is the default for anything plausibly within reach. Never rate a merely unlikely attempt as an outright failure.", 1, 2, 6),
    4: ("Favorable", "the wind is at the player's back. Read attempts charitably, but let real overreach still fall short. Never rate a merely unlikely attempt as an outright failure.", 1, 2, 7),
    5: ("Balanced", "judge each attempt on its merits - success and failure are both live outcomes, decided by capability and circumstance. Never rate a merely unlikely attempt as an outright failure.", 1, 2, 7),
    6: ("Gritty", "the world has teeth. Read attempts realistically and let sloppy or lazy plans underperform. Never rate a merely unlikely attempt as an outright failure.", 2, 3, 7),
    7: ("Demanding", "capabilities are judged strictly. An attempt needs a genuine, demonstrated edge to succeed outright; ambition without preparation lands in the costly middle at best. Never rate a merely unlikely attempt as an outright failure.", 2, 3, 7),
    8: ("Harsh", "overreach is punished. Score ambitious attempts well below what they would otherwise earn; even sound plans succeed at a cost, and only well-prepared attempts squarely within ability succeed outright. Never rate a merely unlikely attempt as an outright failure.", 2, 4, 8),
    9: ("Merciless", "assume the least favorable plausible reading of every attempt. Anything beyond the character's proven, demonstrated ability fails outright; success requires overwhelming advantage; most attempts fail or scrape by at a heavy price.", 3, 4, 8),
    10: ("Brutal", "success is almost impossible. Only trivial actions or an overwhelming, established advantage succeed outright; solid attempts within proven ability scrape by at a cost at best; anything ambitious, uncertain, or beyond proven ability fails and makes things worse. The world is actively hostile - even a merely unlikely action may fail outright.", 5, 5, 9),
}


def _strictness_tier(config: dict) -> tuple[str, str, int, int, int]:
    try:
        strictness = int(config.get("action_rating_strictness", 5))
    except (TypeError, ValueError):
        strictness = 5
    return STRICTNESS_TIERS[max(1, min(10, strictness))]


def _score_span(lo: int, hi: int) -> str:
    return str(lo) if lo == hi else f"{lo}-{hi}"


# The static Reader mutation schema, for on_mutation_schema to extend with the
# character's live skill/effect lists (dynamic schema entries REPLACE the
# manifest's, so the base text must be re-included).
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json"), encoding="utf-8") as _f:
        _BASE_MUTATION_SCHEMA = json.load(_f).get("mutation_schema", {})
except (OSError, json.JSONDecodeError):
    _BASE_MUTATION_SCHEMA = {}


# --------------------------------------------------------------------------
# Customizable instruction slots
#
# Each LLM prompt this module builds splits into a creative "directive" -- the
# part a scenario or story may override via module_instructions -- and fixed
# scaffolding (context sections, exact counts, JSON contracts) that overrides
# can never touch, so output parsing cannot break. The defaults below are the
# verbatim directive text the prompts have always used; a missing or empty
# override means the default applies.
# --------------------------------------------------------------------------

DIRECTIVE_ACTION_ASSESSMENT = """- Social actions: the outcome depends on the TARGET's likely disposition as shown in the recent story and world context, not on the player's stats. A bold ask to a receptive, bored, curious, or amused character is plausible even for a weak character. Only rate a social action 1-2 if the target's established nature makes acceptance truly impossible.
- Reward creativity: a clever, novel, or dramatically interesting approach that fits the established fiction rates one band higher than a blunt attempt at the same goal. Punish contradiction of established facts, not ambition.
- Status effects: weigh them like circumstances, not stats. A [bad] effect lowers feasibility of actions it would plausibly impede (a broken leg makes sprinting far harder) and can push a directly-blocked attempt to 1-2; a [good] effect raises feasibility of actions it aids. The higher an effect's severity, the more it sways the score. Effects unrelated to the attempt change nothing."""

DIRECTIVE_SKILL_CATEGORIES = """BROAD domains of ability this character could plausibly begin learning in this world and at this point in the story. Each category is a wide umbrella that could hold dozens of very different skills - broad strokes, never narrow specialties like "dagger throwing" or "rose gardening". But name them with imagination, in this world's own voice: "The Red Trades" beats "Combat", "Whisperwork" beats "Stealth", "Hearth & Harvest" beats "Survival". A bland textbook label is a failure; so is a name so cryptic the summary can't rescue it. Let the one-clause summary make plain what broad ground the name covers. The 10 must be meaningfully different from each other and together should span most of what anyone could learn in this world. Split them evenly: 5 drawn from the story - its themes, its current events, what this character already does or what the tale has hinted at - and 5 that stand apart from all of that, ability domains this world supports regardless of where the story happens to be right now."""

DIRECTIVE_SKILL_OPTIONS = """The 5 must vary widely in flavor and approach - do not make them five shades of the same idea. Most should belong squarely to the theme, but 1-2 may take a loose, sideways, or surprising interpretation of it; a skill that fits the theme imperfectly but fits the character or story well is better than a fifth on-the-nose variant."""

DIRECTIVE_SKILL_REFINE = """- Keep its identity: same ability, same manner of working. You may polish the name (1-4 words) but never change what the skill IS.
- The description is 1-2 tight sentences and completely FREE-STANDING: exactly what the power does, how it manifests, and any limit or cost, grounded in this world and where the story stands now. Concrete over flowery."""

DIRECTIVE_EVOLUTION_OPTIONS = """Every path must promise a SIGNIFICANT power-up - a major leap beyond the current form, never a sidegrade, tradeoff, or flavor change.

The FIRST path is the pure path: the skill stays exactly what it is but grows dramatically stronger - same identity, same manner of working, pushed far past its current limits. Its theme is a short label conveying refinement or ascension of the skill as it already is (like "Perfected", "Transcendent", "True Mastery" - but fitting THIS skill), and its summary says how the skill's existing strengths intensify.

The other THREE paths each take the skill in a distinct new direction. Each theme is a short evocative label of 1-3 words (like "Brutal", "Efficiency", "Stealthy" - but fitting THIS skill and world), plus one short clause summarizing what that path emphasizes. These three directions must be meaningfully different from each other, from the pure path, and from the skill's current form. Do not repeat the skill's current evolution theme."""

DIRECTIVE_EVOLVE = """- It MUST be a SIGNIFICANT power-up over the current form: broader in scope, stronger in effect, and with fewer limits - a dramatic leap, not a rename or minor tweak. It should feel like a new class of power.
- The power-up applies to the skill's BENEFITS only. Any costs, drawbacks, or negative side effects must NOT grow stronger with the evolution: keep them at their current severity or reduce them, and never introduce new ones."""

INSTRUCTION_SLOTS = [
    {
        "id": "action_assessment",
        "label": "Action Judging & XP",
        "description": "How player actions are judged for feasibility and difficulty. Succeeding at judged actions is what earns XP, so this steers what kinds of attempts pay off.",
        "default": DIRECTIVE_ACTION_ASSESSMENT,
    },
    {
        "id": "skill_categories",
        "label": "Skill Menu: Categories",
        "description": "What the 10 browsable skill categories in the add-skill menu should be like.",
        "default": DIRECTIVE_SKILL_CATEGORIES,
    },
    {
        "id": "skill_options",
        "label": "Skill Menu: Skills",
        "description": "What the 5 skill proposals inside a category (or search) should be like. The prompt states the character's current level, so instructions here can scale what is offered with level.",
        "default": DIRECTIVE_SKILL_OPTIONS,
    },
    {
        "id": "skill_refine",
        "label": "Skill Menu: Finalize Skill",
        "description": "How a chosen draft skill is polished before it lands on the character sheet. The prompt states the character's current level, so instructions here can scale skill strength with level.",
        "default": DIRECTIVE_SKILL_REFINE,
    },
    {
        "id": "evolution_options",
        "label": "Evolution: Paths",
        "description": "What the 4 evolution paths offered for a maxed-out skill should be like. The prompt states the character's current level, so instructions here can scale the paths with level.",
        "default": DIRECTIVE_EVOLUTION_OPTIONS,
    },
    {
        "id": "evolve",
        "label": "Evolution: Final Form",
        "description": "How the evolved form of a skill is designed once a path is chosen. The prompt states the character's current level, so instructions here can scale the evolved power with level.",
        "default": DIRECTIVE_EVOLVE,
    },
]

_SLOT_DEFAULTS = {slot["id"]: slot["default"] for slot in INSTRUCTION_SLOTS}


def get_instruction_slots() -> list[dict]:
    """Generic module contract: the customizable instruction slots this module
    exposes. The host serves these to the UI and the rewrite endpoint."""
    return [dict(slot) for slot in INSTRUCTION_SLOTS]


def _directive(slot_id: str, instructions: dict | None) -> str:
    """The directive text for a prompt slot: the story/scenario override when
    one is set, otherwise the built-in default."""
    text = str((instructions or {}).get(slot_id) or "").strip()
    return text or _SLOT_DEFAULTS[slot_id]


# Status effects are temporary conditions (broken leg, poisoned, blessed,
# brainwashed) with a ONE-sentence description and a severity (1-10). They
# expire after a number of player turns (duration_turns) or at an in-world
# clock time from wb_time_tracker (expires_at_minutes, absolute
# total_minutes_elapsed); with neither set they last until story events
# remove them. turns_active counts how long an effect has lingered.
MAX_STATUS_EFFECTS = 3

# A bad, indefinite status effect at least this severe that has lingered at
# least this many turns hardens into a lasting curse skill.
CURSE_SEVERITY_MIN = 7
CURSE_AGE_TURNS = 10


def _sanitize_status_effect(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name", "")).strip().lower()
    if not name:
        return None
    kind = raw.get("kind")
    if kind not in ("good", "bad"):
        kind = "bad"
    effect = {
        "name": name,
        "description": str(raw.get("description", "")).strip() or name,
        "kind": kind,
        "severity": 3,
        "duration_turns": None,
        "expires_at_minutes": None,
        "turns_active": 0,
    }
    severity = raw.get("severity")
    if isinstance(severity, (int, float)) and not isinstance(severity, bool):
        effect["severity"] = max(1, min(10, int(severity)))
    for key in ("duration_turns", "expires_at_minutes", "turns_active"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and int(val) > 0:
            effect[key] = int(val)
    return effect


def _clock_minutes(state: dict) -> int | None:
    """wb_time_tracker's monotonic in-world clock, or None when the time
    module is inactive or hasn't written a clock yet."""
    clock = state.get("module_data", {}).get("wb_time_tracker", {}).get("clock")
    if isinstance(clock, dict):
        val = clock.get("total_minutes_elapsed")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return int(val)
    return None


def _effect_duration_label(effect: dict, now_minutes: int | None) -> str:
    turns = effect.get("duration_turns")
    if turns is not None:
        return f"{turns} turn{'s' if turns != 1 else ''} left"
    expires_at = effect.get("expires_at_minutes")
    if expires_at is not None and now_minutes is not None:
        left = max(0, expires_at - now_minutes)
        if left >= 1440:
            return f"~{round(left / 1440)}d left"
        if left >= 60:
            return f"~{round(left / 60)}h left"
        return f"{left}m left"
    return "ongoing"


@dataclass
class Character:
    stats: dict[str, int] = field(default_factory=lambda: {s: 10 for s in STAT_NAMES})
    skills: dict[str, dict] = field(default_factory=dict)
    backstory: str = ""
    level: int = 1
    xp: int = 0
    hp: int = 85
    max_hp: int = 85
    practice_counters: dict[str, int] = field(default_factory=dict)
    stat_usage: dict[str, int] = field(default_factory=lambda: {s: 0 for s in STAT_NAMES})
    action_assessment: dict = field(default_factory=dict)
    unspent_attribute_points: int = 0
    unspent_skill_points: int = 0
    pending_evolutions: list = field(default_factory=list)
    recent_evolutions: list = field(default_factory=list)
    level_up_history: list = field(default_factory=list)
    status_effects: list = field(default_factory=list)

    def recalc_hp(self, hp_per_con: int = 7, hp_per_level: int = 2):
        self.max_hp = (self.stats.get("vitality", 10) * hp_per_con) + (self.level * hp_per_level)

    def is_unconscious(self) -> bool:
        return self.hp <= 0

    def to_dict(self):
        return {
            "stats": dict(self.stats),
            "skills": dict(self.skills),
            "backstory": self.backstory,
            "stat_tiers": DEFAULT_STAT_TIERS,
            "level": self.level,
            "xp": self.xp,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "practice_counters": dict(self.practice_counters),
            "stat_usage": dict(self.stat_usage),
            "action_assessment": dict(self.action_assessment),
            "unspent_attribute_points": self.unspent_attribute_points,
            "unspent_skill_points": self.unspent_skill_points,
            "pending_evolutions": list(self.pending_evolutions),
            "recent_evolutions": [dict(e) for e in self.recent_evolutions],
            "level_up_history": list(self.level_up_history),
            "status_effects": [dict(e) for e in self.status_effects],
        }

    @classmethod
    def from_dict(cls, d):
        c = cls()
        raw_stats = d.get("stats", {})
        for s in STAT_NAMES:
            val = raw_stats.get(s) or raw_stats.get(next((old for old, new in OLD_STAT_MAP.items() if new == s and old in raw_stats), ""), 10)
            c.stats[s] = max(1, min(30, int(val)))
        c.skills = {}
        for name, data in d.get("skills", {}).items():
            if isinstance(data, dict):
                skill = {
                    "rating": data.get("rating", 1),
                    "description": data.get("description", ""),
                    "trigger_words": data.get("trigger_words", []),
                    "type": data.get("type", "active"),
                }
                # Evolution fields must survive the per-turn round-trip.
                try:
                    tier = int(data.get("tier", 1))
                except (TypeError, ValueError):
                    tier = 1
                if tier > 1:
                    skill["tier"] = tier
                lineage = data.get("lineage")
                if isinstance(lineage, list) and lineage:
                    skill["lineage"] = [str(x) for x in lineage]
                theme = data.get("evolution_theme")
                if theme:
                    skill["evolution_theme"] = str(theme)
                c.skills[name] = skill
            else:
                c.skills[name] = {"rating": max(1, min(10, int(data))), "description": "", "trigger_words": [], "type": "active"}
        c.backstory = d.get("backstory", "")
        c.level = d.get("level", 1)
        c.xp = d.get("xp", 0)
        c.hp = d.get("hp", 0)
        c.max_hp = d.get("max_hp", 0)
        c.practice_counters = d.get("practice_counters", {})
        c.stat_usage = {s: 0 for s in STAT_NAMES}
        raw_usage = d.get("stat_usage", {})
        for s in STAT_NAMES:
            c.stat_usage[s] = raw_usage.get(s) or raw_usage.get(next((old for old, new in OLD_STAT_MAP.items() if new == s and old in raw_usage), ""), 0)
        if c.max_hp == 0:
            c.recalc_hp()
        if c.hp == 0:
            c.hp = c.max_hp
        c.action_assessment = d.get("action_assessment", {})
        if not isinstance(c.action_assessment, dict):
            c.action_assessment = {}
        try:
            c.unspent_attribute_points = max(0, int(d.get("unspent_attribute_points", 0)))
        except (TypeError, ValueError):
            c.unspent_attribute_points = 0
        try:
            c.unspent_skill_points = max(0, int(d.get("unspent_skill_points", 0)))
        except (TypeError, ValueError):
            c.unspent_skill_points = 0
        pending = d.get("pending_evolutions", [])
        c.pending_evolutions = [e for e in pending if isinstance(e, dict) and e.get("skill")] if isinstance(pending, list) else []
        raw_evos = d.get("recent_evolutions", [])
        c.recent_evolutions = []
        if isinstance(raw_evos, list):
            for e in raw_evos:
                if not (isinstance(e, dict) and e.get("old_name") and e.get("new_name")):
                    continue
                try:
                    tier = max(2, int(e.get("tier", 2)))
                except (TypeError, ValueError):
                    tier = 2
                c.recent_evolutions.append({
                    "old_name": str(e["old_name"]),
                    "new_name": str(e["new_name"]),
                    "tier": tier,
                    "theme": str(e.get("theme", "")),
                    "description": str(e.get("description", "")),
                    "announced": bool(e.get("announced", False)),
                })
        raw_effects = d.get("status_effects", [])
        c.status_effects = []
        if isinstance(raw_effects, list):
            for raw in raw_effects:
                effect = _sanitize_status_effect(raw)
                if effect is not None:
                    c.status_effects.append(effect)
        if len(c.status_effects) > MAX_STATUS_EFFECTS:
            # Saves from before the cap existed can carry more: keep the
            # strongest (ties favor the earlier entry) and drop the rest.
            keep = sorted(c.status_effects, key=lambda e: e.get("severity", 3), reverse=True)[:MAX_STATUS_EFFECTS]
            keep_ids = {id(e) for e in keep}
            dropped = [e["name"] for e in c.status_effects if id(e) not in keep_ids]
            c.status_effects = [e for e in c.status_effects if id(e) in keep_ids]
            print(f"[RPG] Trimmed status effects over the cap of {MAX_STATUS_EFFECTS}: dropped {', '.join(dropped)}")
        history = d.get("level_up_history", [])
        c.level_up_history = [e for e in history if isinstance(e, dict)] if isinstance(history, list) else []
        return c


# How much the base XP award is scaled by the assessed difficulty of an action.
# Harder actions are worth more; an impossible attempt earns nothing.
DIFFICULTY_XP_WEIGHT = {
    "trivial": 0.25,
    "easy": 0.5,
    "moderate": 1.0,
    "hard": 1.75,
    "extreme": 2.5,
    "impossible": 0.0,
}


def _xp_from_assessment(char: "Character", config: dict) -> int:
    """Deterministic XP for the turn, driven by the configured XP gain condition
    and this turn's action assessment. Returns 0 when the condition isn't met or
    no substantive action was assessed."""
    condition = config.get("xp_gain_condition", "successful_action")
    if condition in ("reader", "disabled"):
        return 0

    assessment = char.action_assessment or {}
    try:
        feasibility = int(assessment.get("feasibility"))
    except (TypeError, ValueError):
        # No substantive action was assessed this turn (trivial move, pure
        # dialog, or no player input) -> nothing to reward.
        return 0

    difficulty = str(assessment.get("difficulty", "moderate")).lower()
    # The difficulty tier's failure band decides what counts as a hard failure
    # (1-2 at Balanced, up to 1-5 at Brutal). Both failure bands earn nothing.
    _, _, _, fail_max, _ = _strictness_tier(config)
    succeeded = feasibility > fail_max

    if condition == "successful_action" and not succeeded:
        return 0
    if condition == "challenging_action" and difficulty not in ("hard", "extreme"):
        return 0
    # "any_action" awards regardless of outcome.

    base = config.get("xp_per_action", 10)
    if not isinstance(base, (int, float)) or base <= 0:
        base = 10
    weight = DIFFICULTY_XP_WEIGHT.get(difficulty, 1.0)
    return max(0, round(base * weight))


def xp_for_level(level: int, steepness: int = 2) -> int:
    return int(50 * (level ** steepness))


def total_xp_for_level(level: int, steepness: int = 2) -> int:
    total = 0
    for n in range(1, level):
        total += xp_for_level(n, steepness)
    return total


async def on_gather_context(state: dict, sdk) -> dict:
    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))
    config = state.get("module_configs", {}).get("wb_core_rpg", {})
    input_text = state.get("input_text", "").strip()

    updates = {}

    # Start each turn with a clean assessment so a prior turn's ruling can't be
    # reused (e.g. to re-award XP) on a turn with no substantive player action.
    char.action_assessment = {}

    # Evolution announcements feed exactly one generation: entries announced
    # on a previous turn are dropped, the rest are marked as this turn's.
    char.recent_evolutions = [e for e in char.recent_evolutions if not e.get("announced")]
    for evo in char.recent_evolutions:
        evo["announced"] = True

    # Track which stats are relevant to this action via keyword matching
    if input_text:
        text_lower = input_text.lower()
        for stat, keywords in SKILL_ACTION_KEYWORDS.items():
            for kw in keywords:
                if kw in text_lower:
                    char.stat_usage[stat] = char.stat_usage.get(stat, 0) + 1
                    break

        # Pre-assess action feasibility with a fast model call
        model_pref = config.get("practice_ai_model", "fastest")
        recent_story = [entry[-1200:] for entry in (state.get("history") or [])[-2:]]
        assessment = await _assess_action(
            input_text, char, config, sdk, model_pref, state.get("world_data"), recent_story,
            instructions=state.get("module_instructions"),
        )
        char.action_assessment = assessment

    # Practice-based progression: use AI to detect which skill the action uses
    progression = config.get("progression_system", "xp")
    if progression == "practice" and char.skills and input_text:
        active_skills = {n: d for n, d in char.skills.items() if d.get("type", "active") == "active"}
        if active_skills:
            model_pref = config.get("practice_ai_model", "fastest")
            skill_names = [f"{_skill_prompt_label(n, d)} ({d['rating']}/10)" for n, d in active_skills.items()]
            prompt = (
                f"The player just performed: \"{input_text}\"\n\n"
                f"Character skills: {', '.join(skill_names)}\n\n"
                f"Which single skill is most relevant to this action? "
                f"Respond with just the skill name exactly as listed, or \"none\" if no skill applies."
            )
            try:
                result = await sdk.llm.generate(prompt, model_preference=model_pref)
                detected = result.strip().lower()
                for skill_name in char.skills:
                    if skill_name.lower() == detected or skill_name.lower() in detected:
                        char.practice_counters.setdefault(skill_name, 0)
                        char.practice_counters[skill_name] += char.skills[skill_name]["rating"]
                        break
            except Exception:
                pass

    updates["module_data"] = {"wb_core_rpg": char.to_dict()}
    return updates


async def _assess_action(input_text: str, char: Character, config: dict, sdk, model_pref: str = "fastest", world_data: dict = None, recent_story: list = None, instructions: dict = None) -> dict:
    tier_list = config.get("stat_tiers", DEFAULT_STAT_TIERS) or DEFAULT_STAT_TIERS
    difficulty_label, difficulty_guidance, no_and_max, fail_max, success_min = _strictness_tier(config)
    outcome_parts = []
    if no_and_max >= 1:
        outcome_parts.append(f"{_score_span(1, no_and_max)} = the attempt fails and the situation worsens")
    if fail_max > no_and_max:
        outcome_parts.append(f"{_score_span(no_and_max + 1, fail_max)} = the attempt fails")
    outcome_parts.append(f"{_score_span(fail_max + 1, success_min - 1)} = partial success at a cost")
    outcome_parts.append(f"{_score_span(success_min, 10)} = success")
    outcome_mapping = "; ".join(outcome_parts)
    fail_span = _score_span(1, fail_max)

    active_skills = {n: d for n, d in char.skills.items() if d.get("type") == "active"}
    passive_skills = {n: d for n, d in char.skills.items() if d.get("type") == "passive"}
    curse_skills = {n: d for n, d in char.skills.items() if d.get("type") == "curse"}

    skill_lines = []
    if active_skills:
        skill_lines.append("Active skills: " + ", ".join(
            f'{_skill_prompt_label(n, d)} ({d["rating"]}/10): {d.get("description", "")}' for n, d in active_skills.items()
        ))
    if passive_skills:
        skill_lines.append("Passive skills: " + ", ".join(
            f'{_skill_prompt_label(n, d)} ({d["rating"]}/10): {d.get("description", "")}' for n, d in passive_skills.items()
        ))
    if any(_skill_tier(d) > 1 for d in char.skills.values()):
        skill_lines.append(
            "Note: skills marked [Tier N] are evolved forms - markedly more powerful "
            "than a Tier 1 skill at the same rating; each tier is a major step up in capability."
        )
    if curse_skills:
        for n, d in curse_skills.items():
            triggers = d.get("trigger_words", [])
            if triggers:
                skill_lines.append(f'Curse "{n}" ({d["rating"]}/10): triggers on [{", ".join(triggers)}] - {d.get("description", "")}')
            else:
                skill_lines.append(f'Curse "{n}" ({d["rating"]}/10) [always active]: {d.get("description", "")}')
    if char.status_effects:
        skill_lines.append("Status effects: " + "; ".join(
            f"[{e['kind']}, severity {e.get('severity', 3)}/10] {e['description']}" for e in char.status_effects))

    stats_line = ", ".join(f"{s}={char.stats[s]} ({_tier_for(char.stats[s], tier_list)})" for s in STAT_NAMES)
    unconscious = " [UNCONSCIOUS - cannot act physically]" if char.is_unconscious() else ""

    world_section = _world_context_section(world_data)

    story_section = ""
    if recent_story:
        story_section = "Recent story (most recent last):\n" + "\n---\n".join(recent_story) + "\n"

    prompt = f"""Assess this RPG action for a character. Output ONLY valid JSON, no other text.

First decide whether the player input is a substantive action - something with a contested outcome or mechanical consequence. If the input is pure dialog/speech with nothing at stake, or a trivial everyday action (standing up, walking across a room, looking around, picking up an ordinary object), respond with exactly {{"skip": true}} and nothing else. A social ATTEMPT with a contested outcome (persuading, proposing, deceiving, intimidating, seducing, bargaining) IS substantive - assess it.

{world_section}{story_section}
Character:
  Level {char.level}, HP {char.hp}/{char.max_hp}{unconscious}
  Stats: {stats_line}
  {chr(10).join(skill_lines) or 'No skills'}

Player action: "{input_text}"

Feasibility scale (rate the attempt, not the ambition):
  1-2: violates the world's rules or established story facts, is physically/logically impossible, or is hopeless at the current difficulty.
  3-4: far beyond current ability or an enormous ask, but not impossible.
  5-6: challenging; meaningful chance of failure.
  7-8: within the character's demonstrated abilities.
  9-10: near-certain success.

Outcome mapping at this difficulty: {outcome_mapping}.

Judging guidelines:
{_directive("action_assessment", instructions)}
- Difficulty is set to "{difficulty_label}": {difficulty_guidance}

You are a referee, not a narrator: determine the outcome, do not describe it. Never write story prose.

JSON response:
{{"feasibility": int 1-10, "skill_used": "name or empty string", "difficulty": "trivial|easy|moderate|hard|extreme|impossible", "curse_triggered": "name or empty string", "passive_effects": "brief factual note on which passives apply and how, or empty string", "failure_reason": "empty string unless feasibility is {fail_span}; then one short factual clause naming the world rule, established fact, or decisive capability gap the attempt founders on"}}"""

    try:
        result = await sdk.llm.generate(prompt, model_preference=model_pref)
        assessment = _parse_json_repair(result)
        if assessment is None:
            print(f"[RPG] Action assessment failed: unable to parse JSON response: {(result or '')[:200]}")
            return {}
        if not isinstance(assessment, dict):
            return {}
        if assessment.get("skip"):
            return {}
        return {
            "feasibility": max(1, min(10, int(assessment.get("feasibility", 5)))),
            "skill_used": str(assessment.get("skill_used", "")),
            "difficulty": str(assessment.get("difficulty", "moderate")),
            "curse_triggered": str(assessment.get("curse_triggered", "")),
            "passive_effects": str(assessment.get("passive_effects", "")),
            "failure_reason": str(assessment.get("failure_reason", "")),
        }
    except Exception as e:
        print(f"[RPG] Action assessment failed: {type(e).__name__}: {e}")
        return {}


async def on_render_prompt_block(block: dict, state: dict, sdk) -> dict:
    block_id = block.get("id", "")
    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))
    config = state.get("module_configs", {}).get("wb_core_rpg", {})

    if block_id == "character_sheet":
        return {"content": _render_character_sheet(char, config)}
    elif block_id == "action_feasibility":
        input_text = state.get("input_text", "").strip()
        if not input_text:
            return {}
        return {"content": _build_action_feasibility_prompt(char, input_text, config)}
    return {}


async def on_mutation_schema(state: dict, sdk) -> dict:
    """Make the Reader aware of the character's current level, skills, and
    status effects: the level so new-skill ratings can be judged against where
    the character stands, the lists so it never reports a skill as an effect
    (or vice versa) or duplicates a name."""
    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))
    out = {
        "skill_changes": (
            f"{_BASE_MUTATION_SCHEMA.get('skill_changes', '')} "
            f"The player is currently Level {char.level}."
        ),
    }
    if not char.skills and not char.status_effects:
        return out
    skill_list = ", ".join(f"{n} ({d.get('type', 'active')})" for n, d in char.skills.items()) or "(none)"
    effect_list = ", ".join(
        f"{e['name']} ({e['kind']}, severity {e.get('severity', 3)})" for e in char.status_effects) or "(none)"
    out["status_effects_gained"] = (
        f"{_BASE_MUTATION_SCHEMA.get('status_effects_gained', '')} "
        f"The player's existing skills and curses: {skill_list}. Active status effects: {effect_list}. "
        "Never report an existing skill or curse as a new status effect; re-report an active effect only if the story changed it."
    )
    out["skill_changes"] += (
        f" Active status effects (temporary conditions, NOT skills): {effect_list}. "
        "Never report a temporary condition as a skill, and never duplicate an active status effect's name as a skill."
    )
    return out


async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict:
    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))
    config = state.get("module_configs", {}).get("wb_core_rpg", {})
    # An empty mutation is still processed: condition-based XP is awarded from the
    # turn's action assessment even when the Reader reports no other state change.
    if mutation is None:
        mutation = {}

    hp_per_con = config.get("hp_per_vitality", config.get("hp_per_constitution", 7))
    const_changed = False
    updated = False

    # 1. Apply HP change
    hp_change = mutation.get("hp_change", 0)
    if isinstance(hp_change, int) and hp_change != 0:
        char.hp = max(0, min(char.max_hp, char.hp + hp_change))
        updated = True

    # 2. Apply stat changes from Reader
    stat_changes = mutation.get("stat_changes", {})
    if isinstance(stat_changes, dict):
        max_stat = config.get("max_stat_value", 20)
        old_hp_ratio = char.hp / max(1, char.max_hp) if char.max_hp > 0 else 1.0
        for stat, delta in stat_changes.items():
            actual_stat = OLD_STAT_MAP.get(stat, stat)
            if actual_stat in STAT_NAMES and isinstance(delta, int):
                char.stats[actual_stat] = max(1, min(max_stat, char.stats[actual_stat] + delta))
                if actual_stat == "vitality":
                    const_changed = True
                updated = True
        # Recalc max HP if vitality changed, preserve HP ratio
        if const_changed:
            char.recalc_hp(hp_per_con)
            char.hp = min(char.max_hp, max(0, int(char.max_hp * old_hp_ratio)))

    # 3. Apply skill changes from Reader (now with trigger_words support)
    skill_changes = mutation.get("skill_changes", {})
    if isinstance(skill_changes, dict):
        for skill_name, change in skill_changes.items():
            name = skill_name.strip().lower()
            if not name:
                continue
            if isinstance(change, dict):
                rating = change.get("rating", 0)
                description = change.get("description", "")
                trigger_words = change.get("trigger_words", [])
                skill_type = change.get("type", "active")
            elif isinstance(change, int):
                rating = change
                description = ""
                trigger_words = []
                skill_type = "active"
            else:
                continue
            if name in char.skills:
                char.skills[name]["rating"] = max(1, min(10, char.skills[name]["rating"] + rating))
                if description:
                    char.skills[name]["description"] = description
                if trigger_words:
                    char.skills[name].setdefault("trigger_words", []).extend(
                        w for w in trigger_words if w not in char.skills[name].get("trigger_words", [])
                    )
                if isinstance(change, dict) and change.get("type"):
                    char.skills[name]["type"] = skill_type
            else:
                char.skills[name] = {
                    "rating": max(1, min(10, rating or 3)),
                    "description": description or f"Proficiency in {skill_name}",
                    "trigger_words": trigger_words,
                    "type": skill_type,
                }
                # The condition graduated into a lasting ability/curse: the
                # skill takes over from the same-named status effect.
                if any(e["name"] == name for e in char.status_effects):
                    char.status_effects = [e for e in char.status_effects if e["name"] != name]
                    print(f"[RPG] Status effect '{name}' superseded by new skill of the same name")
            updated = True

    # 3b. Status effects: apply conditions gained/removed in the story, then
    # tick durations. Effects gained this turn are not ticked, so a 1-turn
    # effect influences exactly one player action before wearing off.
    now_minutes = _clock_minutes(state)
    gained_names = set()
    gained = mutation.get("status_effects_gained", [])
    if isinstance(gained, list):
        for raw in gained:
            effect = _sanitize_status_effect(raw)
            if effect is None:
                continue
            minutes = raw.get("duration_minutes")
            if (effect["expires_at_minutes"] is None and now_minutes is not None
                    and isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and int(minutes) > 0):
                effect["expires_at_minutes"] = now_minutes + int(minutes)
            # A same-named skill or curse already covers this condition.
            if effect["name"] in char.skills:
                print(f"[RPG] Status effect '{effect['name']}' skipped: a skill/curse of that name exists")
                continue
            existing = next((e for e in char.status_effects if e["name"] == effect["name"]), None)
            if existing is not None:
                # Re-applying an effect refreshes it (new duration/severity/
                # description) but keeps its lingering age.
                effect["turns_active"] = existing.get("turns_active", 0)
                char.status_effects.remove(existing)
                char.status_effects.append(effect)
            elif len(char.status_effects) < MAX_STATUS_EFFECTS:
                char.status_effects.append(effect)
            else:
                # At the cap: a stronger effect overrides the weakest one.
                weakest = min(char.status_effects, key=lambda e: e.get("severity", 3))
                if effect["severity"] > weakest.get("severity", 3):
                    char.status_effects.remove(weakest)
                    char.status_effects.append(effect)
                    print(f"[RPG] Status effect '{effect['name']}' (severity {effect['severity']}) overrides weaker '{weakest['name']}' (severity {weakest.get('severity', 3)})")
                else:
                    print(f"[RPG] Status effect '{effect['name']}' (severity {effect['severity']}) rejected: {MAX_STATUS_EFFECTS} effects active and none weaker")
                    continue
            gained_names.add(effect["name"])
            updated = True
            print(f"[RPG] Status effect gained: '{effect['name']}' ({effect['kind']}, severity {effect['severity']}, {_effect_duration_label(effect, now_minutes)})")

    removed_effects = mutation.get("status_effects_removed", [])
    if isinstance(removed_effects, list) and removed_effects:
        removed_keys = {str(n).strip().lower() for n in removed_effects}
        kept = [e for e in char.status_effects if e["name"] not in removed_keys]
        if len(kept) != len(char.status_effects):
            for e in char.status_effects:
                if e["name"] in removed_keys:
                    print(f"[RPG] Status effect removed by story: '{e['name']}'")
            char.status_effects = kept
            updated = True

    ticked = []
    for effect in char.status_effects:
        if effect["name"] not in gained_names:
            effect["turns_active"] = effect.get("turns_active", 0) + 1
            updated = True
            if effect.get("duration_turns") is not None:
                effect["duration_turns"] -= 1
                if effect["duration_turns"] <= 0:
                    print(f"[RPG] Status effect wore off: '{effect['name']}'")
                    continue
            expires_at = effect.get("expires_at_minutes")
            if expires_at is not None and now_minutes is not None and now_minutes >= expires_at:
                print(f"[RPG] Status effect expired: '{effect['name']}'")
                continue
            # A strong, bad condition with no expiry that has lingered long
            # enough hardens into a lasting curse skill.
            if (effect["kind"] == "bad"
                    and effect.get("duration_turns") is None
                    and effect.get("expires_at_minutes") is None
                    and effect.get("severity", 3) >= CURSE_SEVERITY_MIN
                    and effect["turns_active"] >= CURSE_AGE_TURNS
                    and effect["name"] not in char.skills):
                rating = max(1, min(10, int(effect.get("severity", 3))))
                char.skills[effect["name"]] = {
                    "rating": rating,
                    "description": effect["description"],
                    "trigger_words": [],
                    "type": "curse",
                }
                print(f"[RPG] Status effect '{effect['name']}' hardened into a curse ({rating}/10) after {effect['turns_active']} turns")
                continue
        ticked.append(effect)
    char.status_effects = ticked

    # 4. Progress system
    progression = config.get("progression_system", "xp")

    if progression == "xp":
        condition = config.get("xp_gain_condition", "successful_action")
        if condition == "reader":
            # Defer entirely to the Reader agent's per-turn judgement.
            raw = mutation.get("xp_gained", 0)
            xp_gained = int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 else 0
        else:
            # Award XP deterministically from the turn's action assessment.
            xp_gained = _xp_from_assessment(char, config)
        if xp_gained > 0:
            char.xp += xp_gained
            steepness = config.get("xp_curve_steepness", 2)
            while total_xp_for_level(char.level + 1, steepness) <= char.xp:
                _apply_level_up(char, char.level + 1, config)
            updated = True

    elif progression == "practice":
        improve_rate = config.get("skill_improvement_rate", 5)
        for skill_name, counter in list(char.practice_counters.items()):
            if skill_name in char.skills:
                threshold = improve_rate * char.skills[skill_name]["rating"]
                while counter >= threshold:
                    char.skills[skill_name]["rating"] = min(10, char.skills[skill_name]["rating"] + 1)
                    counter -= threshold
                    updated = True
            char.practice_counters[skill_name] = counter

    elif progression == "milestone":
        if abs(hp_change) > 20 or (isinstance(stat_changes, dict) and len(stat_changes) >= 3):
            _apply_level_up(char, char.level + 1, config)
            updated = True

    if updated:
        if char.max_hp == 0:
            char.recalc_hp(hp_per_con)
        _check_evolution_ready(char)
        return {"module_data": {"wb_core_rpg": char.to_dict()}}
    return {}


def _parse_json_repair(raw: str):
    """Parse a JSON object from an LLM reply: strip Markdown code fences and
    repair a truncated object by appending missing closing braces."""
    text = (raw or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if text.startswith("{"):
            brace_count = text.count("{") - text.count("}")
            if brace_count > 0:
                try:
                    return json.loads(text.rstrip() + "}" * brace_count)
                except json.JSONDecodeError:
                    return None
    return None


def _world_context_section(world_data: dict | None) -> str:
    world_lines = []
    if world_data:
        rules = world_data.get("rules", {})
        lore = world_data.get("lore", {})
        if rules:
            world_lines.append(f"Genre: {rules.get('genre', 'Fantasy')}")
            world_lines.append(f"Magic Level: {rules.get('magic_level', 'Medium')}")
            world_lines.append(f"Technology Era: {rules.get('tech_era', 'Medieval')}")
            world_lines.append(f"Tone: {rules.get('tone', 'Neutral')}")
        if lore.get("premise"):
            world_lines.append(f"Premise: {lore['premise']}")
    if world_lines:
        return "World context:\n" + "\n".join(world_lines) + "\n"
    return ""


def _parse_json_block(raw: str):
    """Strip Markdown code fences and parse a JSON object/array from an LLM reply."""
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


async def on_librarian(state: dict, sdk) -> dict | None:
    """After the storyteller, detect skills granted, removed, or altered by
    EXTERNAL forces acting on the player (divine boons, curses, blessings,
    magical injuries, a mentor's gift). This is distinct from the player's own
    practice/XP progression and from the Reader's action-driven skill_changes."""
    config = state.get("module_configs", {}).get("wb_core_rpg", {})
    if not config.get("external_skill_events_enabled", True):
        return None

    if state.get("turn", 0) == 0:
        return None

    history = state.get("history", [])
    if not history:
        return None

    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))

    # The grant/curse being detected happened in THIS turn's scene, so that
    # scene must always be in the prompt (a tail-truncated join of the last 3
    # scenes used to cut off the start of a long newest scene, missing skills
    # granted early in it). Earlier scenes are only context.
    latest = str(history[-1])[-4000:]
    earlier = "\n".join(str(h) for h in history[-3:-1])[-2000:]
    earlier_block = f"EARLIER NARRATION (context only):\n{earlier}\n\n" if earlier else ""

    if char.skills:
        skill_lines = ", ".join(f"{_skill_prompt_label(n, d)} ({d['rating']}/10, {d.get('type', 'active')})" for n, d in char.skills.items())
    else:
        skill_lines = "(none)"

    if char.status_effects:
        effect_lines = "; ".join(
            f"{e['name']} ({e['kind']}, severity {e.get('severity', 3)}): {e['description']}" for e in char.status_effects)
    else:
        effect_lines = "(none)"

    model_pref = config.get("practice_ai_model", "fastest")
    prompt = f"""You are the game system that records supernatural or external changes to a player character's abilities in a text RPG.

Read the recent narration and detect ONLY skill changes that an EXTERNAL force imposed on the player: a god or spirit granting a power, a curse or hex stripping/weakening an ability, a blessing or artifact bestowing a skill, a magical injury disabling a skill, or a mentor/entity directly gifting knowledge.

Do NOT report skills the player improved through their own effort, practice, training, or repeated use — those are handled elsewhere. If nothing external happened, return empty arrays.

The player is currently Level {char.level}.

The player's current skills: {skill_lines}

The player's current status effects (temporary conditions tracked separately from skills): {effect_lines}

Do NOT report a temporary condition (an injury, poison, illusion, or short-lived buff or hex) as a skill change - conditions are handled elsewhere. Do not add a skill that duplicates a status effect listed above, and do not remove a skill merely because a temporary condition suppresses it.

{earlier_block}THIS TURN'S SCENE (check this for skill changes):
{latest}

For each added or altered skill, the description must capture the nuance of the power in 1-2 tight sentences: what it does, how it manifests, where it came from, and any limit, cost, or condition the narration implies. Concrete over flowery. Example: "Emberkiss, a boon from the hearth-goddess: the bearer's touch can kindle or snuff small flames, but only fire she can see." not "A powerful fire ability."

Respond with ONLY valid JSON:
{{"added": [{{"name": "skill_name", "rating": 1-10, "description": "what it does, how it manifests, source, limits — 1-2 tight sentences", "trigger_words": ["word1", "word2"], "type": "active|passive|curse"}}], "removed": ["skill_name"], "altered": [{{"name": "existing_skill_name", "new_rating": 1-10, "description": "optional updated description"}}]}}"""

    try:
        sdk.llm._current_module = "wb_core_rpg"
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return None

    updated = False

    for entry in parsed.get("added", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip().lower()
        if not name or name in char.skills:
            continue
        skill_type = entry.get("type", "active")
        if skill_type not in ("active", "passive", "curse"):
            skill_type = "active"
        char.skills[name] = {
            "rating": max(1, min(10, int(entry.get("rating", 3) or 3))),
            "description": str(entry.get("description", "")) or f"Granted power: {name}",
            "trigger_words": [str(w) for w in entry.get("trigger_words", []) if w],
            "type": skill_type,
        }
        # The skill takes over from a same-named temporary condition.
        if any(e["name"] == name for e in char.status_effects):
            char.status_effects = [e for e in char.status_effects if e["name"] != name]
            print(f"[RPG] Status effect '{name}' superseded by granted skill of the same name")
        updated = True
        print(f"[RPG] External event granted skill '{name}' ({char.skills[name]['rating']}/10, {skill_type})")

    for name in parsed.get("removed", []) or []:
        key = _match_skill_key(name, char.skills)
        if key is not None:
            del char.skills[key]
            char.practice_counters.pop(key, None)
            updated = True
            print(f"[RPG] External event removed skill '{key}'")

    for entry in parsed.get("altered", []) or []:
        if not isinstance(entry, dict):
            continue
        key = _match_skill_key(entry.get("name", ""), char.skills)
        if key is None:
            continue
        if entry.get("new_rating") is not None:
            try:
                char.skills[key]["rating"] = max(1, min(10, int(entry["new_rating"])))
                updated = True
            except (TypeError, ValueError):
                pass
        desc = entry.get("description")
        if desc:
            char.skills[key]["description"] = str(desc)
            updated = True
        if updated:
            print(f"[RPG] External event altered skill '{key}' -> {char.skills[key]['rating']}/10")

    if updated:
        _check_evolution_ready(char)
        # module_data is deep-merged into state (additive), which can't delete
        # a dict entry — a removed skill would silently reappear. Skills and
        # practice counters are returned complete, so replace them wholesale.
        return {
            "module_data": {"wb_core_rpg": char.to_dict()},
            "module_data_replace": ["skills", "practice_counters"],
        }
    return None


async def on_command_stats(args: list[str], state: dict, sdk):
    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))
    lines = [f"[Stats] Level {char.level} ({char.xp} XP)"]
    if char.is_unconscious():
        lines.append(f"\u26a0\ufe0f UNCONSCIOUS \u26a0\ufe0f")
    lines.append(f"HP: {char.hp}/{char.max_hp}")
    lines.append(" | ".join(f"{s[:3].upper()}:{v}" for s, v in char.stats.items()))
    if char.skills:
        lines.append("Skills: " + ", ".join(f"{n}({d['rating']})" for n, d in char.skills.items()))
    if char.status_effects:
        now_minutes = _clock_minutes(state)
        lines.append("Effects: " + ", ".join(
            f"{e['name']} [{e['kind']}, {_effect_duration_label(e, now_minutes)}]" for e in char.status_effects))
    return {"message": "\n".join(lines), "signal": "end_turn"}


async def on_command_skills(args, state, sdk):
    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))
    if not char.skills:
        return {"message": "[Skills] No skills acquired yet.", "signal": "end_turn"}
    lines = ["[Skills]"]
    for name, data in sorted(char.skills.items(), key=lambda x: -x[1]["rating"]):
        bar = "\u2588" * data["rating"] + "\u2591" * (10 - data["rating"])
        tier = _skill_tier(data)
        tier_tag = f" [T{tier}]" if tier > 1 else ""
        theme = f" ({data['evolution_theme']})" if data.get("evolution_theme") else ""
        lines.append(f"  {name.title()}{tier_tag}{theme}: {bar} ({data['rating']}/10)")
        if data.get("description"):
            lines.append(f"    {data['description']}")
        if data.get("trigger_words"):
            lines.append(f"    Triggers: {', '.join(data['trigger_words'][:5])}")
    return {"message": "\n".join(lines), "signal": "end_turn"}


async def on_command_level(args, state, sdk):
    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))
    config = state.get("module_configs", {}).get("wb_core_rpg", {})
    progression = config.get("progression_system", "xp")
    steepness = config.get("xp_curve_steepness", 2)
    needed = total_xp_for_level(char.level + 1, steepness)
    lines = [
        f"[Level] Current: {char.level}",
        f"XP: {char.xp}/{needed} (need {max(0, needed - char.xp)} more)" if progression == "xp" else f"Progression: {progression.title()}",
    ]
    return {"message": "\n".join(lines), "signal": "end_turn"}


def _evolution_announcement(char: Character) -> str:
    """One-time storyteller note about skills that evolved since the last
    generation (recorded by the evolve endpoint, dropped after one turn)."""
    if not char.recent_evolutions:
        return ""
    notes = []
    for e in char.recent_evolutions:
        desc = f" It now works thus: {e['description']}" if e.get("description") else ""
        path = f', down the "{e["theme"]}" path' if e.get("theme") else ""
        notes.append(
            f'The skill "{e["old_name"]}" has just evolved into "{e["new_name"]}" '
            f'(Tier {e["tier"]}{path}) - a dramatic surge in power.{desc}'
        )
    plural = len(notes) > 1
    return (
        "JUST HAPPENED - SKILL EVOLUTION (between scenes, not yet narrated): "
        + " ".join(notes)
        + (" Acknowledge these transformations" if plural else " Acknowledge this transformation")
        + " in the next narration - the character feels the new power settle in. "
        "Give it a brief but meaningful beat; refer to the skill by its new name from now on."
    )


def _render_character_sheet(char: Character, config: dict) -> str:
    tier_list = config.get("stat_tiers", DEFAULT_STAT_TIERS) or DEFAULT_STAT_TIERS

    evolution_note = _evolution_announcement(char)

    if char.is_unconscious():
        sheet = "The character is unconscious and incapacitated. Cannot act physically."
        return f"{sheet}\n{evolution_note}" if evolution_note else sheet

    ratio = char.hp / max(1, char.max_hp)
    if ratio > 0.8:
        health = "In good health."
    elif ratio > 0.5:
        health = "Wounded but standing."
    elif ratio > 0:
        health = "Gravely wounded, barely holding on."
    else:
        health = "At death's door."

    if char.level <= 3:
        xp_label = "novice"
    elif char.level <= 7:
        xp_label = "seasoned"
    elif char.level <= 10:
        xp_label = "veteran"
    else:
        xp_label = "legendary"

    lines = [f"A {xp_label} adventurer. {health}"]

    strong_parts = []
    weak_parts = []
    for stat_name in STAT_NAMES:
        value = char.stats[stat_name]
        tier = _tier_for(value, tier_list)
        display = STAT_DISPLAY.get(stat_name, stat_name)
        if "Severely" in tier:
            weak_parts.append(f"{display.lower()} severely limited")
        elif "Below" in tier:
            weak_parts.append(f"{display.lower()} below average")
        elif "Above" in tier:
            strong_parts.append(display.lower())
        elif "Expert" in tier or "Peak" in tier:
            strong_parts.append(f"{display.lower()} at expert level")
        elif "Superhuman" in tier:
            strong_parts.append(f"superhuman {display.lower()}")
        elif "Legendary" in tier:
            strong_parts.append(f"legendary {display.lower()}")

    stat_line = ""
    if strong_parts:
        stat_line = ", ".join(strong_parts)
        if weak_parts:
            stat_line += "; " + ", ".join(weak_parts)
    elif weak_parts:
        stat_line = ", ".join(weak_parts)
    if stat_line:
        lines.append(stat_line + ".")

    active_skills = {n: d for n, d in char.skills.items() if d.get("type") == "active"}
    passive_skills = {n: d for n, d in char.skills.items() if d.get("type") == "passive"}
    curse_skills = {n: d for n, d in char.skills.items() if d.get("type") == "curse"}

    if active_skills:
        abilities = []
        for n, d in active_skills.items():
            desc = d.get("description") or f"proficient in {n}"
            tier = _skill_tier(d)
            if tier > 1:
                desc = f"[Tier {tier} evolved ability, far beyond its ordinary form] {desc}"
            abilities.append(desc)
        lines.append("Abilities: " + "; ".join(abilities) + ".")

    passive_parts = []
    for n, d in passive_skills.items():
        desc = d.get("description") or n
        tier = _skill_tier(d)
        if tier > 1:
            desc = f"[Tier {tier} evolved] {desc}"
        passive_parts.append(desc)
    if passive_parts:
        lines.append("Passives: " + "; ".join(passive_parts) + ".")

    curse_parts = []
    for n, d in curse_skills.items():
        triggers = d.get("trigger_words", [])
        desc = d.get("description") or n
        if triggers:
            desc = f"{desc} (triggered by {', '.join(triggers)})"
        curse_parts.append(desc)
    if curse_parts:
        lines.append("Curses: " + "; ".join(curse_parts) + ".")

    afflictions = [e["description"].rstrip(".") for e in char.status_effects if e["kind"] == "bad"]
    boons = [e["description"].rstrip(".") for e in char.status_effects if e["kind"] == "good"]
    if afflictions:
        lines.append("Current afflictions: " + "; ".join(afflictions) + ".")
    if boons:
        lines.append("Current boons: " + "; ".join(boons) + ".")

    if char.backstory:
        lines.append(f"Backstory: {char.backstory}")

    if evolution_note:
        lines.append(evolution_note)

    return "\n".join(lines)


def _build_action_feasibility_prompt(char: Character, input_text: str, config: dict) -> str:
    assessment = char.action_assessment
    if not assessment:
        return ""

    try:
        feasibility = int(assessment.get("feasibility"))
    except (TypeError, ValueError):
        return ""

    _, _, no_and_max, fail_max, success_min = _strictness_tier(config)
    reason = str(assessment.get("failure_reason", "")).strip()
    because = f" because {reason.rstrip('.')}" if reason else ""
    if feasibility >= success_min:
        ruling = "the attempt succeeds."
    elif feasibility > fail_max:
        ruling = (
            "partial success or success at a cost - weave in a complication, "
            "price, or twist that moves the story forward. Never a flat refusal "
            "or dead end."
        )
    elif feasibility > no_and_max:
        ruling = (
            f"the attempt fails{because}. Show what the failed attempt visibly "
            "reveals or provokes - the world reacts; it is not a silent dead end."
        )
    else:
        ruling = (
            f"the attempt fails{because}, and the situation worsens: on top of "
            "the failure, add a concrete extra cost, threat, or complication. "
            "The world reacts; it is not a silent dead end."
        )

    difficulty = assessment.get("difficulty", "moderate")
    lines = [
        f"The player attempted to: \"{input_text}\"",
        f"Assessed difficulty: {difficulty}",
        f"Ruling: {ruling}",
    ]
    skill_used = str(assessment.get("skill_used", "")).strip()
    if skill_used:
        lines.append(f"Skill in play: {skill_used}")
    curse = str(assessment.get("curse_triggered", "")).strip()
    if curse:
        lines.append(f"Curse triggered this turn: {curse}")
    passives = str(assessment.get("passive_effects", "")).strip()
    if passives:
        lines.append(f"Passive effects in play: {passives}")
    lines.append(
        "Guidance: The ruling decides only WHETHER the action succeeds - how it "
        "plays out is yours to narrate. Honor the ruling, but adapt the specifics "
        "to the living scene - if established characters or events make a "
        "different outcome more natural, follow the story. Unless the difficulty "
        "is \"impossible\", do not resolve the action as a flat refusal or dead "
        "end: fail forward with a cost, complication, partial result, or new "
        "opportunity."
    )
    return "\n".join(lines)


def _apply_level_up(char: Character, new_level: int, config: dict | None = None):
    """Level up: bank player-spendable attribute/skill points (allocated later
    via the level-up popup -> POST /levelup/spend), refresh HP, full heal."""
    config = config or {}
    char.level = new_level
    attr_points = config.get("attribute_points_per_level", 2)
    skill_points = config.get("skill_points_per_level", 1)
    char.unspent_attribute_points += attr_points if isinstance(attr_points, int) and attr_points >= 0 else 2
    char.unspent_skill_points += skill_points if isinstance(skill_points, int) and skill_points >= 0 else 1
    char.level_up_history.append({"level": new_level})
    char.level_up_history = char.level_up_history[-10:]
    # Reset usage counters after level-up
    char.stat_usage = {s: 0 for s in STAT_NAMES}
    char.recalc_hp(config.get("hp_per_vitality", config.get("hp_per_constitution", 7)))
    char.hp = char.max_hp


# Curses don't evolve: evolution is a reward the player steers, which would
# invert a curse's role as an unwanted affliction. Librarian events already
# escalate curses narratively.
EVOLVABLE_TYPES = ("active", "passive")
MAX_SKILL_RATING = 10
EVOLVED_RESET_RATING = 5

# In-flight generation tasks shared across requests (evolution options,
# wizard categories/options/refine, evolve), keyed per save + operation.
# Requests await them through asyncio.shield, so a client disconnect (the app
# closed mid-generation) cancels only the waiting request - the generation
# runs to completion and its result lands in the cache for when the player
# comes back. Duplicate requests (StrictMode double-fires, a reopened app
# re-asking) join the same task instead of paying for a second LLM call.
_inflight_tasks: dict[str, "asyncio.Task"] = {}


async def _join_generation(key: str, factory, replace: bool = False):
    task = None if replace else _inflight_tasks.get(key)
    if task is None:
        task = asyncio.create_task(factory())
        _inflight_tasks[key] = task

        def _pop(_t, key=key, task=task):
            if _inflight_tasks.get(key) is task:
                _inflight_tasks.pop(key, None)

        task.add_done_callback(_pop)
    return await asyncio.shield(task)

# Rarity is rolled once, when the player picks a skill, uniformly over 5-10.
# It shapes how well-crafted the generated skill IS (stronger benefits, weaker
# drawbacks) - not its rating progression: every new skill starts at rating 5,
# except a Mythic (10), which is born at max rating and drops straight into
# the normal pending-evolution flow.
RARITY_LABELS = {5: "Common", 6: "Uncommon", 7: "Rare", 8: "Epic", 9: "Legendary", 10: "Mythic"}


def _roll_strength() -> int:
    return random.randint(5, 10)


# Transient new-skill wizard state, keyed by save_id. Never persisted: the
# wizard is ad-hoc browsing with no pending_evolutions-style entry to hang it
# on, and stale menus must not outlive a learned skill (cleared on skill add).
# Shape: {"categories": [...]|None, "pages": {menu_key: [[5 skills], ...]},
# "rolls": {skill_name_lower: refined skill}} where menu_key is "cat:{name}"
# or "search:{query}" (lowercased). "rolls" locks fate's strength roll per
# skill so re-picking can't re-roll it.
_addskill_cache: dict[str, dict] = {}


def _skill_tier(data: dict) -> int:
    try:
        return max(1, int(data.get("tier", 1)))
    except (TypeError, ValueError):
        return 1


def _skill_prompt_label(name: str, data: dict) -> str:
    tier = _skill_tier(data)
    return f"{name} [Tier {tier}]" if tier > 1 else name


def _match_skill_key(name, skills: dict) -> str | None:
    """Resolve a skill name echoed back by an LLM to its dict key. Prompts show
    evolved skills as "name [Tier N]" (_skill_prompt_label), and models echo
    that label, so a trailing tier tag must not break the lookup."""
    key = str(name).strip().lower()
    if key in skills:
        return key
    key = re.sub(r"\s*\[tier \d+\]$", "", key)
    return key if key in skills else None


def _sync_pending_evolutions(skills: dict, pending: list) -> list:
    """Keep pending_evolutions in sync with the skill list: enqueue any
    evolvable skill that reached max rating, prune entries whose skill no
    longer exists or dropped below max (deferred entries survive as long as
    the skill still qualifies)."""
    if not isinstance(pending, list):
        pending = []
    pending = [
        e for e in pending
        if isinstance(e, dict)
        and e.get("skill") in skills
        and skills[e["skill"]].get("rating") == MAX_SKILL_RATING
        and skills[e["skill"]].get("type", "active") in EVOLVABLE_TYPES
    ]
    queued = {e["skill"] for e in pending}
    for name, data in skills.items():
        if (
            data.get("rating") == MAX_SKILL_RATING
            and data.get("type", "active") in EVOLVABLE_TYPES
            and name not in queued
        ):
            pending.append({"skill": name, "options": None, "status": "pending"})
    return pending


def _check_evolution_ready(char: Character) -> None:
    char.pending_evolutions = _sync_pending_evolutions(char.skills, char.pending_evolutions)


PHYSICAL_ACTION_KEYWORDS = [
    "swing", "slash", "stab", "punch", "kick", "strike", "attack", "hit",
    "run", "sprint", "jump", "leap", "climb", "swim", "dash", "charge",
    "grab", "push", "pull", "lift", "throw", "catch", "wield", "draw",
    "dodge", "block", "parry", "deflect", "roll", "duck", "sidestep",
    "stand", "rise", "walk", "step", "move", "stagger", "advance",
    "dance", "flip", "vault", "wrestle", "tackle",
]

UNCONSCIOUS_PHYSICAL_VETO = (
    "The character is unconscious (HP=0). They CANNOT perform physical actions "
    "like attacking, running, grabbing, jumping, or moving. Do not narrate them "
    "taking any voluntary physical action. Instead, narrate mental experiences "
    "(dreams, visions, memories, thoughts), external events happening around "
    "their body, or other characters interacting with their incapacitated body."
)

DEATH_PHYSICAL_VETO = (
    "The character is dead. They CANNOT perform ANY actions whatsoever. "
    "Do not narrate them doing anything. Instead, describe the aftermath, "
    "other characters' reactions, the environment, or offer a game-over "
    "narrative conclusion."
)


# Temporarily disabled: the unconscious/dead physical-action veto triggers a
# storyteller rewrite loop. Set back to True to re-enable validation.
VALIDATE_OUTPUT_ENABLED = False


async def on_validate_output(llm_output: str, state: dict, sdk) -> None:
    """Validate that the Storyteller doesn't have unconscious/dead characters acting."""
    if not VALIDATE_OUTPUT_ENABLED:
        return

    char_data = state.get("module_data", {}).get("wb_core_rpg", {})
    if not char_data:
        return

    hp = char_data.get("hp", 85)
    max_hp = char_data.get("max_hp", 85)

    if hp > 0:
        return

    output_lower = llm_output.lower()
    physical_action_found = any(kw in output_lower for kw in PHYSICAL_ACTION_KEYWORDS)
    if not physical_action_found:
        return

    if max_hp == 0:
        raise sdk.ValidationVeto(DEATH_PHYSICAL_VETO)
    else:
        raise sdk.ValidationVeto(UNCONSCIOUS_PHYSICAL_VETO)


# --------------------------------------------------------------------------
# Skill evolution prompts (LLM calls made from the module router)
# --------------------------------------------------------------------------


def _skill_record_block(name: str, data: dict) -> str:
    tier = _skill_tier(data)
    lines = [
        f"Name: {name}",
        f"Tier: {tier}",
        f"Rating: {data.get('rating', 1)}/10",
        f"Type: {data.get('type', 'active')}",
        f"Description: {data.get('description') or '(none)'}",
        f"Trigger words: {', '.join(data.get('trigger_words', [])) or '(none)'}",
    ]
    lineage = data.get("lineage") or []
    if lineage:
        lines.append(f"Evolution lineage (oldest first): {' -> '.join(lineage)} -> {name}")
    if data.get("evolution_theme"):
        lines.append(f"Current evolution theme: {data['evolution_theme']}")
    return "\n".join(lines)


def _story_context_section(state: dict) -> str:
    """Story style (themes/tags) and recent scene excerpts for the evolution
    prompts. Offered as plain context - deliberately no directive on how to
    use it; the model decides what, if anything, to draw from it."""
    lines = []
    style = state.get("story_style") or {}
    for key, label in (("themes", "Story themes"), ("tags", "Story tags")):
        value = style.get(key)
        value = value.strip() if isinstance(value, str) else ""
        if value:
            lines.append(f"{label}: {value}")
    history = [h for h in (state.get("history") or []) if isinstance(h, str) and h.strip()]
    if history:
        lines.append("Recent story (most recent last):\n" + "\n---\n".join(history[-3:]))
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def _character_context_block(rpg: dict) -> str:
    stats = rpg.get("stats", {})
    stats_line = ", ".join(f"{s}={stats.get(s, 10)}" for s in STAT_NAMES)
    lines = [
        f"Level {rpg.get('level', 1)} adventurer",
        f"Stats: {stats_line}",
    ]
    backstory = rpg.get("backstory", "")
    if backstory:
        lines.append(f"Backstory: {backstory}")
    other = [
        f"{n} ({d.get('rating', 1)}/10, {d.get('type', 'active')})"
        for n, d in rpg.get("skills", {}).items()
    ]
    if other:
        lines.append("All skills: " + ", ".join(other))
    return "\n".join(lines)


def _evolution_options_prompt(rpg: dict, key: str, data: dict, state: dict, instructions: dict | None = None) -> str:
    tier = _skill_tier(data)
    world_section = _world_context_section(state.get("world_data"))
    story_section = _story_context_section(state)
    return f"""You are the game system for a text RPG. A Level {rpg.get('level', 1)} character's skill has reached maximum rating and is ready to EVOLVE into a more powerful Tier {tier + 1} form. Output ONLY valid JSON, no other text.

{world_section}{story_section}Character:
{_character_context_block(rpg)}

Skill ready to evolve:
{_skill_record_block(key, data)}

Propose exactly 4 evolution paths. {_directive("evolution_options", instructions)}

JSON response (pure path first):
{{"options": [{{"theme": "1-3 words", "summary": "one short clause"}}, {{"theme": "...", "summary": "..."}}, {{"theme": "...", "summary": "..."}}, {{"theme": "...", "summary": "..."}}]}}"""


def _evolve_prompt(rpg: dict, key: str, data: dict, theme: str, state: dict, pure: bool = False, instructions: dict | None = None) -> str:
    tier = _skill_tier(data)
    world_section = _world_context_section(state.get("world_data"))
    story_section = _story_context_section(state)
    if pure:
        theme_req = (
            f'- This is the PURE path: keep the skill\'s exact identity and manner of working - do NOT take it '
            f'in a new direction. It becomes a dramatically more potent version of itself, in the spirit of '
            f'"{theme}": what it already does, done at a far greater scale.'
        )
    else:
        theme_req = f'- It must embody the "{theme}" theme and stay true to the world\'s tone and the skill\'s history.'
    return f"""You are the game system for a text RPG. A Level {rpg.get('level', 1)} character's maxed-out skill is evolving from Tier {tier} to Tier {tier + 1} down the "{theme}" path. Output ONLY valid JSON, no other text.

{world_section}{story_section}Character:
{_character_context_block(rpg)}

Skill that is evolving:
{_skill_record_block(key, data)}

Chosen evolution theme: {theme}

Design the evolved Tier {tier + 1} form. Requirements:
{_directive("evolve", instructions)}
{theme_req}
- Give it a new evocative name of 2-4 words. It must not be the same as the old name.
- The description is 1-2 tight sentences and completely FREE-STANDING: it states exactly what the power does, how it manifests, and any remaining limit or cost, readable by someone who has never heard of the previous form. Never mention, compare to, or assume knowledge of the old skill - no "now", "no longer", "unlike before", "twice as far", or naming the prior form. Concrete over flowery.
- Trigger words: 2-5 short words or phrases a player would naturally use when invoking it.

JSON response:
{{"name": "2-4 word evolved name", "description": "1-2 tight sentences", "trigger_words": ["word1", "word2"]}}"""


# --------------------------------------------------------------------------
# New-skill wizard prompts (LLM calls made from the module router)
# --------------------------------------------------------------------------


def _plot_challenge_section(state: dict) -> str:
    """The plot director's active thread, so the wizard can seed abilities
    that could matter against the current challenge. The challenge line is a
    spoiler the player may not have revealed yet, so prompts that use this
    section must forbid leaking it into player-visible text. Optional: the
    plot_aware_skill_generation toggle turns the whole thing off, leaving
    every wizard prompt plot-blind."""
    config = state.get("module_configs", {}).get("wb_core_rpg", {}) or {}
    if not config.get("plot_aware_skill_generation", True):
        return ""
    thread = state.get("module_data", {}).get("wb_plot_director", {}).get("thread") or {}
    if thread.get("status") != "active" or not str(thread.get("challenge") or "").strip():
        return ""
    lines = ["Current plot thread (the Challenge line is a spoiler hidden from the player):"]
    for key, label in (("title", "Title"), ("hook", "Hook"), ("challenge", "Challenge"), ("stakes", "Stakes")):
        value = str(thread.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) + "\n\n"


def _skill_categories_prompt(rpg: dict, state: dict, instructions: dict | None = None) -> str:
    world_section = _world_context_section(state.get("world_data"))
    story_section = _story_context_section(state)
    plot_section = _plot_challenge_section(state)
    plot_req = ""
    if plot_section:
        plot_req = (
            " Among the 5 story-drawn categories, make 2-3 of them domains whose skills could "
            "genuinely help this character overcome the current plot thread's challenge - but the "
            "connection must show only in what the domain is good for: never name the thread or "
            "echo the hidden challenge in a category name or summary."
        )
    return f"""You are the game system for a text RPG. The player wants to learn a brand-new skill and is browsing what kinds of abilities they could pursue. Output ONLY valid JSON, no other text.

{world_section}{story_section}{plot_section}Character:
{_character_context_block(rpg)}

Propose exactly 10 skill categories - {_directive("skill_categories", instructions)}{plot_req} Do not mark or group which is which. Each has a name of 1-3 words and one short clause summary.

JSON response:
{{"categories": [{{"name": "1-3 words", "summary": "one short clause"}}, ... 10 total]}}"""


def _skill_options_prompt(rpg: dict, menu: str, exclude: list[str], state: dict, search: bool = False, direct: bool = False, instructions: dict | None = None) -> str:
    world_section = _world_context_section(state.get("world_data"))
    story_section = _story_context_section(state)
    plot_section = _plot_challenge_section(state)
    plot_req = ""
    if plot_section:
        plot_req = (
            " Where the theme plausibly allows it, let 1-2 of the 5 be skills that could genuinely "
            "help this character against the current plot thread's challenge - each must still fit "
            "the theme and stand on its own as an ability, and no name or description may mention "
            "the thread or echo the hidden challenge."
        )
    if direct:
        # Category-free browsing (the scenario opted out of the categories
        # step): each page is a fresh spread across unrelated domains.
        intro = "The player is browsing new skills to learn, drawn from any domain of ability."
        task = (
            "Propose exactly 5 NEW skills spanning clearly different domains of ability "
            "(no two from the same domain)"
        )
    elif search:
        intro = (
            f'The player searched for "{menu}" and is browsing skills themed on that search. '
            "If the request is a basic ability you may match it exactly; for elaborate or very "
            "specific requests, offer close interpretations grounded in this world rather than "
            "an exact match."
        )
        task = f'Propose exactly 5 NEW skills themed on the search "{menu}"'
    else:
        intro = f'The player is browsing new skills to learn in the "{menu}" category.'
        task = f'Propose exactly 5 NEW skills in the "{menu}" category'
    exclude_line = ", ".join(exclude) if exclude else "(none)"
    return f"""You are the game system for a text RPG. {intro} Output ONLY valid JSON, no other text.

{world_section}{story_section}{plot_section}Character:
{_character_context_block(rpg)}

Do NOT propose any of these skills or trivial variants of them (already known or already offered): {exclude_line}

{task}, each learnable NOW by this Level {rpg.get('level', 1)} character. {_directive("skill_options", instructions)}{plot_req} Every proposal is a starting-level ability at its base form - the power each one ultimately awakens with is decided later by fate, so write none of the 5 as stronger or weaker than the others.

Each skill:
- name: 1-4 evocative words, distinct from every excluded name above and from the other 4 proposals
- type: "active" (deliberately used) or "passive" (always on)
- description: ONE tight sentence, concrete and specific to this story: what it does, how it manifests, and its source - never generic filler like "skilled in X"
- trigger_words: 2-5 short words or phrases a player would naturally use

JSON response:
{{"skills": [{{"name": "...", "type": "active", "description": "...", "trigger_words": ["w1", "w2"]}}, ... 5 total]}}"""


def _skill_refine_prompt(rpg: dict, skill: dict, menu: str | None, state: dict, instructions: dict | None = None) -> str:
    world_section = _world_context_section(state.get("world_data"))
    story_section = _story_context_section(state)
    plot_section = _plot_challenge_section(state)
    plot_req = ""
    if plot_section:
        plot_req = (
            "\n- If this ability could bear on the current plot thread's challenge, you may sharpen the "
            "description toward the ways it would matter there - but never at the cost of its identity, "
            "and never mention the thread or echo the hidden challenge in the name or description."
        )
    name = skill.get("name", "")
    strength = skill.get("strength")
    menu_note = f" ({menu})" if menu else ""
    strength_req = ""
    if strength:
        label = RARITY_LABELS.get(strength, "Common")
        strength_req = (
            f"\n- Fate has decided this skill's rarity: {label} ({strength}/10 on the quality ladder). "
            "Rarity is how well-crafted the power itself is: the higher the rarity, the STRONGER its "
            "positive effects and the WEAKER its costs, limits, and drawbacks - never the reverse - and "
            "that difference must be unmistakable from the description alone. The ladder: 5 Common is a "
            "solid, ordinary ability with real limits and costs; 6 Uncommon is notably effective with "
            "only minor limits; 7 Rare is strong beyond most peers and its costs are slight; 8 Epic is "
            "potent and versatile with drawbacks all but gone; 9 Legendary is overwhelming, with no "
            "meaningful cost or limit; 10 Mythic is a world-shaking power with none at all."
        )
        if strength >= MAX_SKILL_RATING:
            strength_req += (
                "\n- This Mythic skill sits at the absolute peak of its tier: write it as a fully "
                "realized ability already trembling at the edge of transcending its current form."
            )
    return f"""You are the game system for a text RPG. The player (currently Level {rpg.get('level', 1)}) has chosen to learn the new skill "{name}"{menu_note}. Finalize it before it is added to their sheet. Output ONLY valid JSON, no other text.

{world_section}{story_section}{plot_section}Character:
{_character_context_block(rpg)}

Chosen skill (draft):
Name: {name}
Type: {skill.get('type', 'active')}
Description: {skill.get('description') or '(none)'}
Trigger words: {', '.join(skill.get('trigger_words') or []) or '(none)'}

Refine this skill. Requirements:
{_directive("skill_refine", instructions)}{plot_req}{strength_req}
- type: "active" (deliberately used) or "passive" (always on).
- Trigger words: 2-5 short words or phrases a player would naturally use.

JSON response:
{{"name": "1-4 words", "type": "active", "description": "1-2 tight sentences", "trigger_words": ["w1", "w2"]}}"""


# --------------------------------------------------------------------------
# Skill editing API (router mounted at /api/modules/wb_core_rpg)
# --------------------------------------------------------------------------

SKILL_TYPES = ("active", "passive", "curse")

# Shared engine services injected by the server at startup (set_services).
# The router resolves the session manager through this at request time so
# tests can swap in a fake.
_services: dict = {}


def set_services(services: dict) -> None:
    global _services
    _services = services or {}


def get_router():
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel

    router = APIRouter()

    class SkillPayload(BaseModel):
        name: str | None = None  # POST: skill name; PUT: rename target
        rating: int | None = None
        description: str | None = None
        trigger_words: list[str] | None = None
        type: str | None = None

    class NewSkillSpec(BaseModel):
        name: str
        description: str | None = None
        trigger_words: list[str] | None = None
        type: str | None = None
        rating: int | None = None  # rolled strength from the wizard

    class SpendPayload(BaseModel):
        stat_allocations: dict[str, int] | None = None
        skill_allocations: dict[str, int] | None = None
        new_skill: NewSkillSpec | None = None

    class EvolvePayload(BaseModel):
        theme: str

    class CategoriesPayload(BaseModel):
        regenerate: bool = False

    class SkillOptionsPayload(BaseModel):
        menu: str = ""  # category name or search query; ignored in direct mode
        search: bool = False
        direct: bool = False  # category-free browsing (scenario skipped categories)
        page: int = 0

    class SkillRefinePayload(BaseModel):
        name: str
        description: str | None = None
        trigger_words: list[str] | None = None
        type: str | None = None
        menu: str | None = None
        forced_strength: int | None = None  # honored only when cheats.enabled

    def _session_manager():
        sm = _services.get("session_manager")
        if sm is None or not getattr(sm, "active_save_id", None):
            raise HTTPException(status_code=409, detail="No active story loaded.")
        return sm

    def _rpg_data(sm) -> dict:
        rpg = sm.state.get("module_data", {}).get("wb_core_rpg")
        if not isinstance(rpg, dict):
            raise HTTPException(status_code=404, detail="RPG character data not initialized for this story.")
        return rpg

    def _persist(sm):
        sm.save_manager.save_turn(sm.active_save_id, sm.state, sm.state.get("turn", 0))

    def _config(sm) -> dict:
        return sm.state.get("module_configs", {}).get("wb_core_rpg", {}) or {}

    def _instructions(sm) -> dict:
        # Story/scenario instruction overrides live under a host-owned
        # reserved key, alongside (not inside) the per-module settings.
        overrides = sm.state.get("module_configs", {}).get("__module_instructions__", {}) or {}
        return overrides.get("wb_core_rpg", {}) or {}

    def _llm_bridge():
        engine = _services.get("engine")
        llm = getattr(getattr(engine, "sdk", None), "llm", None)
        if llm is None:
            raise HTTPException(status_code=503, detail="LLM service is not available.")
        return llm

    def _cheats_enabled() -> bool:
        settings = _services.get("settings")
        return bool(settings.get("cheats.enabled")) if settings is not None else False

    def _require_cheats():
        # Hand-editing the character sheet (skills, status effects) bypasses
        # the story, so like forced skill rarity the gate lives server-side
        # on the global cheat toggle. Earned changes (level-up spending,
        # evolutions) stay ungated.
        if not _cheats_enabled():
            raise HTTPException(status_code=403, detail="Editing the character sheet requires cheat mode.")

    def _sync_evolutions(rpg: dict):
        rpg["pending_evolutions"] = _sync_pending_evolutions(
            rpg.get("skills", {}), rpg.get("pending_evolutions", [])
        )

    def _pending_entry(rpg: dict, key: str) -> dict | None:
        for entry in rpg.get("pending_evolutions", []):
            if isinstance(entry, dict) and entry.get("skill") == key:
                return entry
        return None

    def _evolvable_or_409(key: str, data: dict):
        if data.get("rating") != MAX_SKILL_RATING:
            raise HTTPException(status_code=409, detail=f"Skill '{key}' is not at max rating ({MAX_SKILL_RATING}).")
        if data.get("type", "active") not in EVOLVABLE_TYPES:
            raise HTTPException(status_code=409, detail="Curse skills cannot be evolved.")

    def _find_key(skills: dict, name: str) -> str | None:
        if name in skills:
            return name
        lowered = name.strip().lower()
        for key in skills:
            if key.lower() == lowered:
                return key
        return None

    def _clean_name(raw: str) -> str:
        # Skill names are stored lowercase everywhere else (Reader mutations,
        # librarian events), so edits follow suit.
        name = (raw or "").strip().lower()
        if not name:
            raise HTTPException(status_code=400, detail="Skill name cannot be empty.")
        return name

    def _validate_fields(payload: SkillPayload):
        if payload.rating is not None and not (1 <= payload.rating <= 10):
            raise HTTPException(status_code=400, detail="Rating must be between 1 and 10.")
        if payload.type is not None and payload.type not in SKILL_TYPES:
            raise HTTPException(status_code=400, detail=f"Type must be one of: {', '.join(SKILL_TYPES)}.")

    def _clean_triggers(words: list[str]) -> list[str]:
        seen, out = set(), []
        for w in words:
            word = str(w).strip()
            if word and word.lower() not in seen:
                seen.add(word.lower())
                out.append(word)
        return out

    def _clean_generated_skill(item) -> dict | None:
        """Normalize one LLM-proposed skill. Display case is kept for the
        wizard UI; the save endpoints lowercase the name on write."""
        if not isinstance(item, dict):
            return None
        name = " ".join(str(item.get("name", "")).strip().split()[:4])
        if not name:
            return None
        skill_type = str(item.get("type", "")).strip().lower()
        if skill_type not in EVOLVABLE_TYPES:  # never curse, never garbage
            skill_type = "active"
        return {
            "name": name,
            "type": skill_type,
            "description": str(item.get("description", "")).strip(),
            "trigger_words": _clean_triggers([str(w) for w in item.get("trigger_words", []) if w]),
        }

    def _clear_addskill_cache(sm):
        _addskill_cache.pop(sm.active_save_id, None)

    @router.post("/skills")
    def add_skill(payload: SkillPayload):
        _require_cheats()
        sm = _session_manager()
        rpg = _rpg_data(sm)
        skills = rpg.setdefault("skills", {})
        if payload.name is None:
            raise HTTPException(status_code=400, detail="Skill name is required.")
        _validate_fields(payload)
        name = _clean_name(payload.name)
        if _find_key(skills, name) is not None:
            raise HTTPException(status_code=409, detail=f"Skill '{name}' already exists.")
        skills[name] = {
            "rating": payload.rating if payload.rating is not None else 3,
            "description": (payload.description or "").strip(),
            "trigger_words": _clean_triggers(payload.trigger_words or []),
            "type": payload.type or "active",
        }
        _sync_evolutions(rpg)
        _clear_addskill_cache(sm)
        _persist(sm)
        return {"skills": skills}

    @router.put("/skills/{skill_name}")
    def update_skill(skill_name: str, payload: SkillPayload):
        _require_cheats()
        sm = _session_manager()
        rpg = _rpg_data(sm)
        skills = rpg.setdefault("skills", {})
        key = _find_key(skills, skill_name)
        if key is None:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found.")
        _validate_fields(payload)

        # Validate the rename before touching anything, so a rejected request
        # leaves the in-memory state untouched.
        new_name = None
        if payload.name is not None:
            new_name = _clean_name(payload.name)
            if new_name == key:
                new_name = None
            else:
                existing = _find_key(skills, new_name)
                if existing is not None and existing != key:
                    raise HTTPException(status_code=409, detail=f"Skill '{new_name}' already exists.")

        skill = skills[key]
        if payload.rating is not None:
            skill["rating"] = payload.rating
        if payload.description is not None:
            skill["description"] = payload.description.strip()
        if payload.trigger_words is not None:
            skill["trigger_words"] = _clean_triggers(payload.trigger_words)
        if payload.type is not None:
            skill["type"] = payload.type

        if new_name is not None:
            skills[new_name] = skills.pop(key)
            # Practice progression counters are keyed by skill name.
            counters = rpg.get("practice_counters")
            if isinstance(counters, dict) and key in counters:
                counters[new_name] = counters.pop(key)
            # Pending-evolution entries are keyed by skill name too.
            for entry in rpg.get("pending_evolutions", []) or []:
                if isinstance(entry, dict) and entry.get("skill") == key:
                    entry["skill"] = new_name

        _sync_evolutions(rpg)
        _persist(sm)
        return {"skills": skills}

    @router.delete("/skills/{skill_name}")
    def delete_skill(skill_name: str):
        _require_cheats()
        sm = _session_manager()
        rpg = _rpg_data(sm)
        skills = rpg.setdefault("skills", {})
        key = _find_key(skills, skill_name)
        if key is None:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found.")
        del skills[key]
        counters = rpg.get("practice_counters")
        if isinstance(counters, dict):
            counters.pop(key, None)
        _sync_evolutions(rpg)
        _persist(sm)
        return {"skills": skills}

    @router.delete("/status-effects/{effect_name}")
    def delete_status_effect(effect_name: str):
        _require_cheats()
        sm = _session_manager()
        rpg = _rpg_data(sm)
        effects = rpg.get("status_effects") or []
        lowered = effect_name.strip().lower()
        kept = [e for e in effects if not (isinstance(e, dict) and str(e.get("name", "")).lower() == lowered)]
        if len(kept) == len(effects):
            raise HTTPException(status_code=404, detail=f"Status effect '{effect_name}' not found.")
        rpg["status_effects"] = kept
        _persist(sm)
        return {"status_effects": kept}

    @router.post("/levelup/spend")
    def spend_levelup_points(payload: SpendPayload):
        sm = _session_manager()
        rpg = _rpg_data(sm)
        config = _config(sm)
        skills = rpg.setdefault("skills", {})
        stats = rpg.setdefault("stats", {})

        stat_allocations = payload.stat_allocations or {}
        skill_allocations = payload.skill_allocations or {}

        attr_available = max(0, int(rpg.get("unspent_attribute_points", 0) or 0))
        skill_available = max(0, int(rpg.get("unspent_skill_points", 0) or 0))
        max_stat = config.get("max_stat_value", 20)
        new_skill_cost = config.get("new_skill_cost", 3)

        # Validate everything before mutating anything, so a rejected request
        # leaves the in-memory state untouched.
        for stat, delta in stat_allocations.items():
            if stat not in STAT_NAMES:
                raise HTTPException(status_code=400, detail=f"Unknown stat '{stat}'.")
            if not isinstance(delta, int) or delta <= 0:
                raise HTTPException(status_code=400, detail=f"Allocation for '{stat}' must be a positive integer.")
            if int(stats.get(stat, 10)) + delta > max_stat:
                raise HTTPException(status_code=400, detail=f"Stat '{stat}' cannot exceed {max_stat}.")
        if sum(stat_allocations.values()) > attr_available:
            raise HTTPException(status_code=400, detail=f"Not enough attribute points (have {attr_available}).")

        resolved_skills: dict[str, int] = {}
        for skill_name, delta in skill_allocations.items():
            if not isinstance(delta, int) or delta <= 0:
                raise HTTPException(status_code=400, detail=f"Allocation for '{skill_name}' must be a positive integer.")
            key = _find_key(skills, skill_name)
            if key is None:
                raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found.")
            if skills[key].get("type", "active") == "curse":
                raise HTTPException(status_code=400, detail="Curse skills cannot be improved with skill points.")
            if int(skills[key].get("rating", 1)) + delta > MAX_SKILL_RATING:
                raise HTTPException(status_code=400, detail=f"Skill '{key}' cannot exceed rating {MAX_SKILL_RATING}.")
            resolved_skills[key] = delta

        new_skill_name = None
        if payload.new_skill is not None:
            spec = payload.new_skill
            if spec.type is not None and spec.type not in EVOLVABLE_TYPES:
                raise HTTPException(status_code=400, detail="New skills must be active or passive.")
            if spec.rating is not None and not (1 <= spec.rating <= MAX_SKILL_RATING):
                raise HTTPException(status_code=400, detail=f"New skill rating must be between 1 and {MAX_SKILL_RATING}.")
            new_skill_name = _clean_name(spec.name)
            if _find_key(skills, new_skill_name) is not None:
                raise HTTPException(status_code=409, detail=f"Skill '{new_skill_name}' already exists.")

        skill_cost = sum(resolved_skills.values()) + (new_skill_cost if new_skill_name else 0)
        if skill_cost > skill_available:
            raise HTTPException(status_code=400, detail=f"Not enough skill points (have {skill_available}).")

        # Apply.
        vitality_before = int(stats.get("vitality", 10))
        for stat, delta in stat_allocations.items():
            stats[stat] = int(stats.get(stat, 10)) + delta
        if int(stats.get("vitality", 10)) != vitality_before:
            hp_per_con = config.get("hp_per_vitality", config.get("hp_per_constitution", 7))
            hp_per_level = 2
            old_max = max(1, int(rpg.get("max_hp", 1) or 1))
            old_ratio = max(0, int(rpg.get("hp", 0) or 0)) / old_max
            new_max = int(stats.get("vitality", 10)) * hp_per_con + int(rpg.get("level", 1) or 1) * hp_per_level
            rpg["max_hp"] = new_max
            rpg["hp"] = min(new_max, max(0, int(new_max * old_ratio)))

        for key, delta in resolved_skills.items():
            skills[key]["rating"] = int(skills[key].get("rating", 1)) + delta

        if new_skill_name:
            spec = payload.new_skill
            # Wizard skills carry their rolled strength as the starting rating;
            # the cost stays new_skill_cost regardless (high rolls are lucky pulls).
            skills[new_skill_name] = {
                "rating": spec.rating if spec.rating is not None else min(MAX_SKILL_RATING, max(1, int(new_skill_cost))),
                "description": (spec.description or "").strip(),
                "trigger_words": _clean_triggers(spec.trigger_words or []),
                "type": spec.type or "active",
            }
            _clear_addskill_cache(sm)

        rpg["unspent_attribute_points"] = attr_available - sum(stat_allocations.values())
        rpg["unspent_skill_points"] = skill_available - skill_cost
        _sync_evolutions(rpg)
        _persist(sm)
        return rpg

    @router.post("/skills/{skill_name}/evolution-options")
    async def evolution_options(skill_name: str):
        sm = _session_manager()
        rpg = _rpg_data(sm)
        skills = rpg.setdefault("skills", {})
        key = _find_key(skills, skill_name)
        if key is None:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found.")
        data = skills[key]
        _evolvable_or_409(key, data)

        _sync_evolutions(rpg)
        entry = _pending_entry(rpg, key)
        if entry is None:  # unreachable after sync, but stay defensive
            entry = {"skill": key, "options": None, "status": "pending"}
            rpg["pending_evolutions"].append(entry)
        if entry.get("options"):
            return {"skill": key, "tier": _skill_tier(data), "options": entry["options"]}
        save_id = sm.active_save_id

        async def _generate():
            if entry.get("options"):
                return {"skill": key, "tier": _skill_tier(data), "options": entry["options"]}

            llm = _llm_bridge()
            config = _config(sm)
            prompt = _evolution_options_prompt(rpg, key, data, sm.state, instructions=_instructions(sm))
            try:
                llm._current_module = "wb_core_rpg"
                raw = await llm.generate(prompt, model_preference=config.get("evolution_ai_model", "smartest"))
            finally:
                llm._current_module = ""

            parsed = _parse_json_repair(raw)
            options = parsed.get("options") if isinstance(parsed, dict) else None
            cleaned = []
            if isinstance(options, list):
                for opt in options:
                    if not isinstance(opt, dict):
                        continue
                    theme = " ".join(str(opt.get("theme", "")).strip().split()[:3])
                    if not theme:
                        continue
                    cleaned.append({"theme": theme, "summary": str(opt.get("summary", "")).strip()})
            if len(cleaned) != 4:
                raise HTTPException(status_code=502, detail="The AI failed to produce 4 evolution options. Try again.")
            # The prompt puts the pure path first; the other three are new
            # directions. The kind steers the final evolve prompt.
            for i, opt in enumerate(cleaned):
                opt["kind"] = "pure" if i == 0 else "divergent"

            # A reload while the LLM ran replaces the state dicts this closure
            # captured; cache the options on the LIVE pending entry so they
            # survive (the captured one may be an orphan).
            if sm.active_save_id != save_id:
                return {"skill": key, "tier": _skill_tier(data), "options": cleaned}
            live_rpg = _rpg_data(sm)
            _sync_evolutions(live_rpg)
            live_entry = _pending_entry(live_rpg, key)
            if live_entry is not None:
                live_entry["options"] = cleaned
                _persist(sm)
            return {"skill": key, "tier": _skill_tier(data), "options": cleaned}

        return await _join_generation(f"{save_id}:evo-options:{key}", _generate)

    @router.post("/skills/{skill_name}/evolve")
    async def evolve_skill(skill_name: str, payload: EvolvePayload):
        sm = _session_manager()
        rpg = _rpg_data(sm)
        skills = rpg.setdefault("skills", {})
        key = _find_key(skills, skill_name)
        if key is None:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found.")
        data = skills[key]
        _evolvable_or_409(key, data)

        theme = payload.theme.strip()
        if not theme:
            raise HTTPException(status_code=400, detail="An evolution theme is required.")
        entry = _pending_entry(rpg, key)
        cached = entry.get("options") if entry else None
        chosen = next((o for o in cached or [] if o.get("theme", "").lower() == theme.lower()), None)
        if cached and chosen is None:
            raise HTTPException(status_code=400, detail=f"'{theme}' is not one of the offered evolution themes.")
        # Pre-4-option saves cached options without a kind; they evolve as
        # divergent paths, same as before.
        pure = bool(chosen) and chosen.get("kind") == "pure"
        save_id = sm.active_save_id

        async def _do_evolve():
            llm = _llm_bridge()
            config = _config(sm)
            prompt = _evolve_prompt(rpg, key, data, theme, sm.state, pure=pure, instructions=_instructions(sm))
            try:
                llm._current_module = "wb_core_rpg"
                raw = await llm.generate(prompt, model_preference=config.get("evolution_ai_model", "smartest"))
            finally:
                llm._current_module = ""

            parsed = _parse_json_repair(raw)
            if not isinstance(parsed, dict) or not str(parsed.get("name", "")).strip():
                raise HTTPException(status_code=502, detail="The AI failed to produce the evolved skill. Try again.")

            # The app may have been closed and reopened while the LLM ran,
            # which reloads the save from disk and REPLACES the state dicts
            # this closure captured. Mutating the captured `rpg`/`skills`
            # would edit an orphan and the evolution would silently vanish
            # from the character sheet - so re-resolve the live state here
            # and mutate that, never the captured references.
            if sm.active_save_id != save_id:
                raise HTTPException(status_code=409, detail="A different story was loaded while evolving. Try again.")
            live_rpg = _rpg_data(sm)
            live_skills = live_rpg.setdefault("skills", {})
            live_key = _find_key(live_skills, key)
            if live_key is None:
                raise HTTPException(status_code=409, detail=f"Skill '{key}' is no longer available to evolve.")
            live_data = live_skills[live_key]

            old_tier = _skill_tier(live_data)
            new_key = _clean_name(str(parsed["name"]))
            if new_key == live_key:
                new_key = f"{new_key} {old_tier + 1}"
            # A successful (paid) LLM call is never discarded over a name clash:
            # disambiguate deterministically instead.
            candidate, n = new_key, 2
            while _find_key(live_skills, candidate) is not None and candidate != live_key:
                candidate = f"{new_key} {n}"
                n += 1
            new_key = candidate

            evolved = {
                "rating": EVOLVED_RESET_RATING,
                "description": str(parsed.get("description", "")).strip() or live_data.get("description", ""),
                "trigger_words": _clean_triggers([str(w) for w in parsed.get("trigger_words", []) if w]) or live_data.get("trigger_words", []),
                "type": live_data.get("type", "active"),
                "tier": old_tier + 1,
                "lineage": list(live_data.get("lineage") or []) + [live_key],
                "evolution_theme": theme,
            }
            live_skills.pop(live_key)
            live_skills[new_key] = evolved
            counters = live_rpg.get("practice_counters")
            if isinstance(counters, dict) and live_key in counters:
                counters[new_key] = counters.pop(live_key)
            # Queue a one-shot storyteller note so the next generation can
            # acknowledge the transformation in the narrative.
            recent = live_rpg.get("recent_evolutions")
            if not isinstance(recent, list):
                recent = []
                live_rpg["recent_evolutions"] = recent
            recent.append({
                "old_name": live_key,
                "new_name": new_key,
                "tier": old_tier + 1,
                "theme": theme,
                "description": evolved["description"],
                "announced": False,
            })
            _sync_evolutions(live_rpg)
            _persist(sm)
            return {
                "rpg": live_rpg,
                "evolved": {
                    "old_name": live_key,
                    "new_name": new_key,
                    "tier": old_tier + 1,
                    "theme": theme,
                    "description": evolved["description"],
                },
            }

        # The evolve itself is shielded: closing the app mid-evolution lets
        # the transformation finish and persist; the widget restores the
        # reveal from recent_evolutions when the player returns.
        return await _join_generation(f"{save_id}:evolve:{key}:{theme.lower()}", _do_evolve)

    @router.delete("/skills/{skill_name}/evolution")
    def defer_evolution(skill_name: str):
        sm = _session_manager()
        rpg = _rpg_data(sm)
        skills = rpg.setdefault("skills", {})
        key = _find_key(skills, skill_name)
        if key is None:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found.")
        entry = _pending_entry(rpg, key)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Skill '{key}' has no pending evolution.")
        entry["status"] = "deferred"
        _persist(sm)
        return rpg

    async def _wizard_generate(sm, prompt: str):
        llm = _llm_bridge()
        config = _config(sm)
        try:
            llm._current_module = "wb_core_rpg"
            return await llm.generate(prompt, model_preference=config.get("new_skill_ai_model", "smartest"))
        finally:
            llm._current_module = ""

    @router.post("/skills/wizard/categories")
    async def generate_skill_categories(payload: CategoriesPayload | None = None):
        sm = _session_manager()
        rpg = _rpg_data(sm)
        # A scenario can opt the wizard out of the categories step entirely;
        # the widget reads `skip` and browses skills directly instead. Decided
        # server-side so no LLM call is spent preparing categories nobody sees.
        scenario_data = sm.state.get("scenario_data") or {}
        if scenario_data.get("skip_skill_categories"):
            return {"categories": None, "skip": True}
        regenerate = bool(payload and payload.regenerate)
        entry = _addskill_cache.setdefault(sm.active_save_id, {"categories": None, "pages": {}})
        if entry["categories"] and not regenerate:
            return {"categories": entry["categories"]}

        async def _generate():
            raw = await _wizard_generate(sm, _skill_categories_prompt(rpg, sm.state, instructions=_instructions(sm)))
            parsed = _parse_json_repair(raw)
            categories = parsed.get("categories") if isinstance(parsed, dict) else None
            cleaned, seen = [], set()
            if isinstance(categories, list):
                for cat in categories:
                    if not isinstance(cat, dict):
                        continue
                    name = " ".join(str(cat.get("name", "")).strip().split()[:3])
                    if not name or name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    cleaned.append({"name": name, "summary": str(cat.get("summary", "")).strip()})
            if len(cleaned) != 10:
                raise HTTPException(status_code=502, detail="The AI failed to produce 10 skill categories. Try again.")

            entry["categories"] = cleaned
            return {"categories": cleaned}

        # A regenerate is a deliberate click: it replaces any older in-flight
        # generation instead of joining it.
        return await _join_generation(f"{sm.active_save_id}:categories", _generate, replace=regenerate)

    @router.post("/skills/wizard/options")
    async def generate_skill_options(payload: SkillOptionsPayload):
        sm = _session_manager()
        rpg = _rpg_data(sm)
        menu = "" if payload.direct else payload.menu.strip()
        if not menu and not payload.direct:
            raise HTTPException(status_code=400, detail="A category or search query is required.")
        menu_key = "direct:all" if payload.direct else ("search:" if payload.search else "cat:") + menu.lower()

        entry = _addskill_cache.setdefault(sm.active_save_id, {"categories": None, "pages": {}})
        pages = entry["pages"].setdefault(menu_key, [])
        if payload.page < len(pages):
            return {"menu": menu, "search": payload.search, "direct": payload.direct, "page": payload.page, "skills": pages[payload.page]}
        if payload.page > len(pages):
            raise HTTPException(status_code=409, detail="Page out of order; request the next ungenerated page.")

        async def _generate():
            # A joined duplicate may arrive after the original already
            # appended the page; serve it from the cache.
            if payload.page < len(pages):
                return {"menu": menu, "search": payload.search, "direct": payload.direct, "page": payload.page, "skills": pages[payload.page]}

            exclude = [s["name"] for page in pages for s in page] + list(rpg.get("skills", {}).keys())
            raw = await _wizard_generate(
                sm,
                _skill_options_prompt(
                    rpg, menu, exclude, sm.state,
                    search=payload.search, direct=payload.direct, instructions=_instructions(sm),
                ),
            )
            parsed = _parse_json_repair(raw)
            proposals = parsed.get("skills") if isinstance(parsed, dict) else None
            excluded = {n.lower() for n in exclude}
            cleaned = []
            for item in proposals if isinstance(proposals, list) else []:
                skill = _clean_generated_skill(item)
                if skill is None or skill["name"].lower() in excluded:
                    continue
                excluded.add(skill["name"].lower())
                cleaned.append(skill)
            if len(cleaned) != 5:
                raise HTTPException(status_code=502, detail="The AI failed to produce 5 new skills. Try again.")

            pages.append(cleaned)
            return {"menu": menu, "search": payload.search, "direct": payload.direct, "page": payload.page, "skills": cleaned}

        return await _join_generation(f"{sm.active_save_id}:options:{menu_key}", _generate)

    @router.post("/skills/wizard/refine")
    async def refine_skill(payload: SkillRefinePayload):
        sm = _session_manager()
        rpg = _rpg_data(sm)
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Skill name is required.")

        # The cheat gate lives server-side: a forced strength from the client
        # only counts while the global cheat toggle is on; otherwise it is
        # silently ignored and fate rolls as usual.
        forced = payload.forced_strength if _cheats_enabled() else None
        if forced is not None and not (5 <= forced <= MAX_SKILL_RATING):
            raise HTTPException(status_code=400, detail="Forced strength must be between 5 and 10.")

        # Fate's roll is locked per skill: going back and re-picking the same
        # skill returns the identical refined result and strength - no
        # re-rolling your way to Mythic, and no repeat LLM cost. A cheat-forced
        # pick bypasses the lock (and overwrites it below): choosing a rarity
        # is the whole point of the cheat.
        entry = _addskill_cache.setdefault(sm.active_save_id, {"categories": None, "pages": {}})
        rolls = entry.setdefault("rolls", {})
        roll_key = name.lower()
        if forced is None and roll_key in rolls:
            return {"skill": rolls[roll_key]}

        async def _generate():
            if forced is None and roll_key in rolls:
                return {"skill": rolls[roll_key]}

            # The gacha moment: the rarity is rolled here, when the player
            # first commits to a pick, never client-supplied (cheats aside).
            strength = forced if forced is not None else _roll_strength()
            draft = {
                "name": name,
                "type": payload.type or "active",
                "description": (payload.description or "").strip(),
                "trigger_words": payload.trigger_words or [],
                "strength": strength,
            }

            raw = await _wizard_generate(sm, _skill_refine_prompt(rpg, draft, payload.menu, sm.state, instructions=_instructions(sm)))
            parsed = _parse_json_repair(raw)
            if not isinstance(parsed, dict) or not str(parsed.get("name", "")).strip():
                raise HTTPException(status_code=502, detail="The AI failed to refine the skill. Try again.")

            # A paid LLM call is never discarded over missing fields: fall back
            # to the draft's values, same policy as evolve_skill.
            refined = _clean_generated_skill(parsed) or {}
            result = {
                "name": refined.get("name") or draft["name"],
                "type": refined.get("type") or draft["type"],
                "description": refined.get("description") or draft["description"],
                "trigger_words": refined.get("trigger_words") or _clean_triggers([str(w) for w in draft["trigger_words"] if w]),
                "strength": strength,
            }
            rolls[roll_key] = result
            return {"skill": result}

        # A forced (cheat) pick replaces any in-flight roll for this skill
        # instead of joining it.
        return await _join_generation(
            f"{sm.active_save_id}:refine:{roll_key}", _generate, replace=forced is not None
        )

    return router


async def on_character_get_defaults(state: dict, world_context: dict) -> dict:
    hp_per_con = 7
    base_stats = {s: 10 for s in STAT_NAMES}
    vit = base_stats.get("vitality", 10)
    max_hp = vit * hp_per_con + 2
    return {
        "stats": base_stats,
        "skills": {},
        "backstory": "",
        "stat_tiers": DEFAULT_STAT_TIERS,
        "level": 1,
        "xp": 0,
        "hp": max_hp,
        "max_hp": max_hp,
        "unspent_attribute_points": 0,
        "unspent_skill_points": 0,
        "pending_evolutions": [],
        "level_up_history": [],
    }


async def on_character_validate(character_data: dict, state: dict) -> str | None:
    stats = character_data.get("stats", {})
    if not stats:
        return "Character must have stats defined."

    for stat_name in STAT_NAMES:
        val = stats.get(stat_name, 0)
        if not isinstance(val, (int, float)) or val < 1 or val > 30:
            return f"Stat '{stat_name}' must be between 1 and 30."

    skills = character_data.get("skills", {})
    for skill_name, skill_data in skills.items():
        if not isinstance(skill_data, dict):
            return f"Skill '{skill_name}' must be an object with a 'rating' field."
        rating = skill_data.get("rating", 0)
        if not isinstance(rating, (int, float)) or rating < 1 or rating > 10:
            return f"Skill '{skill_name}' rating must be between 1 and 10."
        tier = skill_data.get("tier", 1)
        if not isinstance(tier, int) or tier < 1:
            return f"Skill '{skill_name}' tier must be a positive integer."

    hp = character_data.get("hp", 85)
    max_hp = character_data.get("max_hp", 85)
    if hp > max_hp:
        return "HP cannot exceed max HP."

    return None
