"""The agent harness (C2 of the worldgen plan): the build loop itself.

A server-side agent builds a world the way a coding agent works a task:
it keeps a todo list, calls tools, verifies its own output against the
world rules, fixes what verification finds, and repeats until the done-gate
passes. One agent turn = one structured JSON completion on the smartest
slot (D2 — ``LLMService`` has no native tool-call plumbing on any provider
path): system prompt (brief + rules + toolbox catalog) plus todo state and
recent observations in; ``{"action": {...}}`` or a done claim out. Invalid
JSON, unknown tools and rejected arguments come back to the agent as next
-turn observations — the error-feedback loop is the mechanism (P7).

Refinement over the plan's sketch (decided with Filip at C2 start): the
todo list is part of each turn's completion rather than separate todo
tools — one JSON object per turn keeps the protocol single-shaped and the
todo can never drift from the action stream.

Budgets are harness-enforced structural units (D5): max turns, max tool
calls, max fix rounds per finding (a blocking finding still standing after
that many evaluation rounds is auto-accepted and recorded). Every mutating
tool call is preceded by a world checkpoint keyed by its action index
(v2c) — the revert tool's restore targets; the store is per-build, cleared
at launch and on every terminal state. The user does not approve steps
(P6, as rescoped by C7): steering happened at the go-gate, observability
is the persisted todo/action-log artifact (``agent_build.json`` in the
world directory) streamed live over SSE, and cancel is always available —
but the user keeps a *voice*: messages posted to the build queue on the
handle and drain at the next turn boundary as plain observations (C7a/U3,
no engineered authority), the agent may answer through the completion's
optional ``say`` (U6), and user words become contract through the
brief-edit tools (U2). Clients reattach to a running build through
the in-process build registry; the artifact serves finished builds after
a restart (backend-restart *resume* is a recorded v2 item).

Since C7b the session is two-phased: world creation opens in a **chat
phase** — a message-paced design conversation in which a second agent (U1:
one session, two agents — same state and transport, different persona)
maintains the brief through a restricted tool catalog (the brief tools +
read_conversation, nothing that builds) and offers Go via the completion's
``ready`` flag. The user's Go flips the same session, artifact and event
stream into the build phase above. The chat phase is resumable by
construction: the transcript lives in the artifact, so a dead session
(backend restart, cancel) is recreated from it on the next message, Go or
hand edit — only the *build* phase's restart resume remains v2.
"""

import asyncio
import json
import logging
from datetime import datetime

from wbworldgen.worldgen.agent.evaluator import evaluate_world
from wbworldgen.worldgen.agent.registry import ToolContext, ToolError, invoke_tool
from wbworldgen.worldgen.generation.llm import json_retry_completion

logger = logging.getLogger(__name__)

#: Structural budgets (D5) — defaults, each overridable via settings.
DEFAULT_MAX_TURNS = 40          #: world.agent_max_turns
DEFAULT_MAX_TOOL_CALLS = 60     #: world.agent_max_tool_calls
DEFAULT_FIX_ROUNDS = 3          #: world.agent_fix_rounds

#: Observations kept verbatim in the turn prompt (P9: a structural budget,
#: never a character cap — older results drop off whole; reads are cheap to
#: repeat and the todo list is the agent's durable memory).
RECENT_LIMIT = 8

#: Consecutive agent-turn LLM failures before the build aborts. The chat
#: phase uses the same threshold per exchange, but survives it: a design
#: conversation should not die because one reply failed — the error is
#: surfaced and the next message retries.
MAX_LLM_FAILURES = 3

#: Chat-phase completions allowed per user message (U6: each message opens
#: a small turn-budgeted mini-loop — reply plus a few brief edits; loud on
#: exhaustion, P9). world.agent_chat_turns overrides.
DEFAULT_CHAT_TURNS = 6

#: The chat phase's restricted catalog (C7b): the brief tools —
#: conversation output becoming contract — and the transcript reader.
#: Everything that builds stays locked until Go.
CHAT_TOOL_IDS = ("update_prompt", "update_rules", "update_notes",
                 "read_conversation")

ARTIFACT_FILENAME = "agent_build.json"


class AgentBuild:
    """One session's live state: the registry entry observers attach to.

    Despite the name (C2-era) a handle now spans the whole C7b session:
    ``phase`` is ``"chat"`` while the design conversation runs and
    ``"build"`` from Go (direct launches start there). One artifact, one
    event stream, one message channel across both phases (U1)."""

    def __init__(self, world_id: str, seed_prompt: str, builder,
                 brief: dict = None, phase: str = "build"):
        self.world_id = world_id
        self.seed_prompt = seed_prompt
        self.builder = builder
        self.phase = phase
        #: Chat-phase completions so far (the build's ``turns`` counts only
        #: build turns — chat never burns the build's D5 budgets).
        self.chat_turns = 0
        #: The chat agent's standing go offer (U6): highlights the Go
        #: button, never gates it.
        self.ready = False
        #: Set by request_go(); the session loop flips the phase at the
        #: next boundary.
        self.go_requested = False
        #: Wakes the chat phase's idle wait (message posted, Go, cancel).
        self._wake = asyncio.Event()
        #: The ideation brief (D4/C5): {"prompt", "rules", "notes"} —
        #: surfaced in the snapshot/artifact so observers can show what was
        #: agreed. Note amendments (N5) update the world's copy on disk;
        #: this attribute mirrors it for the snapshot.
        self.brief = brief or {"prompt": seed_prompt, "rules": [], "notes": []}
        self.status = "running"   # running | done | cancelled | failed | budget_exhausted
        self.todo: list = []
        self.log: list = []       # persisted events, each carrying its index "i"
        self.recent: list = []    # last few action/observation pairs, prompt-side
        self.turns = 0
        self.tool_calls = 0
        self.finding_rounds: dict = {}   # finding key -> evaluation sightings
        #: note id -> latest verifier verdict (C5): the context a
        #: discuss_finding exchange answers to.
        self.last_note_verdicts: dict = {}
        #: note id -> {"rounds", "transcript"} — the per-finding dialogue
        #: state discuss_finding budgets against (N5).
        self.note_dialogues: dict = {}
        #: The mid-build message channel (C7a): user messages queue here
        #: and drain at the next turn boundary into ``recent`` and the
        #: persisted log — plain observations, no engineered authority (U3).
        self.pending_messages: list = []
        self.message_seq = 1
        self.cancel_requested = False
        self.result: dict = None
        self.error: str = None
        self.started_at = datetime.utcnow().isoformat() + "Z"
        self.finished_at: str = None
        self.subscribers: list = []      # asyncio.Queue per attached observer
        self.task: asyncio.Task = None

    def post_message(self, text: str) -> dict:
        """Queue one user message for the next turn boundary (C7a). The
        drain emits the persisted ``user_message`` event and hands the text
        to the agent as a plain observation; until then the message rides
        the snapshot's ``queued_messages`` so a reattaching observer still
        shows it."""
        mid = f"m{self.message_seq}"
        self.message_seq += 1
        self.pending_messages.append({"id": mid, "text": text})
        self._wake.set()
        return {"id": mid, "position": len(self.pending_messages)}

    def request_go(self):
        """Ask a chat-phase session to flip into the build (C7b). Honored
        at the session loop's next boundary — mid-exchange, after the
        current reply completes; messages still queued at the flip ride
        into the build and drain at its first turn."""
        self.go_requested = True
        self._wake.set()

    def poke(self):
        """Wake the chat phase's idle wait (cancel uses this)."""
        self._wake.set()

    def snapshot(self) -> dict:
        return {
            "world_id": self.world_id, "status": self.status,
            "phase": self.phase,
            "seed_prompt": self.seed_prompt, "brief": self.brief,
            "turns": self.turns, "chat_turns": self.chat_turns,
            "ready": self.ready,
            "tool_calls": self.tool_calls, "todo": list(self.todo),
            "started_at": self.started_at, "finished_at": self.finished_at,
            "error": self.error, "result": self.result,
            "log_len": len(self.log),
            "last_event": self.log[-1] if self.log else None,
            "queued_messages": [dict(m) for m in self.pending_messages],
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.subscribers:
            self.subscribers.remove(q)


#: world_id -> AgentBuild. In-process registry (the reattach surface while
#: the backend lives); finished builds stay listed so observers can replay.
_BUILDS: dict = {}


def get_build(world_id: str):
    return _BUILDS.get(world_id)


def _artifact_path(builder, world_id: str):
    return builder.services.enrichment_store.world_dir(world_id) / ARTIFACT_FILENAME


def has_build_artifact(builder, world_id: str) -> bool:
    """True when a persisted agent-build artifact exists for the world. The
    world list uses this to route an in-progress world's recovery: reattach
    to the recorded build's observer, or offer a fresh adopt run when no
    build ever touched it. Best-effort like the artifact reads — a builder
    without a store (test fakes) simply has no artifacts."""
    try:
        return _artifact_path(builder, world_id).is_file()
    except Exception:
        return False


def load_build_artifact(builder, world_id: str) -> dict | None:
    """The persisted build artifact (todo + action log + outcome), or None.
    Serves observers when no live build is registered (finished builds
    after a backend restart)."""
    path = _artifact_path(builder, world_id)
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def exchanges_from_log(log: list) -> list:
    """The conversation entries of a session log, oldest first: every
    ``user_message`` verbatim and every non-empty ``say`` reply, from both
    phases. Shared by the read_conversation tool (U4) and the chat phase's
    own prompt assembly — the chat agent converses over the full
    transcript, never truncated (P9)."""
    exchanges = []
    for evt in log or []:
        if evt.get("type") == "user_message":
            exchanges.append({"who": "user", "turn": evt.get("turn"),
                              "text": str(evt.get("text") or ""),
                              **({"unread": True} if evt.get("unread") else {})})
        elif evt.get("type") == "turn" and str(evt.get("say") or "").strip():
            exchanges.append({"who": "agent", "turn": evt.get("turn"),
                              "text": str(evt["say"]).strip()})
    return exchanges


def _persist_artifact(handle: AgentBuild):
    """Write the observability artifact (P6). Best-effort — an artifact
    write failure never fails the build."""
    try:
        path = _artifact_path(handle.builder, handle.world_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = handle.snapshot()
        payload["log"] = handle.log
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception:
        logger.warning("agent build artifact write failed for %s",
                       handle.world_id, exc_info=True)


async def _emit(handle: AgentBuild, evt: dict, persist: bool = True):
    """Record + broadcast one build event. Persisted events land in the
    log/artifact with a monotone index ``i`` (the SSE replay cursor);
    transient events (enrichment progress) only stream."""
    if persist:
        evt = dict(evt)
        evt["i"] = len(handle.log)
        handle.log.append(evt)
        _persist_artifact(handle)
    for q in list(handle.subscribers):
        q.put_nowait(evt)


async def _drain_messages(handle: AgentBuild, unread: bool = False):
    """Move queued user messages into the persisted log and — at a turn
    boundary — into ``recent`` as plain ``{"user_message": ...}``
    observations the coming turn reads (C7a/U3: the text lands verbatim,
    with no engineered authority; the model's instruction-following is the
    mechanism). ``unread=True`` is the terminal path: the build ended with
    messages still queued, so they are recorded — never silently dropped —
    but flagged as never having reached the agent."""
    while handle.pending_messages:
        msg = handle.pending_messages.pop(0)
        evt = {"type": "user_message", "id": msg["id"], "text": msg["text"]}
        if unread:
            evt["unread"] = True
        else:
            evt["turn"] = handle.turns
            handle.recent.append({"turn": handle.turns,
                                  "user_message": msg["text"]})
        await _emit(handle, evt)


def _clear_checkpoints(builder, world_id: str):
    """Drop the world's checkpoint store (v2c). Best-effort like the
    artifact writes — and a no-op for test rigs whose fake store has no
    checkpoint surface."""
    try:
        builder.services.enrichment_store.clear_checkpoints(world_id)
    except Exception:
        pass


def _checkpoint_before(handle: AgentBuild, tool_id: str, action_i: int):
    """World snapshot before a mutating tool call (v2c) — the revert
    tool's restore target, keyed by the action's log index. Returns
    ``(tag, error)``: ``(None, None)`` for read-only/unknown tools and for
    stores without a checkpoint surface (test rigs); a real store that
    fails to snapshot is an ERROR the loop surfaces instead of running the
    action — running a mutation without its safety net would silently
    break revert's contract (P7)."""
    from wbworldgen.worldgen.agent.registry import get_tool
    try:
        if not get_tool(tool_id).mutates:
            return None, None
    except ToolError:
        return None, None  # unknown tool: invoke_tool rejects it next
    store = handle.builder.services.enrichment_store
    snapshot = getattr(store, "snapshot_world", None)
    if snapshot is None:
        return None, None
    try:
        return snapshot(handle.world_id, str(action_i)), None
    except Exception as e:
        return None, f"world checkpoint before '{tool_id}' failed: {e}"


# --- the agent turn (the mock seam) ----------------------------------------

async def agent_turn(services, messages: list) -> dict:
    """One agent-loop completion on the smartest slot. Module-level so
    mock-driven harness tests monkeypatch it with canned action sequences —
    the same patch-point contract as the pass modules."""
    return await json_retry_completion(
        services.llm,
        messages=messages,
        model=services.llm.storyteller_model,
        temperature=0.4,
        inspector_ctx={"call_type": "world_build", "step": "agent:turn"},
        step_label="agent:turn",
        retry_attempts=services.json_retry_attempts,
    )


async def chat_turn(services, messages: list) -> dict:
    """One design-conversation completion on the smartest slot — the chat
    phase's counterpart of ``agent_turn`` (U1: one session, two agents),
    and the second canned-sequence patch seam for tests."""
    return await json_retry_completion(
        services.llm,
        messages=messages,
        model=services.llm.storyteller_model,
        temperature=0.6,
        inspector_ctx={"call_type": "world_ideation", "step": "agent:chat"},
        step_label="agent:chat",
        retry_attempts=services.json_retry_attempts,
    )


# --- prompt assembly --------------------------------------------------------

def _system_prompt(handle: AgentBuild, world_state: dict, budgets: dict) -> str:
    from wbworldgen.worldgen import notes as _notes
    from wbworldgen.worldgen.catalog import render_catalog_markdown

    brief = world_state.get("brief") if isinstance(world_state.get("brief"), dict) else {}
    agreed = [str(r).strip() for r in (brief.get("rules") or []) if str(r).strip()]
    agreed_block = ""
    if agreed:
        agreed_block = (
            "\n### Co-authored world rules\n"
            "Agreed with the user during ideation — fixed design decisions, "
            "not suggestions. The world MUST embody every one; they are the "
            "world_rules step's fixed input (running it expands them), and "
            "every evaluation judges the world against them.\n"
            + "\n".join(f"- {r}" for r in agreed) + "\n")

    try:
        compiled_now = handle.builder.services.compiled.load(handle.world_id)
    except Exception:  # a just-created draft may not compile yet
        compiled_now = {}
    notes_render = _notes.agent_notes_block(world_state, compiled_now)
    notes_block = ""
    if notes_render:
        notes_block = (
            "\n### Agreed design notes (the user's contract)\n"
            "Established facts from ideation. World-wide notes bind the "
            "whole world; subject notes bind the map shown (their full text "
            "is injected into that map's generation calls automatically — "
            "you never need to copy them into steering notes). Every note "
            "is verified before the build can finish: note findings are "
            "NEVER auto-accepted — fix the world, or explicitly accept the "
            "finding with a reason the user will see.\n"
            + notes_render + "\n")

    rules = ((world_state.get("steps", {}).get("world_rules") or {}).get("data")) or {}
    if rules:
        rules_block = json.dumps(rules, indent=2, ensure_ascii=False)
    elif agreed:
        rules_block = (
            "None authored yet. Author them FIRST (run_step 'world_rules') — "
            "the co-authored rules above are its fixed input and will lead "
            "the result; every evaluation judges the world against these "
            "rules.")
    else:
        rules_block = (
            "None authored yet. Author them FIRST (run_step 'world_rules', "
            "with a steering note distilled from the brief) — every "
            "evaluation judges the world against these rules.")
    scenario = str(world_state.get("scenario") or "").strip()
    scenario_block = (
        f"\n## Source material the world is grounded in\n{scenario}\n"
        if scenario else "")
    ordered = [p["id"] for p in handle.builder.get_pipeline()]
    # The brief is re-read from disk every turn (D4) — an update_prompt
    # carried in from a user message takes effect on the very next turn.
    prompt_text = str(brief.get("prompt") or "").strip() or handle.seed_prompt

    return f"""You are the build agent for a game world. You drive the build: the \
user approved the brief and handed you the work — you decide, act, verify \
your own output, and fix what verification finds. The user can watch and may \
speak while you build; their messages are observations to fold in, never a \
gate to wait for. Build a complete, coherent, playable world.

## The brief
{prompt_text}
{agreed_block}{notes_block}{scenario_block}
## World rules (the evaluation rubric)
{rules_block}

## How to work
- Keep a todo list that always reflects your actual plan; update item \
statuses every turn.
- The classic pipeline order is a sound default plan: {", ".join(ordered)}. \
You may deviate when the brief calls for it; each capability's \
requires/produces contract is listed in the catalog below and is enforced.
- Steps produce the world's structure; enrichment passes (label, describe) \
produce its content; review, read_lint and evaluate verify it. Every major \
location must end up named and described (the lint's unnamed/undescribed \
major findings show the gap).
- Child maps (planet surfaces, city streets, interiors) are created ONLY \
by expand_node on an expandable node — re-running map_generation cannot \
add them and replaces the existing maps instead. The hierarchy's \
pregenerate plans are your upfront work list for them.
- Fix content findings with steered regeneration: run_pass with rework, \
node_ids and a guidance note is your primary fix instrument. Regenerating \
a step with a steering note is the recourse for structural problems.
- Every mutating action is checkpointed first; its observation carries the \
checkpoint id. When a mutation made the world WORSE — a regeneration \
replaced content you needed, an edit or surgery broke structure — revert \
to the state before it (the revert tool) instead of rebuilding lost \
content from memory, then take a better path. Revert rewinds world \
content only; your todo, observations and the brief stay as they are.
- Note findings (note:*) are the user's contract and are never \
auto-accepted. Fix the world — or, if you believe the verifier is wrong \
or the note genuinely conflicts with the design, contest the finding with \
discuss_finding: the verifier withdraws on real evidence, or agrees a \
compromise the user reviews after the build. Explicit acceptance in the \
done claim is the last resort and is shown to the user.
- The user may speak mid-build: their words arrive verbatim as \
{{"user_message": ...}} observations at the turn boundary. Respond in that \
turn — answer briefly via "say", and fold any standing instruction into \
your todo so it survives the conversation scrolling off. When their words \
change what the world should BE, record the change in the brief \
(update_prompt / update_rules / update_notes) so generation and \
verification follow it; the brief-edit tools exist ONLY to carry direct \
user input — never rewrite the brief unprompted. Exchanges that scrolled \
out of recent are readable with read_conversation.
- Verify before claiming done: run evaluate, fix the problems it reports, \
re-evaluate. The done claim triggers a final evaluation; blocking findings \
must be fixed or explicitly accepted by key.
- Budgets: turn {handle.turns}/{budgets["max_turns"]}, tool calls \
{handle.tool_calls}/{budgets["max_tool_calls"]}. A blocking finding still \
standing after {budgets["fix_rounds"]} evaluation rounds is auto-accepted. \
Work efficiently — prefer scoped, batched actions over node-by-node ones.

## Response protocol
Reply with exactly ONE JSON object and nothing else. Either act:
{{"thought": "...", "todo": [{{"text": "...", "status": "pending|in_progress|done"}}, ...], "action": {{"tool": "...", "args": {{...}}}}}}
or, when the world is genuinely finished and verified, declare done:
{{"thought": "...", "todo": [...], "done": {{"summary": "...", "accept_findings": ["<finding key>", ...], "note": "why the accepted findings are acceptable"}}}}
- "todo" replaces your previous list; omit the field to keep it unchanged.
- One action per turn; its result arrives as an observation next turn.
- "accept_findings"/"note" only when accepting remaining findings.
- Either shape may add "say": "..." — a short reply the user sees as chat. \
Use it when answering a user_message; it accompanies your action, never \
replaces it.

## Tools and capabilities
{render_catalog_markdown()}"""


def _user_message(handle: AgentBuild, budgets: dict) -> str:
    payload = {
        "world_id": handle.world_id,
        "turn": handle.turns,
        "budgets": {"turns": f"{handle.turns}/{budgets['max_turns']}",
                    "tool_calls": f"{handle.tool_calls}/{budgets['max_tool_calls']}"},
        "todo": handle.todo,
        "recent": handle.recent[-RECENT_LIMIT:],
    }
    return ("Current state:\n" + json.dumps(payload, indent=2, ensure_ascii=False)
            + "\n\nReply with exactly one protocol JSON object.")


# --- the chat phase (C7b) ---------------------------------------------------

def _chat_system_prompt(world_state: dict) -> str:
    """The design partner's standing instructions (U1: separate persona,
    same session): the reshaped C4 ideation prompt — drafts maintained
    through tools instead of riding the completion (U6), conversation and
    drafts supplied per turn from the session's own state."""
    from wbworldgen.worldgen.catalog import render_tools_markdown
    from wbworldgen.worldgen.steps.world_rules import RULES_DOCTRINE

    scenario = str(world_state.get("scenario") or "").strip()
    scenario_block = (
        "\n## Source material the world must fit\n"
        "The player chose this scenario — treat everything in it as already "
        "decided, and design the wider world around it rather than re-asking "
        f"what it settles:\n{scenario}\n" if scenario else "")

    return f"""You are the world-design partner in a game's world builder. The player \
and you are converging, in conversation, on what a world IS before an \
autonomous agent builds the whole thing unattended. Be a sharp, concrete \
collaborator: build on what the player gives, propose ideas they can react \
to, and ask at most a couple of pointed questions per turn — never a \
checklist interrogation. A player with a strong vision needs distillation, \
not invention; a player with a vague itch needs vivid options to pick \
between. Talk about the world only — protagonists and plot belong to the \
stories told in it later.

## The brief: three shared drafts, maintained through your tools
The brief is shown beside the chat and the player can hand-edit it between \
messages, so the drafts you receive are the current truth — never revert \
their edits, only evolve the drafts with the conversation.
- prompt (update_prompt): the world seed prompt — a short, vivid paragraph \
of creative direction (premise, setting, tone, defining features) the \
generator expands into a full world. Direction for a generator, never \
in-fiction narration.
- rules (update_rules): the handful of statements (aim for 3-7) that define \
how this world works. They are the spine of the design and double as the \
rubric the finished world is judged against, so converge them FIRST.
- notes (update_notes): the design notebook — everything the conversation \
settles that is neither seed direction nor a rule: lore, cultures, biology, \
specific places and their quirks. Leave "subject" empty for a fact about \
the world as a whole; name ONE specific thing ("the sand planet Kharos") \
when the note belongs to it — scoped notes steer only their own place \
during the build, so one place's details never bleed into the others. The \
builder is verified against every note, so record what the player settles \
AS the conversation settles it — details the short prompt cannot hold \
survive the handoff only as notes. Never invent notes the player did not \
agree to; an empty list is fine early on.

What makes a good world rule:
{RULES_DOCTRINE}

## When the idea is settled
When the prompt captures it and the rules are concrete enough to judge a \
world by, set "ready" to true and offer in your reply to start the build — \
the Go button is the player's and never waits on you. If the player asks to \
just build it, distill the best prompt, rules and notes you can from what \
you have and set "ready" true immediately. After Go an autonomous build \
agent takes over; the brief is its standing contract and the only thing it \
ambiently carries from this conversation (it can look the transcript up on \
demand), so the brief must hold everything that matters.

## Response protocol
Reply with exactly ONE JSON object and nothing else:
{{"say": "...", "action": {{"tool": "...", "args": {{...}}}}, "ready": false}}
- "say" is your message to the player. It is required on a completion with \
no action; while you are still editing drafts you may omit it and act.
- "action" is one tool call. Its result arrives as an observation and you \
reply again — chain a few to update the drafts, then finish with a \
completion that has NO action: that ends your turn and hands the \
conversation back to the player.
- "ready" is your standing go offer; set it when your judgment changes.

## Tools
{render_tools_markdown(list(CHAT_TOOL_IDS))}{scenario_block}"""


def _chat_user_payload(handle: AgentBuild, world_state: dict,
                       observations: list) -> str:
    """One chat completion's user message: the current drafts (server
    truth), the full conversation (P9: never truncated — the newest user
    entries are what is being answered), the standing ready flag, and this
    exchange's own action results so far."""
    brief = world_state.get("brief") if isinstance(world_state.get("brief"), dict) else {}
    notes = []
    for n in (brief.get("notes") or []):
        if not isinstance(n, dict):
            continue
        note = {"text": str(n.get("text") or "")}
        if n.get("id"):
            note["id"] = n["id"]
        if str(n.get("subject") or "").strip():
            note["subject"] = str(n["subject"]).strip()
        notes.append(note)
    payload = {
        "drafts": {
            "prompt": str(brief.get("prompt") or ""),
            "rules": [str(r) for r in (brief.get("rules") or [])],
            "notes": notes,
        },
        "conversation": [{"who": e["who"], "text": e["text"]}
                         for e in exchanges_from_log(handle.log)],
        "ready": handle.ready,
    }
    if observations:
        payload["observations"] = observations
    return ("Current state:\n" + json.dumps(payload, indent=2, ensure_ascii=False)
            + "\n\nThe newest user entries in the conversation are the "
              "message(s) you are answering now. Reply with exactly one "
              "protocol JSON object.")


def _validate_chat_completion(completion):
    """Protocol check of one chat completion (U6). Returns
    (say, action, ready_or_None, problems)."""
    if not isinstance(completion, dict):
        return "", None, None, ["the completion must be one JSON object"]
    problems = []
    say = str(completion.get("say") or "").strip()
    action = completion.get("action")
    if action is not None:
        if (not isinstance(action, dict) or not isinstance(action.get("tool"), str)
                or not action.get("tool")):
            problems.append('\'action\' must be {"tool": "...", "args": {...}}')
            action = None
        elif action.get("args") is not None and not isinstance(action["args"], dict):
            problems.append("'action.args' must be an object")
            action = None
    if action is None and not say and not problems:
        problems.append("an empty completion does nothing — reply with "
                        "'say', take an 'action', or both")
    ready = completion.get("ready")
    return say, action, (bool(ready) if ready is not None else None), problems


# --- protocol validation ----------------------------------------------------

def _normalize_todo(raw):
    """Validated copy of a completion's todo list, or (None, problem)."""
    if not isinstance(raw, list):
        return None, "'todo' must be a list"
    out = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append({"text": item.strip(), "status": "pending"})
            continue
        if isinstance(item, dict) and str(item.get("text", "")).strip():
            status = item.get("status", "pending")
            if status not in ("pending", "in_progress", "done"):
                return None, f"invalid todo status {status!r} (pending|in_progress|done)"
            out.append({"text": str(item["text"]).strip(), "status": status})
            continue
        return None, f"invalid todo entry: {item!r}"
    return out, None


def _validate_completion(completion):
    """Protocol check of one turn's completion. Returns
    (todo_or_None, action, done_claim, problems)."""
    if not isinstance(completion, dict):
        return None, None, None, ["the completion must be one JSON object"]
    problems = []
    todo = None
    if "todo" in completion:
        todo, problem = _normalize_todo(completion.get("todo"))
        if problem:
            problems.append(problem)
    action = completion.get("action")
    done = completion.get("done")
    if (action is None) == (done is None):
        problems.append("exactly one of 'action' or 'done' is required")
    if action is not None:
        if (not isinstance(action, dict) or not isinstance(action.get("tool"), str)
                or not action.get("tool")):
            problems.append('\'action\' must be {"tool": "...", "args": {...}}')
        elif action.get("args") is not None and not isinstance(action["args"], dict):
            problems.append("'action.args' must be an object")
    if done is not None:
        if not isinstance(done, dict) or not str(done.get("summary", "")).strip():
            problems.append('\'done\' must carry a non-empty "summary"')
        else:
            accepts = done.get("accept_findings")
            if accepts is not None and (
                    not isinstance(accepts, list)
                    or not all(isinstance(k, str) for k in accepts)):
                problems.append("'done.accept_findings' must be a list of finding keys")
            elif accepts and not str(done.get("note", "")).strip():
                problems.append("accepting findings requires a 'note' explaining why "
                                "they are acceptable")
    return todo, action, done, problems


# --- the done-gate ----------------------------------------------------------

def _track_findings(handle: AgentBuild, eval_result: dict):
    for f in eval_result.get("findings", []):
        if f.get("severity") == "problem":
            handle.finding_rounds[f["key"]] = handle.finding_rounds.get(f["key"], 0) + 1
    # Stash the verifier's verdicts: discuss_finding answers to them (N5).
    for v in (eval_result.get("notes") or {}).get("verdicts") or []:
        handle.last_note_verdicts[v["id"]] = v


def _is_note_finding(key: str) -> bool:
    """Note obligations (verifier findings and the unbound-note lint) are
    never auto-accepted (N6): every note ends honored, honored-as-amended,
    or explicitly accepted with a recorded reason."""
    return key.startswith("note:") or ":note_unbound:" in key


def _note_id_of(key: str):
    """The note id a note finding key carries (either shape), else None."""
    parts = str(key or "").split(":")
    if parts and parts[0] == "note" and len(parts) >= 2:
        return parts[1]
    if len(parts) >= 4 and parts[1] == "note_unbound":
        return parts[3]
    return None


def _pending_review(world_state: dict, accepted: list) -> dict:
    """The end-of-build review payload (N7): compromises (amended notes)
    and explicitly accepted note obligations, each with enough context for
    the user to veto. Empty dict when the user got every note as agreed."""
    brief = world_state.get("brief") if isinstance(world_state.get("brief"), dict) else {}
    notes_by_id = {n.get("id"): n for n in (brief.get("notes") or [])
                   if isinstance(n, dict)}
    amended = [
        {"id": n.get("id"), "subject": n.get("subject", ""),
         "original_text": n.get("original_text", ""),
         "amended_text": n.get("text", ""),
         "rationale": n.get("rationale", "")}
        for n in notes_by_id.values() if n.get("status") == "amended"]
    accepted_notes = []
    for a in accepted:
        nid = _note_id_of(a.get("key", ""))
        if nid is None:
            continue
        note = notes_by_id.get(nid, {})
        accepted_notes.append({
            "id": nid, "subject": note.get("subject", ""),
            "text": note.get("text", ""),
            "finding": a.get("finding", ""),
            "reason": a.get("note", "")})
    if not amended and not accepted_notes:
        return {}
    return {"amended": amended, "accepted_notes": accepted_notes}


async def _done_gate(handle: AgentBuild, done_claim: dict, budgets: dict):
    """Final evaluation on a done claim (D3). Returns (result, rejection):
    result when the gate passes — blocking findings all fixed, accepted by
    key in the claim, or auto-accepted after the fix-round budget (note
    findings excepted, N6) — otherwise a rejection observation for the
    agent."""
    builder = handle.builder
    services = builder.services
    world_state = builder.load_world(handle.world_id)
    compiled = services.compiled.load(handle.world_id)

    # Structural preconditions: an empty world lints clean, so the gate
    # itself refuses builds that plainly haven't happened (P7).
    from wbworldgen.worldgen import mapspace as _ms
    if not ((world_state.get("steps", {}).get("world_rules") or {}).get("data")):
        return None, {
            "done_rejected": True, "blocking_findings": [],
            "message": ("No world rules exist — author them (run_step "
                        "'world_rules') and build the world before claiming "
                        "done; they are the evaluation rubric.")}
    if not any(n.get("name") for n in _ms.all_nodes(compiled)):
        return None, {
            "done_rejected": True, "blocking_findings": [],
            "message": ("The world has no named locations yet — generate the "
                        "map and run the label pass before claiming done.")}

    async def on_progress(evt: dict):
        await _emit(handle, {"type": "progress", "event": evt}, persist=False)

    eval_result = await evaluate_world(
        services, world_state, compiled,
        major_floor=type(builder).MAJOR_IMPORTANCE_FLOOR,
        builder=builder, world_id=handle.world_id, on_event=on_progress)
    _track_findings(handle, eval_result)
    await _emit(handle, {"type": "eval", "trigger": "done_claim",
                         "clean": eval_result["clean"],
                         "blocking": eval_result["blocking"],
                         "findings": len(eval_result["findings"])})

    blocking = [f for f in eval_result["findings"] if f["severity"] == "problem"]
    accepts = set(done_claim.get("accept_findings") or [])
    auto = {k for k, n in handle.finding_rounds.items()
            if n > budgets["fix_rounds"] and not _is_note_finding(k)}
    remaining = [f for f in blocking if f["key"] not in accepts and f["key"] not in auto]
    if remaining:
        return None, {
            "done_rejected": True,
            "blocking_findings": [
                {"key": f["key"], "kind": f["kind"], "finding": f["finding"],
                 "suggestion": f.get("suggestion", "")} for f in remaining],
            "message": (
                f"{len(remaining)} blocking finding(s) stand. Fix them (steered "
                "rework / edit_node / regenerate), contest note findings with "
                "discuss_finding, or claim done again with their keys in "
                "accept_findings plus a note."),
        }

    accepted = [
        {"key": f["key"], "finding": f["finding"],
         "auto": f["key"] in auto and f["key"] not in accepts,
         "note": str(done_claim.get("note", ""))}
        for f in blocking]
    nits = [f for f in eval_result["findings"] if f["severity"] == "nit"]
    result = {
        "summary": str(done_claim.get("summary", "")),
        "accepted_findings": accepted,
        "open_nits": [{"key": f["key"], "finding": f["finding"]} for f in nits],
        "eval": {"clean": eval_result["clean"],
                 "blocking": eval_result["blocking"],
                 "findings": len(eval_result["findings"])},
    }
    # The review gate (N7): compromises and accepted note obligations go to
    # the user — at the absolute end, never mid-build. The world completes
    # and saves regardless; not vetoing means done.
    review = _pending_review(world_state, accepted)
    if review:
        result["pending_review"] = review
    return result, None


# --- the loop ---------------------------------------------------------------

def _budgets(builder) -> dict:
    resolve = builder.services.resolve_int_setting
    return {
        "max_turns": resolve("world.agent_max_turns", DEFAULT_MAX_TURNS, 5, 200),
        "max_tool_calls": resolve("world.agent_max_tool_calls",
                                  DEFAULT_MAX_TOOL_CALLS, 5, 400),
        "fix_rounds": resolve("world.agent_fix_rounds", DEFAULT_FIX_ROUNDS, 1, 10),
    }


async def _chat_respond(handle: AgentBuild, ctx: "ToolContext", chat_turns: int):
    """Answer the queued user message(s): one turn-budgeted mini-loop (U6).
    Each completion may carry ``say`` and one action from the restricted
    chat catalog; the action's result feeds the next completion, and a
    completion without an action ends the exchange. Budget exhaustion and
    repeated LLM failures are surfaced loudly and leave the session alive —
    a design conversation survives a failed reply (the next message
    retries); only the build phase escalates failures to a build abort."""
    builder = handle.builder
    services = builder.services
    while handle.pending_messages:
        msg = handle.pending_messages.pop(0)
        await _emit(handle, {"type": "user_message", "phase": "chat",
                             "id": msg["id"], "text": msg["text"]})
    observations: list = []
    failures = 0
    turns_used = 0
    while turns_used < chat_turns:
        if handle.cancel_requested or handle.status != "running":
            return
        world_state = builder.load_world(handle.world_id)
        messages = [
            {"role": "system", "content": _chat_system_prompt(world_state)},
            {"role": "user",
             "content": _chat_user_payload(handle, world_state, observations)},
        ]
        try:
            completion = await chat_turn(services, messages)
        except Exception as e:
            failures += 1
            logger.warning("chat turn failed for %s: %s", handle.world_id, e)
            if failures >= MAX_LLM_FAILURES:
                await _emit(handle, {
                    "type": "observation", "phase": "chat", "ok": False,
                    "error": (f"The design partner could not answer "
                              f"({failures} attempts; last error: {e}). "
                              "Send a message to try again.")})
                return
            continue
        failures = 0
        turns_used += 1
        handle.chat_turns += 1
        say, action, ready, problems = _validate_chat_completion(completion)
        if ready is not None:
            handle.ready = ready
        await _emit(handle, {"type": "turn", "phase": "chat",
                             "chat_turn": handle.chat_turns,
                             "ready": handle.ready,
                             **({"say": say} if say else {})})
        if problems:
            observation = {"protocol_error": "; ".join(problems)}
            observations.append(observation)
            await _emit(handle, {"type": "observation", "phase": "chat",
                                 "ok": False, **observation})
            continue
        if action is None:
            return  # replied; the conversation is the player's again
        tool_id = action["tool"]
        args = action.get("args") or {}
        await _emit(handle, {"type": "action", "phase": "chat",
                             "chat_turn": handle.chat_turns,
                             "tool": tool_id, "args": args})
        if tool_id not in CHAT_TOOL_IDS:
            # The restriction is the harness's, not the registry's: the
            # build tools exist, they are just not this agent's (U1).
            observation = {
                "action": {"tool": tool_id, "args": args},
                "error": (f"'{tool_id}' is not available in the design "
                          f"conversation — only {', '.join(CHAT_TOOL_IDS)} "
                          "are. The build tools unlock when the player "
                          "starts the build.")}
        else:
            # No checkpoint here (v2c stays build-scoped): the only chat
            # mutations are brief edits, which revert deliberately carries
            # forward (R3) — and revert itself is not in this catalog.
            try:
                result = await invoke_tool(ctx, tool_id, args)
                observation = {"action": {"tool": tool_id, "args": args},
                               "result": result}
            except ToolError as e:
                observation = {"action": {"tool": tool_id, "args": args},
                               "error": str(e)}
        ok = "error" not in observation
        observations.append(observation)
        await _emit(handle, {"type": "observation", "phase": "chat", "ok": ok,
                             **({"result": observation.get("result")} if ok
                                else {"error": observation.get("error")})})
    await _emit(handle, {
        "type": "observation", "phase": "chat", "ok": False,
        "error": (f"The design conversation's per-message budget "
                  f"({chat_turns} completions) ran out before the reply "
                  "finished — the drafts hold every change made so far.")})


async def _flip_to_build(handle: AgentBuild):
    """Go (C7b): the same session becomes the self-driving build. The notes
    get their stable ids exactly as N1 specifies, the world's phase marker
    flips, and the checkpoint window opens fresh (v2c's 'cleared at launch'
    — launch is the build phase's start; chat leaves no checkpoints)."""
    from wbworldgen.worldgen import notes as _notes

    builder = handle.builder
    world_state = builder.load_world(handle.world_id)
    brief = (world_state.get("brief")
             if isinstance(world_state.get("brief"), dict) else {})
    brief.setdefault("prompt", world_state.get("seed_prompt", ""))
    brief["rules"] = [str(r).strip() for r in (brief.get("rules") or [])
                      if str(r).strip()]
    brief["notes"] = _notes.assign_ids(_notes.clean_notes(brief.get("notes")))
    seed = str(brief.get("prompt") or "").strip()
    builder.update_brief(handle.world_id, brief=brief,
                         seed_prompt=seed or None, agent_phase="build")
    handle.brief = brief
    if seed:
        handle.seed_prompt = seed
    handle.go_requested = False
    handle.phase = "build"
    _clear_checkpoints(builder, handle.world_id)
    await _emit(handle, {"type": "phase", "phase": "build"})


async def _chat_phase(handle: AgentBuild):
    """The chat phase's outer loop: wait for user messages and answer each
    batch through the mini-loop; flip to the build when Go lands. Unlike
    the build loop this phase is message-paced — it spends nothing while
    the player thinks, and has no budget of its own beyond the per-message
    one (P9: the structural unit is completions per message)."""
    builder = handle.builder
    chat_turns = builder.services.resolve_int_setting(
        "world.agent_chat_turns", DEFAULT_CHAT_TURNS, 2, 24)

    async def on_progress(evt: dict):
        await _emit(handle, {"type": "progress", "event": evt}, persist=False)

    ctx = ToolContext(builder=builder, world_id=handle.world_id,
                      on_event=on_progress, build=handle)
    while handle.status == "running":
        if handle.cancel_requested:
            handle.status = "cancelled"
            return
        if handle.go_requested:
            # Go outranks queued messages: they ride into the build and
            # drain at its first turn boundary (C7a machinery).
            await _flip_to_build(handle)
            return
        if handle.pending_messages:
            await _chat_respond(handle, ctx, chat_turns)
            continue
        handle._wake.clear()
        # Re-check after clearing: a poke that landed between the checks
        # above and the clear must not be slept through.
        if (handle.pending_messages or handle.go_requested
                or handle.cancel_requested):
            continue
        await handle._wake.wait()


async def _run_build(handle: AgentBuild):
    builder = handle.builder
    services = builder.services
    budgets = _budgets(builder)

    async def on_progress(evt: dict):
        await _emit(handle, {"type": "progress", "event": evt}, persist=False)

    ctx = ToolContext(builder=builder, world_id=handle.world_id,
                      on_event=on_progress, build=handle)
    llm_failures = 0
    try:
        while handle.turns < budgets["max_turns"]:
            if handle.cancel_requested:
                handle.status = "cancelled"
                break
            handle.turns += 1
            # The turn boundary is where user messages land (C7a): queued
            # texts become this turn's freshest observations.
            await _drain_messages(handle)
            world_state = builder.load_world(handle.world_id)
            messages = [
                {"role": "system",
                 "content": _system_prompt(handle, world_state, budgets)},
                {"role": "user", "content": _user_message(handle, budgets)},
            ]
            try:
                completion = await agent_turn(services, messages)
            except Exception as e:
                llm_failures += 1
                logger.warning("agent turn %d failed for %s: %s",
                               handle.turns, handle.world_id, e)
                if llm_failures >= MAX_LLM_FAILURES:
                    raise RuntimeError(
                        f"agent loop aborted: {llm_failures} consecutive "
                        f"LLM failures (last: {e})")
                handle.recent.append({"turn": handle.turns,
                                      "error": f"LLM turn failed: {e}"})
                continue
            llm_failures = 0

            todo, action, done_claim, problems = _validate_completion(completion)
            if todo is not None:
                handle.todo = todo
            # ``say`` is the user-facing reply channel (C7a/U6) — free text
            # like ``thought``, carried on the turn event so the observer
            # renders it as chat; the empty default keeps events lean.
            say = (str(completion.get("say") or "").strip()
                   if isinstance(completion, dict) else "")
            await _emit(handle, {"type": "turn", "turn": handle.turns,
                                 "thought": str((completion or {}).get("thought", ""))
                                 if isinstance(completion, dict) else "",
                                 "todo": list(handle.todo),
                                 **({"say": say} if say else {})})
            if problems:
                observation = {"protocol_error": "; ".join(problems)}
                handle.recent.append({"turn": handle.turns, **observation})
                await _emit(handle, {"type": "observation", "turn": handle.turns,
                                     "ok": False, **observation})
                continue

            if done_claim is not None:
                result, rejection = await _done_gate(handle, done_claim, budgets)
                if rejection is not None:
                    handle.recent.append({"turn": handle.turns, **rejection})
                    await _emit(handle, {"type": "observation", "turn": handle.turns,
                                         "ok": False, **rejection})
                    continue
                handle.result = result
                handle.status = "done"
                world_state = builder.load_world(handle.world_id)
                world_state["complete"] = True
                builder.save_world(handle.world_id, world_state)
                break

            # Action turn.
            tool_id = action["tool"]
            args = action.get("args") or {}
            if handle.tool_calls >= budgets["max_tool_calls"]:
                observation = {
                    "error": (f"Tool budget exhausted "
                              f"({budgets['max_tool_calls']} calls). Only a done "
                              "claim is possible now.")}
                handle.recent.append({"turn": handle.turns,
                                      "action": {"tool": tool_id}, **observation})
                await _emit(handle, {"type": "observation", "turn": handle.turns,
                                     "ok": False, **observation})
                continue
            await _emit(handle, {"type": "action", "turn": handle.turns,
                                 "tool": tool_id, "args": args})
            # The world snapshot rides the action's log index (v2c): the id
            # the revert tool restores by, echoed in the observation.
            action_i = handle.log[-1]["i"] if handle.log else 0
            checkpoint, cp_error = _checkpoint_before(handle, tool_id, action_i)
            if cp_error is not None:
                observation = {
                    "action": {"tool": tool_id, "args": args},
                    "error": (cp_error + "; the action was NOT run — the "
                              "world is unchanged.")}
                handle.recent.append({"turn": handle.turns, **observation})
                await _emit(handle, {"type": "observation", "turn": handle.turns,
                                     "ok": False,
                                     "error": observation["error"]})
                continue
            handle.tool_calls += 1
            try:
                result = await invoke_tool(ctx, tool_id, args)
                ok = True
                observation = {"action": {"tool": tool_id, "args": args},
                               "result": result}
                if checkpoint is not None:
                    observation["checkpoint"] = action_i
                if tool_id == "evaluate":
                    _track_findings(handle, result)
                    await _emit(handle, {"type": "eval", "trigger": "tool",
                                         "clean": result.get("clean"),
                                         "blocking": result.get("blocking"),
                                         "findings": len(result.get("findings", []))})
            except ToolError as e:
                ok = False
                observation = {"action": {"tool": tool_id, "args": args},
                               "error": str(e)}
            handle.recent.append({"turn": handle.turns, **observation})
            await _emit(handle, {"type": "observation", "turn": handle.turns,
                                 "ok": ok,
                                 **({"checkpoint": action_i}
                                    if ok and checkpoint is not None else {}),
                                 **({"result": observation.get("result")} if ok
                                    else {"error": observation.get("error")})})
        else:
            handle.status = "budget_exhausted"
        if handle.cancel_requested and handle.status == "running":
            handle.status = "cancelled"
    except Exception as e:
        logger.exception("agent build failed for %s", handle.world_id)
        handle.status = "failed"
        handle.error = str(e)


async def _run_session(handle: AgentBuild):
    """The session task (C7b's two-mode loop): a session that opens in the
    chat phase converses until Go flips it into the self-driving build;
    direct launches (agent/build, veto fix runs, adoption) start in the
    build phase and never see chat. One terminal path serves both phases —
    whatever ends the session, the unread drain, the checkpoint sweep and
    the terminal event happen exactly once."""
    builder = handle.builder
    try:
        if handle.phase == "chat":
            await _chat_phase(handle)
        if handle.status == "running" and handle.phase == "build":
            await _run_build(handle)
    except Exception as e:
        logger.exception("agent session failed for %s", handle.world_id)
        handle.status = "failed"
        handle.error = str(e)
    finally:
        if handle.phase == "chat" and handle.status == "running":
            # The TASK is ending but the conversation is not (task
            # cancellation — a graceful backend shutdown): the chat phase
            # is resumable by construction, so no terminal event and no
            # unread drain — the artifact stays a live snapshot (queued
            # messages included, re-queued on resume) and only the streams
            # close.
            _persist_artifact(handle)
        else:
            handle.finished_at = datetime.utcnow().isoformat() + "Z"
            # Messages still queued when the session ends are recorded
            # unread — the log stays the honest record of everything the
            # user said.
            await _drain_messages(handle, unread=True)
            # Every terminal state closes the revert window (v2c):
            # checkpoints are build-scoped scaffolding, not world content.
            _clear_checkpoints(builder, handle.world_id)
            await _emit(handle, {"type": "done", "status": handle.status,
                                 "phase": handle.phase,
                                 "turns": handle.turns,
                                 "tool_calls": handle.tool_calls,
                                 "error": handle.error,
                                 "result": handle.result})
        for q in list(handle.subscribers):
            q.put_nowait(None)


# --- public surface ---------------------------------------------------------

def start_agent_build(builder, seed_prompt: str, scenario: str = "",
                      scenario_id: str = None, world_id: str = None,
                      rules: list = None, notes: list = None) -> AgentBuild:
    """Create (or adopt) the world draft and launch the build loop as a
    server-side task. Returns the registered handle immediately; observers
    attach via its queue or the SSE route.

    ``rules`` are the ideation conversation's co-authored world rules (C4):
    persisted with the prompt as the world's brief artifact (D4), rendered
    into every turn's system prompt, and fed to the world_rules step as
    fixed input. ``notes`` are the conversation's design notes (C5): they
    ride the brief with stable ids, inject scoped into generation, and the
    build is verified against every one. ``world_id`` normally stays None
    (a fresh draft with a generated id); when given and the world already
    exists on disk, the build adopts its current content instead of
    resetting it (tests, the veto fix run, and the v2 resume-onto-draft
    direction) — passing neither rules nor notes keeps the brief a
    previous launch recorded.
    """
    from wbworldgen.worldgen import notes as _notes

    existing = _BUILDS.get(world_id) if world_id else None
    if existing is not None and existing.status == "running":
        raise ValueError(f"An agent build is already running for '{world_id}'")

    state = {"seed_prompt": seed_prompt, "steps": {}}
    if world_id:
        try:
            state = builder.load_world(world_id)
            state.setdefault("seed_prompt", seed_prompt)
        except FileNotFoundError:
            pass
    if scenario_id:
        state["scenario_id"] = scenario_id
    if scenario:
        state["scenario"] = scenario
    # The brief (D4): the agent's standing instructions, persisted in the
    # world itself.
    rules = [str(r).strip() for r in (rules or []) if str(r).strip()]
    notes = _notes.assign_ids(_notes.clean_notes(notes))
    if rules or notes or not isinstance(state.get("brief"), dict):
        state["brief"] = {"prompt": state.get("seed_prompt", seed_prompt),
                          "rules": rules, "notes": notes}
    # A starting build is by definition incomplete — without this, adopting
    # an already-saved world would record draft_complete and the draft would
    # read as finished before the agent did anything.
    state["complete"] = False
    # Direct launches skip the chat phase (zero-turn Go, veto fix runs,
    # adoption) — the world is in the build phase from its first event.
    state["agent_phase"] = "build"
    world_id = builder.save_draft(world_id or "", state)
    # Leftover checkpoints from an earlier build on this world (adopt/veto
    # relaunch, or a crash that skipped terminal cleanup) would collide
    # with this build's fresh action indices — the revert window is
    # strictly per-build (v2c).
    _clear_checkpoints(builder, world_id)

    handle = AgentBuild(world_id, state.get("seed_prompt", seed_prompt), builder,
                        brief=state.get("brief"))
    _BUILDS[world_id] = handle
    _persist_artifact(handle)
    handle.task = asyncio.create_task(_run_session(handle))
    return handle


def start_chat_session(builder, first_message: str, prompt: str = "",
                       scenario: str = "", scenario_id: str = None) -> AgentBuild:
    """Open a C7b session in the chat phase: lazily create the draft world
    (the session needs a world id from its first event — the artifact lives
    in the world dir; C7 fork 2's settled default), record a brief holding
    whatever prompt the user has typed so far, queue the first message and
    start the session task. A session never taken to Go stays an ordinary
    in-progress draft: resumable by any later message (the artifact carries
    the transcript) and deletable like any world — the explicit-discard
    cleanup story, no sweeps."""
    text = str(first_message or "").strip()
    if not text:
        raise ValueError("Say something to start the design conversation.")
    prompt = str(prompt or "").strip()
    state = {
        "seed_prompt": prompt,
        "steps": {},
        "complete": False,
        "agent_phase": "chat",
        "brief": {"prompt": prompt, "rules": [], "notes": []},
    }
    if scenario_id:
        state["scenario_id"] = scenario_id
    if scenario:
        state["scenario"] = scenario
    world_id = builder.save_draft("", state)
    handle = AgentBuild(world_id, prompt, builder, brief=state["brief"],
                        phase="chat")
    _BUILDS[world_id] = handle
    handle.post_message(text)
    _persist_artifact(handle)
    handle.task = asyncio.create_task(_run_session(handle))
    return handle


def resume_chat_session(builder, world_id: str) -> AgentBuild:
    """A running chat-phase handle for the world: the live one when it
    exists, else a session recreated from the persisted artifact (C7b: the
    transcript in the artifact makes the chat phase resumable by
    construction — a backend restart or a cancelled conversation revives on
    the next message, Go or hand edit). The world state on disk is the
    brief's truth; the artifact contributes the transcript, the message
    counter, and any messages left queued in its snapshot (re-queued — the
    never-dropped promise survives a crash). Raises ValueError when the
    world has no resumable design conversation; the *build* phase's restart
    resume stays a recorded v2 item."""
    existing = _BUILDS.get(world_id)
    if existing is not None and existing.status == "running":
        if existing.phase != "chat":
            raise ValueError(
                f"The session for '{world_id}' is already building.")
        return existing
    artifact = load_build_artifact(builder, world_id)
    if artifact is None:
        raise ValueError(f"No agent session exists for world '{world_id}'.")
    if artifact.get("phase") != "chat":
        raise ValueError(
            f"World '{world_id}' has no resumable design conversation — "
            "its session already went to build.")
    world_state = builder.load_world(world_id)
    handle = AgentBuild(world_id, str(world_state.get("seed_prompt") or ""),
                        builder, brief=world_state.get("brief"), phase="chat")
    handle.log = [e for e in (artifact.get("log") or []) if isinstance(e, dict)]
    handle.chat_turns = int(artifact.get("chat_turns") or 0)
    handle.ready = bool(artifact.get("ready"))
    requeue = [m for m in (artifact.get("queued_messages") or [])
               if isinstance(m, dict) and str(m.get("text") or "").strip()]
    seq = 0
    for m in handle.log + requeue:
        if m.get("type") not in (None, "user_message"):
            continue
        mid = str(m.get("id") or "")
        if mid.startswith("m") and mid[1:].isdigit():
            seq = max(seq, int(mid[1:]))
    handle.message_seq = seq + 1
    handle.pending_messages = [{"id": m["id"], "text": m["text"]}
                               for m in requeue]
    if handle.pending_messages:
        handle._wake.set()
    _BUILDS[world_id] = handle
    _persist_artifact(handle)
    handle.task = asyncio.create_task(_run_session(handle))
    return handle


def veto_notes(builder, world_id: str, note_ids: list) -> AgentBuild:
    """The user's veto (N7): re-assert the vetoed notes as binding and
    relaunch the agent on the finished world as a normal bounded build (the
    adopt-a-world path — same loop, same gate, same review if new
    compromises appear).

    For an amended note the original text is restored; every vetoed note is
    marked ``no_compromise`` — it can never be amended again (the discuss
    channel refuses). Unknown ids and worlds without a brief fail loudly
    (P7); a still-running build raises ValueError like any double launch.
    """
    if not note_ids:
        raise ValueError("No note ids to veto.")
    existing = _BUILDS.get(world_id)
    if existing is not None and existing.status == "running":
        raise ValueError(f"An agent build is already running for '{world_id}'")
    world_state = builder.load_world(world_id)
    brief = world_state.get("brief") if isinstance(world_state.get("brief"), dict) else None
    if not brief:
        raise ValueError(f"World '{world_id}' has no ideation brief — "
                         "nothing to veto.")
    notes_by_id = {n.get("id"): n for n in (brief.get("notes") or [])
                   if isinstance(n, dict)}
    unknown = [nid for nid in note_ids if nid not in notes_by_id]
    if unknown:
        raise ValueError(f"No such note(s) in the brief: {', '.join(unknown)}")

    for nid in note_ids:
        note = notes_by_id[nid]
        if note.get("status") == "amended":
            note["text"] = note.get("original_text") or note["text"]
            note.pop("status", None)
            note.pop("original_text", None)
            note.pop("rationale", None)
        note["no_compromise"] = True
    builder.update_brief(world_id, brief=brief)

    return start_agent_build(
        builder, world_state.get("seed_prompt", ""), world_id=world_id)


def cancel_build(world_id: str) -> bool:
    """Request cancellation of a running build (checked between turns; an
    in-flight enrichment run is cancelled through the engine so the current
    tool call returns early). False when no running build exists."""
    handle = _BUILDS.get(world_id)
    if handle is None or handle.status != "running":
        return False
    handle.cancel_requested = True
    handle.poke()  # a chat phase idling on its wake must see the flag
    try:
        handle.builder.enrich_cancel(world_id)
    except Exception:
        pass
    return True
