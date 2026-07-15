"""Plot Director v3 -- observe-then-respond plot threading with a memory.

Instead of imposing a hidden long arc, the module watches the story the player
is actually creating. After each storyteller turn a background LLM pass updates
a live profile (playstyle weights, tone, themes, evidence-backed likes and
observed aversions, attachments to named story elements, engagement patterns,
narrative preferences) and judges the single active short-term plot thread:
engaged, resolved, or ignored.

Cohesion comes from two layers of persistent state. A narrative direction
(premise, heading, open questions, recurring elements) evolves whenever a
thread closes, and every closed thread leaves a one-sentence consequence in
thread_history; new threads are generated to build on both, grounded in the
NPC system's established characters, and vetted by a fit-check critic before
they go live. Between threads the story gets a configurable quiet period.

The active thread is visible: a soft context line keeps the storyteller aware
of it every turn, /plot and the sidebar widget show it to the player (with the
challenge spoiler-hidden until they opt in), and one-turn nudges ride inside
that context line only when the thread stalls or a natural opening appears.

The player can suspend plot direction at any time (/plot suspend or the widget
button): the context line, assessments, nudges, expiry, and generation all
freeze until /plot resume, and the thread's lifespan clock shifts by the paused
duration so it never expires the moment it wakes up.
"""
import json

MODULE_ID = "wb_plot_director"

SCHEMA_VERSION = 3
PLAYSTYLE_KEYS = (
    "combat", "diplomacy", "exploration", "mystery", "social", "intrigue",
    "stealth", "strategy", "romance", "crafting", "roleplay", "humor",
)
NARRATIVE_KEYS = ("pacing", "agency", "register")
ENGAGEMENT_KEYS = ("bites_on", "ignores")
PROFILE_LIST_CAP = 12
PROFILE_ENTRY_MAX_CHARS = 160
PROFILE_NOTES_MAX_CHARS = 600
THREAD_HISTORY_CAP = 10
MAX_LOG_ENTRIES = 20
GEN_MAX_ATTEMPTS = 3
OPPORTUNITY_COOLDOWN_TURNS = 3
DIRECTION_LIST_CAP = 6
FIT_MAX_REJECTS = 2
DEFER_MAX_STREAK = 2
ABANDON_COOLDOWN_FACTOR = 2
PIVOT_COOLDOWN_TURNS = 3  # min turns between pivot-driven thread replacements
# Evidence marker distinguishing likes the player typed in themselves from
# AI-observed ones; player-set entries survive a /plot reset.
PLAYER_SET_EVIDENCE = "set directly by the player"
BOOTSTRAP_SCENES = 15
BOOTSTRAP_SCENES_MAX_CHARS = 12000
BOOTSTRAP_INPUTS = 15
BOOTSTRAP_INPUTS_MAX_CHARS = 2500

MOMENTUM_VALUES = ("observing", "building", "steady", "stalled", "resolving")
PREF_WEIGHTS = ("low", "medium", "high")  # likes/dislikes strength, weakest first

# Legacy (v1, 3-act outline) keys wiped on migration. Writing "" replaces the
# old dicts/lists outright because _deep_merge only recurses dict-into-dict.
LEGACY_KEYS = (
    "outline", "position", "beats_completed", "assessment_log",
    "outline_created_turn", "outline_attempts", "stall_count",
    "drift_streak", "replan_count", "last_replan_turn",
)

DIFFICULTY_GUIDANCE = {
    1: "Keep it gentle: light complications the player can overcome comfortably; low stakes; failure costs little.",
    2: "Mild challenge: real but forgiving obstacles; modest stakes; setbacks are recoverable.",
    3: "Moderate challenge: capable opposition and meaningful stakes; success requires real effort.",
    4: "Hard: cunning, capable opposition; plans should be able to fail; significant costs and consequences.",
    5: "Brutal: dangerous, relentless opposition; dire stakes; failure carries lasting consequences for the player and the world.",
}


def _config(state: dict) -> dict:
    return state.get("module_configs", {}).get(MODULE_ID, {})


def _own_data(state: dict) -> dict:
    return state.get("module_data", {}).get(MODULE_ID, {})


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


def _empty_thread() -> dict:
    return {
        "id": "",
        "title": "",
        "hook": "",
        "challenge": "",
        "stakes": "",
        "appeal": "",
        "status": "none",  # none | active | resolved | expired | abandoned
        "created_turn": 0,
        "closed_turn": 0,
    }


def _full_thread(**fields) -> dict:
    """Every thread write carries the complete key set: a partial dict would be
    deep-merged with the previous thread's leftover keys."""
    thread = _empty_thread()
    unknown = set(fields) - set(thread)
    if unknown:
        raise ValueError(f"unknown thread fields: {unknown}")
    thread.update(fields)
    return thread


def _default_profile() -> dict:
    return {
        "playstyle": {k: 0 for k in PLAYSTYLE_KEYS},
        "tone": "",
        "themes": [],
        "likes": [],       # [{text, weight, evidence}] -- evidence: the observed behavior behind the entry
        "dislikes": [],    # [{text, weight}] -- player-set only, hard vetoes
        "avoids": [],      # [{text, weight, evidence}] -- AI-observed soft aversions, steer-only
        "attachments": [], # [{name, kind, note}] -- named characters/places/factions the player gravitates to
        "engagement": {k: [] for k in ENGAGEMENT_KEYS},  # hook shapes that work / get ignored
        "narrative": {k: "" for k in NARRATIVE_KEYS},    # pacing, agency, emotional register
        "notes": "",
    }


def _default_direction() -> dict:
    return {
        "premise": "",             # 1-2 sentences: the larger arc the story is telling
        "heading": "",             # one sentence: where events look to be going next
        "open_questions": [],      # short unresolved questions the story has raised
        "recurring_elements": [],  # named characters/places/factions/motifs worth returning to
        "updated_turn": 0,
    }


def _default_data() -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "status": "observing",  # observing | active | failed
        "gen_attempts": 0,
        "profile": _default_profile(),
        "thread": _empty_thread(),
        "thread_history": [],
        "direction": _default_direction(),
        "momentum": "observing",
        "ignored_streak": 0,
        "pending_nudge": "",
        "last_nudge_turn": 0,
        "last_closed_turn": 0,
        "next_thread_turn": 0,  # earliest turn a new thread may be generated
        "defer_streak": 0,      # consecutive generator defers (readiness escape)
        "direction_seed_attempts": 0,  # failed initial-direction reads (capped)
        "last_pivot_turn": 0,  # last pivot-driven replacement (thrash guard)
        "suspended": False,
        "suspended_turn": 0,
        "log": [],
    }


def _clean_str_list(value, previous: list) -> list:
    if not isinstance(value, list):
        return list(previous)[:PROFILE_LIST_CAP]
    cleaned = []
    for item in value:
        text = str(item).strip()[:PROFILE_ENTRY_MAX_CHARS]
        if text:
            cleaned.append(text)
    return cleaned[:PROFILE_LIST_CAP]


def _cap_prefs(entries: list) -> list:
    """Enforce the cap by evicting the lowest-weight entries, oldest first --
    a strongly-held preference should never fall off before a mild one."""
    entries = list(entries)
    while len(entries) > PROFILE_LIST_CAP:
        idx = min(range(len(entries)), key=lambda i: PREF_WEIGHTS.index(entries[i]["weight"]))
        entries.pop(idx)
    return entries


def _clean_pref_list(value, previous: list, with_evidence: bool = False) -> list:
    """Likes/dislikes/avoids: [{text, weight}] entries, plus an evidence clause
    on observed lists. Plain strings (legacy saves, LLM shortcuts) normalize to
    medium weight; bad weights become medium."""
    source = value if isinstance(value, list) else previous
    cleaned = []
    for item in (source if isinstance(source, list) else []):
        if isinstance(item, dict):
            text = item.get("text", "")
            weight = item.get("weight")
            evidence = item.get("evidence", "")
        else:
            text = item
            weight = "medium"
            evidence = ""
        text = str(text).strip()[:PROFILE_ENTRY_MAX_CHARS]
        if not text:
            continue
        if weight not in PREF_WEIGHTS:
            weight = "medium"
        entry = {"text": text, "weight": weight}
        if with_evidence:
            entry["evidence"] = str(evidence or "").strip()[:PROFILE_ENTRY_MAX_CHARS]
        cleaned.append(entry)
    return _cap_prefs(cleaned)


def _norm_entry(text: str) -> str:
    return " ".join(str(text).split()).lower()


def _clean_attachments(value, previous: list) -> list:
    """Attachments: [{name, kind, note}] -- named story elements the player
    gravitates to (a character, place, faction, object, motif)."""
    source = value if isinstance(value, list) else previous
    cleaned = []
    for item in (source if isinstance(source, list) else []):
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()[:PROFILE_ENTRY_MAX_CHARS]
            kind = str(item.get("kind", "")).strip()[:PROFILE_ENTRY_MAX_CHARS]
            note = str(item.get("note", "")).strip()[:PROFILE_ENTRY_MAX_CHARS]
        else:
            name = str(item).strip()[:PROFILE_ENTRY_MAX_CHARS]
            kind = note = ""
        if name:
            cleaned.append({"name": name, "kind": kind, "note": note})
    return cleaned[:PROFILE_LIST_CAP]


def _clean_engagement(value, previous) -> dict:
    previous = previous if isinstance(previous, dict) else {}
    value = value if isinstance(value, dict) else {}
    return {
        key: _clean_str_list(value.get(key), previous.get(key, []) or [])
        for key in ENGAGEMENT_KEYS
    }


def _clean_narrative(value, previous) -> dict:
    previous = previous if isinstance(previous, dict) else {}
    value = value if isinstance(value, dict) else {}
    return {
        key: str(value.get(key) or previous.get(key) or "").strip()[:PROFILE_ENTRY_MAX_CHARS]
        for key in NARRATIVE_KEYS
    }


def _clean_direction(parsed, previous: dict) -> dict:
    """Sanitize an LLM narrative-direction reply; fall back to previous values
    on garbage. Always returns the full direction shape -- _deep_merge merges
    dict-into-dict, so a partial write would leave stale keys behind."""
    previous = previous if isinstance(previous, dict) else _default_direction()
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "premise": str(parsed.get("premise") or previous.get("premise") or "").strip(),
        "heading": str(parsed.get("heading") or previous.get("heading") or "").strip(),
        "open_questions": _clean_str_list(
            parsed.get("open_questions"), previous.get("open_questions", []) or [])[:DIRECTION_LIST_CAP],
        "recurring_elements": _clean_str_list(
            parsed.get("recurring_elements"), previous.get("recurring_elements", []) or [])[:DIRECTION_LIST_CAP],
        "updated_turn": int(previous.get("updated_turn", 0) or 0),
    }


def _clean_profile(parsed, previous: dict) -> dict:
    """Sanitize an LLM profile reply; fall back to previous values on garbage.
    Always returns the full profile shape (all playstyle keys, capped lists).

    Dislikes are player authority: the LLM's dislikes are ignored outright
    (in-character aversion often contradicts the player's real taste), and any
    proposed like that matches a player-set dislike is dropped. Avoids are the
    AI-observed counterpart -- soft aversions that only steer generation, never
    veto -- and yield to both sides of the player's stated taste: an avoid that
    matches a like or a dislike is dropped."""
    previous = previous if isinstance(previous, dict) else _default_profile()
    if not isinstance(parsed, dict):
        parsed = {}

    prev_style = previous.get("playstyle", {})
    parsed_style = parsed.get("playstyle")
    parsed_style = parsed_style if isinstance(parsed_style, dict) else {}
    playstyle = {}
    for key in PLAYSTYLE_KEYS:
        raw = parsed_style.get(key, prev_style.get(key, 0))
        try:
            playstyle[key] = max(0, min(10, int(raw)))
        except (TypeError, ValueError):
            try:
                playstyle[key] = max(0, min(10, int(prev_style.get(key, 0))))
            except (TypeError, ValueError):
                playstyle[key] = 0

    tone = str(parsed.get("tone") or previous.get("tone") or "").strip()[:PROFILE_ENTRY_MAX_CHARS]
    dislikes = _clean_pref_list(None, previous.get("dislikes", []))
    disliked = {_norm_entry(e["text"]) for e in dislikes}
    likes = [
        e for e in _clean_pref_list(parsed.get("likes"), previous.get("likes", []), with_evidence=True)
        if _norm_entry(e["text"]) not in disliked
    ]
    liked = {_norm_entry(e["text"]) for e in likes}
    avoids = [
        e for e in _clean_pref_list(parsed.get("avoids"), previous.get("avoids", []), with_evidence=True)
        if _norm_entry(e["text"]) not in disliked and _norm_entry(e["text"]) not in liked
    ]
    return {
        "playstyle": playstyle,
        "tone": tone,
        "themes": _clean_str_list(parsed.get("themes"), previous.get("themes", [])),
        "likes": likes,
        "dislikes": dislikes,
        "avoids": avoids,
        "attachments": _clean_attachments(parsed.get("attachments"), previous.get("attachments", [])),
        "engagement": _clean_engagement(parsed.get("engagement"), previous.get("engagement")),
        "narrative": _clean_narrative(parsed.get("narrative"), previous.get("narrative")),
        "notes": str(parsed.get("notes") or previous.get("notes") or "").strip()[:PROFILE_NOTES_MAX_CHARS],
    }


def _merge_player_prefs(profile: dict, kept_likes: list, kept_dislikes: list) -> dict:
    """Re-seat player-stated preferences into a rebuilt profile: dislikes
    replace wholesale (they are player-only), and player-set likes are
    re-added -- winning over an observed duplicate so their weight and marker
    survive. Player authority still applies: anything matching a dislike is
    dropped from likes and avoids."""
    merged = _clean_profile({}, profile)
    merged["dislikes"] = _clean_pref_list(kept_dislikes, [])
    disliked = {_norm_entry(e["text"]) for e in merged["dislikes"]}

    likes = [e for e in merged["likes"] if _norm_entry(e["text"]) not in disliked]
    for entry in _clean_pref_list(kept_likes, [], with_evidence=True):
        norm = _norm_entry(entry["text"])
        if norm in disliked:
            continue
        if any(_norm_entry(e["text"]) == norm for e in likes):
            likes = [entry if _norm_entry(e["text"]) == norm else e for e in likes]
        else:
            likes.append(entry)
    merged["likes"] = _cap_prefs(likes)

    liked = {_norm_entry(e["text"]) for e in merged["likes"]}
    merged["avoids"] = [
        e for e in merged["avoids"]
        if _norm_entry(e["text"]) not in disliked and _norm_entry(e["text"]) not in liked
    ]
    return merged


def _npc_story_threads(state: dict) -> list[str]:
    threads = state.get("module_data", {}).get("wb_npc_system", {}).get("story_threads", [])
    lines = []
    if isinstance(threads, list):
        for thread in threads:
            if isinstance(thread, dict):
                text = str(thread.get("text", "")).strip()
                if text:
                    lines.append(text)
    return lines


def _storylines_block(state: dict) -> str:
    lines = _npc_story_threads(state)
    if not lines:
        return ""
    return "OTHER ACTIVE STORYLINES (tracked by the NPC system):\n" + "\n".join(f"- {t}" for t in lines) + "\n\n"


def _character_roster(state: dict) -> str:
    """Established characters from the NPC system's bank, for grounding threads
    in people the story already knows. Unintroduced characters are included --
    a thread is a natural way to debut a prepared character -- but tagged so
    the generator knows the player hasn't met them."""
    bank = state.get("module_data", {}).get("wb_npc_system", {}).get("characters", {})
    if not isinstance(bank, dict):
        return ""
    lines = []
    for npc in bank.values():
        if not isinstance(npc, dict):
            continue
        name = str(npc.get("name", "")).strip()
        if not name or npc.get("status") == "deceased":
            continue
        archetype = str(npc.get("archetype", "")).strip()
        role = str(npc.get("role", "")).strip()
        if npc.get("status") == "departed":
            tag = "departed -- could return"
        elif npc.get("introduced"):
            tag = "met"
        else:
            tag = "not yet met -- may debut through this thread"
        descriptor = ", ".join(p for p in (role, tag) if p)
        lines.append(f"- {name} -- {archetype or 'character'} ({descriptor})")
        pitch = str(npc.get("pitch", "")).strip()
        if pitch:
            lines.append(f"  Pitch: {pitch}")
    if not lines:
        return ""
    return (
        "ESTABLISHED CHARACTERS (anchor the thread in these people where possible):\n"
        + "\n".join(lines) + "\n\n"
    )


def _direction_block(data: dict) -> str:
    """The evolving macro-arc, injected into generation and fit-check prompts.
    Empty while the story is too young to have a premise."""
    direction = data.get("direction") or {}
    premise = str(direction.get("premise", "")).strip()
    if not premise:
        return ""
    lines = [
        "NARRATIVE DIRECTION (the story's larger arc -- the new thread should "
        "advance, complicate, or pay off part of it, not sit beside it):",
        f"Premise: {premise}",
    ]
    heading = str(direction.get("heading", "")).strip()
    if heading:
        lines.append(f"Heading: {heading}")
    questions = [str(q).strip() for q in direction.get("open_questions", []) if str(q).strip()]
    if questions:
        lines.append("Open questions: " + "; ".join(questions))
    recurring = [str(r).strip() for r in direction.get("recurring_elements", []) if str(r).strip()]
    if recurring:
        lines.append("Recurring elements: " + ", ".join(recurring))
    return "\n".join(lines) + "\n\n"


def _setting_material(state: dict) -> list[str]:
    """Scenario and world rules/lore blocks shared by every generation prompt."""
    parts = []

    scenario = state.get("scenario_data")
    if isinstance(scenario, dict):
        description = str(scenario.get("scenario_description", "")).strip()
        if description:
            parts.append(f"<scenario>\n{description}\n</scenario>")

    world = state.get("world_data")
    if isinstance(world, dict):
        rules = world.get("rules", {}) or {}
        lore = world.get("lore", {}) or {}
        lines = []
        for label, value in (
            ("Genre", rules.get("genre")),
            ("Tone", rules.get("tone")),
            ("World", lore.get("world_name")),
            ("Premise", lore.get("premise")),
            ("Central Conflict", lore.get("central_conflict")),
        ):
            if value:
                lines.append(f"{label}: {value}")
        if lines:
            parts.append("<world>\n" + "\n".join(lines) + "\n</world>")

    return parts


def _story_material(state: dict) -> str:
    """Collect the material threads are woven from: scenario, world rules/lore,
    and the most recent scenes (newest always in full)."""
    parts = _setting_material(state)

    history = state.get("history", [])
    if history:
        latest = str(history[-1])[-4000:]
        earlier = "\n".join(str(h) for h in history[-6:-1])[-3000:]
        story = (earlier + "\n" if earlier else "") + latest
        parts.append(f"<story_so_far>\n{story}\n</story_so_far>")

    return "\n\n".join(parts)


def _is_profile_empty(profile: dict) -> bool:
    if not isinstance(profile, dict):
        return True
    return (
        not any(profile.get("playstyle", {}).values())
        and not profile.get("tone")
        and not profile.get("themes")
        and not profile.get("likes")
        and not profile.get("dislikes")
    )


async def _bootstrap_profile(state: dict, sdk, preserved: dict | None = None) -> dict | None:
    """One-time deep read when the module joins a story already in progress:
    distill an initial player profile from the whole story so far instead of
    starting blind. ``preserved`` optionally carries player-stated likes and
    dislikes that survived a reset -- they anchor the analysis and are merged
    back by the caller. Returns the sanitized profile, or None on a bad
    reply."""
    config = _config(state)

    preserved_block = ""
    if preserved and (preserved.get("likes") or preserved.get("dislikes")):
        def _fmt(entries):
            return "; ".join(f"{e.get('text', '')} ({e.get('weight', 'medium')})" for e in entries)
        lines = [
            "PLAYER-STATED PREFERENCES (set directly by the player; they are kept "
            "verbatim, so do not restate them -- build the rest of the profile "
            "around them and never contradict them):"
        ]
        if preserved.get("likes"):
            lines.append(f"Likes: {_fmt(preserved['likes'])}")
        if preserved.get("dislikes"):
            lines.append(f"Dislikes: {_fmt(preserved['dislikes'])}")
        preserved_block = "\n".join(lines) + "\n\n"

    scenes = "\n\n".join(str(h) for h in state.get("history", [])[-BOOTSTRAP_SCENES:])[-BOOTSTRAP_SCENES_MAX_CHARS:]
    inputs = [
        str(m.get("content", "")).strip()
        for m in state.get("chat_messages", [])
        if m.get("role") == "user" and str(m.get("content", "")).strip()
    ]
    inputs_block = "\n".join(f"- {i}" for i in inputs[-BOOTSTRAP_INPUTS:])[-BOOTSTRAP_INPUTS_MAX_CHARS:]
    setting = "\n\n".join(_setting_material(state))

    prompt = f"""You are joining a text RPG already in progress. Study the story so far and build an initial profile of the player: how they actually play, the story's prevailing tone, its recurring themes, and what the player seems to enjoy or avoid.

{setting}

STORY SO FAR (oldest to newest):
{scenes}

THE PLAYER'S OWN ACTIONS (their typed inputs, oldest to newest):
{inputs_block or '(none recorded)'}

{preserved_block}Base the profile on demonstrated behavior, not on what the setting suggests. Every field:
- playstyle: integers 0-10 for how much the player actually does each thing.
- tone: the story's prevailing tone in a few words.
- themes: short phrases for the story's recurring themes.
- likes: what the player demonstrably enjoys -- each with a weight (low, medium, high) for how strongly they seem to feel and one clause of evidence citing what they actually did.
- avoids: what the player consistently steers away from in practice -- observed behavior only, each with weight and evidence. These are soft observations, not vetoes.
- attachments: named characters, places, factions, or objects the player keeps returning to -- name, kind, and a short note on why they seem to matter.
- engagement: bites_on lists the shapes of hooks this player pursues ("a mystery with a personal stake"); ignores lists shapes they let pass by.
- narrative: pacing (slow-burn vs action-forward), agency (drives the story vs reacts to it), register (the emotional payoffs that visibly land).
- notes: anything else a storyteller should know about this player, a few sentences.
At most {PROFILE_LIST_CAP} entries per list. Do not infer dislikes -- those are set by the player themselves, and a character acting averse to something in-story does not mean the player dislikes it.

Respond with ONLY valid JSON:
{{"playstyle": {{{", ".join(f'"{k}": 0' for k in PLAYSTYLE_KEYS)}}}, "tone": "...", "themes": [], "likes": [{{"text": "...", "weight": "low|medium|high", "evidence": "..."}}], "avoids": [{{"text": "...", "weight": "low|medium|high", "evidence": "..."}}], "attachments": [{{"name": "...", "kind": "...", "note": "..."}}], "engagement": {{"bites_on": [], "ignores": []}}, "narrative": {{"pacing": "...", "agency": "...", "register": "..."}}, "notes": "..."}}"""

    model_pref = config.get("thread_ai_model", "smartest")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return None
    if isinstance(parsed.get("profile"), dict):
        parsed = parsed["profile"]
    profile = _clean_profile(parsed, _default_profile())
    return None if _is_profile_empty(profile) else profile


def _history_block(data: dict) -> str:
    """Closed threads with their lasting consequences -- the story's memory.
    Generation builds on these instead of merely avoiding repeats."""
    past = []
    for entry in data.get("thread_history", [])[-6:]:
        if not entry.get("title"):
            continue
        fallout = str(entry.get("consequence") or entry.get("note") or "").strip()
        line = f'- "{entry.get("title", "")}" -- {entry.get("outcome", "")} (turn {entry.get("closed_turn", 0)})'
        if fallout:
            line += f": {fallout}"
        past.append(line)
    if not past:
        return ""
    return (
        "PREVIOUS THREADS AND THEIR CONSEQUENCES (the story's memory -- BUILD on "
        "these: callbacks, fallout, returning characters, debts coming due. Never "
        "repeat a premise, but let a past consequence visibly shape the new thread "
        "when one fits):\n" + "\n".join(past) + "\n\n"
    )


async def _generate_thread(state: dict, sdk, data: dict, avoid_previous_kind: bool = False,
                           allow_defer: bool = False, critique: str = "",
                           pivot_intent: str = "") -> dict:
    """Generate a new short-term thread from the profile, narrative direction,
    and the story's established material. Returns the module_data update dict:
    a fresh active thread, a defer (story not ready for new plot material), or
    the failure bookkeeping."""
    config = _config(state)
    turn = state.get("turn", 0)
    attempts = int(data.get("gen_attempts", 0) or 0) + 1
    profile = data.get("profile") or _default_profile()

    level = int(config.get("difficulty", 3) or 3)
    level = max(1, min(5, level))
    thread_max_turns = max(1, int(config.get("thread_max_turns", 12) or 12))

    different = (
        " The player ignored the previous thread entirely -- choose a clearly DIFFERENT kind of thread: "
        "different activity, different flavor of challenge."
        if avoid_previous_kind else ""
    )
    critique_block = (
        f"\n\nA previous candidate was rejected by a quality check: {critique} "
        "Design a different thread that addresses this."
        if critique else ""
    )
    pivot_block = (
        f"\n\nTHE PLAYER JUST CHANGED COURSE: {pivot_intent} "
        "Meet them there: design the thread to serve what they are heading toward "
        "-- it should travel with them and enrich their new pursuit, never pull "
        "them back to what they left behind."
        if pivot_intent else ""
    )
    defer_streak = int(data.get("defer_streak", 0) or 0)
    may_defer = allow_defer and defer_streak < DEFER_MAX_STREAK
    defer_block = (
        "\n\nIf the current scene is mid-climax or mid-crisis -- a moment where "
        "introducing new plot material would intrude rather than enrich -- respond "
        'instead with ONLY: {"defer": true}'
        if may_defer else ""
    )

    prompt = f"""You design short plot threads for a text RPG storyteller. A thread is a small self-contained arc the storyteller can weave in over roughly the next {thread_max_turns} turns: a hook that enters the story naturally, a challenge that opposes or complicates, and stakes. The player will see the thread openly, so make it enticing, not a spoiler-dependent twist.

PLAYER PROFILE (cater to their demonstrated preferences; weight marks how strongly the player feels -- give high-weight entries the most influence. Dislikes were set directly by the player: steer firmly away from them. Avoids are observed aversions: steer away unless the profile suggests otherwise. Attachments name what the player cares about -- prefer anchoring threads in them. engagement.ignores are hook shapes this player lets pass by -- avoid those shapes):
{json.dumps(profile, ensure_ascii=False)}

CHALLENGE DIFFICULTY:
{DIFFICULTY_GUIDANCE[level]}

{_direction_block(data)}STORY MATERIAL:
{_story_material(state)}

{_storylines_block(state)}{_character_roster(state)}{_history_block(data)}Design ONE new thread that fits the established genre, tone and current situation, plays to what this player enjoys, and injects challenge at the guided difficulty. GROUND IT: anchor the hook in established characters, places, factions, or unresolved hooks from the material above -- invent at most one new minor element, and only when nothing established fits. Where a past consequence or open question offers a natural seed, grow the thread from it.{different}{critique_block}{pivot_block}{defer_block}

Respond with ONLY valid JSON:
{{"title": "3-6 words", "hook": "how it surfaces in the story, one sentence", "challenge": "the complication or opposition, one sentence", "stakes": "what is at risk, one sentence", "appeal": "which player preference this serves, a few words"}}"""

    model_pref = config.get("thread_ai_model", "smartest")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)

    if may_defer and isinstance(parsed, dict) and parsed.get("defer") is True:
        print(f"[Plot Director] Turn {turn}: generation deferred -- story mid-climax.")
        return {
            "gen_attempts": 0,
            "defer_streak": defer_streak + 1,
            "next_thread_turn": turn + 2,
            "momentum": "observing",
        }

    title = str(parsed.get("title", "")).strip() if isinstance(parsed, dict) else ""
    hook = str(parsed.get("hook", "")).strip() if isinstance(parsed, dict) else ""
    challenge = str(parsed.get("challenge", "")).strip() if isinstance(parsed, dict) else ""

    if not (title and hook and challenge):
        status = "failed" if attempts >= GEN_MAX_ATTEMPTS else data.get("status", "observing")
        if status == "failed":
            print(f"[Plot Director] Thread generation failed {attempts} times; going dormant.")
        return {"gen_attempts": attempts, "status": status}

    print(f"[Plot Director] Turn {turn}: new thread ready -- {title}")
    return {
        "gen_attempts": 0,
        "status": "active",
        "momentum": "building",
        "ignored_streak": 0,
        "defer_streak": 0,
        "thread": _full_thread(
            id=f"t{turn}",
            title=title,
            hook=hook,
            challenge=challenge,
            stakes=str(parsed.get("stakes", "")).strip(),
            appeal=str(parsed.get("appeal", "")).strip(),
            status="active",
            created_turn=turn,
        ),
    }


async def _fit_check(state: dict, sdk, data: dict, candidate: dict) -> tuple[bool, str]:
    """Independent critic pass over a candidate thread: does it actually fit
    this story and this player? Fails open -- a malformed reply accepts, and
    the caller force-accepts after repeated rejections -- so an over-strict
    critic can never push the module dormant."""
    profile = data.get("profile") or _default_profile()
    shown = {k: candidate.get(k, "") for k in ("title", "hook", "challenge", "stakes", "appeal")}

    prompt = f"""You are a quality gate for plot threads in a text RPG. Judge ONE candidate thread strictly on fit, not on cleverness.

{_direction_block(data)}PLAYER PROFILE (dislikes are hard vetoes; avoids are soft observed aversions):
{json.dumps(profile, ensure_ascii=False)}

STORY MATERIAL:
{_story_material(state)}

{_storylines_block(state)}{_character_roster(state)}CANDIDATE THREAD:
{json.dumps(shown, ensure_ascii=False)}

Reject the candidate if ANY of these fail:
1. Tone/genre fit: it belongs in this story's genre and prevailing tone.
2. Grounding: its hook rises from established characters, places, or events -- not a disconnected new invention.
3. Continuity: it does not contradict anything established in the story.
4. Arc fit: it advances or complicates the narrative direction (skip this check if no direction is given).
5. It does not touch any player dislike.

Respond with ONLY valid JSON:
{{"verdict": "accept" | "reject", "critique": "1-2 sentences naming the failed check, only when rejecting"}}"""

    model_pref = _config(state).get("assessment_ai_model", "balanced")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict) or parsed.get("verdict") != "reject":
        return True, ""
    return False, str(parsed.get("critique") or "").strip()


async def _generate_checked_thread(state: dict, sdk, data: dict, avoid_previous_kind: bool = False,
                                   allow_defer: bool = False, pivot_intent: str = "") -> dict:
    """Generate a thread and run it through the fit-check critic. One rejection
    earns a critique-fed retry; a second rejection accepts anyway (the critic
    gates quality, it must never starve the module). Malformed generation
    replies keep the existing gen_attempts/dormancy bookkeeping untouched."""
    result = await _generate_thread(state, sdk, data, avoid_previous_kind, allow_defer,
                                    pivot_intent=pivot_intent)
    if "thread" not in result or not _config(state).get("fit_check_enabled", True):
        return result

    ok, critique = await _fit_check(state, sdk, data, result["thread"])
    if ok:
        return result

    retry = await _generate_thread(state, sdk, data, avoid_previous_kind,
                                   allow_defer=False, critique=critique,
                                   pivot_intent=pivot_intent)
    if "thread" not in retry:
        # The retry came back malformed; the first candidate is still a valid
        # thread, so use it rather than burning gen_attempts on a quality pass.
        return result

    ok, _ = await _fit_check(state, sdk, data, retry["thread"])
    if not ok:
        print("[Plot Director] Fit check rejected twice; accepting the second candidate.")
    return retry


async def _update_direction(state: dict, sdk, data: dict, closed_thread: dict | None = None,
                            outcome: str = "", note: str = "") -> tuple[dict | None, str, dict | None]:
    """Evolve the narrative direction, distill a closed thread's lasting
    consequence, and refresh the slow-moving deep profile fields (attachments,
    engagement, narrative, notes). Runs at thread close -- exactly when "what
    hooks work on this player" becomes learnable -- and once at the start of a
    story (closed_thread None) so the direction exists from the first turns
    instead of only after the first close.

    Returns (direction or None on garbage, consequence sentence, deep profile
    fields or None on garbage)."""
    config = _config(state)
    direction = _clean_direction({}, data.get("direction") or _default_direction())
    profile = data.get("profile") or _default_profile()

    if closed_thread is not None:
        event_block = f"""A PLOT THREAD JUST CLOSED ({outcome}):
Title: {closed_thread.get('title', '')}
Hook: {closed_thread.get('hook', '')}
Challenge: {closed_thread.get('challenge', '')}
Stakes: {closed_thread.get('stakes', '')}
How it ended: {note or '(it faded without resolution)'}

"""
        consequence_rule = ("- consequence: ONE sentence stating the lasting fallout of this thread's "
                            "outcome -- something a future thread could call back to (a debt, a grudge, "
                            "a change in the world). Write it even for expired or abandoned threads: "
                            "unfinished business is fallout too.\n")
    else:
        event_block = ("No plot thread has closed yet -- establish the INITIAL narrative direction "
                       "from the story material below. Keep it faithful to what is actually on the "
                       "page; where the story is still young, lean on the scenario and premise.\n\n")
        consequence_rule = ""

    prompt = f"""You maintain the long-term narrative direction of a text RPG -- a short living summary of where the story is heading, used to keep future plot threads coherent with each other and with everything already established. You also keep the slow-moving parts of the player's profile current.

CURRENT NARRATIVE DIRECTION (evolve it, don't rewrite from scratch; empty means the story is young):
{json.dumps({k: direction[k] for k in ("premise", "heading", "open_questions", "recurring_elements")}, ensure_ascii=False)}

CURRENT PLAYER PROFILE:
{json.dumps(profile, ensure_ascii=False)}

{event_block}{_storylines_block(state)}STORY MATERIAL:
{_story_material(state)}

Update:
{consequence_rule}- premise: 1-2 sentences: the larger arc the story seems to be telling.
- heading: one sentence: where events look to be going next.
- open_questions: up to {DIRECTION_LIST_CAP} short unresolved questions the story has raised.
- recurring_elements: up to {DIRECTION_LIST_CAP} named characters, places, factions, or motifs worth returning to.
- attachments: named story elements the player keeps gravitating to (name, kind, why they matter).
- engagement: bites_on -- hook shapes this player pursues (update with what this thread's fate reveals); ignores -- hook shapes they let pass by.
- narrative: pacing, agency, and emotional register preferences the play so far demonstrates.
- notes: anything else a storyteller should know about this player, a few sentences.
Keep everything grounded in what actually happened -- never invent.

Respond with ONLY valid JSON:
{{"consequence": "...", "premise": "...", "heading": "...", "open_questions": [], "recurring_elements": [], "attachments": [{{"name": "...", "kind": "...", "note": "..."}}], "engagement": {{"bites_on": [], "ignores": []}}, "narrative": {{"pacing": "...", "agency": "...", "register": "..."}}, "notes": "..."}}"""

    model_pref = config.get("assessment_ai_model", "balanced")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return None, note, None

    cleaned = _clean_direction(parsed, direction)
    cleaned["updated_turn"] = state.get("turn", 0)
    consequence = str(parsed.get("consequence") or "").strip() or note
    deep_fields = {
        key: parsed[key]
        for key in ("attachments", "engagement", "narrative", "notes")
        if key in parsed
    }
    return cleaned, consequence, deep_fields or None


def _finalize_thread(updates: dict, data: dict, thread: dict, outcome: str, turn: int,
                     note: str = "", consequence: str = "", cooldown: int = 0) -> None:
    """Close the active thread into history, clear the nudge, and start the
    quiet period (cooldown 0 keeps the old replace-immediately behavior). The
    caller regenerates afterwards (overwriting "thread" on success); writing
    the terminal thread here keeps state consistent when regeneration fails --
    the next pass then retries via the no-active-thread branch."""
    updates["thread"] = _full_thread(**{
        **{k: thread.get(k, v) for k, v in _empty_thread().items()},
        "status": outcome,
        "closed_turn": turn,
    })
    history = list(data.get("thread_history", []))
    history.append({
        "title": thread.get("title", ""),
        "outcome": outcome,
        "created_turn": int(thread.get("created_turn", 0) or 0),
        "closed_turn": turn,
        "note": note,
        "consequence": consequence,
    })
    updates["thread_history"] = history[-THREAD_HISTORY_CAP:]
    updates["pending_nudge"] = ""
    updates["last_closed_turn"] = turn
    updates["next_thread_turn"] = turn + max(0, int(cooldown))
    updates["ignored_streak"] = 0
    updates["momentum"] = "resolving" if outcome == "resolved" else "observing"

    # Abandonment feeds back only through thread_history (the consequences
    # list and the different-kind instruction) -- never through dislikes, which
    # are player-set only: ignoring a thread is too weak a signal to outweigh
    # what the player tells us directly.


async def _close_thread(state: dict, sdk, data: dict, updates: dict, thread: dict,
                        outcome: str, turn: int, note: str = "", cooldown: int = 0,
                        avoid_previous_kind: bool = False, pivot_intent: str = "") -> None:
    """Full close sequence: evolve the narrative direction (and the deep
    profile fields), record the thread's consequence, finalize into history
    with the quiet-period clock, and -- only when no cooldown applies --
    regenerate in the same pass."""
    direction, consequence, deep_fields = await _update_direction(
        state, sdk, {**data, **updates}, thread, outcome, note)
    if direction is not None:
        updates["direction"] = direction
    if deep_fields:
        base_profile = updates.get("profile") or data.get("profile") or _default_profile()
        updates["profile"] = _clean_profile(deep_fields, base_profile)
    _finalize_thread(updates, data, thread, outcome, turn,
                     note=note, consequence=consequence, cooldown=cooldown)
    if turn >= int(updates["next_thread_turn"]):
        updates.update(await _generate_checked_thread(
            state, sdk, {**data, **updates},
            avoid_previous_kind=avoid_previous_kind,
            allow_defer=not pivot_intent,  # a pivot means the player is moving NOW
            pivot_intent=pivot_intent))


async def on_gather_context(state: dict, sdk) -> dict:
    data = _own_data(state)
    if not data:
        return {"module_data": {MODULE_ID: _default_data()}}

    if data.get("schema") == 2:
        # v2 -> v3: additive, in place. _deep_merge adds the new keys; the
        # profile is rewritten in full to pick up its new fields while keeping
        # every learned value.
        print("[Plot Director] Migrated plot data v2 -> v3 (narrative direction added).")
        return {"module_data": {MODULE_ID: {
            "schema": SCHEMA_VERSION,
            "profile": _clean_profile({}, data.get("profile") or _default_profile()),
            "direction": _default_direction(),
            "last_closed_turn": 0,
            "next_thread_turn": 0,
            "defer_streak": 0,
            "direction_seed_attempts": 0,
            "last_pivot_turn": 0,
        }}}

    if data.get("schema") != SCHEMA_VERSION:
        # Legacy v1 save (3-act outline). Reset to the current default and
        # blank the dead keys -- "" replaces old dicts/lists under _deep_merge.
        fresh = _default_data()
        fresh.update({key: "" for key in LEGACY_KEYS})
        print("[Plot Director] Migrated legacy plot data (profile starts fresh).")
        return {"module_data": {MODULE_ID: fresh}}

    config = _config(state)
    thread = data.get("thread") or {}
    if (
        config.get("plot_enabled", True)
        and not data.get("suspended")
        and data.get("status") == "active"
        and thread.get("status") == "active"
    ):
        line = (
            "Background plot thread (entirely optional -- the player has not "
            "been told about it in-story; let it surface only where the scene "
            "naturally opens toward it, and never at the expense of what the "
            f"player is doing): \"{thread.get('title', '')}\". {thread.get('hook', '')} "
            f"Complication: {thread.get('challenge', '')}"
        )
        stall_threshold = max(1, int(config.get("stall_threshold", 3) or 3))
        if int(data.get("ignored_streak", 0) or 0) >= stall_threshold:
            line += (
                " The player has been pursuing other things -- keep this thread "
                "as faint background color unless they turn toward it."
            )
        nudge = str(data.get("pending_nudge") or "").strip()
        if nudge:
            line += f" If a natural opening appears this turn, one light way in: {nudge}"
        return {"context_string": line}
    return {}


async def on_librarian(state: dict, sdk) -> dict | None:
    config = _config(state)
    if not config.get("plot_enabled", True):
        return None
    if not state.get("history"):
        return None

    data = _own_data(state) or _default_data()
    if data.get("status") == "failed":
        return None
    # Suspended: freeze everything -- no assessments, no expiry, no generation.
    # The pending nudge was cleared when the player suspended.
    if data.get("suspended"):
        return None

    turn = state.get("turn", 0)
    updates: dict = {}

    # A nudge armed on an earlier turn was rendered into this turn's prompt;
    # consume it so it fires exactly once. _deep_merge never deletes keys, so
    # the empty string must be written explicitly.
    if data.get("pending_nudge") and int(data.get("last_nudge_turn", 0) or 0) < turn:
        updates["pending_nudge"] = ""

    # The narrative direction should exist from the story's first turns, not
    # only after the first thread closes: seed it once while it is empty
    # (covers fresh stories, mid-story adoption, and migrated v2 saves with a
    # thread already running). Two failed reads stop the retries.
    if (
        not str((data.get("direction") or {}).get("premise", "")).strip()
        and int(data.get("direction_seed_attempts", 0) or 0) < 2
    ):
        seeded, _, deep_fields = await _update_direction(state, sdk, data)
        if seeded is not None and seeded.get("premise"):
            seeded["updated_turn"] = turn
            updates["direction"] = seeded
            if deep_fields:
                updates["profile"] = _clean_profile(
                    deep_fields, data.get("profile") or _default_profile())
            print(f"[Plot Director] Turn {turn}: initial narrative direction established.")
        else:
            updates["direction_seed_attempts"] = int(data.get("direction_seed_attempts", 0) or 0) + 1

    thread = data.get("thread") or _empty_thread()
    cooldown = max(0, int(config.get("thread_cooldown_turns", 3) or 0))

    # No active thread (fresh save or previous thread closed): generate one,
    # unless the story is still in its quiet period -- those turns cost zero
    # LLM calls.
    if thread.get("status") != "active":
        if turn < int(data.get("next_thread_turn", 0) or 0):
            return {"module_data": {MODULE_ID: updates}} if updates else None
        # Joining a story already in progress with no profile yet: read the
        # story so far once, so the first thread caters to how the player has
        # actually been playing. Skipped for fresh stories (nothing to read)
        # and never retried -- a failed bootstrap just means starting blind.
        if turn >= 2 and _is_profile_empty(data.get("profile") or {}):
            bootstrapped = await _bootstrap_profile(state, sdk)
            if bootstrapped is not None:
                print(f"[Plot Director] Turn {turn}: profile bootstrapped from the story so far.")
                updates["profile"] = bootstrapped
        last_outcome = (data.get("thread_history") or [{}])[-1].get("outcome", "")
        updates.update(await _generate_checked_thread(
            state, sdk, {**data, **updates},
            avoid_previous_kind=last_outcome == "abandoned", allow_defer=True))
        return {"module_data": {MODULE_ID: updates}}

    # Deterministic expiry, checked before spending an assessment call.
    thread_max_turns = max(1, int(config.get("thread_max_turns", 12) or 12))
    if turn - int(thread.get("created_turn", 0) or 0) >= thread_max_turns:
        print(f"[Plot Director] Turn {turn}: thread expired -- {thread.get('title', '')}")
        await _close_thread(state, sdk, data, updates, thread, "expired", turn, cooldown=cooldown)
        return {"module_data": {MODULE_ID: updates}}

    frequency = max(1, int(config.get("assessment_frequency", 1) or 1))
    if turn == 0 or turn % frequency != 0 or turn <= int(thread.get("created_turn", 0) or 0):
        return {"module_data": {MODULE_ID: updates}} if updates else None

    history = state.get("history", [])
    latest = str(history[-1])[-4000:]
    earlier = "\n".join(str(h) for h in history[-3:-1])[-2000:]
    earlier_block = f"EARLIER NARRATION (context only):\n{earlier}\n\n" if earlier else ""

    player_input = ""
    for message in reversed(state.get("chat_messages", [])):
        if message.get("role") == "user" and str(message.get("content", "")).strip():
            player_input = str(message["content"])[-1000:]
            break

    # The seed pass above may have just written deep profile fields; build on
    # that version so this turn's assessment doesn't clobber them.
    profile = updates.get("profile") or data.get("profile") or _default_profile()

    prompt = f"""You maintain a live profile of the player and track one active plot thread in a text RPG. The thread is visible to the player; your profile drives what threads they get next.

ACTIVE PLOT THREAD:
Title: {thread.get('title', '')}
Hook: {thread.get('hook', '')}
Challenge: {thread.get('challenge', '')}
Stakes: {thread.get('stakes', '')}

CURRENT PLAYER PROFILE (update it with what this turn reveals; evolve, don't rewrite from scratch):
{json.dumps(profile, ensure_ascii=False)}

{_storylines_block(state)}{earlier_block}THIS TURN'S SCENE (judge this):
{latest}

LAST PLAYER INPUT:
{player_input or '(none)'}

Judge and update:
- thread_engaged: did the player or the scene meaningfully interact with the active thread this turn?
- thread_resolved: is the thread's challenge definitively over (overcome, defused, or conclusively failed)?
- player_pivot: has the player clearly COMMITTED to a different pursuit than the active thread -- a stated intention, a new goal, leaving the situation behind for good? A brief detour, a side errand, or a single scene elsewhere is NOT a pivot.
- pivot_intent: one sentence naming what the player is now pursuing (only when player_pivot is true).
- opportunity: does the scene end with a natural opening to bring the thread forward next turn?
- nudge: one subtle piece of story material (a rumor, an arrival, a discovery) that could pull the scene toward the thread without contradicting what the player is doing. Never an instruction to the player.
- momentum: building | steady | stalled
- profile: the updated fast-moving profile fields. playstyle values are integers 0-10 for what the player actually does. tone is the story's prevailing tone in a few words. themes are short phrases. likes are what the player demonstrably enjoys; avoids are what they consistently steer away from in practice -- both carry a weight (low, medium, high) for how strongly the player seems to feel and one clause of evidence citing what the player actually did (an entry without real evidence does not belong). At most {PROFILE_LIST_CAP} entries per list -- drop the least relevant to make room. Some entries may have been set directly by the player; preserve those (including their weight) unless the story clearly contradicts them. The profile's dislikes are set by the player themselves and are NOT yours to change -- do not include dislikes in your reply, and never add a like that contradicts one. A character acting averse to something in-story does not mean the player dislikes it. The profile's attachments, engagement, narrative, and notes fields are maintained elsewhere -- do not include them.

Respond with ONLY valid JSON:
{{"thread_engaged": false, "thread_resolved": false, "resolution_note": "one sentence, only if resolved", "player_pivot": false, "pivot_intent": "", "opportunity": false, "nudge": "...", "momentum": "steady", "profile": {{"playstyle": {{{", ".join(f'"{k}": 0' for k in PLAYSTYLE_KEYS)}}}, "tone": "...", "themes": [], "likes": [{{"text": "...", "weight": "low|medium|high", "evidence": "..."}}], "avoids": [{{"text": "...", "weight": "low|medium|high", "evidence": "..."}}]}}}}"""

    model_pref = config.get("assessment_ai_model", "balanced")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return {"module_data": {MODULE_ID: updates}} if updates else None

    engaged = bool(parsed.get("thread_engaged"))
    resolved = bool(parsed.get("thread_resolved"))
    pivot = bool(parsed.get("player_pivot"))
    pivot_intent = str(parsed.get("pivot_intent") or "").strip()
    opportunity = bool(parsed.get("opportunity"))
    nudge_text = str(parsed.get("nudge") or "").strip()
    resolution_note = str(parsed.get("resolution_note") or "").strip()
    momentum = parsed.get("momentum")
    if momentum not in MOMENTUM_VALUES:
        momentum = "steady" if engaged else "stalled"

    updates["profile"] = _clean_profile(parsed.get("profile"), profile)
    ignored_streak = 0 if engaged else int(data.get("ignored_streak", 0) or 0) + 1
    updates["ignored_streak"] = ignored_streak
    updates["momentum"] = momentum

    log = list(data.get("log", []))
    log.append({"turn": turn, "engaged": engaged, "note": resolution_note})
    updates["log"] = log[-MAX_LOG_ENTRIES:]

    if resolved:
        print(f"[Plot Director] Turn {turn}: thread resolved -- {thread.get('title', '')}")
        await _close_thread(state, sdk, data, updates, thread, "resolved", turn,
                            note=resolution_note, cooldown=cooldown)
        return {"module_data": {MODULE_ID: updates}}

    # The player has clearly committed to a different pursuit: don't wait for
    # the ignore-streak to grind through -- supersede the thread now and weave
    # one that meets them where they are going. No quiet period (the player is
    # actively moving), and a thrash guard so back-to-back pivots settle down.
    if (
        pivot
        and not engaged
        and pivot_intent
        and config.get("pivot_adapt", True)
        and turn - int(data.get("last_pivot_turn", 0) or 0) >= PIVOT_COOLDOWN_TURNS
    ):
        print(f"[Plot Director] Turn {turn}: player pivoted -- superseding {thread.get('title', '')}")
        updates["last_pivot_turn"] = turn
        await _close_thread(state, sdk, data, updates, thread, "superseded", turn,
                            note=pivot_intent, cooldown=0, pivot_intent=pivot_intent)
        return {"module_data": {MODULE_ID: updates}}

    abandon_after = max(
        int(config.get("abandon_after", 4) or 4),
        int(config.get("stall_threshold", 3) or 3) + 1,
    )
    if ignored_streak >= abandon_after:
        print(f"[Plot Director] Turn {turn}: thread abandoned by player -- {thread.get('title', '')}")
        # An ignored plot earns a longer breather before the next attempt.
        await _close_thread(state, sdk, data, updates, thread, "abandoned", turn,
                            cooldown=cooldown * ABANDON_COOLDOWN_FACTOR,
                            avoid_previous_kind=True)
        return {"module_data": {MODULE_ID: updates}}

    if nudge_text:
        stall_threshold = max(1, int(config.get("stall_threshold", 3) or 3))
        # Fires once per streak (exact hit); the streak keeps climbing toward
        # abandonment, so a nudge must not reset it.
        stalled = ignored_streak == stall_threshold
        opportune = (
            opportunity
            and config.get("opportunity_nudges", True)
            and turn - int(data.get("last_nudge_turn", 0) or 0) >= OPPORTUNITY_COOLDOWN_TURNS
        )
        if stalled or opportune:
            print(f"[Plot Director] Turn {turn}: nudge armed ({'stall' if stalled else 'opportunity'}).")
            updates["pending_nudge"] = nudge_text
            updates["last_nudge_turn"] = turn

    return {"module_data": {MODULE_ID: updates}}


def _profile_line(profile: dict) -> str:
    parts = []
    playstyle = profile.get("playstyle", {})
    top = sorted(
        ((k, v) for k, v in playstyle.items() if isinstance(v, int) and v > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )[:2]
    if top:
        parts.append("/".join(k for k, _ in top) + "-leaning")
    if profile.get("tone"):
        parts.append(f"tone: {profile['tone']}")
    likes = _clean_pref_list(profile.get("likes", []), [])
    likes.sort(key=lambda e: PREF_WEIGHTS.index(e["weight"]), reverse=True)
    if likes:
        parts.append("likes: " + ", ".join(e["text"] for e in likes[:2]))
    attachments = _clean_attachments(profile.get("attachments", []), [])
    if attachments:
        parts.append("drawn to: " + ", ".join(a["name"] for a in attachments[:2]))
    register = str((profile.get("narrative") or {}).get("register", "")).strip()
    if register:
        parts.append(f"register: {register}")
    return " · ".join(parts)


async def _command_regen(state: dict, sdk, data: dict) -> dict:
    """Player-requested reroll: close the active thread as 'rerolled' and weave
    a new one immediately -- the quiet period never applies to an explicit
    request, and the direction call is skipped to keep the command snappy. On
    a failed generation nothing is written back, so the old thread survives.
    Also revives a dormant 'failed' module on success."""
    if not data or data.get("schema") != SCHEMA_VERSION:
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn"}

    turn = state.get("turn", 0)
    updates: dict = {}
    thread = data.get("thread") or {}
    if thread.get("status") == "active":
        _finalize_thread(updates, data, thread, "rerolled", turn,
                         consequence="Set aside by the player.", cooldown=0)

    generated = await _generate_checked_thread(
        state, sdk, {**data, **updates, "gen_attempts": 0})
    if "thread" not in generated:
        return {"message": "[Plot] Couldn't weave a new thread -- nothing changed. Try again.", "signal": "end_turn"}

    updates.update(generated)
    updates["next_thread_turn"] = 0
    return {
        "message": f"[Plot] New thread: {generated['thread']['title']}",
        "signal": "end_turn",
        "module_data": {MODULE_ID: updates},
    }


async def _command_reset(state: dict, sdk) -> dict:
    """Cheat: wipe the plot data -- observed profile, narrative direction,
    thread history, log, and the active thread -- then rebuild from the story
    so far in the same pass: a fresh player analysis, a newly seeded
    direction, and a first thread. Preferences the player stated themselves
    (all dislikes, and likes carrying the player-set marker) survive the wipe
    and anchor the new analysis. Any step that fails is retried by the normal
    librarian machinery on the following turns. The widget empties itself
    optimistically while this runs."""
    turn = state.get("turn", 0)
    fresh = _default_data()
    notes = []

    # Player-stated taste is not the module's data to lose: it was typed in,
    # so it carries over and seeds the rebuilt profile.
    old_profile = _clean_profile({}, _own_data(state).get("profile") or _default_profile())
    kept_dislikes = old_profile["dislikes"]
    kept_likes = [e for e in old_profile["likes"] if e.get("evidence") == PLAYER_SET_EVIDENCE]
    preserved = {"likes": kept_likes, "dislikes": kept_dislikes}
    fresh["profile"] = _merge_player_prefs(fresh["profile"], kept_likes, kept_dislikes)

    if turn >= 2 and state.get("history"):
        bootstrapped = await _bootstrap_profile(state, sdk, preserved=preserved)
        if bootstrapped is not None:
            fresh["profile"] = _merge_player_prefs(bootstrapped, kept_likes, kept_dislikes)
            notes.append("player profile rebuilt from the story so far")
        else:
            notes.append("player analysis failed -- profile starts blank")
    else:
        notes.append("story too young to analyze -- profile starts blank")
    if kept_likes or kept_dislikes:
        notes.append("your stated likes/dislikes carried over")

    seeded, _, deep_fields = await _update_direction(state, sdk, fresh)
    if seeded is not None and seeded.get("premise"):
        seeded["updated_turn"] = turn
        fresh["direction"] = seeded
        if deep_fields:
            fresh["profile"] = _clean_profile(deep_fields, fresh["profile"])
        notes.append("new story direction seeded")
    else:
        notes.append("direction seeding failed -- it retries next turn")

    fresh.update(await _generate_checked_thread(state, sdk, fresh))
    thread = fresh.get("thread") or {}
    if thread.get("status") == "active":
        notes.append(f"new thread woven: {thread.get('title', '')}")
    else:
        notes.append("thread generation failed -- it retries next turn")

    print(f"[Plot Director] Turn {turn}: plot data reset by the player.")
    return {
        "message": "[Plot] Reset complete -- " + "; ".join(notes) + ".",
        "signal": "end_turn",
        "module_data": {MODULE_ID: fresh},
        # Deep-merge can't delete: every top-level key is replaced wholesale so
        # the old profile, history, and log are actually gone.
        "module_data_replace": list(fresh.keys()),
    }


def _command_suspend(state: dict, data: dict) -> dict:
    """Freeze plot direction: the librarian pass and the context line go quiet
    until resume. The armed nudge is cleared -- it was written for a scene that
    may be long gone by the time the player resumes."""
    if not data or data.get("schema") != SCHEMA_VERSION:
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn"}
    if data.get("suspended"):
        return {"message": "[Plot] Plot direction is already suspended.", "signal": "end_turn"}
    return {
        "message": "[Plot] Plot direction suspended -- the thread is frozen until /plot resume.",
        "signal": "end_turn",
        "module_data": {MODULE_ID: {
            "suspended": True,
            "suspended_turn": state.get("turn", 0),
            "pending_nudge": "",
        }},
    }


def _command_resume(state: dict, data: dict) -> dict:
    """Unfreeze plot direction. The active thread's creation turn shifts
    forward by the paused duration so the expiry clock resumes where it
    stopped instead of expiring the thread on the next librarian pass."""
    if not data or data.get("schema") != SCHEMA_VERSION:
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn"}
    if not data.get("suspended"):
        return {"message": "[Plot] Plot direction is not suspended.", "signal": "end_turn"}

    turn = state.get("turn", 0)
    updates: dict = {"suspended": False, "suspended_turn": 0}
    paused = max(0, turn - int(data.get("suspended_turn", 0) or 0))
    thread = data.get("thread") or {}
    if thread.get("status") == "active":
        created = min(turn, int(thread.get("created_turn", 0) or 0) + paused)
        updates["thread"] = _full_thread(**{
            **{k: thread.get(k, v) for k, v in _empty_thread().items()},
            "created_turn": created,
        })
    # A quiet period that was still running when the player suspended resumes
    # where it stopped, same as the thread's expiry clock.
    next_thread_turn = int(data.get("next_thread_turn", 0) or 0)
    if next_thread_turn > int(data.get("suspended_turn", 0) or 0):
        updates["next_thread_turn"] = next_thread_turn + paused
    return {
        "message": "[Plot] Plot direction resumed.",
        "signal": "end_turn",
        "module_data": {MODULE_ID: updates},
    }


PROFILE_USAGE = (
    "[Plot] Usage: /plot profile tone <text> | "
    "/plot profile themes <add|remove> <text> | "
    "/plot profile <likes|dislikes> <add|remove> [low|medium|high] <text> | "
    "/plot profile avoids remove <text> | "
    "/plot profile <pacing|agency|register|notes> <text>"
)


def _command_profile_edit(data: dict, args: list[str]) -> dict:
    """Player edits to the story profile. List entries are matched with
    collapsed whitespace, case-insensitively. Re-adding an existing
    like/dislike with a different weight updates the weight in place; adds
    past the cap evict the lowest-weight (themes: oldest) entry. Failed
    edits write nothing back."""
    if not data or data.get("schema") != SCHEMA_VERSION:
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn"}
    if not args:
        return {"message": PROFILE_USAGE, "signal": "end_turn"}

    # Normalize the stored profile as the "previous" side: passing it as the
    # parsed side would discard dislikes, which _clean_profile treats as
    # player-authority and never accepts from the parsed reply.
    profile = _clean_profile({}, data.get("profile") or _default_profile())
    field = args[0].lower()

    if field == "tone":
        tone = " ".join(args[1:]).strip()[:PROFILE_ENTRY_MAX_CHARS]
        profile["tone"] = tone
        message = f'[Plot] Tone set to "{tone}".' if tone else "[Plot] Tone cleared."
    elif field in NARRATIVE_KEYS:
        text = " ".join(args[1:]).strip()[:PROFILE_ENTRY_MAX_CHARS]
        profile["narrative"] = {**profile["narrative"], field: text}
        message = f'[Plot] {field.capitalize()} set to "{text}".' if text else f"[Plot] {field.capitalize()} cleared."
    elif field == "notes":
        text = " ".join(args[1:]).strip()[:PROFILE_NOTES_MAX_CHARS]
        profile["notes"] = text
        message = "[Plot] Notes updated." if text else "[Plot] Notes cleared."
    elif field == "avoids" and len(args) >= 3 and args[1].lower() == "remove":
        text = " ".join(args[2:]).strip()
        kept = [e for e in profile["avoids"] if _norm_entry(e["text"]) != _norm_entry(text)]
        if len(kept) == len(profile["avoids"]):
            return {"message": f'[Plot] "{text}" is not in avoids.', "signal": "end_turn"}
        profile["avoids"] = kept
        message = f'[Plot] Removed "{text}" from avoids.'
    elif field == "themes" and len(args) >= 3 and args[1].lower() in ("add", "remove"):
        op = args[1].lower()
        text = " ".join(args[2:]).strip()[:PROFILE_ENTRY_MAX_CHARS]
        if not text:
            return {"message": PROFILE_USAGE, "signal": "end_turn"}
        entries = list(profile["themes"])
        if op == "add":
            if any(_norm_entry(e) == _norm_entry(text) for e in entries):
                return {"message": f'[Plot] "{text}" is already in themes.', "signal": "end_turn"}
            entries.append(text)
            entries = entries[-PROFILE_LIST_CAP:]
            message = f'[Plot] Added "{text}" to themes.'
        else:
            kept = [e for e in entries if _norm_entry(e) != _norm_entry(text)]
            if len(kept) == len(entries):
                return {"message": f'[Plot] "{text}" is not in themes.', "signal": "end_turn"}
            entries = kept
            message = f'[Plot] Removed "{text}" from themes.'
        profile["themes"] = entries
    elif field in ("likes", "dislikes") and len(args) >= 3 and args[1].lower() in ("add", "remove"):
        op = args[1].lower()
        rest = args[2:]
        weight = "medium"
        if op == "add" and len(rest) >= 2 and rest[0].lower() in PREF_WEIGHTS:
            weight = rest[0].lower()
            rest = rest[1:]
        text = " ".join(rest).strip()[:PROFILE_ENTRY_MAX_CHARS]
        if not text:
            return {"message": PROFILE_USAGE, "signal": "end_turn"}
        entries = list(profile[field])
        if op == "add":
            existing = next((e for e in entries if _norm_entry(e["text"]) == _norm_entry(text)), None)
            if existing is not None:
                if existing["weight"] == weight:
                    return {"message": f'[Plot] "{existing["text"]}" is already in {field} ({weight}).', "signal": "end_turn"}
                existing["weight"] = weight
                message = f'[Plot] "{existing["text"]}" set to {weight}.'
            else:
                # A like and a dislike can't coexist: adding to one side moves
                # the entry out of the other.
                other = "dislikes" if field == "likes" else "likes"
                kept = [e for e in profile[other] if _norm_entry(e["text"]) != _norm_entry(text)]
                if len(kept) != len(profile[other]):
                    profile[other] = kept
                    message = f'[Plot] Moved "{text}" from {other} to {field} ({weight}).'
                else:
                    message = f'[Plot] Added "{text}" to {field} ({weight}).'
                entry = {"text": text, "weight": weight}
                if field == "likes":
                    entry["evidence"] = PLAYER_SET_EVIDENCE
                entries.append(entry)
                entries = _cap_prefs(entries)
            # A stated preference supersedes the observed aversion either way:
            # a matching avoid is promoted (dislike) or contradicted (like).
            avoids_kept = [e for e in profile["avoids"] if _norm_entry(e["text"]) != _norm_entry(text)]
            if len(avoids_kept) != len(profile["avoids"]):
                profile["avoids"] = avoids_kept
        else:
            kept = [e for e in entries if _norm_entry(e["text"]) != _norm_entry(text)]
            if len(kept) == len(entries):
                return {"message": f'[Plot] "{text}" is not in {field}.', "signal": "end_turn"}
            entries = kept
            message = f'[Plot] Removed "{text}" from {field}.'
        profile[field] = entries
    else:
        return {"message": PROFILE_USAGE, "signal": "end_turn"}

    return {
        "message": message,
        "signal": "end_turn",
        "module_data": {MODULE_ID: {"profile": profile}},
    }


async def on_command_plot(args: list[str], state: dict, sdk) -> dict:
    config = _config(state)
    data = _own_data(state)

    if not config.get("plot_enabled", True):
        return {"message": "[Plot] Plot direction is inactive.", "signal": "end_turn"}

    if args and args[0].lower() in ("suspend", "pause"):
        return _command_suspend(state, data)

    if args and args[0].lower() in ("resume", "unpause"):
        return _command_resume(state, data)

    if args and args[0].lower() in ("regen", "reroll", "new"):
        if data.get("suspended"):
            return {"message": "[Plot] Plot direction is suspended -- /plot resume first.", "signal": "end_turn"}
        return await _command_regen(state, sdk, data)

    if args and args[0].lower() == "reset":
        # Deliberately works from any state (suspended, dormant, legacy) --
        # a full reset is the escape hatch.
        if len(args) < 2 or args[1].lower() != "confirm":
            return {"message": (
                "[Plot] This clears the plot data -- observed profile, narrative "
                "direction, thread history, and the active thread -- then rebuilds "
                "everything from the story so far. Likes and dislikes you added "
                "yourself are kept and feed the new analysis. Run "
                "'/plot reset confirm' to proceed."
            ), "signal": "end_turn"}
        return await _command_reset(state, sdk)

    if args and args[0].lower() == "profile":
        return _command_profile_edit(data, args[1:])

    if args and args[0].lower() == "challenge":
        thread = data.get("thread") or {}
        if thread.get("status") != "active" or not thread.get("challenge"):
            return {"message": "[Plot] No active thread challenge to reveal.", "signal": "end_turn"}
        return {
            "message": f"[Plot] Challenge (spoiler): {thread['challenge']}",
            "signal": "end_turn",
        }

    if args and args[0].lower() == "direction":
        direction = data.get("direction") or {}
        premise = str(direction.get("premise", "")).strip()
        if not premise:
            return {"message": "[Plot] No story direction yet -- it takes shape within the first turns.", "signal": "end_turn"}
        lines = [f"[Plot] Story direction (spoiler): {premise}"]
        heading = str(direction.get("heading", "")).strip()
        if heading:
            lines.append(f"Heading: {heading}")
        questions = [str(q).strip() for q in direction.get("open_questions", []) if str(q).strip()]
        if questions:
            lines.append("Open questions: " + "; ".join(questions))
        recurring = [str(r).strip() for r in direction.get("recurring_elements", []) if str(r).strip()]
        if recurring:
            lines.append("Recurring elements: " + ", ".join(recurring))
        return {"message": "\n".join(lines), "signal": "end_turn"}

    if data.get("status") == "failed":
        return {"message": "[Plot] Plot direction is inactive.", "signal": "end_turn"}
    if data.get("suspended"):
        return {
            "message": f"[Plot] Suspended since turn {data.get('suspended_turn', 0)} -- /plot resume to continue.",
            "signal": "end_turn",
        }
    if data.get("status") != "active":
        return {"message": "[Plot] Watching how you play -- the first plot thread arrives shortly.", "signal": "end_turn"}

    thread = data.get("thread") or {}
    if thread.get("status") != "active":
        next_thread_turn = int(data.get("next_thread_turn", 0) or 0)
        if state.get("turn", 0) < next_thread_turn:
            return {
                "message": f"[Plot] Between threads -- letting the story breathe (a new thread arrives around turn {next_thread_turn}).",
                "signal": "end_turn",
            }
        return {"message": "[Plot] Between threads -- a new one is being woven.", "signal": "end_turn"}

    lines = [f"[Plot] Active thread: {thread.get('title', '')} (since turn {thread.get('created_turn', 0)})"]
    if thread.get("hook"):
        lines.append(f"Hook: {thread['hook']}")
    if thread.get("challenge"):
        # The opposition stays a surprise unless the player opts in.
        lines.append("Challenge: (spoiler -- '/plot challenge' reveals it)")
    if thread.get("stakes"):
        lines.append(f"Stakes: {thread['stakes']}")

    streak = int(data.get("ignored_streak", 0) or 0)
    attention = "engaged" if streak == 0 else f"ignored for {streak} checks"
    lines.append(f"Momentum: {data.get('momentum', 'steady')} · Attention: {attention}")

    profile_line = _profile_line(data.get("profile") or {})
    if profile_line:
        lines.append(f"Profile: {profile_line}")

    if str((data.get("direction") or {}).get("premise", "")).strip():
        # The arc is spoiler territory, same as the challenge.
        lines.append("Story direction: (spoiler -- '/plot direction' reveals it)")

    recent = [
        f'"{entry.get("title", "")}" {entry.get("outcome", "")} (t{entry.get("closed_turn", 0)})'
        for entry in data.get("thread_history", [])[-3:]
        if entry.get("title")
    ]
    if recent:
        lines.append("Recent threads: " + " · ".join(recent))

    return {"message": "\n".join(lines), "signal": "end_turn"}
