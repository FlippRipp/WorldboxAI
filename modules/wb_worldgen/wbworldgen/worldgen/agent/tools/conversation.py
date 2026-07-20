"""The user-channel tools (C7a of the worldgen plan): brief edits and the
conversation reader.

The brief-edit tools (U2) are how a mid-build user message becomes contract:
the agent's hand, moved by the user's words — update_prompt / update_rules /
update_notes write the brief through ``save_world`` exactly as the discuss
channel's compromise path does, and the next turn's system prompt (re-read
from disk, D4) plus the note verifier (which binds and judges fresh from the
brief) follow the current text. The gate is the description, not machinery
(U2): each tool states it carries direct user input only. A user-directed
note edit is a hand edit, not a compromise (U5) — it clears the note's
negotiation state instead of creating amendment state for the N7 review.
Notes the user vetoed (``no_compromise``) refuse edit and removal outright:
whether the note's *author* may override their own veto through this channel
is an explicitly open fork of the C7 design — until Filip decides it, the
veto lock binds this tool too.

``read_conversation`` (U4) serves the transcript on demand, never ambiently:
every user message and every ``say`` reply from the session's persisted log
— since C7b that is one continuous record from the first ideation exchange
through the running build. It reads the live handle when invoked inside a
build and falls back to the ``agent_build.json`` artifact, so the note
verifier inherits it through N4's mechanical carve (its ToolContext carries
no build handle) and it doubles as recovery for instructions that scrolled
off the harness's recent window.
"""

from wbworldgen.worldgen import notes as _notes
from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool

#: The U2 gate, stated on every brief-edit tool: the description is the
#: mechanism — no harness enforcement ties an edit to a message (U3).
_GATE = (
    "Use ONLY to record what the user just said in a user_message — never "
    "on your own initiative: the brief is the USER'S contract, and an "
    "unprompted edit rewrites the standard your own work is judged against."
)

#: Note-entry keys the agent may pass; everything else (status,
#: original_text, no_compromise, ...) is machinery the tool owns.
_NOTE_KEYS = frozenset({"id", "text", "subject"})


def _load_brief(ctx):
    """(world_state, brief) — loud when the world has no brief to edit
    (every agent build records one at launch; only a bare non-build world
    lacks it)."""
    world_state = ctx.builder.load_world(ctx.world_id)
    brief = world_state.get("brief")
    if not isinstance(brief, dict):
        raise ToolError(
            "This world has no ideation brief — there is no contract to "
            "edit. Briefs are created when a build launches.")
    return world_state, brief


def _persist(ctx, world_state, seed_prompt: str = None):
    """Save the edited brief and keep the live handle's mirror current.
    Metadata-surgical (``update_brief``): a full ``save_world`` would flip
    an in-progress draft to finished as a side effect — fatal in the C7b
    chat phase, whose worlds are drafts mutated only through brief edits.
    ``update_brief`` invalidates the compiled cache, so note bindings
    recompute against the new text."""
    ctx.builder.update_brief(ctx.world_id, brief=world_state["brief"],
                             seed_prompt=seed_prompt)
    if ctx.build is not None:
        ctx.build.brief = world_state["brief"]


async def update_prompt(ctx, prompt: str) -> dict:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ToolError("update_prompt: the prompt cannot be empty.")
    world_state, brief = _load_brief(ctx)
    previous = str(brief.get("prompt") or world_state.get("seed_prompt") or "")
    brief["prompt"] = prompt
    # The brief's prompt IS the world's seed prompt — generation seams
    # (run_step chain context, expansion, compiled generated_from) read
    # state["seed_prompt"], so both move together or the edit is cosmetic.
    world_state["seed_prompt"] = prompt
    _persist(ctx, world_state, seed_prompt=prompt)
    if ctx.build is not None:
        ctx.build.seed_prompt = prompt
    return {"prompt": prompt, "previous": previous,
            "note": ("Future generation reads the new prompt; content "
                     "already generated keeps its text until regenerated.")}


async def update_rules(ctx, rules: list) -> dict:
    cleaned = [r.strip() for r in rules if r.strip()]
    world_state, brief = _load_brief(ctx)
    previous = [str(r).strip() for r in (brief.get("rules") or [])
                if str(r).strip()]
    brief["rules"] = cleaned
    _persist(ctx, world_state)
    result = {"rules": cleaned,
              "added": [r for r in cleaned if r not in previous],
              "removed": [r for r in previous if r not in cleaned]}
    if (world_state.get("steps", {}).get("world_rules") or {}).get("data"):
        result["note"] = (
            "world_rules is already authored — re-run it (run_step "
            "'world_rules') so the authored document, and with it the "
            "evaluation rubric, embodies the change.")
    return result


def _apply_note_entry(entry, by_id, seen_ids):
    """One update_notes entry -> (note dict, disposition) where disposition
    is "added" | "edited" | "kept". Raises ToolError on everything the
    agent must correct."""
    if not isinstance(entry, dict):
        raise ToolError(
            f"update_notes: each entry must be an object, got {entry!r}.")
    unknown = sorted(set(entry) - _NOTE_KEYS)
    if unknown:
        raise ToolError(
            f"update_notes: unknown key(s) {unknown} in {entry!r} — entries "
            "carry id, text and/or subject only; amendment state is not "
            "yours to write.")
    nid = entry.get("id")
    if nid is not None and not isinstance(nid, str):
        raise ToolError(f"update_notes: 'id' must be a string, got {nid!r}.")

    if not nid:
        text = str(entry.get("text") or "").strip()
        if not text:
            raise ToolError(
                "update_notes: a new note (no id) needs a non-empty 'text'.")
        return ({"text": text, "subject": str(entry.get("subject") or "").strip()},
                "added")

    current = by_id.get(nid)
    if current is None:
        raise ToolError(
            f"update_notes: no note '{nid}' exists in the brief (current "
            f"ids: {', '.join(by_id) or 'none'}). Omit 'id' to add a new "
            "note.")
    if nid in seen_ids:
        raise ToolError(f"update_notes: note '{nid}' appears twice.")
    seen_ids.add(nid)

    new_text = None
    if "text" in entry:
        new_text = str(entry.get("text") or "").strip()
        if not new_text:
            raise ToolError(
                f"update_notes: note '{nid}' cannot have empty text — omit "
                "the note from the list to remove it.")
    new_subject = (str(entry.get("subject") or "").strip()
                   if "subject" in entry else None)
    changes = ((new_text is not None and new_text != current.get("text"))
               or (new_subject is not None
                   and new_subject != str(current.get("subject") or "")))
    if not changes:
        return dict(current), "kept"
    if current.get("no_compromise"):
        raise ToolError(
            f"update_notes: note '{nid}' was VETOED by the user — its text "
            "is binding as written and cannot be edited through this tool. "
            "Build what it says.")
    note = dict(current)
    if new_text is not None:
        note["text"] = new_text
    if new_subject is not None:
        note["subject"] = new_subject
    # U5: a user-directed edit makes the note current truth by the user's
    # own hand — negotiation state (a pending amendment, an old withdrawal
    # recorded against the previous text) must not survive onto text the
    # user has since rewritten, and there is nothing left for N7 to review.
    for key in ("status", "original_text", "rationale", "verifier_context"):
        note.pop(key, None)
    return note, "edited"


async def update_notes(ctx, notes: list) -> dict:
    world_state, brief = _load_brief(ctx)
    existing = [n for n in (brief.get("notes") or []) if isinstance(n, dict)]
    by_id = {n["id"]: n for n in existing if n.get("id")}

    out, edited, kept = [], [], []
    seen_ids: set = set()
    for entry in notes:
        note, disposition = _apply_note_entry(entry, by_id, seen_ids)
        out.append(note)
        if disposition == "edited":
            edited.append(note["id"])
        elif disposition == "kept":
            kept.append(note["id"])

    removed = [n for n in existing if n.get("id") not in seen_ids]
    blocked = [n["id"] for n in removed if n.get("no_compromise")]
    if blocked:
        raise ToolError(
            f"update_notes: note(s) {', '.join(blocked)} were VETOED by the "
            "user — they are binding and cannot be removed. Include them "
            "(by id) in the list.")

    # Fresh ids for additions. Unlike the Go handoff's ``assign_ids`` this
    # seeds the used set with EVERY pre-call id, so a note removed and a
    # note added in the same call never swap identities — verifier verdicts
    # and finding keys ride note ids.
    out = _notes.clean_notes(out)
    used = ({n["id"] for n in existing if n.get("id")}
            | {n["id"] for n in out if n.get("id")})
    added, i = [], 1
    for n in out:
        if not n.get("id"):
            while f"n{i}" in used:
                i += 1
            n["id"] = f"n{i}"
            used.add(n["id"])
            added.append(n["id"])
    brief["notes"] = out
    _persist(ctx, world_state)
    return {"notes": out, "added": added, "edited": edited, "kept": kept,
            "removed": [{"id": n.get("id"), "text": n.get("text", "")}
                        for n in removed]}


async def read_conversation(ctx) -> dict:
    from wbworldgen.worldgen.agent import harness as _harness

    log = None
    if ctx.build is not None:
        log = ctx.build.log
    else:
        artifact = _harness.load_build_artifact(ctx.builder, ctx.world_id)
        if artifact is not None:
            log = artifact.get("log") or []
    if log is None:
        raise ToolError(
            "No build conversation exists for this world — no agent build "
            "has recorded one.")
    exchanges = _harness.exchanges_from_log(log)
    return {"exchanges": exchanges, "count": len(exchanges)}


register_tool(ToolSpec(
    id="update_prompt",
    label="Update the brief's world prompt",
    description=(
        "Replace the brief's seed prompt — the standing description of what "
        "the world IS, read by every future generation call. " + _GATE
    ),
    invoke=update_prompt,
    mutates=True,
    params={
        "prompt": {"type": "string", "required": True,
                   "description": "The full replacement prompt text."},
    },
))

register_tool(ToolSpec(
    id="update_rules",
    label="Update the co-authored world rules",
    description=(
        "Replace the brief's co-authored world rules — the fixed input of "
        "the world_rules step and the standard every evaluation judges "
        "against. Pass the FULL list (it replaces the current rules; the "
        "result reports added/removed). If world_rules is already authored, "
        "re-run it afterwards so the rubric follows. " + _GATE
    ),
    invoke=update_rules,
    mutates=True,
    params={
        "rules": {"type": "list", "item_type": "string", "required": True,
                  "description": "The full replacement rules list."},
    },
))

register_tool(ToolSpec(
    id="update_notes",
    label="Update the brief's design notes",
    description=(
        "Replace the brief's design notes with the list given. Entry "
        "shapes: {\"id\": \"n3\"} keeps a note unchanged; {\"id\": \"n3\", "
        "\"text\"/\"subject\": ...} edits it; {\"text\": ..., \"subject\"?: "
        "...} (no id) adds one (empty subject = world-wide); a note whose "
        "id is omitted from the list is REMOVED — removals are reported "
        "loudly. Notes the user vetoed cannot be edited or removed. A "
        "user-directed edit is the user's own hand: it clears amendment/"
        "negotiation state rather than creating a compromise to review. "
        "Every note is still verified before the build can finish. " + _GATE
    ),
    invoke=update_notes,
    mutates=True,
    params={
        "notes": {"type": "list", "item_type": "object", "required": True,
                  "description": "The full replacement notes list (see "
                                 "entry shapes in the description)."},
    },
))

register_tool(ToolSpec(
    id="read_conversation",
    label="Read the build's conversation",
    description=(
        "The session's whole conversation so far, oldest first: every user "
        "message verbatim and every 'say' reply — the design conversation "
        "before the build included. Use it when a message refers to "
        "earlier exchanges (\"like we discussed...\") or when older "
        "instructions have scrolled out of your recent observations."
    ),
    invoke=read_conversation,
))
