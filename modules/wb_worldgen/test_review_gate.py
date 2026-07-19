"""Tests for the C5 review gate (N7): the done result's pending_review
payload (amended notes + explicitly accepted note obligations), and the
veto — original text restored, no_compromise stamped, fix run relaunched on
the finished world.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_review_gate.py
"""

import asyncio
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import harness as harness_mod


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_review_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


def _world(builder, notes, world_id="review_world"):
    nodes = [
        {"id": "a1", "name": "Harbor Town", "type": "town", "importance": 8,
         "x": 0.0, "y": 0.0, "description": "A busy harbor."},
        {"id": "a2", "name": "Salt Keep", "type": "keep", "importance": 7,
         "x": 1.0, "y": 0.0, "description": "A fortress."},
    ]
    return builder.save_world(world_id, {
        "seed_prompt": "test world",
        "brief": {"prompt": "test world", "rules": [], "notes": notes},
        "steps": {
            "world_rules": {"data": {"genre": "salt"}, "approved": True},
            "map_generation": {"data": {
                "nodes": nodes, "edges": [{"from": "a1", "to": "a2"}]},
                "approved": True},
        },
    })


AMENDED = {"id": "n1", "subject": "Harbor Town",
           "text": "One grand lighthouse.", "status": "amended",
           "original_text": "Three lighthouses.",
           "rationale": "One serves the intent."}
PLAIN = {"id": "n2", "subject": "", "text": "Salt is currency."}


# --- pending_review assembly -------------------------------------------------

def test_pending_review_collects_amendments_and_accepted_notes():
    state = {"brief": {"notes": [dict(AMENDED), dict(PLAIN)]}}
    accepted = [
        {"key": "note:n2:-:-", "finding": "not honored", "auto": False,
         "note": "cannot fit salt everywhere"},
        {"key": "lint:note_unbound:-:n1", "finding": "unbound", "auto": False,
         "note": "reason"},
        {"key": "critique:tone:-:-", "finding": "tone", "auto": True,
         "note": ""},
    ]
    review = harness_mod._pending_review(state, accepted)
    assert [a["id"] for a in review["amended"]] == ["n1"]
    assert review["amended"][0]["original_text"] == "Three lighthouses."
    assert review["amended"][0]["amended_text"] == "One grand lighthouse."
    ids = [a["id"] for a in review["accepted_notes"]]
    assert ids == ["n2", "n1"]  # both key shapes parse; critique excluded
    assert review["accepted_notes"][0]["reason"] == "cannot fit salt everywhere"
    # Nothing to review -> empty dict (no pending_review key downstream).
    assert harness_mod._pending_review({"brief": {"notes": [dict(PLAIN)]}}, []) == {}


def test_done_gate_result_carries_pending_review(builder, monkeypatch):
    wid = _world(builder, [dict(AMENDED), dict(PLAIN)])
    handle = harness_mod.AgentBuild(wid, "test world", builder)

    note_finding = {"key": "note:n2:-:-", "source": "note",
                    "kind": "note_violation", "severity": "problem",
                    "map_id": None, "node_id": None,
                    "finding": "salt not established", "suggestion": ""}

    async def fake_eval(*args, **kwargs):
        return {"clean": False, "findings": [note_finding], "blocking": 1,
                "lint": {"problems": []},
                "notes": {"skipped": False, "checked": 2, "honored": 1,
                          "unverified": 0, "verdicts": []}}

    monkeypatch.setattr(harness_mod, "evaluate_world", fake_eval)
    budgets = {"max_turns": 40, "max_tool_calls": 60, "fix_rounds": 3}
    result, rejection = run(harness_mod._done_gate(
        handle, {"summary": "done", "accept_findings": ["note:n2:-:-"],
                 "note": "cannot fit salt everywhere"}, budgets))
    assert rejection is None
    review = result["pending_review"]
    assert [a["id"] for a in review["amended"]] == ["n1"]
    assert [a["id"] for a in review["accepted_notes"]] == ["n2"]
    assert review["accepted_notes"][0]["text"] == "Salt is currency."


def test_done_gate_omits_pending_review_when_clean(builder, monkeypatch):
    wid = _world(builder, [dict(PLAIN)])
    handle = harness_mod.AgentBuild(wid, "test world", builder)

    async def fake_eval(*args, **kwargs):
        return {"clean": True, "findings": [], "blocking": 0,
                "lint": {"problems": []},
                "notes": {"skipped": False, "checked": 1, "honored": 1,
                          "unverified": 0, "verdicts": []}}

    monkeypatch.setattr(harness_mod, "evaluate_world", fake_eval)
    budgets = {"max_turns": 40, "max_tool_calls": 60, "fix_rounds": 3}
    result, rejection = run(harness_mod._done_gate(
        handle, {"summary": "done"}, budgets))
    assert rejection is None
    assert "pending_review" not in result


# --- the veto ----------------------------------------------------------------

def test_veto_restores_originals_and_relaunches(builder, monkeypatch):
    wid = _world(builder, [dict(AMENDED), dict(PLAIN)])
    state = builder.load_world(wid)
    state["complete"] = True
    builder.save_world(wid, state)

    async def _no_loop(handle):
        handle.status = "cancelled"

    monkeypatch.setattr(harness_mod, "_run_build", _no_loop)

    async def go():
        handle = harness_mod.veto_notes(builder, wid, ["n1", "n2"])
        await handle.task
        return handle

    handle = run(go())
    assert handle.world_id == wid
    state = builder.load_world(wid)
    notes = {n["id"]: n for n in state["brief"]["notes"]}
    # The amendment is undone and locked.
    assert notes["n1"]["text"] == "Three lighthouses."
    assert notes["n1"]["no_compromise"] is True
    assert "status" not in notes["n1"] and "original_text" not in notes["n1"]
    # The plain (accepted) note is simply locked.
    assert notes["n2"]["text"] == "Salt is currency."
    assert notes["n2"]["no_compromise"] is True
    # The fix run adopted the world: incomplete again, brief kept.
    assert state["complete"] is False
    assert state["brief"]["prompt"] == "test world"


def test_veto_validation(builder, monkeypatch):
    wid = _world(builder, [dict(AMENDED)])
    with pytest.raises(ValueError, match="No note ids"):
        harness_mod.veto_notes(builder, wid, [])
    with pytest.raises(ValueError, match="No such note"):
        harness_mod.veto_notes(builder, wid, ["n9"])
    # A running build refuses a veto relaunch.
    running = harness_mod.AgentBuild(wid, "test world", builder)
    running.status = "running"
    monkeypatch.setitem(harness_mod._BUILDS, wid, running)
    with pytest.raises(ValueError, match="already running"):
        harness_mod.veto_notes(builder, wid, ["n1"])


def test_veto_route(builder, monkeypatch):
    import routes as world_routes

    monkeypatch.setattr(world_routes, "world_builder", builder)
    wid = _world(builder, [dict(AMENDED)])

    async def _no_loop(handle):
        handle.status = "cancelled"

    monkeypatch.setattr(harness_mod, "_run_build", _no_loop)

    async def go():
        resp = await world_routes.agent_build_veto(
            wid, world_routes.AgentVetoRequest(note_ids=["n1"]))
        handle = harness_mod.get_build(wid)
        if handle and handle.task:
            await handle.task
        return resp

    resp = run(go())
    assert resp["world_id"] == wid and resp["vetoed"] == ["n1"]

    with pytest.raises(Exception) as exc:
        run(world_routes.agent_build_veto(
            wid, world_routes.AgentVetoRequest(note_ids=["n9"])))
    assert getattr(exc.value, "status_code", None) == 400
    with pytest.raises(Exception) as exc:
        run(world_routes.agent_build_veto(
            "nope", world_routes.AgentVetoRequest(note_ids=["n1"])))
    assert getattr(exc.value, "status_code", None) == 404
