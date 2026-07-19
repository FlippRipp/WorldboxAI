"""Tests for the server-driven enrichment run: bounded concurrency, batched
labeling, flush cadence, cancellation, the compiled-world cache, the SSE route
and the skip_review terrain pre-warm.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_enrichment_run.py
"""

import asyncio
import json
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder
from wbworldgen.worldgen.enrichment.context import collect_nodes_by_layer
from wbworldgen.worldgen.enrichment.passes import describe as describe_pass
from wbworldgen.worldgen.enrichment.passes import label as label_pass


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_enrich_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    wb._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot")
    return wb


def _map_world(builder, n_nodes=6, world_id="run_world"):
    """Persist a flat single-layer world whose map has n_nodes unenriched nodes
    in strictly decreasing importance (n0 is the most important)."""
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": n_nodes - i,
         "x": float(i), "y": 0.0, "name": "", "description": "", "region": ""}
        for i in range(n_nodes)
    ]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(n_nodes - 1)]
    return builder.save_world(world_id, {
        "seed_prompt": "test",
        "steps": {"map_generation": {"data": {"nodes": nodes, "edges": edges}, "approved": True}},
    })


# ---------------------------------------------------------------------------
# engine.run — phases, concurrency, ordering, failures, flushes, cancellation
# ---------------------------------------------------------------------------

def test_run_all_labels_then_describes(builder, monkeypatch):
    wid = _map_world(builder, 6)
    builder._enrichment_batch_size = 1  # single-node path for this test
    calls = {"label": [], "desc": []}
    active = {"now": 0, "max": 0}

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1
        calls["label"].append(node["id"])
        return f"Name {node['id']}", f"snippet {node['id']}"

    async def fake_desc(services, node, context, existing_description=""):
        calls["desc"].append(node["id"])
        return f"Flavor text for {node['name']}"

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    monkeypatch.setattr(describe_pass, "generate_description", fake_desc)

    events = []

    async def on_event(evt):
        events.append(evt)

    summary = asyncio.run(builder.enrich_run(wid, phase="all", on_event=on_event))

    assert summary["labeled"] == 6
    assert summary["described"] == 6
    assert summary["failed_node_ids"] == []
    # Work is dispatched by descending importance.
    assert set(calls["label"][:3]) == {"n0", "n1", "n2"}
    assert set(calls["desc"][:3]) == {"n0", "n1", "n2"}
    # Bounded concurrency: overlapped, but never above the default of 3.
    assert 2 <= active["max"] <= 3
    # Everything persisted.
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert all(n["name"] and n["description"] for n in nodes)
    # Event stream shape.
    kinds = [e["type"] for e in events]
    assert kinds.count("phase") == 2 and kinds[-1] == "done"
    node_events = [e for e in events if e["type"] == "node"]
    assert len(node_events) == 12
    assert all("per_layer" in e and "total_labeled" in e for e in node_events)


def test_run_partial_failure_continues(builder, monkeypatch):
    wid = _map_world(builder, 4)
    builder._enrichment_batch_size = 1

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        if node["id"] == "n1":
            raise ValueError("boom")
        return f"Name {node['id']}", ""

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    events = []

    async def on_event(evt):
        events.append(evt)

    summary = asyncio.run(builder.enrich_run(wid, phase="label", on_event=on_event))

    assert summary["labeled"] == 3
    assert summary["failed_node_ids"] == ["n1"]
    assert any(e["type"] == "failed" and e["node_id"] == "n1" for e in events)
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert sum(1 for n in nodes if n["name"]) == 3


def test_run_cancel_stops_midway(builder, monkeypatch):
    wid = _map_world(builder, 12)
    builder._enrichment_batch_size = 1
    builder._enrichment_concurrency = 1

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        await asyncio.sleep(0.005)
        return f"Name {node['id']}", ""

    monkeypatch.setattr(label_pass, "generate_label", fake_label)

    async def main():
        seen = []

        async def on_event(evt):
            if evt["type"] == "node":
                seen.append(evt)
                if len(seen) == 2:
                    builder.enrich_cancel(wid)

        return await builder.enrich_run(wid, phase="label", on_event=on_event)

    summary = asyncio.run(main())
    assert summary["cancelled"] is True
    assert 0 < summary["labeled"] < 12
    # Finished nodes were flushed despite the cancel.
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert sum(1 for n in nodes if n["name"]) == summary["labeled"]


def test_run_flushes_every_ten_not_every_node(builder, monkeypatch):
    wid = _map_world(builder, 25)
    builder._enrichment_batch_size = 1

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        return f"Name {node['id']}", ""

    monkeypatch.setattr(label_pass, "generate_label", fake_label)

    writes = []
    orig = builder._persistence.write_enrichment_to_disk

    def counting(world_id, evict=False):
        writes.append(world_id)
        return orig(world_id, evict=evict)

    monkeypatch.setattr(builder._persistence, "write_enrichment_to_disk", counting)

    summary = asyncio.run(builder.enrich_run(wid, phase="label"))
    assert summary["labeled"] == 25
    # Two cadence flushes (10, 20) + the end-of-phase flush — not one per node.
    assert 2 <= len(writes) <= 5
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert all(n["name"] for n in nodes)


def test_run_respects_count_and_exclude(builder, monkeypatch):
    wid = _map_world(builder, 6)
    builder._enrichment_batch_size = 1

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        return f"Name {node['id']}", ""

    monkeypatch.setattr(label_pass, "generate_label", fake_label)

    summary = asyncio.run(builder.enrich_run(
        wid, phase="label", count=2, exclude_node_ids=["n0"]))
    assert summary["labeled"] == 2
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    named = {n["id"] for n in nodes if n["name"]}
    # n0 excluded; the two most important remaining nodes were labeled.
    assert named == {"n1", "n2"}


# ---------------------------------------------------------------------------
# Batched labeling
# ---------------------------------------------------------------------------

def test_run_label_batch_partial_validation(builder, monkeypatch):
    wid = _map_world(builder, 4)

    async def fake_batch(services, batch, contexts, used_names):
        return {"nodes": [
            {"id": "n0", "name": "The Emberfall", "label_description": "d0"},  # strips to Emberfall
            {"id": "n1", "name": "Emberfall", "label_description": "dup"},     # duplicate after strip
            {"id": "n3", "name": "", "label_description": ""},                 # empty name
            {"id": "zz", "name": "Ghost", "label_description": ""},            # id never requested
        ]}  # n2 missing entirely

    monkeypatch.setattr(label_pass, "generate_label_batch", fake_batch)
    compiled = builder._enrichment._load_compiled(wid)
    all_nodes, _ = collect_nodes_by_layer(compiled)

    results, leftovers = asyncio.run(
        label_pass.run_label_batch(builder._services, all_nodes, all_nodes, compiled, []))

    assert results == {"n0": {"name": "Emberfall", "label_description": "d0"}}
    assert {n["id"] for n in leftovers} == {"n1", "n2", "n3"}


def test_run_label_batch_failure_bisects_then_singles(builder, monkeypatch):
    wid = _map_world(builder, 8)
    attempts = []

    async def fake_batch(services, batch, contexts, used_names):
        attempts.append(len(batch))
        raise ValueError("malformed json")

    monkeypatch.setattr(label_pass, "generate_label_batch", fake_batch)
    compiled = builder._enrichment._load_compiled(wid)
    all_nodes, _ = collect_nodes_by_layer(compiled)

    results, leftovers = asyncio.run(
        label_pass.run_label_batch(builder._services, all_nodes, all_nodes, compiled, []))

    assert results == {}
    assert len(leftovers) == 8  # everything falls back to single-node calls
    assert attempts == [8, 4, 4]  # full batch, then one bisect of each half


def test_run_batched_labels_end_to_end(builder, monkeypatch):
    wid = _map_world(builder, 10)
    builder._enrichment_batch_size = 8
    batch_sizes = []

    async def fake_batch(services, batch, contexts, used_names):
        batch_sizes.append(len(batch))
        return {"nodes": [
            {"id": n["id"], "name": f"Uniq {n['id']}", "label_description": "x"}
            for n in batch
        ]}

    monkeypatch.setattr(label_pass, "generate_label_batch", fake_batch)

    summary = asyncio.run(builder.enrich_run(wid, phase="label"))
    assert summary["labeled"] == 10
    assert sorted(batch_sizes) == [2, 8]
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert len({n["name"] for n in nodes}) == 10


def test_live_label_prompt_carries_containment_rule(builder):
    # A single labeling call must know (a) part-of names require the parent to
    # be an actual neighbor, and (b) which names exist elsewhere on the map —
    # otherwise "School Rooftop" gets invented far from the school.
    wid = _map_world(builder, 3)
    captured = {}

    async def fake_completion(messages=None, **kwargs):
        captured["messages"] = messages
        return json.dumps({"name": "Mill Row", "label_description": "d"})

    builder._llm_service.simple_completion = fake_completion
    compiled = builder._enrichment._load_compiled(wid)
    all_nodes, _ = collect_nodes_by_layer(compiled)
    from wbworldgen.worldgen.enrichment.context import build_enrichment_context
    node = all_nodes[0]
    ctx = build_enrichment_context(node, all_nodes, compiled)

    name, _snippet = asyncio.run(
        label_pass.generate_label(builder._services, node, ctx, ["Northgate School"]))

    assert name == "Mill Row"
    system = captured["messages"][0]["content"]
    assert "standalone place" in system
    assert "Nearby nodes" in system
    assert "Northgate School" in system
    assert "part or sub-location" in system


def test_live_label_batch_prompt_far_apart_rule_and_full_avoid_list(builder):
    wid = _map_world(builder, 4)
    captured = {}

    async def fake_completion(messages=None, **kwargs):
        captured["messages"] = messages
        return json.dumps({"nodes": []})

    builder._llm_service.simple_completion = fake_completion
    compiled = builder._enrichment._load_compiled(wid)
    all_nodes, _ = collect_nodes_by_layer(compiled)
    contexts = {n["id"]: {} for n in all_nodes}
    # 45 used names: the old prompt truncated to the last 40, hiding the first.
    used = [f"Oldtown {i}" for i in range(45)]

    asyncio.run(label_pass.generate_label_batch(builder._services, all_nodes, contexts, used))

    system = captured["messages"][0]["content"]
    assert "far apart on the map" in system
    assert "standalone place" in system
    user = captured["messages"][1]["content"]
    assert "Oldtown 0" in user and "Oldtown 44" in user  # no truncation
    assert "part or sub-location" in user


# ---------------------------------------------------------------------------
# Compiled-world cache (single-node runs)
# ---------------------------------------------------------------------------

def test_single_node_runs_reuse_compiled_and_see_prior_labels(builder, monkeypatch):
    wid = _map_world(builder, 3)
    builder._enrichment_batch_size = 1
    names = iter(["Alpha", "Beta", "Gamma"])
    labeled = []

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        labeled.append(node["id"])
        return next(names), ""

    monkeypatch.setattr(label_pass, "generate_label", fake_label)

    loads = []
    real_load = WorldBuilder.load_world

    def counting_load(world_id):
        loads.append(world_id)
        return real_load(builder, world_id)

    builder.load_world = counting_load

    for _ in range(3):
        summary = asyncio.run(builder.enrich_run(wid, phase="label", count=1))
        assert summary["labeled"] == 1
    done = asyncio.run(builder.enrich_run(wid, phase="label", count=1))

    assert labeled == ["n0", "n1", "n2"]  # each run advanced; no repeats
    assert done["labeled"] == 0  # nothing left to label
    assert len(loads) == 1  # world read from disk once, then served from cache


def test_save_step_invalidates_compiled_cache(builder, monkeypatch):
    wid = _map_world(builder, 2)
    builder._enrichment_batch_size = 1

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        return f"Name {node['id']}", ""

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    asyncio.run(builder.enrich_run(wid, phase="label", count=1))
    assert wid in builder._compiled

    step_data = builder.load_world(wid)["steps"]["map_generation"]
    builder.save_step(wid, "map_generation", step_data)
    assert wid not in builder._compiled


# ---------------------------------------------------------------------------
# Routes: SSE enrichment stream + skip_review terrain pre-warm
# ---------------------------------------------------------------------------

def test_enrich_run_route_streams_sse_and_syncs_draft():
    import routes as world_routes

    class FakeBuilder:
        def default_importance_floor(self):
            return None

        async def enrich_run(self, world_id, phase="all", count=None, layer_filter=None,
                             rework=False, exclude_node_ids=None, on_event=None,
                             importance_floor=None, node_ids=None):
            await on_event({"type": "phase", "phase": "label", "pending": 1,
                            "total_labeled": 0, "total_nodes": 1, "per_layer": {}})
            await on_event({"type": "node", "phase": "label", "node_id": "n1",
                            "label": "Emberhold", "label_description": "snippet",
                            "layer_id": "", "total_labeled": 1, "total_nodes": 1,
                            "per_layer": {}})
            await on_event({"type": "done", "labeled": 1, "described": 0,
                            "failed_node_ids": [], "cancelled": False})
            return {"labeled": 1, "described": 0, "failed_node_ids": [], "cancelled": False}

        def sync_enrichment_to_map_state(self, map_data, node_map):
            for n in map_data.get("nodes", []):
                if n["id"] in node_map:
                    n.update(node_map[n["id"]])

    old_builder = world_routes.world_builder
    world_routes.world_builder = FakeBuilder()
    world_routes.world_gen_sessions["sse_test"] = {
        "steps": {"map_generation": {"data": {"nodes": [{"id": "n1", "name": ""}]},
                                     "approved": False}},
    }
    world_routes.world_draft_ids["sse_test"] = "wid1"
    try:
        async def main():
            resp = await world_routes.enrich_run(
                "wid1", world_routes.EnrichRunRequest(phase="all"), session_id="sse_test")
            assert resp.media_type == "text/event-stream"
            chunks = []
            async for chunk in resp.body_iterator:
                chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
            return "".join(chunks)

        body = asyncio.run(main())
        frames = [json.loads(block[len("data: "):])
                  for block in body.strip().split("\n\n")]
        assert [f["type"] for f in frames] == ["phase", "node", "done"]
        # The node event was mirrored into the in-memory draft.
        synced = world_routes.world_gen_sessions["sse_test"]["steps"]["map_generation"]["data"]["nodes"][0]
        assert synced["name"] == "Emberhold"
    finally:
        world_routes.world_builder = old_builder
        world_routes.world_gen_sessions.pop("sse_test", None)
        world_routes.world_draft_ids.pop("sse_test", None)


def test_skip_review_prewarms_terrain_during_layer_rules():
    import routes as world_routes

    class FakeBuilder:
        def __init__(self):
            self._ordered_ids = ["world_rules", "lore", "layer_design", "layer_rules",
                                 "terrain_generation", "terrain_regions", "map_generation",
                                 "node_labeling", "node_descriptions"]
            self._steps = {sid: object() for sid in self._ordered_ids}
            self.timeline = {}

        async def generate_step(self, step_id, state, prompt, user_note="", config=None):
            loop = asyncio.get_running_loop()
            self.timeline[step_id] = {"start": loop.time()}
            if step_id == "terrain_generation":
                assert state.get("_draft_id"), "_draft_id must be pinned before the pre-warm"
                await asyncio.sleep(0.03)
            elif step_id == "layer_rules":
                await asyncio.sleep(0.03)
            else:
                await asyncio.sleep(0.001)
            self.timeline[step_id]["end"] = loop.time()
            return {"step": step_id}

    fake = FakeBuilder()
    old_builder = world_routes.world_builder
    world_routes.world_builder = fake
    try:
        resp = asyncio.run(world_routes.generate_world(
            world_routes.WorldGenerateRequest(seed_prompt="seed", skip_review=True),
            session_id="prewarm_test"))
    finally:
        world_routes.world_builder = old_builder
        world_routes.world_gen_sessions.pop("prewarm_test", None)

    assert resp["complete"] is True
    generated = set(resp["state"]["steps"])
    assert generated == set(fake._ordered_ids) - {"node_labeling", "node_descriptions"}
    # Terrain ran concurrently with the layer_rules "LLM call": it started
    # before layer_rules finished instead of waiting its turn in the chain.
    assert fake.timeline["terrain_generation"]["start"] < fake.timeline["layer_rules"]["end"]
    # And downstream steps only started after terrain completed.
    assert fake.timeline["terrain_regions"]["start"] >= fake.timeline["terrain_generation"]["end"]


def test_generating_flag_visible_to_polling_clients():
    """While a step generates, /api/world/state (the same session dict) carries
    ``_generating`` + ``skip_review`` so a relaunched client (Android killed
    the PWA mid-run) can restore the wizard and poll; both the review and
    one-shot paths clear the flag when the run ends."""
    import routes as world_routes

    class FakeBuilder:
        def __init__(self):
            self._ordered_ids = ["world_rules", "lore"]
            self._steps = {sid: object() for sid in self._ordered_ids}
            self.mid_generation = {}

        async def generate_step(self, step_id, state, prompt, user_note="", config=None):
            # Snapshot what a concurrent poll of the session state would see.
            self.mid_generation[step_id] = dict(state)
            await asyncio.sleep(0)
            return {"step": step_id}

    fake = FakeBuilder()
    old_builder = world_routes.world_builder
    world_routes.world_builder = fake
    try:
        resp = asyncio.run(world_routes.generate_world(
            world_routes.WorldGenerateRequest(seed_prompt="p"), session_id="gen_flag"))
        assert fake.mid_generation["world_rules"]["_generating"] == "world_rules"
        assert fake.mid_generation["world_rules"]["skip_review"] is False
        assert "_generating" not in resp["state"]

        # Reroll route flags the step it is regenerating.
        resp2 = asyncio.run(world_routes.generate_world_step("lore", session_id="gen_flag"))
        assert fake.mid_generation["lore"]["_generating"] == "lore"
        assert "_generating" not in resp2["state"]

        # Approve flags the NEXT step while it generates.
        fake.mid_generation.clear()
        resp3 = asyncio.run(world_routes.approve_world_step("world_rules", session_id="gen_flag"))
        assert fake.mid_generation["lore"]["_generating"] == "lore"
        assert "_generating" not in resp3["state"]

        # One-shot mode flags "all" for the whole run.
        resp4 = asyncio.run(world_routes.generate_world(
            world_routes.WorldGenerateRequest(seed_prompt="p", skip_review=True),
            session_id="gen_flag_all"))
        assert fake.mid_generation["world_rules"]["_generating"] == "all"
        assert fake.mid_generation["world_rules"]["skip_review"] is True
        assert resp4["complete"] is True
        assert "_generating" not in resp4["state"]
    finally:
        world_routes.world_builder = old_builder
        world_routes.world_gen_sessions.pop("gen_flag", None)
        world_routes.world_gen_sessions.pop("gen_flag_all", None)


# ---------------------------------------------------------------------------
# Route: LLM-as-author world prompt rewrite
# ---------------------------------------------------------------------------

def test_rewrite_world_prompt_route():
    import routes as world_routes

    captured = {}

    async def fake_completion(messages, model=None, response_format=None, inspector_ctx=None):
        captured["messages"] = messages
        captured["model"] = model
        return json.dumps({"text": "A drowned city of rival guilds."})

    fake_engine = types.SimpleNamespace(
        llm=types.SimpleNamespace(storyteller_model="fake/model",
                                  simple_completion=fake_completion))
    old_engine = world_routes.engine
    world_routes.engine = fake_engine
    try:
        resp = asyncio.run(world_routes.rewrite_world_prompt(
            world_routes.RewriteWorldPromptRequest(instruction="a drowned city")))
        assert resp["text"] == "A drowned city of rival guilds."
        assert captured["model"] == "fake/model"
        assert "a drowned city" in captured["messages"][1]["content"]

        # Nothing to work from → 400, no LLM call.
        with pytest.raises(Exception) as exc:
            asyncio.run(world_routes.rewrite_world_prompt(
                world_routes.RewriteWorldPromptRequest()))
        assert getattr(exc.value, "status_code", None) == 400
    finally:
        world_routes.engine = old_engine


def test_world_prompt_questions_route():
    import routes as world_routes

    captured = {}

    async def fake_completion(messages, model=None, response_format=None, inspector_ctx=None):
        captured["messages"] = messages
        captured["model"] = model
        return json.dumps({"questions": ["What era is it?", "  ", "Who holds power?"]})

    fake_engine = types.SimpleNamespace(
        llm=types.SimpleNamespace(storyteller_model="fake/model",
                                  simple_completion=fake_completion))
    old_engine = world_routes.engine
    world_routes.engine = fake_engine
    try:
        # Empty prompt is allowed — the interview works from scratch. Blank
        # questions are dropped from the response.
        resp = asyncio.run(world_routes.world_prompt_questions(
            world_routes.WorldPromptQuestionsRequest()))
        assert resp["questions"] == ["What era is it?", "Who holds power?"]
        assert captured["model"] == "fake/model"

        # History rides along so the model never repeats itself.
        asyncio.run(world_routes.world_prompt_questions(
            world_routes.WorldPromptQuestionsRequest(
                current_text="A drowned city.",
                history=[{"question": "What era is it?", "answer": "Late medieval."}])))
        user = captured["messages"][1]["content"]
        assert "A drowned city." in user
        assert "What era is it?" in user and "Late medieval." in user
    finally:
        world_routes.engine = old_engine


def test_fold_world_answers_route():
    import routes as world_routes

    captured = {}

    async def fake_completion(messages, model=None, response_format=None, inspector_ctx=None):
        captured["messages"] = messages
        return json.dumps({"text": "A late-medieval drowned city of rival guilds."})

    fake_engine = types.SimpleNamespace(
        llm=types.SimpleNamespace(storyteller_model="fake/model",
                                  simple_completion=fake_completion))
    old_engine = world_routes.engine
    world_routes.engine = fake_engine
    try:
        resp = asyncio.run(world_routes.fold_world_answers(
            world_routes.FoldWorldAnswersRequest(
                current_text="A drowned city of rival guilds.",
                answers=[{"question": "What era?", "answer": "Late medieval."},
                         {"question": "Any magic?", "answer": "  "}])))
        assert resp["text"] == "A late-medieval drowned city of rival guilds."
        user = captured["messages"][1]["content"]
        assert "A drowned city of rival guilds." in user
        assert "Late medieval." in user
        # The blank answer was dropped before it reached the LLM.
        assert "Any magic?" not in user

        # All answers skipped → 400, no LLM call.
        with pytest.raises(Exception) as exc:
            asyncio.run(world_routes.fold_world_answers(
                world_routes.FoldWorldAnswersRequest(
                    current_text="x", answers=[{"question": "Q?", "answer": ""}])))
        assert getattr(exc.value, "status_code", None) == 400
    finally:
        world_routes.engine = old_engine
