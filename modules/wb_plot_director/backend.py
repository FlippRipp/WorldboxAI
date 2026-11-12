"""Plot Director v3 -- observe-then-respond plot threading with an elastic grand arc.

Instead of imposing a rigid outline or jumping in on turn 1, the module first
observes how the player actually plays during an initial observation window.
After turns complete, a non-blocking background pass updates a live profile
(playstyle weights, tone, themes, evidence-backed likes and observed aversions,
attachments to named story elements, engagement patterns, narrative preferences)
by distinguishing what the player proactively initiates from what they merely
react to, and judges the active short-term plot thread.

Cohesion and long-term purpose come from two layers of persistent state:
1. An elastic Narrative Direction (premise, a North Star endgame, upcoming
   horizon milestones, planted foreshadowing seeds with planned payoffs, open
   questions, and recurring elements) that gives the story an overarching plan
   while reshaping smoothly whenever the player pulls in a new direction.
2. Thread History, where every closed thread leaves a lasting consequence.
New threads are adapted to the story's genre and playstyle via thread modes
(spectacle, intrigue, character, variety), grounded in established NPCs, and
vetted by a fit-check critic before going live.
"""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
import re
import zipfile

try:
    from backend.engine import jev_tasks
except ImportError:
    jev_tasks = None

MODULE_ID = "wb_plot_director"

SCHEMA_VERSION = 3
DEFAULT_OBSERVATION_TURNS = 4
THREAD_MODES = ("spectacle", "intrigue", "character", "variety")
ENGAGEMENT_KINDS = ("proactive", "reactive", "indifferent", "resistant")
INJECT_MODES = ("withhold", "hook_only", "active")
PREF_ROLES = ("actor", "target", "mutual", "world")
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
HORIZON_LIST_CAP = 4
SEEDS_LIST_CAP = 4
RECENT_INPUTS_COUNT = 6
FIT_MAX_REJECTS = 2
DEFER_MAX_STREAK = 2
ABANDON_COOLDOWN_FACTOR = 2
PIVOT_COOLDOWN_TURNS = 3  # min turns between pivot-driven thread replacements
# In-world time thresholds (when wb_time_tracker is active; falls back to turns when absent):
MIN_THREAD_LIFESPAN_MINUTES = 180   # 3 hours: rapid-fire dialogue turns (<3h) won't expire an active thread early
COOLDOWN_REST_SKIP_MINUTES = 360    # 6 hours: sleeping/traveling satisfies a normal quiet period once >=1 turn passes
MIN_COOLDOWN_MINUTES = 30           # 30 minutes: rapid dialogue turns (<30m) wait up to 2 extra turns before weaving
TIME_OF_DAY_BANDS = (
    (0, 5, "the dead of night"),
    (5, 8, "early morning"),
    (8, 12, "morning"),
    (12, 14, "midday"),
    (14, 18, "afternoon"),
    (18, 21, "evening"),
    (21, 24, "night"),
)
# Evidence marker distinguishing likes the player typed in themselves from
# AI-observed ones; player-set entries survive a /plot reset.
PLAYER_SET_EVIDENCE = "set directly by the player"
BOOTSTRAP_SCENES = 15
BOOTSTRAP_INPUTS = 15

MOMENTUM_VALUES = ("observing", "building", "steady", "stalled", "resolving")
PREF_WEIGHTS = ("low", "medium", "high")  # likes/dislikes strength, weakest first

# Shared engine services injected by the server at startup (set_services).
# When running inside a live WebSocket turn, on_librarian hands off LLM passes
# to a non-blocking background task spawned at on_turn_stopped so plot
# generation never delays turn completion.
_services: dict = {}
_bg_tasks: set[asyncio.Task] = set()
_current_bg_task: asyncio.Task | None = None
_current_bg_job_meta: dict | None = None
_pending_bg_job: dict | None = None
_staged_bg_updates: dict | None = None
_bg_epoch: int = 0


def set_services(services: dict) -> None:
    global _services
    _services = services or {}


def _cancel_bg_task() -> None:
    global _current_bg_task, _current_bg_job_meta, _pending_bg_job, _staged_bg_updates, _bg_epoch
    _bg_epoch += 1
    _pending_bg_job = None
    _staged_bg_updates = None
    _current_bg_job_meta = None
    for task in list(_bg_tasks):
        if not task.done():
            task.cancel()
    if _current_bg_task is not None and not _current_bg_task.done():
        _current_bg_task.cancel()
    _current_bg_task = None


def _job_matches_state(meta: dict | None, state: dict, allow_later_turn: bool = True) -> bool:
    """True when a background job or staged update still belongs to the timeline
    of `state` (same save, same epoch, not rolled back past the job's turn, and
    the scene at the job's history index has not been deleted or replaced)."""
    if not isinstance(meta, dict):
        return False
    if meta.get("epoch") is not None and meta.get("epoch") != _bg_epoch:
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


def _patch_zip_plot_state(zip_path: Path, engine, own_updates: dict, fallback_base: dict | None = None) -> bool:
    """Merge `own_updates` into `Module_States/wb_plot_director.json` inside an
    existing turn snapshot or swipe variant zip archive on disk."""
    if not zip_path.exists():
        return False
    target_arc = f"Module_States/{MODULE_ID}.json"
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = {}
            existing_plot = None
            for name in zf.namelist():
                raw_bytes = zf.read(name)
                if name.replace("\\", "/") == target_arc:
                    try:
                        existing_plot = json.loads(raw_bytes.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        existing_plot = None
                else:
                    members[name] = raw_bytes
        base_plot = existing_plot if isinstance(existing_plot, dict) else deepcopy(fallback_base or _default_data())
        merged_plot = engine._deep_merge(base_plot, own_updates)
        tmp_path = zip_path.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, raw_bytes in members.items():
                zf.writestr(name, raw_bytes)
            zf.writestr(target_arc, json.dumps(merged_plot, indent=2).encode("utf-8"))
        tmp_path.replace(zip_path)
        return True
    except OSError:
        return False


def _patch_saved_turn_archives(
    sm,
    engine,
    save_id: str | None,
    turn_number: int,
    own_updates: dict,
    fallback_base: dict | None = None,
    patch_snapshot: bool = True,
    patch_swipe: bool = True,
    repack: bool = False,
) -> None:
    """Keep `Snapshots/turn_{N}.zip` and `Swipes/v{active}.zip` in sync when a
    background Plot Director pass finishes after the turn was already saved."""
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
        if _patch_zip_plot_state(snap_path, engine, own_updates, fallback_base=fallback_base):
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
                if _patch_zip_plot_state(swipe_zip, engine, own_updates, fallback_base=fallback_base):
                    changed = True

    if changed and repack and hasattr(save_manager, "_pack_save"):
        try:
            save_manager._pack_save(save_id)
        except OSError:
            pass

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


# --------------------------------------------------------------------------
# Customizable instruction slots
#
# Same contract as wb_core_rpg: each LLM prompt splits into a creative
# "directive" a scenario or story may override via module_instructions, and
# fixed scaffolding (context sections, event blocks, JSON contracts) that
# overrides can never touch, so output parsing cannot break. The defaults are
# the verbatim directive text the prompts have always used.
# --------------------------------------------------------------------------

DIRECTIVE_THREAD_GENERATION = """Design ONE new thread that fits the established genre, tone and current situation, plays to what this player enjoys, and injects challenge or variety at the guided difficulty. Pick the thread `mode` that fits the genre and player profile:
- spectacle (action, combat, high adventure): worthy rivals, escalating battles, and opportunities for the player to pull off epic feats and flex their skills.
- intrigue (mystery, stealth, politics, strategy): secrets to uncover, hidden agendas, faction tensions, or investigations.
- character (romance, social, roleplay, drama): deepening bonds with attached NPCs, personal dilemmas, loyalties, or emotional crossroads.
- variety (slice-of-life, cozy, humor, crafting, exploration): low-stress invitations, local events, charming oddities, or wonders to explore -- adding texture and variety rather than hostile opposition.
GROUND IT: anchor the hook in established characters, places, factions, or unresolved hooks from the material above -- invent at most one new minor element, and only when nothing established fits. Where a past consequence, planted seed, horizon milestone, or open question offers a natural seed, grow the thread from it.
THINK LIKE A TABLETOP RPG GM: Look at what drives this player (their demonstrated tastes, roles, and what their character is pursuing) and use gm_notes to think through a plot driver or situation that gives the player opportunities to play the game the way they want to play it (e.g. a shadowy organization for mystery lovers, compelling rivals with personal chemistry for enemies-to-lovers, or fitting NPCs when the player wants to be the actor in a dynamic). If an idea has a longer-term goal or twist (like a patron secretly using the player against a rival), feel free to work backwards to what would naturally happen first right now and put ONLY that current opening into hook and challenge -- or keep it simple if a direct situation fits best.
RESPECT ROLE & DIRECTION IN LIKES/AVOIDS: Each like or avoid may specify a player role (actor = the player doing it to NPCs; target = having it done to the player; mutual = shared two-way dynamic; world = general theme/setting element) and relational phrasing. NEVER invert the player's role (e.g. if the player likes initiating a trope or transformation as the actor on NPCs, create opportunities where the player is the actor targeting a fitting NPC -- never make the player the victim or target of that trope unless their profile explicitly says role: target)."""

DIRECTIVE_FIT_CHECK = """Reject the candidate if ANY of these fail:
1. Tone/genre fit: it belongs in this story's genre, prevailing tone, and chosen mode (e.g. slice-of-life/variety threads should not force grim crisis, and action/spectacle threads should offer exciting conflict).
2. Grounding: its hook rises from established characters, places, or events -- not a disconnected new invention.
3. Continuity: it does not contradict anything established in the story.
4. Arc fit: it advances, complicates, or gives a fitting breather within the narrative direction and horizon (skip this check if no direction is given).
5. It does not touch any player dislike, and it NEVER inverts the player's role in a liked trope or dynamic (e.g. targeting the player with something they only enjoy doing as the actor to NPCs, or vice versa).
6. Player freedom: the challenge describes opposition or tension in the world, not a course of action -- reject anything that dictates what the player must or should do, or bakes in a specific solution."""

DIRECTIVE_TURN_ASSESSMENT = f"""- thread_engaged: did the player or the scene meaningfully interact with the active thread this turn?
- player_interest: proactive | reactive | indifferent | resistant. "proactive" means the player actively initiated, probed deeper, or leaned into this hook; "reactive" means the player only dealt with it because the narrator put it directly in their path; "indifferent" means their focus was elsewhere; "resistant" means the player actively deflected, cut short, or walked away from the hook.
- scene_mode: withhold | hook_only | active. How the active plot thread should be injected into the storyteller prompt for the NEXT turn. Choose "withhold" when the scene is in or entering an intimate, emotional, heart-to-heart, romantic, or private conversation/moment where plot injection would intrude. Choose "hook_only" when the scene has room for a subtle background rumor or atmosphere from the hook without pushing the thread's complication/adversary. Choose "active" when the scene is open for both the hook and complication.
- thread_resolved: is the thread's challenge definitively over (overcome, defused, or conclusively failed)?
- player_pivot: has the player clearly COMMITTED to a different pursuit than the active thread -- a stated intention, a new goal, leaving the situation behind for good? A brief detour, a side errand, or a single scene elsewhere is NOT a pivot.
- pivot_intent: one sentence naming what the player is now pursuing (only when player_pivot is true).
- opportunity: does the scene end with a natural opening to bring the thread forward next turn?
- nudge: one subtle piece of story material (a rumor, an arrival, a discovery) that could pull the scene toward the thread without contradicting what the player is doing. Never an instruction to the player.
- momentum: building | steady | stalled
- gm_notes: your working tabletop GM scratchpad. Jot down or refine whatever notes help you steer the story (character motives behind the scenes, how the current plot driver is developing, what the player latched onto, or ideas for what comes next).
- next_hook / next_challenge (optional): if the active thread's initial hook has already played out and the plotline is moving to its next natural phase (or adapting to how the player approached it), provide an updated one-sentence hook and/or challenge for what is in play now; leave them empty when the current hook and challenge still apply.
- profile: the updated fast-moving profile fields. playstyle values are integers 0-10 for what the player actually does. tone is the story's prevailing tone in a few words. themes are short phrases. likes are what the player demonstrably enjoys; avoids are what they consistently steer away from in practice -- both carry a weight (low, medium, high) for how strongly the player seems to feel, a role (actor = the player doing it to others/NPCs, target = having it done to the player, mutual = shared two-way dynamic, world = general theme/setting), and one clause of evidence citing what the player actually did (an entry without real evidence does not belong). Always phrase likes and avoids relationally, stating who is the actor and who is the target -- NEVER use bare ambiguous tags (e.g. write "initiating playful transformation on willing NPCs" with role "actor", never a bare tag like "bimbofication" that leaves actor vs target unclear). Learn from PLAYER INITIATIVE, not narrator imposition: add to likes or raise playstyle weights ONLY when the player proactively pursues, initiates, or lingers on something (proactive) -- never merely because the narrator threw an event at them and they reacted (reactive). If the player actively deflects or cuts short a hook type (resistant), record that pattern in avoids with evidence. At most {PROFILE_LIST_CAP} entries per list -- drop the least relevant to make room. Some entries may have been set directly by the player; preserve those (including their weight and role) unless the story clearly contradicts them. The profile's dislikes are set by the player themselves and are NOT yours to change -- do not include dislikes in your reply, and never add a like that contradicts one. A character acting averse to something in-story does not mean the player dislikes it. You may also include updated attachments or engagement (bites_on / ignores) when this turn reveals a clear signal."""

DIRECTIVE_DIRECTION_UPDATE = f"""- premise: 1-2 sentences: the larger arc the story seems to be telling.
- endgame: 1-2 sentences: the overarching North Star or long-term destination giving the story purpose and comfort (an epic culmination for action/adventure, or a fulfilling life/bond milestone for slice-of-life/cozy play). Keep it ELASTIC: if the player has pulled the story in a new direction that diverges from the previous endgame, reshape the endgame around what they are actually building toward now.
- heading: one sentence: where events look to be going next.
- horizon: up to {HORIZON_LIST_CAP} future milestones or phases bridging the current situation to the endgame (drop completed ones; reshape if the player pivoted). Describe world/narrative phases, never scripted player actions.
- planted_seeds: up to {SEEDS_LIST_CAP} foreshadowing seeds ({{seed, payoff}}) -- a subtle detail or hook in the world and what truth or event it will eventually blossom into (drop seeds that have already paid off).
- open_questions: up to {DIRECTION_LIST_CAP} short unresolved questions the story has raised.
- recurring_elements: up to {DIRECTION_LIST_CAP} named characters, places, factions, or motifs worth returning to.
- attachments: named story elements the player keeps gravitating to (name, kind, why they matter).
- engagement: bites_on -- hook shapes this player pursues (update with what this thread's fate reveals); ignores -- hook shapes they let pass by.
- narrative: pacing, agency, and emotional register preferences the play so far demonstrates.
- notes: anything else a storyteller should know about this player, a few sentences.
Keep everything grounded in what actually happened -- never invent."""

DIRECTIVE_PROFILE_BOOTSTRAP = f"""Base the profile on demonstrated behavior, not on what the setting suggests. Every field:
- playstyle: integers 0-10 for how much the player actually does each thing.
- tone: the story's prevailing tone in a few words.
- themes: short phrases for the story's recurring themes.
- likes: what the player demonstrably enjoys -- each with a weight (low, medium, high) for how strongly they seem to feel, a role (actor = the player doing it to others/NPCs, target = having it done to the player, mutual = shared two-way dynamic, world = general theme/setting), and one clause of evidence citing what they actually did. Always phrase relationally, stating who is the actor and who is the target -- NEVER use bare ambiguous 1-word tags that leave actor vs target unclear.
- avoids: what the player consistently steers away from in practice -- observed behavior only, each with weight, role (actor|target|mutual|world), and evidence, phrased relationally (who is actor vs target). These are soft observations, not vetoes.
- attachments: named characters, places, factions, or objects the player keeps returning to -- name, kind, and a short note on why they seem to matter.
- engagement: bites_on lists the shapes of hooks this player pursues ("a mystery with a personal stake"); ignores lists shapes they let pass by.
- narrative: pacing (slow-burn vs action-forward), agency (drives the story vs reacts to it), register (the emotional payoffs that visibly land).
- notes: anything else a storyteller should know about this player, a few sentences.
At most {PROFILE_LIST_CAP} entries per list. Do not infer dislikes -- those are set by the player themselves, and a character acting averse to something in-story does not mean the player dislikes it."""

INSTRUCTION_SLOTS = [
    {
        "id": "thread_generation",
        "label": "Thread Generation",
        "description": "What new plot threads should be like: how they ground in the story, what they draw on, and what flavors to favor.",
        "default": DIRECTIVE_THREAD_GENERATION,
    },
    {
        "id": "fit_check",
        "label": "Thread Quality Gate",
        "description": "The criteria a candidate thread must pass before it goes live.",
        "default": DIRECTIVE_FIT_CHECK,
    },
    {
        "id": "turn_assessment",
        "label": "Turn Assessment",
        "description": "How each turn is judged for thread engagement, pivots, nudges, and the live player profile.",
        "default": DIRECTIVE_TURN_ASSESSMENT,
    },
    {
        "id": "direction_update",
        "label": "Narrative Direction",
        "description": "How the long-term narrative direction and deep player profile evolve when a thread closes.",
        "default": DIRECTIVE_DIRECTION_UPDATE,
    },
    {
        "id": "profile_bootstrap",
        "label": "Profile Bootstrap",
        "description": "How the initial player profile is distilled when the module joins a story already in progress.",
        "default": DIRECTIVE_PROFILE_BOOTSTRAP,
    },
]

_SLOT_DEFAULTS = {slot["id"]: slot["default"] for slot in INSTRUCTION_SLOTS}


def get_instruction_slots() -> list[dict]:
    """Generic module contract: the customizable instruction slots this module
    exposes. The host serves these to the UI and the rewrite endpoints."""
    return [dict(slot) for slot in INSTRUCTION_SLOTS]


def _directive(slot_id: str, instructions: dict | None) -> str:
    """The directive text for a prompt slot: the story/scenario override when
    one is set, otherwise the built-in default."""
    text = str((instructions or {}).get(slot_id) or "").strip()
    return text or _SLOT_DEFAULTS[slot_id]


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
        "mode": "",        # spectacle | intrigue | character | variety | ""
        "hook": "",
        "challenge": "",
        "stakes": "",
        "appeal": "",
        "status": "none",  # none | active | resolved | expired | abandoned
        "created_turn": 0,
        "closed_turn": 0,
        "created_minutes": 0,
        "closed_minutes": 0,
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
        "endgame": "",             # 1-2 sentences: the elastic North Star destination
        "heading": "",             # one sentence: where events look to be going next
        "horizon": [],             # future milestones/phases bridging current play to the endgame
        "planted_seeds": [],       # [{seed, payoff}] -- foreshadowing details and their planned payoffs
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
        "gm_notes": "",
        "momentum": "observing",
        "scene_mode": "active",
        "last_assessment": None,
        "ignored_streak": 0,
        "pending_nudge": "",
        "last_nudge_turn": 0,
        "last_closed_turn": 0,
        "last_closed_minutes": 0,
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
    """Likes/dislikes/avoids: [{text, weight}] entries (plus optional role:
    actor|target|mutual|world), plus an evidence clause on observed lists. Plain
    strings (legacy saves, LLM shortcuts) normalize to medium weight; bad
    weights become medium."""
    source = value if isinstance(value, list) else previous
    cleaned = []
    for item in (source if isinstance(source, list) else []):
        if isinstance(item, dict):
            text = item.get("text", "")
            weight = item.get("weight")
            role = str(item.get("role", "") or "").strip().lower()
            evidence = item.get("evidence", "")
        else:
            text = item
            weight = "medium"
            role = ""
            evidence = ""
        text = str(text).strip()[:PROFILE_ENTRY_MAX_CHARS]
        if not text:
            continue
        if weight not in PREF_WEIGHTS:
            weight = "medium"
        entry = {"text": text, "weight": weight}
        if role in PREF_ROLES:
            entry["role"] = role
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


def _clean_seeds(value, previous: list) -> list:
    """Planted foreshadowing seeds: [{seed, payoff}]. Plain strings normalize
    by splitting on '--' or '->' when present."""
    source = value if isinstance(value, list) else previous
    cleaned = []
    for item in (source if isinstance(source, list) else []):
        if isinstance(item, dict):
            seed = str(item.get("seed", "")).strip()[:PROFILE_ENTRY_MAX_CHARS]
            payoff = str(item.get("payoff", "")).strip()[:PROFILE_ENTRY_MAX_CHARS]
        else:
            raw = str(item).strip()
            if "->" in raw:
                s, p = raw.split("->", 1)
            elif "--" in raw:
                s, p = raw.split("--", 1)
            else:
                s, p = raw, ""
            seed = s.strip()[:PROFILE_ENTRY_MAX_CHARS]
            payoff = p.strip()[:PROFILE_ENTRY_MAX_CHARS]
        if seed:
            cleaned.append({"seed": seed, "payoff": payoff})
    return cleaned[:SEEDS_LIST_CAP]


def _clean_direction(parsed, previous: dict) -> dict:
    """Sanitize an LLM narrative-direction reply; fall back to previous values
    on garbage. Always returns the full direction shape -- _deep_merge merges
    dict-into-dict, so a partial write would leave stale keys behind."""
    previous = previous if isinstance(previous, dict) else _default_direction()
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "premise": str(parsed.get("premise") or previous.get("premise") or "").strip(),
        "endgame": str(parsed.get("endgame") or previous.get("endgame") or "").strip(),
        "heading": str(parsed.get("heading") or previous.get("heading") or "").strip(),
        "horizon": _clean_str_list(
            parsed.get("horizon"), previous.get("horizon", []) or [])[:HORIZON_LIST_CAP],
        "planted_seeds": _clean_seeds(
            parsed.get("planted_seeds"), previous.get("planted_seeds", []) or []),
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


def _parse_seed_prefs(raw, with_evidence: bool = False) -> list:
    """Parse a creation-form preference string ("haggling, sea voyages" --
    commas, semicolons, or newlines) into weighted entries. Deliberate typed
    input, so likes carry the player-set marker and survive resets."""
    entries = []
    seen = set()
    for part in re.split(r"[,;\n]", str(raw or "")):
        text = part.strip()[:PROFILE_ENTRY_MAX_CHARS]
        if not text or _norm_entry(text) in seen:
            continue
        seen.add(_norm_entry(text))
        entry = {"text": text, "weight": "medium"}
        if with_evidence:
            entry["evidence"] = PLAYER_SET_EVIDENCE
        entries.append(entry)
    return entries[:PROFILE_LIST_CAP]


def _seeded_default_data(state: dict) -> dict:
    """The fresh module_data skeleton, with any creation-form likes/dislikes
    (stored on this save's config by the create endpoint) parsed into the
    profile as player-set entries."""
    data = _default_data()
    config = _config(state)
    likes = _parse_seed_prefs(config.get("seed_likes"), with_evidence=True)
    dislikes = _parse_seed_prefs(config.get("seed_dislikes"))
    if likes or dislikes:
        data["profile"] = _merge_player_prefs(data["profile"], likes, dislikes)
        print(f"[Plot Director] Seeded {len(likes)} like(s) and {len(dislikes)} dislike(s) from story creation.")
    return data


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


def _character_sheet_block(state: dict) -> str:
    """The player character's capabilities from the RPG system, so generated
    challenges have something to bite on: favored skills get a stage, and the
    opposition is pitched at abilities that actually exist. Soft-fails to ""
    when the RPG module is absent."""
    rpg = state.get("module_data", {}).get("wb_core_rpg", {})
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


def _direction_block(data: dict) -> str:
    """The evolving macro-arc, injected into generation and fit-check prompts.
    Empty while the story is too young to have a premise."""
    direction = data.get("direction") or {}
    premise = str(direction.get("premise", "")).strip()
    if not premise:
        return ""
    lines = [
        "NARRATIVE DIRECTION (the story's larger arc -- the new thread should "
        "advance, complicate, or pay off part of it, or provide a fitting "
        "breather between milestones):",
        f"Premise: {premise}",
    ]
    endgame = str(direction.get("endgame", "")).strip()
    if endgame:
        lines.append(f"North Star (Endgame): {endgame}")
    heading = str(direction.get("heading", "")).strip()
    if heading:
        lines.append(f"Heading: {heading}")
    horizon = [str(h).strip() for h in direction.get("horizon", []) if str(h).strip()]
    if horizon:
        lines.append("Horizon milestones (upcoming phases): " + " -> ".join(horizon))
    seeds = _clean_seeds(direction.get("planted_seeds"), [])
    if seeds:
        seed_strs = [
            f"{s['seed']} (payoff: {s['payoff']})" if s.get("payoff") else s["seed"]
            for s in seeds
        ]
        lines.append("Planted seeds (foreshadowing to grow or cash in when ripe): " + "; ".join(seed_strs))
    questions = [str(q).strip() for q in direction.get("open_questions", []) if str(q).strip()]
    if questions:
        lines.append("Open questions: " + "; ".join(questions))
    recurring = [str(r).strip() for r in direction.get("recurring_elements", []) if str(r).strip()]
    if recurring:
        lines.append("Recurring elements: " + ", ".join(recurring))
    return "\n".join(lines) + "\n\n"


def _get_time_clock(state: dict) -> dict | None:
    """Return the active wb_time_tracker clock dict if present, else None."""
    time_data = (state.get("module_data") or {}).get("wb_time_tracker")
    if not isinstance(time_data, dict):
        return None
    clock = time_data.get("clock")
    if not isinstance(clock, dict):
        return None
    return clock


def _current_minutes(state: dict) -> int | None:
    """Return total in-world minutes elapsed from wb_time_tracker, or None when
    the time module is inactive/uninitialized."""
    clock = _get_time_clock(state)
    if clock is None:
        return None
    try:
        return int(clock.get("total_minutes_elapsed", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _time_of_day_label(hour: int) -> str:
    for start, end, label in TIME_OF_DAY_BANDS:
        if start <= hour < end:
            return label
    return "night"


def _format_clock_text(clock: dict) -> str:
    year = int(clock.get("year", 1) or 1)
    month = int(clock.get("month", 1) or 1)
    day = int(clock.get("day", 1) or 1)
    hour = int(clock.get("hour", 8) or 0)
    minute = int(clock.get("minute", 0) or 0)
    return (
        f"Year {year}, Month {month}, Day {day} -- "
        f"{hour:02d}:{minute:02d} ({_time_of_day_label(hour)})"
    )


def _format_duration_minutes(minutes: int) -> str:
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


def _should_expire_thread(state: dict, thread: dict, turn: int, thread_max_turns: int) -> bool:
    """Whether an active thread has reached the end of its lifespan.
    When wb_time_tracker is active, rapid-fire dialogue turns (<3 hours of
    in-world time) do not prematurely expire an active thread at thread_max_turns,
    up to a hard safety ceiling of 2x thread_max_turns."""
    turns_elapsed = turn - int(thread.get("created_turn", 0) or 0)
    if turns_elapsed < thread_max_turns:
        return False
    cur_minutes = _current_minutes(state)
    if cur_minutes is None:
        return True
    if turns_elapsed >= thread_max_turns * 2:
        return True
    created_minutes = int(thread.get("created_minutes", 0) or 0)
    return max(0, cur_minutes - created_minutes) >= MIN_THREAD_LIFESPAN_MINUTES


def _in_quiet_period(state: dict, data: dict, turn: int) -> bool:
    """Whether the story is still in its between-thread breather.
    Falls back to pure turn counting when wb_time_tracker is absent.
    When wb_time_tracker is active:
    - A major in-world rest/travel skip (>=6h, or >=12h after an abandoned
      thread) satisfies the breather once at least 1 turn has passed.
    - Rapid 1-minute dialogue turns where <30 in-world minutes have passed
      extend the breather by up to 2 extra turns so a new plot thread does not
      drop into the middle of the same conversation."""
    next_thread_turn = int(data.get("next_thread_turn", 0) or 0)
    last_closed_turn = int(data.get("last_closed_turn", 0) or 0)
    if next_thread_turn <= 0 or (last_closed_turn > 0 and next_thread_turn <= last_closed_turn):
        return False

    cur_minutes = _current_minutes(state)
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


def _setting_material(state: dict) -> list[str]:
    """Scenario, world rules/lore, and in-world clock blocks shared by every
    generation prompt."""
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

    clock = _get_time_clock(state)
    if clock is not None:
        parts.append(
            f"<in_world_time>\nCurrent in-world date and time: {_format_clock_text(clock)}.\n</in_world_time>"
        )

    return parts


def _recent_player_inputs(state: dict, limit: int = RECENT_INPUTS_COUNT) -> list[str]:
    """Recent non-empty player messages (oldest to newest)."""
    inputs = [
        str(m.get("content", "")).strip()
        for m in state.get("chat_messages", [])
        if m.get("role") == "user" and str(m.get("content", "")).strip()
    ]
    return inputs[-limit:] if limit > 0 else inputs


def _story_material(state: dict) -> str:
    """Collect the material threads are woven from: scenario, world rules/lore,
    the most recent scenes, and the player's own recent actions."""
    parts = _setting_material(state)

    history = state.get("history", [])
    if history:
        latest = str(history[-1])
        earlier = "\n".join(str(h) for h in history[-6:-1])
        story = (earlier + "\n" if earlier else "") + latest
        parts.append(f"<story_so_far>\n{story}\n</story_so_far>")

    recent_inputs = _recent_player_inputs(state)
    if recent_inputs:
        inputs_text = "\n".join(f"- {inp}" for inp in recent_inputs)
        parts.append(f"<recent_player_actions>\n{inputs_text}\n</recent_player_actions>")

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


def _is_profile_unobserved(profile: dict) -> bool:
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


def _in_initial_observation(state: dict, data: dict) -> bool:
    """Whether a fresh story is still in its initial observation window before
    the first direction and plot thread are woven."""
    if data.get("status") != "observing" or data.get("thread_history"):
        return False
    if (data.get("thread") or {}).get("status") == "active":
        return False
    config = _config(state)
    turn = int(state.get("turn", 0) or 0)
    if "observation_turns" in config:
        obs_turns = max(0, int(config.get("observation_turns") or 0))
        return turn < obs_turns
    # Default observation window applies when neither direction nor observed
    # profile exists yet (a genuinely fresh story).
    has_direction = bool(str((data.get("direction") or {}).get("premise", "")).strip())
    if has_direction or not _is_profile_unobserved(data.get("profile") or {}):
        return False
    return turn < DEFAULT_OBSERVATION_TURNS


async def _bootstrap_profile(state: dict, sdk, preserved: dict | None = None) -> dict | None:
    """One-time deep read when the module joins a story already in progress (or
    finishes its initial observation window): distill an initial player profile
    from the whole story so far instead of starting blind. ``preserved``
    optionally carries player-stated likes and dislikes that survived a reset
    or were seeded at story creation -- they anchor the analysis and are merged
    back by the caller. Returns the sanitized profile, or None on a bad
    reply."""
    config = _config(state)

    preserved_block = ""
    if preserved and (preserved.get("likes") or preserved.get("dislikes")):
        def _fmt(entries):
            return "; ".join(
                f"{e.get('text', '')} ({e.get('weight', 'medium')}"
                + (f", role: {e['role']}" if e.get("role") else "")
                + ")"
                for e in entries
            )
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

    scenes = "\n\n".join(str(h) for h in state.get("history", [])[-BOOTSTRAP_SCENES:])
    inputs = _recent_player_inputs(state, limit=BOOTSTRAP_INPUTS)
    inputs_block = "\n".join(f"- {i}" for i in inputs)
    setting = "\n\n".join(_setting_material(state))

    prompt = f"""You are joining a text RPG already in progress. Study the story so far and build an initial profile of the player: how they actually play, the story's prevailing tone, its recurring themes, and what the player seems to enjoy or avoid. Base your conclusions on what the PLAYER actively initiated, chose, or leaned into in their own inputs -- not merely what the narrator described around them.

{setting}

STORY SO FAR (oldest to newest):
{scenes}

THE PLAYER'S OWN ACTIONS (their typed inputs, oldest to newest):
{inputs_block or '(none recorded)'}

{preserved_block}{_directive("profile_bootstrap", state.get("module_instructions"))}

Respond with ONLY valid JSON:
{{"playstyle": {{{", ".join(f'"{k}": 0' for k in PLAYSTYLE_KEYS)}}}, "tone": "...", "themes": [], "likes": [{{"text": "...", "weight": "low|medium|high", "role": "actor|target|mutual|world", "evidence": "..."}}], "avoids": [{{"text": "...", "weight": "low|medium|high", "role": "actor|target|mutual|world", "evidence": "..."}}], "attachments": [{{"name": "...", "kind": "...", "note": "..."}}], "engagement": {{"bites_on": [], "ignores": []}}, "narrative": {{"pacing": "...", "agency": "...", "register": "..."}}, "notes": "..."}}"""

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


def _history_block(data: dict, state: dict | None = None) -> str:
    """Closed threads with their lasting consequences -- the story's memory.
    Generation builds on these instead of merely avoiding repeats."""
    cur_minutes = _current_minutes(state) if state is not None else None
    past = []
    for entry in data.get("thread_history", [])[-6:]:
        if not entry.get("title"):
            continue
        fallout = str(entry.get("consequence") or entry.get("note") or "").strip()
        closed_turn = entry.get("closed_turn", 0)
        when_label = f"turn {closed_turn}"
        if cur_minutes is not None and "closed_minutes" in entry:
            elapsed = max(0, cur_minutes - int(entry.get("closed_minutes", 0) or 0))
            when_label += f", {_format_duration_minutes(elapsed)} ago in-world"
        line = f'- "{entry.get("title", "")}" -- {entry.get("outcome", "")} ({when_label})'
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


def _gm_notes_block(data: dict) -> str:
    """The Game Master's persistent backstage working notes and plot plans."""
    notes = str((data or {}).get("gm_notes") or "").strip()
    if not notes:
        return ""
    return f"GM NOTEBOOK (your backstage working notes and plot plans):\n{notes}\n\n"


def _planning_iterations(config: dict) -> int:
    """Number of GM planning passes configured (1-3). Defaults to 2 in live
    server sessions (matching manifest.json) and 1 when unset in unit tests."""
    if "planning_iterations" in (config or {}):
        try:
            return max(1, min(3, int(config.get("planning_iterations") or 1)))
        except (TypeError, ValueError):
            return 1
    return 2 if _services.get("engine") is not None else 1


async def _iterate_gm_notes(state: dict, sdk, data: dict, passes: int = 1) -> str:
    """Run 1-2 lightweight GM Notebook passes to brainstorm or refine plot
    drivers, character motives, and player-tailored opportunities."""
    config = _config(state)
    profile = data.get("profile") or _default_profile()
    notes = str(data.get("gm_notes") or "").strip()
    model_pref = config.get("thread_ai_model", "smartest")

    for _ in range(max(0, int(passes))):
        cur_data = {**data, "gm_notes": notes}
        prompt = f"""You maintain the working Game Master notebook for a tabletop-style text RPG.
Your goal is not to script a novel, but to think like a tabletop RPG GM: understand what drives this player (their demonstrated tastes, roles, and what their character is pursuing) and sketch or refine plot drivers, character motives, and situations that give the player opportunities to play the game the way they want to play it.

If an idea has a longer-term goal or hidden motive (like a patron hoping to use the player against a rival, a mysterious organization, or a slow-burn dynamic), feel free to work backwards from that core idea to what would naturally surface first right now -- or keep it simple if a direct, immediate situation fits best. Do not force a multi-step plan when a simple one is more natural.

PLAYER PROFILE:
{json.dumps(profile, ensure_ascii=False)}

{_direction_block(cur_data)}{_gm_notes_block(cur_data)}STORY MATERIAL:
{_story_material(state)}

{_storylines_block(state)}{_character_roster(state)}{_character_sheet_block(state)}{_history_block(cur_data, state)}Write or refine your working GM notes based on the latest story state.

Respond with ONLY valid JSON:
{{"gm_notes": "your freeform GM working notes and thoughts"}}"""
        try:
            sdk.llm._current_module = MODULE_ID
            raw = await sdk.llm.generate(prompt, model_preference=model_pref)
        finally:
            sdk.llm._current_module = ""
        parsed = _parse_json_block(raw)
        if isinstance(parsed, dict) and str(parsed.get("gm_notes") or "").strip():
            notes = str(parsed.get("gm_notes")).strip()
    return notes


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

    iterations = _planning_iterations(config)
    gm_notes = str(data.get("gm_notes") or "").strip()
    if iterations > 1 and not critique:
        gm_notes = await _iterate_gm_notes(
            state, sdk, {**data, "gm_notes": gm_notes}, passes=iterations - 1
        )
    data_with_notes = {**data, "gm_notes": gm_notes}

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
        "\n\nIf the current scene is mid-climax, mid-crisis, or in an intimate / "
        "emotional / heart-to-heart conversation -- a moment where introducing new "
        "plot material would intrude rather than enrich -- respond instead with "
        'ONLY: {"defer": true}'
        if may_defer else ""
    )

    prompt = f"""You design short plot threads for a text RPG storyteller. A thread is a small self-contained arc the storyteller can weave in over roughly the next {thread_max_turns} turns: a hook that enters the story naturally, a challenge or situational tension that gives the thread shape, and stakes. The player will see the thread openly, so make it enticing, not a spoiler-dependent twist.

A challenge is opposition or tension that exists in the world -- forces, obstacles, mysteries, or agendas in play (or in character/variety modes, a personal dilemma, invitation, or lively situation). It must NEVER prescribe the player's actions, name a required solution, or assume how they will respond: what to do about it, and whether to engage at all, is entirely theirs.

PLAYER PROFILE (cater to their demonstrated preferences; weight marks how strongly the player feels -- give high-weight entries the most influence. Dislikes were set directly by the player: steer firmly away from them. Avoids are observed aversions: steer away unless the profile suggests otherwise. Attachments name what the player cares about -- prefer anchoring threads in them. engagement.ignores are hook shapes this player lets pass by -- avoid those shapes):
{json.dumps(profile, ensure_ascii=False)}

CHALLENGE DIFFICULTY:
{DIFFICULTY_GUIDANCE[level]}

{_direction_block(data_with_notes)}{_gm_notes_block(data_with_notes)}STORY MATERIAL:
{_story_material(state)}

{_storylines_block(state)}{_character_roster(state)}{_character_sheet_block(state)}{_history_block(data_with_notes, state)}{_directive("thread_generation", state.get("module_instructions"))}{different}{critique_block}{pivot_block}{defer_block}

Respond with ONLY valid JSON:
{{"title": "3-6 words", "mode": "spectacle|intrigue|character|variety", "hook": "how it surfaces in the story right now, one sentence", "challenge": "what stands in the way right now -- the opposition or situational tension itself, one sentence, never what the player should do about it", "stakes": "what is at risk or what could be gained, one sentence", "appeal": "which player preference this serves, a few words", "scene_mode": "withhold|hook_only|active", "gm_notes": "your updated working GM notes and backstage thoughts (optional)"}}"""

    model_pref = config.get("thread_ai_model", "smartest")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if isinstance(parsed, dict) and str(parsed.get("gm_notes") or "").strip():
        gm_notes = str(parsed.get("gm_notes")).strip()

    if may_defer and isinstance(parsed, dict) and parsed.get("defer") is True:
        print(f"[Plot Director] Turn {turn}: generation deferred -- story mid-climax.")
        ret = {
            "gen_attempts": 0,
            "defer_streak": defer_streak + 1,
            "next_thread_turn": turn + 2,
            "momentum": "observing",
        }
        if gm_notes:
            ret["gm_notes"] = gm_notes
        return ret

    title = str(parsed.get("title", "")).strip() if isinstance(parsed, dict) else ""
    hook = str(parsed.get("hook", "")).strip() if isinstance(parsed, dict) else ""
    challenge = str(parsed.get("challenge", "")).strip() if isinstance(parsed, dict) else ""
    raw_mode = str(parsed.get("mode", "")).strip().lower() if isinstance(parsed, dict) else ""
    mode = raw_mode if raw_mode in THREAD_MODES else ""
    raw_scene_mode = str(parsed.get("scene_mode", "")).strip().lower() if isinstance(parsed, dict) else ""
    scene_mode = raw_scene_mode if raw_scene_mode in INJECT_MODES else "active"

    if not (title and hook and challenge):
        status = "failed" if attempts >= GEN_MAX_ATTEMPTS else data.get("status", "observing")
        if status == "failed":
            print(f"[Plot Director] Thread generation failed {attempts} times; going dormant.")
        return {"gen_attempts": attempts, "status": status}

    cur_minutes = _current_minutes(state)
    print(f"[Plot Director] Turn {turn}: new thread ready -- {title}")
    ret = {
        "gen_attempts": 0,
        "status": "active",
        "momentum": "building",
        "scene_mode": scene_mode,
        "last_assessment": None,
        "ignored_streak": 0,
        "defer_streak": 0,
        "thread": _full_thread(
            id=f"t{turn}",
            title=title,
            mode=mode,
            hook=hook,
            challenge=challenge,
            stakes=str(parsed.get("stakes", "")).strip(),
            appeal=str(parsed.get("appeal", "")).strip(),
            status="active",
            created_turn=turn,
            created_minutes=cur_minutes if cur_minutes is not None else 0,
        ),
    }
    if gm_notes:
        ret["gm_notes"] = gm_notes
    return ret


async def _fit_check(state: dict, sdk, data: dict, candidate: dict) -> tuple[bool, str]:
    """Independent critic pass over a candidate thread: does it actually fit
    this story and this player? Fails open -- a malformed reply accepts, and
    the caller force-accepts after repeated rejections -- so an over-strict
    critic can never push the module dormant."""
    profile = data.get("profile") or _default_profile()
    shown = {k: candidate.get(k, "") for k in ("title", "hook", "challenge", "stakes", "appeal")}
    if candidate.get("mode"):
        shown["mode"] = candidate["mode"]

    prompt = f"""You are a quality gate for plot threads in a text RPG. Judge ONE candidate thread strictly on fit, not on cleverness.

{_direction_block(data)}PLAYER PROFILE (dislikes are hard vetoes; avoids are soft observed aversions):
{json.dumps(profile, ensure_ascii=False)}

STORY MATERIAL:
{_story_material(state)}

{_storylines_block(state)}{_character_roster(state)}CANDIDATE THREAD:
{json.dumps(shown, ensure_ascii=False)}

{_directive("fit_check", state.get("module_instructions"))}

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

    prompt = f"""You maintain the long-term narrative direction of a text RPG -- a living plan of where the story is heading (its premise, its elastic North Star endgame, upcoming horizon milestones, and foreshadowed seeds), used to give the story overarching purpose while reshaping smoothly when the player pulls in a new direction. You also keep the slow-moving parts of the player's profile current.

CURRENT NARRATIVE DIRECTION (evolve it, don't rewrite from scratch; empty means the story is young):
{json.dumps({k: direction[k] for k in ("premise", "endgame", "heading", "horizon", "planted_seeds", "open_questions", "recurring_elements")}, ensure_ascii=False)}

CURRENT PLAYER PROFILE:
{json.dumps(profile, ensure_ascii=False)}

{event_block}{_storylines_block(state)}STORY MATERIAL:
{_story_material(state)}

Update:
{consequence_rule}{_directive("direction_update", state.get("module_instructions"))}

Respond with ONLY valid JSON:
{{"consequence": "...", "premise": "...", "endgame": "...", "heading": "...", "horizon": [], "planted_seeds": [{{"seed": "...", "payoff": "..."}}], "open_questions": [], "recurring_elements": [], "attachments": [{{"name": "...", "kind": "...", "note": "..."}}], "engagement": {{"bites_on": [], "ignores": []}}, "narrative": {{"pacing": "...", "agency": "...", "register": "..."}}, "notes": "..."}}"""

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
                     note: str = "", consequence: str = "", cooldown: int = 0,
                     closed_minutes: int | None = None) -> None:
    """Close the active thread into history, clear the nudge, and start the
    quiet period (cooldown 0 keeps the old replace-immediately behavior). The
    caller regenerates afterwards (overwriting "thread" on success); writing
    the terminal thread here keeps state consistent when regeneration fails --
    the next pass then retries via the no-active-thread branch."""
    updates["thread"] = _full_thread(**{
        **{k: thread.get(k, v) for k, v in _empty_thread().items()},
        "status": outcome,
        "closed_turn": turn,
        "closed_minutes": closed_minutes if closed_minutes is not None else int(thread.get("closed_minutes", 0) or 0),
    })
    history = list(data.get("thread_history", []))
    entry = {
        "title": thread.get("title", ""),
        "outcome": outcome,
        "created_turn": int(thread.get("created_turn", 0) or 0),
        "closed_turn": turn,
        "note": note,
        "consequence": consequence,
    }
    if closed_minutes is not None:
        entry["closed_minutes"] = closed_minutes
    history.append(entry)
    updates["thread_history"] = history[-THREAD_HISTORY_CAP:]
    updates["pending_nudge"] = ""
    updates["scene_mode"] = "active"
    updates["last_assessment"] = None
    updates["last_closed_turn"] = turn
    if closed_minutes is not None:
        updates["last_closed_minutes"] = closed_minutes
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
                     note=note, consequence=consequence, cooldown=cooldown,
                     closed_minutes=_current_minutes(state))
    if turn >= int(updates["next_thread_turn"]):
        updates.update(await _generate_checked_thread(
            state, sdk, {**data, **updates},
            avoid_previous_kind=avoid_previous_kind,
            allow_defer=not pivot_intent,  # a pivot means the player is moving NOW
            pivot_intent=pivot_intent))


async def on_gather_context(state: dict, sdk) -> dict:
    global _staged_bg_updates
    if _current_bg_job_meta is not None and not _job_matches_state(_current_bg_job_meta, state):
        _cancel_bg_task()
    if _staged_bg_updates is not None and not _job_matches_state(_staged_bg_updates, state):
        _staged_bg_updates = None

    data = _own_data(state)
    if not data:
        seeded = _seeded_default_data(state)
        sm = _services.get("session_manager")
        engine = _services.get("engine")
        if sm is not None and engine is not None:
            _patch_saved_turn_archives(
                sm,
                engine,
                state.get("active_save_id") or getattr(sm, "active_save_id", None),
                int(state.get("turn", 0) or 0),
                seeded,
                fallback_base=seeded,
            )
        return {"module_data": {MODULE_ID: seeded}}

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

    # The active thread reaches the storyteller through the plot_thread
    # prompt block (a dedicated, visible system block) -- not through the
    # engine-context grab bag, where it proved easy to overlook.
    return {}


MODE_STORYTELLER_GUIDANCE = {
    "spectacle": "Mode: Spectacle/Action -- stage cinematic set-pieces, worthy opposition, and openings for the player to pull off impressive feats.",
    "intrigue": "Mode: Intrigue/Mystery -- weave in subtle clues, hidden agendas, and tension that reward curiosity without forcing a confrontation.",
    "character": "Mode: Character/Heart -- center interpersonal chemistry, personal stakes, and emotional resonance with the characters involved.",
    "variety": "Mode: Slice-of-Life/Variety -- treat this as a low-pressure invitation or colorful world event that adds charm and variety without manufacturing crisis.",
}


async def on_render_prompt_block(block: dict, state: dict, sdk) -> dict:
    """The active thread's standing guidance to the storyteller: its own
    labeled system block, so it is discoverable in the prompt and carries
    real weight -- advance the thread when the scene gives it room, follow
    the player's lead, never railroad. An armed nudge rides along for
    exactly one turn (consumed by the librarian)."""
    global _staged_bg_updates
    if block.get("id") != "plot_thread":
        return {"content": ""}
    config = _config(state)
    data = _own_data(state)
    if _staged_bg_updates is not None and isinstance(data, dict):
        if _job_matches_state(_staged_bg_updates, state) and isinstance(_staged_bg_updates.get("updates"), dict):
            data = deepcopy(data)
            data.update(_staged_bg_updates["updates"])
        else:
            _staged_bg_updates = None

    thread = (data.get("thread") or {}) if data else {}
    if (
        not data
        or not config.get("plot_enabled", True)
        or data.get("suspended")
        or data.get("status") != "active"
        or thread.get("status") != "active"
    ):
        return {"content": ""}

    turn = int(state.get("turn", 0) or 0)
    bg_in_flight = (
        (_current_bg_task is not None and not _current_bg_task.done())
        or (_pending_bg_job is not None)
    )
    raw_assessment = data.get("last_assessment")
    fresh_assessment = (
        raw_assessment
        if (
            isinstance(raw_assessment, dict)
            and not bg_in_flight
            and int(raw_assessment.get("turn", -1)) == turn
        )
        else None
    )
    assessment_missing = bg_in_flight or (
        isinstance(raw_assessment, dict) and int(raw_assessment.get("turn", -1)) < turn
    )

    jev_mode = None
    if jev_tasks is not None and getattr(getattr(sdk, "llm", None), "decide", None) is not None:
        jev_mode = await jev_tasks.plot_injection(
            sdk, state, thread, last_assessment=fresh_assessment
        )

    if jev_mode in INJECT_MODES:
        inject_mode = jev_mode
    elif assessment_missing:
        inject_mode = "withhold"
    elif fresh_assessment is not None:
        raw_mode = str(fresh_assessment.get("scene_mode") or "").strip().lower()
        inject_mode = raw_mode if raw_mode in INJECT_MODES else "withhold"
    else:
        raw_mode = str(data.get("scene_mode", "active") or "").strip().lower()
        inject_mode = raw_mode if raw_mode in INJECT_MODES else "active"

    if inject_mode == "withhold":
        return {"content": ""}

    if inject_mode == "hook_only":
        thread_line = f"Thread: \"{thread.get('title', '')}\". {thread.get('hook', '')}"
    else:
        thread_line = (
            f"Thread: \"{thread.get('title', '')}\". {thread.get('hook', '')} "
            f"Complication: {thread.get('challenge', '')}"
        )

    lines = [
        "An ongoing plot thread runs through this story -- living story "
        "material, not a script. Advance it whenever the scene gives it room "
        "(an encounter, a rumor, a consequence, a shift in the air), and give "
        "it visible momentum when the player isn't steering somewhere else. "
        "Follow the player's lead first: never override their current action, "
        "never force the thread into a scene it doesn't fit, and never mention "
        "that a thread exists.",
        thread_line,
    ]
    if inject_mode == "hook_only":
        lines.append(
            "Scene pacing: Keep this thread to a light background hook or rumor "
            "only if it fits naturally -- hold back active complications or "
            "adversaries this turn."
        )
    mode = str(thread.get("mode") or "").strip().lower()
    if mode in MODE_STORYTELLER_GUIDANCE:
        lines.append(MODE_STORYTELLER_GUIDANCE[mode])
    seeds = _clean_seeds((data.get("direction") or {}).get("planted_seeds"), [])
    if seeds:
        seed_hints = "; ".join(s["seed"] for s in seeds[:2] if s.get("seed"))
        if seed_hints:
            lines.append(
                f"Background foreshadowing seeds (touch on lightly only when a scene naturally invites it): {seed_hints}"
            )
    stall_threshold = max(1, int(config.get("stall_threshold", 3) or 3))
    if int(data.get("ignored_streak", 0) or 0) >= stall_threshold:
        lines.append(
            "The player has been pursuing other things -- keep the thread "
            "present as quiet background pressure, resurfacing it lightly "
            "rather than pushing."
        )
    nudge = str(data.get("pending_nudge") or "").strip()
    if nudge:
        lines.append(f"A natural way to move it forward this turn, if the scene allows: {nudge}")
    return {"content": "\n".join(lines)}


def _is_live_turn_armed() -> bool:
    """True when running inside a live server turn that will fire on_turn_stopped,
    so heavy LLM passes can run in a non-blocking background task after the
    turn completes."""
    engine = _services.get("engine")
    sm = _services.get("session_manager")
    return bool(engine is not None and sm is not None and getattr(engine, "_turn_hooks_armed", False))


async def _run_librarian_llm_pass(state: dict, sdk, base_updates: dict | None = None) -> dict | None:
    """The core LLM analysis/generation pass for on_librarian. Runs inline when
    called directly (e.g. unit tests) and in a non-blocking background task
    after turn completion during live play."""
    config = _config(state)
    data = deepcopy(_own_data(state) or _default_data())
    updates: dict = dict(base_updates or {})
    if updates:
        data.update(updates)

    turn = state.get("turn", 0)
    thread = data.get("thread") or _empty_thread()
    cooldown = max(0, int(config.get("thread_cooldown_turns", 3) or 0))

    # Fresh stories wait out the initial observation window before seeding a
    # direction or weaving the first thread, so the opening turns can breathe
    # and establish real player behavior first.
    if _in_initial_observation(state, data):
        return {"module_data": {MODULE_ID: updates}} if updates else None

    # When no thread is active and we are still inside a quiet period, spend
    # zero LLM calls.
    if thread.get("status") != "active" and _in_quiet_period(state, data, turn):
        return {"module_data": {MODULE_ID: updates}} if updates else None

    # Joining a story already in progress (or finishing the initial observation
    # window) with an unobserved profile: read the story so far once, so both
    # the initial direction and the first thread cater to how the player has
    # actually been playing. Preserves any player-seeded likes/dislikes.
    if (
        thread.get("status") != "active"
        and not data.get("thread_history")
        and turn >= 2
        and _is_profile_unobserved(data.get("profile") or {})
    ):
        cur_profile = _clean_profile({}, data.get("profile") or _default_profile())
        kept_dislikes = cur_profile["dislikes"]
        kept_likes = [e for e in cur_profile["likes"] if e.get("evidence") == PLAYER_SET_EVIDENCE]
        preserved = {"likes": kept_likes, "dislikes": kept_dislikes} if (kept_likes or kept_dislikes) else None
        bootstrapped = await _bootstrap_profile(state, sdk, preserved=preserved)
        if bootstrapped is not None:
            if preserved:
                bootstrapped = _merge_player_prefs(bootstrapped, kept_likes, kept_dislikes)
            print(f"[Plot Director] Turn {turn}: profile bootstrapped from the story so far.")
            updates["profile"] = bootstrapped
            data["profile"] = bootstrapped

    # Seed the initial narrative direction once the observation window has
    # passed (or for migrated saves with a thread already running). Two failed
    # reads stop the retries.
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
                    deep_fields, updates.get("profile") or data.get("profile") or _default_profile())
            print(f"[Plot Director] Turn {turn}: initial narrative direction established.")
        else:
            updates["direction_seed_attempts"] = int(data.get("direction_seed_attempts", 0) or 0) + 1

    # No active thread (observation finished or previous thread closed): weave one.
    if thread.get("status") != "active":
        last_outcome = (data.get("thread_history") or [{}])[-1].get("outcome", "")
        updates.update(await _generate_checked_thread(
            state, sdk, {**data, **updates},
            avoid_previous_kind=last_outcome == "abandoned", allow_defer=True))
        return {"module_data": {MODULE_ID: updates}}

    # Deterministic expiry, checked before spending an assessment call.
    thread_max_turns = max(1, int(config.get("thread_max_turns", 12) or 12))
    if _should_expire_thread(state, thread, turn, thread_max_turns):
        print(f"[Plot Director] Turn {turn}: thread expired -- {thread.get('title', '')}")
        await _close_thread(state, sdk, data, updates, thread, "expired", turn, cooldown=cooldown)
        return {"module_data": {MODULE_ID: updates}}

    frequency = max(1, int(config.get("assessment_frequency", 1) or 1))
    if turn == 0 or turn % frequency != 0 or turn <= int(thread.get("created_turn", 0) or 0):
        return {"module_data": {MODULE_ID: updates}} if updates else None

    history = state.get("history", [])
    latest = str(history[-1])
    earlier = "\n".join(str(h) for h in history[-3:-1])
    earlier_block = f"EARLIER NARRATION (context only):\n{earlier}\n\n" if earlier else ""

    recent_inputs = _recent_player_inputs(state)
    player_input = recent_inputs[-1] if recent_inputs else ""
    earlier_inputs = recent_inputs[:-1]
    earlier_inputs_block = (
        "EARLIER PLAYER INPUTS (trajectory of what the player has been choosing to do):\n"
        + "\n".join(f"- {inp}" for inp in earlier_inputs) + "\n\n"
        if earlier_inputs else ""
    )

    # The seed pass above may have just written deep profile fields; build on
    # that version so this turn's assessment doesn't clobber them.
    profile = updates.get("profile") or data.get("profile") or _default_profile()

    prompt = f"""You maintain a live profile of the player and track one active plot thread in a text RPG. The thread is visible to the player; your profile drives what threads they get next.

ACTIVE PLOT THREAD:
Title: {thread.get('title', '')}
Hook: {thread.get('hook', '')}
Challenge: {thread.get('challenge', '')}
Stakes: {thread.get('stakes', '')}

{_gm_notes_block(data)}CURRENT PLAYER PROFILE (update it with what this turn reveals; evolve, don't rewrite from scratch):
{json.dumps(profile, ensure_ascii=False)}

{_storylines_block(state)}{earlier_block}THIS TURN'S SCENE (judge this):
{latest}

{earlier_inputs_block}LAST PLAYER INPUT:
{player_input or '(none)'}

Judge and update:
{_directive("turn_assessment", state.get("module_instructions"))}

Respond with ONLY valid JSON:
{{"thread_engaged": false, "player_interest": "proactive|reactive|indifferent|resistant", "scene_mode": "withhold|hook_only|active", "thread_resolved": false, "resolution_note": "one sentence, only if resolved", "player_pivot": false, "pivot_intent": "", "opportunity": false, "nudge": "...", "momentum": "steady", "gm_notes": "...", "next_hook": "", "next_challenge": "", "profile": {{"playstyle": {{{", ".join(f'"{k}": 0' for k in PLAYSTYLE_KEYS)}}}, "tone": "...", "themes": [], "likes": [{{"text": "...", "weight": "low|medium|high", "role": "actor|target|mutual|world", "evidence": "..."}}], "avoids": [{{"text": "...", "weight": "low|medium|high", "role": "actor|target|mutual|world", "evidence": "..."}}]}}}}"""

    model_pref = config.get("assessment_ai_model", "balanced")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=model_pref)
    finally:
        sdk.llm._current_module = ""

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict):
        return {"module_data": {MODULE_ID: updates}} if updates else None

    interest = str(parsed.get("player_interest") or "").strip().lower()
    # Active resistance (deflecting or walking away from the hook) counts as
    # not engaging, even if the scene briefly touched the thread.
    engaged = bool(parsed.get("thread_engaged")) and interest != "resistant"
    resolved = bool(parsed.get("thread_resolved"))
    pivot = bool(parsed.get("player_pivot"))
    pivot_intent = str(parsed.get("pivot_intent") or "").strip()
    opportunity = bool(parsed.get("opportunity"))
    nudge_text = str(parsed.get("nudge") or "").strip()
    resolution_note = str(parsed.get("resolution_note") or "").strip()
    momentum = parsed.get("momentum")
    if momentum not in MOMENTUM_VALUES:
        momentum = "steady" if engaged else "stalled"
    raw_scene_mode = str(parsed.get("scene_mode") or "").strip().lower()
    scene_mode = raw_scene_mode if raw_scene_mode in INJECT_MODES else "active"
    gm_notes = str(parsed.get("gm_notes") or "").strip()
    if gm_notes:
        updates["gm_notes"] = gm_notes

    updates["profile"] = _clean_profile(parsed.get("profile"), profile)
    ignored_streak = 0 if engaged else int(data.get("ignored_streak", 0) or 0) + 1
    updates["ignored_streak"] = ignored_streak
    updates["momentum"] = momentum
    updates["scene_mode"] = scene_mode
    updates["last_assessment"] = {
        "turn": turn,
        "scene_mode": scene_mode,
        "thread_engaged": engaged,
        "player_interest": interest if interest in ENGAGEMENT_KINDS else "",
        "momentum": momentum,
    }

    log = list(data.get("log", []))
    log_entry = {"turn": turn, "engaged": engaged, "note": resolution_note}
    if interest in ENGAGEMENT_KINDS:
        log_entry["interest"] = interest
    log.append(log_entry)
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

    next_hook = str(parsed.get("next_hook") or "").strip()
    next_challenge = str(parsed.get("next_challenge") or "").strip()
    if next_hook or next_challenge:
        updates["thread"] = _full_thread(**{
            **{k: thread.get(k, v) for k, v in _empty_thread().items()},
            "hook": next_hook or thread.get("hook", ""),
            "challenge": next_challenge or thread.get("challenge", ""),
        })

    if _planning_iterations(config) >= 3:
        refined_notes = await _iterate_gm_notes(state, sdk, {**data, **updates}, passes=1)
        if refined_notes:
            updates["gm_notes"] = refined_notes

    if nudge_text:
        stall_threshold = max(1, int(config.get("stall_threshold", 3) or 3))
        # Fires once per streak (exact hit); the streak keeps climbing toward
        # abandonment, so a nudge must not reset it.
        stalled = ignored_streak == stall_threshold
        opportune = (
            opportunity
            and config.get("opportunity_nudges", False)
            and turn - int(data.get("last_nudge_turn", 0) or 0) >= OPPORTUNITY_COOLDOWN_TURNS
        )
        if stalled or opportune:
            print(f"[Plot Director] Turn {turn}: nudge armed ({'stall' if stalled else 'opportunity'}).")
            updates["pending_nudge"] = nudge_text
            updates["last_nudge_turn"] = turn

    return {"module_data": {MODULE_ID: updates}}


async def _execute_bg_job(job: dict) -> None:
    """Run the deferred librarian pass in the background after the turn has
    already completed and unlocked the UI, then persist and broadcast the
    updated module_data."""
    global _staged_bg_updates
    try:
        prev_task = job.get("prev_task")
        if prev_task is not None and not prev_task.done():
            try:
                await prev_task
            except (asyncio.CancelledError, Exception):
                pass
        if job.get("epoch") != _bg_epoch:
            return
        if _staged_bg_updates is not None:
            staged = _staged_bg_updates
            if (
                _job_matches_state(staged, job["state"])
                and int(staged.get("turn", 0) or 0) < int(job.get("turn", 0) or 0)
                and isinstance(staged.get("updates"), dict)
            ):
                _staged_bg_updates = None
                job["state"].setdefault("module_data", {}).setdefault(MODULE_ID, {}).update(staged["updates"])
                job.setdefault("base_updates", {}).update(staged["updates"])

        result = await _run_librarian_llm_pass(job["state"], job["sdk"], job.get("base_updates"))
        own_updates = (result or {}).get("module_data", {}).get(MODULE_ID)
        if not isinstance(own_updates, dict) or not own_updates:
            return
        if job.get("epoch") != _bg_epoch:
            return

        sm = _services.get("session_manager")
        engine = _services.get("engine")
        chat_hub = _services.get("chat_hub")
        if sm is None or engine is None:
            return

        live_state = sm.state
        if not _job_matches_state(job, live_state):
            # The turn this job analyzed was deleted, undone, regenerated, or branched away from.
            return
        live_plot = (live_state.get("module_data") or {}).get(MODULE_ID) or {}
        if live_plot.get("suspended"):
            return

        job_turn = int(job.get("turn", 0) or 0)
        live_turn = int(live_state.get("turn", 0) or 0)
        fallback_base = (job["state"].get("module_data") or {}).get(MODULE_ID)

        if chat_hub is not None and chat_hub.turn_running():
            # Another turn started while this background pass was in flight:
            # patch this turn's snapshot/swipe archives on disk right away so
            # rolling back or branching to `job_turn` has the completed plot
            # state, and stage the updates for the running turn's librarian.
            _patch_saved_turn_archives(
                sm, engine, sm.active_save_id, job_turn, own_updates,
                fallback_base=fallback_base, repack=False,
            )
            if live_turn == job_turn:
                merged = engine._deep_merge(live_plot, own_updates)
                live_state.setdefault("module_data", {})[MODULE_ID] = merged
            _staged_bg_updates = {
                "save_id": job.get("save_id"),
                "turn": job_turn,
                "hist_len": job.get("hist_len", 0),
                "scene": job.get("scene", ""),
                "epoch": job.get("epoch"),
                "updates": deepcopy(own_updates),
            }
            return

        if live_turn == job_turn:
            merged = engine._deep_merge(live_plot, own_updates)
            live_state.setdefault("module_data", {})[MODULE_ID] = merged
            if getattr(sm, "save_manager", None) is not None:
                sm.save_manager.save_turn(sm.active_save_id, live_state, job_turn)
                _patch_saved_turn_archives(
                    sm, engine, sm.active_save_id, job_turn, own_updates,
                    fallback_base=merged, patch_snapshot=False, patch_swipe=True, repack=True,
                )
            if chat_hub is not None:
                await chat_hub.send({"type": "state_update", "state": dict(live_state)})
        elif live_turn > job_turn:
            _patch_saved_turn_archives(
                sm, engine, sm.active_save_id, job_turn, own_updates,
                fallback_base=fallback_base, repack=True,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[Plot Director] Background pass failed: {exc}")


async def on_librarian(state: dict, sdk) -> dict | None:
    global _pending_bg_job, _staged_bg_updates
    config = _config(state)
    if not config.get("plot_enabled", True):
        return None
    if not state.get("history"):
        return None

    data = deepcopy(_own_data(state) or _default_data())
    if data.get("status") == "failed":
        return None
    # Suspended: freeze everything -- no assessments, no expiry, no generation.
    # The pending nudge was cleared when the player suspended.
    if data.get("suspended"):
        return None

    turn = state.get("turn", 0)
    updates: dict = {}

    # Pick up any updates completed by a previous turn's background task while
    # this turn was already streaming (discarding anything from a rolled-back turn).
    if _staged_bg_updates is not None:
        staged = _staged_bg_updates
        _staged_bg_updates = None
        staged_turn = int(staged.get("turn", 0) or 0)
        if (
            _job_matches_state(staged, state)
            and staged_turn < turn
            and isinstance(staged.get("updates"), dict)
        ):
            updates.update(staged["updates"])
            data.update(staged["updates"])

    # A nudge armed on an earlier turn was rendered into this turn's prompt;
    # consume it so it fires exactly once. _deep_merge never deletes keys, so
    # the empty string must be written explicitly.
    if data.get("pending_nudge") and int(data.get("last_nudge_turn", 0) or 0) < turn:
        updates["pending_nudge"] = ""

    # In a live WebSocket turn, defer all LLM calls to the post-turn background
    # task spawned at on_turn_stopped so the turn finishes with zero added delay.
    if _is_live_turn_armed():
        snap = deepcopy(state)
        if updates:
            snap.setdefault("module_data", {}).setdefault(MODULE_ID, {}).update(updates)
        history = state.get("history") or []
        _pending_bg_job = {
            "save_id": state.get("active_save_id"),
            "turn": turn,
            "hist_len": len(history),
            "scene": str(history[-1]) if history else "",
            "epoch": _bg_epoch,
            "state": snap,
            "sdk": sdk,
            "base_updates": {},
        }
        return {"module_data": {MODULE_ID: updates}} if updates else None

    return await _run_librarian_llm_pass(state, sdk, base_updates=updates)


async def on_turn_stopped(state: dict, sdk, reason: str) -> None:
    """Spawn the queued background Plot Director pass once the turn has been
    saved (reason == 'completed'), or discard it if the turn was cancelled."""
    global _pending_bg_job, _current_bg_task, _current_bg_job_meta
    job = _pending_bg_job
    _pending_bg_job = None
    if reason != "completed" or job is None:
        return
    prev_task = _current_bg_task
    if prev_task is not None and not prev_task.done():
        if (
            isinstance(_current_bg_job_meta, dict)
            and _current_bg_job_meta.get("save_id") == job.get("save_id")
            and int(_current_bg_job_meta.get("turn", 0) or 0) < int(job.get("turn", 0) or 0)
            and _current_bg_job_meta.get("epoch") == job.get("epoch")
        ):
            job["prev_task"] = prev_task
        else:
            prev_task.cancel()
    _current_bg_job_meta = {
        "save_id": job.get("save_id"),
        "turn": job.get("turn"),
        "hist_len": job.get("hist_len"),
        "scene": job.get("scene"),
        "epoch": job.get("epoch"),
    }
    task = asyncio.get_running_loop().create_task(_execute_bg_job(job))
    _current_bg_task = task
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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
        parts.append("likes: " + ", ".join(
            f"{e['text']} [{e['role']}]" if e.get("role") else e["text"]
            for e in likes[:2]
        ))
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
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn", "error": True}

    _cancel_bg_task()
    turn = state.get("turn", 0)
    updates: dict = {}
    thread = data.get("thread") or {}
    if thread.get("status") == "active":
        _finalize_thread(updates, data, thread, "rerolled", turn,
                         consequence="Set aside by the player.", cooldown=0)

    generated = await _generate_checked_thread(
        state, sdk, {**data, **updates, "gen_attempts": 0})
    if "thread" not in generated:
        return {"message": "[Plot] Couldn't weave a new thread -- nothing changed. Try again.", "signal": "end_turn", "error": True}

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
    _cancel_bg_task()
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
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn", "error": True}
    if data.get("suspended"):
        return {"message": "[Plot] Plot direction is already suspended.", "signal": "end_turn", "error": True}
    _cancel_bg_task()
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
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn", "error": True}
    if not data.get("suspended"):
        return {"message": "[Plot] Plot direction is not suspended.", "signal": "end_turn", "error": True}

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
    "[Plot] Usage: /plot profile regen | "
    "/plot profile tone <text> | "
    "/plot profile themes <add|remove> <text> | "
    "/plot profile <likes|dislikes> <add|remove> [low|medium|high] [actor|target|mutual|world] <text> | "
    "/plot profile avoids remove <text> | "
    "/plot profile <pacing|agency|register|notes> <text>"
)


async def _command_profile_regen(state: dict, sdk, data: dict) -> dict:
    """Re-analyze the player profile from the story so far using the current
    role-explicit rules, without wiping the active plot thread, GM notes,
    direction, or thread history. Preserves player-set dislikes."""
    if not data or data.get("schema") != SCHEMA_VERSION:
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn", "error": True}
    if not state.get("history"):
        return {"message": "[Plot] Not enough story history to analyze yet.", "signal": "end_turn", "error": True}

    _cancel_bg_task()
    old_profile = _clean_profile({}, data.get("profile") or _default_profile())
    kept_dislikes = old_profile["dislikes"]
    preserved = {"dislikes": kept_dislikes} if kept_dislikes else None
    bootstrapped = await _bootstrap_profile(state, sdk, preserved=preserved)
    if bootstrapped is None:
        return {"message": "[Plot] Couldn't re-analyze the profile -- nothing changed. Try again.", "signal": "end_turn", "error": True}

    if kept_dislikes:
        bootstrapped = _merge_player_prefs(bootstrapped, [], kept_dislikes)
    return {
        "message": "[Plot] Player profile re-analyzed from the story so far.",
        "signal": "end_turn",
        "module_data": {MODULE_ID: {"profile": bootstrapped}},
    }


def _command_profile_edit(data: dict, args: list[str]) -> dict:
    """Player edits to the story profile. List entries are matched with
    collapsed whitespace, case-insensitively. Re-adding an existing
    like/dislike with a different weight updates the weight in place; adds
    past the cap evict the lowest-weight (themes: oldest) entry. Failed
    edits write nothing back."""
    if not data or data.get("schema") != SCHEMA_VERSION:
        return {"message": "[Plot] The story is still settling in -- try again next turn.", "signal": "end_turn", "error": True}
    if not args:
        return {"message": PROFILE_USAGE, "signal": "end_turn", "error": True}

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
            return {"message": f'[Plot] "{text}" is not in avoids.', "signal": "end_turn", "error": True}
        profile["avoids"] = kept
        message = f'[Plot] Removed "{text}" from avoids.'
    elif field == "themes" and len(args) >= 3 and args[1].lower() in ("add", "remove"):
        op = args[1].lower()
        text = " ".join(args[2:]).strip()[:PROFILE_ENTRY_MAX_CHARS]
        if not text:
            return {"message": PROFILE_USAGE, "signal": "end_turn", "error": True}
        entries = list(profile["themes"])
        if op == "add":
            if any(_norm_entry(e) == _norm_entry(text) for e in entries):
                return {"message": f'[Plot] "{text}" is already in themes.', "signal": "end_turn", "error": True}
            entries.append(text)
            entries = entries[-PROFILE_LIST_CAP:]
            message = f'[Plot] Added "{text}" to themes.'
        else:
            kept = [e for e in entries if _norm_entry(e) != _norm_entry(text)]
            if len(kept) == len(entries):
                return {"message": f'[Plot] "{text}" is not in themes.', "signal": "end_turn", "error": True}
            entries = kept
            message = f'[Plot] Removed "{text}" from themes.'
        profile["themes"] = entries
    elif field in ("likes", "dislikes") and len(args) >= 3 and args[1].lower() in ("add", "remove"):
        op = args[1].lower()
        rest = args[2:]
        weight = "medium"
        role = ""
        if op == "add":
            while len(rest) >= 2 and rest[0].lower() in (*PREF_WEIGHTS, *PREF_ROLES):
                token = rest[0].lower()
                if token in PREF_WEIGHTS:
                    weight = token
                elif token in PREF_ROLES:
                    role = token
                rest = rest[1:]
        text = " ".join(rest).strip()[:PROFILE_ENTRY_MAX_CHARS]
        if not text:
            return {"message": PROFILE_USAGE, "signal": "end_turn", "error": True}
        entries = list(profile[field])
        if op == "add":
            existing = next((e for e in entries if _norm_entry(e["text"]) == _norm_entry(text)), None)
            if existing is not None:
                same_weight = existing["weight"] == weight
                same_role = (existing.get("role", "") == role) if role else True
                if same_weight and same_role:
                    return {"message": f'[Plot] "{existing["text"]}" is already in {field} ({weight}).', "signal": "end_turn", "error": True}
                existing["weight"] = weight
                if role:
                    existing["role"] = role
                message = f'[Plot] "{existing["text"]}" set to {weight}' + (f" [{role}]." if role else ".")
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
                if role:
                    entry["role"] = role
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
                return {"message": f'[Plot] "{text}" is not in {field}.', "signal": "end_turn", "error": True}
            entries = kept
            message = f'[Plot] Removed "{text}" from {field}.'
        profile[field] = entries
    else:
        return {"message": PROFILE_USAGE, "signal": "end_turn", "error": True}

    return {
        "message": message,
        "signal": "end_turn",
        "module_data": {MODULE_ID: {"profile": profile}},
    }


async def on_command_plot(args: list[str], state: dict, sdk) -> dict:
    config = _config(state)
    data = _own_data(state)

    if not config.get("plot_enabled", True):
        return {"message": "[Plot] Plot direction is inactive.", "signal": "end_turn", "error": True}

    if args and args[0].lower() in ("suspend", "pause"):
        return _command_suspend(state, data)

    if args and args[0].lower() in ("resume", "unpause"):
        return _command_resume(state, data)

    if args and args[0].lower() in ("regen", "reroll", "new"):
        if data.get("suspended"):
            return {"message": "[Plot] Plot direction is suspended -- /plot resume first.", "signal": "end_turn", "error": True}
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
        if len(args) == 2 and args[1].lower() in ("regen", "reanalyze", "rebuild"):
            return await _command_profile_regen(state, sdk, data)
        return _command_profile_edit(data, args[1:])

    if args and args[0].lower() == "notes":
        gm_notes = str((data or {}).get("gm_notes") or "").strip()
        if not gm_notes:
            return {"message": "[Plot] No GM notes recorded yet.", "signal": "end_turn"}
        return {
            "message": f"[Plot] GM notes (spoiler):\n{gm_notes}",
            "signal": "end_turn",
        }

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
        endgame = str(direction.get("endgame", "")).strip()
        if endgame:
            lines.append(f"North Star: {endgame}")
        heading = str(direction.get("heading", "")).strip()
        if heading:
            lines.append(f"Heading: {heading}")
        horizon = [str(h).strip() for h in direction.get("horizon", []) if str(h).strip()]
        if horizon:
            lines.append("Horizon: " + " -> ".join(horizon))
        seeds = _clean_seeds(direction.get("planted_seeds"), [])
        if seeds:
            lines.append("Planted seeds: " + "; ".join(
                f"{s['seed']} -> {s['payoff']}" if s.get("payoff") else s["seed"]
                for s in seeds
            ))
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
        if _in_quiet_period(state, data, state.get("turn", 0)):
            return {
                "message": f"[Plot] Between threads -- letting the story breathe (a new thread arrives around turn {next_thread_turn}).",
                "signal": "end_turn",
            }
        return {"message": "[Plot] Between threads -- a new one is being woven.", "signal": "end_turn"}

    mode_tag = f" [{thread['mode']}]" if thread.get("mode") else ""
    lines = [f"[Plot] Active thread: {thread.get('title', '')}{mode_tag} (since turn {thread.get('created_turn', 0)})"]
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
