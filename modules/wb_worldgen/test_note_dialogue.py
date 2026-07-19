"""Tests for the C5 discuss_finding dialogue (N5): tool preconditions, the
three outcomes (upheld / withdrawn / compromise), amendment persistence and
the no-re-amendment rule for vetoed notes, round budgets, and the verifier's
in-exchange read access.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_note_dialogue.py
"""

import asyncio
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import harness as harness_mod
from wbworldgen.worldgen.agent import verifier as verifier_mod
from wbworldgen.worldgen.agent.registry import (
    ToolContext,
    ToolError,
    describe_tools,
    invoke_tool,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_dialog_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = register_default_steps(WorldBuilder(worlds_dir=tmpdir))
    wb._llm_service = types.SimpleNamespace(
        mode="live", storyteller_model="smart-slot",
        module_fast_model="fast-slot", reader_model="reader-slot")
    return wb


def _world(builder, note_extra=None, world_id="dialog_world"):
    note = {"id": "n2", "text": "The town has three lighthouses.",
            "subject": "Harbor Town"}
    note.update(note_extra or {})
    nodes = [
        {"id": "a1", "name": "Harbor Town", "type": "town", "importance": 8,
         "x": 0.0, "y": 0.0, "description": "A busy harbor."},
        {"id": "a2", "name": "Salt Keep", "type": "keep", "importance": 7,
         "x": 1.0, "y": 0.0, "description": "A fortress."},
    ]
    return builder.save_world(world_id, {
        "seed_prompt": "test world",
        "brief": {"prompt": "test world", "rules": [], "notes": [note]},
        "steps": {"map_generation": {
            "data": {"nodes": nodes,
                     "edges": [{"from": "a1", "to": "a2"}]},
            "approved": True}},
    })


def _ctx(builder, wid, with_build=True):
    build = None
    if with_build:
        build = harness_mod.AgentBuild(wid, "test world", builder)
        build.last_note_verdicts["n2"] = {
            "id": "n2", "verdict": "not_honored",
            "evidence": "no lighthouse in any description",
            "suggestion": "rework Harbor Town", "map_id": "root",
            "subject": "Harbor Town", "text": "The town has three lighthouses."}
    return ToolContext(builder=builder, world_id=wid, build=build), build


def _canned_discuss(monkeypatch, replies):
    calls = []

    async def fake_turn(services, messages):
        calls.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(verifier_mod, "discuss_turn", fake_turn)
    return calls


# --- preconditions -----------------------------------------------------------

def test_discuss_finding_is_registered_and_excluded_from_verifier():
    ids = {t["id"] for t in describe_tools()}
    assert "discuss_finding" in ids
    assert "discuss_finding" not in set(verifier_mod.verifier_tool_ids())


def test_discuss_requires_build_note_key_and_verdict(builder):
    wid = _world(builder)
    ctx_nobuild, _ = _ctx(builder, wid, with_build=False)
    with pytest.raises(ToolError, match="inside an agent build"):
        run(invoke_tool(ctx_nobuild, "discuss_finding",
                        {"key": "note:n2:-:-", "message": "hi"}))
    ctx, _ = _ctx(builder, wid)
    with pytest.raises(ToolError, match="not a note finding"):
        run(invoke_tool(ctx, "discuss_finding",
                        {"key": "lint:orphan_node:root:a1", "message": "hi"}))
    with pytest.raises(ToolError, match="No note 'n9'"):
        run(invoke_tool(ctx, "discuss_finding",
                        {"key": "note:n9:-:-", "message": "hi"}))
    ctx.build.last_note_verdicts.clear()
    with pytest.raises(ToolError, match="no standing finding"):
        run(invoke_tool(ctx, "discuss_finding",
                        {"key": "note:n2:-:-", "message": "hi"}))


# --- outcomes ----------------------------------------------------------------

def test_upheld_keeps_everything_and_records_transcript(builder, monkeypatch):
    wid = _world(builder)
    ctx, build = _ctx(builder, wid)
    calls = _canned_discuss(monkeypatch, [
        {"reply": "Show me a lighthouse in any description.",
         "outcome": "upheld"},
    ])
    result = run(invoke_tool(ctx, "discuss_finding", {
        "key": "note:n2:-:-",
        "message": "Two lighthouses are implied by the harbor."}))
    assert result["outcome"] == "upheld"
    assert result["rounds_used"] == 1
    assert build.note_dialogues["n2"]["transcript"][0]["outcome"] == "upheld"
    # The finding stays standing and the note is untouched.
    assert "n2" in build.last_note_verdicts
    note = builder.load_world(wid)["brief"]["notes"][0]
    assert note["text"] == "The town has three lighthouses."
    # The verifier saw the builder's message and its own original evidence.
    system = calls[0][0]["content"]
    assert "no lighthouse in any description" in system
    assert "Two lighthouses are implied" in calls[0][1]["content"]


def test_withdrawn_records_context_on_the_note(builder, monkeypatch):
    wid = _world(builder)
    ctx, build = _ctx(builder, wid)
    _canned_discuss(monkeypatch, [
        {"reply": "You are right — the keep's description names the beacon.",
         "outcome": "withdrawn"},
    ])
    result = run(invoke_tool(ctx, "discuss_finding", {
        "key": "note:n2:-:-", "message": "See Salt Keep's beacon line."}))
    assert result["outcome"] == "withdrawn"
    note = builder.load_world(wid)["brief"]["notes"][0]
    assert "withdrawn after builder evidence" in note["verifier_context"]
    assert "n2" not in build.last_note_verdicts
    # The next verification checklist carries the resolution.
    compiled = builder.services.compiled.load(wid)
    from wbworldgen.worldgen.notes import bound_notes
    checklist = verifier_mod._checklist(bound_notes({"brief":
        builder.load_world(wid)["brief"]}, compiled))
    assert "earlier discussion" in checklist


def test_compromise_amends_the_note_pending_review(builder, monkeypatch):
    wid = _world(builder)
    ctx, build = _ctx(builder, wid)
    _canned_discuss(monkeypatch, [
        {"reply": "One grand lighthouse serves the intent.",
         "outcome": "compromise",
         "amended_text": "The town has one grand lighthouse."},
    ])
    result = run(invoke_tool(ctx, "discuss_finding", {
        "key": "note:n2:-:-",
        "message": "Three lighthouses crowd this tiny map — one iconic one?"}))
    assert result["outcome"] == "compromise"
    assert result["amended_text"] == "The town has one grand lighthouse."
    note = builder.load_world(wid)["brief"]["notes"][0]
    assert note["text"] == "The town has one grand lighthouse."
    assert note["original_text"] == "The town has three lighthouses."
    assert note["status"] == "amended"
    assert note["rationale"] == "One grand lighthouse serves the intent."
    # The handle mirror and the compiled world follow the amendment.
    assert build.brief["notes"][0]["text"] == "The town has one grand lighthouse."
    compiled = builder.services.compiled.load(wid)
    assert compiled["brief"]["notes"][0]["text"] == \
        "The town has one grand lighthouse."


def test_second_compromise_keeps_first_original(builder, monkeypatch):
    wid = _world(builder, note_extra={
        "status": "amended", "original_text": "The town has three lighthouses.",
        "text": "The town has two lighthouses."})
    ctx, build = _ctx(builder, wid)
    _canned_discuss(monkeypatch, [
        {"reply": "Down to one.", "outcome": "compromise",
         "amended_text": "The town has one lighthouse."},
    ])
    run(invoke_tool(ctx, "discuss_finding",
                    {"key": "note:n2:-:-", "message": "Still too many."}))
    note = builder.load_world(wid)["brief"]["notes"][0]
    assert note["text"] == "The town has one lighthouse."
    assert note["original_text"] == "The town has three lighthouses."


def test_vetoed_note_cannot_be_compromised(builder, monkeypatch):
    wid = _world(builder, note_extra={"no_compromise": True})
    ctx, build = _ctx(builder, wid)
    calls = _canned_discuss(monkeypatch, [
        {"reply": "Let's soften it.", "outcome": "compromise",
         "amended_text": "One lighthouse."},
        {"reply": "The veto stands; build the three lighthouses.",
         "outcome": "upheld"},
    ])
    result = run(invoke_tool(ctx, "discuss_finding", {
        "key": "note:n2:-:-", "message": "Can we soften this?"}))
    assert result["outcome"] == "upheld"
    note = builder.load_world(wid)["brief"]["notes"][0]
    assert note["text"] == "The town has three lighthouses."
    # The compromise attempt bounced inside the exchange loop.
    assert "compromise is not possible" in calls[1][1]["content"]
    # And the vetoed stance is in the system prompt.
    assert "VETOED" in calls[0][0]["content"]


def test_round_budget_exhausts_loudly(builder, monkeypatch):
    wid = _world(builder)
    builder.set_settings(types.SimpleNamespace(
        get=lambda k: {"world.note_discussion_rounds": 2}.get(k)))
    ctx, build = _ctx(builder, wid)
    _canned_discuss(monkeypatch, [
        {"reply": "No.", "outcome": "upheld"},
        {"reply": "Still no.", "outcome": "upheld"},
    ])
    for _ in range(2):
        run(invoke_tool(ctx, "discuss_finding",
                        {"key": "note:n2:-:-", "message": "please"}))
    with pytest.raises(ToolError, match="budget for note 'n2' is exhausted"):
        run(invoke_tool(ctx, "discuss_finding",
                        {"key": "note:n2:-:-", "message": "please again"}))


def test_verifier_may_read_during_the_exchange(builder, monkeypatch):
    wid = _world(builder)
    ctx, build = _ctx(builder, wid)
    calls = _canned_discuss(monkeypatch, [
        {"action": {"tool": "read_node", "args": {"node_id": "a2"}}},
        {"reply": "Checked the keep — no beacon; the finding stands.",
         "outcome": "upheld"},
    ])
    result = run(invoke_tool(ctx, "discuss_finding", {
        "key": "note:n2:-:-", "message": "Salt Keep has a beacon."}))
    assert result["outcome"] == "upheld"
    # The read's result reached the verifier's next turn.
    assert "Salt Keep" in calls[1][1]["content"]
