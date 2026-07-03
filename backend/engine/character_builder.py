import os
import json
import shutil
import re
import random
from datetime import datetime, timezone
from typing import Any, Optional


class CharacterBuilder:
    def __init__(self, characters_dir: str = "data/characters"):
        self.characters_dir = characters_dir
        self._llm_service = None
        self._settings = None
        # Context providers let modules contribute setting context to character
        # generation. Each provider is called as provider(context: dict) -> dict
        # and the merged result is offered to every generation method.
        self._context_providers = []
        os.makedirs(self.characters_dir, exist_ok=True)

    def set_llm_service(self, llm_service):
        self._llm_service = llm_service

    def set_settings(self, settings):
        self._settings = settings

    def register_context_provider(self, provider):
        """Register a provider(context: dict) -> dict of setting-context fields.

        ``context`` is an opaque, module-contributed dict (e.g. the world module
        reads ``context['world_id']``); providers ignore keys they don't own.
        """
        self._context_providers.append(provider)

    def _gather_context(self, context: dict) -> dict:
        """Merge setting context from all registered providers."""
        context = context or {}
        ctx = {}
        for provider in self._context_providers:
            try:
                data = provider(context)
                if isinstance(data, dict):
                    ctx.update(data)
            except Exception as e:
                print(f"[CharacterBuilder] Context provider failed: {e}")
        return ctx

    @staticmethod
    def _context_json(ctx: dict, fields: list[str]) -> str:
        """Serialize a subset of gathered context, omitting empty values."""
        subset = {k: ctx[k] for k in fields if ctx.get(k)}
        return json.dumps(subset, indent=2) if subset else ""

    def _sanitize_id(self, character_id: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", character_id).lower()
        return sanitized or "character"

    def _get_character_path(self, character_id: str) -> str:
        return os.path.join(self.characters_dir, self._sanitize_id(character_id))

    async def generate_name(self, context: Optional[dict] = None, gender: str = "", race: str = "", seed: str = "") -> dict:
        if not self._llm_service:
            return {"name": self._random_name()}

        world_context = self._context_json(
            self._gather_context(context),
            ["world_name", "premise", "genre", "regions"],
        )

        constraints = []
        if gender:
            constraints.append(f"gender: {gender}")
        if race:
            constraints.append(f"race: {race}")
        if seed:
            constraints.append(f"seed: {seed}")

        constraint_text = ", ".join(constraints) if constraints else "any theme"
        world_text = f"\n\nThe character lives in this world:\n{world_context}" if world_context else ""

        prompt = f"""Generate a single unique character name for a character with these details: {constraint_text}.{world_text}

Respond with ONLY the name, nothing else. The name should be 2-3 words at most, memorable, and fitting to the context."""
        try:
            name = await self._llm_service.simple_completion(
                messages=[{"role": "user", "content": prompt}],
                model=self._llm_service.reader_model,
                temperature=0.9,
                inspector_ctx={"call_type": "character_build", "step": "character_build:name"},
            )
            name = name.strip().strip('"').strip("'").strip()
            if not name or len(name) > 100:
                name = self._random_name()
            return {"name": name}
        except Exception as e:
            print(f"[CharacterBuilder] Name generation failed: {e}")
            return {"name": self._random_name()}

    async def generate_race(self, context: Optional[dict] = None, gender: str = "", seed: str = "") -> dict:
        if not self._llm_service:
            return {"race": ""}

        world_context = self._context_json(
            self._gather_context(context),
            ["world_name", "premise", "genre", "regions", "factions"],
        )

        constraints = []
        if gender:
            constraints.append(f"gender: {gender}")
        if seed:
            constraints.append(f"inspiration: {seed}")

        constraint_text = ", ".join(constraints) if constraints else ""
        constraint_prefix = f" with these details: {constraint_text}" if constraint_text else ""
        world_text = f"\n\nThe character lives in this world:\n{world_context}" if world_context else ""

        prompt = f"""Generate a single fantasy race or species for a character{constraint_prefix}.{world_text}

The race should be fitting to the world's setting and lore. Consider the factions, regions, and genre when choosing.

Respond with ONLY the race name, nothing else. Keep it to 1-2 words maximum."""
        try:
            race = await self._llm_service.simple_completion(
                messages=[{"role": "user", "content": prompt}],
                model=self._llm_service.reader_model,
                temperature=0.9,
                inspector_ctx={"call_type": "character_build", "step": "character_build:race"},
            )
            race = race.strip().strip('"').strip("'").strip()
            if not race or len(race) > 60:
                race = ""
            return {"race": race}
        except Exception as e:
            print(f"[CharacterBuilder] Race generation failed: {e}")
            return {"race": ""}

    async def generate_full_appearance(self, short_desc: str, context: Optional[dict] = None, gender: str = "", race: str = "", name: str = "") -> dict:
        if not self._llm_service or not short_desc.strip():
            return {"full_appearance": short_desc}

        world_context = self._context_json(
            self._gather_context(context),
            ["world_name", "genre", "tone"],
        )

        world_text = f"\n\nThe character exists in this world (use for thematic inspiration):\n{world_context}" if world_context else ""
        traits = []
        if gender:
            traits.append(f"Gender: {gender}")
        if race:
            traits.append(f"Race: {race}")
        traits_text = f"\n\n{', '.join(traits)}" if traits else ""

        name_instruction = (
            f' Refer to the character by their name, "{name}", instead of pronouns like "her", "him", "he", or "she".'
            if name else ""
        )
        prompt = f"""Expand this short character appearance into a vivid, detailed 2-3 sentence description:

Short description: "{short_desc}"{traits_text}{world_text}

Write a rich physical description that brings this character to life. Include details like build, facial features, hair, eyes, skin, and distinctive marks (scars, tattoos, etc). Do NOT describe clothing, attire, equipment, or anything worn — physical body only.{name_instruction} Make it immersive and evocative. Write ONLY the expanded description, nothing else."""
        try:
            full = await self._llm_service.simple_completion(
                messages=[{"role": "user", "content": prompt}],
                model=self._llm_service.reader_model,
                temperature=0.8,
                inspector_ctx={"call_type": "character_build", "step": "character_build:appearance"},
            )
            full = full.strip().strip('"').strip("'").strip()
            return {"full_appearance": full}
        except Exception as e:
            print(f"[CharacterBuilder] Appearance generation failed: {e}")
            return {"full_appearance": short_desc}

    async def generate_stats(
        self,
        concept: str,
        context: Optional[dict] = None,
        gender: str = "",
        race: str = "",
        name: str = "",
        short_appearance: str = "",
        full_appearance: str = "",
    ) -> dict:
        """Generate stats, skills, and a polished backstory from a character concept description."""
        if not self._llm_service or not concept.strip():
            return {"stats": {}, "skills": {}, "backstory": ""}

        world_context = self._context_json(
            self._gather_context(context),
            ["world_name", "genre", "tone", "magic_level", "tech_era", "premise"],
        )

        traits = []
        if name:
            traits.append(f"Name: {name}")
        if gender:
            traits.append(f"Gender: {gender}")
        if race:
            traits.append(f"Race: {race}")
        traits_text = f"\n\nCharacter traits: {', '.join(traits)}" if traits else ""
        appearance = full_appearance or short_appearance
        appearance_text = f"\n\nCharacter appearance: {appearance}" if appearance else ""
        world_text = f"\n\nWorld context (use for thematic inspiration):\n{world_context}" if world_context else ""

        system = """You are a character designer for an isekai / light-novel RPG. Given a character concept, you design their attributes in the style of an ability panel from a fantasy world.

STAT SYSTEM (range 1-30):
- 1-4: Severely Impaired
- 5-8: Below Average
- 9-12: Average Human
- 13-16: Above Average / Trained
- 17-20: Expert / Peak Human
- 21-25: Superhuman
- 26-30: Legendary / Demigod

The six attributes:
- power: Raw physical might. Melee attacks, lifting, breaking, charging through obstacles.
- agility: Speed, reflexes, precision. Ranged attacks, stealth, acrobatics, evasion, dual-wielding.
- vitality: Stamina and resilience. HP, poison resistance, endurance, survival through hardship.
- intelligence: Knowledge and magical power. Spellcraft, investigation, languages, crafting, tactics.
- spirit: Perception and willpower. Instincts, mental resistance, spiritual awareness, sixth sense.
- charm: Presence and influence. Persuasion, commanding presence, deception, negotiation, divine favor.

SKILLS: Each skill is a specific, named ability the character possesses — not generic categories. Think of them like entries in a status screen. Each skill has a "type" field.

SKILL TYPES:
- "active": An ability the character intentionally uses. Triggered by player actions. Has trigger_words.
  Example: "shadow_step" {"type": "active", "rating": 7, "description": "Melts into the nearest shadow...", "trigger_words": ["shadow", "step", "vanish"]}
- "passive": An always-on trait or buff. Affects what the character can perceive, resist, or do without conscious effort. No trigger_words needed.
  Example: "iron_will" {"type": "passive", "rating": 6, "description": "Years of hardship forged an unshakeable mind. Minor fear and charm effects slide off like water."}
  Example: "darkvision" {"type": "passive", "rating": 5, "description": "Eyes adapted to the abyss. Sees perfectly in total darkness up to 30 meters."}
- "curse": A burden or drawback.
  With trigger_words: dormant until the player's action matches. Surfaces dramatically.
    Example: "beast_within" {"type": "curse", "rating": 4, "description": "A dormant monster slumbers in his soul. When blood is spilled, it hungers to seize control.", "trigger_words": ["rage", "blood", "kill", "feral"]}
  Without trigger_words: always active. A constant affliction the character lives with.
    Example: "fragile_constitution" {"type": "curse", "rating": 5, "description": "A childhood plague left his body frail. Physical exertion causes sharp pain and risks fainting.", "trigger_words": []}

Assign types thoughtfully. A warrior should have mostly active skills, maybe one passive and possibly a curse from their backstory. A sage might have more passives.

NAMING: Be specific and evocative. Include the character's fighting style, trade, unique traits, or world-given abilities.
  GOOD: "shadow_step", "twin_moons_slash", "beast_taming", "alchemical_brewing", "divine_judgment", "battlefield_analysis"
  BAD: "combat", "social", "magic", "knowledge" (too generic)
  GOOD: "crescent_cleave_lv3", "silver_tongue", "mana_sense", "iron_wall_stance", "forbidden_pact"

DESCRIPTION: For each skill, write 1-2 vivid sentences describing WHAT the ability does, HOW the character uses it, and its EFFECT. Use action-oriented language.
  Example: "shadow_step": {"description": "Melts into the nearest shadow and reappears up to 10 meters away. Used to ambush enemies or escape deadly blows. Leaves a faint afterimage that distracts foes for a split second."}

TRIGGER_WORDS: Include 3-5 words that, when spoken by the player, should trigger this skill. These are action words.
  Example: for "shadow_step" -> ["shadow", "step", "teleport", "vanish", "afterimage"]

RATING: 1-10. 1=novice, 5=competent, 8=expert, 10=legendary mastery. Gate by the character's backstory — a farm boy doesn't have 9-rated swordsmanship.

Include 3-5 skills that define the character. Favor quality and specificity over quantity.

BACKSTORY: Write a 2-4 sentence polished backstory in third person. Mention where they come from, a defining moment, what drives them, and a hint at their potential. Write in light-novel style — vivid, a touch dramatic, focused on identity and latent power.

STAT DISTRIBUTION: The sum of all 6 stats should typically be 60-80 (average 10-13). One or two standout stats push to 15-18 for specialists. Gaps create interesting weaknesses. Match the concept's narrative.

OUTPUT FORMAT: Return ONLY valid JSON matching this exact schema:
{
  "stats": {"power": int, "agility": int, "vitality": int, "intelligence": int, "spirit": int, "charm": int},
  "skills": {"skill_name": {"type": "active|passive|curse", "rating": int 1-10, "description": "vivid 1-2 sentence ability description", "trigger_words": ["word1", "word2", "word3"]}},
  "backstory": "2-4 sentence polished backstory in light-novel third-person style"
}"""  # noqa: E501

        prompt = f"""Create a character based on this concept: "{concept}"{traits_text}{appearance_text}{world_text}

Return ONLY the JSON object, no other text."""
        try:
            result = await self._llm_service.simple_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                model=self._llm_service.reader_model,
                temperature=0.8,
                response_format={"type": "json_object"},
                inspector_ctx={"call_type": "character_build", "step": "character_build:stats"},
            )
            parsed = json.loads(result)
            stats = parsed.get("stats", {})
            skills = parsed.get("skills", {})
            backstory = parsed.get("backstory", "")

            for s in ["power", "agility", "vitality", "intelligence", "spirit", "charm"]:
                val = stats.get(s, 10)
                stats[s] = max(1, min(30, int(val)))

            validated_skills = {}
            for skill_name, data in skills.items():
                if isinstance(data, dict):
                    rating = max(1, min(10, int(data.get("rating", 5))))
                    skill_type = data.get("type", "active")
                    if skill_type not in ("active", "passive", "curse"):
                        skill_type = "active"
                    validated_skills[str(skill_name)] = {
                        "type": skill_type,
                        "rating": rating,
                        "description": str(data.get("description", "")),
                        "trigger_words": [str(w) for w in data.get("trigger_words", [])],
                    }

            backstory = str(backstory)[:600]

            return {"stats": stats, "skills": validated_skills, "backstory": backstory}
        except Exception as e:
            print(f"[CharacterBuilder] Stats generation failed: {e}")
            return {"stats": {}, "skills": {}, "backstory": ""}

    def _random_name(self) -> str:
        prefixes = ["Al", "El", "Th", "Ar", "Ka", "Mar", "Sel", "Val", "Zan", "Lor", "Gar", "Fin"]
        suffixes = ["ara", "ion", "us", "en", "ia", "an", "or", "wyn", "eth", "is", "ok", "il"]
        return f"{random.choice(prefixes)}{random.choice(suffixes)}"

    def save_character(self, character_id: str, state: dict) -> str:
        char_id = self._sanitize_id(character_id)
        char_path = self._get_character_path(char_id)

        if os.path.exists(char_path):
            shutil.rmtree(char_path)
        os.makedirs(char_path, exist_ok=True)

        character_data = {
            "id": char_id,
            "name": state.get("name", ""),
            "gender": state.get("gender", ""),
            "race": state.get("race", ""),
            "short_appearance": state.get("short_appearance", ""),
            "full_appearance": state.get("full_appearance", ""),
            # Opaque module-contributed generation context (e.g. {"world_id": ...}).
            "context": state.get("context") or {},
            "module_data": state.get("module_data", {}),
            "created_at": state.get("created_at", datetime.now(timezone.utc).isoformat()),
        }

        with open(os.path.join(char_path, "character.json"), "w", encoding="utf-8") as f:
            json.dump(character_data, f, indent=2)

        metadata = {
            "id": char_id,
            "name": character_data["name"],
            "created_at": character_data["created_at"],
            "has_context": bool(character_data["context"]),
        }
        with open(os.path.join(char_path, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        return char_id

    def load_character(self, character_id: str) -> dict:
        char_path = self._get_character_path(character_id)
        char_file = os.path.join(char_path, "character.json")

        if not os.path.exists(char_file):
            raise FileNotFoundError(f"Character '{character_id}' not found.")

        with open(char_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Back-compat: migrate legacy world_id records into the generic context.
        if "context" not in data:
            data["context"] = {"world_id": data["world_id"]} if data.get("world_id") else {}
        return data

    def list_characters(self) -> list[dict]:
        if not os.path.exists(self.characters_dir):
            return []

        characters = []
        for item in sorted(os.listdir(self.characters_dir)):
            char_path = os.path.join(self.characters_dir, item)
            if not os.path.isdir(char_path):
                continue
            meta_path = os.path.join(char_path, "metadata.json")
            if os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                characters.append(metadata)
            else:
                char_file = os.path.join(char_path, "character.json")
                if os.path.exists(char_file):
                    with open(char_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    characters.append({
                        "id": data.get("id", item),
                        "name": data.get("name", item),
                        "created_at": data.get("created_at", ""),
                        "has_context": bool(data.get("context") or data.get("world_id")),
                    })

        characters.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        return characters

    def delete_character(self, character_id: str) -> None:
        char_path = self._get_character_path(character_id)
        if not os.path.exists(char_path):
            raise FileNotFoundError(f"Character '{character_id}' not found.")
        shutil.rmtree(char_path)
