"""Plot Director v2 -- observe-then-respond plot threading.

Instead of imposing a hidden long arc, the module watches the story the player
is actually creating. After each storyteller turn a background LLM pass updates
a live profile (playstyle weights, tone, themes, likes/dislikes) and judges the
single active short-term plot thread: engaged, resolved, or ignored. Threads
that resolve, expire, or get ignored long enough are replaced with a fresh one
generated from the profile -- catering to what the player enjoys while
injecting challenge scaled by the difficulty setting.

The active thread is fully visible: a soft context line keeps the storyteller
aware of it every turn, /plot and the sidebar widget show it to the player, and
one-turn nudges fire only when the thread stalls or a natural opening appears.
"""
import json

MODULE_ID = "wb_plot_director"

SCHEMA_VERSION = 2
PLAYSTYLE_KEYS = ("combat", "diplomacy", "exploration", "mystery", "social", "intrigue")
PROFILE_LIST_CAP = 8
PROFILE_ENTRY_MAX_CHARS = 60
THREAD_HISTORY_CAP = 10
MAX_LOG_ENTRIES = 20
GEN_MAX_ATTEMPTS = 3
OPPORTUNITY_COOLDOWN_TURNS = 2

MOMENTUM_VALUES = ("observing", "building", "steady", "stalled", "resolving")

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
        "likes": [],
        "dislikes": [],
    }


def _default_data() -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "status": "observing",  # observing | active | failed
        "gen_attempts": 0,
        "profile": _default_profile(),
        "thread": _empty_thread(),
        "thread_history": [],
        "momentum": "observing",
        "ignored_streak": 0,
        "pending_nudge": "",
        "last_nudge_turn": 0,
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


def _clean_profile(parsed, previous: dict) -> dict:
    """Sanitize an LLM profile reply; fall back to previous values on garbage.
    Always returns the full profile shape (all playstyle keys, capped lists)."""
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
    return {
        "playstyle": playstyle,
        "tone": tone,
        "themes": _clean_str_list(parsed.get("themes"), previous.get("themes", [])),
        "likes": _clean_str_list(parsed.get("likes"), previous.get("likes", [])),
        "dislikes": _clean_str_list(parsed.get("dislikes"), previous.get("dislikes", [])),
    }


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


def _story_material(state: dict) -> str:
    """Collect the material threads are woven from: scenario, world rules/lore,
    and the most recent scenes (newest always in full)."""
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

    history = state.get("history", [])
    if history:
        latest = str(history[-1])[-4000:]
        earlier = "\n".join(str(h) for h in history[-6:-1])[-3000:]
        story = (earlier + "\n" if earlier else "") + latest
        parts.append(f"<story_so_far>\n{story}\n</story_so_far>")

    return "\n\n".join(parts)


async def _generate_thread(state: dict, sdk, data: dict, avoid_previous_kind: bool = False) -> dict:
    """Generate a new short-term thread from the profile. Returns the
    module_data update dict (either a fresh active thread or the failure
    bookkeeping)."""
    config = _config(state)
    turn = state.get("turn", 0)
    attempts = int(data.get("gen_attempts", 0) or 0) + 1
    profile = data.get("profile") or _default_profile()

    level = int(config.get("difficulty", 3) or 3)
    level = max(1, min(5, level))
    thread_max_turns = max(1, int(config.get("thread_max_turns", 12) or 12))

    history_lines = ""
    past = [
        f'- "{entry.get("title", "")}" -- {entry.get("outcome", "")} (turn {entry.get("closed_turn", 0)})'
        for entry in data.get("thread_history", [])[-6:]
        if entry.get("title")
    ]
    if past:
        history_lines = "PAST THREADS (do not repeat these):\n" + "\n".join(past) + "\n\n"

    different = (
        " The player ignored the previous thread entirely -- choose a clearly DIFFERENT kind of thread: "
        "different activity, different flavor of challenge."
        if avoid_previous_kind else ""
    )

    prompt = f"""You design short plot threads for a text RPG storyteller. A thread is a small self-contained arc the storyteller can weave in over roughly the next {thread_max_turns} turns: a hook that enters the story naturally, a challenge that opposes or complicates, and stakes. The player will see the thread openly, so make it enticing, not a spoiler-dependent twist.

PLAYER PROFILE (cater to their demonstrated preferences):
{json.dumps(profile, ensure_ascii=False)}

CHALLENGE DIFFICULTY:
{DIFFICULTY_GUIDANCE[level]}

STORY MATERIAL:
{_story_material(state)}

{_storylines_block(state)}{history_lines}Design ONE new thread that fits the established genre, tone and current situation, plays to what this player enjoys, and injects challenge at the guided difficulty.{different}

Respond with ONLY valid JSON:
{{"title": "3-6 words", "hook": "how it surfaces in the story, one sentence", "challenge": "the complication or opposition, one sentence", "stakes": "what is at risk, one sentence", "appeal": "which player preference this serves, a few words"}}"""

    model_pref = config.get("thread_ai_model", "smartest")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
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


def _finalize_thread(updates: dict, data: dict, thread: dict, outcome: str, turn: int, note: str = "") -> None:
    """Close the active thread into history and clear the nudge. The caller
    regenerates afterwards (overwriting "thread" on success); writing the
    terminal thread here keeps state consistent when regeneration fails --
    the next pass then retries via the no-active-thread branch. Profile
    feedback for abandonment also happens here."""
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
    })
    updates["thread_history"] = history[-THREAD_HISTORY_CAP:]
    updates["pending_nudge"] = ""

    if outcome == "abandoned":
        profile = updates.get("profile") or data.get("profile") or _default_profile()
        profile = json.loads(json.dumps(profile))  # deep copy; mutated below
        flavor = thread.get("appeal") or str(thread.get("challenge", ""))[:40]
        dislikes = list(profile.get("dislikes", []))
        dislikes.append(f'ignored thread: "{thread.get("title", "")}" ({flavor})')
        profile["dislikes"] = dislikes[-PROFILE_LIST_CAP:]
        updates["profile"] = profile


async def on_gather_context(state: dict, sdk) -> dict:
    data = _own_data(state)
    if not data:
        return {"module_data": {MODULE_ID: _default_data()}}

    if data.get("schema") != SCHEMA_VERSION:
        # Legacy v1 save (3-act outline). Reset to the v2 default and blank the
        # dead keys -- "" replaces old dicts/lists under _deep_merge.
        fresh = _default_data()
        fresh.update({key: "" for key in LEGACY_KEYS})
        print("[Plot Director] Migrated legacy plot data to v2 (profile starts fresh).")
        return {"module_data": {MODULE_ID: fresh}}

    config = _config(state)
    thread = data.get("thread") or {}
    if (
        config.get("plot_enabled", True)
        and data.get("status") == "active"
        and thread.get("status") == "active"
    ):
        return {"context_string": (
            "Ongoing plot thread (optional material -- weave it in only where it "
            f"fits naturally): \"{thread.get('title', '')}\". {thread.get('hook', '')} "
            f"Complication: {thread.get('challenge', '')}"
        )}
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

    turn = state.get("turn", 0)
    updates: dict = {}

    # A nudge armed on an earlier turn was rendered into this turn's prompt;
    # consume it so it fires exactly once. _deep_merge never deletes keys, so
    # the empty string must be written explicitly.
    if data.get("pending_nudge") and int(data.get("last_nudge_turn", 0) or 0) < turn:
        updates["pending_nudge"] = ""

    thread = data.get("thread") or _empty_thread()

    # No active thread (fresh save or previous thread closed): generate one.
    if thread.get("status") != "active":
        last_outcome = (data.get("thread_history") or [{}])[-1].get("outcome", "")
        updates.update(await _generate_thread(state, sdk, data, avoid_previous_kind=last_outcome == "abandoned"))
        return {"module_data": {MODULE_ID: updates}}

    # Deterministic expiry, checked before spending an assessment call.
    thread_max_turns = max(1, int(config.get("thread_max_turns", 12) or 12))
    if turn - int(thread.get("created_turn", 0) or 0) >= thread_max_turns:
        print(f"[Plot Director] Turn {turn}: thread expired -- {thread.get('title', '')}")
        _finalize_thread(updates, data, thread, "expired", turn)
        updates.update(await _generate_thread(state, sdk, {**data, **updates}))
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

    profile = data.get("profile") or _default_profile()

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
- opportunity: does the scene end with a natural opening to bring the thread forward next turn?
- nudge: one subtle piece of story material (a rumor, an arrival, a discovery) that could pull the scene toward the thread without contradicting what the player is doing. Never an instruction to the player.
- momentum: building | steady | stalled
- profile: the FULL updated profile. playstyle values are integers 0-10 for what the player actually does. tone is the story's prevailing tone in a few words. themes, likes and dislikes are short phrases, at most 8 each -- drop the least relevant to make room.

Respond with ONLY valid JSON:
{{"thread_engaged": false, "thread_resolved": false, "resolution_note": "one sentence, only if resolved", "opportunity": false, "nudge": "...", "momentum": "steady", "profile": {{"playstyle": {{"combat": 0, "diplomacy": 0, "exploration": 0, "mystery": 0, "social": 0, "intrigue": 0}}, "tone": "...", "themes": [], "likes": [], "dislikes": []}}}}"""

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
        updates["momentum"] = "resolving"
        _finalize_thread(updates, data, thread, "resolved", turn, note=resolution_note)
        updates.update(await _generate_thread(state, sdk, {**data, **updates}))
        return {"module_data": {MODULE_ID: updates}}

    abandon_after = max(
        int(config.get("abandon_after", 4) or 4),
        int(config.get("stall_threshold", 2) or 2) + 1,
    )
    if ignored_streak >= abandon_after:
        print(f"[Plot Director] Turn {turn}: thread abandoned by player -- {thread.get('title', '')}")
        _finalize_thread(updates, data, thread, "abandoned", turn)
        updates.update(await _generate_thread(state, sdk, {**data, **updates}, avoid_previous_kind=True))
        return {"module_data": {MODULE_ID: updates}}

    if nudge_text:
        stall_threshold = max(1, int(config.get("stall_threshold", 2) or 2))
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


async def on_render_prompt_block(block: dict, state: dict, sdk) -> dict:
    if block.get("id") != "plot_nudge":
        return {"content": ""}
    nudge = str(_own_data(state).get("pending_nudge") or "").strip()
    if not nudge:
        return {"content": ""}
    return {"content": (
        "Optional storytelling suggestion -- use it ONLY if it fits the player's "
        f"action and the scene naturally; otherwise ignore it entirely: {nudge} "
        "Weave it in subtly (a rumor, a passerby, an object, an opening). "
        "Never force events or override what the player is doing, and never "
        "mention that a suggestion exists."
    )}


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
    likes = profile.get("likes", [])[:2]
    if likes:
        parts.append("likes: " + ", ".join(likes))
    return " · ".join(parts)


async def on_command_plot(args: list[str], state: dict, sdk) -> dict:
    config = _config(state)
    data = _own_data(state)

    if not config.get("plot_enabled", True) or data.get("status") == "failed":
        return {"message": "[Plot] Plot direction is inactive.", "signal": "end_turn"}
    if data.get("status") != "active":
        return {"message": "[Plot] Watching how you play -- the first plot thread arrives shortly.", "signal": "end_turn"}

    thread = data.get("thread") or {}
    if thread.get("status") != "active":
        return {"message": "[Plot] Between threads -- a new one is being woven.", "signal": "end_turn"}

    lines = [f"[Plot] Active thread: {thread.get('title', '')} (since turn {thread.get('created_turn', 0)})"]
    if thread.get("hook"):
        lines.append(f"Hook: {thread['hook']}")
    if thread.get("challenge"):
        lines.append(f"Challenge: {thread['challenge']}")
    if thread.get("stakes"):
        lines.append(f"Stakes: {thread['stakes']}")

    streak = int(data.get("ignored_streak", 0) or 0)
    attention = "engaged" if streak == 0 else f"ignored for {streak} checks"
    lines.append(f"Momentum: {data.get('momentum', 'steady')} · Attention: {attention}")

    profile_line = _profile_line(data.get("profile") or {})
    if profile_line:
        lines.append(f"Profile: {profile_line}")

    recent = [
        f'"{entry.get("title", "")}" {entry.get("outcome", "")} (t{entry.get("closed_turn", 0)})'
        for entry in data.get("thread_history", [])[-3:]
        if entry.get("title")
    ]
    if recent:
        lines.append("Recent threads: " + " · ".join(recent))

    return {"message": "\n".join(lines), "signal": "end_turn"}
