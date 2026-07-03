"""Core RPG System -- stats, skills, XP, leveling, action judgement, HP."""
import math
import random
import json
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
                c.skills[name] = {
                    "rating": data.get("rating", 1),
                    "description": data.get("description", ""),
                    "trigger_words": data.get("trigger_words", []),
                    "type": data.get("type", "active"),
                }
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
        return c


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
            skill_names = [f"{n} ({d['rating']}/10)" for n, d in active_skills.items()]
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
    strictness = config.get("action_rating_strictness", 5)

    active_skills = {n: d for n, d in char.skills.items() if d.get("type") == "active"}
    passive_skills = {n: d for n, d in char.skills.items() if d.get("type") == "passive"}
    curse_skills = {n: d for n, d in char.skills.items() if d.get("type") == "curse"}

    skill_lines = []
    if active_skills:
        skill_lines.append("Active skills: " + ", ".join(
            f'{n} ({d["rating"]}/10): {d.get("description", "")}' for n, d in active_skills.items()
        ))
    if passive_skills:
        skill_lines.append("Passive skills: " + ", ".join(
            f'{n} ({d["rating"]}/10): {d.get("description", "")}' for n, d in passive_skills.items()
        ))
    if curse_skills:
        for n, d in curse_skills.items():
            triggers = d.get("trigger_words", [])
            if triggers:
                skill_lines.append(f'Curse "{n}" ({d["rating"]}/10): triggers on [{", ".join(triggers)}] - {d.get("description", "")}')
            else:
                skill_lines.append(f'Curse "{n}" ({d["rating"]}/10) [always active]: {d.get("description", "")}')

    stats_line = ", ".join(f"{s}={char.stats[s]} ({_tier_for(char.stats[s], tier_list)})" for s in STAT_NAMES)
    unconscious = " [UNCONSCIOUS - cannot act physically]" if char.is_unconscious() else ""

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

    world_section = ""
    if world_lines:
        world_section = "World context:\n" + "\n".join(world_lines) + "\n"

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
  1-2: violates the world's rules or established story facts, or is physically/logically impossible. This is the ONLY band where the attempt simply fails.
  3-4: far beyond current ability or an enormous ask, but not impossible.
  5-6: challenging; meaningful chance of failure.
  7-8: within the character's demonstrated abilities.
  9-10: near-certain success.

Judging guidelines:
- Social actions: the outcome depends on the TARGET's likely disposition as shown in the recent story and world context, not on the player's stats. A bold ask to a receptive, bored, curious, or amused character is plausible even for a weak character. Only rate a social action 1-2 if the target's established nature makes acceptance truly impossible.
- Reward creativity: a clever, novel, or dramatically interesting approach that fits the established fiction rates one band higher than a blunt attempt at the same goal. Punish contradiction of established facts, not ambition.
- Strictness is {strictness}/10: 1-3 means cinematic - lean generous, favor the player and rule-of-cool; 4-6 means balanced; 7-10 means simulationist - judge capabilities strictly. Strictness shifts scores within bands but never turns a merely unlikely action into a 1-2.

outcome_narrative by feasibility band:
  7-10: they succeed - describe the successful result.
  3-6: partial success or success at a cost - a complication, price, or twist that moves the story forward. Never a flat refusal or dead end.
  1-2: the attempt fails - state WHY in world terms and what the failed attempt visibly reveals or provokes. The world reacts; it is not a silent dead end.

JSON response:
{{"feasibility": int 1-10, "skill_used": "name or empty string", "difficulty": "trivial|easy|moderate|hard|extreme|impossible", "curse_triggered": "name or empty string", "passive_effects": "brief note or empty string", "outcome_narrative": "1-2 concise objective sentences describing the outcome per the band rules above. Then state every passive and every curse with a short description of each. Be objective and factual, not narrative or flowery. Never mention stat names, numbers, or game mechanics."}}"""

    try:
        result = await sdk.llm.generate(prompt, model_preference=model_pref)
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if not cleaned:
            print(f"[RPG] Action assessment failed: empty LLM response")
            return {}
        try:
            assessment = json.loads(cleaned)
        except json.JSONDecodeError:
            if cleaned.startswith("{"):
                brace_count = cleaned.count("{") - cleaned.count("}")
                if brace_count > 0:
                    cleaned = cleaned.rstrip() + "}" * brace_count
                    try:
                        assessment = json.loads(cleaned)
                    except json.JSONDecodeError:
                        print(f"[RPG] Action assessment failed: unable to parse JSON response: {cleaned[:200]}")
                        return {}
                else:
                    print(f"[RPG] Action assessment failed: invalid JSON response: {cleaned[:200]}")
                    return {}
            else:
                print(f"[RPG] Action assessment failed: response is not JSON: {cleaned[:200]}")
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
            "outcome_narrative": str(assessment.get("outcome_narrative", "")),
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
    if not mutation:
        return {}

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
                if change.get("type"):
                    char.skills[name]["type"] = skill_type
            else:
                char.skills[name] = {
                    "rating": max(1, min(10, rating or 3)),
                    "description": description or f"Proficiency in {skill_name}",
                    "trigger_words": trigger_words,
                    "type": skill_type,
                }
            updated = True

    # 4. Progress system
    progression = config.get("progression_system", "xp")

    if progression == "xp":
        xp_gained = mutation.get("xp_gained", 0)
        if isinstance(xp_gained, int) and xp_gained > 0:
            char.xp += xp_gained
            steepness = config.get("xp_curve_steepness", 2)
            while total_xp_for_level(char.level + 1, steepness) <= char.xp:
                _apply_level_up(char, char.level + 1)
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
            _apply_level_up(char, char.level + 1)
            char.recalc_hp(hp_per_con)
            char.hp = char.max_hp
            updated = True

    if updated:
        if char.max_hp == 0:
            char.recalc_hp(hp_per_con)
        return {"module_data": {"wb_core_rpg": char.to_dict()}}
    return {}


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
    recent = "\n".join(str(h) for h in history[-3:])[:2500]

    if char.skills:
        skill_lines = ", ".join(f"{n} ({d['rating']}/10, {d.get('type', 'active')})" for n, d in char.skills.items())
    else:
        skill_lines = "(none)"

    model_pref = config.get("practice_ai_model", "fastest")
    prompt = f"""You are the game system that records supernatural or external changes to a player character's abilities in a text RPG.

Read the recent narration and detect ONLY skill changes that an EXTERNAL force imposed on the player: a god or spirit granting a power, a curse or hex stripping/weakening an ability, a blessing or artifact bestowing a skill, a magical injury disabling a skill, or a mentor/entity directly gifting knowledge.

Do NOT report skills the player improved through their own effort, practice, training, or repeated use — those are handled elsewhere. If nothing external happened, return empty arrays.

The player's current skills: {skill_lines}

RECENT NARRATION:
{recent}

Respond with ONLY valid JSON:
{{"added": [{{"name": "skill_name", "rating": 1-10, "description": "1-2 sentence vivid description", "trigger_words": ["word1", "word2"], "type": "active|passive|curse"}}], "removed": ["skill_name"], "altered": [{{"name": "existing_skill_name", "new_rating": 1-10, "description": "optional updated description"}}]}}"""

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
        key = str(name).strip().lower()
        if key in char.skills:
            del char.skills[key]
            char.practice_counters.pop(key, None)
            updated = True
            print(f"[RPG] External event removed skill '{key}'")

    for entry in parsed.get("altered", []) or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("name", "")).strip().lower()
        if key not in char.skills:
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
        return {"module_data": {"wb_core_rpg": char.to_dict()}}
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
    return {"message": "\n".join(lines), "signal": "end_turn"}


async def on_command_skills(args, state, sdk):
    char = Character.from_dict(state.get("module_data", {}).get("wb_core_rpg", {}))
    if not char.skills:
        return {"message": "[Skills] No skills acquired yet.", "signal": "end_turn"}
    lines = ["[Skills]"]
    for name, data in sorted(char.skills.items(), key=lambda x: -x[1]["rating"]):
        bar = "\u2588" * data["rating"] + "\u2591" * (10 - data["rating"])
        lines.append(f"  {name.title()}: {bar} ({data['rating']}/10)")
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
        abilities = [d.get("description") or f"proficient in {n}" for n, d in active_skills.items()]
        lines.append("Abilities: " + "; ".join(abilities) + ".")

    passive_parts = []
    for n, d in passive_skills.items():
        passive_parts.append(d.get("description") or n)
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

    if char.backstory:
        lines.append(f"Backstory: {char.backstory}")

    return "\n".join(lines)


def _build_action_feasibility_prompt(char: Character, input_text: str, config: dict) -> str:
    assessment = char.action_assessment
    if not assessment:
        return ""

    outcome = assessment.get("outcome_narrative", "")
    if not outcome:
        return ""

    difficulty = assessment.get("difficulty", "moderate")
    lines = [
        f"The player attempted to: \"{input_text}\"",
        f"Assessed difficulty: {difficulty}",
        f"Suggested outcome: {outcome}",
        "Guidance: This assessment is advisory. Honor its difficulty, but adapt the "
        "specifics to the living scene - if established characters or events make a "
        "different outcome more natural, follow the story. Unless the difficulty is "
        "\"impossible\", do not resolve the action as a flat refusal or dead end: "
        "fail forward with a cost, complication, partial result, or new opportunity.",
    ]
    return "\n".join(lines)


def _apply_level_up(char: Character, new_level: int):
    char.level = new_level
    # Pick the 2 most-used stats since last level-up (context-aware)
    ranked = sorted(char.stat_usage.items(), key=lambda x: -x[1])
    improved = [ranked[0][0], ranked[1][0]]
    for stat in improved:
        if char.stats[stat] < 20:
            char.stats[stat] += 1
    # Reset usage counters after level-up
    char.stat_usage = {s: 0 for s in STAT_NAMES}
    char.recalc_hp()
    char.hp = char.max_hp


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

    hp = character_data.get("hp", 85)
    max_hp = character_data.get("max_hp", 85)
    if hp > max_hp:
        return "HP cannot exceed max HP."

    return None
