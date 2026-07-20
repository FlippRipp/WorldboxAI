"""The verification tools: evaluate (D3) and discuss_finding (C5/N5).

``evaluate`` wraps the evaluator so the agent may check its work at any time
— the same evaluation the harness runs at the done-gate, so a clean
mid-build evaluate is an honest signal the gate will pass.

``discuss_finding`` is the builder's channel TO the note verifier: contest
or seek clarification on a note finding, bounded exchanges per finding. The
verifier may uphold, withdraw on evidence, or agree a compromise — an
amended note text persisted to the brief and reviewed by the user at the
absolute end of the build (N7)."""

from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen import notes as _notes
from wbworldgen.worldgen.agent import verifier as _verifier
from wbworldgen.worldgen.agent.evaluator import evaluate_world
from wbworldgen.worldgen.agent.registry import ToolError, ToolSpec, register_tool


async def evaluate(ctx, map_id: str = None) -> dict:
    builder = ctx.builder
    compiled = builder.services.compiled.load(ctx.world_id)
    if map_id is not None and map_id not in _ms.maps_by_id(compiled):
        raise ToolError(
            f"Unknown map '{map_id}'. This world's maps: "
            f"{', '.join(_ms.maps_by_id(compiled)) or 'none yet'}")
    world_state = builder.load_world(ctx.world_id)
    return await evaluate_world(
        builder.services, world_state, compiled, map_id=map_id,
        major_floor=type(builder).MAJOR_IMPORTANCE_FLOOR,
        builder=builder, world_id=ctx.world_id, on_event=ctx.on_event)


def _note_id_from_key(key: str):
    """``note:<id>:...`` -> the note id; None for every other key shape."""
    parts = str(key or "").split(":")
    if len(parts) >= 2 and parts[0] == "note" and parts[1]:
        return parts[1]
    return None


async def discuss_finding(ctx, key: str, message: str) -> dict:
    build = ctx.build
    if build is None:
        raise ToolError("discuss_finding is only available inside an agent "
                        "build (the dialogue state lives on the build).")
    nid = _note_id_from_key(key)
    if nid is None:
        raise ToolError(
            f"'{key}' is not a note finding. Only note:* findings can be "
            "discussed with the verifier — lint findings are mechanical "
            "facts (fix them), and critique findings are fixed or accepted.")
    message = str(message or "").strip()
    if not message:
        raise ToolError("Say something to the verifier: 'message' is empty.")

    builder = ctx.builder
    world_state = builder.load_world(ctx.world_id)
    brief = world_state.get("brief") if isinstance(world_state.get("brief"), dict) else {}
    note = next((n for n in (brief.get("notes") or [])
                 if isinstance(n, dict) and n.get("id") == nid), None)
    if note is None:
        raise ToolError(f"No note '{nid}' exists in the brief.")
    verdict = build.last_note_verdicts.get(nid)
    if verdict is None:
        raise ToolError(
            f"The verifier has no standing finding on note '{nid}' — run "
            "evaluate first; discussion answers a concrete finding.")

    rounds_budget = builder.services.resolve_int_setting(
        "world.note_discussion_rounds", _verifier.DEFAULT_DISCUSSION_ROUNDS,
        1, 10)
    dialogue = build.note_dialogues.setdefault(
        nid, {"rounds": 0, "transcript": []})
    if dialogue["rounds"] >= rounds_budget:
        raise ToolError(
            f"The discussion budget for note '{nid}' is exhausted "
            f"({rounds_budget} exchanges); the finding stands. Fix the "
            "world, or accept the finding explicitly in the done claim.")
    llm = builder.services.llm
    if llm is None or getattr(llm, "mode", "mock") == "mock":
        raise ToolError("The verifier needs a live LLM to discuss.")

    dialogue["rounds"] += 1
    compiled = builder.services.compiled.load(ctx.world_id)
    bound = next((n for n in _notes.bound_notes(world_state, compiled)
                  if n.get("id") == nid), note)
    result = await _verifier.discuss_note(
        builder.services, builder, ctx.world_id, bound, verdict,
        dialogue["transcript"], message, on_event=ctx.on_event)
    outcome = result["outcome"]
    dialogue["transcript"].append(
        {"builder": message, "verifier": result["reply"], "outcome": outcome})

    if outcome == "compromise":
        # Persist the amendment: the amended text becomes the binding note
        # (injection and verification follow it from the next compiled
        # load); the original is kept for the user's end-of-build review
        # and restored on veto (N7). update_brief is metadata-surgical (a
        # save_world here would flip the draft to finished) and invalidates
        # the compiled cache.
        if "original_text" not in note:
            note["original_text"] = note["text"]
        note["text"] = result["amended_text"]
        note["status"] = "amended"
        note["rationale"] = result["reply"]
        builder.update_brief(ctx.world_id, brief=world_state["brief"])
        build.brief = world_state["brief"]
        build.last_note_verdicts.pop(nid, None)
    elif outcome == "withdrawn":
        # Record the withdrawal on the note so later verification runs see
        # the resolution and don't re-flag capriciously (N5).
        note["verifier_context"] = (
            "A previous objection was withdrawn after builder evidence: "
            + result["reply"])
        builder.update_brief(ctx.world_id, brief=world_state["brief"])
        build.brief = world_state["brief"]
        build.last_note_verdicts.pop(nid, None)

    return {
        "note_id": nid,
        "outcome": outcome,
        "verifier_reply": result["reply"],
        **({"amended_text": result["amended_text"]}
           if outcome == "compromise" else {}),
        "rounds_used": dialogue["rounds"],
        "rounds_budget": rounds_budget,
    }


register_tool(ToolSpec(
    id="evaluate",
    label="Evaluate the build",
    description=(
        "Full verification: the deterministic lint report, (when world "
        "rules exist) an LLM critique of the built content against those "
        "rules, and (when the brief carries notes) the note verifier — a "
        "read-only agent that checks every agreed note against the world "
        "and reports per-note verdicts. Returns findings with stable keys; "
        "severity 'problem' findings block a done claim until fixed or "
        "explicitly accepted — note findings (note:*) are NEVER "
        "auto-accepted. This is the same evaluation the done-gate runs — "
        "evaluate before claiming done."
    ),
    invoke=evaluate,
    params={"map_id": {"type": "string",
                       "description": "Evaluate only one map."}},
))

register_tool(ToolSpec(
    id="discuss_finding",
    label="Discuss a note finding with the verifier",
    description=(
        "Contest, or ask for clarification on, a note finding (note:* "
        "keys) in a bounded exchange with the note verifier. The verifier "
        "answers for the world's creator: it upholds the finding (telling "
        "you exactly what the world must show), withdraws it when your "
        "evidence proves the note IS honored (point at concrete content), "
        "or — when an amendment genuinely serves the creator's intent or "
        "resolves a conflict between notes/rules — agrees a compromise: "
        "the amended note becomes binding and the creator reviews it after "
        "the build. Do not use this to negotiate away work; a vetoed "
        "compromise can never be re-amended."
    ),
    invoke=discuss_finding,
    params={
        "key": {"type": "string", "required": True,
                "description": "The note finding's key (note:<id>:-:-)."},
        "message": {"type": "string", "required": True,
                    "description": "What you say to the verifier: your "
                                   "evidence, question, or proposal."},
    },
    mutates=True,
))
