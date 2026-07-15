"""NPC System -- background character generation and introduction management."""
import asyncio
import json
import re
import urllib.parse
import uuid


NPC_ROLES = ["quest_giver", "antagonist", "ally", "informant", "rival", "neutral", "wildcard"]
NPC_STATUSES = ["unintroduced", "active", "departed", "deceased"]

# Fields the character browser may change, via manual edit or story update.
EDITABLE_FIELDS = ("name", "race", "gender", "appearance", "archetype", "pitch",
                   "personality", "role", "notes", "status")
# Subset the story-update pass is asked to rewrite (race/gender/archetype are
# creation-time facts the story rarely changes; edit them manually instead).
UPDATE_FIELDS = ("name", "appearance", "personality", "pitch", "role", "status", "notes")
# Fields that feed _profile_text -- changing any of these makes the RAG profile
# stale, so edits touching them must replace the stored embedding.
PROFILE_FIELDS = ("name", "race", "gender", "appearance", "archetype", "pitch",
                  "personality", "role")
MAX_CHANGE_LOG = 20
MAX_GEN_REQUEST_CHARS = 500  # cap on the free-text /npc generate brief
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


def _parse_json_block(raw: str):
    """Strip Markdown code fences and parse a JSON value from an LLM reply."""
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


def _profile_text(npc: dict) -> str:
    """A compact, self-contained description of a character for RAG storage."""
    parts = [f"Character: {npc.get('name', 'Unknown')}"]
    ident = " / ".join(p for p in (npc.get("race", ""), npc.get("gender", "")) if p)
    if ident:
        parts.append(ident)
    if npc.get("archetype"):
        parts.append(f"Archetype: {npc['archetype']}")
    parts.append(f"Role: {npc.get('role', 'neutral')}")
    if npc.get("appearance"):
        parts.append(f"Appearance: {npc['appearance']}")
    personality = ", ".join(npc.get("personality", []))
    if personality:
        parts.append(f"Personality: {personality}")
    if npc.get("pitch"):
        parts.append(f"Background: {npc['pitch']}")
    return ". ".join(parts)


async def _embed_profile(npc: dict, turn: int, sdk, force: bool = False) -> None:
    """Embed a character's full profile into RAG as a permanent memory so it
    stays retrievable for the rest of the story. Idempotent per NPC unless
    ``force`` is set, which deletes the stored profile and embeds the current
    one in its place."""
    if npc.get("profile_embedded") and not force:
        return
    npc_id = npc.get("id")
    if not npc_id:
        return
    if npc.get("profile_embedded"):
        await sdk.memory.forget(npc_id, tags=["profile"])
    await sdk.memory.remember(
        npc_id, _profile_text(npc), turn,
        importance=8, permanent=True, tags=["profile"],
    )
    npc["profile_embedded"] = True


async def _refresh_profile_embedding(npc: dict, changed_fields: list[str], state: dict, sdk) -> None:
    """After an edit, replace the character's RAG profile if any field that
    feeds it changed. NPCs with no embedded profile yet are left alone -- they
    get an already-current embedding when they are introduced."""
    if not npc.get("profile_embedded"):
        return
    if not _config(state).get("embed_profiles", True):
        return
    if not any(f in PROFILE_FIELDS for f in changed_fields):
        return
    await _embed_profile(npc, state.get("turn", 0), sdk, force=True)


def _build_npc_record(npc_data: dict, turn: int, bank: dict, *, introduced: bool = False,
                      source: str = "generated", location: tuple = (None, None, None)) -> dict:
    """Build a bank record from raw generated/captured character fields, keeping
    the schema in one place. ``location`` is (node_id, region, layer_id)."""
    npc_id = f"npc_{uuid.uuid4().hex[:8]}"

    role = npc_data.get("role", "neutral")
    if role not in NPC_ROLES:
        role = "neutral"

    relationships = []
    raw_rels = npc_data.get("relationships", [])
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

    node_id, region, layer_id = location
    return npc_id, {
        "id": npc_id,
        "name": npc_data.get("name", "Unknown"),
        "race": npc_data.get("race", ""),
        "gender": npc_data.get("gender", ""),
        "appearance": npc_data.get("appearance", ""),
        "archetype": npc_data.get("archetype", ""),
        "pitch": npc_data.get("pitch", ""),
        "personality": npc_data.get("personality", []),
        "role": role,
        "encounter_type": "location_bound" if introduced else npc_data.get("encounter_type", "encounter"),
        "location_node_id": node_id if introduced else npc_data.get("location_node_id"),
        "location_region": region if introduced else npc_data.get("location_region"),
        "location_layer_id": layer_id if introduced else npc_data.get("location_layer_id"),
        "introduced": introduced,
        "met_turn": turn if introduced else None,
        "status": "active" if introduced else "unintroduced",
        "creation_need": str(npc_data.get("need", "") or "").strip(),
        "notes": "",
        "created_turn": turn,
        "source": source,
        "relationships": relationships,
        "traveling_with_player": False,
        "last_interaction_turn": turn,
        "last_travel_check_minutes": 0,
    }


def _get_bank(state: dict) -> dict[str, dict]:
    bank = state.get("module_data", {}).get("wb_npc_system", {}).get("characters", {})
    # Reconcile records whose status was manually flipped to active before the
    # introduction sync existed: an active character is by definition known.
    # Healing here repairs every stale save the first time any hook or command
    # touches the bank, and the fix persists with the next state save.
    for npc in bank.values():
        if isinstance(npc, dict) and npc.get("status") == "active" and not npc.get("introduced"):
            npc["introduced"] = True
            if npc.get("met_turn") is None:
                npc["met_turn"] = state.get("turn", 0)
    return bank


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


def _plot_profile_block(state: dict) -> str:
    """When the Plot Director module is present, surface its learned story
    profile (tone, themes, the player's weighted likes/dislikes and observed
    avoids) and its evolving narrative direction (premise, heading, open
    questions) so newly generated characters serve the same direction. Returns
    "" when the module is absent, unmet, or has learned nothing yet."""
    plot = state.get("module_data", {}).get("wb_plot_director")
    if not isinstance(plot, dict):
        return ""
    profile = plot.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    direction = plot.get("direction")
    direction = direction if isinstance(direction, dict) else {}

    tone = str(profile.get("tone") or "").strip()
    themes = [str(t).strip() for t in profile.get("themes", []) if str(t).strip()]

    def _prefs(entries) -> list[str]:
        out = []
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                text = str(e.get("text", "")).strip()
                weight = e.get("weight", "medium")
            else:
                text, weight = str(e).strip(), "medium"
            if text:
                out.append(f"{text} ({weight})")
        return out

    likes = _prefs(profile.get("likes"))
    dislikes = _prefs(profile.get("dislikes"))
    avoids = _prefs(profile.get("avoids"))

    premise = str(direction.get("premise") or "").strip()
    heading = str(direction.get("heading") or "").strip()
    questions = [str(q).strip() for q in direction.get("open_questions", []) if str(q).strip()]

    if not (tone or themes or likes or dislikes or avoids or premise):
        return ""

    lines = [
        "STORY DIRECTION (from the Plot Director -- align this character with the story's "
        "established tone and themes and the player's tastes; the weight in parentheses marks "
        "how strongly the player feels):"
    ]
    if premise:
        lines.append(f"Arc premise: {premise}")
    if heading:
        lines.append(f"Heading: {heading}")
    if questions:
        lines.append("Open questions: " + "; ".join(questions))
    if tone:
        lines.append(f"Tone: {tone}")
    if themes:
        lines.append(f"Themes: {', '.join(themes)}")
    if likes:
        lines.append(f"Player enjoys (lean into these): {'; '.join(likes)}")
    if dislikes:
        lines.append(f"Player dislikes (steer clear of these): {'; '.join(dislikes)}")
    if avoids:
        lines.append(f"Player tends to steer away from (observed): {'; '.join(avoids)}")
    return "\n".join(lines)


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


RECENT_STORY_ENTRIES = 3

# Tokens too generic to identify a character by ("The Hooded Stranger" must not
# alias "The Grey Warden" just because both start with "the").
NAME_STOPWORDS = {"the", "of", "a", "an", "and"}


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^\w']+", str(name).lower())
            if len(t) >= 3 and t not in NAME_STOPWORDS}


def _same_character_name(a: str, b: str) -> bool:
    """Loose identity check between two character names: exact match
    (case-insensitive) or one name's tokens a subset of the other's, so
    "Kara" and "Kara Vane" or "Borin" and "Borin Ironvein" count as the same
    character. An epithet and a real name never match here -- that
    reconciliation needs story context and is the capture LLM's job."""
    a_l = str(a).strip().lower()
    b_l = str(b).strip().lower()
    if not a_l or not b_l:
        return False
    if a_l == b_l:
        return True
    ta, tb = _name_tokens(a_l), _name_tokens(b_l)
    return bool(ta and tb and (ta <= tb or tb <= ta))


def _find_by_name(name: str, bank: dict) -> dict | None:
    """Resolve a story name to a bank record: exact name first, then a loose
    match -- but only when it is unambiguous (exactly one candidate)."""
    target = str(name).strip().lower()
    if not target:
        return None
    for npc in bank.values():
        if str(npc.get("name", "")).strip().lower() == target:
            return npc
    loose = [npc for npc in bank.values() if _same_character_name(name, npc.get("name", ""))]
    return loose[0] if len(loose) == 1 else None


def _named_in_recent_story(npc: dict, state: dict) -> bool:
    """Whether the character is mentioned by name in the last few story turns.
    Catches on-stage characters whose location data can't confirm presence --
    saves without location tracking, or records the travel pass moved."""
    name = str(npc.get("name", "")).strip()
    if not name:
        return False
    recent = " ".join(str(h) for h in state.get("history", [])[-RECENT_STORY_ENTRIES:])
    return bool(re.search(rf"\b{re.escape(name)}\b", recent, re.IGNORECASE))


def _named_in_latest_story(npc: dict, state: dict) -> bool:
    """Whether this turn's narration names the character. Token-tolerant --
    "Borin Ironvein" counts when the story says just "Borin" -- because it is
    used to confirm a pending introduction the storyteller was explicitly
    instructed to make, where a strict full-name match would miss it."""
    history = state.get("history", [])
    if not history:
        return False
    latest = str(history[-1])
    return any(
        re.search(rf"\b{re.escape(t)}\b", latest, re.IGNORECASE)
        for t in _name_tokens(npc.get("name", ""))
    )


def _presence_pin_fresh(npc: dict, turn) -> bool:
    """Player-authority scene pin: stamped when a character is manually added
    or activated from the browser, honored for the following turn so presence
    tracking (which only knows the story so far) cannot drop a character the
    player just placed in the scene. By the time the pin expires the character
    has been in the storyteller's context and story-based tracking takes over."""
    pinned = npc.get("presence_pinned_turn")
    try:
        return pinned is not None and int(turn or 0) - int(pinned) <= 1
    except (TypeError, ValueError):
        return False


async def _llm_scene_presence(state: dict, sdk, candidates: list[dict]) -> set[str]:
    """One fast-model call deciding which candidate characters are physically
    in the current scene. Used when there is no location tracking to consult.
    Falls back to name matching so a bad LLM reply never hides a character."""
    scene = "\n".join(str(h) for h in state.get("history", [])[-RECENT_STORY_ENTRIES:])[-3000:]
    listing = "\n".join(
        f"- {npc['id']} | {npc.get('name', '?')} ({npc.get('archetype', '')}): {npc.get('pitch', '')}"
        for npc in candidates
    )
    prompt = f"""You track which characters are on stage in a text RPG.

RECENT STORY (oldest to newest):
{scene}

KNOWN CHARACTERS:
{listing}

Which of these characters are PHYSICALLY PRESENT in the current scene -- actually there with the player right now? Being mentioned, remembered, or discussed does not count as present.

Respond with ONLY a JSON array of the present character ids (may be empty):
["npc_xxxxxxxx", ...]"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="fastest")
        parsed = _parse_json_block(result)
        if isinstance(parsed, list):
            valid = {npc["id"] for npc in candidates}
            return {str(i) for i in parsed if str(i) in valid}
    except Exception as e:
        print(f"[NPC System] LLM scene presence failed, falling back to name matching: {e}")
    return {npc["id"] for npc in candidates if _named_in_recent_story(npc, state)}


async def _present_npcs(state: dict, sdk) -> list[dict]:
    """Introduced, living characters who are in the player's scene right now.
    Party members always count. With location tracking, so do characters at
    the player's node/region (plus recent story mentions, since stored
    locations can drift). Without location tracking, a fast LLM pass decides
    from the story itself (name matching when disabled or on failure)."""
    p_node = state.get("player_location_node_id")
    p_region = state.get("player_location_region")
    p_layer = state.get("player_location_layer_id")
    location_tracked = bool(p_node or p_region)

    present = []
    undetermined = []
    for npc in _get_bank(state).values():
        # status alone decides: "active" implies known, even on records whose
        # introduced flag predates the browser-activation sync.
        if npc.get("status") != "active":
            continue
        if npc.get("traveling_with_player") or _presence_pin_fresh(npc, state.get("turn")):
            present.append(npc)
            continue
        if location_tracked:
            node, region, layer = _npc_effective_location(npc, state)
            layer_ok = not (layer and p_layer and layer != p_layer)
            located_here = layer_ok and (
                (node and node == p_node) or (region and region == p_region)
            )
            if located_here or _named_in_recent_story(npc, state):
                present.append(npc)
        else:
            undetermined.append(npc)

    if undetermined and state.get("history"):
        if _config(state).get("scene_presence_use_llm", True):
            present_ids = await _llm_scene_presence(state, sdk, undetermined)
            present.extend(npc for npc in undetermined if npc["id"] in present_ids)
        else:
            present.extend(npc for npc in undetermined if _named_in_recent_story(npc, state))
    return present


def _present_characters_context(state: dict, present: list[dict]) -> str:
    """A per-turn context block with the established records of every character
    in the scene, so the storyteller always has them -- RAG retrieval is
    query-dependent and can miss a character who is standing right there."""
    if not present or not _config(state).get("present_character_context", True):
        return ""

    lines = ["Characters currently present in the scene. Keep them consistent with these established records:"]
    for npc in present:
        party = " (traveling with the player)" if npc.get("traveling_with_player") else ""
        lines.append(f"- {_profile_text(npc)}{party}")
        notes = str(npc.get("notes", "")).strip()
        if notes:
            lines.append(f"  Notes: {notes[-400:]}")
    return "\n".join(lines)


async def on_gather_context(state: dict, sdk) -> dict | None:
    result = {}

    # Scene presence (active characters) and the introduction pass
    # (unintroduced candidates) look at disjoint parts of the bank, so their
    # LLM calls run concurrently instead of back-to-back.
    present, introduction = await asyncio.gather(
        _present_npcs(state, sdk),
        _introduction_pass(state, sdk),
    )

    context = _present_characters_context(state, present)
    if context:
        result["context_string"] = context

    if introduction:
        result.update(introduction)

    # Publish this turn's scene roster so other modules (e.g. image gen's
    # character reference and LoRA gating) share the storyteller's notion of
    # who is present. A character about to be introduced counts: they will
    # appear in the narration this roster is consumed against.
    payload = result.setdefault("module_data", {}).setdefault("wb_npc_system", {})
    npc_ids = [npc["id"] for npc in present if npc.get("id")]
    pending = payload.get("pending_introduction")
    if pending and pending not in npc_ids:
        npc_ids.append(pending)
    payload["scene_presence"] = {
        "turn": int(state.get("turn") or 0),
        "npc_ids": npc_ids,
    }
    return result


async def _introduction_pass(state: dict, sdk) -> dict | None:
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
        need = str(npc.get("creation_need", "") or "").strip()
        need_line = f"    Created for: {need}\n" if need else ""
        candidate_text += (
            f"[{i}] ID: {npc['id']} | {npc['name']} ({npc.get('race', '?')}, {npc.get('gender', '?')})\n"
            f"    Archetype: {npc.get('archetype', '')}\n"
            f"    Role: {npc.get('role', 'neutral')}\n"
            f"    Pitch: {npc.get('pitch', '')}\n"
            f"{need_line}"
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


def _player_name(state: dict) -> str:
    player = state.get("characters", {}).get("default_player")
    if isinstance(player, dict):
        return str(player.get("name", "") or "").strip()
    return ""


def _known_names(bank: dict, state: dict) -> set[str]:
    names = {str(n.get("name", "")).strip().lower() for n in bank.values() if n.get("name")}
    player = _player_name(state)
    if player:
        names.add(player.lower())
    return names


async def on_mutation_schema(state: dict, sdk) -> dict | None:
    """Dynamic reader schema. Two jobs: (1) spell out the ids of unmet bank
    characters so the reader can report an introduction by a VALID npc_id --
    the reader only ever sees the story text plus this schema, so without the
    roster every introduction is reported with a hallucinated id and silently
    dropped, leaving the character unintroduced, out of the storyteller's
    context, drifting from their record, and ripe for duplicate capture.
    (2) Ask the reader to flag significant characters the storyteller
    introduced on its own, excluding everyone we already know. Reuses the
    reader pass so no extra per-turn LLM call is spent."""
    bank = _get_bank(state)
    schema = {}

    unmet = [n for n in bank.values()
             if not n.get("introduced") and n.get("status") == "unintroduced"]
    if unmet:
        roster = "; ".join(f'{n["id"]} = "{n.get("name", "?")}"' for n in unmet)
        schema["npc_introductions"] = (
            "array of objects: {npc_id: string (MUST be an id from the list below), "
            "name: string (the name the story uses for them), first_impression: string "
            "(one sentence describing how they met the player), notes: string (optional "
            "evolving observations)} -- report each listed character who appears in "
            "person in this scene for the first time. Known-but-unmet characters: "
            f"{roster}. The story may render a name slightly differently (shortened, "
            "a title added) -- match by identity and always use the npc_id from the "
            "list. Never report a character who is not on the list."
        )
    else:
        schema["npc_introductions"] = (
            "always an empty array -- there are no unmet characters who could be introduced."
        )

    if not _config(state).get("capture_story_characters", True):
        return schema

    known = sorted(n for n in _known_names(bank, state) if n)
    known_list = ", ".join(known) if known else "(none yet)"
    schema["story_characters"] = (
        "array of objects: {name: string, descriptor: string (who they are / what "
        "they did this scene), evidence: string (brief quote or action from the scene)} "
        "for characters who materially speak or act in this scene and are NOT already "
        "known. Use the character's name; if the story has not named them yet but they "
        "clearly matter, coin a short distinctive title-case epithet from how the story "
        "refers to them (e.g. 'The Hooded Stranger', 'The One-Eyed Innkeeper') and use "
        "that as the name. "
        f"Already-known characters (exclude these, case-insensitive): {known_list} -- "
        "some of these are epithets for characters not yet named by the story; match "
        "them by identity, not exact wording. "
        "Omit incidental extras with no story weight (a passing guard, the crowd), "
        "the player, and anyone already known."
    )
    return schema


async def _introduce_existing(npc: dict, state: dict, sdk, memory_text: str = "") -> None:
    """Bring an existing bank character into play: everything a story
    introduction sets, shared by the reader-reported path, the pending-
    introduction fallback, and duplicate-capture resolution."""
    turn = state.get("turn", 0)
    npc["introduced"] = True
    npc["met_turn"] = turn
    npc["status"] = "active"

    if npc.get("encounter_type") == "encounter":
        npc["encounter_type"] = "location_bound"
        npc["location_node_id"] = state.get("player_location_node_id")
        npc["location_region"] = state.get("player_location_region")
        npc["location_layer_id"] = state.get("player_location_layer_id")

    npc["last_interaction_turn"] = turn

    await sdk.memory.remember(
        npc["id"], memory_text or f"Met {npc.get('name', 'someone')}.", turn, importance=6,
    )
    if _config(state).get("embed_profiles", True):
        await _embed_profile(npc, turn, sdk)
    print(f"[NPC System] {npc.get('name')} ({npc['id']}) introduced at turn {turn}")


def _is_epithet(name: str) -> bool:
    return str(name).strip().lower().startswith("the ")


def _rename_collides(new_name: str, npc: dict, bank: dict, state: dict) -> bool:
    """A rename must never take a name that already identifies someone else --
    another bank character or the player. An LLM confusing two characters
    would otherwise silently merge their records."""
    other = _find_by_name(new_name, bank)
    if other is not None and other.get("id") != npc.get("id"):
        return True
    player = _player_name(state)
    return bool(player and _same_character_name(new_name, player))


def _finish_rename(npc: dict, old_name: str) -> None:
    """Bookkeeping shared by every rename path, run after the name field is
    already updated: sweep the old name out of the record's other text fields
    (any the LLM did not rewrite itself) and keep it retrievable in notes, so
    story references to the old name still resolve to this character."""
    new_name = str(npc.get("name", ""))
    old_name = str(old_name).strip()
    if not old_name or old_name.lower() == new_name.lower():
        return
    pattern = re.compile(rf"\b{re.escape(old_name)}\b", re.IGNORECASE)
    for field in ("appearance", "pitch", "notes"):
        text = npc.get(field)
        if isinstance(text, str) and pattern.search(text):
            npc[field] = pattern.sub(new_name, text)
    notes = str(npc.get("notes", "")).strip()
    npc["notes"] = f"{notes} Formerly known as {old_name}.".strip()


async def _adopt_story_name(npc: dict, story_name: str, state: dict, bank: dict, sdk) -> bool:
    """When the capture pass resolves a reported character to an existing
    record under a different name -- the story revealed the real name of a
    character tracked under a coined epithet, or now firmly calls them
    something new -- rename the record instead of letting the story name
    spawn a duplicate. Never renames TO an epithet (an epithet reported for
    a real-named character is a description, not a new name)."""
    story_name = str(story_name).strip()
    if (not story_name
            or _same_character_name(story_name, npc.get("name", ""))
            or _is_epithet(story_name)
            or _rename_collides(story_name, npc, bank, state)):
        return False
    old = npc.get("name", "")
    npc["name"] = story_name
    _finish_rename(npc, old)
    _log_change(npc, state.get("turn", 0),
                f"Story revealed {old} is named {story_name}", ["name"], "story")
    await _refresh_profile_embedding(npc, ["name"], state, sdk)
    return True


async def _capture_story_characters(mutation: dict, state: dict, bank: dict, sdk) -> bool:
    """Generate full profiles for significant characters the story introduced on
    its own, add them to the bank as introduced NPCs, and embed them into RAG.
    Reported characters who are actually existing bank characters -- under the
    same name, a variant of it, or (LLM-resolved) a different name entirely --
    are introduced in place instead of duplicated."""
    if not _config(state).get("capture_story_characters", True):
        return False

    reported = mutation.get("story_characters")
    if isinstance(reported, dict):
        reported = [reported]
    if not isinstance(reported, list) or not reported:
        return False

    player = _player_name(state)
    turn = state.get("turn", 0)
    added = False
    pending = []
    for entry in reported:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name or (player and _same_character_name(name, player)):
            continue
        if any(_same_character_name(name, p["name"]) for p in pending):
            continue
        descriptor = str(entry.get("descriptor", "")).strip()

        # A name variant of a character already in the bank is that character,
        # not a new one: introduce them if the story just put them on stage.
        match = _find_by_name(name, bank)
        if match:
            if not match.get("introduced"):
                await _introduce_existing(match, state, sdk,
                                          memory_text=descriptor or f"Met {match.get('name', name)}.")
                added = True
            continue

        pending.append({
            "name": name,
            "descriptor": descriptor,
            "evidence": str(entry.get("evidence", "")).strip(),
        })

    if not pending:
        return added

    scene = "\n".join(str(h) for h in state.get("history", [])[-3:])[-2500:]
    listing = "\n".join(
        f"- {p['name']}: {p['descriptor']}" + (f" (in the scene: {p['evidence']})" if p["evidence"] else "")
        for p in pending
    )

    roster_section = ""
    dedup_rule = ""
    if bank:
        roster = "\n".join(
            f"- {npc['id']} | {npc.get('name', '?')} ({npc.get('archetype', '')}): {npc.get('pitch', '')}"
            for npc in bank.values()
        )
        roster_section = f"""

CHARACTERS ALREADY ON RECORD (for identity checks -- these are already tracked):
{roster}"""
        dedup_rule = ("\n\nIMPORTANT -- no duplicates: compare each character to profile against the "
                      "characters already on record. If one of them is actually an existing character "
                      "appearing under a different name, spelling, title, or epithet (or the story "
                      "finally revealed the real name of a character recorded under an epithet), do "
                      'NOT build a profile for them. Instead return {"existing_npc_id": "<their '
                      'npc_id from the record list>", "name": "<the name the story now uses>"} for '
                      "that character. Only profile genuinely new characters.")

    prompt = f"""You are a character designer for a text RPG. The story just introduced the following characters on its own. Build a full character record for EACH one, staying faithful to how they appear in the scene — do not contradict it, and infer sensible details where the scene is silent.

SCENE:
{scene}

CHARACTERS TO PROFILE (use these exact names):
{listing}{roster_section}

Some of these names may be descriptive epithets for characters the story has not named yet (e.g. 'The Hooded Stranger'). Keep such an epithet as the name exactly as given -- do NOT invent a real name for them; the record is renamed once the story reveals one.{dedup_rule}

For each character return:
- name (exactly as given above)
- race, gender (infer from the scene; use "unknown" if truly unclear)
- appearance: 1-2 sentence physical description grounded in the scene; ALWAYS state hair color and eye color (or the being's closest equivalent -- fur, scales, glow), inferring plausible ones where the scene is silent
- archetype: short label
- pitch: 2-3 sentence concept with a story hook
- personality: exactly 3 trait keywords
- role: one of quest_giver|antagonist|ally|informant|rival|neutral|wildcard

Respond with ONLY valid JSON:
{{"npcs": [{{"name": "string", "race": "string", "gender": "male|female|nonbinary|unknown", "appearance": "string", "archetype": "string", "pitch": "string", "personality": ["trait1", "trait2", "trait3"], "role": "string"}}]}}"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="balanced")
        parsed = _parse_json_block(result)
    except Exception as e:
        print(f"[NPC System] Story-character capture failed: {e}")
        return added

    if not isinstance(parsed, dict):
        return added
    new_npcs = parsed.get("npcs", [])
    if not isinstance(new_npcs, list) or not new_npcs:
        return added

    location = (
        state.get("player_location_node_id"),
        state.get("player_location_region"),
        state.get("player_location_layer_id"),
    )

    for npc_data in new_npcs:
        if not isinstance(npc_data, dict):
            continue

        # The profiler recognized a reported "new" character as an existing
        # record: introduce/rename that record instead of duplicating it.
        existing_id = str(npc_data.get("existing_npc_id") or "").strip()
        if existing_id:
            npc = bank.get(existing_id)
            if not npc:
                continue
            story_name = str(npc_data.get("name", "")).strip()
            descriptor = next(
                (p["descriptor"] for p in pending if _same_character_name(p["name"], story_name)), "")
            if await _adopt_story_name(npc, story_name, state, bank, sdk):
                added = True
            if not npc.get("introduced"):
                await _introduce_existing(npc, state, sdk,
                                          memory_text=descriptor or f"Met {npc.get('name', story_name)}.")
                added = True
            continue

        name = str(npc_data.get("name", "")).strip()
        if not name or _find_by_name(name, bank) or (player and _same_character_name(name, player)):
            continue

        npc_id, record = _build_npc_record(
            npc_data, turn, bank, introduced=True, source="story", location=location,
        )
        record["last_interaction_turn"] = turn
        bank[npc_id] = record

        descriptor = next((p["descriptor"] for p in pending if _same_character_name(p["name"], name)), "")
        await sdk.memory.remember(npc_id, descriptor or f"Encountered {name}.", turn, importance=6)
        if _config(state).get("embed_profiles", True):
            await _embed_profile(record, turn, sdk)

        added = True
        print(f"[NPC System] Captured story character {name} ({npc_id}) at turn {turn}")

    return added


def _resolve_intro_npc(intro: dict, bank: dict) -> dict | None:
    """Resolve a reader-reported introduction to a bank record. Reader models
    regularly echo the character's name (or a hallucinated id) instead of the
    bank id, so fall back to name matching among unmet characters -- dropping
    the introduction silently is what leaves records unintroduced and lets the
    same character be captured again as a duplicate."""
    npc_id = str(intro.get("npc_id", "") or "").strip()
    if npc_id in bank:
        return bank[npc_id]
    for candidate in (npc_id, str(intro.get("name", "") or "").strip()):
        if not candidate:
            continue
        match = _find_by_name(candidate, bank)
        if match and not match.get("introduced"):
            return match
    return None


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
        npc = _resolve_intro_npc(intro, bank)
        if npc is None or npc.get("introduced"):
            continue

        impression = intro.get("first_impression", "")
        if impression:
            npc["notes"] = impression

        notes = intro.get("notes", "")
        if notes and notes != impression:
            existing = npc.get("notes", "")
            npc["notes"] = f"{existing} {notes}".strip()

        await _introduce_existing(npc, state, sdk,
                                  memory_text=impression or notes or f"Met {npc['name']}.")
        updated = True

    # Fallback for the turn's planned introduction: the storyteller was
    # explicitly told to bring this character on stage, so if the reader
    # failed to report it by a resolvable id but the narration names them,
    # register the introduction anyway. Otherwise the record stays
    # unintroduced while the character walks the story -- the exact split
    # that ends in duplicate capture.
    npc_module_data = state.get("module_data", {}).get("wb_npc_system", {})
    pending_id = npc_module_data.get("pending_introduction")
    if pending_id:
        npc = bank.get(pending_id)
        if npc and not npc.get("introduced") and _named_in_latest_story(npc, state):
            await _introduce_existing(npc, state, sdk)
            updated = True

    if await _capture_story_characters(mutation, state, bank, sdk):
        updated = True

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

    # Always clear a consumed pending introduction, even when nothing else
    # changed: leaving it set re-renders the "introduce this character" block
    # every following turn, making the storyteller re-introduce someone who is
    # already in the scene.
    if updated or pending_id or npc_module_data.get("introduction_reason"):
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


def _tracking_candidates(state: dict, bank: dict) -> list[dict]:
    """Known, living characters who plausibly appear in this turn's scene --
    the change-tracking pass only spends prompt space (and the LLM's
    attention) on them: party members, the published scene roster (when
    fresh; it is stamped one turn behind by the time the librarian runs),
    characters the latest narration names, and anyone the reader just
    touched this turn (introductions, party changes)."""
    turn = int(state.get("turn") or 0)
    presence = state.get("module_data", {}).get("wb_npc_system", {}).get("scene_presence")
    scene_ids = set()
    if isinstance(presence, dict):
        try:
            if abs(turn - int(presence.get("turn"))) <= 1:
                scene_ids = {str(i) for i in presence.get("npc_ids") or []}
        except (TypeError, ValueError):
            pass

    candidates = []
    for npc in bank.values():
        if not npc.get("introduced") or npc.get("status") != "active":
            continue
        try:
            recently_touched = turn - int(npc.get("last_interaction_turn") or 0) <= 1
        except (TypeError, ValueError):
            recently_touched = False
        if (npc.get("traveling_with_player") or npc.get("id") in scene_ids
                or recently_touched or _named_in_latest_story(npc, state)):
            candidates.append(npc)
    return candidates


def _tracking_record(npc: dict) -> str:
    personality = ", ".join(npc.get("personality", []))
    return (
        f"[{npc['id']}] Name: {npc.get('name', '')}\n"
        f"  Race/Gender: {npc.get('race', '') or '?'} / {npc.get('gender', '') or '?'}\n"
        f"  Appearance: {npc.get('appearance', '') or '(not yet described)'}\n"
        f"  Personality: {personality or '(not yet described)'}\n"
        f"  Role: {npc.get('role', 'neutral')} | Status: {npc.get('status', 'active')}\n"
        f"  Pitch: {npc.get('pitch', '') or '(none)'}\n"
        f"  Notes: {npc.get('notes', '') or '(none)'}"
    )


async def _track_character_changes(state: dict, bank: dict, sdk) -> bool:
    """Per-turn evolution pass for NPCs, mirroring wb_character_tracker's pass
    for the player: one batched LLM call checks this turn's scene for lasting
    changes to the characters plausibly in it -- renames included -- and
    rewrites the changed record fields. Runs in the librarian phase, parallel
    with other modules' post-turn work, so it neither burdens the reader's
    mutation schema nor delays the storyteller."""
    if not _config(state).get("track_character_changes", True):
        return False
    history = state.get("history", [])
    if not history:
        return False
    candidates = _tracking_candidates(state, bank)
    if not candidates:
        return False

    turn = state.get("turn", 0)
    latest = str(history[-1])
    earlier = "\n".join(str(h) for h in history[-3:-1])
    earlier_block = f"EARLIER NARRATION (context only):\n{earlier}\n\n" if earlier else ""
    player_action = str(state.get("last_input_text") or state.get("input_text") or "").strip()
    action_block = f"THE PLAYER'S ACTION THIS TURN:\n{player_action}\n\n" if player_action else ""
    records = "\n".join(_tracking_record(npc) for npc in candidates)

    prompt = f"""You maintain the character records for the NPCs of a text RPG. After each scene you check whether any of the tracked characters changed in a LASTING way and bring their records up to date.

TRACKED CHARACTERS (the only characters you may report on):
{records}

{earlier_block}{action_block}THIS TURN'S SCENE (check this for changes):
{latest}

Report changes ONLY in these areas:
- name: what the character is called (see NAME CHANGES below)
- appearance / physical condition: new scars, wounds, lost limbs, aging, a transformation, altered hair/eyes/skin, changed garb that defines their look
- personality: a lasting shift in temperament, outlook, values, or defining traits
- pitch: their concept or role in the story materially changed (a secret revealed, an allegiance switched, their story hook resolved or replaced)
- role: their narrative function clearly shifted
- status: the story shows them dying (deceased) or leaving the story for good (departed)
- notes: noteworthy things they did or revealed, appended to the running log

NAME CHANGES need no ceremony -- report one whenever:
- the story reveals the actual name of a character recorded under a descriptive epithet (e.g. "The Hooded Stranger" introduces herself as Veyra), OR
- the character adopts, accepts, or is granted a new name, alias, nickname, or title and answers to it, OR
- the scene consistently calls the character something other than the recorded name.

Do NOT report momentary emotions, temporary states (drunk, tired, briefly disguised), location changes, or characters who do not actually appear in this turn's scene. Only durable changes to who a character IS, how they LOOK, what they are CALLED, or where their story stands.

For each changed field return its NEW full value (rewrite the field in full, incorporating the change):
- "name": the new name only
- "appearance": full rewritten text; keep hair color and eye color stated (updated when the change is about them, otherwise carried over from the record)
- "personality": the full updated list of exactly 3 trait keywords
- "pitch": full rewritten text
- "role": one of {'|'.join(NPC_ROLES)}
- "status": active|departed|deceased
- "notes": the existing notes plus the new observations appended (keep it a compact running log)
Also return "change_note": one short sentence summarizing what changed for that character.

Respond with ONLY valid JSON:
{{"updates": [{{"npc_id": "id from the list above", "<changed field>": "new value", "change_note": "one short sentence"}}]}}
Return {{"updates": []}} if nothing durable changed."""

    try:
        raw = await sdk.llm.generate(prompt, model_preference="balanced")
    except Exception as e:
        print(f"[NPC System] Change-tracking pass failed: {e}")
        return False
    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return False
    updates = parsed.get("updates")
    if not isinstance(updates, list):
        return False

    by_id = {npc["id"]: npc for npc in candidates}
    changed_any = False
    for entry in updates:
        if not isinstance(entry, dict):
            continue
        ref = str(entry.get("npc_id", "") or "").strip()
        npc = by_id.get(ref)
        if npc is None:
            # Tracker models sometimes echo the character's name instead of
            # the bank id; accept it when it is unambiguous.
            matches = [n for n in candidates if _same_character_name(ref, n.get("name", ""))]
            npc = matches[0] if len(matches) == 1 else None
        if npc is None:
            continue

        fields = {k: v for k, v in entry.items() if k in UPDATE_FIELDS}
        # Story-driven passes may retire a character, never un-introduce one.
        if fields.get("status") == "unintroduced":
            del fields["status"]
        changes = _sanitize_edits(fields, npc)
        if "name" in changes and _rename_collides(changes["name"], npc, bank, state):
            del changes["name"]
        if not changes:
            continue

        old_name = str(npc.get("name", ""))
        note = str(entry.get("change_note", "")).strip()
        npc.update(changes)
        if "name" in changes:
            _finish_rename(npc, old_name)
        _log_change(npc, turn, note or f"Auto-tracked: {', '.join(changes)}", list(changes), "auto")
        await _refresh_profile_embedding(npc, list(changes), state, sdk)
        if note:
            await sdk.memory.remember(npc["id"], note, turn, importance=6)
        changed_any = True
        print(f"[NPC System] Tracked change to {npc.get('name', '?')}: {note or ', '.join(changes)}")

    return changed_any


async def on_librarian(state: dict, sdk) -> dict | None:
    config = _config(state)
    frequency = config.get("generator_frequency", DEFAULT_GENERATOR_FREQUENCY)
    max_pool = config.get("max_unintroduced_pool", DEFAULT_MAX_POOL)
    turn = state.get("turn", 0)

    if turn == 0:
        return None

    bank = _get_bank(state)

    if turn % frequency != 0:
        evolved = await _track_character_changes(state, bank, sdk)
        return _set_bank({}, bank) if evolved else None

    # Generator turn: change tracking, thread extraction, and the travel pass
    # are independent LLM calls touching disjoint record fields, so run them
    # concurrently.
    evolved, threads, traveled = await asyncio.gather(
        _track_character_changes(state, bank, sdk),
        _update_story_threads(state, sdk),
        _independent_travel_pass(state, bank, sdk),
    )
    bank_changed = evolved or traveled

    unintroduced_count = sum(
        1 for n in bank.values()
        if not n.get("introduced") and n.get("status") == "unintroduced"
    )

    if unintroduced_count >= max_pool:
        return _set_bank({"story_threads": threads}, bank) if (threads or bank_changed) else None

    scene = _scene_summary(state)
    bank_text = _bank_summary(bank)
    wctx = _world_context(state)
    region = state.get("player_location_region", "unknown")
    node_id = state.get("player_location_node_id", "")

    needed = min(3, max_pool - unintroduced_count)
    demand_driven = config.get("demand_driven_generation", True)

    plot_block = _plot_profile_block(state)
    plot_section = f"\n\n{plot_block}" if plot_block else ""
    plot_rule = ("\n9. Make these characters resonate with the STORY DIRECTION above -- fit its tone "
                 "and themes, lean into what the player enjoys, and steer clear of what they dislike."
                 if plot_block else "")

    if demand_driven:
        # Characters exist to serve the story, not to fill a pool: nothing is
        # created without a concrete, citable story need, and an empty reply
        # is the normal outcome.
        task = f"""You are a casting director for a text-based RPG. First decide whether the story ACTUALLY NEEDS any new character right now -- only then design new NPC concepts for the game.

A new character is justified ONLY when a concrete story signal calls for one: the story direction or one of its open questions points at a person who does not exist yet, an active story thread needs an actor no existing character can play, or where the story is clearly heading will obviously require someone new. If an EXISTING character (introduced or not) can serve the need, use them instead and create no one.

If nothing clearly calls for a new character, respond with {{"npcs": []}} -- that is the normal, expected outcome, not a failure."""
        rules = f"""INSTRUCTIONS (only for characters that pass the test above):
1. Create at most {needed} characters, and no more than the story clearly needs -- usually zero or one.
2. Every character carries a "need": one sentence naming the specific story signal (direction, open question, story thread, or scene) that demands them. A character without a concrete need must not be created.
3. Each character must have a DISTINCT archetype, personality, and role from all existing ones.
4. Bind a character to a location (current region: {region}) only when their need ties them there; otherwise make them encounter-type.
5. Characters should feel authentic to this world's genre, factions, and regions.
6. Pitches should be 2-3 sentences -- a hook that suggests story potential.
7. Personality should be exactly 3 keywords describing core traits.
8. Optionally relate 0-2 new characters to EXISTING characters above via "relationships", using their exact npc_id (e.g. ally, rival, family, mentor, rumored_enemy). Omit "relationships" or leave it empty if no natural connection exists.{plot_rule}"""
        need_field = '"need": "the story signal that demands this character, one sentence", '
    else:
        task = f"You are a character designer for a text-based RPG. Create {needed} new NPC concepts for the game."
        rules = f"""INSTRUCTIONS:
1. Create {needed} characters that fill gaps NOT covered by existing NPCs.
2. Each character must have a DISTINCT archetype, personality, and role from all existing ones.
3. At least one should be location-bound to the current region: {region}
4. The rest can be encounter-type (can appear anywhere).
5. Characters should feel authentic to this world's genre, factions, and regions.
6. Pitches should be 2-3 sentences -- a hook that suggests story potential.
7. Personality should be exactly 3 keywords describing core traits.
8. Optionally relate 0-2 new characters to EXISTING characters above via "relationships", using their exact npc_id (e.g. ally, rival, family, mentor, rumored_enemy). Omit "relationships" or leave it empty if no natural connection exists.{plot_rule}"""
        need_field = ""

    prompt = f"""{task}

WORLD CONTEXT:
{wctx}{plot_section}

CURRENT STORY STATE:
{scene}

EXISTING CHARACTERS (DO NOT duplicate or create similar concepts):
{bank_text}

{rules}

Respond with ONLY valid JSON:
{{"npcs": [{{{need_field}"name": "string", "race": "string", "gender": "male|female|nonbinary", "appearance": "1-2 sentence physical description that always states hair color and eye color (or the being's closest equivalent)", "archetype": "short archetype label", "pitch": "2-3 sentence character concept with story hook", "personality": ["trait1", "trait2", "trait3"], "role": "quest_giver|antagonist|ally|informant|rival|neutral|wildcard", "encounter_type": "location_bound|encounter", "location_node_id": "node_id or null", "location_region": "region name or null", "location_layer_id": "layer_id or null (only if encounter_type is location_bound)", "relationships": [{{"npc_id": "existing npc_id", "type": "ally|rival|family|mentor|rumored_enemy|...", "description": "short description of the connection"}}]}}]}}"""

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
        return _set_bank({"story_threads": threads}, bank) if (threads or bank_changed) else None

    new_npcs = parsed.get("npcs", [])
    if demand_driven:
        # No citable story need, no character -- drop anything the model
        # created out of habit, and cap at what was asked for.
        justified = [n for n in new_npcs
                     if isinstance(n, dict) and str(n.get("need", "")).strip()]
        dropped = len(new_npcs) - len(justified)
        if dropped:
            print(f"[NPC System] Dropped {dropped} generated NPC(s) without a stated story need.")
        new_npcs = justified[:needed]
    if not new_npcs:
        return _set_bank({"story_threads": threads}, bank) if (threads or bank_changed) else None

    for npc_data in new_npcs:
        npc_id, record = _build_npc_record(npc_data, turn, bank, source="generated")
        bank[npc_id] = record

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


def _log_change(npc: dict, turn: int, note: str, fields: list[str], source: str) -> None:
    log = list(npc.get("change_log", []))
    log.append({"turn": turn, "note": note, "fields": fields, "source": source})
    npc["change_log"] = log[-MAX_CHANGE_LOG:]


def _sanitize_edits(raw: dict, npc: dict) -> dict:
    """Whitelist, coerce, and validate browser-supplied field changes, keeping
    only fields whose value actually differs from the current record."""
    edits = {}
    for field in EDITABLE_FIELDS:
        if field not in raw:
            continue
        val = raw[field]
        if field == "personality":
            if isinstance(val, str):
                val = val.split(",")
            if not isinstance(val, list):
                continue
            val = [str(p).strip() for p in val if str(p).strip()]
        elif field == "role":
            if val not in NPC_ROLES:
                continue
        elif field == "status":
            if val not in NPC_STATUSES:
                continue
        else:
            if not isinstance(val, str):
                continue
            val = val.strip()
            if not val and field != "notes":
                continue
        if val != npc.get(field):
            edits[field] = val
    return edits


async def _update_npc_from_story(npc_id: str, state: dict, sdk) -> dict:
    """Player-requested refresh of one NPC's record: check the recent story for
    lasting changes to the character and rewrite only the fields that changed."""
    bank = _get_bank(state)
    npc = bank.get(npc_id)
    if not npc:
        return {"message": f"[NPC] Unknown character id: {npc_id}", "signal": "end_turn"}

    history = state.get("history", [])
    if not history:
        return {"message": "[NPC] There is no story yet to update from.", "signal": "end_turn"}

    story = "\n\n".join(str(h) for h in history)[-10000:]
    personality = ", ".join(npc.get("personality", []))

    prompt = f"""You maintain the character records for the NPCs of a text RPG. The player has asked you to bring one character's record up to date: check the recent story for LASTING changes to this character and rewrite only the fields that changed.

CHARACTER RECORD FOR: {npc.get('name', '')}
  Name: {npc.get('name', '')}
  Race: {npc.get('race', '') or '(not recorded)'}
  Gender: {npc.get('gender', '') or '(not recorded)'}
  Appearance: {npc.get('appearance', '') or '(not yet described)'}
  Archetype: {npc.get('archetype', '')}
  Role: {npc.get('role', 'neutral')}
  Status: {npc.get('status', 'active')}
  Personality: {personality or '(not yet described)'}
  Pitch: {npc.get('pitch', '') or '(none)'}
  Notes: {npc.get('notes', '') or '(none)'}

RECENT STORY (oldest to newest):
{story}

Report ONLY durable changes this character undergoes in the story above -- new injuries or looks, a new name or title, a lasting personality shift, a changed narrative role, death or departure, or noteworthy things they did. Ignore scenes that don't involve them and momentary emotions or states.

For each changed field return its NEW full value (rewrite the field in full, incorporating the change):
- "appearance": full rewritten text; keep hair color and eye color stated (updated when the change is about them, otherwise carried over from the record)
- "pitch": full rewritten text
- "personality": the full updated list of exactly 3 trait keywords
- "name": only if they are now called something else
- "role": one of {'|'.join(NPC_ROLES)} -- only if their narrative function clearly shifted
- "status": one of active|departed|deceased -- only if the story shows them dying or leaving the story
- "notes": the existing notes plus new observations appended (keep it a compact running log)
Also return "change_note": one short sentence summarizing what changed.

If nothing durable changed, return an empty object {{}}.

Respond with ONLY valid JSON containing just the changed fields (plus change_note)."""

    raw = await sdk.llm.generate(prompt, model_preference="balanced")
    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return {"message": "[NPC] The update pass returned nothing usable -- try again.", "signal": "end_turn"}

    fields = {k: v for k, v in parsed.items() if k in UPDATE_FIELDS}
    # Story-driven passes may retire a character, never un-introduce one.
    if fields.get("status") == "unintroduced":
        del fields["status"]
    changes = _sanitize_edits(fields, npc)
    if "name" in changes and _rename_collides(changes["name"], npc, bank, state):
        del changes["name"]
    if not changes:
        return {
            "message": f"[NPC] No lasting changes to {npc.get('name', npc_id)} found in the recent story.",
            "signal": "end_turn",
        }

    turn = state.get("turn", 0)
    change_note = str(parsed.get("change_note", "")).strip()
    old_name = str(npc.get("name", ""))
    npc.update(changes)
    if "name" in changes:
        _finish_rename(npc, old_name)
    _log_change(npc, turn, change_note or f"Story update: {', '.join(changes)}", list(changes), "story")
    await _refresh_profile_embedding(npc, list(changes), state, sdk)

    # Keep unmet characters out of RAG: only met characters get their change
    # note embedded, mirroring the profile-on-introduction rule.
    if change_note and npc.get("introduced"):
        await sdk.memory.remember(npc_id, change_note, turn, importance=6)

    print(f"[NPC System] {npc.get('name', npc_id)} updated from story: {', '.join(changes)}")

    lines = [f"[NPC] Updated {npc.get('name', npc_id)} from the recent story."]
    if change_note:
        lines.append(change_note)
    lines.append(f"Fields updated: {', '.join(changes)}")
    return {"message": "\n".join(lines), "signal": "end_turn", **_set_bank({}, bank)}


async def _activate_npc_manually(npc: dict, state: dict, sdk) -> None:
    """Player authority: (re)activating a character from the browser puts them
    in play and in the scene right now, mirroring what a story introduction
    sets. Idempotent, so a no-change re-save of an active character simply
    re-asserts their presence (and heals records from before this existed)."""
    turn = state.get("turn", 0)
    npc["introduced"] = True
    npc["status"] = "active"
    if npc.get("met_turn") is None:
        npc["met_turn"] = turn
    npc["last_interaction_turn"] = turn
    npc["presence_pinned_turn"] = turn
    if npc.get("encounter_type") == "encounter":
        npc["encounter_type"] = "location_bound"
        npc["location_node_id"] = state.get("player_location_node_id")
        npc["location_region"] = state.get("player_location_region")
        npc["location_layer_id"] = state.get("player_location_layer_id")
    if _config(state).get("embed_profiles", True):
        await _embed_profile(npc, turn, sdk)
    print(f"[NPC System] {npc.get('name')} manually activated at turn {turn}")


async def _apply_manual_edit(npc_id: str, payload: str, state: dict, sdk) -> dict:
    """Apply a browser edit. The payload is URL-encoded JSON (one whitespace-free
    token, so it survives the command dispatcher's text.split())."""
    bank = _get_bank(state)
    npc = bank.get(npc_id)
    if not npc:
        return {"message": f"[NPC] Unknown character id: {npc_id}", "signal": "end_turn"}

    try:
        raw = json.loads(urllib.parse.unquote(payload))
    except (json.JSONDecodeError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {"message": "[NPC] Could not parse the edit payload.", "signal": "end_turn"}

    edits = _sanitize_edits(raw, npc)
    if not edits:
        return {"message": f"[NPC] Nothing to change for {npc.get('name', npc_id)}.", "signal": "end_turn"}

    turn = state.get("turn", 0)
    npc.update(edits)
    _log_change(npc, turn, f"Manual edit: {', '.join(edits)}", list(edits), "manual")
    await _refresh_profile_embedding(npc, list(edits), state, sdk)

    if edits.get("status") == "active":
        # Switching to active is the player asserting this character is in
        # play, in the scene, NOW.
        await _activate_npc_manually(npc, state, sdk)
    elif edits.get("status") == "unintroduced":
        # Back into the hidden pool: unmet again until the story reintroduces them.
        npc["introduced"] = False
        npc["met_turn"] = None
        npc["traveling_with_player"] = False
        npc.pop("presence_pinned_turn", None)

    return {
        "message": f"[NPC] Updated {npc.get('name', npc_id)}: {', '.join(edits)}",
        "signal": "end_turn",
        **_set_bank({}, bank),
    }


def _coerce_personality(value) -> list[str]:
    """Normalize a personality field (comma string or list) to a trait list."""
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return [str(p).strip() for p in value if str(p).strip()]


async def _apply_manual_add(payload: str, state: dict, sdk) -> dict:
    """Create a brand-new character from browser-supplied fields. The payload is
    URL-encoded JSON (one whitespace-free token, matching the edit path).

    Manually added characters default to already-introduced (they show up in the
    known cast immediately); pass ``introduced: false`` to drop one into the
    unintroduced pool instead."""
    bank = _get_bank(state)

    try:
        raw = json.loads(urllib.parse.unquote(payload))
    except (json.JSONDecodeError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {"message": "[NPC] Could not parse the character payload.", "signal": "end_turn"}

    name = str(raw.get("name", "")).strip()
    if not name:
        return {"message": "[NPC] A new character needs a name.", "signal": "end_turn"}

    introduced = bool(raw.get("introduced", True))
    turn = state.get("turn", 0)
    location = (
        (state.get("player_location_node_id"),
         state.get("player_location_region"),
         state.get("player_location_layer_id"))
        if introduced else (None, None, None)
    )

    npc_data = {
        "name": name,
        "race": str(raw.get("race", "")).strip(),
        "gender": str(raw.get("gender", "")).strip(),
        "appearance": str(raw.get("appearance", "")).strip(),
        "archetype": str(raw.get("archetype", "")).strip(),
        "pitch": str(raw.get("pitch", "")).strip(),
        "personality": _coerce_personality(raw.get("personality", [])),
        "role": raw.get("role", "neutral"),
    }

    npc_id, record = _build_npc_record(
        npc_data, turn, bank, introduced=introduced, source="manual", location=location,
    )
    if introduced:
        # Player authority: a character added straight into the known cast is
        # standing in the scene, even before the story has mentioned them.
        record["presence_pinned_turn"] = turn
    notes = str(raw.get("notes", "")).strip()
    if notes:
        record["notes"] = notes
    _log_change(record, turn, f"Manually added {name}", ["name"], "manual")
    bank[npc_id] = record

    if introduced and _config(state).get("embed_profiles", True):
        await _embed_profile(record, turn, sdk)

    print(f"[NPC System] Manually added character {name} ({npc_id}) at turn {turn}")
    return {
        "message": f"[NPC] Added {name}.",
        "signal": "end_turn",
        "module_data_replace": ["characters"],
        **_set_bank({}, bank),
    }


async def _generate_random_character(state: dict, sdk, request: str = "") -> dict:
    """Generate a single random character via the LLM and drop it straight into
    the unintroduced pool, kept hidden until the player meets them in the story.
    Triggered by the browser's "Generate Character" button (/npc generate).

    ``request`` is an optional free-text brief from the player (e.g. "a grumpy
    dwarven blacksmith with a secret") that steers the design."""
    bank = _get_bank(state)
    turn = state.get("turn", 0)

    scene = _scene_summary(state)
    bank_text = _bank_summary(bank)
    wctx = _world_context(state)
    region = state.get("player_location_region", "unknown")

    request = (request or "").strip()[:MAX_GEN_REQUEST_CHARS]
    request_section = (
        f"\n\nPLAYER REQUEST (honor this as the primary brief for the character, while keeping "
        f"them consistent with the world's genre and tone):\n{request}" if request else ""
    )

    plot_block = _plot_profile_block(state)
    plot_section = f"\n\n{plot_block}" if plot_block else ""
    plot_rule = ("\n8. Make the character resonate with the STORY DIRECTION above -- fit its tone "
                 "and themes, lean into what the player enjoys, and steer clear of what they dislike."
                 if plot_block else "")

    prompt = f"""You are a character designer for a text-based RPG. Create 1 new NPC concept for the game.{request_section}

WORLD CONTEXT:
{wctx}{plot_section}

CURRENT STORY STATE:
{scene}

EXISTING CHARACTERS (DO NOT duplicate or create similar concepts):
{bank_text}

INSTRUCTIONS:
1. Create 1 character that fills a gap NOT covered by existing NPCs.
2. The character must have a DISTINCT archetype, personality, and role from all existing ones.
3. The character can be location-bound to the current region ({region}) or an encounter-type NPC that can appear anywhere.
4. The character should feel authentic to this world's genre, factions, and regions.
5. The pitch should be 2-3 sentences -- a hook that suggests story potential.
6. Personality should be exactly 3 keywords describing core traits.
7. Optionally relate the character to an EXISTING character above via "relationships", using their exact npc_id (e.g. ally, rival, family, mentor, rumored_enemy). Omit "relationships" or leave it empty if no natural connection exists.{plot_rule}

Respond with ONLY valid JSON:
{{"npc": {{"name": "string", "race": "string", "gender": "male|female|nonbinary", "appearance": "1-2 sentence physical description that always states hair color and eye color (or the being's closest equivalent)", "archetype": "short archetype label", "pitch": "2-3 sentence character concept with story hook", "personality": ["trait1", "trait2", "trait3"], "role": "quest_giver|antagonist|ally|informant|rival|neutral|wildcard", "encounter_type": "location_bound|encounter", "location_node_id": "node_id or null", "location_region": "region name or null", "location_layer_id": "layer_id or null (only if encounter_type is location_bound)", "relationships": [{{"npc_id": "existing npc_id", "type": "ally|rival|family|mentor|rumored_enemy|...", "description": "short description of the connection"}}]}}}}"""

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
        print(f"[NPC System] Generate-character command failed: {e}")
        return {"message": "[NPC] Could not generate a character right now. Try again.", "signal": "end_turn"}

    npc_data = parsed.get("npc")
    if not isinstance(npc_data, dict) or not str(npc_data.get("name", "")).strip():
        return {"message": "[NPC] The generator returned nothing usable. Try again.", "signal": "end_turn"}

    # Always hidden: force it into the unintroduced pool regardless of what the
    # LLM suggested, so the character stays a spoiler until met in the story.
    npc_id, record = _build_npc_record(npc_data, turn, bank, introduced=False, source="generated")
    name = record["name"]
    note = f"Generated {name}" + (f' (request: "{request}")' if request else "")
    _log_change(record, turn, note, ["name"], "generated")
    bank[npc_id] = record

    print(f"[NPC System] Generated hidden character {name} ({npc_id}) at turn {turn}")
    return {
        "message": f"[NPC] Generated {name} — hidden until you meet them in the story.",
        "signal": "end_turn",
        "module_data_replace": ["characters"],
        **_set_bank({}, bank),
    }


async def _delete_npc(npc_id: str, state: dict, sdk) -> dict:
    """Permanently remove a character from the bank, purge their stored memories
    (RAG profile included), and drop any relationship references the surviving
    characters held to them."""
    bank = _get_bank(state)
    npc = bank.get(npc_id)
    if not npc:
        return {"message": f"[NPC] Unknown character id: {npc_id}", "signal": "end_turn"}

    name = npc.get("name", npc_id)

    try:
        await sdk.memory.forget(npc_id)
    except Exception as e:
        print(f"[NPC System] Failed to purge memories for {npc_id}: {e}")

    del bank[npc_id]

    for other in bank.values():
        rels = other.get("relationships")
        if isinstance(rels, list) and any(r.get("npc_id") == npc_id for r in rels):
            other["relationships"] = [r for r in rels if r.get("npc_id") != npc_id]

    print(f"[NPC System] Deleted character {name} ({npc_id})")
    return {
        "message": f"[NPC] Deleted {name}.",
        "signal": "end_turn",
        "module_data_replace": ["characters"],
        **_set_bank({}, bank),
    }


async def on_command_npc(args: list[str], state: dict, sdk) -> dict:
    usage = ("[NPC] Usage: /npc generate | /npc add <data> | /npc update <npc_id> | "
             "/npc edit <npc_id> <data> | /npc delete <npc_id>")
    if not args:
        return {"message": usage, "signal": "end_turn"}

    sub = args[0].lower()
    if sub in ("generate", "gen", "random"):
        request = urllib.parse.unquote(" ".join(args[1:])).strip() if len(args) >= 2 else ""
        return await _generate_random_character(state, sdk, request)
    if sub in ("update", "refresh") and len(args) >= 2:
        return await _update_npc_from_story(args[1], state, sdk)
    if sub == "edit" and len(args) >= 3:
        return await _apply_manual_edit(args[1], " ".join(args[2:]), state, sdk)
    if sub == "add" and len(args) >= 2:
        return await _apply_manual_add(args[1], state, sdk)
    if sub in ("delete", "remove") and len(args) >= 2:
        return await _delete_npc(args[1], state, sdk)
    return {"message": usage, "signal": "end_turn"}
