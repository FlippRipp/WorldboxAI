"""Tests for iterate mode: a free-text change request relaunches the agent
on a FINISHED world (back to an in-progress draft, content and brief kept),
the request rides every turn's system prompt and the evaluator's critique,
and the done-gate pass clears it as the world reads finished again.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_agent_iterate.py
"""

import asyncio
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import evaluator as evaluator_mod
from wbworldgen.worldgen.agent import harness
from wbworldgen.worldgen.agent import verifier as verifier_mod


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_iterate_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    harness._BUILDS.clear()


def _finished_world(builder, world_id="iterate_world"):
    """A finished, lint-clean world: rules authored, chain-connected named
    and described towns — the shape the done-gate accepts in mock mode."""
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": 6 - i,
         "x": float(i), "y": 0.0, "name": f"Town {i}",
         "description": f"Flavor for Town {i}.", "region": ""}
        for i in range(6)
    ]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(5)]
    return builder.save_world(world_id, {
        "seed_prompt": "a quiet land",
        "brief": {"prompt": "a quiet land", "rules": [], "notes": []},
        "steps": {
            "world_rules": {"data": {"genre": "pastoral", "tone": "calm"},
                            "approved": True},
            "map_generation": {"data": {"nodes": nodes, "edges": edges},
                               "approved": True},
        },
    })


BUDGETS = {"max_turns": 40, "max_tool_calls": 60, "fix_rounds": 3}


# --- the launch --------------------------------------------------------------

def test_iterate_flips_finished_world_to_draft_and_records_request(
        builder, monkeypatch):
    wid = _finished_world(builder)

    async def _no_loop(handle):
        handle.status = "cancelled"

    monkeypatch.setattr(harness, "_run_build", _no_loop)

    async def go():
        handle = harness.start_iterate_build(
            builder, wid, "  Make the towns rivals.  ")
        await handle.task
        return handle

    handle = run(go())
    assert handle.world_id == wid
    state = builder.load_world(wid)
    # Back to a draft, content and brief kept, request standing — a
    # cancelled/failed run leaves it for the next relaunch to work toward.
    assert state["complete"] is False
    assert state["iterate_request"] == "Make the towns rivals."
    assert state["brief"]["prompt"] == "a quiet land"
    assert state["steps"]["map_generation"]["data"]["nodes"]


def test_iterate_validation(builder, monkeypatch):
    wid = _finished_world(builder)
    with pytest.raises(ValueError, match="change or improve"):
        harness.start_iterate_build(builder, wid, "   ")
    with pytest.raises(FileNotFoundError):
        harness.start_iterate_build(builder, "nope", "change it")
    # A running session refuses the relaunch, like any double launch.
    running = harness.AgentBuild(wid, "a quiet land", builder)
    running.status = "running"
    monkeypatch.setitem(harness._BUILDS, wid, running)
    with pytest.raises(ValueError, match="already running"):
        harness.start_iterate_build(builder, wid, "change it")
    harness._BUILDS.clear()
    # An in-progress draft is not iterable — it has its own recovery paths.
    # (complete must be cleared: save_draft records a still-complete state
    # as draft_complete, which reads as finished.)
    state = builder.load_world(wid)
    state["complete"] = False
    builder.save_draft(wid, state)
    with pytest.raises(ValueError, match="still in progress"):
        harness.start_iterate_build(builder, wid, "change it")


# --- the prompt --------------------------------------------------------------

def test_change_request_rendered_in_every_turn_system_prompt(builder):
    wid = _finished_world(builder)
    handle = harness.AgentBuild(wid, "a quiet land", builder)
    state = builder.load_world(wid)
    assert "Change request" not in harness._system_prompt(handle, state, BUDGETS)
    state["iterate_request"] = "Make the towns rivals."
    prompt = harness._system_prompt(handle, state, BUDGETS)
    assert "### Change request (why this build is running)" in prompt
    assert "Make the towns rivals." in prompt


# --- the full loop: request satisfied on done --------------------------------

def test_done_gate_pass_marks_finished_and_clears_request(builder, monkeypatch):
    wid = _finished_world(builder)
    seen_prompts = []

    async def fake_turn(services, messages):
        seen_prompts.append(messages[0]["content"])
        return {"thought": "request handled",
                "done": {"summary": "Rivalry threaded through the towns."}}

    monkeypatch.setattr(harness, "agent_turn", fake_turn)

    async def go():
        handle = harness.start_iterate_build(builder, wid, "Make the towns rivals.")
        await handle.task
        return handle

    handle = run(go())
    assert handle.status == "done"
    # The agent worked under the standing request...
    assert "Make the towns rivals." in seen_prompts[0]
    # ...and the completing save marked the world finished and consumed it.
    state = builder.load_world(wid)
    assert state["complete"] is True
    assert "iterate_request" not in state


# --- the evaluator -----------------------------------------------------------

def test_critique_prompt_carries_change_request(monkeypatch):
    captured = {}

    async def fake_completion(llm, messages, **kwargs):
        captured["messages"] = messages
        return {"findings": []}

    monkeypatch.setattr(evaluator_mod, "json_retry_completion", fake_completion)
    services = types.SimpleNamespace(
        llm=types.SimpleNamespace(storyteller_model="m"), json_retry_attempts=1)
    run(evaluator_mod.generate_critique(
        services, {"genre": "pastoral"}, {}, {"problem_count": 0}, "excerpt",
        change_request="Make the towns rivals."))
    system, user = (m["content"] for m in captured["messages"])
    assert "change request" in system and "change_request" in system
    assert "Make the towns rivals." in user

    run(evaluator_mod.generate_critique(
        services, {"genre": "pastoral"}, {}, {"problem_count": 0}, "excerpt"))
    system, user = (m["content"] for m in captured["messages"])
    assert "change request" not in system
    assert "standing change request" not in user


def test_evaluate_world_feeds_request_to_critique(builder, monkeypatch):
    wid = _finished_world(builder)
    state = builder.load_world(wid)
    state["iterate_request"] = "Make the towns rivals."
    compiled = builder.services.compiled.load(wid)
    captured = {}

    async def fake_critique(services, rules, lore, lint_report, excerpts,
                            scope_note="", change_request=""):
        captured["change_request"] = change_request
        return []

    async def fake_verify(*args, **kwargs):
        return {"verdicts": [], "unverified": [], "skipped": True}

    monkeypatch.setattr(evaluator_mod, "generate_critique", fake_critique)
    monkeypatch.setattr(verifier_mod, "verify_notes", fake_verify)
    live = types.SimpleNamespace(llm=types.SimpleNamespace(mode="live"))
    result = run(evaluator_mod.evaluate_world(live, state, compiled))
    assert captured["change_request"] == "Make the towns rivals."
    assert result["clean"] is True


# --- the route ---------------------------------------------------------------

def test_iterate_route(builder, monkeypatch):
    import routes as world_routes

    monkeypatch.setattr(world_routes, "world_builder", builder)
    wid = _finished_world(builder)

    async def _no_loop(handle):
        handle.status = "cancelled"

    monkeypatch.setattr(harness, "_run_build", _no_loop)

    async def go():
        resp = await world_routes.agent_build_iterate(
            wid, world_routes.AgentIterateRequest(text="Make the towns rivals."))
        handle = harness.get_build(wid)
        if handle and handle.task:
            await handle.task
        return resp

    resp = run(go())
    assert resp["world_id"] == wid

    # The relaunch left the world an in-progress draft: a second iterate on
    # it is refused (400), like an empty request; an unknown world is 404.
    with pytest.raises(Exception) as exc:
        run(world_routes.agent_build_iterate(
            wid, world_routes.AgentIterateRequest(text="again")))
    assert getattr(exc.value, "status_code", None) == 400
    with pytest.raises(Exception) as exc:
        run(world_routes.agent_build_iterate(
            wid, world_routes.AgentIterateRequest(text="   ")))
    assert getattr(exc.value, "status_code", None) == 400
    with pytest.raises(Exception) as exc:
        run(world_routes.agent_build_iterate(
            "nope", world_routes.AgentIterateRequest(text="change it")))
    assert getattr(exc.value, "status_code", None) == 404
