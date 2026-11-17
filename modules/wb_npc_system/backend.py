"""NPC System -- background character generation, depth-focused casting, and introduction management."""
from copy import deepcopy
import asyncio
import json
import re
import urllib.parse
import uuid
from backend.engine import director_shared as _shared
from backend.engine import jev_tasks
from backend.engine.jev import Decision, fallback_generate
from backend.engine.narrative_voice import state_pronoun_grounding

MODULE_ID = "wb_npc_system"

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
MIN_GEN_INTERVAL_MINUTES = 30  # avoid periodic NPC generation mid-conversation on rapid 1-min turns
FIT_MAX_REJECTS = 1

# Distance-tier check cadence, keyed off wb_time_tracker's total_minutes_elapsed
# (turns vary 5-1440 in-world minutes each, so gating on minutes rather than
# turn count keeps travel paced to actual story time).
TRAVEL_CHECK_MINUTES = {"near": 60, "far": 360, "very_far": 1440}
TRAVEL_HOP_BUDGET = {"near": 1, "far": 3, "very_far": 1}
MOTIVATION_THRESHOLD = 0.3

# Shared non-blocking background runner (services injected at startup via set_services).
_bg_runner = _shared.ModuleBackgroundRunner(
    MODULE_ID,
    label="NPC System",
    default_data=lambda: {"characters": {}},
)


def set_services(services: dict) -> None:
    _bg_runner.set_services(services)


def _cancel_bg_task() -> None:
    _bg_runner.cancel()


def __getattr__(name: str):
    if name == "_services":
        return _bg_runner.services
    if name == "_bg_tasks":
        return _bg_runner.tasks
    if name == "_bg_epoch":
        return _bg_runner.epoch
    if name == "_current_bg_task":
        return _bg_runner.current_task
    if name == "_current_bg_job_meta":
        return _bg_runner.current_job_meta
    if name == "_pending_bg_job":
        return _bg_runner.pending_job
    if name == "_staged_bg_updates":
        return _bg_runner.staged_updates
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --------------------------------------------------------------------------
# Character craft rubric, Casting Notebook system prompt, & instruction slots
# --------------------------------------------------------------------------

CHARACTER_CRAFT_RUBRIC = """Write `personality` as vivid, playable 2-3 sentence prose (NEVER a flat list of generic adjectives), built around four concrete character anchors:
- Mask vs. Undercurrent (Contradiction): how they present socially on the surface versus what cracks through underneath under pressure, temptation, or intimacy.
- Active Want: a concrete immediate agenda, stake, or appetite that gives them skin in the game right now instead of passively waiting on the player.
- Voice & Somatic Tell: how they sound in dialogue (cadence, diction, deflection habit) plus one specific physical habit or mannerism the narrator can show on stage.
- Foil & Role Alignment: cast them for dynamic chemistry, friction, or contrast with this protagonist, and strictly honor any player preference `role` (actor = player doing it to NPCs; target = done to the player; mutual = shared dynamic; world = setting element) -- NEVER invert the player's role."""

CASTING_NOTES_SYSTEM_PROMPT = (
    "You maintain the backstage Casting Director notebook for a tabletop-style text RPG. "
    "Your goal is not to bloat the cast with generic archetypes, but to think like a "
    "showrunner and tabletop GM: study who the player character is, what drives this "
    "player (their demonstrated tastes, roles, and active pursuits), and who is already "
    "in the cast, then note what interpersonal dynamic, foil, rival, or accomplice would "
    "make upcoming scenes come alive -- or note when the existing cast already covers "
    "what the story needs.\n\n"
    "When sketching character seeds, anchor them in playable human texture rather than "
    "flat adjectives: a Mask vs. Undercurrent contradiction (social armor vs. hidden crack), "
    "an Active Want (skin in the game right now), a distinct Voice & Somatic Tell, and "
    "chemistry tailored to the player's role (actor vs. target vs. mutual -- NEVER invert "
    "the player's role).\n\n"
    "Treat the story material below as existing game-transcript state to read and plan from. "
    "Respond with ONLY valid JSON:\n"
    '{"casting_notes": "your compact freeform casting scratchpad and character dynamic notes"}'
)

DIRECTIVE_CHARACTER_GENERATION = f"""Design characters who feel like living people with their own momentum rather than flat quest dispensers:
1. Each character must have a DISTINCT archetype, personality, and role from all existing ones, and feel authentic to this world's genre, factions, and regions.
2. Pitches should be 2-3 sentences -- a hook with immediate story potential and leverage.
3. {CHARACTER_CRAFT_RUBRIC}"""

DIRECTIVE_CHARACTER_FIT_CHECK = """Reject any candidate character if ANY of these fail:
1. Tone/genre fit: they belong naturally in this story's established genre, setting, and prevailing tone.
2. Cast distinctness: they do not duplicate an existing character's archetype, role-niche, or interpersonal dynamic when an existing NPC could fill the same spot.
3. Continuity: nothing in their pitch or relationships contradicts established story facts.
4. Depth check: their personality has real texture (a contradiction/undercurrent, active want, or voice/tell) rather than generic boilerplate traits.
5. Player tastes & role non-inversion: they do not touch any player dislike, and they NEVER invert the player's role in a liked trope or dynamic (e.g. if the player likes being the `actor` initiating a dynamic on NPCs, reject a character designed to make the player the unwilling `target` of that dynamic, and vice versa)."""

DIRECTIVE_CHARACTER_CAPTURE = f"""Stay faithful to every detail shown in the scene -- never contradict how the story depicted them, and infer plausible, grounded details where the scene was silent.
{CHARACTER_CRAFT_RUBRIC}"""

DIRECTIVE_CHARACTER_INTRODUCTION = """- Only introduce a character if the scene naturally calls for one (player enters a populated area, seeks information, encounters travelers, pursues a hook tied to them, etc.).
- Do NOT introduce anyone during intimate, romantic, erotic, emotional, heart-to-heart, or private conversations/moments -- let interpersonal beats breathe uninterrupted.
- Do NOT introduce anyone if the player is alone in isolated wilderness, mid-combat, or the scene is self-contained.
- If introducing, pick the character that best fits the scene's tone, timing, and needs, preferring location-bound NPCs over encounter NPCs when at their specific location.
- Never introduce an NPC in the very first turn (turn 0), and never re-introduce an NPC who is already introduced."""

DIRECTIVE_CHARACTER_TRACKING = """Report changes ONLY in these areas:
- name: what the character is called (revealed true names of epithet records, adopted aliases/titles, or a new name the story consistently uses)
- appearance / physical condition: new scars, wounds, lost limbs, aging, a transformation, altered hair/eyes/skin, changed garb that defines their look
- personality: a lasting shift in temperament, mask/undercurrent, active want, demeanor, or voice/mannerisms (rewritten as 2-3 sentence prose preserving unchanged core traits)
- pitch: their concept or role in the story materially changed (a secret revealed, an allegiance switched, their story hook resolved or replaced)
- role: their narrative function clearly shifted
- status: the story shows them dying (deceased) or leaving the story for good (departed)
- notes: noteworthy things they did or revealed, appended to the running log
Do NOT report momentary emotions, temporary states (drunk, tired, briefly disguised), location changes, or characters who do not actually appear in this turn's scene."""

INSTRUCTION_SLOTS = [
    {
        "id": "character_generation",
        "label": "Character Generation",
        "description": "How new NPCs are cast and written: depth anchors, voice, foil chemistry, and role alignment.",
        "default": DIRECTIVE_CHARACTER_GENERATION,
    },
    {
        "id": "character_fit_check",
        "label": "Character Quality Gate",
        "description": "The criteria a newly generated NPC must pass before entering the character bank.",
        "default": DIRECTIVE_CHARACTER_FIT_CHECK,
    },
    {
        "id": "character_capture",
        "label": "Story Character Capture",
        "description": "How characters introduced directly by the storyteller are profiled into full records.",
        "default": DIRECTIVE_CHARACTER_CAPTURE,
    },
    {
        "id": "character_introduction",
        "label": "Character Introduction Pacing",
        "description": "When the Introduction Director brings a waiting character onstage vs. holding back.",
        "default": DIRECTIVE_CHARACTER_INTRODUCTION,
    },
    {
        "id": "character_tracking",
        "label": "Character Change Tracking",
        "description": "What counts as a lasting change when updating NPC records after scenes.",
        "default": DIRECTIVE_CHARACTER_TRACKING,
    },
]

_SLOT_DEFAULTS = {slot["id"]: slot["default"] for slot in INSTRUCTION_SLOTS}


def get_instruction_slots() -> list[dict]:
    """Generic module contract: the customizable instruction slots this module
    exposes to the UI and scenario overrides."""
    return [dict(slot) for slot in INSTRUCTION_SLOTS]


def _directive(slot_id: str, instructions: dict | None) -> str:
    text = str((instructions or {}).get(slot_id) or "").strip()
    return text or _SLOT_DEFAULTS[slot_id]


def _config(state: dict) -> dict:
    return state.get("module_configs", {}).get(MODULE_ID, {})


def _own_data(state: dict) -> dict:
    return state.get("module_data", {}).get(MODULE_ID, {})


def _parse_json_block(raw: str):
    return _shared.parse_json_block(raw)


async def _llm_generate(sdk, prompt: str, model_preference: str = "balanced",
                        system_prompt: str | None = None) -> str:
    return await _shared.llm_generate(
        sdk, MODULE_ID, prompt, model_preference=model_preference, system_prompt=system_prompt
    )


def _normalize_personality(value) -> str:
    """Normalize an NPC personality (prose string or legacy trait list) into a
    clean prose string."""
    if isinstance(value, list):
        return ", ".join(str(p).strip() for p in value if str(p).strip())
    if isinstance(value, str):
        return value.strip()
    return ""


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
    personality = _normalize_personality(npc.get("personality", ""))
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
    the schema in one place. ``location`` is (node_id, region, map_id)."""
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

    node_id, region, map_id = location
    return npc_id, {
        "id": npc_id,
        "name": npc_data.get("name", "Unknown"),
        "race": npc_data.get("race", ""),
        "gender": npc_data.get("gender", ""),
        "appearance": npc_data.get("appearance", ""),
        "archetype": npc_data.get("archetype", ""),
        "pitch": npc_data.get("pitch", ""),
        "personality": _normalize_personality(npc_data.get("personality", "")),
        "role": role,
        "encounter_type": "location_bound" if introduced else npc_data.get("encounter_type", "encounter"),
        "location_node_id": node_id if introduced else npc_data.get("location_node_id"),
        "location_region": region if introduced else npc_data.get("location_region"),
        "location_map_id": map_id if introduced
        else npc_data.get("location_map_id") or npc_data.get("location_layer_id"),
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
    world_data = state.get("world_data") or {}
    maps = world_data.get("maps")
    maps = maps if isinstance(maps, dict) else None
    # Reconcile records whose status was manually flipped to active before the
    # introduction sync existed: an active character is by definition known.
    # Healing here repairs every stale save the first time any hook or command
    # touches the bank, and the fix persists with the next state save.
    for npc in bank.values():
        if not isinstance(npc, dict):
            continue
        if isinstance(npc.get("personality"), list):
            npc["personality"] = _normalize_personality(npc["personality"])
        if npc.get("status") == "active" and not npc.get("introduced"):
            npc["introduced"] = True
            if npc.get("met_turn") is None:
                npc["met_turn"] = state.get("turn", 0)
        # world_format 2: NPCs live on maps, not layers. Lazily rename
        # location_layer_id -> location_map_id. Migrated map ids equal the old
        # layer ids, except layers that vanished in migration (e.g. the primary
        # layer, which became the root map) -- resolve those via each map's
        # legacy_layer_id, falling back to the root map.
        if "location_layer_id" in npc:
            old = npc.pop("location_layer_id")
            if not npc.get("location_map_id"):
                if maps is not None and old and old not in maps:
                    old = next((mid for mid, m in maps.items()
                                if m.get("legacy_layer_id") == old),
                               world_data.get("root_map_id", "root"))
                npc["location_map_id"] = old
    return bank


def _set_bank(overrides: dict, npcs: dict[str, dict]) -> dict:
    payload = dict(overrides)
    payload["characters"] = npcs
    return {"module_data": {"wb_npc_system": payload}}


def _player_map_id(state: dict) -> str | None:
    """The map the player is on (world_format 2), falling back to the legacy
    layer key for un-migrated sessions."""
    return state.get("player_location_map_id") or state.get("player_location_layer_id")


def _filter_candidates(state: dict) -> list[dict]:
    bank = _get_bank(state)
    node_id = state.get("player_location_node_id", "")
    region = state.get("player_location_region", "")
    map_id = _player_map_id(state)

    candidates = []
    for npc in bank.values():
        if npc.get("introduced") or npc.get("status") != "unintroduced":
            continue

        etype = npc.get("encounter_type", "encounter")
        if etype == "encounter":
            candidates.append(npc)
        elif etype == "location_bound":
            npc_map = npc.get("location_map_id")
            if npc_map and map_id and npc_map != map_id:
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
        state_pronoun_grounding(state),
    ]
    clock = _shared.get_time_clock(state)
    if clock is not None:
        parts.append(f"<in_world_time>\nCurrent in-world date and time: {_shared.format_clock_text(clock)}.\n</in_world_time>")

    if recent:
        parts.append("Recent events:")
        for i, h in enumerate(recent):
            parts.append(f"  [{i + 1}] {h}")

    recent_inputs = _shared.recent_player_inputs(state)
    if recent_inputs:
        parts.append("Recent player actions:")
        for inp in recent_inputs:
            parts.append(f"  - {inp}")

    threads = _shared.plot_threads_for_npc(state, _get_bank(state))
    if threads is None:
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
        personality = _normalize_personality(npc.get("personality", ""))
        pers_line = f"\n    Personality: {personality}" if personality else ""
        lines.append(
            f"  - [{npc_id}] {npc.get('name')} -- {npc.get('archetype')} "
            f"({npc.get('role', 'neutral')}, {npc.get('encounter_type', 'encounter')}) {status}\n"
            f"    Pitch: {npc.get('pitch', '')}{pers_line}"
        )
        rels = npc.get("relationships", [])
        if rels:
            rel_text = ", ".join(f"{r.get('type', '?')}:{r.get('npc_id', '?')}" for r in rels)
            lines.append(f"    Relationships: {rel_text}")
    return "\n".join(lines)


def _world_context(state: dict) -> str:
    parts = list(_shared.setting_material(state))
    world_data = state.get("world_data", {})
    if isinstance(world_data, dict) and world_data:
        regions = world_data.get("regions", {}).get("regions", [])
        if regions:
            reg_lines = ["Regions:"]
            for r in regions:
                reg_lines.append(f"  - {r.get('name', 'unknown')}: terrain={r.get('terrain', '?')}, climate={r.get('climate', '?')}")
            factions = world_data.get("regions", {}).get("factions", [])
            if factions:
                reg_lines.append(f"  Factions: {', '.join(factions)}")
            parts.append("\n".join(reg_lines))

    protagonist = _shared.protagonist_context_block(state)
    if protagonist:
        parts.append(protagonist)

    return "\n\n".join(parts)


def _plot_profile_block(state: dict) -> str:
    """Borrow Plot Director's learned player profile (including role-explicit
    likes/avoids/dislikes), narrative direction, active thread, and GM notes."""
    return _shared.format_plot_casting_block(state)


def _casting_notes_block(npc_mod_data: dict) -> str:
    notes = str((npc_mod_data or {}).get("casting_notes") or "").strip()
    if not notes:
        return ""
    return f"CASTING NOTEBOOK (your backstage character dynamic and foil notes):\n{notes}"


def _casting_iterations(config: dict) -> int:
    return _shared.planning_iterations(config, "casting_iterations", _bg_runner.services)


async def _iterate_casting_notes(state: dict, sdk, own_data: dict, bank: dict, passes: int = 1) -> str:
    """Run 1-2 lightweight Casting Director notebook passes to brainstorm what
    interpersonal dynamic or foil the cast needs (or confirm the existing cast suffices)."""
    config = _config(state)
    notes = str((own_data or {}).get("casting_notes") or "").strip()
    model_pref = config.get("casting_ai_model", "smartest")
    wctx = _world_context(state)
    plot_block = _plot_profile_block(state)
    scene = _scene_summary(state)
    bank_text = _bank_summary(bank)

    for _ in range(max(0, int(passes))):
        notes_section = f"\n\n{_casting_notes_block({'casting_notes': notes})}" if notes else ""
        plot_section = f"\n\n{plot_block}" if plot_block else ""
        prompt = f"""WORLD & PROTAGONIST CONTEXT:
{wctx}{plot_section}{notes_section}

CURRENT STORY STATE:
{scene}

EXISTING CAST:
{bank_text}

Write or refine your working Casting Director notes for this story right now.

Respond with ONLY valid JSON:
{{"casting_notes": "your compact freeform casting scratchpad and character dynamic notes"}}"""
        raw = await _llm_generate(
            sdk, prompt, model_preference=model_pref, system_prompt=CASTING_NOTES_SYSTEM_PROMPT
        )
        parsed = _parse_json_block(raw)
        if isinstance(parsed, dict) and str(parsed.get("casting_notes") or "").strip():
            notes = str(parsed.get("casting_notes")).strip()
    return notes


async def _fit_check_npcs(state: dict, sdk, candidates: list[dict], bank: dict) -> tuple[list[dict], str]:
    """Quality gate vetting generated NPCs against genre/tone fit, cast redundancy,
    continuity, depth, player dislikes, and player role non-inversion. Fails open
    on malformed/unparseable replies so transient LLM hiccups never lose valid work."""
    if not candidates:
        return [], ""
    config = _config(state)
    if not config.get("fit_check_enabled", True):
        return candidates, ""

    wctx = _world_context(state)
    plot_block = _plot_profile_block(state)
    plot_section = f"\n\n{plot_block}" if plot_block else ""
    scene = _scene_summary(state)
    bank_text = _bank_summary(bank)
    cand_json = json.dumps(candidates, ensure_ascii=False)

    prompt = f"""You are a continuity and casting editor for a text-based RPG. Review the candidate NPC(s) below before they enter the game's character pool.

WORLD & PROTAGONIST CONTEXT:
{wctx}{plot_section}

CURRENT STORY STATE:
{scene}

EXISTING CAST:
{bank_text}

CANDIDATE CHARACTERS TO VET:
{cand_json}

{_directive("character_fit_check", state.get("module_instructions"))}

Respond with ONLY valid JSON:
{{"verdict": "accept|reject", "rejected_names": ["exact candidate name if rejecting"], "critique": "one concise sentence explaining what failed, or empty if accepted"}}"""

    try:
        model_pref = config.get("tracking_ai_model", "balanced")
        raw = await _llm_generate(sdk, prompt, model_preference=model_pref)
        parsed = _parse_json_block(raw)
    except Exception as e:
        print(f"[NPC System] Fit-check failed open: {e}")
        return candidates, ""

    if not isinstance(parsed, dict):
        return candidates, ""
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict != "reject":
        return candidates, ""

    critique = str(parsed.get("critique") or "").strip()
    raw_rejected = parsed.get("rejected_names")
    rejected_lower = {
        str(n).strip().lower()
        for n in (raw_rejected if isinstance(raw_rejected, list) else [])
        if str(n).strip()
    }
    if rejected_lower:
        kept = [
            c for c in candidates
            if str(c.get("name") or "").strip().lower() not in rejected_lower
        ]
        if len(kept) < len(candidates):
            return kept, critique
    return [], critique


async def _update_story_threads(state: dict, sdk) -> list[dict]:
    """Ask the LLM to extract active story threads from recent history, tagged with
    which existing NPCs (if any) each thread involves. Used only as a fallback when
    Plot Director is absent or has no active thread/direction yet."""
    history = state.get("history", [])
    if not history:
        return []

    bank = _get_bank(state)
    recent = "\n".join(str(h) for h in history[-5:])
    bank_text = _bank_summary(bank)
    voice_note = state_pronoun_grounding(state)
    prompt = f"""Extract 3-5 active story threads from this RPG narrative. These are ongoing plotlines, goals, or tensions.

{voice_note}

Narrative:
{recent}

EXISTING CHARACTERS:
{bank_text}

For each thread, note which existing characters (by exact npc_id from the list above) are involved, if any.
Keep each thread description under 10 words.

Respond with ONLY a JSON array:
[{{"text": "short thread description", "npc_ids": ["npc_xxxxxxxx"]}}, ...]"""

    try:
        result = await _llm_generate(sdk, prompt, model_preference="fastest")
        parsed = _parse_json_block(result)
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
    selected, decision = await jev_tasks.npc_ids(sdk, state, candidates, "presence")
    if selected is not None:
        return selected
    scene = "\n".join(str(h) for h in state.get("history", [])[-RECENT_STORY_ENTRIES:])
    listing = "\n".join(
        f"- {npc['id']} | {npc.get('name', '?')} ({npc.get('archetype', '')}): {npc.get('pitch', '')}"
        for npc in candidates
    )
    prompt = f"""You track which characters are on stage in a text RPG.

{state_pronoun_grounding(state)}

RECENT STORY (oldest to newest):
{scene}

KNOWN CHARACTERS:
{listing}

Which of these characters are PHYSICALLY PRESENT in the current scene -- actually there with the player right now? Being mentioned, remembered, or discussed does not count as present.

Respond with ONLY a JSON array of the present character ids (may be empty):
["npc_xxxxxxxx", ...]"""

    try:
        result = await fallback_generate(sdk, decision, prompt, "fastest")
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
    p_map = _player_map_id(state)
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
            node, region, npc_map = _npc_effective_location(npc, state)
            map_ok = not (npc_map and p_map and npc_map != p_map)
            located_here = map_ok and (
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
            lines.append(f"  Notes: {notes}")
    return "\n".join(lines)


def _state_with_staged_overlay(state: dict) -> dict:
    """When a background pass finished while a new turn started, overlay its
    staged NPC updates onto a shallow state view for context gathering."""
    if _bg_runner.staged_updates is None:
        return state
    overlaid_mod = _bg_runner.overlay_staged(state, _own_data(state))
    if overlaid_mod is _own_data(state):
        return state
    view = dict(state)
    view["module_data"] = {**(state.get("module_data") or {}), MODULE_ID: overlaid_mod}
    return view


async def on_gather_context(state: dict, sdk) -> dict | None:
    _bg_runner.reconcile_on_gather(state)
    effective_state = _state_with_staged_overlay(state)
    result = {}

    # Scene presence (active characters) and the introduction pass
    # (unintroduced candidates) look at disjoint parts of the bank, so their
    # LLM calls run concurrently instead of back-to-back.
    present, introduction = await asyncio.gather(
        _present_npcs(effective_state, sdk),
        _introduction_pass(effective_state, sdk),
    )

    context = _present_characters_context(effective_state, present)
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

    if state.get("turn", 0) == 0 or not config.get("introduction_enabled", True):
        return None

    # Stand down whenever Plot Director is holding back external world momentum
    # (intimate/emotional withhold, deferred mid-climax hold, or between-thread quiet period).
    if _shared.is_plot_holding(state):
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
        personality = _normalize_personality(npc.get("personality", ""))
        candidate_text += (
            f"[{i}] ID: {npc['id']} | {npc['name']} ({npc.get('race', '?')}, {npc.get('gender', '?')})\n"
            f"    Archetype: {npc.get('archetype', '')}\n"
            f"    Role: {npc.get('role', 'neutral')}\n"
            f"    Pitch: {npc.get('pitch', '')}\n"
            f"{need_line}"
            f"    Personality: {personality}\n"
            f"    Type: {npc.get('encounter_type', 'encounter')}\n\n"
        )

    prompt = f"""You are a narrative director. Given the current scene, decide if a new character should be introduced.

SCENE:
{scene}

AVAILABLE CHARACTERS (unintroduced, in this location):
{candidate_text}

RULES:
{_directive("character_introduction", state.get("module_instructions"))}

Respond with ONLY valid JSON:
{{"introduce": true/false, "npc_id": "id or null", "reason": "one sentence why/why not"}}"""

    decision, decision_call = await jev_tasks.introduce(sdk, state, candidates, prompt)
    try:
        result = (json.dumps(decision) if decision is not None else
                  await fallback_generate(sdk, decision_call, prompt, "fastest"))
        decision = _parse_json_block(result)
        if not isinstance(decision, dict):
            return None
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

    effective_state = _state_with_staged_overlay(state)
    bank = _get_bank(effective_state)
    npc_data = effective_state.get("module_data", {}).get("wb_npc_system", {})
    pending_id = npc_data.get("pending_introduction")
    reason = npc_data.get("introduction_reason", "")

    if not pending_id or pending_id not in bank:
        return None

    npc = bank[pending_id]
    personality = _normalize_personality(npc.get("personality", ""))

    past = await sdk.memory.recall(pending_id, limit=2)
    past_section = ""
    if past:
        past_lines = "\n".join(f"  - {m.get('text', '')}" for m in past)
        past_section = f"\nPast interactions:\n{past_lines}\n"

    content = f"""<npc_introduction>
A prepared character has a natural opening to enter this scene. Follow the player's lead first: weave them in organically where the player's action gives room, never overriding what the player is doing or intruding on a private moment.

Character to introduce:
  Name: {npc.get('name')}
  Race: {npc.get('race')}
  Gender: {npc.get('gender')}
  Appearance: {npc.get('appearance')}
  Archetype: {npc.get('archetype')}
  Personality: {personality}
  Narrative Role: {npc.get('role', 'neutral')}
  Character Pitch: {npc.get('pitch')}

Why they fit this moment: {reason}
{past_section}
How to bring them on stage:
- Make their entrance feel unhurried and organic to the setting and current action
- Show their personality, voice, physical mannerisms, and immediate agenda through action and dialogue rather than exposition dumps
- Mention their name ({npc.get('name')}) naturally when they introduce themselves or someone addresses them (if their name would not naturally surface yet on first sight, make their look and demeanor unmistakably match this profile)
</npc_introduction>"""

    return {"content": content}


def _npc_effective_location(npc: dict, state: dict) -> tuple:
    """Where this NPC actually is right now -- the player's location while
    traveling with them, otherwise their own stored location fields."""
    if npc.get("traveling_with_player"):
        return (
            state.get("player_location_node_id"),
            state.get("player_location_region"),
            _player_map_id(state),
        )
    return (npc.get("location_node_id"), npc.get("location_region"), npc.get("location_map_id"))


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

    known_parts = []
    for n in sorted(bank.values(), key=lambda c: str(c.get("name", "")).lower()):
        raw_name = str(n.get("name", "")).strip()
        if not raw_name:
            continue
        cues = [str(n.get(k, "")).strip() for k in ("race", "gender", "archetype") if str(n.get(k, "")).strip()]
        cue_str = f" ({'/'.join(cues)})" if cues else ""
        known_parts.append(f"{raw_name.lower()}{cue_str}")
    player = _player_name(state)
    if player and player.lower() not in {str(n.get("name", "")).strip().lower() for n in bank.values()}:
        known_parts.append(player.lower())
    known_list = ", ".join(known_parts) if known_parts else "(none yet)"
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
        "them by identity, not exact wording. Never report an already-known character "
        "who physically transformed, shapeshifted, changed outfit or form, had their "
        "mind or persona altered, or is referred to by a pet name or new descriptive "
        "epithet in this scene -- they are still the same character, not a new one. "
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
        npc["location_map_id"] = _player_map_id(state)

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
    for field in ("appearance", "personality", "pitch", "notes"):
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

    scene = "\n\n".join(str(h) for h in state.get("history", [])[-3:])
    listing = "\n".join(
        f"- {p['name']}: {p['descriptor']}" + (f" (in the scene: {p['evidence']})" if p["evidence"] else "")
        for p in pending
    )

    roster_section = ""
    dedup_rule = ""
    if bank:
        roster_lines = []
        for npc in bank.values():
            ident = " / ".join(
                str(npc.get(k, "")).strip()
                for k in ("race", "gender", "archetype")
                if str(npc.get(k, "")).strip()
            )
            head = f"- {npc['id']} | {npc.get('name', '?')}" + (f" ({ident})" if ident else "")
            details = [f"Pitch: {npc.get('pitch', '')}"] if str(npc.get("pitch", "")).strip() else []
            if str(npc.get("appearance", "")).strip():
                details.append(f"Appearance: {npc['appearance']}")
            if str(npc.get("notes", "")).strip():
                details.append(f"Notes: {npc['notes']}")
            roster_lines.append(head + (": " + " | ".join(details) if details else ""))
        roster = "\n".join(roster_lines)
        roster_section = f"""

CHARACTERS ALREADY ON RECORD (for identity checks -- these are already tracked):
{roster}"""
        dedup_rule = ("\n\nIMPORTANT -- no duplicates: compare each character to profile against the "
                      "characters already on record. If one of them is actually an existing character "
                      "appearing under a different name, spelling, title, pet name, or epithet -- "
                      "including an existing character who physically transformed, shapeshifted, "
                      "changed form/species/outfit, disguised themselves, or had their mind or "
                      "persona altered/rewritten in the scene (or whose real name was finally "
                      "revealed) -- do NOT build a new profile for them. Instead return "
                      '{"existing_npc_id": "<their npc_id from the record list>", "name": "<the name '
                      'the story now uses>"} for that character. Only profile genuinely new, separate individuals.')

    wctx = _world_context(state)
    plot_block = _plot_profile_block(state)
    context_extras = "\n\n".join(b for b in (wctx, plot_block) if b)
    context_section = f"\n\nWORLD & STORY CONTEXT:\n{context_extras}" if context_extras else ""

    prompt = f"""You are a character designer for a text RPG. The story just introduced the following characters on its own. Build a full character record for EACH one, staying faithful to how they appear in the scene — do not contradict it, and infer sensible details where the scene is silent.{context_section}

SCENE:
{scene}

CHARACTERS TO PROFILE (use these exact names):
{listing}{roster_section}

Some of these names may be descriptive epithets for characters the story has not named yet (e.g. 'The Hooded Stranger'). Keep such an epithet as the name exactly as given -- do NOT invent a real name for them; the record is renamed once the story reveals one.{dedup_rule}

{_directive("character_capture", state.get("module_instructions"))}

For each character return:
- name (exactly as given above)
- race, gender (infer from the scene; use "unknown" if truly unclear)
- appearance: 1-2 sentence physical description grounded in the scene; ALWAYS state hair color and eye color (or the being's closest equivalent -- fur, scales, glow) and their visual age (how old they look, e.g. "appears mid-twenties", "elderly"), inferring plausible ones where the scene is silent
- archetype: short label
- pitch: 2-3 sentence concept with a story hook
- personality: 2-3 sentence prose capturing their outward mask vs. undercurrent contradiction, immediate active want, and voice/physical tell
- role: one of quest_giver|antagonist|ally|informant|rival|neutral|wildcard

Respond with ONLY valid JSON:
{{"npcs": [{{"name": "string", "race": "string", "gender": "male|female|nonbinary|unknown", "appearance": "string", "archetype": "string", "pitch": "string", "personality": "2-3 sentence prose personality", "role": "string"}}]}}"""

    try:
        model_pref = _config(state).get("tracking_ai_model", "balanced")
        result = await _llm_generate(sdk, prompt, model_preference=model_pref)
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
        _player_map_id(state),
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
            npc["location_map_id"] = _player_map_id(state)

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
    """Node adjacency for one map (world_format 2) or legacy layer, built from
    the plain world_data dict a module receives (mirrors the engine's internal
    graph builder, but modules can't import engine code). ``layer_id`` is a map
    id on migrated data."""
    maps = world_data.get("maps")
    if isinstance(maps, dict):
        edges = []
        for map_id, map_rec in maps.items():
            if layer_id and map_id != layer_id:
                continue
            edges.extend(map_rec.get("edges", []))
    elif world_data.get("map_layers"):
        edges = []
        for layer in world_data["map_layers"]:
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
    maps = world_data.get("maps")
    nodes = []
    if isinstance(maps, dict):
        for map_id, map_rec in maps.items():
            if layer_id and map_id != layer_id:
                continue
            nodes.extend(map_rec.get("nodes", []))
    elif world_data.get("map_layers"):
        for layer in world_data["map_layers"]:
            if layer_id and layer.get("layer_id") != layer_id:
                continue
            nodes.extend(layer.get("map", {}).get("nodes", []))
    else:
        nodes = world_data.get("map", {}).get("nodes", [])
    return {n["id"]: n for n in nodes if n.get("id")}


def _region_layer_id(world_data: dict, region_name: str | None) -> str | None:
    """The map/layer a region's records claim to live on. world_format 2 keeps
    no region->map mapping, so stale legacy layer ids resolve through each
    map's legacy_layer_id (None when nothing matches, never a crash)."""
    if not region_name:
        return None
    for r in world_data.get("regions", {}).get("regions", []):
        if r.get("name") == region_name:
            lid = r.get("layer_id")
            maps = world_data.get("maps")
            if lid and isinstance(maps, dict) and lid not in maps:
                return next((mid for mid, m in maps.items()
                             if m.get("legacy_layer_id") == lid), None)
            return lid
    return None


def _distance_tier(npc: dict, state: dict) -> str:
    world_data = state.get("world_data", {}) or {}
    p_node = state.get("player_location_node_id")
    p_region = state.get("player_location_region")
    p_map = _player_map_id(state)

    npc_map = npc.get("location_map_id") or _region_layer_id(world_data, npc.get("location_region"))

    if npc_map and p_map and npc_map != p_map:
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
    selected, decision = await jev_tasks.npc_ids(sdk, state, eligible, "motivation")
    if selected is not None:
        return selected
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
        result = await fallback_generate(sdk, decision, prompt, "fastest")
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
    p_map = _player_map_id(state)

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
        npc_map = npc.get("location_map_id") or _region_layer_id(world_data, npc.get("location_region"))

        if tier == "very_far":
            if not motivated or not npc_map or not p_map:
                continue
            if isinstance(world_data.get("maps"), dict):
                # world_format 2: pick a connection out of the NPC's map
                # (either direction when bidirectional), preferring one whose
                # far side is the player's map. NPCs ignore requirements and
                # travel turns -- they teleport through, as before.
                candidates = []
                for c in world_data.get("connections", []):
                    near, far = c.get("from") or {}, c.get("to") or {}
                    if near.get("map_id") == npc_map:
                        candidates.append((near, far))
                    elif c.get("bidirectional") and far.get("map_id") == npc_map:
                        candidates.append((far, near))
                if not candidates:
                    continue
                direct = [pair for pair in candidates if pair[1].get("map_id") == p_map]
                near, far = direct[0] if direct else candidates[0]
                exit_node = near.get("node_id")
                far_map, far_node = far.get("map_id"), far.get("node_id")
            else:
                connections = [
                    c for c in world_data.get("map_connections", [])
                    if c.get("from_layer_id") == npc_map
                ]
                if not connections:
                    continue
                direct = [c for c in connections if c.get("to_layer_id") == p_map]
                connection = direct[0] if direct else connections[0]
                exit_node = connection.get("from_node_id")
                far_map, far_node = connection.get("to_layer_id"), connection.get("to_node_id")
            current_node = npc.get("location_node_id") or exit_node
            if current_node == exit_node:
                npc["location_map_id"] = far_map
                npc["location_node_id"] = far_node
                npc["location_region"] = None
                changed = True
                print(f"[NPC System] {npc['name']} ({npc['id']}) crosses into map {far_map}")
            elif exit_node:
                new_node = _step_toward(world_data, npc_map, current_node, exit_node, hops=TRAVEL_HOP_BUDGET["very_far"])
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
            neighbors = _build_layer_adjacency(world_data, npc_map).get(current_node, [])
            target_node = neighbors[0] if neighbors and turn % 3 == 0 else None

        if not target_node or target_node == current_node:
            continue

        new_node = _step_toward(world_data, npc_map, current_node, target_node, hops=TRAVEL_HOP_BUDGET[tier])
        if new_node != current_node:
            node_lookup = _build_node_lookup(world_data, npc_map)
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
    personality = _normalize_personality(npc.get("personality", ""))
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
    config = _config(state)
    if not config.get("track_character_changes", True):
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

{state_pronoun_grounding(state)}

TRACKED CHARACTERS (the only characters you may report on):
{records}

{earlier_block}{action_block}THIS TURN'S SCENE (check this for changes):
{latest}

{_directive("character_tracking", state.get("module_instructions"))}

NAME CHANGES need no ceremony -- report one whenever:
- the story reveals the actual name of a character recorded under a descriptive epithet (e.g. "The Hooded Stranger" introduces herself as Veyra), OR
- the character adopts, accepts, or is granted a new name, alias, nickname, or title and answers to it, OR
- the scene consistently calls the character something other than the recorded name.

For each changed field return its NEW full value (rewrite the field in full, incorporating the change):
- "name": the new name only
- "appearance": full rewritten text; keep hair color, eye color, and visual age stated (updated when the change is about them, otherwise carried over from the record)
- "personality": full rewritten 2-3 sentence prose incorporating the shift while preserving core traits, voice, and mannerisms that still hold
- "pitch": full rewritten text
- "role": one of {'|'.join(NPC_ROLES)}
- "status": active|departed|deceased
- "notes": the existing notes plus the new observations appended (keep it a compact running log)
Also return "change_note": one short sentence summarizing what changed for that character.

Respond with ONLY valid JSON:
{{"updates": [{{"npc_id": "id from the list above", "<changed field>": "new value", "change_note": "one short sentence"}}]}}
Return {{"updates": []}} if nothing durable changed."""

    skip, decision = await jev_tasks.screen(sdk,
        {**jev_tasks.npc_context(state, candidates, player_action=state.get("last_input_text", "")),
         "task": prompt},
        {f"npc_{i}": f"Does character {npc['id']!r} have any reportable change in THIS turn under the supplied tracking rules? Include revealed or adopted names, lasting appearance/personality changes, pitch/role/status changes, death/departure and noteworthy additions to notes. Exclude mere temporary emotions or conditions."
         for i, npc in enumerate(candidates)}, jev_tasks.NPC, "npc:record_updates")
    if skip:
        return False
    try:
        model_pref = config.get("tracking_ai_model", "balanced")
        raw = await fallback_generate(sdk, decision, prompt, model_pref)
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


def _rapid_dialogue_hold(state: dict, own_data: dict, turn: int, frequency: int) -> bool:
    """True when wb_time_tracker shows <30 in-world minutes have passed since the
    last periodic generation check, holding off mid-conversation up to a 2x turn cap."""
    cur_minutes = _shared.current_minutes(state)
    if cur_minutes is None:
        return False
    last_gen_turn = int((own_data or {}).get("last_gen_turn", 0) or 0)
    if last_gen_turn <= 0 or "last_gen_minutes" not in (own_data or {}):
        return False
    if turn - last_gen_turn >= max(1, frequency) * 2:
        return False
    last_gen_minutes = int(own_data.get("last_gen_minutes", 0) or 0)
    return max(0, cur_minutes - last_gen_minutes) < MIN_GEN_INTERVAL_MINUTES


def _build_generator_prompt(state: dict, bank: dict, needed: int, demand_driven: bool,
                            casting_notes: str = "", critique: str = "") -> str:
    scene = _scene_summary(state)
    bank_text = _bank_summary(bank)
    wctx = _world_context(state)
    region = state.get("player_location_region", "unknown")

    plot_block = _plot_profile_block(state)
    plot_section = f"\n\n{plot_block}" if plot_block else ""
    notes_block = _casting_notes_block({"casting_notes": casting_notes})
    notes_section = f"\n\n{notes_block}" if notes_block else ""
    critique_section = (
        f"\n\nPREVIOUS DRAFT REJECTED -- FIX THIS ISSUE:\n{critique}"
        if critique else ""
    )
    plot_rule = ("\n7. Make these characters resonate with the STORY DIRECTION above -- fit its tone "
                 "and themes, lean into what the player enjoys (strictly honoring their actor/target/mutual role), "
                 "and steer clear of what they dislike."
                 if plot_block else "")

    craft_directive = _directive("character_generation", state.get("module_instructions"))

    if demand_driven:
        task = """You are a casting director for a text-based RPG. First decide whether the story ACTUALLY NEEDS any new character right now -- only then design new NPC concepts for the game.

A new character is justified ONLY when a concrete story signal calls for one: the story direction or one of its open questions points at a person who does not exist yet, an active story thread needs an actor no existing character can play, or where the story is clearly heading will obviously require someone new. If an EXISTING character (introduced or not) can serve the need, use them instead and create no one.

If nothing clearly calls for a new character, respond with {"npcs": []} -- that is the normal, expected outcome, not a failure."""
        rules = f"""INSTRUCTIONS (only for characters that pass the test above):
1. Create at most {needed} characters, and no more than the story clearly needs -- usually zero or one.
2. Every character carries a "need": one sentence naming the specific story signal (direction, open question, story thread, or scene) that demands them. A character without a concrete need must not be created.
3. Bind a character to a location (current region: {region}) only when their need ties them there; otherwise make them encounter-type.
4. {craft_directive}
6. Optionally relate 0-2 new characters to EXISTING characters above via "relationships", using their exact npc_id (e.g. ally, rival, family, mentor, rumored_enemy). Omit "relationships" or leave it empty if no natural connection exists.{plot_rule}"""
        need_field = '"need": "the story signal that demands this character, one sentence", '
    else:
        task = f"You are a character designer for a text-based RPG. Create {needed} new NPC concepts for the game."
        rules = f"""INSTRUCTIONS:
1. Create {needed} characters that fill gaps NOT covered by existing NPCs.
2. At least one should be location-bound to the current region: {region}; the rest can be encounter-type (can appear anywhere).
3. {craft_directive}
6. Optionally relate 0-2 new characters to EXISTING characters above via "relationships", using their exact npc_id (e.g. ally, rival, family, mentor, rumored_enemy). Omit "relationships" or leave it empty if no natural connection exists.{plot_rule}"""
        need_field = ""

    return f"""{task}

WORLD CONTEXT:
{wctx}{plot_section}{notes_section}{critique_section}

CURRENT STORY STATE:
{scene}

EXISTING CHARACTERS (DO NOT duplicate or create similar concepts):
{bank_text}

{rules}

Respond with ONLY valid JSON:
{{"npcs": [{{{need_field}"name": "string", "race": "string", "gender": "male|female|nonbinary", "appearance": "1-2 sentence physical description that always states hair color and eye color (or the being's closest equivalent) and visual age (how old they look)", "archetype": "short archetype label", "pitch": "2-3 sentence character concept with story hook", "personality": "2-3 sentence prose capturing outward mask vs. undercurrent contradiction, immediate active want, and voice/physical tell", "role": "quest_giver|antagonist|ally|informant|rival|neutral|wildcard", "encounter_type": "location_bound|encounter", "location_node_id": "node_id or null", "location_region": "region name or null", "location_map_id": "map_id or null (only if encounter_type is location_bound)", "relationships": [{{"npc_id": "existing npc_id", "type": "ally|rival|family|mentor|rumored_enemy|...", "description": "short description of the connection"}}]}}], "casting_notes": "optional updated backstage casting notes"}}"""


async def _run_librarian_llm_pass(state: dict, sdk, base_updates: dict | None = None) -> dict | None:
    """Core post-turn tracking, travel, and casting pass. Runs inline in unit
    tests and asynchronously via ModuleBackgroundRunner after live turns."""
    config = _config(state)
    frequency = max(1, int(config.get("generator_frequency", DEFAULT_GENERATOR_FREQUENCY) or DEFAULT_GENERATOR_FREQUENCY))
    max_pool = int(config.get("max_unintroduced_pool", DEFAULT_MAX_POOL) or DEFAULT_MAX_POOL)
    turn = int(state.get("turn", 0) or 0)

    if turn == 0:
        return None

    if base_updates:
        mod_state = state.setdefault("module_data", {}).setdefault(MODULE_ID, {})
        if isinstance(base_updates.get("characters"), dict):
            mod_state.setdefault("characters", {}).update(deepcopy(base_updates["characters"]))
        for k, v in base_updates.items():
            if k != "characters":
                mod_state[k] = deepcopy(v)

    own_data = _own_data(state)
    bank = _get_bank(state)
    extra_updates: dict = {k: v for k, v in (base_updates or {}).items() if k != "characters"}

    if turn % frequency != 0:
        evolved = await _track_character_changes(state, bank, sdk)
        if evolved or extra_updates:
            return _set_bank(extra_updates, bank)
        return None

    # Borrow active story threads directly from Plot Director when available so
    # we don't spend a duplicate LLM call extracting threads every N turns.
    borrowed_threads = _shared.plot_threads_for_npc(state, bank)

    async def _get_threads():
        if borrowed_threads is not None:
            return borrowed_threads
        return await _update_story_threads(state, sdk)

    evolved, threads, traveled = await asyncio.gather(
        _track_character_changes(state, bank, sdk),
        _get_threads(),
        _independent_travel_pass(state, bank, sdk),
    )
    bank_changed = evolved or traveled
    if threads:
        extra_updates["story_threads"] = threads

    # Respect Plot Director's initial observation window and avoid periodic
    # character generation in the middle of rapid-fire (<30 min) conversations.
    if _shared.is_plot_observing(state) or _rapid_dialogue_hold(state, own_data, turn, frequency):
        return _set_bank(extra_updates, bank) if (extra_updates or bank_changed) else None

    unintroduced_count = sum(
        1 for n in bank.values()
        if not n.get("introduced") and n.get("status") == "unintroduced"
    )

    if unintroduced_count >= max_pool:
        return _set_bank(extra_updates, bank) if (extra_updates or bank_changed) else None

    needed = min(3, max_pool - unintroduced_count)
    demand_driven = config.get("demand_driven_generation", True)
    casting_notes = str(own_data.get("casting_notes") or "").strip()

    probe_prompt = _build_generator_prompt(
        state, bank, needed, demand_driven, casting_notes=casting_notes
    )
    decision = Decision()
    if demand_driven:
        skip, decision = await jev_tasks.screen(sdk,
            {**jev_tasks.npc_context(state, list(bank.values()), player_action=state.get("last_input_text", "")),
             "task": probe_prompt},
            {"new_character": "Does the story actually need any NEW character now, because of a concrete scene, direction or thread that no EXISTING character (introduced or unintroduced) can serve?"},
            jev_tasks.NPC, "npc:creation_screen")
        if skip:
            return _set_bank(extra_updates, bank) if (extra_updates or bank_changed) else None

    iterations = _casting_iterations(config)
    if iterations > 1:
        casting_notes = await _iterate_casting_notes(
            state, sdk, {**own_data, "casting_notes": casting_notes}, bank, passes=iterations - 1
        )
        if casting_notes:
            extra_updates["casting_notes"] = casting_notes

    casting_model = config.get("casting_ai_model", "smartest")
    critique = ""
    vetted_npcs: list[dict] = []

    for attempt in range(FIT_MAX_REJECTS + 1):
        prompt = _build_generator_prompt(
            state, bank, needed, demand_driven, casting_notes=casting_notes, critique=critique
        )
        try:
            if attempt == 0:
                result = await fallback_generate(sdk, decision, prompt, casting_model)
            else:
                result = await _llm_generate(sdk, prompt, model_preference=casting_model)
            parsed = _parse_json_block(result)
            if not isinstance(parsed, dict):
                break
        except Exception as e:
            print(f"[NPC System] Generator Agent failed: {e}")
            break

        returned_notes = str(parsed.get("casting_notes") or "").strip()
        if returned_notes:
            casting_notes = returned_notes
            extra_updates["casting_notes"] = casting_notes

        raw_npcs = parsed.get("npcs", [])
        if not isinstance(raw_npcs, list):
            raw_npcs = []
        if demand_driven:
            justified = [
                n for n in raw_npcs
                if isinstance(n, dict) and str(n.get("need", "")).strip()
            ]
            dropped = len(raw_npcs) - len(justified)
            if dropped:
                print(f"[NPC System] Dropped {dropped} generated NPC(s) without a stated story need.")
            candidates = justified[:needed]
        else:
            candidates = [n for n in raw_npcs if isinstance(n, dict)][:needed]

        if not candidates:
            break

        vetted_npcs, critique = await _fit_check_npcs(state, sdk, candidates, bank)
        if vetted_npcs or not critique:
            break
        print(f"[NPC System] Fit-check rejected candidate batch (attempt {attempt + 1}): {critique}")

    cur_minutes = _shared.current_minutes(state)
    extra_updates["last_gen_turn"] = turn
    if cur_minutes is not None:
        extra_updates["last_gen_minutes"] = cur_minutes

    if not vetted_npcs:
        return _set_bank(extra_updates, bank) if (extra_updates or bank_changed) else None

    for npc_data in vetted_npcs:
        npc_id, record = _build_npc_record(npc_data, turn, bank, source="generated")
        bank[npc_id] = record

    if iterations >= 3:
        refined_notes = await _iterate_casting_notes(
            state, sdk, {**own_data, "casting_notes": casting_notes}, bank, passes=1
        )
        if refined_notes:
            extra_updates["casting_notes"] = refined_notes

    print(f"[NPC System] Generated {len(vetted_npcs)} new NPCs (bank size: {len(bank)})")
    return _set_bank(extra_updates, bank)


async def _execute_bg_job(job: dict) -> None:
    await _bg_runner._execute_job(job, _run_librarian_llm_pass)


async def on_librarian(state: dict, sdk) -> dict | None:
    turn = int(state.get("turn", 0) or 0)
    if turn == 0:
        return None

    staged = _bg_runner.consume_staged_for_librarian(state)
    if _bg_runner.is_live_turn_armed():
        _bg_runner.queue_job(state, sdk, immediate_updates=staged)
        return {"module_data": {MODULE_ID: staged}} if staged else None

    return await _run_librarian_llm_pass(state, sdk, base_updates=staged)


async def on_turn_stopped(state: dict, sdk, reason: str) -> None:
    """Spawn the queued background NPC pass once the turn has completed and
    unlocked the UI, or discard it if cancelled."""
    await _bg_runner.on_turn_stopped(reason, _run_librarian_llm_pass)


async def on_command_npcs(args: list[str], state: dict, sdk) -> dict:
    bank = _get_bank(state)

    introduced = [n for n in bank.values() if n.get("introduced")]
    nearby = [n for n in bank.values() if not n.get("introduced")]
    node_id = state.get("player_location_node_id", "")
    region = state.get("player_location_region", "")
    map_id = _player_map_id(state) or ""

    lines = ["[NPCs]"]

    if introduced:
        lines.append("--- Known Characters ---")
        for npc in introduced:
            loc = ""
            if npc.get("encounter_type") == "location_bound":
                loc = f" @ {npc.get('location_node_id', npc.get('location_region', '?'))} (map={npc.get('location_map_id', '-')})"
            party = " [PARTY]" if npc.get("traveling_with_player") else ""
            lines.append(f"  {npc['name']} ({npc.get('archetype', '?')}) -- {npc.get('role', '?')} [{npc.get('status', '?')}]{loc}{party}")

    if nearby:
        location_bound_here = [
            n for n in nearby
            if n.get("encounter_type") == "location_bound"
            and not (n.get("location_map_id") and map_id and n.get("location_map_id") != map_id)
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
            loc = f" bound@{npc.get('location_node_id', npc.get('location_region', '?'))} map={npc.get('location_map_id', '-')}"
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
            if not isinstance(val, (str, list)):
                continue
            val = _normalize_personality(val)
            if not val:
                continue
            if val != _normalize_personality(npc.get("personality", "")):
                edits[field] = val
            continue
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
        return {"message": f"[NPC] Unknown character id: {npc_id}", "signal": "end_turn", "error": True}

    history = state.get("history", [])
    if not history:
        return {"message": "[NPC] There is no story yet to update from.", "signal": "end_turn", "error": True}

    story = "\n\n".join(str(h) for h in history)
    personality = _normalize_personality(npc.get("personality", ""))

    prompt = f"""You maintain the character records for the NPCs of a text RPG. The player has asked you to bring one character's record up to date: check the recent story for LASTING changes to this character and rewrite only the fields that changed.

{state_pronoun_grounding(state)}

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

{_directive("character_tracking", state.get("module_instructions"))}

For each changed field return its NEW full value (rewrite the field in full, incorporating the change):
- "appearance": full rewritten text; keep hair color, eye color, and visual age stated (updated when the change is about them, otherwise carried over from the record)
- "pitch": full rewritten text
- "personality": full rewritten 2-3 sentence prose incorporating the shift while preserving core traits, voice, and mannerisms that still hold
- "name": only if they are now called something else
- "role": one of {'|'.join(NPC_ROLES)} -- only if their narrative function clearly shifted
- "status": one of active|departed|deceased -- only if the story shows them dying or leaving the story
- "notes": the existing notes plus new observations appended (keep it a compact running log)
Also return "change_note": one short sentence summarizing what changed.

If nothing durable changed, return an empty object {{}}.

Respond with ONLY valid JSON containing just the changed fields (plus change_note)."""

    model_pref = _config(state).get("tracking_ai_model", "balanced")
    raw = await _llm_generate(sdk, prompt, model_preference=model_pref)
    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return {"message": "[NPC] The update pass returned nothing usable -- try again.", "signal": "end_turn", "error": True}

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
        npc["location_map_id"] = _player_map_id(state)
    if _config(state).get("embed_profiles", True):
        await _embed_profile(npc, turn, sdk)
    print(f"[NPC System] {npc.get('name')} manually activated at turn {turn}")


async def _apply_manual_edit(npc_id: str, payload: str, state: dict, sdk) -> dict:
    """Apply a browser edit. The payload is URL-encoded JSON (one whitespace-free
    token, so it survives the command dispatcher's text.split())."""
    bank = _get_bank(state)
    npc = bank.get(npc_id)
    if not npc:
        return {"message": f"[NPC] Unknown character id: {npc_id}", "signal": "end_turn", "error": True}

    try:
        raw = json.loads(urllib.parse.unquote(payload))
    except (json.JSONDecodeError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {"message": "[NPC] Could not parse the edit payload.", "signal": "end_turn", "error": True}

    edits = _sanitize_edits(raw, npc)
    if not edits:
        return {"message": f"[NPC] Nothing to change for {npc.get('name', npc_id)}.", "signal": "end_turn", "error": True}

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


def _coerce_personality(value) -> str:
    """Normalize a personality field (prose string or legacy list) to prose."""
    return _normalize_personality(value)


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
        return {"message": "[NPC] Could not parse the character payload.", "signal": "end_turn", "error": True}

    name = str(raw.get("name", "")).strip()
    if not name:
        return {"message": "[NPC] A new character needs a name.", "signal": "end_turn", "error": True}

    introduced = bool(raw.get("introduced", True))
    turn = state.get("turn", 0)
    location = (
        (state.get("player_location_node_id"),
         state.get("player_location_region"),
         _player_map_id(state))
        if introduced else (None, None, None)
    )

    npc_data = {
        "name": name,
        "race": str(raw.get("race", "")).strip(),
        "gender": str(raw.get("gender", "")).strip(),
        "appearance": str(raw.get("appearance", "")).strip(),
        "archetype": str(raw.get("archetype", "")).strip(),
        "pitch": str(raw.get("pitch", "")).strip(),
        "personality": _coerce_personality(raw.get("personality", "")),
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
    notes_block = _casting_notes_block(_own_data(state))
    notes_section = f"\n\n{notes_block}" if notes_block else ""
    plot_rule = ("\n7. Make the character resonate with the STORY DIRECTION above -- fit its tone "
                 "and themes, lean into what the player enjoys (strictly honoring their actor/target/mutual role), "
                 "and steer clear of what they dislike."
                 if plot_block else "")

    craft_directive = _directive("character_generation", state.get("module_instructions"))

    prompt = f"""You are a character designer for a text-based RPG. Create 1 new NPC concept for the game.{request_section}

WORLD CONTEXT:
{wctx}{plot_section}{notes_section}

CURRENT STORY STATE:
{scene}

EXISTING CHARACTERS (DO NOT duplicate or create similar concepts):
{bank_text}

INSTRUCTIONS:
1. Create 1 character that fills a gap NOT covered by existing NPCs.
2. The character can be location-bound to the current region ({region}) or an encounter-type NPC that can appear anywhere.
3. {craft_directive}
6. Optionally relate the character to an EXISTING character above via "relationships", using their exact npc_id (e.g. ally, rival, family, mentor, rumored_enemy). Omit "relationships" or leave it empty if no natural connection exists.{plot_rule}

Respond with ONLY valid JSON:
{{"npc": {{"name": "string", "race": "string", "gender": "male|female|nonbinary", "appearance": "1-2 sentence physical description that always states hair color and eye color (or the being's closest equivalent) and visual age (how old they look)", "archetype": "short archetype label", "pitch": "2-3 sentence character concept with story hook", "personality": "2-3 sentence prose capturing outward mask vs. undercurrent contradiction, immediate active want, and voice/physical tell", "role": "quest_giver|antagonist|ally|informant|rival|neutral|wildcard", "encounter_type": "location_bound|encounter", "location_node_id": "node_id or null", "location_region": "region name or null", "location_map_id": "map_id or null (only if encounter_type is location_bound)", "relationships": [{{"npc_id": "existing npc_id", "type": "ally|rival|family|mentor|rumored_enemy|...", "description": "short description of the connection"}}]}}}}"""

    try:
        model_pref = _config(state).get("casting_ai_model", "smartest")
        result = await _llm_generate(sdk, prompt, model_preference=model_pref)
        parsed = _parse_json_block(result)
        if not isinstance(parsed, dict):
            raise ValueError("Non-JSON generator output")
    except Exception as e:
        print(f"[NPC System] Generate-character command failed: {e}")
        return {"message": "[NPC] Could not generate a character right now. Try again.", "signal": "end_turn", "error": True}

    npc_data = parsed.get("npc")
    if not isinstance(npc_data, dict) or not str(npc_data.get("name", "")).strip():
        return {"message": "[NPC] The generator returned nothing usable. Try again.", "signal": "end_turn", "error": True}

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
        return {"message": f"[NPC] Unknown character id: {npc_id}", "signal": "end_turn", "error": True}

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
             "/npc edit <npc_id> <data> | /npc delete <npc_id> | /npc notes")
    if not args:
        return {"message": usage, "signal": "end_turn", "error": True}

    sub = args[0].lower()
    if sub == "notes":
        notes = str(_own_data(state).get("casting_notes") or "").strip()
        if not notes:
            return {"message": "[NPC] No casting notes recorded yet.", "signal": "end_turn"}
        return {"message": f"[NPC] Casting notes (spoiler):\n{notes}", "signal": "end_turn"}
    if sub in ("generate", "gen", "random"):
        _cancel_bg_task()
        request = urllib.parse.unquote(" ".join(args[1:])).strip() if len(args) >= 2 else ""
        return await _generate_random_character(state, sdk, request)
    if sub in ("update", "refresh") and len(args) >= 2:
        _cancel_bg_task()
        return await _update_npc_from_story(args[1], state, sdk)
    if sub == "edit" and len(args) >= 3:
        _cancel_bg_task()
        return await _apply_manual_edit(args[1], " ".join(args[2:]), state, sdk)
    if sub == "add" and len(args) >= 2:
        _cancel_bg_task()
        return await _apply_manual_add(args[1], state, sdk)
    if sub in ("delete", "remove") and len(args) >= 2:
        _cancel_bg_task()
        return await _delete_npc(args[1], state, sdk)
    return {"message": usage, "signal": "end_turn"}
