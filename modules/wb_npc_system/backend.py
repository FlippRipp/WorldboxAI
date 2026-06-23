"""NPC System -- background character generation and introduction management."""
import json
import uuid


NPC_ROLES = ["quest_giver", "antagonist", "ally", "informant", "rival", "neutral", "wildcard"]
DEFAULT_GENERATOR_FREQUENCY = 5
DEFAULT_MAX_POOL = 6


def _config(state: dict) -> dict:
    return state.get("module_configs", {}).get("wb_npc_system", {})


def _get_bank(state: dict) -> dict[str, dict]:
    return state.get("module_data", {}).get("wb_npc_system", {}).get("characters", {})


def _set_bank(overrides: dict, npcs: dict[str, dict]) -> dict:
    payload = dict(overrides)
    payload["characters"] = npcs
    return {"module_data": {"wb_npc_system": payload}}


def _filter_candidates(state: dict) -> list[dict]:
    bank = _get_bank(state)
    node_id = state.get("player_location_node_id", "")
    region = state.get("player_location_region", "")

    candidates = []
    for npc in bank.values():
        if npc.get("introduced") or npc.get("status") != "unintroduced":
            continue

        etype = npc.get("encounter_type", "encounter")
        if etype == "encounter":
            candidates.append(npc)
        elif etype == "location_bound":
            if npc.get("location_node_id") == node_id or npc.get("location_region") == region:
                candidates.append(npc)

    return candidates


def _scene_summary(state: dict) -> str:
    history = state.get("history", [])
    recent = history[-5:] if len(history) > 5 else history

    parts = [
        f"Location: node={state.get('player_location_node_id', 'unknown')}, region={state.get('player_location_region', 'unknown')}",
        f"Player action: {state.get('input_text', '(system/game start)')}",
        f"Turn: {state.get('turn', 0)}",
    ]

    if recent:
        parts.append("Recent events:")
        for i, h in enumerate(recent):
            parts.append(f"  [{i + 1}] {h}")

    threads = state.get("module_data", {}).get("wb_npc_system", {}).get("story_threads", [])
    if threads:
        parts.append(f"Active story threads: {', '.join(threads)}")

    return "\n".join(parts)


def _bank_summary(bank: dict[str, dict]) -> str:
    if not bank:
        return "  (no NPCs yet)"

    lines = []
    for npc_id, npc in bank.items():
        status = "(introduced)" if npc.get("introduced") else "(unintroduced)"
        lines.append(
            f"  - [{npc_id}] {npc.get('name')} -- {npc.get('archetype')} "
            f"({npc.get('role', 'neutral')}, {npc.get('encounter_type', 'encounter')}) {status}\n"
            f"    Pitch: {npc.get('pitch', '')}"
        )
    return "\n".join(lines)


def _world_context(state: dict) -> str:
    world_data = state.get("world_data", {})
    if not world_data:
        return ""

    parts = []
    regions = world_data.get("regions", {}).get("regions", [])
    if regions:
        parts.append("Regions:")
        for r in regions:
            parts.append(f"  - {r.get('name', 'unknown')}: terrain={r.get('terrain', '?')}, climate={r.get('climate', '?')}")
        factions = world_data.get("regions", {}).get("factions", [])
        if factions:
            parts.append(f"  Factions: {', '.join(factions)}")

    lore = world_data.get("lore", {})
    if lore:
        prem = lore.get("premise", "")
        if prem:
            parts.append(f"Premise: {prem}")

    return "\n".join(parts)


async def on_gather_context(state: dict, sdk) -> dict | None:
    config = _config(state)

    if not config.get("introduction_enabled", True):
        return None

    bank = _get_bank(state)
    candidates = _filter_candidates(state)

    if not candidates:
        return None

    scene = _scene_summary(state)

    candidate_text = ""
    for i, npc in enumerate(candidates):
        candidate_text += (
            f"[{i}] ID: {npc['id']} | {npc['name']} ({npc.get('race', '?')}, {npc.get('gender', '?')})\n"
            f"    Archetype: {npc.get('archetype', '')}\n"
            f"    Role: {npc.get('role', 'neutral')}\n"
            f"    Pitch: {npc.get('pitch', '')}\n"
            f"    Personality: {', '.join(npc.get('personality', []))}\n"
            f"    Type: {npc.get('encounter_type', 'encounter')}\n\n"
        )

    prompt = f"""You are a narrative director. Given the current scene, decide if a new character should be introduced.

SCENE:
{scene}

AVAILABLE CHARACTERS (unintroduced, in this location):
{candidate_text}

RULES:
- Only introduce a character if the scene naturally calls for one (player enters a populated area, seeks information, encounters travelers, needs help, etc.)
- Do NOT introduce anyone if the player is alone in wilderness, mid-combat, or the scene is self-contained.
- If introducing, pick the character that best fits the scene's tone and needs.
- Prefer location-bound NPCs over encounter NPCs when at their specific location.
- IMPORTANT: Never introduce an NPC in the very first turn (turn 0) -- the opening scene should establish the world first.
- An NPC who is already in the story (introduced) should not be re-introduced.

Respond with ONLY valid JSON:
{{"introduce": true/false, "npc_id": "id or null", "reason": "one sentence why/why not"}}"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="fastest")
        result = result.strip()
        if result.startswith("```"):
            parts = result.split("```")
            result = parts[1] if len(parts) > 1 else result
            if result.startswith("json"):
                result = result[4:]
            result = result.strip()

        decision = json.loads(result)
    except Exception as e:
        print(f"[NPC System] Introduction Agent failed: {e}")
        return None

    if not decision.get("introduce"):
        return None

    npc_id = decision.get("npc_id")
    if not npc_id or npc_id not in bank:
        return None

    return _set_bank(
        {"pending_introduction": npc_id, "introduction_reason": decision.get("reason", "")},
        bank,
    )


async def on_render_prompt_block(block: dict, state: dict, sdk) -> dict | None:
    block_id = block.get("id", "")
    if block_id != "npc_introduction":
        return None

    bank = _get_bank(state)
    npc_data = state.get("module_data", {}).get("wb_npc_system", {})
    pending_id = npc_data.get("pending_introduction")
    reason = npc_data.get("introduction_reason", "")

    if not pending_id or pending_id not in bank:
        return None

    npc = bank[pending_id]
    personality = ', '.join(npc.get('personality', []))

    content = f"""<npc_introduction>
A new character should be introduced in this scene. Weave them naturally into the narrative.

Character to introduce:
  Name: {npc.get('name')}
  Race: {npc.get('race')}
  Gender: {npc.get('gender')}
  Appearance: {npc.get('appearance')}
  Archetype: {npc.get('archetype')}
  Personality: {personality}
  Narrative Role: {npc.get('role', 'neutral')}
  Character Pitch: {npc.get('pitch')}

Why they should appear now: {reason}

How to introduce them:
- Make their entrance feel organic to the scene
- Show their personality through action and dialogue
- Do not dump their entire backstory -- reveal character through interaction

IMPORTANT: Mention the character's name clearly in the narrative so they can be identified.
</npc_introduction>"""

    return {"content": content}


async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict | None:
    introductions = mutation.get("npc_introductions")
    if not introductions:
        return None

    if not isinstance(introductions, list):
        introductions = [introductions] if isinstance(introductions, dict) else []

    bank = _get_bank(state)
    turn = state.get("turn", 0)
    updated = False

    for intro in introductions:
        if not isinstance(intro, dict):
            continue
        npc_id = intro.get("npc_id", "")
        if not npc_id or npc_id not in bank:
            continue

        npc = bank[npc_id]
        npc["introduced"] = True
        npc["met_turn"] = turn
        npc["status"] = "active"

        impression = intro.get("first_impression", "")
        if impression:
            npc["notes"] = impression

        notes = intro.get("notes", "")
        if notes and notes != impression:
            existing = npc.get("notes", "")
            npc["notes"] = f"{existing} {notes}".strip()

        updated = True
        print(f"[NPC System] {npc['name']} ({npc_id}) introduced at turn {turn}")

    if updated:
        return _set_bank(
            {"pending_introduction": None, "introduction_reason": None},
            bank,
        )

    return None


async def on_librarian(state: dict, sdk) -> dict | None:
    config = _config(state)
    frequency = config.get("generator_frequency", DEFAULT_GENERATOR_FREQUENCY)
    max_pool = config.get("max_unintroduced_pool", DEFAULT_MAX_POOL)
    turn = state.get("turn", 0)

    if turn == 0 or turn % frequency != 0:
        return None

    bank = _get_bank(state)

    unintroduced_count = sum(
        1 for n in bank.values()
        if not n.get("introduced") and n.get("status") == "unintroduced"
    )

    if unintroduced_count >= max_pool:
        return None

    scene = _scene_summary(state)
    bank_text = _bank_summary(bank)
    wctx = _world_context(state)
    region = state.get("player_location_region", "unknown")
    node_id = state.get("player_location_node_id", "")

    needed = min(3, max_pool - unintroduced_count)

    prompt = f"""You are a character designer for a text-based RPG. Create {needed} new NPC concepts for the game.

WORLD CONTEXT:
{wctx}

CURRENT STORY STATE:
{scene}

EXISTING CHARACTERS (DO NOT duplicate or create similar concepts):
{bank_text}

INSTRUCTIONS:
1. Create {needed} characters that fill gaps NOT covered by existing NPCs.
2. Each character must have a DISTINCT archetype, personality, and role from all existing ones.
3. At least one should be location-bound to the current region: {region}
4. The rest can be encounter-type (can appear anywhere).
5. Characters should feel authentic to this world's genre, factions, and regions.
6. Pitches should be 2-3 sentences -- a hook that suggests story potential.
7. Personality should be exactly 3 keywords describing core traits.

Respond with ONLY valid JSON:
{{"npcs": [{{"name": "string", "race": "string", "gender": "male|female|nonbinary", "appearance": "1-2 sentence physical description", "archetype": "short archetype label", "pitch": "2-3 sentence character concept with story hook", "personality": ["trait1", "trait2", "trait3"], "role": "quest_giver|antagonist|ally|informant|rival|neutral|wildcard", "encounter_type": "location_bound|encounter", "location_node_id": "node_id or null", "location_region": "region name or null"}}]}}"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="balanced")
        result = result.strip()
        if result.startswith("```"):
            parts = result.split("```")
            result = parts[1] if len(parts) > 1 else result
            if result.startswith("json"):
                result = result[4:]
            result = result.strip()

        parsed = json.loads(result)
    except Exception as e:
        print(f"[NPC System] Generator Agent failed: {e}")
        return None

    new_npcs = parsed.get("npcs", [])
    if not new_npcs:
        return None

    for npc_data in new_npcs:
        npc_id = f"npc_{uuid.uuid4().hex[:8]}"

        role = npc_data.get("role", "neutral")
        if role not in NPC_ROLES:
            role = "neutral"

        bank[npc_id] = {
            "id": npc_id,
            "name": npc_data.get("name", "Unknown"),
            "race": npc_data.get("race", ""),
            "gender": npc_data.get("gender", ""),
            "appearance": npc_data.get("appearance", ""),
            "archetype": npc_data.get("archetype", ""),
            "pitch": npc_data.get("pitch", ""),
            "personality": npc_data.get("personality", []),
            "role": role,
            "encounter_type": npc_data.get("encounter_type", "encounter"),
            "location_node_id": npc_data.get("location_node_id"),
            "location_region": npc_data.get("location_region"),
            "introduced": False,
            "met_turn": None,
            "status": "unintroduced",
            "notes": "",
            "created_turn": turn,
        }

    print(f"[NPC System] Generated {len(new_npcs)} new NPCs (bank size: {len(bank)})")
    return _set_bank({}, bank)


async def on_command_npcs(args: list[str], state: dict, sdk) -> dict:
    bank = _get_bank(state)

    introduced = [n for n in bank.values() if n.get("introduced")]
    nearby = [n for n in bank.values() if not n.get("introduced")]
    node_id = state.get("player_location_node_id", "")
    region = state.get("player_location_region", "")

    lines = ["[NPCs]"]

    if introduced:
        lines.append("--- Known Characters ---")
        for npc in introduced:
            loc = ""
            if npc.get("encounter_type") == "location_bound":
                loc = f" @ {npc.get('location_node_id', npc.get('location_region', '?'))}"
            lines.append(f"  {npc['name']} ({npc.get('archetype', '?')}) -- {npc.get('role', '?')} [{npc.get('status', '?')}]{loc}")

    if nearby:
        location_bound_here = [
            n for n in nearby
            if n.get("encounter_type") == "location_bound"
            and (n.get("location_node_id") == node_id or n.get("location_region") == region)
        ]
        encounter = [n for n in nearby if n.get("encounter_type") == "encounter"]

        if location_bound_here:
            lines.append(f"\n--- In This Location ({len(location_bound_here)} bound) ---")
            for npc in location_bound_here:
                lines.append(f"  {npc['name']} ({npc.get('archetype', '?')}) -- {npc.get('role', '?')}")

        if encounter:
            lines.append(f"\n--- Travelers ({len(encounter)} encounter) ---")
            for npc in encounter:
                lines.append(f"  {npc['name']} ({npc.get('archetype', '?')}) -- {npc.get('role', '?')}")

    if not introduced and not nearby:
        lines.append("  No NPCs generated yet.")

    lines.append("")
    lines.append(f"Total bank: {len(bank)} NPCs ({len(introduced)} introduced, {len(bank) - len(introduced)} pending)")

    return {"message": "\n".join(lines), "signal": "end_turn"}


async def on_command_npclist(args: list[str], state: dict, sdk) -> dict:
    bank = _get_bank(state)
    if not bank:
        return {"message": "[NPC List] No NPCs in bank.", "signal": "end_turn"}

    lines = ["[NPC List] Full Bank"]
    for npc_id, npc in sorted(bank.items(), key=lambda x: x[1].get("created_turn", 0)):
        intro = "[INTRO]" if npc.get("introduced") else "[pending]"
        loc = ""
        if npc.get("encounter_type") == "location_bound":
            loc = f" bound@{npc.get('location_node_id', npc.get('location_region', '?'))}"
        lines.append(f"  {intro} {npc['name']} ({npc.get('archetype', '?')}) [{npc.get('role', '?')}]{loc}")
        if npc.get("pitch"):
            lines.append(f"    {npc['pitch'][:120]}")

    return {"message": "\n".join(lines), "signal": "end_turn"}
