"""The note verifier (C5/N4): a read-only agent that checks every ideation
note against the built world.

The recorded v2 "tool-looping evaluator" upgrade, arrived scoped to notes: a
bounded loop on the smartest slot whose toolset is carved mechanically from
the tool registry — every ``mutates=False`` tool except ``evaluate`` (the
verifier runs *inside* evaluation) and ``discuss_finding`` (the builder's
channel TO the verifier) — so future read tools flow to it without edits
(P2). In: the note checklist with live bindings. Out: one verdict per note
(honored / not honored, evidence, suggestion). The evaluator turns
not-honored and unverified notes into blocking ``note:*`` findings, which
the done-gate never auto-accepts (N6).

The verifier serves the note's AUTHOR — the world's creator — not the
builder; its system prompt fixes that stance, and the discuss channel (N5)
reuses this module's loop with a per-finding transcript. Without a live LLM
``verify_notes`` reports itself skipped and note verification degrades to
the deterministic ``note_unbound`` lint, keeping the done-gate testable
offline; ``verifier_turn`` is the canned-sequence patch point, the same
contract as ``harness.agent_turn``.
"""

import json
import logging

from wbworldgen.worldgen import notes as _notes
from wbworldgen.worldgen.agent.registry import (
    ToolContext,
    ToolError,
    invoke_tool,
    registered_tools,
    unavailable_tool_ids,
)
from wbworldgen.worldgen.generation.llm import json_retry_completion

logger = logging.getLogger(__name__)

#: Structural budget (D5): verifier turns per verification run, setting
#: ``world.note_verifier_max_turns``.
DEFAULT_MAX_TURNS = 16

#: Consecutive verifier-turn LLM failures before the run gives up (the
#: remaining notes report as unverified — loud, never silent).
MAX_LLM_FAILURES = 2

#: Read-only tools the verifier still must not touch.
_EXCLUDED_TOOLS = frozenset({"evaluate", "discuss_finding"})

#: Observations kept verbatim in the verifier's turn prompt (P9: structural
#: budget — older reads drop off whole; re-reading is cheap).
_RECENT_LIMIT = 6


def verifier_tool_ids(services=None) -> list:
    """The verifier's toolset: mechanically the non-mutating slice of the
    registry, minus the exclusions. With ``services`` given,
    availability-gated tools that fail their predicate are excluded too —
    the verifier reads through the same wiring the builder does (v2e)."""
    hidden = unavailable_tool_ids(services) if services is not None else set()
    return [s.id for s in registered_tools()
            if not s.mutates and s.id not in _EXCLUDED_TOOLS
            and s.id not in hidden]


def _render_tools(services=None) -> str:
    ids = set(verifier_tool_ids(services))
    lines = []
    for s in registered_tools():
        if s.id not in ids:
            continue
        params = ", ".join(
            f"{name}{'*' if p.get('required') else ''}"
            for name, p in (s.params or {}).items())
        lines.append(f"- {s.id}({params}): {s.description}")
    return "\n".join(lines)


async def verifier_turn(services, messages: list) -> dict:
    """One verifier completion on the smartest slot. Module-level so
    canned-sequence tests monkeypatch it — the ``agent_turn`` contract."""
    return await json_retry_completion(
        services.llm,
        messages=messages,
        model=services.llm.storyteller_model,
        temperature=0.2,
        inspector_ctx={"call_type": "world_build", "step": "agent:verify_notes"},
        step_label="agent:verify_notes",
        retry_attempts=services.json_retry_attempts,
    )


def _checklist(notes: list) -> str:
    lines = []
    for n in notes:
        where = ("the whole world" if n["scope"] == "world"
                 else f"map '{n['map_id']}'")
        amended = (" (text amended by an agreed compromise — verify the "
                   "amended text)" if n.get("status") == "amended" else "")
        lines.append(f"- {n['id']} [{where}]{amended}: {n['text']}")
        if n.get("verifier_context"):
            lines.append(f"  (context from an earlier discussion: "
                         f"{n['verifier_context']})")
    return "\n".join(lines)


def _system_prompt(notes: list, max_turns: int, services=None) -> str:
    return f"""You are the note verifier for an AI-built game world. The world's \
creator agreed on the notes below during ideation; a build agent has since \
built the world; you now check, note by note, whether the built world actually \
embodies each one. You work for the notes' AUTHOR — the world's creator — \
never for the builder: judge only what the world's content shows, not what \
would be convenient to claim.

Verify against evidence: read the world with the tools below before judging. \
A note bound to a map is honored only if that map's own content (its \
description, its locations' names and descriptions) embodies it. A \
world-wide note must hold everywhere it applies. Do not judge style or \
taste — only whether the note's substance is present and uncontradicted. \
When the evidence genuinely supports the note, say honored: false findings \
waste the builder's budget.

## The notes to verify
{_checklist(notes)}

## Read tools
{_render_tools(services)}

## Response protocol
Reply with exactly ONE JSON object and nothing else. Either read:
{{"thought": "...", "action": {{"tool": "...", "args": {{...}}}}}}
or, when you have seen enough evidence for every note, deliver ALL verdicts:
{{"thought": "...", "verdicts": [{{"id": "n1", "verdict": "honored"|"not_honored", "evidence": "what the world shows, one sentence", "suggestion": "how to fix it, one sentence (not_honored only)"}}, ...]}}
- Exactly one verdict per note id, every note covered.
- You have {max_turns} turns total — read what you need, then judge."""


def _normalize_verdicts(raw: list, notes: list) -> dict:
    """Validated per-note verdicts: every checklist note gets exactly one;
    ids the checklist doesn't know are dropped; notes the model skipped
    report as unverified (loud, never silently passed)."""
    by_id = {n["id"]: n for n in notes}
    verdicts, seen = [], set()
    for v in raw if isinstance(raw, list) else []:
        if not isinstance(v, dict):
            continue
        nid = str(v.get("id") or "")
        note = by_id.get(nid)
        if note is None or nid in seen:
            continue
        seen.add(nid)
        verdicts.append({
            "id": nid,
            "subject": note.get("subject", ""),
            "map_id": note.get("map_id"),
            "text": note["text"],
            "verdict": "honored" if v.get("verdict") == "honored" else "not_honored",
            "evidence": str(v.get("evidence", "")),
            "suggestion": str(v.get("suggestion", "")),
        })
    unverified = [nid for nid in by_id if nid not in seen]
    return {"verdicts": verdicts, "unverified": unverified, "skipped": False}


def notes_to_verify(world_state: dict, compiled: dict, map_id: str = None) -> list:
    """The verifier's checklist: bound notes only (unbound subjects are the
    ``note_unbound`` lint's job — there is nothing to read for them). A
    ``map_id`` scope narrows to that map's notes."""
    notes = [n for n in _notes.bound_notes(world_state, compiled)
             if n["scope"] in ("world", "map")]
    if map_id is not None:
        notes = [n for n in notes if n.get("map_id") == map_id]
    return notes


async def verify_notes(services, builder, world_id: str, world_state: dict,
                       compiled: dict, map_id: str = None,
                       on_event=None) -> dict:
    """Run the verifier loop over the world's notes. Returns ``{"verdicts",
    "unverified", "skipped"}``; ``skipped`` is True when there is nothing to
    verify or no live LLM (offline degradation, N4). ``on_event`` (optional
    async sink) receives transient ``verifier_action`` progress events."""
    notes = notes_to_verify(world_state, compiled, map_id=map_id)
    llm_live = (services.llm is not None
                and getattr(services.llm, "mode", "mock") != "mock")
    if not notes or not llm_live or builder is None:
        return {"verdicts": [], "unverified": [], "skipped": True}

    max_turns = services.resolve_int_setting(
        "world.note_verifier_max_turns", DEFAULT_MAX_TURNS, 5, 40)
    system = _system_prompt(notes, max_turns, services)
    allowed = set(verifier_tool_ids(services))
    ctx = ToolContext(builder=builder, world_id=world_id, on_event=on_event)
    recent: list = []
    failures = 0

    for turn in range(1, max_turns + 1):
        user = ("Current state:\n"
                + json.dumps({"turn": f"{turn}/{max_turns}",
                              "recent": recent[-_RECENT_LIMIT:]},
                             indent=2, ensure_ascii=False)
                + "\n\nReply with exactly one protocol JSON object.")
        try:
            completion = await verifier_turn(
                services,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}])
            failures = 0
        except Exception as e:
            failures += 1
            logger.warning("note-verifier turn failed for %s: %s", world_id, e)
            if failures >= MAX_LLM_FAILURES:
                break
            recent.append({"error": f"LLM turn failed: {e}"})
            continue

        raw_verdicts = (completion.get("verdicts")
                        if isinstance(completion, dict) else None)
        if isinstance(raw_verdicts, list):
            return _normalize_verdicts(raw_verdicts, notes)

        action = completion.get("action") if isinstance(completion, dict) else None
        if not isinstance(action, dict) or not action.get("tool"):
            recent.append({"protocol_error":
                           "reply with either 'action' or 'verdicts'"})
            continue
        tool_id = str(action["tool"])
        args = action.get("args") or {}
        if tool_id not in allowed:
            recent.append({"action": {"tool": tool_id},
                           "error": (f"tool '{tool_id}' is not available to "
                                     "the verifier; allowed: "
                                     + ", ".join(sorted(allowed)))})
            continue
        if on_event is not None:
            await on_event({"type": "verifier_action", "tool": tool_id,
                            "args": args})
        try:
            result = await invoke_tool(ctx, tool_id, args)
            recent.append({"action": {"tool": tool_id, "args": args},
                           "result": result})
        except ToolError as e:
            recent.append({"action": {"tool": tool_id, "args": args},
                           "error": str(e)})

    # Loop exhausted (or the LLM kept failing) without verdicts: every note
    # reports unverified — a blocking finding, never a silent pass (P7).
    return {"verdicts": [], "unverified": [n["id"] for n in notes],
            "skipped": False}


# --- the discussion channel (N5) --------------------------------------------

#: Verifier turns inside ONE discussion exchange (reads + the answer).
DEFAULT_DISCUSSION_TURNS = 6

#: Builder<->verifier exchanges per finding before it stands as-is,
#: setting ``world.note_discussion_rounds``.
DEFAULT_DISCUSSION_ROUNDS = 3


async def discuss_turn(services, messages: list) -> dict:
    """One discussion completion. Module-level patch point, separate from
    ``verifier_turn`` so canned tests can drive the two loops apart."""
    return await json_retry_completion(
        services.llm,
        messages=messages,
        model=services.llm.storyteller_model,
        temperature=0.3,
        inspector_ctx={"call_type": "world_build", "step": "agent:discuss_note"},
        step_label="agent:discuss_note",
        retry_attempts=services.json_retry_attempts,
    )


def _discussion_system(note: dict, verdict: dict, transcript: list,
                       allow_compromise: bool, max_turns: int,
                       services=None) -> str:
    outcomes = ('"upheld" | "withdrawn" | "compromise"' if allow_compromise
                else '"upheld" | "withdrawn"')
    compromise_rule = (
        "- \"compromise\" ONLY when an amended note text genuinely serves "
        "the creator's evident intent — or resolves a real conflict between "
        "notes or with the world rules — never because the builder finds "
        "the note hard or expensive. The amendment goes to the creator for "
        "review after the build; they can veto it.\n"
        if allow_compromise else
        "- A compromise on this note was already VETOED by the creator: "
        "amending it again is impossible. Only \"upheld\" or \"withdrawn\" "
        "exist.\n")
    lines = []
    for t in transcript:
        lines.append(f"Builder: {t['builder']}")
        if t.get("verifier"):
            lines.append(f"You ({t.get('outcome', 'replied')}): {t['verifier']}")
    history = "\n".join(lines) if lines else "(none yet)"
    return f"""You are the note verifier for an AI-built game world, in a discussion \
with the build agent about ONE of your findings. You flagged the note below as \
not honored; the builder is contesting or seeking clarification. You work for \
the note's AUTHOR — the world's creator — not for the builder. Be firm but \
honest: the point is the creator getting the world they asked for.

## The note
{note.get('id', '?')}{f" (about: {note['subject']})" if note.get('subject') else ""}: {note.get('text', '')}

## Your finding
{verdict.get('evidence') or '(no evidence recorded)'}

## The discussion so far
{history}

## How to answer
- "upheld": the finding stands — say precisely what the world must show \
before you will pass it.
- "withdrawn": ONLY when the builder's evidence shows the note IS honored \
and your finding was wrong — re-read the world first to check any claim.
{compromise_rule}
## Read tools (to check claims before answering)
{_render_tools(services)}

## Response protocol
Reply with exactly ONE JSON object and nothing else. Either read:
{{"thought": "...", "action": {{"tool": "...", "args": {{...}}}}}}
or answer the builder:
{{"thought": "...", "reply": "your answer to the builder", "outcome": {outcomes}{', "amended_text": "the full amended note text"' if allow_compromise else ''}}}
- "amended_text" is REQUIRED with outcome "compromise" and forbidden otherwise.
- You have {max_turns} turns in this exchange — read what you need, then answer."""


async def discuss_note(services, builder, world_id: str, note: dict,
                       verdict: dict, transcript: list, message: str,
                       on_event=None) -> dict:
    """One builder->verifier exchange about one note finding. Returns
    ``{"reply", "outcome", "amended_text"?}``; outcome is always one of
    upheld/withdrawn/compromise (budget exhaustion upholds — the finding
    never dissolves by silence)."""
    allow_compromise = not note.get("no_compromise")
    system = _discussion_system(note, verdict or {}, transcript,
                                allow_compromise, DEFAULT_DISCUSSION_TURNS,
                                services)
    allowed = set(verifier_tool_ids(services))
    ctx = ToolContext(builder=builder, world_id=world_id, on_event=on_event)
    recent: list = []
    failures = 0

    for turn in range(1, DEFAULT_DISCUSSION_TURNS + 1):
        user = ("The builder says:\n" + message + "\n\nCurrent state:\n"
                + json.dumps({"turn": f"{turn}/{DEFAULT_DISCUSSION_TURNS}",
                              "recent": recent[-_RECENT_LIMIT:]},
                             indent=2, ensure_ascii=False)
                + "\n\nReply with exactly one protocol JSON object.")
        try:
            completion = await discuss_turn(
                services,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}])
            failures = 0
        except Exception as e:
            failures += 1
            logger.warning("note-discussion turn failed for %s: %s", world_id, e)
            if failures >= MAX_LLM_FAILURES:
                break
            recent.append({"error": f"LLM turn failed: {e}"})
            continue

        if isinstance(completion, dict) and completion.get("reply"):
            outcome = completion.get("outcome")
            amended = str(completion.get("amended_text") or "").strip()
            if outcome == "compromise" and (not allow_compromise or not amended):
                recent.append({"protocol_error": (
                    "compromise is not possible for this note" if not allow_compromise
                    else "outcome 'compromise' requires a non-empty 'amended_text'")})
                continue
            if outcome not in ("upheld", "withdrawn", "compromise"):
                recent.append({"protocol_error":
                               "outcome must be upheld, withdrawn or compromise"})
                continue
            result = {"reply": str(completion["reply"]), "outcome": outcome}
            if outcome == "compromise":
                result["amended_text"] = amended
            return result

        action = completion.get("action") if isinstance(completion, dict) else None
        if not isinstance(action, dict) or not action.get("tool"):
            recent.append({"protocol_error":
                           "reply with either 'action' or a 'reply' + 'outcome'"})
            continue
        tool_id = str(action["tool"])
        args = action.get("args") or {}
        if tool_id not in allowed:
            recent.append({"action": {"tool": tool_id},
                           "error": f"tool '{tool_id}' is not available to "
                                    "the verifier"})
            continue
        if on_event is not None:
            await on_event({"type": "verifier_action", "tool": tool_id,
                            "args": args, "discussing": note.get("id")})
        try:
            result = await invoke_tool(ctx, tool_id, args)
            recent.append({"action": {"tool": tool_id, "args": args},
                           "result": result})
        except ToolError as e:
            recent.append({"action": {"tool": tool_id, "args": args},
                           "error": str(e)})

    return {"reply": ("The verifier did not reach a conclusion in this "
                      "exchange; the finding stands."),
            "outcome": "upheld"}
