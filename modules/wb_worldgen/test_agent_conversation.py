"""Tests for the C7a mid-build conversation channel: user messages queued on
the build handle and drained at turn boundaries into recent + the persisted
log, the completion's ``say`` reply channel (U6), the brief-edit tools
(update_prompt / update_rules / update_notes — U2/U5, with the vetoed-note
lock held conservatively while the C7 fork is open) and read_conversation
(U4, including the artifact fallback the note verifier relies on), plus the
``agent/message`` route. No tokens are spent: ``harness.agent_turn`` is the
canned-sequence patch seam and the tools run against the mock-path builder.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests):
python -m pytest modules/wb_worldgen/test_agent_conversation.py
"""

import asyncio
import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import harness
from wbworldgen.worldgen.agent.registry import ToolContext, ToolError, invoke_tool
from wbworldgen.worldgen.agent.verifier import verifier_tool_ids


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_conv_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    harness._BUILDS.clear()


def _content_world(builder, world_id="built_world", brief=None):
    """A finished-looking world (rules + six named, described, connected
    towns) so done claims pass the gate; ``brief`` optionally records an
    ideation brief the way a launch would."""
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": 6 - i,
         "x": float(i), "y": 0.0, "name": f"Town {i}",
         "description": f"Flavor for Town {i}.", "region": ""}
        for i in range(6)
    ]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(5)]
    state = {
        "seed_prompt": "a quiet land",
        "steps": {
            "world_rules": {"data": {"genre": "pastoral", "tone": "calm",
                                     "custom_rules": ["Nothing hurries here."]},
                            "approved": True},
            "map_generation": {"data": {"nodes": nodes, "edges": edges},
                               "approved": True},
        },
    }
    if brief is not None:
        state["brief"] = brief
    return builder.save_world(world_id, state)


def _canned(monkeypatch, turns):
    seq = list(turns)

    async def fake_turn(services, messages):
        if not seq:
            raise AssertionError("agent_turn called beyond the canned sequence")
        item = seq.pop(0)
        if callable(item):
            result = item()
            if asyncio.iscoroutine(result):
                result = await result
            return result
        return item

    monkeypatch.setattr(harness, "agent_turn", fake_turn)


def _run_build(builder, seed="a quiet land", world_id=None):
    async def go():
        handle = harness.start_agent_build(builder, seed, world_id=world_id)
        await handle.task
        return handle

    return run(go())


def _events_of(handle, kind):
    return [e for e in handle.log if e.get("type") == kind]


# ---------------------------------------------------------------------------
# The channel: queue, drain, say
# ---------------------------------------------------------------------------

def test_messages_drain_at_the_next_turn_boundary(builder, monkeypatch):
    wid = _content_world(builder)
    seen = {}

    def post_two_then_read():
        h = harness.get_build(wid)
        h.post_message("make the towns older")
        h.post_message("and name one Quietholm")
        seen["queued"] = h.snapshot()["queued_messages"]
        return {"action": {"tool": "read_lint"}}

    def second_turn():
        h = harness.get_build(wid)
        seen["recent"] = list(h.recent)
        seen["queued_after"] = h.snapshot()["queued_messages"]
        return {"done": {"summary": "done"}}

    _canned(monkeypatch, [post_two_then_read, second_turn])
    handle = _run_build(builder, world_id=wid)
    assert handle.status == "done"

    # Queued while turn 1 was in flight, in order, with snapshot visibility
    # (the reattaching observer's source for pending bubbles).
    assert seen["queued"] == [{"id": "m1", "text": "make the towns older"},
                              {"id": "m2", "text": "and name one Quietholm"}]
    # Drained at the turn-2 boundary as verbatim plain observations (U3).
    assert [r for r in seen["recent"] if "user_message" in r] == [
        {"turn": 2, "user_message": "make the towns older"},
        {"turn": 2, "user_message": "and name one Quietholm"}]
    assert seen["queued_after"] == []
    # Persisted user_message events land before turn 2's own event.
    msgs = _events_of(handle, "user_message")
    assert [(m["id"], m["turn"]) for m in msgs] == [("m1", 2), ("m2", 2)]
    turn2 = next(e for e in _events_of(handle, "turn") if e["turn"] == 2)
    assert all(m["i"] < turn2["i"] for m in msgs)
    # Messages spend nothing themselves — only reacting turns count (C7a).
    assert handle.turns == 2 and handle.tool_calls == 1


def test_say_rides_the_turn_event_and_only_when_given(builder, monkeypatch):
    wid = _content_world(builder)
    _canned(monkeypatch, [
        {"say": "  Noted — folding that in. ", "action": {"tool": "read_lint"}},
        {"done": {"summary": "done"}},
    ])
    handle = _run_build(builder, world_id=wid)
    turns = _events_of(handle, "turn")
    assert turns[0]["say"] == "Noted — folding that in."
    assert "say" not in turns[1]


def test_messages_left_queued_at_the_end_are_recorded_unread(builder, monkeypatch):
    wid = _content_world(builder)

    def post_then_done():
        harness.get_build(wid).post_message("too late")
        return {"done": {"summary": "done"}}

    _canned(monkeypatch, [post_then_done])
    handle = _run_build(builder, world_id=wid)
    assert handle.status == "done"

    msgs = _events_of(handle, "user_message")
    assert len(msgs) == 1
    assert msgs[0]["text"] == "too late" and msgs[0]["unread"] is True
    assert "turn" not in msgs[0]
    assert msgs[0]["i"] < handle.log[-1]["i"]  # recorded before the terminal
    # It never reached the agent, and nothing stays queued.
    assert not any("user_message" in r for r in handle.recent)
    assert handle.snapshot()["queued_messages"] == []
    # The artifact keeps the honest record.
    artifact = harness.load_build_artifact(builder, wid)
    assert any(e.get("unread") for e in artifact["log"])


def test_a_message_steers_the_build_through_say_and_update_rules(builder, monkeypatch):
    wid = _content_world(builder, brief={"prompt": "a quiet land",
                                         "rules": ["Nothing hurries here."],
                                         "notes": []})
    systems, users = [], []

    async def fake_turn(services, messages):
        systems.append(messages[0]["content"])
        users.append(messages[1]["content"])
        turn = len(systems)
        if turn == 1:
            harness.get_build(wid).post_message(
                "Add a rule: the rivers sing at dusk.")
            return {"action": {"tool": "read_world"}}
        if turn == 2:
            return {"say": "Adding it now.",
                    "action": {"tool": "update_rules",
                               "args": {"rules": ["Nothing hurries here.",
                                                  "The rivers sing at dusk."]}}}
        return {"done": {"summary": "done"}}

    monkeypatch.setattr(harness, "agent_turn", fake_turn)
    handle = _run_build(builder, world_id=wid)
    assert handle.status == "done"

    # The channel guidance is standing system-prompt text.
    assert "user_message" in systems[0] and "read_conversation" in systems[0]
    # The message reached turn 2 verbatim in the prompt-side recent window.
    assert '"user_message": "Add a rule: the rivers sing at dusk."' in users[1]
    # The say rode turn 2's event (the observer's chat bubble).
    turn2 = next(e for e in _events_of(handle, "turn") if e["turn"] == 2)
    assert turn2["say"] == "Adding it now."
    # The rule became contract: disk, handle mirror, and turn 3's system
    # prompt (the brief is re-read from disk every turn, D4).
    assert builder.load_world(wid)["brief"]["rules"] == [
        "Nothing hurries here.", "The rivers sing at dusk."]
    assert handle.brief["rules"][-1] == "The rivers sing at dusk."
    assert "- The rivers sing at dusk." not in systems[1]
    assert "- The rivers sing at dusk." in systems[2]
    # The observation reported the diff and the world_rules re-run pointer.
    obs = _events_of(handle, "observation")[1]
    assert obs["ok"] is True
    assert obs["result"]["added"] == ["The rivers sing at dusk."]
    assert "re-run" in obs["result"]["note"]


def test_update_prompt_reaches_the_next_turn_prompt(builder, monkeypatch):
    wid = _content_world(builder, brief={"prompt": "a quiet land",
                                         "rules": [], "notes": []})
    systems = []

    async def fake_turn(services, messages):
        systems.append(messages[0]["content"])
        if len(systems) == 1:
            return {"action": {"tool": "update_prompt",
                               "args": {"prompt": "a land of singing rivers"}}}
        return {"done": {"summary": "done"}}

    monkeypatch.setattr(harness, "agent_turn", fake_turn)
    handle = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    assert "## The brief\na quiet land" in systems[0]
    assert "## The brief\na land of singing rivers" in systems[1]
    assert handle.seed_prompt == "a land of singing rivers"
    assert builder.load_world(wid)["seed_prompt"] == "a land of singing rivers"


# ---------------------------------------------------------------------------
# The brief-edit tools (U2/U5)
# ---------------------------------------------------------------------------

def test_update_prompt_moves_brief_and_seed_together(builder):
    wid = _content_world(builder, brief={"prompt": "a quiet land",
                                         "rules": [], "notes": []})
    ctx = ToolContext(builder=builder, world_id=wid)
    result = run(invoke_tool(ctx, "update_prompt", {"prompt": "a loud land"}))
    assert result["previous"] == "a quiet land"
    state = builder.load_world(wid)
    assert state["brief"]["prompt"] == "a loud land"
    assert state["seed_prompt"] == "a loud land"
    with pytest.raises(ToolError, match="empty"):
        run(invoke_tool(ctx, "update_prompt", {"prompt": "   "}))


def test_brief_tools_refuse_without_a_brief(builder):
    wid = _content_world(builder)  # no brief recorded
    ctx = ToolContext(builder=builder, world_id=wid)
    for tool, args in [("update_prompt", {"prompt": "x"}),
                       ("update_rules", {"rules": []}),
                       ("update_notes", {"notes": []})]:
        with pytest.raises(ToolError, match="no ideation brief"):
            run(invoke_tool(ctx, tool, args))


def test_update_rules_reports_the_diff(builder):
    wid = _content_world(builder, brief={"prompt": "p",
                                         "rules": ["Old rule.", "Kept rule."],
                                         "notes": []})
    ctx = ToolContext(builder=builder, world_id=wid)
    result = run(invoke_tool(ctx, "update_rules",
                             {"rules": ["Kept rule.", " New rule. ", ""]}))
    assert result["rules"] == ["Kept rule.", "New rule."]
    assert result["added"] == ["New rule."]
    assert result["removed"] == ["Old rule."]
    assert "re-run" in result["note"]  # world_rules is authored here

    # No authored world_rules -> nothing to re-run, no note.
    bare = builder.save_world("bare", {
        "seed_prompt": "p", "steps": {},
        "brief": {"prompt": "p", "rules": [], "notes": []}})
    result = run(invoke_tool(ToolContext(builder=builder, world_id=bare),
                             "update_rules", {"rules": ["Only rule."]}))
    assert result["added"] == ["Only rule."] and "note" not in result


def _noted_brief():
    return {"prompt": "p", "rules": [], "notes": [
        {"id": "n1", "text": "The wells are deep.", "subject": "",
         "verifier_context": "A previous objection was withdrawn."},
        {"id": "n2", "text": "The mill burned twice.", "subject": "the mill",
         "status": "amended", "original_text": "The mill burned once.",
         "rationale": "compromise"},
        {"id": "n3", "text": "No horses anywhere.", "subject": "",
         "no_compromise": True},
        {"id": "n4", "text": "Doomed note.", "subject": "somewhere"},
    ]}


def test_update_notes_edits_add_remove_and_clear_negotiation_state(builder):
    wid = _content_world(builder, brief=_noted_brief())
    ctx = ToolContext(builder=builder, world_id=wid)
    result = run(invoke_tool(ctx, "update_notes", {"notes": [
        {"id": "n1"},                                     # keep, id only
        {"id": "n2", "text": "The mill burned thrice."},  # user-directed edit
        {"id": "n3"},                                     # vetoed, kept as-is
        {"text": "A new fact.", "subject": "the mill"},   # addition
    ]}))
    assert result["kept"] == ["n1", "n3"]
    assert result["edited"] == ["n2"]
    assert result["removed"] == [{"id": "n4", "text": "Doomed note."}]
    # The removed id is not reused within the call: the addition gets a
    # fresh id (verdicts and finding keys ride note ids).
    assert result["added"] == ["n5"]

    notes = {n["id"]: n for n in builder.load_world(wid)["brief"]["notes"]}
    assert set(notes) == {"n1", "n2", "n3", "n5"}
    # U5: the user's edit is a hand edit — negotiation state cleared, no
    # amendment left for the N7 review...
    assert notes["n2"]["text"] == "The mill burned thrice."
    for key in ("status", "original_text", "rationale"):
        assert key not in notes["n2"]
    # ...while untouched notes keep their state.
    assert notes["n1"]["verifier_context"] == "A previous objection was withdrawn."
    assert notes["n3"]["no_compromise"] is True
    assert notes["n5"] == {"id": "n5", "text": "A new fact.",
                           "subject": "the mill"}


def test_update_notes_refusals(builder):
    wid = _content_world(builder, brief=_noted_brief())
    ctx = ToolContext(builder=builder, world_id=wid)
    keep_all = [{"id": nid} for nid in ("n1", "n2", "n3", "n4")]
    cases = [
        ([{"id": "n3", "text": "Some horses."}] + keep_all[:2],
         "VETOED"),                                        # edit a vetoed note
        ([{"id": "n1"}, {"id": "n2"}, {"id": "n4"}],
         "cannot be removed"),                             # drop a vetoed note
        (keep_all + [{"id": "n9"}], "no note 'n9'"),
        (keep_all + [{"id": "n1", "text": "dup"}], "appears twice"),
        (keep_all + [{"text": "   "}], "non-empty 'text'"),
        ([{"id": "n1", "text": "  "}] + keep_all[1:], "empty text"),
        ([{"id": "n1", "status": "amended"}] + keep_all[1:], "unknown key"),
    ]
    for notes_arg, match in cases:
        with pytest.raises(ToolError, match=match):
            run(invoke_tool(ctx, "update_notes", {"notes": notes_arg}))
    # No refused call persisted anything.
    assert [n["id"] for n in builder.load_world(wid)["brief"]["notes"]] == [
        "n1", "n2", "n3", "n4"]


# ---------------------------------------------------------------------------
# read_conversation (U4)
# ---------------------------------------------------------------------------

def test_read_conversation_reads_live_handle_and_artifact(builder):
    wid = _content_world(builder)
    handle = harness.AgentBuild(wid, "a quiet land", builder)
    handle.log = [
        {"type": "turn", "turn": 1, "thought": "x", "todo": [], "i": 0},
        {"type": "user_message", "id": "m1", "text": "hello there",
         "turn": 2, "i": 1},
        {"type": "turn", "turn": 2, "thought": "y",
         "say": "Hello! Building on.", "todo": [], "i": 2},
        {"type": "user_message", "id": "m2", "text": "unheard",
         "unread": True, "i": 3},
    ]
    live = run(invoke_tool(
        ToolContext(builder=builder, world_id=wid, build=handle),
        "read_conversation", {}))
    assert live["exchanges"] == [
        {"who": "user", "turn": 2, "text": "hello there"},
        {"who": "agent", "turn": 2, "text": "Hello! Building on."},
        {"who": "user", "turn": None, "text": "unheard", "unread": True},
    ]
    assert live["count"] == 3

    # Artifact fallback — how the note verifier reads it (its ToolContext
    # carries no build handle; N4's carve hands it the tool).
    harness._persist_artifact(handle)
    off = run(invoke_tool(ToolContext(builder=builder, world_id=wid),
                          "read_conversation", {}))
    assert off == live


def test_read_conversation_without_any_build_is_loud(builder):
    wid = _content_world(builder)
    with pytest.raises(ToolError, match="No build conversation"):
        run(invoke_tool(ToolContext(builder=builder, world_id=wid),
                        "read_conversation", {}))


def test_the_verifier_inherits_read_conversation_but_no_brief_tools():
    ids = verifier_tool_ids()
    assert "read_conversation" in ids
    for tool in ("update_prompt", "update_rules", "update_notes"):
        assert tool not in ids


# ---------------------------------------------------------------------------
# The message route
# ---------------------------------------------------------------------------

def test_message_route_queues_and_refuses(builder, monkeypatch):
    import routes as world_routes
    from fastapi import HTTPException

    monkeypatch.setattr(world_routes, "world_builder", builder)
    wid = _content_world(builder)
    _canned(monkeypatch, [{"done": {"summary": "done"}}])

    async def go():
        handle = harness.start_agent_build(builder, "a quiet land",
                                           world_id=wid)
        # The build task hasn't been scheduled yet (nothing here yields to
        # the loop), so queueing and status are deterministic.
        resp = await world_routes.agent_build_message(
            wid, world_routes.AgentMessageRequest(text="  hello  "))
        assert resp == {"world_id": wid, "queued": True,
                        "id": "m1", "position": 1}
        status = await world_routes.agent_build_status(wid)
        assert status["queued_messages"] == [{"id": "m1", "text": "hello"}]

        with pytest.raises(HTTPException) as e:
            await world_routes.agent_build_message(
                wid, world_routes.AgentMessageRequest(text="   "))
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e:
            await world_routes.agent_build_message(
                "never_built", world_routes.AgentMessageRequest(text="x"))
        assert e.value.status_code == 404

        await handle.task
        assert handle.status == "done"
        with pytest.raises(HTTPException) as e:
            await world_routes.agent_build_message(
                wid, world_routes.AgentMessageRequest(text="x"))
        assert e.value.status_code == 409

    run(go())
