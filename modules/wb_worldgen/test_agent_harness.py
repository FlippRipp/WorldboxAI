"""Tests for the C2 agent harness and evaluator: canned action sequences
through the real loop (budget exhaustion, invalid-action recovery, done-gate
refusal and acceptance, todo round-trip, cancel, reattach/replay) plus the
evaluator's lint-only and critique-merge paths. No tokens are spent: the
turn completion (``harness.agent_turn``) is the monkeypatch seam, exactly
like the pass-module patch points.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_agent_harness.py
"""

import asyncio
import json
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import evaluate_world, harness


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_harness_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    """Mock-path builder: no LLM wired, so the evaluator is lint-only and
    generate_step takes the mock generators."""
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    harness._BUILDS.clear()


def _settings(builder, **overrides):
    values = dict(overrides)
    builder.set_settings(types.SimpleNamespace(get=lambda k: values.get(k)))


def _content_world(builder, world_id="built_world", orphan=False,
                   broken_link=False):
    """A finished-looking world: rules authored, six named + described
    chain-connected towns. ``broken_link`` plants exactly ONE blocking lint
    finding (a description referencing a nonexistent node) — the defect the
    done-gate tests accept by key. ``orphan`` adds an unreachable node,
    which deliberately yields TWO findings (orphan + disconnected map)."""
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": 6 - i,
         "x": float(i), "y": 0.0, "name": f"Town {i}",
         "description": f"Flavor for Town {i}.", "region": ""}
        for i in range(6)
    ]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(5)]
    if orphan:
        nodes.append({"id": "x", "type": "ruin", "importance": 1,
                      "x": 9.0, "y": 9.0, "name": "Lost Hut",
                      "description": "Alone.", "region": ""})
    if broken_link:
        nodes[1]["description"] = "Past ${link_ghost} and onward."
    return builder.save_world(world_id, {
        "seed_prompt": "a quiet land",
        "steps": {
            "world_rules": {"data": {"genre": "pastoral", "tone": "calm",
                                     "custom_rules": ["Nothing hurries here."]},
                            "approved": True},
            "map_generation": {"data": {"nodes": nodes, "edges": edges},
                               "approved": True},
        },
    })


def _canned(monkeypatch, turns):
    """Drive the loop with a canned completion sequence. Entries may be
    dicts (returned as the completion), exceptions (raised), or callables
    (awaited/called for side effects returning the completion)."""
    seq = list(turns)
    seen = {"count": 0}

    async def fake_turn(services, messages):
        seen["count"] += 1
        if not seq:
            raise AssertionError("agent_turn called beyond the canned sequence")
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            result = item()
            if asyncio.iscoroutine(result):
                result = await result
            return result
        return item

    monkeypatch.setattr(harness, "agent_turn", fake_turn)
    return seen


def _run_build(builder, seed="a quiet land", world_id=None, subscribe=False):
    """Start a build and await its task; optionally also collect every
    broadcast event through a live subscriber queue."""
    async def go():
        handle = harness.start_agent_build(builder, seed, world_id=world_id)
        events = []
        if subscribe:
            q = handle.subscribe()
            while True:
                item = await q.get()
                if item is None:
                    break
                events.append(item)
        await handle.task
        return handle, events

    return run(go())


def _events_of(handle, kind):
    return [e for e in handle.log if e.get("type") == kind]


# ---------------------------------------------------------------------------
# The loop: happy path, todo round-trip, live streaming
# ---------------------------------------------------------------------------

def test_happy_build_completes_and_saves(builder, monkeypatch):
    wid = _content_world(builder)
    _canned(monkeypatch, [
        {"thought": "look around",
         "todo": [{"text": "inspect", "status": "in_progress"},
                  {"text": "verify", "status": "pending"}],
         "action": {"tool": "read_lint"}},
        {"thought": "clean — done",
         "todo": [{"text": "inspect", "status": "done"},
                  {"text": "verify", "status": "done"}],
         "done": {"summary": "A quiet land of six towns."}},
    ])
    handle, events = _run_build(builder, world_id=wid, subscribe=True)

    assert handle.status == "done"
    assert handle.result["summary"] == "A quiet land of six towns."
    assert handle.result["accepted_findings"] == []
    assert handle.turns == 2 and handle.tool_calls == 1
    # The world graduated from draft to complete.
    assert builder.load_world(wid)["complete"] is True
    # Persisted artifact mirrors the outcome.
    artifact = harness.load_build_artifact(builder, wid)
    assert artifact["status"] == "done"
    assert [t["status"] for t in artifact["todo"]] == ["done", "done"]
    assert artifact["log"] == handle.log
    # Event discipline: turn/action/observation/eval/done, indexed.
    kinds = [e["type"] for e in handle.log]
    assert kinds == ["turn", "action", "observation", "turn", "eval", "done"]
    assert [e["i"] for e in handle.log] == list(range(len(handle.log)))
    # Live subscribers saw the same persisted events (no transients here).
    assert events == handle.log


def test_todo_kept_when_field_omitted(builder, monkeypatch):
    wid = _content_world(builder)
    _canned(monkeypatch, [
        {"todo": ["plan the land"], "action": {"tool": "read_world"}},
        {"action": {"tool": "read_lint"}},  # no todo field — list unchanged
        {"todo": [{"text": "plan the land", "status": "done"}],
         "done": {"summary": "done"}},
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    turn_todos = [e["todo"] for e in _events_of(handle, "turn")]
    assert turn_todos[0] == [{"text": "plan the land", "status": "pending"}]
    assert turn_todos[1] == turn_todos[0]  # omitted field kept it
    assert turn_todos[2] == [{"text": "plan the land", "status": "done"}]


# ---------------------------------------------------------------------------
# Error feedback: invalid actions, protocol errors
# ---------------------------------------------------------------------------

def test_invalid_actions_come_back_as_observations(builder, monkeypatch):
    wid = _content_world(builder)
    _canned(monkeypatch, [
        {"action": {"tool": "no_such_tool"}},
        {"action": {"tool": "read_map", "args": {"map_id": "moon"}}},
        {"done": {"summary": "done anyway"}},
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    observations = _events_of(handle, "observation")
    assert observations[0]["ok"] is False
    assert "Unknown tool 'no_such_tool'" in observations[0]["error"]
    assert observations[1]["ok"] is False
    assert "Unknown map 'moon'" in observations[1]["error"]
    # The agent saw both errors in its prompt-side recent window.
    assert any("no_such_tool" in json.dumps(r) for r in handle.recent)


def test_protocol_errors_are_recoverable(builder, monkeypatch):
    wid = _content_world(builder)
    _canned(monkeypatch, [
        {"thought": "hmm"},                                   # neither action nor done
        {"action": {"tool": "read_lint"}, "done": {"summary": "x"}},  # both
        {"todo": [{"text": "x", "status": "someday"}],        # bad status
         "action": {"tool": "read_lint"}},
        {"done": {"summary": "recovered"}},
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    observations = _events_of(handle, "observation")
    assert "exactly one of 'action' or 'done'" in observations[0]["protocol_error"]
    assert "exactly one of 'action' or 'done'" in observations[1]["protocol_error"]
    assert "invalid todo status" in observations[2]["protocol_error"]
    assert handle.todo == []  # the invalid todo never replaced the list


# ---------------------------------------------------------------------------
# Budgets (D5)
# ---------------------------------------------------------------------------

def test_turn_budget_exhaustion_leaves_a_draft(builder, monkeypatch):
    wid = _content_world(builder)
    _settings(builder, **{"world.agent_max_turns": 5})
    _canned(monkeypatch, [{"action": {"tool": "read_world"}}] * 5)
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "budget_exhausted"
    assert handle.turns == 5
    assert handle.log[-1] == {"type": "done", "status": "budget_exhausted",
                              "turns": 5, "tool_calls": 5, "error": None,
                              "result": None, "i": handle.log[-1]["i"]}
    # The world stays an in-progress draft for the user to pick up.
    assert builder.load_world(wid)["complete"] is False


def test_tool_budget_forces_the_endgame(builder, monkeypatch):
    wid = _content_world(builder)
    _settings(builder, **{"world.agent_max_tool_calls": 5})
    _canned(monkeypatch, [{"action": {"tool": "read_world"}}] * 6
            + [{"done": {"summary": "wrapping up"}}])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    assert handle.tool_calls == 5  # the sixth call was refused
    refused = _events_of(handle, "observation")[5]
    assert refused["ok"] is False and "Tool budget exhausted" in refused["error"]


def test_llm_failures_abort_after_three(builder, monkeypatch):
    wid = _content_world(builder)
    _canned(monkeypatch, [ValueError("provider down")] * 3)
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "failed"
    assert "3 consecutive LLM failures" in handle.error
    assert handle.log[-1]["type"] == "done" and handle.log[-1]["status"] == "failed"


# ---------------------------------------------------------------------------
# The done-gate (D3)
# ---------------------------------------------------------------------------

def test_done_gate_refuses_structurally_empty_builds(builder, monkeypatch):
    _settings(builder, **{"world.agent_max_turns": 5})
    _canned(monkeypatch, [{"done": {"summary": "nothing happened"}}] * 5)
    handle, _ = _run_build(builder)  # fresh empty draft
    assert handle.status == "budget_exhausted"
    first = _events_of(handle, "observation")[0]
    assert first["done_rejected"] is True
    assert "world rules" in first["message"].lower()


def test_done_gate_blocks_then_accepts_findings(builder, monkeypatch):
    wid = _content_world(builder, broken_link=True)
    key = "lint:broken_link_token:root:n1"
    _canned(monkeypatch, [
        {"done": {"summary": "ship it"}},
        {"done": {"summary": "ship it", "accept_findings": [key],
                  "note": "The ghost reference is an intentional mystery."}},
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    rejection = _events_of(handle, "observation")[0]
    assert rejection["done_rejected"] is True
    assert [f["key"] for f in rejection["blocking_findings"]] == [key]
    accepted = handle.result["accepted_findings"]
    assert len(accepted) == 1 and accepted[0]["key"] == key
    assert accepted[0]["auto"] is False
    assert accepted[0]["note"] == "The ghost reference is an intentional mystery."
    assert handle.finding_rounds[key] == 2  # seen by both gate runs


def test_done_claim_accepting_without_note_is_protocol_error(builder, monkeypatch):
    wid = _content_world(builder, broken_link=True)
    _settings(builder, **{"world.agent_max_turns": 5})
    _canned(monkeypatch, [
        {"done": {"summary": "x",
                  "accept_findings": ["lint:broken_link_token:root:n1"]}},
    ] * 5)
    handle, _ = _run_build(builder, world_id=wid)
    observations = _events_of(handle, "observation")
    assert "requires a 'note'" in observations[0]["protocol_error"]


def test_findings_auto_accept_after_fix_round_budget(builder, monkeypatch):
    wid = _content_world(builder, broken_link=True)
    _settings(builder, **{"world.agent_fix_rounds": 1})
    _canned(monkeypatch, [
        {"done": {"summary": "try 1"}},   # sighting 1 -> rejected
        {"done": {"summary": "try 2"}},   # sighting 2 > 1 -> auto-accepted
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    accepted = handle.result["accepted_findings"]
    assert len(accepted) == 1 and accepted[0]["auto"] is True


def test_evaluate_tool_runs_feed_the_fix_round_tracking(builder, monkeypatch):
    wid = _content_world(builder, broken_link=True)
    key = "lint:broken_link_token:root:n1"
    _canned(monkeypatch, [
        {"action": {"tool": "evaluate"}},
        {"done": {"summary": "accepting",
                  "accept_findings": [key], "note": "fine"}},
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    evals = _events_of(handle, "eval")
    assert [e["trigger"] for e in evals] == ["tool", "done_claim"]
    assert handle.finding_rounds[key] == 2
    observation = _events_of(handle, "observation")[0]
    assert observation["ok"] is True
    assert observation["result"]["clean"] is False


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

def test_cancel_between_turns(builder, monkeypatch):
    wid = _content_world(builder)

    def act_then_cancel():
        harness.cancel_build(wid)
        return {"action": {"tool": "read_world"}}

    _canned(monkeypatch, [
        {"action": {"tool": "read_world"}},
        act_then_cancel,  # cancel lands mid-turn; the loop stops before turn 3
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "cancelled"
    assert handle.turns == 2
    assert handle.log[-1]["status"] == "cancelled"
    assert builder.load_world(wid)["complete"] is False


def test_cancel_of_unknown_or_finished_build_is_false(builder, monkeypatch):
    assert harness.cancel_build("nope") is False
    wid = _content_world(builder)
    _canned(monkeypatch, [{"done": {"summary": "done"}}])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    assert harness.cancel_build(wid) is False


# ---------------------------------------------------------------------------
# The evaluator (D3)
# ---------------------------------------------------------------------------

def test_evaluator_lint_only_without_llm(builder):
    wid = _content_world(builder, orphan=True)
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    result = run(evaluate_world(builder.services, world_state, compiled,
                                major_floor=6))
    assert result["clean"] is False
    keys = [f["key"] for f in result["findings"]]
    assert "lint:orphan_node:root:x" in keys
    assert all(f["source"] == "lint" for f in result["findings"])


def test_evaluator_merges_critique_findings(builder):
    wid = _content_world(builder)

    async def fake_completion(messages=None, model=None, **kwargs):
        assert model == "smart-slot"
        return json.dumps({"findings": [
            {"kind": "tone_break", "severity": "problem", "map_id": "root",
             "node_id": "n1", "finding": "Town 1 reads frantic.",
             "suggestion": "Rework its description toward calm."},
            {"kind": "flavor", "severity": "nit", "finding": "Could be richer."},
        ]})

    builder._llm_service = types.SimpleNamespace(
        mode="live", storyteller_model="smart-slot",
        simple_completion=fake_completion)
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    result = run(evaluate_world(builder.services, world_state, compiled,
                                major_floor=6))
    by_key = {f["key"]: f for f in result["findings"]}
    assert "critique:tone_break:root:n1" in by_key
    assert by_key["critique:tone_break:root:n1"]["severity"] == "problem"
    assert by_key["critique:flavor:-:-"]["severity"] == "nit"
    assert result["clean"] is False and result["blocking"] == 1


def test_evaluator_skips_critique_without_rules(builder):
    wid = _content_world(builder)
    world_state = builder.load_world(wid)
    world_state["steps"].pop("world_rules")

    async def exploding_completion(**kwargs):  # must never be called
        raise AssertionError("critique attempted without rules")

    builder._llm_service = types.SimpleNamespace(
        mode="live", storyteller_model="smart-slot",
        simple_completion=exploding_completion)
    compiled = builder.services.compiled.load(wid)
    result = run(evaluate_world(builder.services, world_state, compiled))
    assert result["clean"] is True  # lint-only, and the content is sound


# ---------------------------------------------------------------------------
# Routes: launch, status, events replay + reattach
# ---------------------------------------------------------------------------

async def _sse_events(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return [json.loads(line[len("data: "):])
            for line in "".join(chunks).split("\n\n") if line.startswith("data: ")]


def test_routes_launch_status_events_and_reattach(builder, monkeypatch):
    import routes as world_routes

    monkeypatch.setattr(world_routes, "world_builder", builder)
    wid_holder = {}

    def late_done():
        return {"done": {"summary": "built"}}

    _canned(monkeypatch, [
        {"todo": ["look"], "action": {"tool": "read_world"}},
        late_done,
    ])

    async def go():
        from fastapi import HTTPException
        # Empty prompts are refused.
        with pytest.raises(HTTPException):
            await world_routes.agent_build_start(
                world_routes.AgentBuildRequest(seed_prompt="   "))
        launched = await world_routes.agent_build_start(
            world_routes.AgentBuildRequest(seed_prompt="a quiet land"))
        wid = launched["world_id"]
        wid_holder["wid"] = wid
        handle = harness.get_build(wid)
        # Seed the fresh draft with finished content so the gate passes:
        # the canned sequence claims done on turn 2.
        _content_world(builder, world_id=wid)
        await handle.task

        status = await world_routes.agent_build_status(wid)
        assert status["status"] == "done" and status["turns"] == 2

        # Live-handle replay honors the cursor and ends after the terminal.
        resp = await world_routes.agent_build_events(
            wid, world_routes.AgentEventsRequest(after=2))
        events = await _sse_events(resp)
        assert events[0]["i"] == 2
        assert events[-1]["type"] == "done"

        # Reattach after a backend restart: no live handle, artifact serves.
        harness._BUILDS.clear()
        status = await world_routes.agent_build_status(wid)
        assert status["status"] == "done" and "log" not in status
        resp = await world_routes.agent_build_events(
            wid, world_routes.AgentEventsRequest(after=0))
        replayed = await _sse_events(resp)
        assert [e["type"] for e in replayed][-1] == "done"
        assert len(replayed) == status["log_len"]

        with pytest.raises(HTTPException):
            await world_routes.agent_build_status("never_built")

    run(go())


def test_double_launch_for_same_world_is_refused(builder, monkeypatch):
    wid = _content_world(builder)

    async def slow_turn():
        await asyncio.sleep(0.05)
        return {"done": {"summary": "done"}}

    _canned(monkeypatch, [slow_turn])

    async def go():
        handle = harness.start_agent_build(builder, "x", world_id=wid)
        with pytest.raises(ValueError, match="already running"):
            harness.start_agent_build(builder, "x", world_id=wid)
        await handle.task

    run(go())


def test_brief_rules_persist_and_reach_the_prompt(builder, monkeypatch):
    """C4: co-authored rules land in state['brief'] (persisted), in the
    handle/snapshot (observers), and in every turn's system prompt as
    fixed input for world_rules."""
    seen = {}

    async def failing_turn(services, messages):
        seen["system"] = messages[0]["content"]
        raise RuntimeError("no model in this test")

    monkeypatch.setattr(harness, "agent_turn", failing_turn)
    cleaned = ["The tide is a living god.", "Iron rusts overnight."]

    async def go():
        handle = harness.start_agent_build(
            builder, "a drowned world",
            rules=["  The tide is a living god. ", "", "Iron rusts overnight."])
        await handle.task
        return handle

    handle = run(go())
    assert handle.brief == {"prompt": "a drowned world", "rules": cleaned,
                            "notes": []}
    assert handle.snapshot()["brief"]["rules"] == cleaned
    assert builder.load_world(handle.world_id)["brief"] == {
        "prompt": "a drowned world", "rules": cleaned, "notes": []}
    system = seen["system"]
    assert "Co-authored world rules" in system
    for rule in cleaned:
        assert f"- {rule}" in system
    # The author-rules-first instruction points at the fixed input.
    assert "co-authored rules above are its fixed input" in system

    # Adopting the same world without passing rules keeps the recorded brief.
    async def adopt():
        h2 = harness.start_agent_build(builder, "a drowned world",
                                       world_id=handle.world_id)
        await h2.task
        return h2

    h2 = run(adopt())
    assert h2.brief["rules"] == cleaned
    assert builder.load_world(handle.world_id)["brief"]["rules"] == cleaned


# ---------------------------------------------------------------------------
# World checkpoints + revert (v2c): every mutating action snapshots first
# ---------------------------------------------------------------------------

def _store(builder):
    return builder.services.enrichment_store


def test_mutating_action_checkpoints_and_revert_restores(builder, monkeypatch):
    wid = _content_world(builder)
    _canned(monkeypatch, [
        # Log: turn i=0, action i=1 → the edit is checkpointed as "1".
        {"thought": "rename",
         "action": {"tool": "edit_node",
                    "args": {"node_id": "n0", "name": "Renamed Keep"}}},
        {"thought": "that made it worse — go back",
         "action": {"tool": "revert", "args": {"checkpoint": 1}}},
        {"thought": "back on the good timeline",
         "done": {"summary": "Six quiet towns, as they were."}},
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"

    observations = _events_of(handle, "observation")
    assert observations[0]["ok"] is True
    assert observations[0]["checkpoint"] == 1        # the agent's handle
    assert observations[1]["ok"] is True
    assert observations[1]["result"]["reverted_to_before_action"] == 1

    # The rename is gone from disk; the revert window closed with the build.
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert nodes[0]["name"] == "Town 0"
    assert _store(builder).list_checkpoints(wid) == []


def test_read_actions_do_not_checkpoint(builder, monkeypatch):
    wid = _content_world(builder)
    seen_during = {}

    def snoop():
        seen_during["tags"] = _store(builder).list_checkpoints(wid)
        return {"thought": "done", "done": {"summary": "A quiet land."}}

    _canned(monkeypatch, [
        {"thought": "look", "action": {"tool": "read_lint"}},
        snoop,
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"
    assert seen_during["tags"] == []                 # read-only: no snapshot
    assert all("checkpoint" not in o for o in _events_of(handle, "observation"))


def test_stale_checkpoints_cleared_at_launch(builder, monkeypatch):
    wid = _content_world(builder)
    _store(builder).snapshot_world(wid, "99")        # a previous build's leftover

    def first_turn():
        # Cleared before the first agent turn: stale tags must never
        # collide with this build's fresh action indices.
        assert _store(builder).list_checkpoints(wid) == []
        return {"thought": "done", "done": {"summary": "A quiet land."}}

    _canned(monkeypatch, [first_turn])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"


def test_checkpoint_failure_blocks_the_mutation(builder, monkeypatch):
    wid = _content_world(builder)

    def broken_snapshot(world_id, tag):
        raise OSError("disk full")

    monkeypatch.setattr(_store(builder), "snapshot_world", broken_snapshot)
    _canned(monkeypatch, [
        {"thought": "rename",
         "action": {"tool": "edit_node",
                    "args": {"node_id": "n0", "name": "Renamed Keep"}}},
        {"thought": "give up", "done": {"summary": "A quiet land."}},
    ])
    handle, _ = _run_build(builder, world_id=wid)
    assert handle.status == "done"

    failed = _events_of(handle, "observation")[0]
    assert failed["ok"] is False
    assert "checkpoint before 'edit_node' failed" in failed["error"]
    assert "NOT run" in failed["error"]
    # The world is untouched and the unprotected call cost no budget.
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert nodes[0]["name"] == "Town 0"
    assert handle.tool_calls == 0
