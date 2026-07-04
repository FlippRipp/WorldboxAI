"""Player Character Tracker -- detects and records changes to the player
character's appearance, identity, and personality after each storyteller turn.

Runs a focused LLM pass in the librarian phase (parallel with other modules'
on_librarian hooks). Detected changes are written back into the canonical
per-save character (characters["default_player"]) via the sanctioned
``character_update`` key collected by the engine's librarian node, and a
human-readable change log is kept in this module's own module_data.
"""
import json
import re


# Canonical character fields this module is allowed to evolve. The engine's
# librarian node whitelists the same set before merging into the save.
UPDATABLE_FIELDS = ("name", "gender", "race", "full_appearance", "short_appearance", "personality")
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

    # The change being detected is in THIS turn's scene, so it must always be
    # in the prompt in full; earlier scenes are only context and get whatever
    # budget is left. (A head-truncated join of the last 3 scenes used to cut
    # off the newest scene entirely once the story got going.)
    latest = str(history[-1])[-4000:]
    earlier = "\n".join(str(h) for h in history[-3:-1])[-2000:]

    current = {
        "name": player.get("name", ""),
        "gender": player.get("gender", ""),
        "race": player.get("race", ""),
        "appearance": player.get("full_appearance") or player.get("short_appearance", ""),
        "personality": player.get("personality", ""),
    }

    earlier_block = f"EARLIER NARRATION (context only):\n{earlier}\n\n" if earlier else ""

    player_action = str(state.get("last_input_text") or state.get("input_text") or "").strip()[:600]
    action_block = f"THE PLAYER'S ACTION THIS TURN:\n{player_action}\n\n" if player_action else ""

    prompt = f"""You maintain the character record for the player of a text RPG. After each scene you check whether the player's action or the narration shows the player character CHANGING in a lasting way.

Report changes ONLY in these areas:
- appearance / physical condition (new scars, wounds, lost limbs, aging, a transformation, altered hair/eyes/skin)
- identity (a new name, an earned title or epithet, a change of gender, a change of race/species such as becoming undead or a vampire)
- personality (a lasting shift in temperament, outlook, values, or defining traits)

NAME CHANGES need no ceremony or magic — report one whenever:
- the player declares or adopts a new name or alias ("call me X", introducing themselves under a new name, taking a false identity), OR
- another character gives the player a name, nickname, or title and the player accepts or answers to it, OR
- the scene consistently calls the player character something other than the recorded name below.
A deliberate rename is a durable identity change even if it happens casually in dialog.

Do NOT report momentary emotions, temporary states, location changes, inventory, or skills/stats — only durable changes to who the character IS or how they LOOK or what they are CALLED.

CURRENT CHARACTER RECORD:
  Name: {current['name']}
  Gender: {current['gender'] or '(not recorded)'}
  Race: {current['race']}
  Appearance: {current['appearance']}
  Personality: {current['personality'] or '(not yet described)'}

{earlier_block}{action_block}THIS TURN'S SCENE (check this for changes):
{latest}

Return ONLY the fields that CHANGED this scene. If a field changed, give its NEW full value (rewrite appearance/personality in full, incorporating the change), not just the delta. If the name changed, also return any other record field whose text still mentions the old name, rewritten to use the new name. If nothing durable changed, return an empty object {{}}.

Respond with ONLY valid JSON:
{{"name": "new name (only if it changed)", "gender": "new gender (only if it changed)", "race": "new race (only if it changed)", "full_appearance": "full updated appearance (only if it changed)", "personality": "full updated personality (only if it changed)", "change_note": "one short sentence describing what changed"}}"""

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

    # A rename must not leave the old name behind elsewhere in the record. The
    # LLM is asked to rewrite affected fields itself; for any it didn't return,
    # sweep the old name out deterministically.
    new_name = character_update.get("name", "")
    old_name = str(player.get("name") or "").strip()
    if new_name and old_name and new_name.lower() != old_name.lower():
        old_name_re = re.compile(rf"\b{re.escape(old_name)}\b", re.IGNORECASE)
        for field in ("full_appearance", "short_appearance", "personality"):
            if field in character_update:
                continue
            text = player.get(field)
            if isinstance(text, str) and old_name_re.search(text):
                character_update[field] = old_name_re.sub(new_name, text)

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
