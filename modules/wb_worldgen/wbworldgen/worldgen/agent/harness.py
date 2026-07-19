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
that many evaluation rounds is auto-accepted and recorded). The user is
out of the loop during the build (P6): steering happened at the go-gate,
observability is the persisted todo/action-log artifact
(``agent_build.json`` in the world directory) streamed live over SSE, and
cancel is always available. Clients reattach to a running build through
the in-process build registry; the artifact serves finished builds after
a restart (backend-restart *resume* is a recorded v2 item).
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

#: Consecutive agent-turn LLM failures before the build aborts.
MAX_LLM_FAILURES = 3

ARTIFACT_FILENAME = "agent_build.json"


class AgentBuild:
    """One build's live state: the registry entry observers attach to."""

    def __init__(self, world_id: str, seed_prompt: str, builder, brief: dict = None):
        self.world_id = world_id
        self.seed_prompt = seed_prompt
        self.builder = builder
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
        self.cancel_requested = False
        self.result: dict = None
        self.error: str = None
        self.started_at = datetime.utcnow().isoformat() + "Z"
        self.finished_at: str = None
        self.subscribers: list = []      # asyncio.Queue per attached observer
        self.task: asyncio.Task = None

    def snapshot(self) -> dict:
        return {
            "world_id": self.world_id, "status": self.status,
            "seed_prompt": self.seed_prompt, "brief": self.brief,
            "turns": self.turns,
            "tool_calls": self.tool_calls, "todo": list(self.todo),
            "started_at": self.started_at, "finished_at": self.finished_at,
            "error": self.error, "result": self.result,
            "log_len": len(self.log),
            "last_event": self.log[-1] if self.log else None,
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

    return f"""You are the build agent for a game world. You work alone: the user \
approved the brief and left — you decide, act, verify your own output, and \
fix what verification finds. Build a complete, coherent, playable world.

## The brief
{handle.seed_prompt}
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
- Fix content findings with steered regeneration: run_pass with rework, \
node_ids and a guidance note is your primary fix instrument. Regenerating \
a step with a steering note is the recourse for structural problems.
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


def _is_note_finding(key: str) -> bool:
    """Note obligations (verifier findings and the unbound-note lint) are
    never auto-accepted (N6): every note ends honored, honored-as-amended,
    or explicitly accepted with a recorded reason."""
    return key.startswith("note:") or ":note_unbound:" in key


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
                "rework / edit_node / regenerate), or claim done again with "
                "their keys in accept_findings plus a note."),
        }

    accepted = [
        {"key": f["key"], "finding": f["finding"],
         "auto": f["key"] in auto and f["key"] not in accepts,
         "note": str(done_claim.get("note", ""))}
        for f in blocking]
    nits = [f for f in eval_result["findings"] if f["severity"] == "nit"]
    return {
        "summary": str(done_claim.get("summary", "")),
        "accepted_findings": accepted,
        "open_nits": [{"key": f["key"], "finding": f["finding"]} for f in nits],
        "eval": {"clean": eval_result["clean"],
                 "blocking": eval_result["blocking"],
                 "findings": len(eval_result["findings"])},
    }, None


# --- the loop ---------------------------------------------------------------

def _budgets(builder) -> dict:
    resolve = builder.services.resolve_int_setting
    return {
        "max_turns": resolve("world.agent_max_turns", DEFAULT_MAX_TURNS, 5, 200),
        "max_tool_calls": resolve("world.agent_max_tool_calls",
                                  DEFAULT_MAX_TOOL_CALLS, 5, 400),
        "fix_rounds": resolve("world.agent_fix_rounds", DEFAULT_FIX_ROUNDS, 1, 10),
    }


async def _run_build(handle: AgentBuild):
    builder = handle.builder
    services = builder.services
    budgets = _budgets(builder)

    async def on_progress(evt: dict):
        await _emit(handle, {"type": "progress", "event": evt}, persist=False)

    ctx = ToolContext(builder=builder, world_id=handle.world_id,
                      on_event=on_progress)
    llm_failures = 0
    try:
        while handle.turns < budgets["max_turns"]:
            if handle.cancel_requested:
                handle.status = "cancelled"
                break
            handle.turns += 1
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
            await _emit(handle, {"type": "turn", "turn": handle.turns,
                                 "thought": str((completion or {}).get("thought", ""))
                                 if isinstance(completion, dict) else "",
                                 "todo": list(handle.todo)})
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
            handle.tool_calls += 1
            await _emit(handle, {"type": "action", "turn": handle.turns,
                                 "tool": tool_id, "args": args})
            try:
                result = await invoke_tool(ctx, tool_id, args)
                ok = True
                observation = {"action": {"tool": tool_id, "args": args},
                               "result": result}
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
    finally:
        handle.finished_at = datetime.utcnow().isoformat() + "Z"
        await _emit(handle, {"type": "done", "status": handle.status,
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
    world_id = builder.save_draft(world_id or "", state)

    handle = AgentBuild(world_id, state.get("seed_prompt", seed_prompt), builder,
                        brief=state.get("brief"))
    _BUILDS[world_id] = handle
    _persist_artifact(handle)
    handle.task = asyncio.create_task(_run_build(handle))
    return handle


def cancel_build(world_id: str) -> bool:
    """Request cancellation of a running build (checked between turns; an
    in-flight enrichment run is cancelled through the engine so the current
    tool call returns early). False when no running build exists."""
    handle = _BUILDS.get(world_id)
    if handle is None or handle.status != "running":
        return False
    handle.cancel_requested = True
    try:
        handle.builder.enrich_cancel(world_id)
    except Exception:
        pass
    return True
