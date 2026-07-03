"""Player Character Tracker -- detects and records changes to the player
character's appearance, identity, and personality after each storyteller turn.

Runs a focused LLM pass in the librarian phase (parallel with other modules'
on_librarian hooks). Detected changes are written back into the canonical
per-save character (characters["default_player"]) via the sanctioned
``character_update`` key collected by the engine's librarian node, and a
human-readable change log is kept in this module's own module_data.
"""
import json


# Canonical character fields this module is allowed to evolve. The engine's
# librarian node whitelists the same set before merging into the save.
UPDATABLE_FIELDS = ("name", "race", "full_appearance", "short_appearance", "personality")
MAX_LOG_ENTRIES = 50


def _config(state: dict) -> dict:
    return state.get("module_configs", {}).get("wb_character_tracker", {})


def _own_data(state: dict) -> dict:
    return state.get("module_data", {}).get("wb_character_tracker", {})


def _player(state: dict) -> dict:
    player = state.get("characters", {}).get("default_player")
    return player if isinstance(player, dict) else {}


def _parse_json_block(raw: str):
    """Strip Markdown code fences and parse a JSON object from an LLM reply."""
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
    config = _config(state)
    if not config.get("evolution_enabled", True):
        return None

    turn = state.get("turn", 0)
    frequency = max(1, int(config.get("evolution_frequency", 1) or 1))
    if turn == 0 or turn % frequency != 0:
        return None

    history = state.get("history", [])
    if not history:
        return None

    player = _player(state)
    if not player:
        return None

    recent = "\n".join(str(h) for h in history[-3:])[:2500]

    current = {
        "name": player.get("name", ""),
        "race": player.get("race", ""),
        "appearance": player.get("full_appearance") or player.get("short_appearance", ""),
        "personality": player.get("personality", ""),
    }

    prompt = f"""You maintain the character record for the player of a text RPG. After each scene you check whether the narration shows the player character CHANGING in a lasting way.

Report changes ONLY in these areas:
- appearance / physical condition (new scars, wounds, lost limbs, aging, a transformation, altered hair/eyes/skin)
- identity (a new name, an earned title or epithet, a change of race/species such as becoming undead or a vampire)
- personality (a lasting shift in temperament, outlook, values, or defining traits)

Do NOT report momentary emotions, temporary states, location changes, inventory, or skills/stats — only durable changes to who the character IS or how they LOOK.

CURRENT CHARACTER RECORD:
  Name: {current['name']}
  Race: {current['race']}
  Appearance: {current['appearance']}
  Personality: {current['personality'] or '(not yet described)'}

RECENT NARRATION:
{recent}

Return ONLY the fields that CHANGED this scene. If a field changed, give its NEW full value (rewrite appearance/personality in full, incorporating the change), not just the delta. If nothing durable changed, return an empty object {{}}.

Respond with ONLY valid JSON:
{{"name": "new name (only if it changed)", "race": "new race (only if it changed)", "full_appearance": "full updated appearance (only if it changed)", "personality": "full updated personality (only if it changed)", "change_note": "one short sentence describing what changed"}}"""

    model_pref = config.get("evolution_ai_model", "balanced")
    try:
        sdk.llm._current_module = "wb_character_tracker"
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return None

    character_update = {}
    for field in UPDATABLE_FIELDS:
        val = parsed.get(field)
        if isinstance(val, str) and val.strip():
            character_update[field] = val.strip()

    if not character_update:
        return None

    change_note = str(parsed.get("change_note", "")).strip()
    log = list(_own_data(state).get("evolution_log", []))
    log.append({
        "turn": turn,
        "note": change_note or f"Updated: {', '.join(character_update.keys())}",
        "fields": list(character_update.keys()),
    })
    log = log[-MAX_LOG_ENTRIES:]

    print(f"[Character Tracker] Turn {turn}: {change_note or ', '.join(character_update.keys())}")

    return {
        "character_update": character_update,
        "module_data": {"wb_character_tracker": {"evolution_log": log}},
    }


async def on_command_character(args: list[str], state: dict, sdk) -> dict:
    player = _player(state)
    if not player:
        return {"message": "[Character] No player character loaded.", "signal": "end_turn"}

    lines = [f"[Character] {player.get('name', 'Adventurer')}"]
    race = player.get("race", "")
    gender = player.get("gender", "")
    ident = " / ".join(p for p in (race, gender) if p)
    if ident:
        lines.append(ident)

    appearance = player.get("full_appearance") or player.get("short_appearance", "")
    if appearance:
        lines.append(f"\nAppearance: {appearance}")

    personality = player.get("personality", "")
    if personality:
        lines.append(f"Personality: {personality}")

    log = _own_data(state).get("evolution_log", [])
    if log:
        lines.append("\n--- Recent Changes ---")
        for entry in log[-8:]:
            lines.append(f"  [turn {entry.get('turn', '?')}] {entry.get('note', '')}")

    return {"message": "\n".join(lines), "signal": "end_turn"}
