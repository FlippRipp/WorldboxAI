"""NPC System -- background character generation and introduction management."""
import json
import uuid


NPC_ROLES = ["quest_giver", "antagonist", "ally", "informant", "rival", "neutral", "wildcard"]
DEFAULT_GENERATOR_FREQUENCY = 5
DEFAULT_MAX_POOL = 6
DEFAULT_TRAVEL_COOLDOWN_TURNS = 10

# Distance-tier check cadence, keyed off wb_time_tracker's total_minutes_elapsed
# (turns vary 5-1440 in-world minutes each, so gating on minutes rather than
# turn count keeps travel paced to actual story time).
TRAVEL_CHECK_MINUTES = {"near": 60, "far": 360, "very_far": 1440}
TRAVEL_HOP_BUDGET = {"near": 1, "far": 3, "very_far": 1}
MOTIVATION_THRESHOLD = 0.3


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
    layer_id = state.get("player_location_layer_id", "")

    candidates = []
    for npc in bank.values():
        if npc.get("introduced") or npc.get("status") != "unintroduced":
            continue

        etype = npc.get("encounter_type", "encounter")
        if etype == "encounter":
            candidates.append(npc)
        elif etype == "location_bound":
            npc_layer = npc.get("location_layer_id")
            if npc_layer and layer_id and npc_layer != layer_id:
                continue
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

    threads = _normalize_threads(state.get("module_data", {}).get("wb_npc_system", {}).get("story_threads", []))
    if threads:
        parts.append("Active story threads:")
        for t in threads:
            tag = f" (involves: {', '.join(t['npc_ids'])})" if t["npc_ids"] else ""
            parts.append(f"  - {t['text']}{tag}")

    return "\n".join(parts)


def _normalize_threads(threads: list) -> list[dict]:
    """Accept either legacy list[str] or list[{text, npc_ids}] and return the latter."""
    out = []
    for t in threads or []:
        if isinstance(t, str):
            out.append({"text": t, "npc_ids": []})
        elif isinstance(t, dict):
            out.append({"text": t.get("text", ""), "npc_ids": t.get("npc_ids", [])})
    return out


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
        rels = npc.get("relationships", [])
        if rels:
            rel_text = ", ".join(f"{r.get('type', '?')}:{r.get('npc_id', '?')}" for r in rels)
            lines.append(f"    Relationships: {rel_text}")
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


async def _update_story_threads(state: dict, sdk) -> list[dict]:
    """Ask the LLM to extract active story threads from recent history, tagged with
    which existing NPCs (if any) each thread involves."""
    history = state.get("history", [])
    if not history:
        return []

    bank = _get_bank(state)
    recent = "\n".join(str(h) for h in history[-5:])
    bank_text = _bank_summary(bank)
    prompt = f"""Extract 3-5 active story threads from this RPG narrative. These are ongoing plotlines, goals, or tensions.

Narrative:
{recent[:2000]}

EXISTING CHARACTERS:
{bank_text}

For each thread, note which existing characters (by exact npc_id from the list above) are involved, if any.
Keep each thread description under 10 words.

Respond with ONLY a JSON array:
[{{"text": "short thread description", "npc_ids": ["npc_xxxxxxxx"]}}, ...]"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="fastest")
        result = result.strip()
        if result.startswith("```"):
            parts = result.split("```")
            result = parts[1] if len(parts) > 1 else result
            if result.startswith("json"):
                result = result[4:]
            result = result.strip()

        parsed = json.loads(result)
        if not isinstance(parsed, list):
            return []

        threads = []
        for item in parsed[:5]:
            if isinstance(item, str):
                threads.append({"text": item, "npc_ids": []})
            elif isinstance(item, dict):
                npc_ids = [i for i in item.get("npc_ids", []) if i in bank]
                threads.append({"text": str(item.get("text", "")), "npc_ids": npc_ids})
        return threads
    except Exception as e:
        print(f"[NPC System] Story thread extraction failed: {e}")

    return []


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

    past = await sdk.memory.recall(pending_id, limit=2)
    past_section = ""
    if past:
        past_lines = "\n".join(f"  - {m.get('text', '')}" for m in past)
        past_section = f"\nPast interactions:\n{past_lines}\n"

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
{past_section}
How to introduce them:
- Make their entrance feel organic to the scene
- Show their personality through action and dialogue
- Do not dump their entire backstory -- reveal character through interaction

IMPORTANT: Mention the character's name clearly in the narrative so they can be identified.
</npc_introduction>"""

    return {"content": content}


def _npc_effective_location(npc: dict, state: dict) -> tuple:
    """Where this NPC actually is right now -- the player's location while
    traveling with them, otherwise their own stored location fields."""
    if npc.get("traveling_with_player"):
        return (
            state.get("player_location_node_id"),
            state.get("player_location_region"),
            state.get("player_location_layer_id"),
        )
    return (npc.get("location_node_id"), npc.get("location_region"), npc.get("location_layer_id"))


async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict | None:
    bank = _get_bank(state)
    turn = state.get("turn", 0)
    updated = False

    introductions = mutation.get("npc_introductions")
    if not isinstance(introductions, list):
        introductions = [introductions] if isinstance(introductions, dict) else []

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

        if npc.get("encounter_type") == "encounter":
            npc["encounter_type"] = "location_bound"
            npc["location_node_id"] = state.get("player_location_node_id")
            npc["location_region"] = state.get("player_location_region")
            npc["location_layer_id"] = state.get("player_location_layer_id")

        npc["last_interaction_turn"] = turn

        impression = intro.get("first_impression", "")
        if impression:
            npc["notes"] = impression

        notes = intro.get("notes", "")
        if notes and notes != impression:
            existing = npc.get("notes", "")
            npc["notes"] = f"{existing} {notes}".strip()

        memory_text = impression or notes or f"Met {npc['name']}."
        await sdk.memory.remember(npc_id, memory_text, turn, importance=6)

        updated = True
        print(f"[NPC System] {npc['name']} ({npc_id}) introduced at turn {turn}")

    party_travel = mutation.get("npc_party_travel")
    if not isinstance(party_travel, list):
        party_travel = [party_travel] if isinstance(party_travel, dict) else []

    for entry in party_travel:
        if not isinstance(entry, dict):
            continue
        npc_id = entry.get("npc_id", "")
        if not npc_id or npc_id not in bank:
            continue
        npc = bank[npc_id]
        if not npc.get("introduced"):
            continue

        joining = bool(entry.get("joining"))
        npc["traveling_with_player"] = joining
        npc["last_interaction_turn"] = turn
        if not joining:
            npc["location_node_id"] = state.get("player_location_node_id")
            npc["location_region"] = state.get("player_location_region")
            npc["location_layer_id"] = state.get("player_location_layer_id")

        updated = True
        print(f"[NPC System] {npc['name']} ({npc_id}) {'joins' if joining else 'leaves'} the party at turn {turn}")

    if updated:
        return _set_bank(
            {"pending_introduction": None, "introduction_reason": None},
            bank,
        )

    return None


def _build_layer_adjacency(world_data: dict, layer_id: str | None) -> dict[str, list[str]]:
    """Node adjacency for one layer, built from the plain world_data dict a
    module receives (mirrors the engine's internal graph builder, but modules
    can't import engine code)."""
    map_layers = world_data.get("map_layers", [])
    if map_layers:
        edges = []
        for layer in map_layers:
            if layer_id and layer.get("layer_id") != layer_id:
                continue
            edges.extend(layer.get("map", {}).get("edges", []))
    else:
        edges = world_data.get("map", {}).get("edges", [])

    adj: dict[str, list[str]] = {}
    for e in edges:
        fr, to = e.get("from"), e.get("to")
        if fr and to:
            adj.setdefault(fr, []).append(to)
            adj.setdefault(to, []).append(fr)
    return adj


def _build_node_lookup(world_data: dict, layer_id: str | None) -> dict[str, dict]:
    map_layers = world_data.get("map_layers", [])
    nodes = []
    if map_layers:
        for layer in map_layers:
            if layer_id and layer.get("layer_id") != layer_id:
                continue
            nodes.extend(layer.get("map", {}).get("nodes", []))
    else:
        nodes = world_data.get("map", {}).get("nodes", [])
    return {n["id"]: n for n in nodes if n.get("id")}


def _region_layer_id(world_data: dict, region_name: str | None) -> str | None:
    if not region_name:
        return None
    for r in world_data.get("regions", {}).get("regions", []):
        if r.get("name") == region_name:
            return r.get("layer_id")
    return None


def _distance_tier(npc: dict, state: dict) -> str:
    world_data = state.get("world_data", {}) or {}
    p_node = state.get("player_location_node_id")
    p_region = state.get("player_location_region")
    p_layer = state.get("player_location_layer_id")

    npc_layer = npc.get("location_layer_id") or _region_layer_id(world_data, npc.get("location_region"))

    if npc_layer and p_layer and npc_layer != p_layer:
        return "very_far"
    if npc.get("location_node_id") == p_node:
        return "near"
    if npc.get("location_region") == p_region:
        return "near"
    return "far"


def _npc_motivation_score(npc: dict, state: dict) -> float:
    score = 0.0
    bank = _get_bank(state)

    role = npc.get("role", "neutral")
    if role in ("quest_giver", "antagonist", "rival"):
        score += 0.6
    elif role in ("neutral", "wildcard", "informant"):
        score += 0.1

    for rel in npc.get("relationships", []):
        other = bank.get(rel.get("npc_id", ""))
        if other and other.get("introduced"):
            score += 0.3
            break

    threads = _normalize_threads(state.get("module_data", {}).get("wb_npc_system", {}).get("story_threads", []))
    if any(npc["id"] in t["npc_ids"] for t in threads):
        score += 0.4

    return min(score, 1.0)


def _travel_eligible(npc: dict, turn: int, cooldown_turns: int) -> bool:
    if not npc.get("introduced"):
        return npc.get("encounter_type") == "location_bound"
    if npc.get("traveling_with_player"):
        return False
    return (turn - npc.get("last_interaction_turn", 0)) >= cooldown_turns


def _euclidean(a: dict, b: dict) -> float:
    return ((a.get("x", 0) - b.get("x", 0)) ** 2 + (a.get("y", 0) - b.get("y", 0)) ** 2) ** 0.5


def _greedy_step(adjacency: dict, node_lookup: dict, current: str, target: str) -> str | None:
    """One hop from current toward target: the adjacent node closest (Euclidean) to target."""
    neighbors = adjacency.get(current, [])
    if not neighbors or target not in node_lookup or current not in node_lookup:
        return None
    target_node = node_lookup[target]
    best = None
    best_dist = None
    for nid in neighbors:
        nnode = node_lookup.get(nid)
        if not nnode:
            continue
        d = _euclidean(nnode, target_node)
        if best_dist is None or d < best_dist:
            best, best_dist = nid, d
    return best


def _step_toward(world_data: dict, layer_id: str, current_node: str, target_node: str, hops: int) -> str:
    adjacency = _build_layer_adjacency(world_data, layer_id)
    node_lookup = _build_node_lookup(world_data, layer_id)
    node = current_node
    for _ in range(max(1, hops)):
        nxt = _greedy_step(adjacency, node_lookup, node, target_node)
        if not nxt or nxt == node:
            break
        node = nxt
        if node == target_node:
            break
    return node


async def _llm_motivated_ids(state: dict, sdk, eligible: list[dict]) -> set[str]:
    """Ask the LLM, in a single batched call, which eligible NPCs have a
    narrative reason to travel toward the player right now."""
    scene = _scene_summary(state)
    listing = "\n".join(
        f"- {npc['id']} | {npc.get('name', '?')} ({npc.get('role', 'neutral')}): {npc.get('pitch', '')}"
        for npc in eligible
    )
    prompt = f"""You are the world simulation director for a text RPG. Off-screen, some background
characters may decide to travel toward the player because the story gives them a reason to.

CURRENT STORY STATE:
{scene}

CANDIDATE CHARACTERS (not currently in the scene):
{listing}

Decide which of these characters have a genuine narrative reason RIGHT NOW to set out toward
the player (a goal, grudge, debt, errand, or plot tie that pulls them onstage). Most should NOT --
only pick the ones the story actively motivates.

Respond with ONLY a JSON array of the motivated character ids (may be empty):
["npc_xxxxxxxx", ...]"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="fastest")
        result = result.strip()
        if result.startswith("```"):
            parts = result.split("```")
            result = parts[1] if len(parts) > 1 else result
            if result.startswith("json"):
                result = result[4:]
            result = result.strip()
        parsed = json.loads(result)
        if isinstance(parsed, list):
            valid = {npc["id"] for npc in eligible}
            return {str(i) for i in parsed if str(i) in valid}
    except Exception as e:
        print(f"[NPC System] LLM travel motivation failed, falling back to heuristic: {e}")
    # Fallback to the heuristic so a bad/empty LLM response never freezes travel.
    return {npc["id"] for npc in eligible if _npc_motivation_score(npc, state) >= MOTIVATION_THRESHOLD}


async def _compute_motivations(state: dict, sdk, eligible: list[dict], use_llm: bool) -> set[str]:
    if not eligible:
        return set()
    if use_llm:
        return await _llm_motivated_ids(state, sdk, eligible)
    return {npc["id"] for npc in eligible if _npc_motivation_score(npc, state) >= MOTIVATION_THRESHOLD}


async def _independent_travel_pass(state: dict, bank: dict, sdk) -> bool:
    """Periodically move eligible location_bound NPCs, gated by in-world elapsed
    time and biased toward the player when an NPC has narrative reason to seek
    them out. Motivation is decided either by a fast heuristic (default) or, if
    the user enables it, a single batched LLM call -- see travel_motivation_use_llm."""
    config = _config(state)
    if not config.get("autonomous_travel_enabled", True):
        return False

    world_data = state.get("world_data", {}) or {}
    turn = state.get("turn", 0)
    cooldown_turns = config.get("travel_cooldown_turns", DEFAULT_TRAVEL_COOLDOWN_TURNS)
    use_llm = config.get("travel_motivation_use_llm", False)
    total_minutes = state.get("module_data", {}).get("wb_time_tracker", {}).get("clock", {}).get("total_minutes_elapsed", 0)

    p_node = state.get("player_location_node_id")
    p_layer = state.get("player_location_layer_id")

    eligible = [
        npc for npc in bank.values()
        if _travel_eligible(npc, turn, cooldown_turns)
        and (npc.get("location_node_id") or npc.get("location_region"))
    ]
    motivated_ids = await _compute_motivations(state, sdk, eligible, use_llm)

    changed = False

    for npc in eligible:
        tier = _distance_tier(npc, state)
        threshold = TRAVEL_CHECK_MINUTES[tier]
        last_check = npc.get("last_travel_check_minutes", 0)
        if total_minutes - last_check < threshold:
            continue

        npc["last_travel_check_minutes"] = total_minutes
        motivated = npc["id"] in motivated_ids
        npc_layer = npc.get("location_layer_id") or _region_layer_id(world_data, npc.get("location_region"))

        if tier == "very_far":
            if not motivated or not npc_layer or not p_layer:
                continue
            connections = [
                c for c in world_data.get("map_connections", [])
                if c.get("from_layer_id") == npc_layer
            ]
            if not connections:
                continue
            direct = [c for c in connections if c.get("to_layer_id") == p_layer]
            connection = direct[0] if direct else connections[0]
            exit_node = connection.get("from_node_id")
            current_node = npc.get("location_node_id") or exit_node
            if current_node == exit_node:
                npc["location_layer_id"] = connection.get("to_layer_id")
                npc["location_node_id"] = connection.get("to_node_id")
                npc["location_region"] = None
                changed = True
                print(f"[NPC System] {npc['name']} ({npc['id']}) crosses into layer {npc['location_layer_id']}")
            elif exit_node:
                new_node = _step_toward(world_data, npc_layer, current_node, exit_node, hops=TRAVEL_HOP_BUDGET["very_far"])
                if new_node != current_node:
                    npc["location_node_id"] = new_node
                    changed = True
                    print(f"[NPC System] {npc['name']} ({npc['id']}) travels {current_node}->{new_node} (tier=very_far)")
            continue

        current_node = npc.get("location_node_id")
        if not current_node:
            continue

        if motivated and p_node:
            target_node = p_node
        else:
            neighbors = _build_layer_adjacency(world_data, npc_layer).get(current_node, [])
            target_node = neighbors[0] if neighbors and turn % 3 == 0 else None

        if not target_node or target_node == current_node:
            continue

        new_node = _step_toward(world_data, npc_layer, current_node, target_node, hops=TRAVEL_HOP_BUDGET[tier])
        if new_node != current_node:
            node_lookup = _build_node_lookup(world_data, npc_layer)
            new_region = node_lookup.get(new_node, {}).get("region", npc.get("location_region"))
            npc["location_node_id"] = new_node
            npc["location_region"] = new_region
            changed = True
            print(f"[NPC System] {npc['name']} ({npc['id']}) travels {current_node}->{new_node} (tier={tier})")

    return changed


async def on_librarian(state: dict, sdk) -> dict | None:
    config = _config(state)
    frequency = config.get("generator_frequency", DEFAULT_GENERATOR_FREQUENCY)
    max_pool = config.get("max_unintroduced_pool", DEFAULT_MAX_POOL)
    turn = state.get("turn", 0)

    if turn == 0 or turn % frequency != 0:
        return None

    bank = _get_bank(state)

    threads = await _update_story_threads(state, sdk)
    traveled = await _independent_travel_pass(state, bank, sdk)

    unintroduced_count = sum(
        1 for n in bank.values()
        if not n.get("introduced") and n.get("status") == "unintroduced"
    )

    if unintroduced_count >= max_pool:
        return _set_bank({"story_threads": threads}, bank) if (threads or traveled) else None

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
8. Optionally relate 0-2 new characters to EXISTING characters above via "relationships", using their exact npc_id (e.g. ally, rival, family, mentor, rumored_enemy). Omit "relationships" or leave it empty if no natural connection exists.

Respond with ONLY valid JSON:
{{"npcs": [{{"name": "string", "race": "string", "gender": "male|female|nonbinary", "appearance": "1-2 sentence physical description", "archetype": "short archetype label", "pitch": "2-3 sentence character concept with story hook", "personality": ["trait1", "trait2", "trait3"], "role": "quest_giver|antagonist|ally|informant|rival|neutral|wildcard", "encounter_type": "location_bound|encounter", "location_node_id": "node_id or null", "location_region": "region name or null", "location_layer_id": "layer_id or null (only if encounter_type is location_bound)", "relationships": [{{"npc_id": "existing npc_id", "type": "ally|rival|family|mentor|rumored_enemy|...", "description": "short description of the connection"}}]}}]}}"""

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
        return _set_bank({"story_threads": threads}, bank) if (threads or traveled) else None

    new_npcs = parsed.get("npcs", [])
    if not new_npcs:
        return _set_bank({"story_threads": threads}, bank) if (threads or traveled) else None

    for npc_data in new_npcs:
        npc_id = f"npc_{uuid.uuid4().hex[:8]}"

        role = npc_data.get("role", "neutral")
        if role not in NPC_ROLES:
            role = "neutral"

        raw_rels = npc_data.get("relationships", [])
        relationships = []
        if isinstance(raw_rels, list):
            for r in raw_rels:
                if not isinstance(r, dict):
                    continue
                rid = r.get("npc_id")
                if rid and rid in bank:
                    relationships.append({
                        "npc_id": rid,
                        "type": str(r.get("type", "neutral")),
                        "description": str(r.get("description", "")),
                    })

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
            "location_layer_id": npc_data.get("location_layer_id"),
            "introduced": False,
            "met_turn": None,
            "status": "unintroduced",
            "notes": "",
            "created_turn": turn,
            "relationships": relationships,
            "traveling_with_player": False,
            "last_interaction_turn": turn,
            "last_travel_check_minutes": 0,
        }

    print(f"[NPC System] Generated {len(new_npcs)} new NPCs (bank size: {len(bank)})")
    return _set_bank({"story_threads": threads}, bank)


async def on_command_npcs(args: list[str], state: dict, sdk) -> dict:
    bank = _get_bank(state)

    introduced = [n for n in bank.values() if n.get("introduced")]
    nearby = [n for n in bank.values() if not n.get("introduced")]
    node_id = state.get("player_location_node_id", "")
    region = state.get("player_location_region", "")
    layer_id = state.get("player_location_layer_id", "")

    lines = ["[NPCs]"]

    if introduced:
        lines.append("--- Known Characters ---")
        for npc in introduced:
            loc = ""
            if npc.get("encounter_type") == "location_bound":
                loc = f" @ {npc.get('location_node_id', npc.get('location_region', '?'))} (layer={npc.get('location_layer_id', '-')})"
            party = " [PARTY]" if npc.get("traveling_with_player") else ""
            lines.append(f"  {npc['name']} ({npc.get('archetype', '?')}) -- {npc.get('role', '?')} [{npc.get('status', '?')}]{loc}{party}")

    if nearby:
        location_bound_here = [
            n for n in nearby
            if n.get("encounter_type") == "location_bound"
            and not (n.get("location_layer_id") and layer_id and n.get("location_layer_id") != layer_id)
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
            loc = f" bound@{npc.get('location_node_id', npc.get('location_region', '?'))} layer={npc.get('location_layer_id', '-')}"
        party = " [PARTY]" if npc.get("traveling_with_player") else ""
        lines.append(f"  {intro} {npc['name']} ({npc.get('archetype', '?')}) [{npc.get('role', '?')}]{loc}{party} last_check={npc.get('last_travel_check_minutes', 0)}min")
        if npc.get("pitch"):
            lines.append(f"    {npc['pitch'][:120]}")
        rels = npc.get("relationships", [])
        if rels:
            rel_text = ", ".join(
                f"{r.get('type', '?')} of {bank.get(r.get('npc_id', ''), {}).get('name', r.get('npc_id', '?'))}"
                for r in rels
            )
            lines.append(f"    Relationships: {rel_text}")

    threads = _normalize_threads(state.get("module_data", {}).get("wb_npc_system", {}).get("story_threads", []))
    if threads:
        lines.append("\n--- Story Threads ---")
        for t in threads:
            names = ", ".join(bank.get(nid, {}).get("name", nid) for nid in t["npc_ids"])
            tag = f" (involves: {names})" if names else ""
            lines.append(f"  - {t['text']}{tag}")

    return {"message": "\n".join(lines), "signal": "end_turn"}
