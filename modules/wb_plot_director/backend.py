"""Plot Director -- keeps a hidden overarching plotline alive in the background.

On the first turn it distills a 3-act outline (acts -> beats) from whatever
material exists: scenario, world rules/lore, and the opening scenes. After each
turn a background LLM pass judges whether the latest scene advanced the current
beat. Only when the plot has stalled for several checks (or a natural opening
appears) does it arm a one-turn nudge that the storyteller sees as an optional
suggestion; on quiet turns the storyteller prompt is untouched.

The plot is fluid: sustained player redirection (two consecutive drift flags)
triggers a re-plan that rewrites the remaining beats to embrace the new
direction, preserving completed beats as history. The outline is never shown to
the player -- /plot reports only vague, spoiler-safe progress.
"""
import json

MODULE_ID = "wb_plot_director"

MAX_LOG_ENTRIES = 30
MAX_COMPLETED_BEATS = 40
OUTLINE_MAX_ATTEMPTS = 3
DRIFT_STREAK_TO_REPLAN = 2
REPLAN_MIN_GAP_TURNS = 5
OPPORTUNITY_COOLDOWN_TURNS = 2

MOMENTUM_VALUES = ("building", "steady", "stalled", "resolving")


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


def _default_data() -> dict:
    return {
        "outline": {},
        "status": "pending",
        "outline_created_turn": 0,
        "outline_attempts": 0,
        "position": {"act": 1, "beat_index": 0},
        "beats_completed": [],
        "stall_count": 0,
        "drift_streak": 0,
        "pending_nudge": "",
        "last_nudge_turn": 0,
        "replan_count": 0,
        "last_replan_turn": 0,
        "momentum": "building",
        "assessment_log": [],
    }


def _normalize_outline(parsed: dict) -> dict | None:
    """Validate an LLM outline reply and assign beat ids/statuses.

    Beats may carry a status from a re-plan reply ("done" beats are preserved
    history); everything else becomes pending, with the first non-done beat
    marked active.
    """
    acts_in = parsed.get("acts")
    if not isinstance(acts_in, list) or not acts_in:
        return None

    acts = []
    first_open = None
    for ai, act in enumerate(acts_in, start=1):
        if not isinstance(act, dict):
            return None
        beats_in = act.get("beats")
        if not isinstance(beats_in, list) or not beats_in:
            return None
        beats = []
        for bi, beat in enumerate(beats_in, start=1):
            if isinstance(beat, str):
                beat = {"description": beat}
            if not isinstance(beat, dict):
                return None
            description = str(beat.get("description", "")).strip()
            if not description:
                return None
            status = "done" if beat.get("status") == "done" else "pending"
            if status == "pending" and first_open is None:
                first_open = (ai, bi - 1)
            beats.append({"id": f"a{ai}b{bi}", "description": description, "status": status})
        acts.append({
            "title": str(act.get("title", f"Act {ai}")).strip() or f"Act {ai}",
            "goal": str(act.get("goal", "")).strip(),
            "beats": beats,
        })

    if first_open is None:
        return None
    act_no, beat_index = first_open
    acts[act_no - 1]["beats"][beat_index]["status"] = "active"
    return {
        "outline": {
            "premise": str(parsed.get("premise", "")).strip(),
            "driving_tension": str(parsed.get("driving_tension", "")).strip(),
            "acts": acts,
        },
        "position": {"act": act_no, "beat_index": beat_index},
    }


def _current_beat(data: dict):
    """Return (act_dict, beat_dict, act_number, total_acts) or None when the
    position points past the outline (story resolved)."""
    acts = data.get("outline", {}).get("acts", [])
    position = data.get("position", {})
    act_no = int(position.get("act", 1) or 1)
    beat_index = int(position.get("beat_index", 0) or 0)
    if act_no < 1 or act_no > len(acts):
        return None
    act = acts[act_no - 1]
    beats = act.get("beats", [])
    if beat_index < 0 or beat_index >= len(beats):
        return None
    return act, beats[beat_index], act_no, len(acts)


def _advance_position(outline: dict, position: dict) -> dict:
    """Mark the current beat done and move to the next beat/act. Mutates the
    outline's beat statuses in place; returns the new position (act past the
    last act means the outline is fully resolved)."""
    acts = outline.get("acts", [])
    act_no = int(position.get("act", 1) or 1)
    beat_index = int(position.get("beat_index", 0) or 0)

    if 1 <= act_no <= len(acts):
        beats = acts[act_no - 1].get("beats", [])
        if 0 <= beat_index < len(beats):
            beats[beat_index]["status"] = "done"
        beat_index += 1
        while act_no <= len(acts) and beat_index >= len(acts[act_no - 1].get("beats", [])):
            act_no += 1
            beat_index = 0
        if act_no <= len(acts):
            acts[act_no - 1]["beats"][beat_index]["status"] = "active"

    return {"act": act_no, "beat_index": beat_index}


def _story_material(state: dict) -> str:
    """Collect the material an outline is distilled from: scenario, world
    rules/lore, and the most recent scenes (newest always in full)."""
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


async def _generate_outline(state: dict, sdk) -> dict:
    data = _own_data(state) or _default_data()
    config = _config(state)
    turn = state.get("turn", 0)
    attempts = int(data.get("outline_attempts", 0) or 0) + 1

    prompt = f"""You are a hidden story architect for a text RPG. From the material below, design a concise 3-act plot outline that a storyteller will be SECRETLY guided toward. The player must never see it. It must fit the established genre, tone, and events so far, and leave room for player freedom -- beats are situations and pressures, not scripted player actions.

MATERIAL:
{_story_material(state)}

Respond with ONLY valid JSON:
{{"premise": "one-sentence hidden premise", "driving_tension": "the core dramatic tension", "acts": [{{"title": "...", "goal": "what this act accomplishes", "beats": [{{"description": "one concrete story beat"}}]}}]}}
Exactly 3 acts, each with 2-4 beats."""

    model_pref = config.get("outline_ai_model", "smartest")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    normalized = _normalize_outline(parsed) if isinstance(parsed, dict) else None
    if normalized is None:
        status = "failed" if attempts >= OUTLINE_MAX_ATTEMPTS else "pending"
        if status == "failed":
            print(f"[Plot Director] Outline generation failed {attempts} times; going dormant.")
        return {"module_data": {MODULE_ID: {"outline_attempts": attempts, "status": status}}}

    print(f"[Plot Director] Turn {turn}: hidden outline ready ({len(normalized['outline']['acts'])} acts).")
    return {"module_data": {MODULE_ID: {
        "outline": normalized["outline"],
        "position": normalized["position"],
        "status": "ready",
        "outline_created_turn": turn,
        "outline_attempts": attempts,
        "momentum": "building",
    }}}


async def _replan_outline(state: dict, sdk, data: dict, drift_notes: list[str]) -> dict | None:
    """Rewrite the remaining outline to embrace the player's new direction.
    Returns the module_data update dict, or None when the re-plan failed."""
    config = _config(state)
    history = state.get("history", [])
    recent = "\n".join(str(h) for h in history[-4:])[-6000:]
    notes = "; ".join(n for n in drift_notes if n) or "The player has changed the story's theme or tone."

    prompt = f"""You are a hidden story architect for a text RPG. The player has deliberately taken the story in a new direction: {notes}

Here is the current hidden outline. Beats with "status": "done" already happened and are IMMUTABLE history -- keep them exactly as given, including their status. REWRITE all remaining beats (and act goals/titles where needed) to embrace the player's new direction. Do not force the story back to the old plan; follow the player.

CURRENT OUTLINE:
{json.dumps(data.get("outline", {}), ensure_ascii=False)}

RECENT SCENES:
{recent}

Respond with ONLY valid JSON in the same outline format:
{{"premise": "...", "driving_tension": "...", "acts": [{{"title": "...", "goal": "...", "beats": [{{"description": "...", "status": "done or pending"}}]}}]}}"""

    model_pref = config.get("outline_ai_model", "smartest")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    normalized = _normalize_outline(parsed) if isinstance(parsed, dict) else None
    if normalized is None:
        return None
    return {
        "outline": normalized["outline"],
        "position": normalized["position"],
    }


async def on_gather_context(state: dict, sdk) -> dict:
    if not _own_data(state):
        return {"module_data": {MODULE_ID: _default_data()}}
    return {}


async def on_librarian(state: dict, sdk) -> dict | None:
    config = _config(state)
    if not config.get("plot_enabled", True):
        return None
    if not state.get("history"):
        return None

    data = _own_data(state) or _default_data()
    status = data.get("status", "pending")
    if status == "failed":
        return None

    turn = state.get("turn", 0)

    if status != "ready":
        return await _generate_outline(state, sdk)

    updates: dict = {}

    # A nudge armed on an earlier turn was rendered into this turn's prompt;
    # consume it so it fires exactly once. _deep_merge never deletes keys, so
    # the empty string must be written explicitly.
    if data.get("pending_nudge") and int(data.get("last_nudge_turn", 0) or 0) < turn:
        updates["pending_nudge"] = ""

    frequency = max(1, int(config.get("assessment_frequency", 1) or 1))
    if turn == 0 or turn % frequency != 0 or turn <= int(data.get("outline_created_turn", 0) or 0):
        return {"module_data": {MODULE_ID: updates}} if updates else None

    current = _current_beat(data)
    if current is None:
        # Outline fully resolved -- nothing left to steer toward.
        updates["momentum"] = "resolving"
        return {"module_data": {MODULE_ID: updates}}
    act, beat, act_no, total_acts = current

    history = state.get("history", [])
    latest = str(history[-1])[-4000:]
    earlier = "\n".join(str(h) for h in history[-3:-1])[-2000:]
    earlier_block = f"EARLIER NARRATION (context only):\n{earlier}\n\n" if earlier else ""

    player_input = ""
    for message in reversed(state.get("chat_messages", [])):
        if message.get("role") == "user" and str(message.get("content", "")).strip():
            player_input = str(message["content"])[-1000:]
            break

    prompt = f"""You silently track whether a hidden plotline is progressing in a text RPG. The player must never learn of it.

HIDDEN CURRENT OBJECTIVE (never reveal it):
Act {act_no} of {total_acts}: {act.get('goal') or act.get('title')}
Current beat: {beat.get('description')}

{earlier_block}THIS TURN'S SCENE (judge this):
{latest}

LAST PLAYER INPUT:
{player_input or '(none)'}

Judge:
- beat_advanced: did this scene move the current beat forward at all?
- beat_completed: is the current beat fully resolved?
- drift_detected: has the player deliberately steered the story's theme, tone, or direction away from the hidden objective? Drift means sustained redirection, not a single detour or side scene.
- opportunity: does the scene end with a natural opening to advance the beat next turn?
- nudge: one subtle, optional hint that could organically pull the story toward the current beat without contradicting what the player is doing. Phrase it as story material (a rumor, an arrival, a discovery), not an instruction to the player.

Respond with ONLY valid JSON:
{{"beat_advanced": false, "beat_completed": false, "beat_summary": "one sentence, only if completed", "drift_detected": false, "drift_note": "what changed, only if drifting", "opportunity": false, "nudge": "...", "momentum": "building|steady|stalled"}}"""

    model_pref = config.get("assessment_ai_model", "balanced")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return {"module_data": {MODULE_ID: updates}} if updates else None

    advanced = bool(parsed.get("beat_advanced"))
    completed = bool(parsed.get("beat_completed"))
    drift = bool(parsed.get("drift_detected"))
    opportunity = bool(parsed.get("opportunity"))
    nudge_text = str(parsed.get("nudge") or "").strip()
    drift_note = str(parsed.get("drift_note") or "").strip()
    momentum = parsed.get("momentum")
    if momentum not in MOMENTUM_VALUES:
        momentum = "steady" if advanced else "stalled"

    outline = json.loads(json.dumps(data.get("outline", {})))  # deep copy; mutated below
    position = dict(data.get("position", {"act": 1, "beat_index": 0}))
    beats_completed = list(data.get("beats_completed", []))

    if completed:
        beats_completed.append({
            "id": beat.get("id", ""),
            "turn": turn,
            "summary": str(parsed.get("beat_summary") or "").strip() or beat.get("description", ""),
        })
        beats_completed = beats_completed[-MAX_COMPLETED_BEATS:]
        position = _advance_position(outline, position)
        updates["outline"] = outline
        updates["beats_completed"] = beats_completed
        if position.get("act", 1) > len(outline.get("acts", [])):
            momentum = "resolving"

    updates["position"] = position
    updates["stall_count"] = 0 if (advanced or completed) else int(data.get("stall_count", 0) or 0) + 1
    updates["drift_streak"] = int(data.get("drift_streak", 0) or 0) + 1 if drift else 0
    updates["momentum"] = momentum

    log = list(data.get("assessment_log", []))
    log.append({
        "turn": turn,
        "advanced": advanced or completed,
        "drift": drift,
        "note": drift_note or (str(parsed.get("beat_summary") or "").strip() if completed else ""),
    })
    updates["assessment_log"] = log[-MAX_LOG_ENTRIES:]

    replanned = False
    if (
        updates["drift_streak"] >= DRIFT_STREAK_TO_REPLAN
        and turn - int(data.get("last_replan_turn", 0) or 0) >= REPLAN_MIN_GAP_TURNS
    ):
        drift_notes = [e.get("note", "") for e in updates["assessment_log"][-DRIFT_STREAK_TO_REPLAN:] if e.get("drift")]
        replan = await _replan_outline(state, sdk, data, drift_notes)
        if replan is not None:
            print(f"[Plot Director] Turn {turn}: player changed direction -- outline re-planned.")
            updates["outline"] = replan["outline"]
            updates["position"] = replan["position"]
            updates["stall_count"] = 0
            updates["drift_streak"] = 0
            updates["pending_nudge"] = ""
            updates["last_replan_turn"] = turn
            updates["replan_count"] = int(data.get("replan_count", 0) or 0) + 1
            updates["momentum"] = "building"
            replanned = True
        else:
            # Keep a foot in the door: one more drift flag re-triggers the re-plan.
            updates["drift_streak"] = 1

    if not replanned and momentum != "resolving" and nudge_text:
        stall_threshold = max(1, int(config.get("stall_threshold", 3) or 3))
        stalled = updates["stall_count"] >= stall_threshold
        opportune = (
            opportunity
            and config.get("opportunity_nudges", True)
            and turn - int(data.get("last_nudge_turn", 0) or 0) >= OPPORTUNITY_COOLDOWN_TURNS
        )
        if stalled or opportune:
            print(f"[Plot Director] Turn {turn}: nudge armed ({'stall' if stalled else 'opportunity'}).")
            updates["pending_nudge"] = nudge_text
            updates["stall_count"] = 0
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


# Spoiler-safe phrasing tables for /plot. Derived only from position/momentum
# metadata -- beat and act text must never appear here.
_MOMENTUM_LINES = {
    "building": "The story is gathering momentum.",
    "steady": "The story is moving steadily.",
    "stalled": "The story drifts in the moment.",
    "resolving": "The threads are drawing to a close.",
}


def _stage_phrase(data: dict) -> str:
    acts = data.get("outline", {}).get("acts", [])
    total = len(acts)
    position = data.get("position", {})
    act_no = int(position.get("act", 1) or 1)
    if act_no > total:
        return f"Act {total} of {total} -- the tale nears its end"

    beats = acts[act_no - 1].get("beats", []) if 0 < act_no <= total else []
    done = sum(1 for b in beats if b.get("status") == "done")
    late = beats and done >= len(beats) / 2

    if act_no == 1:
        phrase = "the pieces are moving into place"
    elif act_no < total:
        phrase = "a turning point draws near" if late else "tension building"
    else:
        phrase = "threads are converging"
    return f"Act {act_no} of {total} -- {phrase}"


async def on_command_plot(args: list[str], state: dict, sdk) -> dict:
    config = _config(state)
    data = _own_data(state)

    if not config.get("plot_enabled", True) or data.get("status") == "failed":
        return {"message": "[Plot] The story is charting its own course.", "signal": "end_turn"}
    if data.get("status") != "ready":
        return {"message": "[Plot] The story is still finding its shape.", "signal": "end_turn"}

    lines = [f"[Plot] {_stage_phrase(data)}."]
    lines.append(_MOMENTUM_LINES.get(data.get("momentum", ""), _MOMENTUM_LINES["steady"]))
    turn = state.get("turn", 0)
    if data.get("replan_count", 0) and turn - int(data.get("last_replan_turn", 0) or 0) <= 3:
        lines.append("The story has recently found a new direction.")
    return {"message": " ".join(lines), "signal": "end_turn"}
