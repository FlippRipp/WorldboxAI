"""Tests for the C5 note verifier: the read-only toolset carve, the
canned-sequence verification loop (verdicts, disallowed tools, protocol
recovery, budget exhaustion), the evaluator's note findings, offline
degradation, and the done-gate's never-auto-accept rule for notes (N6).

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_note_verifier.py
"""

import asyncio
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import evaluator as evaluator_mod
from wbworldgen.worldgen.agent import harness as harness_mod
from wbworldgen.worldgen.agent import verifier as verifier_mod
from wbworldgen.worldgen.agent.registry import registered_tools


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_verify_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


def _live(builder):
    builder._llm_service = types.SimpleNamespace(
        mode="live", storyteller_model="smart-slot",
        module_fast_model="fast-slot", reader_model="reader-slot")
    return builder


def _noted_world(builder, world_id="verify_world", notes=None):
    """Named two-node world whose brief carries notes: n1 world-scoped, n2
    bound to the node 'Harbor Town'."""
    nodes = [
        {"id": "a1", "name": "Harbor Town", "type": "town", "importance": 8,
         "x": 0.0, "y": 0.0, "description": "A busy harbor."},
        {"id": "a2", "name": "Salt Keep", "type": "keep", "importance": 7,
         "x": 1.0, "y": 0.0, "description": "A fortress of salt."},
    ]
    edges = [{"from": "a1", "to": "a2"}]
    return builder.save_world(world_id, {
        "seed_prompt": "test world",
        "brief": {"prompt": "test world", "rules": [], "notes": notes or [
            {"id": "n1", "text": "Salt is currency.", "subject": ""},
            {"id": "n2", "text": "The town has three lighthouses.",
             "subject": "Harbor Town"},
        ]},
        "steps": {"map_generation": {
            "data": {"nodes": nodes, "edges": edges}, "approved": True}},
    })


def _canned(monkeypatch, replies):
    """Patch verifier_turn with a popping sequence; returns the call log."""
    calls = []

    async def fake_turn(services, messages):
        calls.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(verifier_mod, "verifier_turn", fake_turn)
    return calls


# --- toolset -----------------------------------------------------------------

def test_verifier_toolset_is_the_read_slice():
    ids = set(verifier_mod.verifier_tool_ids())
    assert {"read_world", "read_map", "read_node", "read_step",
            "read_lint", "read_catalog"} <= ids
    assert "evaluate" not in ids
    mutating = {s.id for s in registered_tools() if s.mutates}
    assert not (ids & mutating)


# --- the loop ----------------------------------------------------------------

def test_verify_notes_skipped_offline_and_without_notes(builder):
    wid = _noted_world(builder)
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    # Mock LLM -> skipped.
    report = run(verifier_mod.verify_notes(
        builder.services, builder, wid, world_state, compiled))
    assert report == {"verdicts": [], "unverified": [], "skipped": True}
    # Live LLM but no notes -> skipped before any turn.
    _live(builder)
    world_state["brief"]["notes"] = []
    report = run(verifier_mod.verify_notes(
        builder.services, builder, wid, world_state, compiled))
    assert report["skipped"] is True


def test_verifier_reads_then_delivers_verdicts(builder, monkeypatch):
    wid = _noted_world(builder)
    _live(builder)
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    calls = _canned(monkeypatch, [
        {"thought": "look", "action": {"tool": "read_world", "args": {}}},
        {"thought": "judge", "verdicts": [
            {"id": "n1", "verdict": "honored", "evidence": "salt everywhere"},
            {"id": "n2", "verdict": "not_honored",
             "evidence": "no lighthouse anywhere",
             "suggestion": "rework Harbor Town's description"},
        ]},
    ])
    report = run(verifier_mod.verify_notes(
        builder.services, builder, wid, world_state, compiled))
    assert report["skipped"] is False and report["unverified"] == []
    by_id = {v["id"]: v for v in report["verdicts"]}
    assert by_id["n1"]["verdict"] == "honored"
    assert by_id["n2"]["verdict"] == "not_honored"
    assert by_id["n2"]["map_id"]  # bound to the town's map
    # Turn 2's prompt carried the read action and its result as an
    # observation (read_world returns a structural summary).
    assert '"read_world"' in calls[1][1]["content"]
    assert '"result"' in calls[1][1]["content"]
    # The checklist names each note with its binding.
    system = calls[0][0]["content"]
    assert "n1 [the whole world]" in system
    assert "Salt is currency." in system


def test_verifier_rejects_disallowed_tool_and_recovers(builder, monkeypatch):
    wid = _noted_world(builder)
    _live(builder)
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    calls = _canned(monkeypatch, [
        {"action": {"tool": "edit_node",
                    "args": {"node_id": "a1", "name": "Hacked"}}},
        {"verdicts": [{"id": "n1", "verdict": "honored", "evidence": "ok"},
                      {"id": "n2", "verdict": "honored", "evidence": "ok"}]},
    ])
    report = run(verifier_mod.verify_notes(
        builder.services, builder, wid, world_state, compiled))
    assert len(report["verdicts"]) == 2
    # The mutation never happened and the rejection reached the next turn.
    node = builder.services.compiled.load(wid)
    names = [n["name"] for m in node["maps"].values() for n in m["nodes"]] \
        if isinstance(node.get("maps"), dict) else []
    assert "Hacked" not in names
    assert "not available to the verifier" in calls[1][1]["content"]


def test_verifier_protocol_error_recovers(builder, monkeypatch):
    wid = _noted_world(builder)
    _live(builder)
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    calls = _canned(monkeypatch, [
        {"nonsense": True},
        {"verdicts": [{"id": "n1", "verdict": "honored", "evidence": "ok"},
                      {"id": "n2", "verdict": "honored", "evidence": "ok"}]},
    ])
    report = run(verifier_mod.verify_notes(
        builder.services, builder, wid, world_state, compiled))
    assert len(report["verdicts"]) == 2
    assert "protocol_error" in calls[1][1]["content"]


def test_missing_verdicts_report_unverified(builder, monkeypatch):
    wid = _noted_world(builder)
    _live(builder)
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    _canned(monkeypatch, [
        {"verdicts": [{"id": "n1", "verdict": "honored", "evidence": "ok"},
                      {"id": "bogus", "verdict": "honored", "evidence": ""}]},
    ])
    report = run(verifier_mod.verify_notes(
        builder.services, builder, wid, world_state, compiled))
    assert [v["id"] for v in report["verdicts"]] == ["n1"]
    assert report["unverified"] == ["n2"]


def test_budget_exhaustion_reports_all_unverified(builder, monkeypatch):
    wid = _noted_world(builder)
    _live(builder)
    builder.set_settings(types.SimpleNamespace(
        get=lambda k: {"world.note_verifier_max_turns": 5}.get(k)))
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    _canned(monkeypatch, [
        {"action": {"tool": "read_world", "args": {}}} for _ in range(5)
    ])
    report = run(verifier_mod.verify_notes(
        builder.services, builder, wid, world_state, compiled))
    assert report["skipped"] is False
    assert set(report["unverified"]) == {"n1", "n2"}


# --- evaluator integration ---------------------------------------------------

def test_evaluate_world_turns_verdicts_into_note_findings(builder, monkeypatch):
    wid = _noted_world(builder)
    _live(builder)
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    _canned(monkeypatch, [
        {"verdicts": [
            {"id": "n1", "verdict": "honored", "evidence": "fine"},
            {"id": "n2", "verdict": "not_honored", "evidence": "no lighthouses",
             "suggestion": "rework the description"},
        ]},
    ])
    result = run(evaluator_mod.evaluate_world(
        builder.services, world_state, compiled,
        builder=builder, world_id=wid))
    by_key = {f["key"]: f for f in result["findings"]}
    assert "note:n2:-:-" in by_key
    assert by_key["note:n2:-:-"]["severity"] == "problem"
    assert by_key["note:n2:-:-"]["kind"] == "note_violation"
    assert "note:n1:-:-" not in by_key
    assert result["notes"]["checked"] == 2
    assert result["notes"]["honored"] == 1
    assert result["clean"] is False


def test_evaluate_world_offline_degrades_to_unbound_lint(builder):
    wid = _noted_world(builder, notes=[
        {"id": "n1", "text": "Ghost fact.", "subject": "The Glass Moon"}])
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    result = run(evaluator_mod.evaluate_world(
        builder.services, world_state, compiled,
        builder=builder, world_id=wid))
    assert result["notes"]["skipped"] is True
    kinds = {f["kind"] for f in result["findings"]}
    assert "note_unbound" in kinds
    assert "lint:note_unbound:-:n1" in {f["key"] for f in result["findings"]}


def test_unverified_notes_become_blocking_findings(builder, monkeypatch):
    wid = _noted_world(builder)
    _live(builder)
    builder.set_settings(types.SimpleNamespace(
        get=lambda k: {"world.note_verifier_max_turns": 5}.get(k)))
    world_state = builder.load_world(wid)
    compiled = builder.services.compiled.load(wid)
    _canned(monkeypatch, [
        {"action": {"tool": "read_world", "args": {}}} for _ in range(5)
    ])
    result = run(evaluator_mod.evaluate_world(
        builder.services, world_state, compiled,
        builder=builder, world_id=wid))
    unverified = [f for f in result["findings"]
                  if f["kind"] == "note_unverified"]
    assert {f["key"] for f in unverified} == {"note:n1:-:-", "note:n2:-:-"}
    assert all(f["severity"] == "problem" for f in unverified)


# --- done-gate: never auto-accept notes (N6) ---------------------------------

def _gate_handle(builder, wid):
    handle = harness_mod.AgentBuild(wid, "test world", builder)
    return handle


def test_done_gate_never_auto_accepts_note_findings(builder, monkeypatch):
    wid = _noted_world(builder)
    handle = _gate_handle(builder, wid)
    # world_rules data must exist for the gate's structural precondition.
    state = builder.load_world(wid)
    state["steps"]["world_rules"] = {"data": {"genre": "salt"}, "approved": True}
    builder.save_world(wid, state)

    note_finding = {"key": "note:n2:-:-", "source": "note",
                    "kind": "note_violation", "severity": "problem",
                    "map_id": None, "node_id": None,
                    "finding": "not honored", "suggestion": ""}
    lint_note = {"key": "lint:note_unbound:-:n9", "source": "lint",
                 "kind": "note_unbound", "severity": "problem",
                 "map_id": None, "node_id": "n9",
                 "finding": "unbound", "suggestion": ""}
    other = {"key": "critique:tone:-:-", "source": "critique",
             "kind": "tone", "severity": "problem",
             "map_id": None, "node_id": None,
             "finding": "tonal break", "suggestion": ""}

    async def fake_eval(*args, **kwargs):
        return {"clean": False, "findings": [note_finding, lint_note, other],
                "blocking": 3, "lint": {"problems": []},
                "notes": {"skipped": False, "checked": 1, "honored": 0,
                          "unverified": 0, "verdicts": []}}

    monkeypatch.setattr(harness_mod, "evaluate_world", fake_eval)
    budgets = {"max_turns": 40, "max_tool_calls": 60, "fix_rounds": 3}
    # Every finding far beyond the fix-round budget.
    handle.finding_rounds = {"note:n2:-:-": 99, "lint:note_unbound:-:n9": 99,
                             "critique:tone:-:-": 99}

    result, rejection = run(harness_mod._done_gate(
        handle, {"summary": "done"}, budgets))
    # The generic finding auto-accepted; both note obligations still block.
    assert result is None
    blocked = {f["key"] for f in rejection["blocking_findings"]}
    assert blocked == {"note:n2:-:-", "lint:note_unbound:-:n9"}

    # Explicit acceptance with a note passes the gate.
    result, rejection = run(harness_mod._done_gate(
        handle, {"summary": "done",
                 "accept_findings": ["note:n2:-:-", "lint:note_unbound:-:n9"],
                 "note": "the user's subject is unbuildable at this scale"},
        budgets))
    assert rejection is None
    accepted = {a["key"]: a for a in result["accepted_findings"]}
    assert accepted["note:n2:-:-"]["auto"] is False
    assert accepted["critique:tone:-:-"]["auto"] is True
