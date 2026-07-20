"""Tests for C7b — the merged conversational front door: the session's chat
phase (message-paced mini-loops on the restricted catalog, the U6
``{say, action?, ready?}`` protocol, loud budget exhaustion, LLM-failure
survival), the Go flip into the build (N1 note ids, the phase event), chat
resumability from the artifact, the session routes (chat start / go /
brief hand-edits / message resurrection), and the metadata-surgical
``update_brief`` every brief-edit surface now writes through.

No tokens are spent: ``harness.chat_turn`` (and ``agent_turn`` where a
build phase runs) are the canned-sequence patch seams.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests):
python -m pytest modules/wb_worldgen/test_agent_chat.py
"""

import asyncio
import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import harness


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_chat_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    harness._BUILDS.clear()


def _canned_chat(monkeypatch, turns):
    """Script chat_turn with a list of completions/callables — the
    agent_turn canned-sequence pattern, for the second agent."""
    seq = list(turns)

    async def fake_chat(services, messages):
        if not seq:
            raise AssertionError("chat_turn called beyond the canned sequence")
        item = seq.pop(0)
        if callable(item):
            result = item(messages)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        return item

    monkeypatch.setattr(harness, "chat_turn", fake_chat)
    return seq


async def _until(pred, ticks=2000):
    """Yield to the session task until ``pred()`` holds (the canned turns
    never block, so a bounded number of loop passes always suffices)."""
    for _ in range(ticks):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("session never reached the expected state")


async def _end(handle):
    """Cancel a session and wait the task out (test teardown)."""
    harness.cancel_build(handle.world_id)
    try:
        await asyncio.wait_for(handle.task, timeout=5)
    except (asyncio.CancelledError, Exception):
        pass


def _events_of(handle, kind):
    return [e for e in handle.log if e.get("type") == kind]


# ---------------------------------------------------------------------------
# The chat phase: session start, mini-loop, protocol
# ---------------------------------------------------------------------------

def test_first_message_lazily_creates_a_chat_draft(builder, monkeypatch):
    _canned_chat(monkeypatch, [
        {"say": "A tidal world, then — dead calm or violent?", "ready": False},
    ])

    async def go():
        handle = harness.start_chat_session(
            builder, "  something with tides  ", prompt="a tidal world")
        await _until(lambda: _events_of(handle, "turn"))
        return handle

    handle = run(go())
    # The draft world exists from the first message (fork 2: lazy-create),
    # in progress, phase-marked, with the typed prompt as the brief's start.
    state = builder.load_world(handle.world_id)
    assert state["complete"] is False
    assert state["agent_phase"] == "chat"
    assert state["brief"] == {"prompt": "a tidal world", "rules": [],
                              "notes": []}
    listed = {w["id"]: w for w in builder.list_worlds()}[handle.world_id]
    assert listed["in_progress"] is True and listed["agent_phase"] == "chat"
    assert harness.has_build_artifact(builder, handle.world_id)

    # The message drained verbatim into the log; the reply rode a
    # phase-tagged turn event; no build budget was touched.
    msgs = _events_of(handle, "user_message")
    assert [(m["id"], m["text"], m["phase"]) for m in msgs] == [
        ("m1", "something with tides", "chat")]
    turn = _events_of(handle, "turn")[0]
    assert turn["phase"] == "chat" and turn["chat_turn"] == 1
    assert turn["say"].startswith("A tidal world")
    assert turn["ready"] is False
    assert handle.turns == 0 and handle.tool_calls == 0
    snap = handle.snapshot()
    assert snap["phase"] == "chat" and snap["chat_turns"] == 1
    assert snap["status"] == "running"
    run(_end(handle))


def test_chat_mini_loop_edits_drafts_then_replies(builder, monkeypatch):
    payloads = []

    def first(messages):
        payloads.append(messages[1]["content"])
        return {"action": {"tool": "update_rules",
                           "args": {"rules": ["The tide is a living god."]}}}

    def second(messages):
        payloads.append(messages[1]["content"])
        return {"say": "Recorded the tide rule.", "ready": True}

    _canned_chat(monkeypatch, [first, second])

    async def go():
        handle = harness.start_chat_session(builder, "the tide is alive",
                                            prompt="a drowned world")
        await _until(lambda: handle.ready)
        return handle

    handle = run(go())
    # The action executed against the brief (server truth on disk)...
    assert builder.load_world(handle.world_id)["brief"]["rules"] == [
        "The tide is a living god."]
    # ...its observation fed the SECOND completion of the same exchange...
    assert '"observations"' not in payloads[0]
    assert '"The tide is a living god."' in payloads[1]
    # ...and the transcript so far rides every payload (P9: whole).
    assert "the tide is alive" in payloads[0]
    # Events: action + ok observation, both phase-tagged; ready on turn 2.
    action = _events_of(handle, "action")[0]
    assert action["tool"] == "update_rules" and action["phase"] == "chat"
    obs = _events_of(handle, "observation")[0]
    assert obs["ok"] is True and obs["phase"] == "chat"
    turns = _events_of(handle, "turn")
    assert turns[-1]["ready"] is True and turns[-1]["say"] == "Recorded the tide rule."
    # The chat phase never checkpoints (v2c stays build-scoped).
    assert builder.services.enrichment_store.list_checkpoints(handle.world_id) == []
    run(_end(handle))


def test_chat_catalog_is_restricted_and_protocol_errors_feed_back(builder, monkeypatch):
    def try_build(messages):
        return {"action": {"tool": "run_step",
                           "args": {"step_id": "world_rules"}}}

    def empty(messages):
        return {}

    def recover(messages):
        assert "not available in the design conversation" in messages[1]["content"]
        assert "does nothing" in messages[1]["content"]
        return {"say": "Sticking to the brief tools."}

    _canned_chat(monkeypatch, [try_build, empty, recover])

    async def go():
        handle = harness.start_chat_session(builder, "hello", prompt="p")
        await _until(lambda: len(_events_of(handle, "turn")) == 3)
        return handle

    handle = run(go())
    obs = _events_of(handle, "observation")
    # The build tool was refused by the harness (the registry knows it;
    # this agent doesn't get it), the empty completion was a protocol
    # error, and both fed back into the exchange.
    assert obs[0]["ok"] is False and "run_step" in obs[0]["error"]
    assert "unlock when the player starts the build" in obs[0]["error"]
    assert obs[1]["ok"] is False and "does nothing" in obs[1]["protocol_error"]
    # No build tool ran: the world still has no steps.
    assert builder.load_world(handle.world_id)["steps"] == {}
    run(_end(handle))


def test_chat_budget_exhaustion_is_loud_and_survivable(builder, monkeypatch):
    builder.set_settings(__import__("types").SimpleNamespace(
        get=lambda k: {"world.agent_chat_turns": 2}.get(k)))
    _canned_chat(monkeypatch, [
        {"action": {"tool": "read_conversation"}},
        {"action": {"tool": "read_conversation"}},
        # exhausted here — the next message opens a fresh mini-loop
        {"say": "Still here."},
    ])

    async def go():
        handle = harness.start_chat_session(builder, "hi", prompt="p")
        await _until(lambda: any(
            "per-message budget" in str(e.get("error"))
            for e in _events_of(handle, "observation")))
        assert handle.status == "running"
        handle.post_message("are you alive?")
        await _until(lambda: any(e.get("say") == "Still here."
                                 for e in _events_of(handle, "turn")))
        return handle

    handle = run(go())
    assert handle.status == "running"
    run(_end(handle))


def test_chat_llm_failures_surface_but_do_not_kill_the_session(builder, monkeypatch):
    def boom(messages):
        raise RuntimeError("model fell over")

    _canned_chat(monkeypatch, [boom, boom, boom, {"say": "Back now."}])

    async def go():
        handle = harness.start_chat_session(builder, "hi", prompt="p")
        await _until(lambda: any(
            "could not answer" in str(e.get("error"))
            for e in _events_of(handle, "observation")))
        assert handle.status == "running"  # a chat outlives a failed reply
        handle.post_message("retry?")
        await _until(lambda: any(e.get("say") == "Back now."
                                 for e in _events_of(handle, "turn")))
        return handle

    handle = run(go())
    run(_end(handle))


# ---------------------------------------------------------------------------
# Go: the flip into the build phase
# ---------------------------------------------------------------------------

def test_go_flips_the_session_assigns_note_ids_and_starts_the_build(
        builder, monkeypatch):
    _canned_chat(monkeypatch, [
        {"say": "Ready when you are.", "ready": True},
    ])

    async def _no_build(handle):
        handle.status = "cancelled"

    monkeypatch.setattr(harness, "_run_build", _no_build)

    async def go():
        handle = harness.start_chat_session(builder, "two moons", prompt="p")
        await _until(lambda: handle.ready)
        # Hand edits before Go leave id-less notes (the PUT surface).
        builder.update_brief(handle.world_id, brief={
            "prompt": "a moonlit world", "rules": ["Moons rule the tides."],
            "notes": [{"text": "Three moons.", "subject": "Kharos"},
                      {"text": "No iron anywhere.", "subject": ""}]})
        handle.request_go()
        await asyncio.wait_for(handle.task, timeout=5)
        return handle

    handle = run(go())
    # N1 at Go: stable ids; the phase flipped in state, metadata and event.
    state = builder.load_world(handle.world_id)
    assert [n["id"] for n in state["brief"]["notes"]] == ["n1", "n2"]
    assert state["agent_phase"] == "build"
    assert handle.phase == "build"
    assert handle.seed_prompt == "a moonlit world"
    assert state["seed_prompt"] == "a moonlit world"
    phases = [e for e in handle.log if e.get("type") == "phase"]
    assert [p["phase"] for p in phases] == ["build"]
    # The terminal event names the phase it ended in.
    assert handle.log[-1]["type"] == "done"
    assert handle.log[-1]["phase"] == "build"


def test_go_outranks_queued_messages_which_ride_into_the_build(
        builder, monkeypatch):
    _canned_chat(monkeypatch, [{"say": "hi", "ready": False}])
    seen = {}

    async def fake_agent_turn(services, messages):
        h = harness._BUILDS[seen["wid"]]
        seen["recent"] = list(h.recent)
        return {"done": {"summary": "x"}}  # rejected (empty world) is fine

    async def _one_turn_build(handle):
        # Run exactly one real turn boundary then stop: enough to see the
        # C7a drain deliver the message the user sent before Go landed.
        handle.turns += 1
        await harness._drain_messages(handle)
        handle.status = "cancelled"

    monkeypatch.setattr(harness, "_run_build", _one_turn_build)

    async def go():
        handle = harness.start_chat_session(builder, "hello", prompt="p")
        seen["wid"] = handle.world_id
        await _until(lambda: _events_of(handle, "turn"))
        # Queue a message and Go before the loop wakes: Go wins, the
        # message drains at the build's first boundary instead.
        handle.post_message("and make it cold")
        handle.request_go()
        await asyncio.wait_for(handle.task, timeout=5)
        return handle

    handle = run(go())
    drained = [m for m in _events_of(handle, "user_message")
               if m["text"] == "and make it cold"]
    assert len(drained) == 1
    assert "phase" not in drained[0]          # a build-phase drain (C7a)
    assert drained[0]["turn"] == 1
    assert not drained[0].get("unread")


# ---------------------------------------------------------------------------
# Resumability (the artifact is the transcript)
# ---------------------------------------------------------------------------

def test_chat_session_resumes_from_artifact_after_task_death(builder, monkeypatch):
    payloads = []

    def reply1(messages):
        return {"say": "Salt and bone, noted."}

    def reply2(messages):
        payloads.append(messages[1]["content"])
        return {"say": "As we said: salt, bone, and now bells."}

    _canned_chat(monkeypatch, [reply1, reply2])

    async def go():
        handle = harness.start_chat_session(builder, "a world of salt and bone",
                                            prompt="p")
        wid = handle.world_id
        await _until(lambda: _events_of(handle, "turn"))
        # The backend dies: the task is cancelled mid-wait; the artifact
        # keeps status=running/phase=chat (no false terminal event).
        handle.task.cancel()
        try:
            await handle.task
        except asyncio.CancelledError:
            pass
        harness._BUILDS.clear()
        artifact = harness.load_build_artifact(builder, wid)
        assert artifact["phase"] == "chat" and artifact["status"] == "running"
        assert not any(e.get("type") == "done" for e in artifact["log"])

        # The next message revives the session from the artifact.
        resumed = harness.resume_chat_session(builder, wid)
        assert resumed is not handle
        assert resumed.log and resumed.chat_turns == handle.chat_turns
        queued = resumed.post_message("add bells")
        assert queued["id"] == "m2"  # the counter continues, no id reuse
        await _until(lambda: any("bells" in str(e.get("say"))
                                 for e in _events_of(resumed, "turn")))
        return resumed

    resumed = run(go())
    # The revived agent saw the whole earlier conversation (U4/P9).
    assert "a world of salt and bone" in payloads[0]
    assert "Salt and bone, noted." in payloads[0]
    run(_end(resumed))


def test_resume_requeues_messages_stranded_in_the_snapshot(builder, monkeypatch):
    replies = _canned_chat(monkeypatch, [])

    async def go():
        handle = harness.start_chat_session(builder, "first", prompt="p")
        wid = handle.world_id
        # Kill the task before it ever drains the queue: the snapshot (and
        # so the artifact) still carries the message.
        handle.task.cancel()
        try:
            await handle.task
        except asyncio.CancelledError:
            pass
        harness._BUILDS.clear()
        assert harness.load_build_artifact(builder, wid)["queued_messages"] == [
            {"id": "m1", "text": "first"}]

        replies.append({"say": "Caught up."})
        resumed = harness.resume_chat_session(builder, wid)
        assert resumed.snapshot()["queued_messages"] == [
            {"id": "m1", "text": "first"}]
        await _until(lambda: any(e.get("say") == "Caught up."
                                 for e in _events_of(resumed, "turn")))
        assert [m["id"] for m in _events_of(resumed, "user_message")] == ["m1"]
        await _end(resumed)

    run(go())


def test_resume_refuses_builds_and_missing_sessions(builder):
    wid = builder.save_draft("built_once", {
        "seed_prompt": "p", "steps": {},
        "brief": {"prompt": "p", "rules": [], "notes": []}})
    harness._persist_artifact(harness.AgentBuild(wid, "p", builder,
                                                 phase="build"))

    async def go():
        with pytest.raises(ValueError, match="No agent session"):
            harness.resume_chat_session(builder, "never_existed")
        with pytest.raises(ValueError, match="already went to build"):
            harness.resume_chat_session(builder, wid)

    run(go())


# ---------------------------------------------------------------------------
# Routes: chat start, go, brief hand-edits, message resurrection
# ---------------------------------------------------------------------------

def _routes(monkeypatch, builder):
    import routes as world_routes
    monkeypatch.setattr(world_routes, "world_builder", builder)
    return world_routes


def test_chat_route_starts_a_session_and_validates(builder, monkeypatch):
    from fastapi import HTTPException
    world_routes = _routes(monkeypatch, builder)
    _canned_chat(monkeypatch, [{"say": "hello!"}])

    async def go():
        with pytest.raises(HTTPException) as e:
            await world_routes.agent_chat_start(
                world_routes.AgentChatRequest(text="   "))
        assert e.value.status_code == 400

        resp = await world_routes.agent_chat_start(
            world_routes.AgentChatRequest(text="drowned gods",
                                          prompt="a drowned world"))
        assert resp["phase"] == "chat" and resp["status"] == "running"
        handle = harness.get_build(resp["world_id"])
        await _until(lambda: _events_of(handle, "turn"))
        await _end(handle)

    run(go())


def test_go_route_validates_prompt_and_phase(builder, monkeypatch):
    from fastapi import HTTPException
    world_routes = _routes(monkeypatch, builder)
    _canned_chat(monkeypatch, [{"say": "hi"}])

    async def _no_build(handle):
        await asyncio.sleep(0)
        handle.status = "cancelled"

    monkeypatch.setattr(harness, "_run_build", _no_build)

    async def go():
        with pytest.raises(HTTPException) as e:
            await world_routes.agent_session_go("nope")
        assert e.value.status_code == 404

        # A session whose brief has no prompt yet cannot build.
        resp = await world_routes.agent_chat_start(
            world_routes.AgentChatRequest(text="just thinking"))
        wid = resp["world_id"]
        handle = harness.get_build(wid)
        await _until(lambda: _events_of(handle, "turn"))
        with pytest.raises(HTTPException) as e:
            await world_routes.agent_session_go(wid)
        assert e.value.status_code == 400
        assert "needs a prompt" in e.value.detail

        # With a prompt (hand edit) Go flips; a second Go is refused.
        await world_routes.agent_brief_edit(
            wid, world_routes.AgentBriefRequest(prompt="a drowned world"))
        resp2 = await world_routes.agent_session_go(wid)
        assert resp2["phase"] == "build"
        await _until(lambda: handle.phase == "build")
        with pytest.raises(HTTPException) as e:
            await world_routes.agent_session_go(wid)
        assert e.value.status_code == 409
        await asyncio.wait_for(handle.task, timeout=5)

    run(go())


def test_brief_route_hand_edits_preserve_note_state_and_draft_status(
        builder, monkeypatch):
    from fastapi import HTTPException
    world_routes = _routes(monkeypatch, builder)
    _canned_chat(monkeypatch, [{"say": "hi"}])

    async def go():
        resp = await world_routes.agent_chat_start(
            world_routes.AgentChatRequest(text="hello", prompt="p"))
        wid = resp["world_id"]
        handle = harness.get_build(wid)
        await _until(lambda: _events_of(handle, "turn"))

        builder.update_brief(wid, brief={
            "prompt": "p", "rules": ["Rule A.", "Rule B."],
            "notes": [{"id": "n1", "text": "Keep me.", "subject": "",
                       "verifier_context": "withdrawn once"},
                      {"id": "n2", "text": "Drop me.", "subject": ""}]})
        # The user's ✕ removes rule B and note n2; machinery keys survive
        # on kept notes; the world stays an in-progress draft.
        resp2 = await world_routes.agent_brief_edit(
            wid, world_routes.AgentBriefRequest(
                rules=["Rule A."],
                notes=[{"id": "n1", "text": "Keep me.", "subject": "",
                        "verifier_context": "withdrawn once"}]))
        assert resp2["brief"]["rules"] == ["Rule A."]
        assert resp2["brief"]["notes"][0]["verifier_context"] == "withdrawn once"
        state = builder.load_world(wid)
        assert state["brief"]["rules"] == ["Rule A."]
        assert [n["id"] for n in state["brief"]["notes"]] == ["n1"]
        assert state["complete"] is False           # still a draft
        assert state["agent_phase"] == "chat"       # still conversational
        assert handle.brief["rules"] == ["Rule A."]  # live mirror follows

        with pytest.raises(HTTPException) as e:
            await world_routes.agent_brief_edit(
                "nope", world_routes.AgentBriefRequest(prompt="x"))
        assert e.value.status_code == 404
        await _end(handle)

    run(go())


def test_brief_route_refuses_mid_build(builder, monkeypatch):
    from fastapi import HTTPException
    world_routes = _routes(monkeypatch, builder)

    async def turn(services, messages):
        return {"action": {"tool": "read_lint"}}

    monkeypatch.setattr(harness, "agent_turn", turn)

    async def go():
        handle = harness.start_agent_build(builder, "a quiet land")
        with pytest.raises(HTTPException) as e:
            await world_routes.agent_brief_edit(
                handle.world_id, world_routes.AgentBriefRequest(prompt="x"))
        assert e.value.status_code == 409
        harness.cancel_build(handle.world_id)
        await asyncio.wait_for(handle.task, timeout=5)

    run(go())


def test_message_route_resurrects_a_dead_chat_session(builder, monkeypatch):
    world_routes = _routes(monkeypatch, builder)
    replies = _canned_chat(monkeypatch, [{"say": "first reply"}])

    async def go():
        handle = harness.start_chat_session(builder, "hello", prompt="p")
        wid = handle.world_id
        await _until(lambda: _events_of(handle, "turn"))
        handle.task.cancel()
        try:
            await handle.task
        except asyncio.CancelledError:
            pass
        harness._BUILDS.clear()

        replies.append({"say": "resumed reply"})
        resp = await world_routes.agent_build_message(
            wid, world_routes.AgentMessageRequest(text="still there?"))
        assert resp["queued"] is True and resp["id"] == "m2"
        revived = harness.get_build(wid)
        assert revived is not None and revived.phase == "chat"
        await _until(lambda: any(e.get("say") == "resumed reply"
                                 for e in _events_of(revived, "turn")))
        await _end(revived)

    run(go())


# ---------------------------------------------------------------------------
# update_brief: the metadata-surgical write every brief surface rides
# ---------------------------------------------------------------------------

def test_update_brief_preserves_draft_status_and_metadata(builder):
    import json as _json

    wid = builder.save_draft("draft_world", {
        "seed_prompt": "old", "steps": {}, "complete": False,
        "agent_phase": "build",
        "brief": {"prompt": "old", "rules": [], "notes": []}})
    store = builder.services.enrichment_store
    meta_path = store.world_dir(wid) / "metadata.json"
    with open(meta_path, encoding="utf-8") as f:
        before = _json.load(f)

    builder.update_brief(wid, brief={"prompt": "new", "rules": ["R."],
                                     "notes": []}, seed_prompt="new")
    with open(meta_path, encoding="utf-8") as f:
        after = _json.load(f)
    # The draft status, phase and creation time all survive the edit —
    # this is the C7a wart (save_world's in_progress=False side effect)
    # closed.
    assert after["in_progress"] is True
    assert after["agent_phase"] == "build"
    assert after["created_at"] == before["created_at"]
    assert after["brief"]["prompt"] == "new"
    assert after["seed_prompt"] == "new"
    state = builder.load_world(wid)
    assert state["complete"] is False and state["seed_prompt"] == "new"

    with pytest.raises(FileNotFoundError):
        builder.update_brief("never_saved", brief={})


def test_mid_build_brief_tool_no_longer_finishes_the_draft(builder):
    from wbworldgen.worldgen.agent.registry import ToolContext, invoke_tool

    wid = builder.save_draft("mid_build", {
        "seed_prompt": "p", "steps": {}, "complete": False,
        "brief": {"prompt": "p", "rules": [], "notes": []}})
    run(invoke_tool(ToolContext(builder=builder, world_id=wid),
                    "update_rules", {"rules": ["The rivers sing."]}))
    state = builder.load_world(wid)
    assert state["brief"]["rules"] == ["The rivers sing."]
    assert state["complete"] is False  # was flipped to True before the fix
