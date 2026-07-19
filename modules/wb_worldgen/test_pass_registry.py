"""Tests for the enrichment pass registry (B1 of the worldgen architecture
plan): PassSpec registration and loud unknown-id failure (P1/P7), node-vs-map
scheduling, trigger firing only for completed maps, batching gated on
``batchable``, a synthetic extra pass running with zero engine edits (P2),
and an event-stream compatibility assertion (a built-ins run emits the same
SSE sequence as before the registry existed).

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_pass_registry.py
"""

import asyncio
import copy
import json
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder
from wbworldgen.worldgen.enrichment import registry
from wbworldgen.worldgen.enrichment.registry import (
    PassSpec,
    register_pass,
    unregister_pass,
)
from wbworldgen.worldgen.enrichment.passes import describe as describe_pass
from wbworldgen.worldgen.enrichment.passes import label as label_pass
from wbworldgen.worldgen.enrichment.passes import review as review_pass


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_passes_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    wb._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot")
    return wb


def _map_world(builder, n_nodes=6, world_id="pass_world", named=False):
    """A flat single-layer world with n_nodes in strictly decreasing
    importance; ``named`` pre-labels every node (for passes needing names)."""
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": n_nodes - i,
         "x": float(i), "y": 0.0,
         "name": f"Place {i}" if named else "",
         "description": "", "region": ""}
        for i in range(n_nodes)
    ]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(n_nodes - 1)]
    return builder.save_world(world_id, {
        "seed_prompt": "test",
        "steps": {"map_generation": {"data": {"nodes": nodes, "edges": edges}, "approved": True}},
    })


# ---------------------------------------------------------------------------
# Registration + loud failure (P1/P7)
# ---------------------------------------------------------------------------

def test_builtins_registered_in_order():
    assert [s.id for s in registry.registered_passes()] == ["label", "describe", "review"]
    # phase="all" runs the non-triggered passes in dependency order; review
    # is trigger-fired and excluded.
    assert registry.phase_pass_ids() == ["label", "describe"]


def test_duplicate_registration_fails_loudly():
    with pytest.raises(ValueError, match="already registered"):
        register_pass(registry.get_pass("label"))


def test_unknown_pass_id_fails_loudly(builder):
    with pytest.raises(ValueError, match="Unknown enrichment pass"):
        registry.get_pass("bogus")
    wid = _map_world(builder, 2)
    with pytest.raises(ValueError, match="Unknown enrichment pass"):
        asyncio.run(builder.enrich_run(wid, phase="bogus"))


def test_spec_validation_rejects_incomplete_specs():
    async def noop(services, unit, state):
        return None

    with pytest.raises(ValueError, match="unit"):
        PassSpec(id="x", label="x", description="x", unit="galaxy", run=noop,
                 is_done=lambda n: True)
    with pytest.raises(ValueError, match="is_done"):
        PassSpec(id="x", label="x", description="x", unit="node", run=noop)
    with pytest.raises(ValueError, match="run are required"):
        PassSpec(id="x", label="x", description="x", unit="node", run=None,
                 is_done=lambda n: True)
    with pytest.raises(ValueError, match="run_batch"):
        PassSpec(id="x", label="x", description="x", unit="node", run=noop,
                 is_done=lambda n: True, batchable=True)


# ---------------------------------------------------------------------------
# A synthetic pass schedules with zero engine edits (P2)
# ---------------------------------------------------------------------------

def test_synthetic_node_pass_runs_without_engine_edits(builder):
    ran = []

    async def run_history(services, node, state):
        ran.append(node["id"])
        return {"history_note": f"chronicle of {node['id']}"}

    register_pass(PassSpec(
        id="history", label="History", description="test-only history pass",
        unit="node", run=run_history,
        is_done=lambda n: bool(n.get("history_note")),
        in_domain=lambda n: bool(n.get("name")),
        after=("describe",)))
    try:
        # Registered passes join the catalog and the "all" ordering.
        assert registry.phase_pass_ids() == ["label", "describe", "history"]

        wid = _map_world(builder, 3, named=True)
        events = []

        async def on_event(evt):
            events.append(copy.deepcopy(evt))

        summary = asyncio.run(builder.enrich_run(wid, phase="history", on_event=on_event))

        assert summary["history"] == 3
        assert ran == ["n0", "n1", "n2"]  # importance order
        assert events[0]["type"] == "phase" and events[0]["phase"] == "history"
        assert events[0]["total_nodes"] == 3  # domain: named nodes
        # The engine stored the pass's field generically: enrichment store +
        # compiled cache both see it.
        assert builder.get_map_node(wid, "n0")["history_note"] == "chronicle of n0"

        # Idempotent: a second run finds no pending work.
        again = asyncio.run(builder.enrich_run(wid, phase="history"))
        assert again["history"] == 0
    finally:
        unregister_pass("history")


# ---------------------------------------------------------------------------
# Node-vs-map scheduling + trigger firing
# ---------------------------------------------------------------------------

def test_trigger_fires_only_when_a_map_completes(builder, monkeypatch):
    wid = _map_world(builder, 4)
    builder._enrichment_batch_size = 1
    reviewed = []

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        return f"Name {node['id']}", ""

    async def fake_review_map(services, rec, state):
        reviewed.append(rec.get("map_id"))
        return {"reviewed_maps": 1, "flagged": 0, "relabeled": []}

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    monkeypatch.setattr(review_pass, "review_map", fake_review_map)

    # Partial run: the map's naming is not complete — no review.
    partial = asyncio.run(builder.enrich_run(wid, phase="label", count=2))
    assert partial["labeled"] == 2
    assert reviewed == []
    assert "review" not in partial

    # Finishing run: naming completes, the trigger fires exactly once.
    full = asyncio.run(builder.enrich_run(wid, phase="label"))
    assert full["labeled"] == 2
    assert len(reviewed) == 1
    assert full["review"] == {"reviewed_maps": 1, "flagged": 0, "relabeled": []}


def test_map_pass_runs_standalone_via_phase(builder, monkeypatch):
    wid = _map_world(builder, 3, named=True)
    seen = []

    async def fake_review_map(services, rec, state):
        seen.append(rec.get("map_id"))
        return {"reviewed_maps": 1, "flagged": 0, "relabeled": []}

    monkeypatch.setattr(review_pass, "review_map", fake_review_map)
    events = []

    async def on_event(evt):
        events.append(copy.deepcopy(evt))

    summary = asyncio.run(builder.enrich_run(wid, phase="review", on_event=on_event))

    assert seen == ["root"]
    assert summary["review"] == {"reviewed_maps": 1, "flagged": 0, "relabeled": []}
    # Explicit map phases announce themselves; map passes count maps.
    assert events[0] == {"type": "phase", "phase": "review", "pending": 1,
                         "total_labeled": 0, "total_nodes": 1, "per_layer": {}}


# ---------------------------------------------------------------------------
# Batching is gated on the spec
# ---------------------------------------------------------------------------

def test_batching_only_for_batchable_specs(builder, monkeypatch):
    wid = _map_world(builder, 5, named=True)
    calls = []

    async def fake_desc(services, node, context, existing_description=""):
        calls.append(node["id"])
        return f"Flavor for {node['id']}"

    monkeypatch.setattr(describe_pass, "generate_description", fake_desc)

    # Batch size stays at the default (8), but describe is not batchable:
    # one call per node, never a shared one.
    summary = asyncio.run(builder.enrich_run(wid, phase="describe"))
    assert summary["described"] == 5
    assert sorted(calls) == [f"n{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# Event-stream compatibility: the built-ins emit the pre-B1 sequence
# ---------------------------------------------------------------------------

def test_builtin_run_emits_pre_b1_event_sequence(builder, monkeypatch):
    wid = _map_world(builder, 4)
    builder._enrichment_batch_size = 1
    builder._enrichment_concurrency = 1  # deterministic event order

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        return f"Name {node['id']}", f"snippet {node['id']}"

    async def fake_desc(services, node, context, existing_description=""):
        return f"Flavor text for {node['name']}"

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    monkeypatch.setattr(describe_pass, "generate_description", fake_desc)

    events = []

    async def on_event(evt):
        # Snapshot at emit time, exactly like the SSE serializer does —
        # phase events carry live progress dicts that mutate afterwards.
        events.append(json.loads(json.dumps(evt)))

    summary = asyncio.run(builder.enrich_run(wid, phase="all", on_event=on_event))

    assert [e["type"] for e in events] == (
        ["phase"] + ["node"] * 4 + ["phase"] + ["node"] * 4 + ["done"])

    label_phase = events[0]
    assert label_phase == {"type": "phase", "phase": "label", "pending": 4,
                           "total_labeled": 0, "total_nodes": 4,
                           "per_layer": {"root": {"done": 0, "total": 4}}}
    first_label = events[1]
    assert set(first_label) == {"type", "phase", "node_id", "layer_id", "label",
                                "label_description", "total_labeled",
                                "total_nodes", "per_layer"}
    assert first_label["phase"] == "label" and first_label["node_id"] == "n0"
    assert first_label["label"] == "Name n0" and first_label["total_labeled"] == 1

    describe_phase = events[5]
    assert describe_phase["phase"] == "describe"
    assert describe_phase["total_nodes"] == 4  # every node named by then
    first_desc = events[6]
    assert set(first_desc) == {"type", "phase", "node_id", "layer_id",
                               "description", "total_labeled", "total_nodes",
                               "per_layer"}
    assert first_desc["description"] == "Flavor text for Name n0"

    done = events[-1]
    assert done["labeled"] == 4 and done["described"] == 4
    assert done["failed_node_ids"] == [] and done["cancelled"] is False
    # The label phase completed the map, so the review trigger fired; with no
    # reviewer LLM wired it degrades to a zero summary — same as pre-B1.
    assert done["review"] == {"reviewed_maps": 0, "flagged": 0, "relabeled": []}
    assert summary["labeled"] == 4 and summary["described"] == 4
