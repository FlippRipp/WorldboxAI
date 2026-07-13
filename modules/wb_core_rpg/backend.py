"""Core RPG System -- stats, skills, XP, leveling, action judgement, HP."""
import math
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

# Each action_rating_strictness value maps to a difficulty label + guidance
# for the feasibility judge. The prompt gets ONLY the chosen tier, never the
# 1-10 scale or its band ranges.
STRICTNESS_TIERS = {
    1: ("Power Fantasy", "the player is the unstoppable protagonist of this story. Practically anything they attempt succeeds, and succeeds with flair; rate attempts 7+ unless they are truly impossible. Never turn a merely unlikely action into a 1-2."),
    2: ("Cinematic", "rule-of-cool cinema. Lean very generous - style, momentum, and daring carry attempts that mundane logic would question; when torn between two bands, pick the higher. Never turn a merely unlikely action into a 1-2."),
    3: ("Heroic", "the player is the hero. Favor them in any doubt; success is the default for anything plausibly within reach. Never turn a merely unlikely action into a 1-2."),
    4: ("Favorable", "the wind is at the player's back. Read attempts charitably, but let real overreach still fall short. Never turn a merely unlikely action into a 1-2."),
    5: ("Balanced", "judge each attempt on its merits - success and failure are both live outcomes, decided by capability and circumstance. Never turn a merely unlikely action into a 1-2."),
    6: ("Gritty", "the world has teeth. Read attempts realistically and let sloppy or lazy plans underperform. Never turn a merely unlikely action into a 1-2."),
    7: ("Demanding", "capabilities are judged strictly. An attempt needs a genuine, demonstrated edge to score above 6; ambition without preparation lands in 3-4. Never turn a merely unlikely action into a 1-2."),
    8: ("Harsh", "overreach is punished. Rate ambitious attempts a full band lower than they would otherwise earn; even sound plans succeed at a cost, and only well-prepared attempts squarely within ability score above 6. Never turn a merely unlikely action into a 1-2."),
    9: ("Merciless", "assume the least favorable plausible reading of every attempt. Anything beyond the character's proven, demonstrated ability rates 3-4 at best; a 7+ requires overwhelming advantage; most attempts fail or succeed only at a heavy price."),
    10: ("Brutal", "success is almost impossible. A 7+ is reserved for trivial actions or overwhelming, established advantage; ambitious or uncertain attempts rate 3-4; anything beyond the character's proven ability rates 1-2 and simply fails. The world is actively hostile - at this tier even a merely unlikely action may fail outright."),
}


def _strictness_tier(config: dict) -> tuple[str, str]:
    try:
        strictness = int(config.get("action_rating_strictness", 5))
    except (TypeError, ValueError):
        strictness = 5
    return STRICTNESS_TIERS[max(1, min(10, strictness))]


# Status effects are temporary conditions (broken leg, poisoned, blessed,
# brainwashed) with a ONE-sentence description. They expire after a number of
# player turns (duration_turns) or at an in-world clock time from
# wb_time_tracker (expires_at_minutes, absolute total_minutes_elapsed); with
# neither set they last until story events remove them.
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
        "duration_turns": None,
        "expires_at_minutes": None,
    }
    for key in ("duration_turns", "expires_at_minutes"):
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
        raw_effects = d.get("status_effects", [])
        c.status_effects = []
        if isinstance(raw_effects, list):
            for raw in raw_effects:
                effect = _sanitize_status_effect(raw)
                if effect is not None:
                    c.status_effects.append(effect)
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
    # Feasibility 1-2 is the only band that resolves as a hard failure.
    succeeded = feasibility >= 3

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
        assessment = await _assess_action(input_text, char, config, sdk, model_pref, state.get("world_data"), recent_story)
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


async def _assess_action(input_text: str, char: Character, config: dict, sdk, model_pref: str = "fastest", world_data: dict = None, recent_story: list = None) -> dict:
    tier_list = config.get("stat_tiers", DEFAULT_STAT_TIERS) or DEFAULT_STAT_TIERS
    difficulty_label, difficulty_guidance = _strictness_tier(config)

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
            f"[{e['kind']}] {e['description']}" for e in char.status_effects))

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
  1-2: violates the world's rules or established story facts, is physically/logically impossible, or is doomed under the current difficulty's guidance. This is the ONLY band where the attempt simply fails.
  3-4: far beyond current ability or an enormous ask, but not impossible.
  5-6: challenging; meaningful chance of failure.
  7-8: within the character's demonstrated abilities.
  9-10: near-certain success.

Judging guidelines:
- Social actions: the outcome depends on the TARGET's likely disposition as shown in the recent story and world context, not on the player's stats. A bold ask to a receptive, bored, curious, or amused character is plausible even for a weak character. Only rate a social action 1-2 if the target's established nature makes acceptance truly impossible.
- Reward creativity: a clever, novel, or dramatically interesting approach that fits the established fiction rates one band higher than a blunt attempt at the same goal. Punish contradiction of established facts, not ambition.
- Status effects: weigh them like circumstances, not stats. A [bad] effect lowers feasibility of actions it would plausibly impede (a broken leg makes sprinting far harder) and can push a directly-blocked attempt to 1-2; a [good] effect raises feasibility of actions it aids. Effects unrelated to the attempt change nothing.
- Difficulty is set to "{difficulty_label}": {difficulty_guidance}

You are a referee, not a narrator: determine the outcome, do not describe it. Never write story prose.

JSON response:
{{"feasibility": int 1-10, "skill_used": "name or empty string", "difficulty": "trivial|easy|moderate|hard|extreme|impossible", "curse_triggered": "name or empty string", "passive_effects": "brief factual note on which passives apply and how, or empty string", "failure_reason": "empty string unless feasibility is 1-2; then one short factual clause naming the world rule, established fact, or decisive capability gap the attempt founders on"}}"""

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
            # Re-applying an effect refreshes it (new duration/description).
            char.status_effects = [e for e in char.status_effects if e["name"] != effect["name"]]
            char.status_effects.append(effect)
            gained_names.add(effect["name"])
            updated = True
            print(f"[RPG] Status effect gained: '{effect['name']}' ({effect['kind']}, {_effect_duration_label(effect, now_minutes)})")

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
            if effect.get("duration_turns") is not None:
                effect["duration_turns"] -= 1
                updated = True
                if effect["duration_turns"] <= 0:
                    print(f"[RPG] Status effect wore off: '{effect['name']}'")
                    continue
            expires_at = effect.get("expires_at_minutes")
            if expires_at is not None and now_minutes is not None and now_minutes >= expires_at:
                print(f"[RPG] Status effect expired: '{effect['name']}'")
                updated = True
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

    model_pref = config.get("practice_ai_model", "fastest")
    prompt = f"""You are the game system that records supernatural or external changes to a player character's abilities in a text RPG.

Read the recent narration and detect ONLY skill changes that an EXTERNAL force imposed on the player: a god or spirit granting a power, a curse or hex stripping/weakening an ability, a blessing or artifact bestowing a skill, a magical injury disabling a skill, or a mentor/entity directly gifting knowledge.

Do NOT report skills the player improved through their own effort, practice, training, or repeated use — those are handled elsewhere. If nothing external happened, return empty arrays.

The player's current skills: {skill_lines}

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


def _render_character_sheet(char: Character, config: dict) -> str:
    tier_list = config.get("stat_tiers", DEFAULT_STAT_TIERS) or DEFAULT_STAT_TIERS

    if char.is_unconscious():
        return "The character is unconscious and incapacitated. Cannot act physically."

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

    return "\n".join(lines)


def _build_action_feasibility_prompt(char: Character, input_text: str, config: dict) -> str:
    assessment = char.action_assessment
    if not assessment:
        return ""

    try:
        feasibility = int(assessment.get("feasibility"))
    except (TypeError, ValueError):
        return ""

    if feasibility >= 7:
        ruling = "the attempt succeeds."
    elif feasibility >= 3:
        ruling = (
            "partial success or success at a cost - weave in a complication, "
            "price, or twist that moves the story forward. Never a flat refusal "
            "or dead end."
        )
    else:
        reason = str(assessment.get("failure_reason", "")).strip()
        because = f" because {reason.rstrip('.')}" if reason else ""
        ruling = (
            f"the attempt fails{because}. Show what the failed attempt visibly "
            "reveals or provokes - the world reacts; it is not a silent dead end."
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


def _evolution_options_prompt(rpg: dict, key: str, data: dict, world_data: dict | None) -> str:
    tier = _skill_tier(data)
    world_section = _world_context_section(world_data)
    return f"""You are the game system for a text RPG. A character's skill has reached maximum rating and is ready to EVOLVE into a more powerful Tier {tier + 1} form. Output ONLY valid JSON, no other text.

{world_section}Character:
{_character_context_block(rpg)}

Skill ready to evolve:
{_skill_record_block(key, data)}

Propose exactly 3 distinct thematic directions this skill could evolve toward. Each theme is a short evocative label of 1-3 words (like "Brutal", "Efficiency", "Stealthy" - but fitting THIS skill and world), plus one short clause summarizing what that path emphasizes. The three themes must be meaningfully different from each other and from the skill's current form. Do not repeat the skill's current evolution theme.

JSON response:
{{"options": [{{"theme": "1-3 words", "summary": "one short clause"}}, {{"theme": "...", "summary": "..."}}, {{"theme": "...", "summary": "..."}}]}}"""


def _evolve_prompt(rpg: dict, key: str, data: dict, theme: str, world_data: dict | None) -> str:
    tier = _skill_tier(data)
    world_section = _world_context_section(world_data)
    return f"""You are the game system for a text RPG. A character's maxed-out skill is evolving from Tier {tier} to Tier {tier + 1} down the "{theme}" path. Output ONLY valid JSON, no other text.

{world_section}Character:
{_character_context_block(rpg)}

Skill that is evolving:
{_skill_record_block(key, data)}

Chosen evolution theme: {theme}

Design the evolved Tier {tier + 1} form. Requirements:
- It MUST be strictly more powerful than the current form: broader in scope, stronger in effect, or with fewer limits - a major step up, not a rename.
- It must embody the "{theme}" theme and stay true to the world's tone and the skill's history.
- Give it a new evocative name of 2-4 words. It must not be the same as the old name.
- The description is 1-2 tight sentences: what it does, how it manifests, and any remaining limit or cost. Concrete over flowery.
- Trigger words: 2-5 short words or phrases a player would naturally use when invoking it.

JSON response:
{{"name": "2-4 word evolved name", "description": "1-2 tight sentences", "trigger_words": ["word1", "word2"]}}"""


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

    class SpendPayload(BaseModel):
        stat_allocations: dict[str, int] | None = None
        skill_allocations: dict[str, int] | None = None
        new_skill: NewSkillSpec | None = None

    class EvolvePayload(BaseModel):
        theme: str

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

    def _llm_bridge():
        engine = _services.get("engine")
        llm = getattr(getattr(engine, "sdk", None), "llm", None)
        if llm is None:
            raise HTTPException(status_code=503, detail="LLM service is not available.")
        return llm

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

    @router.post("/skills")
    def add_skill(payload: SkillPayload):
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
        _persist(sm)
        return {"skills": skills}

    @router.put("/skills/{skill_name}")
    def update_skill(skill_name: str, payload: SkillPayload):
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
            skills[new_skill_name] = {
                "rating": min(MAX_SKILL_RATING, max(1, int(new_skill_cost))),
                "description": (spec.description or "").strip(),
                "trigger_words": _clean_triggers(spec.trigger_words or []),
                "type": spec.type or "active",
            }

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

        llm = _llm_bridge()
        config = _config(sm)
        prompt = _evolution_options_prompt(rpg, key, data, sm.state.get("world_data"))
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
        if len(cleaned) != 3:
            raise HTTPException(status_code=502, detail="The AI failed to produce 3 evolution options. Try again.")

        entry["options"] = cleaned
        _persist(sm)
        return {"skill": key, "tier": _skill_tier(data), "options": cleaned}

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
        if cached and theme.lower() not in {o.get("theme", "").lower() for o in cached}:
            raise HTTPException(status_code=400, detail=f"'{theme}' is not one of the offered evolution themes.")

        llm = _llm_bridge()
        config = _config(sm)
        prompt = _evolve_prompt(rpg, key, data, theme, sm.state.get("world_data"))
        try:
            llm._current_module = "wb_core_rpg"
            raw = await llm.generate(prompt, model_preference=config.get("evolution_ai_model", "smartest"))
        finally:
            llm._current_module = ""

        parsed = _parse_json_repair(raw)
        if not isinstance(parsed, dict) or not str(parsed.get("name", "")).strip():
            raise HTTPException(status_code=502, detail="The AI failed to produce the evolved skill. Try again.")

        old_tier = _skill_tier(data)
        new_key = _clean_name(str(parsed["name"]))
        if new_key == key:
            new_key = f"{new_key} {old_tier + 1}"
        # A successful (paid) LLM call is never discarded over a name clash:
        # disambiguate deterministically instead.
        candidate, n = new_key, 2
        while _find_key(skills, candidate) is not None and candidate != key:
            candidate = f"{new_key} {n}"
            n += 1
        new_key = candidate

        evolved = {
            "rating": EVOLVED_RESET_RATING,
            "description": str(parsed.get("description", "")).strip() or data.get("description", ""),
            "trigger_words": _clean_triggers([str(w) for w in parsed.get("trigger_words", []) if w]) or data.get("trigger_words", []),
            "type": data.get("type", "active"),
            "tier": old_tier + 1,
            "lineage": list(data.get("lineage") or []) + [key],
            "evolution_theme": theme,
        }
        skills.pop(key)
        skills[new_key] = evolved
        counters = rpg.get("practice_counters")
        if isinstance(counters, dict) and key in counters:
            counters[new_key] = counters.pop(key)
        _sync_evolutions(rpg)
        _persist(sm)
        return {
            "rpg": rpg,
            "evolved": {
                "old_name": key,
                "new_name": new_key,
                "tier": old_tier + 1,
                "theme": theme,
                "description": evolved["description"],
            },
        }

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
