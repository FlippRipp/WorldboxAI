"""Shared helpers borrowed across Narrative/Director modules (wb_plot_director, wb_npc_system).

Centralizes:
1. In-world clock formatting and time-aware pacing helpers (wb_time_tracker).
2. Setting, player-input, protagonist/RPG-sheet, and Plot Director profile/direction
   context builders (keeping Plot Director as the single source of truth for player
   preferences, roles, observation window, and HOLD scene pacing).
3. Safe system-prompt-aware `llm_generate` wrapper.
4. `ModuleBackgroundRunner` for non-blocking post-turn (`on_turn_stopped`) LLM passes
   with rollback/epoch validation and thread-safe snapshot/swipe archive patching.
"""
from __future__ import annotations

from copy import deepcopy
import asyncio
import inspect
import json
from pathlib import Path
import re
import threading
from typing import Callable
import zipfile

DEFAULT_OBSERVATION_TURNS = 4
RECENT_INPUTS_COUNT = 6
ABANDON_COOLDOWN_FACTOR = 2
COOLDOWN_REST_SKIP_MINUTES = 360
MIN_COOLDOWN_MINUTES = 30
PLAYER_SET_EVIDENCE = "set directly by the player"
PREF_WEIGHTS = ("low", "medium", "high")
PREF_ROLES = ("actor", "target", "mutual", "world")

TIME_OF_DAY_BANDS = (
    (0, 5, "the dead of night"),
    (5, 8, "early morning"),
    (8, 12, "morning"),
    (12, 14, "midday"),
    (14, 18, "afternoon"),
    (18, 21, "evening"),
    (21, 24, "night"),
)

_ARCHIVE_PATCH_LOCK = threading.Lock()


def parse_json_block(raw: str):
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


def get_time_clock(state: dict) -> dict | None:
    """Return the active wb_time_tracker clock dict if present, else None."""
    time_data = (state.get("module_data") or {}).get("wb_time_tracker")
    if not isinstance(time_data, dict):
        return None
    clock = time_data.get("clock")
    if not isinstance(clock, dict):
        return None
    return clock


def current_minutes(state: dict) -> int | None:
    """Return total in-world minutes elapsed from wb_time_tracker, or None when
    the time module is inactive/uninitialized."""
    clock = get_time_clock(state)
    if clock is None:
        return None
    try:
        return int(clock.get("total_minutes_elapsed", 0) or 0)
    except (TypeError, ValueError):
        return 0


def time_of_day_label(hour: int) -> str:
    for start, end, label in TIME_OF_DAY_BANDS:
        if start <= hour < end:
            return label
    return "night"


def format_clock_text(clock: dict) -> str:
    year = int(clock.get("year", 1) or 1)
    month = int(clock.get("month", 1) or 1)
    day = int(clock.get("day", 1) or 1)
    hour = int(clock.get("hour", 8) or 0)
    minute = int(clock.get("minute", 0) or 0)
    return (
        f"Year {year}, Month {month}, Day {day} -- "
        f"{hour:02d}:{minute:02d} ({time_of_day_label(hour)})"
    )


def format_duration_minutes(minutes: int) -> str:
    minutes = max(0, int(minutes or 0))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if not days and mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return ", ".join(parts) or "0 minutes"


def setting_material(state: dict) -> list[str]:
    """Scenario, world rules/lore, and in-world clock blocks shared across
    director and casting prompts."""
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

    clock = get_time_clock(state)
    if clock is not None:
        parts.append(
            f"<in_world_time>\nCurrent in-world date and time: {format_clock_text(clock)}.\n</in_world_time>"
        )

    return parts


def recent_player_inputs(state: dict, limit: int = RECENT_INPUTS_COUNT) -> list[str]:
    """Recent non-empty player messages (oldest to newest)."""
    inputs = [
        str(m.get("content", "")).strip()
        for m in state.get("chat_messages", [])
        if m.get("role") == "user" and str(m.get("content", "")).strip()
    ]
    return inputs[-limit:] if limit > 0 else inputs


def character_sheet_block(state: dict) -> str:
    """The player character's capabilities from wb_core_rpg."""
    rpg = (state.get("module_data") or {}).get("wb_core_rpg", {})
    if not isinstance(rpg, dict):
        return ""
    skills = rpg.get("skills", {})
    skills = skills if isinstance(skills, dict) else {}

    lines = []
    header_bits = []
    level = rpg.get("level")
    if isinstance(level, int) and level > 0:
        header_bits.append(f"Level {level}")
    stats = rpg.get("stats", {})
    if isinstance(stats, dict):
        top_stats = sorted(
            ((k, v) for k, v in stats.items() if isinstance(v, (int, float))),
            key=lambda kv: kv[1], reverse=True,
        )[:3]
        if top_stats:
            header_bits.append("strongest stats: " + ", ".join(f"{k} {int(v)}" for k, v in top_stats))
    if header_bits:
        lines.append(" · ".join(header_bits))

    for name, skill in skills.items():
        if not isinstance(skill, dict):
            continue
        kind = str(skill.get("type", "active")).strip() or "active"
        rating = skill.get("rating", "")
        description = str(skill.get("description", "")).strip()
        line = f"- [{kind}] {name}"
        if rating != "":
            line += f" ({rating}/10)"
        if description:
            line += f": {description}"
        lines.append(line)

    if not lines:
        return ""
    return (
        "PLAYER CHARACTER SHEET (from the RPG system -- design the challenge so "
        "these abilities matter: give favored skills a stage to shine, pitch "
        "opposition their capabilities can meaningfully engage at the guided "
        "difficulty, and let a curse or weakness complicate things where one "
        "exists):\n" + "\n".join(lines) + "\n\n"
    )


def protagonist_context_block(state: dict) -> str:
    """Protagonist identity, personality, appearance, and RPG capabilities so
    NPC casting designs foils, allies, and rivals tailored to who the player
    character actually is."""
    player = (state.get("characters") or {}).get("default_player")
    lines = []
    if isinstance(player, dict):
        name = str(player.get("name") or "").strip()
        ident = " / ".join(
            str(player.get(k) or "").strip()
            for k in ("race", "gender")
            if str(player.get(k) or "").strip()
        )
        if name or ident:
            head = name or "Player"
            if ident:
                head += f" ({ident})"
            lines.append(f"Protagonist: {head}")
        appearance = str(player.get("full_appearance") or player.get("short_appearance") or "").strip()
        if appearance:
            lines.append(f"Appearance: {appearance}")
        personality = str(player.get("personality") or "").strip()
        if personality:
            lines.append(f"Personality: {personality}")
        context = str(player.get("context") or "").strip()
        if context:
            lines.append(f"Background: {context}")

    rpg_sheet = character_sheet_block(state).strip()
    if rpg_sheet:
        # Strip the plot-challenge header and embed the capability lines cleanly.
        rpg_body = "\n".join(rpg_sheet.splitlines()[1:])
        if rpg_body:
            lines.append("Capabilities:\n" + rpg_body)

    if not lines:
        return ""
    return "PLAYER PROTAGONIST (cast NPCs who create compelling chemistry, contrast, or opportunity alongside them):\n" + "\n".join(lines)


def planning_iterations(config: dict | None, key: str = "planning_iterations",
                        services: dict | None = None) -> int:
    """Number of backstage notebook passes configured (1-3). Defaults to 2 in
    live server sessions and 1 when unset in standalone unit tests."""
    if key in (config or {}):
        try:
            return max(1, min(3, int(config.get(key) or 1)))
        except (TypeError, ValueError):
            return 1
    return 2 if (services or {}).get("engine") is not None else 1


async def llm_generate(sdk, module_id: str, prompt: str, model_preference: str = "balanced",
                       system_prompt: str | None = None) -> str:
    """Invoke sdk.llm.generate, passing system_prompt as a separate system role
    when supported by the SDK bridge (falling back to a combined prompt for
    minimal test stubs)."""
    gen = sdk.llm.generate
    sdk.llm._current_module = module_id
    try:
        if system_prompt:
            try:
                params = inspect.signature(gen).parameters
            except (TypeError, ValueError):
                params = {}
            if "system_prompt" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                return await gen(prompt, model_preference=model_preference, system_prompt=system_prompt)
            return await gen(f"{system_prompt}\n\n{prompt}", model_preference=model_preference)
        return await gen(prompt, model_preference=model_preference)
    finally:
        sdk.llm._current_module = ""


# ---------------------------------------------------------------------------
# Plot Director state readers (borrowed by wb_npc_system, owned by wb_plot_director)
# ---------------------------------------------------------------------------

def is_profile_unobserved(profile: dict) -> bool:
    """True while the AI has not yet observed any behavior from the story
    (ignoring likes/dislikes typed in directly by the player at creation)."""
    if not isinstance(profile, dict):
        return True
    observed_likes = [
        e for e in (profile.get("likes") or [])
        if isinstance(e, dict) and e.get("evidence") != PLAYER_SET_EVIDENCE
    ]
    return (
        not any((profile.get("playstyle") or {}).values())
        and not profile.get("tone")
        and not profile.get("themes")
        and not observed_likes
        and not profile.get("avoids")
        and not profile.get("attachments")
    )


def in_initial_observation(state: dict, data: dict) -> bool:
    """Whether a fresh story is still in Plot Director's initial observation
    window before the first direction and plot thread are woven."""
    if data.get("status") != "observing" or data.get("thread_history"):
        return False
    if (data.get("thread") or {}).get("status") == "active":
        return False
    config = (state.get("module_configs") or {}).get("wb_plot_director", {})
    turn = int(state.get("turn", 0) or 0)
    if "observation_turns" in config:
        obs_turns = max(0, int(config.get("observation_turns") or 0))
        return turn < obs_turns
    has_direction = bool(str((data.get("direction") or {}).get("premise", "")).strip())
    if has_direction or not is_profile_unobserved(data.get("profile") or {}):
        return False
    return turn < DEFAULT_OBSERVATION_TURNS


def in_quiet_period(state: dict, data: dict, turn: int) -> bool:
    """Whether Plot Director is still in its between-thread breather."""
    next_thread_turn = int(data.get("next_thread_turn", 0) or 0)
    last_closed_turn = int(data.get("last_closed_turn", 0) or 0)
    if next_thread_turn <= 0 or (last_closed_turn > 0 and next_thread_turn <= last_closed_turn):
        return False

    cur_minutes = current_minutes(state)
    if cur_minutes is None:
        return turn < next_thread_turn

    last_closed_minutes = int(data.get("last_closed_minutes", 0) or 0)
    minutes_since_close = max(0, cur_minutes - last_closed_minutes)
    last_outcome = (data.get("thread_history") or [{}])[-1].get("outcome", "")
    skip_threshold = COOLDOWN_REST_SKIP_MINUTES * (
        ABANDON_COOLDOWN_FACTOR if last_outcome == "abandoned" else 1
    )
    if turn > last_closed_turn and minutes_since_close >= skip_threshold:
        return False

    if turn < next_thread_turn:
        return True

    if minutes_since_close < MIN_COOLDOWN_MINUTES and turn < next_thread_turn + 2:
        return True

    return False


def _active_plot_data(state: dict) -> dict | None:
    plot_cfg = (state.get("module_configs") or {}).get("wb_plot_director", {})
    if not plot_cfg.get("plot_enabled", True):
        return None
    plot = (state.get("module_data") or {}).get("wb_plot_director")
    if not isinstance(plot, dict) or plot.get("suspended") or plot.get("status") == "failed":
        return None
    return plot


def is_plot_observing(state: dict) -> bool:
    """True when Plot Director is active in state and currently inside its
    initial observation window."""
    plot = _active_plot_data(state)
    if plot is None:
        return False
    return in_initial_observation(state, plot)


def is_plot_holding(state: dict) -> bool:
    """True when Plot Director is actively holding back external world
    momentum (intimate/emotional withhold beat, deferred mid-climax hold, or
    post-thread quiet period), so other directors (like NPC introductions) stand
    down instead of intruding."""
    plot = _active_plot_data(state)
    if plot is None:
        return False
    turn = int(state.get("turn", 0) or 0)
    last_assessment = plot.get("last_assessment")
    if isinstance(last_assessment, dict) and int(last_assessment.get("turn", -1)) == turn:
        if str(last_assessment.get("scene_mode") or "").strip().lower() == "withhold":
            return True
    elif str(plot.get("scene_mode") or "").strip().lower() == "withhold":
        return True

    thread = plot.get("thread") or {}
    if plot.get("status") != "active" or thread.get("status") != "active":
        deferred_hold = int(plot.get("defer_streak", 0) or 0) > 0
        quiet_hold = (
            int(plot.get("last_closed_turn", 0) or 0) > 0
            and in_quiet_period(state, plot, turn)
        )
        return deferred_hold or quiet_hold
    return False


def plot_threads_for_npc(state: dict, bank: dict | None = None) -> list[dict] | None:
    """Borrow active storyline signals directly from Plot Director when it is
    running and populated, avoiding a duplicate LLM story-thread extraction pass
    in wb_npc_system. Returns None when Plot Director has no active material yet."""
    plot = _active_plot_data(state)
    if plot is None:
        return None
    thread = plot.get("thread") or {}
    direction = plot.get("direction") or {}

    raw_texts: list[str] = []
    if thread.get("status") == "active" and str(thread.get("title") or "").strip():
        title = str(thread.get("title")).strip()
        hook = str(thread.get("hook") or "").strip()
        raw_texts.append(f"{title}: {hook}" if hook else title)
    heading = str(direction.get("heading") or "").strip()
    if heading:
        raw_texts.append(heading)
    for q in (direction.get("open_questions") or [])[:3]:
        q_str = str(q).strip()
        if q_str:
            raw_texts.append(q_str)

    if not raw_texts:
        return None

    bank = bank if isinstance(bank, dict) else {}
    synthesized = []
    for text in raw_texts[:5]:
        lowered = text.lower()
        npc_ids = [
            npc_id for npc_id, npc in bank.items()
            if isinstance(npc, dict)
            and str(npc.get("name") or "").strip()
            and str(npc.get("name")).strip().lower() in lowered
        ]
        synthesized.append({"text": text, "npc_ids": npc_ids})
    return synthesized


def format_plot_casting_block(state: dict) -> str:
    """Format Plot Director's full learned player profile (including role-explicit
    likes/avoids, dislikes, playstyle, attachments, narrative register), elastic
    direction (premise, endgame, horizon, planted seeds, open questions), active
    thread, and GM notebook for character generation and capture."""
    plot = (state.get("module_data") or {}).get("wb_plot_director")
    if not isinstance(plot, dict):
        return ""
    plot_cfg = (state.get("module_configs") or {}).get("wb_plot_director", {})
    if not plot_cfg.get("plot_enabled", True) or plot.get("status") == "failed":
        return ""

    profile = plot.get("profile") if isinstance(plot.get("profile"), dict) else {}
    direction = plot.get("direction") if isinstance(plot.get("direction"), dict) else {}
    thread = plot.get("thread") if isinstance(plot.get("thread"), dict) else {}

    tone = str(profile.get("tone") or "").strip()
    themes = [str(t).strip() for t in profile.get("themes", []) if str(t).strip()]

    playstyle = profile.get("playstyle") if isinstance(profile.get("playstyle"), dict) else {}
    top_styles = sorted(
        ((k, v) for k, v in playstyle.items() if isinstance(v, int) and v > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )[:3]

    def _prefs(entries) -> list[str]:
        out = []
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                text = str(e.get("text", "")).strip()
                weight = str(e.get("weight") or "medium").strip()
                role = str(e.get("role") or "").strip().lower()
            else:
                text, weight, role = str(e).strip(), "medium", ""
            if not text:
                continue
            if role in PREF_ROLES:
                out.append(f"{text} ({weight}, role: {role})")
            else:
                out.append(f"{text} ({weight})")
        return out

    likes = _prefs(profile.get("likes"))
    dislikes = _prefs(profile.get("dislikes"))
    avoids = _prefs(profile.get("avoids"))

    attachments = []
    for a in (profile.get("attachments") or []):
        if isinstance(a, dict) and str(a.get("name") or "").strip():
            note = str(a.get("note") or "").strip()
            attachments.append(f"{a['name'].strip()}" + (f" ({note})" if note else ""))

    narrative = profile.get("narrative") if isinstance(profile.get("narrative"), dict) else {}
    narrative_parts = [
        f"{k}: {str(narrative.get(k)).strip()}"
        for k in ("pacing", "agency", "register")
        if str(narrative.get(k) or "").strip()
    ]

    premise = str(direction.get("premise") or "").strip()
    endgame = str(direction.get("endgame") or "").strip()
    heading = str(direction.get("heading") or "").strip()
    horizon = [str(h).strip() for h in (direction.get("horizon") or []) if str(h).strip()]
    seeds = []
    for s in (direction.get("planted_seeds") or []):
        if isinstance(s, dict) and str(s.get("seed") or "").strip():
            payoff = str(s.get("payoff") or "").strip()
            seeds.append(f"{s['seed'].strip()}" + (f" -> {payoff}" if payoff else ""))
    questions = [str(q).strip() for q in (direction.get("open_questions") or []) if str(q).strip()]

    active_thread_line = ""
    if thread.get("status") == "active" and str(thread.get("title") or "").strip():
        mode = str(thread.get("mode") or "").strip()
        mode_tag = f" [{mode}]" if mode else ""
        hook = str(thread.get("hook") or "").strip()
        challenge = str(thread.get("challenge") or "").strip()
        active_thread_line = f'"{thread["title"].strip()}"{mode_tag}'
        if hook:
            active_thread_line += f" -- {hook}"
        if challenge:
            active_thread_line += f" (Complication: {challenge})"

    gm_notes = str(plot.get("gm_notes") or "").strip()

    if not (
        tone or themes or top_styles or likes or dislikes or avoids
        or attachments or narrative_parts or premise or endgame or heading
        or horizon or seeds or questions or active_thread_line or gm_notes
    ):
        return ""

    lines = [
        "STORY DIRECTION (from the Plot Director -- align characters with the story's "
        "established tone, themes, and player tastes; weight marks how strongly the player "
        "feels, and role marks actor=player doing it to NPCs, target=done to the player, "
        "mutual=shared dynamic, world=setting element. NEVER invert the player's role):"
    ]
    if premise:
        lines.append(f"Arc premise: {premise}")
    if endgame:
        lines.append(f"North Star (Endgame): {endgame}")
    if heading:
        lines.append(f"Heading: {heading}")
    if horizon:
        lines.append("Horizon milestones: " + " -> ".join(horizon))
    if seeds:
        lines.append("Planted seeds: " + "; ".join(seeds))
    if questions:
        lines.append("Open questions: " + "; ".join(questions))
    if active_thread_line:
        lines.append(f"Active plot thread: {active_thread_line}")
    if gm_notes:
        lines.append(f"Plot GM notes: {gm_notes}")
    if tone:
        lines.append(f"Tone: {tone}")
    if themes:
        lines.append(f"Themes: {', '.join(themes)}")
    if top_styles:
        lines.append("Playstyle leans: " + ", ".join(f"{k} ({v}/10)" for k, v in top_styles))
    if narrative_parts:
        lines.append("Narrative preferences: " + "; ".join(narrative_parts))
    if attachments:
        lines.append("Player attachments: " + "; ".join(attachments))
    if likes:
        lines.append(f"Player enjoys (lean into these): {'; '.join(likes)}")
    if dislikes:
        lines.append(f"Player dislikes (steer clear of these): {'; '.join(dislikes)}")
    if avoids:
        lines.append(f"Player tends to steer away from (observed): {'; '.join(avoids)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Non-blocking post-turn background execution (shared across modules)
# ---------------------------------------------------------------------------

def patch_zip_module_state(
    zip_path: Path,
    module_id: str,
    engine,
    own_updates: dict,
    fallback_base: dict | None = None,
) -> bool:
    """Merge `own_updates` into `Module_States/{module_id}.json` inside an
    existing turn snapshot or swipe variant zip archive on disk."""
    if not zip_path.exists():
        return False
    target_arc = f"Module_States/{module_id}.json"
    with _ARCHIVE_PATCH_LOCK:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                members = {}
                existing_mod = None
                for name in zf.namelist():
                    raw_bytes = zf.read(name)
                    if name.replace("\\", "/") == target_arc:
                        try:
                            existing_mod = json.loads(raw_bytes.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            existing_mod = None
                    else:
                        members[name] = raw_bytes
            base_mod = existing_mod if isinstance(existing_mod, dict) else deepcopy(fallback_base or {})
            merged_mod = engine._deep_merge(base_mod, own_updates)
            tmp_path = zip_path.with_suffix(f".{module_id}.zip.tmp")
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, raw_bytes in members.items():
                    zf.writestr(name, raw_bytes)
                zf.writestr(target_arc, json.dumps(merged_mod, indent=2).encode("utf-8"))
            tmp_path.replace(zip_path)
            return True
        except OSError:
            return False


def patch_saved_turn_archives(
    sm,
    engine,
    module_id: str,
    save_id: str | None,
    turn_number: int,
    own_updates: dict,
    fallback_base: dict | None = None,
    patch_snapshot: bool = True,
    patch_swipe: bool = True,
    repack: bool = False,
) -> None:
    """Keep `Snapshots/turn_{N}.zip` and `Swipes/v{active}.zip` in sync when a
    background module pass finishes after the turn was already saved."""
    save_manager = getattr(sm, "save_manager", None)
    saves_dir = getattr(save_manager, "saves_dir", None)
    if not save_id or saves_dir is None:
        return
    save_path = Path(saves_dir) / str(save_id)
    if not save_path.exists():
        return

    changed = False
    if patch_snapshot:
        snap_path = save_path / "Snapshots" / f"turn_{turn_number}.zip"
        if patch_zip_module_state(snap_path, module_id, engine, own_updates, fallback_base=fallback_base):
            changed = True

    if patch_swipe:
        manifest_path = save_path / "Swipes" / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                manifest = None
            if isinstance(manifest, dict) and int(manifest.get("turn", -1)) == turn_number:
                active_idx = int(manifest.get("active", 0) or 0)
                swipe_zip = save_path / "Swipes" / f"v{active_idx}.zip"
                if patch_zip_module_state(swipe_zip, module_id, engine, own_updates, fallback_base=fallback_base):
                    changed = True

    if changed and repack and hasattr(save_manager, "_pack_save"):
        with _ARCHIVE_PATCH_LOCK:
            try:
                save_manager._pack_save(save_id)
            except OSError:
                pass


class ModuleBackgroundRunner:
    """Manages non-blocking post-turn (`on_turn_stopped`) LLM execution,
    rollback/epoch validation, turn staging, and snapshot/swipe sync for one module."""

    def __init__(
        self,
        module_id: str,
        label: str,
        default_data: Callable[[], dict] | None = None,
        should_discard_live: Callable[[dict], bool] | None = None,
    ) -> None:
        self.module_id = module_id
        self.label = label
        self._default_data = default_data or dict
        self._should_discard_live = should_discard_live
        self.services: dict = {}
        self.tasks: set[asyncio.Task] = set()
        self.current_task: asyncio.Task | None = None
        self.current_job_meta: dict | None = None
        self.pending_job: dict | None = None
        self.staged_updates: dict | None = None
        self.epoch: int = 0

    def set_services(self, services: dict) -> None:
        self.services = services or {}

    def cancel(self) -> None:
        self.epoch += 1
        self.pending_job = None
        self.staged_updates = None
        self.current_job_meta = None
        for task in list(self.tasks):
            if not task.done():
                task.cancel()
        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()
        self.current_task = None

    def is_live_turn_armed(self) -> bool:
        engine = self.services.get("engine")
        sm = self.services.get("session_manager")
        return bool(engine is not None and sm is not None and getattr(engine, "_turn_hooks_armed", False))

    def in_flight(self) -> bool:
        return (
            (self.current_task is not None and not self.current_task.done())
            or (self.pending_job is not None)
        )

    def job_matches_state(self, meta: dict | None, state: dict, allow_later_turn: bool = True) -> bool:
        if not isinstance(meta, dict):
            return False
        if meta.get("epoch") is not None and meta.get("epoch") != self.epoch:
            return False
        if meta.get("save_id") != state.get("active_save_id"):
            return False
        state_turn = int(state.get("turn", 0) or 0)
        job_turn = int(meta.get("turn", 0) or 0)
        if state_turn < job_turn:
            return False
        if not allow_later_turn and state_turn != job_turn:
            return False
        hist_len = int(meta.get("hist_len", 0) or 0)
        if hist_len > 0:
            history = state.get("history") or []
            if len(history) < hist_len or str(history[hist_len - 1]) != str(meta.get("scene", "")):
                return False
        return True

    def reconcile_on_gather(self, state: dict) -> None:
        if self.current_job_meta is not None and not self.job_matches_state(self.current_job_meta, state):
            self.cancel()
        if self.staged_updates is not None and not self.job_matches_state(self.staged_updates, state):
            self.staged_updates = None

    def overlay_staged(self, state: dict, data: dict) -> dict:
        """Return `data` overlaid with any matching staged updates (or drop stale ones)."""
        if self.staged_updates is None or not isinstance(data, dict):
            return data
        if self.job_matches_state(self.staged_updates, state) and isinstance(self.staged_updates.get("updates"), dict):
            engine = self.services.get("engine")
            if engine is not None and hasattr(engine, "_deep_merge"):
                return engine._deep_merge(deepcopy(data), self.staged_updates["updates"])
            out = deepcopy(data)
            out.update(self.staged_updates["updates"])
            return out
        self.staged_updates = None
        return data

    def consume_staged_for_librarian(self, state: dict) -> dict:
        """Pop valid staged updates from an earlier turn so the current turn's
        librarian pass builds on top of them."""
        if self.staged_updates is None:
            return {}
        staged = self.staged_updates
        self.staged_updates = None
        turn = int(state.get("turn", 0) or 0)
        staged_turn = int(staged.get("turn", 0) or 0)
        if (
            self.job_matches_state(staged, state)
            and staged_turn < turn
            and isinstance(staged.get("updates"), dict)
        ):
            return deepcopy(staged["updates"])
        return {}

    def queue_job(self, state: dict, sdk, immediate_updates: dict | None = None) -> None:
        snap = deepcopy(state)
        if immediate_updates:
            mod_snap = snap.setdefault("module_data", {}).setdefault(self.module_id, {})
            engine = self.services.get("engine")
            if engine is not None and hasattr(engine, "_deep_merge"):
                snap["module_data"][self.module_id] = engine._deep_merge(mod_snap, immediate_updates)
            else:
                mod_snap.update(immediate_updates)
        history = state.get("history") or []
        self.pending_job = {
            "save_id": state.get("active_save_id"),
            "turn": int(state.get("turn", 0) or 0),
            "hist_len": len(history),
            "scene": str(history[-1]) if history else "",
            "epoch": self.epoch,
            "state": snap,
            "sdk": sdk,
            "base_updates": {},
        }

    def patch_archives(
        self,
        save_id: str | None,
        turn_number: int,
        own_updates: dict,
        fallback_base: dict | None = None,
        patch_snapshot: bool = True,
        patch_swipe: bool = True,
        repack: bool = False,
    ) -> None:
        sm = self.services.get("session_manager")
        engine = self.services.get("engine")
        if sm is None or engine is None:
            return
        patch_saved_turn_archives(
            sm,
            engine,
            self.module_id,
            save_id or getattr(sm, "active_save_id", None),
            turn_number,
            own_updates,
            fallback_base=fallback_base,
            patch_snapshot=patch_snapshot,
            patch_swipe=patch_swipe,
            repack=repack,
        )

    async def _execute_job(self, job: dict, run_pass: Callable) -> None:
        try:
            prev_task = job.get("prev_task")
            if prev_task is not None and not prev_task.done():
                try:
                    await prev_task
                except (asyncio.CancelledError, Exception):
                    pass
            if job.get("epoch") != self.epoch:
                return
            if self.staged_updates is not None:
                staged = self.staged_updates
                if (
                    self.job_matches_state(staged, job["state"])
                    and int(staged.get("turn", 0) or 0) < int(job.get("turn", 0) or 0)
                    and isinstance(staged.get("updates"), dict)
                ):
                    self.staged_updates = None
                    engine = self.services.get("engine")
                    mod_state = job["state"].setdefault("module_data", {}).setdefault(self.module_id, {})
                    if engine is not None and hasattr(engine, "_deep_merge"):
                        job["state"]["module_data"][self.module_id] = engine._deep_merge(mod_state, staged["updates"])
                        job["base_updates"] = engine._deep_merge(job.get("base_updates") or {}, staged["updates"])
                    else:
                        mod_state.update(staged["updates"])
                        job.setdefault("base_updates", {}).update(staged["updates"])

            result = await run_pass(job["state"], job["sdk"], job.get("base_updates"))
            own_updates = (result or {}).get("module_data", {}).get(self.module_id)
            if not isinstance(own_updates, dict) or not own_updates:
                return
            if job.get("epoch") != self.epoch:
                return

            sm = self.services.get("session_manager")
            engine = self.services.get("engine")
            chat_hub = self.services.get("chat_hub")
            if sm is None or engine is None:
                return

            live_state = sm.state
            if not self.job_matches_state(job, live_state):
                return
            live_mod = (live_state.get("module_data") or {}).get(self.module_id) or {}
            if self._should_discard_live is not None and self._should_discard_live(live_mod):
                return

            job_turn = int(job.get("turn", 0) or 0)
            live_turn = int(live_state.get("turn", 0) or 0)
            fallback_base = (job["state"].get("module_data") or {}).get(self.module_id) or self._default_data()

            if chat_hub is not None and chat_hub.turn_running():
                patch_saved_turn_archives(
                    sm, engine, self.module_id, sm.active_save_id, job_turn, own_updates,
                    fallback_base=fallback_base, repack=False,
                )
                if live_turn == job_turn:
                    merged = engine._deep_merge(live_mod, own_updates)
                    live_state.setdefault("module_data", {})[self.module_id] = merged
                self.staged_updates = {
                    "save_id": job.get("save_id"),
                    "turn": job_turn,
                    "hist_len": job.get("hist_len", 0),
                    "scene": job.get("scene", ""),
                    "epoch": job.get("epoch"),
                    "updates": deepcopy(own_updates),
                }
                return

            if live_turn == job_turn:
                merged = engine._deep_merge(live_mod, own_updates)
                live_state.setdefault("module_data", {})[self.module_id] = merged
                if getattr(sm, "save_manager", None) is not None:
                    with _ARCHIVE_PATCH_LOCK:
                        sm.save_manager.save_turn(sm.active_save_id, live_state, job_turn)
                    patch_saved_turn_archives(
                        sm, engine, self.module_id, sm.active_save_id, job_turn, own_updates,
                        fallback_base=merged, patch_snapshot=False, patch_swipe=True, repack=True,
                    )
                if chat_hub is not None:
                    await chat_hub.send({"type": "state_update", "state": dict(live_state)})
            elif live_turn > job_turn:
                patch_saved_turn_archives(
                    sm, engine, self.module_id, sm.active_save_id, job_turn, own_updates,
                    fallback_base=fallback_base, repack=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[{self.label}] Background pass failed: {exc}")

    async def on_turn_stopped(self, reason: str, run_pass: Callable) -> None:
        job = self.pending_job
        self.pending_job = None
        if reason != "completed" or job is None:
            return
        prev_task = self.current_task
        if prev_task is not None and not prev_task.done():
            if (
                isinstance(self.current_job_meta, dict)
                and self.current_job_meta.get("save_id") == job.get("save_id")
                and int(self.current_job_meta.get("turn", 0) or 0) < int(job.get("turn", 0) or 0)
                and self.current_job_meta.get("epoch") == job.get("epoch")
            ):
                job["prev_task"] = prev_task
            else:
                prev_task.cancel()
        self.current_job_meta = {
            "save_id": job.get("save_id"),
            "turn": job.get("turn"),
            "hist_len": job.get("hist_len"),
            "scene": job.get("scene"),
            "epoch": job.get("epoch"),
        }
        task = asyncio.get_running_loop().create_task(self._execute_job(job, run_pass))
        self.current_task = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
