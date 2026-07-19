"""Tests for the C1 agent toolbox: the ToolSpec registry (the fourth
catalog), argument validation, the per-action B3 precondition checks, the
v1 tools over a real WorldBuilder, the run-level guidance channel, and the
deterministic lint report over fixture worlds with known defects.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_agent_toolbox.py
"""

import asyncio
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import (
    ToolContext,
    ToolError,
    ToolSpec,
    describe_tools,
    invoke_tool,
    lint_world,
    register_tool,
    unregister_tool,
)
from wbworldgen.worldgen.agent.tools import build as build_tools
from wbworldgen.worldgen.catalog import (
    capability_catalog,
    produced_artifacts,
    render_catalog_markdown,
)
from wbworldgen.worldgen.enrichment.passes import describe as describe_pass
from wbworldgen.worldgen.enrichment.passes import label as label_pass


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_agent_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    """Builder with a live-mode fake LLM handle: enrichment runs are allowed
    and the pass-module functions get monkeypatched per test."""
    wb = register_default_steps(WorldBuilder(worlds_dir=tmpdir))
    wb._llm_service = types.SimpleNamespace(
        mode="live", module_fast_model="fast-slot", reader_model="reader-slot")
    return wb


@pytest.fixture
def mock_builder(tmpdir):
    """Builder with no LLM wired: generate_step takes the mock path."""
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


def _map_world(builder, n_nodes=6, world_id="agent_world", named=False):
    """Flat single-map world with n_nodes nodes in strictly decreasing
    importance (n0 most important, importance n_nodes..1), chained by edges."""
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": n_nodes - i,
         "x": float(i), "y": 0.0,
         "name": f"Town {i}" if named else "",
         "description": "", "region": ""}
        for i in range(n_nodes)
    ]
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(n_nodes - 1)]
    return builder.save_world(world_id, {
        "seed_prompt": "test world",
        "steps": {"map_generation": {"data": {"nodes": nodes, "edges": edges}, "approved": True}},
    })


def _ctx(builder, world_id):
    return ToolContext(builder=builder, world_id=world_id)


# ---------------------------------------------------------------------------
# Registry: registration, lookup, argument validation (P1/P7)
# ---------------------------------------------------------------------------

def test_register_duplicate_tool_id_fails():
    async def noop(ctx):
        return {}

    spec = ToolSpec(id="tmp_tool", label="Tmp", description="temp", invoke=noop)
    register_tool(spec)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_tool(ToolSpec(id="tmp_tool", label="Tmp2", description="x", invoke=noop))
    finally:
        unregister_tool("tmp_tool")


def test_invoke_unknown_tool_lists_registered(builder):
    with pytest.raises(ToolError) as err:
        run(invoke_tool(_ctx(builder, "w"), "no_such_tool"))
    assert "Unknown tool 'no_such_tool'" in str(err.value)
    assert "read_world" in str(err.value)  # the observation teaches the agent


def test_invoke_rejects_undeclared_and_missing_args(builder):
    with pytest.raises(ToolError, match="unknown argument 'bogus'"):
        run(invoke_tool(_ctx(builder, "w"), "read_map", {"bogus": 1}))
    with pytest.raises(ToolError, match="missing required argument 'map_id'"):
        run(invoke_tool(_ctx(builder, "w"), "read_map", {}))


def test_invoke_rejects_ill_typed_args(builder):
    wid = _map_world(builder)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="'count' must be a integer"):
        run(invoke_tool(ctx, "run_pass", {"pass_id": "label", "count": "three"}))
    with pytest.raises(ToolError, match="'rework' must be a boolean"):
        run(invoke_tool(ctx, "run_pass", {"pass_id": "label", "rework": "yes"}))
    with pytest.raises(ToolError, match="list of strings"):
        run(invoke_tool(ctx, "run_pass", {"pass_id": "label", "node_ids": [1, 2]}))
    with pytest.raises(ToolError, match="must be >= 0"):
        run(invoke_tool(ctx, "run_pass", {"pass_id": "label", "importance_floor": -2}))


def test_every_tool_is_self_describing():
    # P1: a tool IS its catalog entry — id, label, description, typed params.
    entries = describe_tools()
    assert len(entries) >= 11
    for entry in entries:
        assert entry["kind"] == "tool"
        assert entry["id"] and isinstance(entry["id"], str)
        assert entry["label"] and isinstance(entry["label"], str)
        assert entry["description"] and isinstance(entry["description"], str)
        assert isinstance(entry["mutates"], bool)
        for name, p in entry["params"].items():
            assert p.get("type") in ("string", "integer", "number", "boolean",
                                     "list", "object"), (entry["id"], name)
            assert p.get("description"), (entry["id"], name)


def test_catalog_includes_tools_section():
    cat = capability_catalog()
    tool_ids = [e["id"] for e in cat["tools"]]
    for expected in ("read_world", "read_map", "read_lint", "run_step",
                     "patch_step", "run_pass", "run_custom_pass", "edit_node"):
        assert expected in tool_ids
    doc = render_catalog_markdown(cat)
    assert "## Agent tools" in doc
    assert "**run_pass**" in doc
    assert "`pass_id` (string, required)" in doc
    assert "[read-only]" in doc and "[mutates world]" in doc


# ---------------------------------------------------------------------------
# produced_artifacts: the executor-side half of B3
# ---------------------------------------------------------------------------

def test_produced_artifacts_from_step_data():
    ws = {"steps": {
        "world_rules": {"data": {"genre": "noir"}, "approved": True},
        "hierarchy_design": {"data": {"levels": [{}]}, "approved": True},
        "terrain_generation": {"data": {}, "approved": True},  # seeded placeholder
    }}
    produced = produced_artifacts(ws)
    assert {"rules", "hierarchy"} <= produced
    assert "terrain" not in produced  # empty data is not the artifact
    assert "maps" not in produced


def test_produced_artifacts_from_pass_effects():
    ws = {"steps": {"map_generation": {"data": {"nodes": [{}]}, "approved": True}}}
    unnamed = {"maps": {"root": {"map_id": "root", "nodes": [
        {"id": "a", "name": "", "description": ""}]}}}
    named = {"maps": {"root": {"map_id": "root", "nodes": [
        {"id": "a", "name": "Keep", "description": ""}]}}}
    assert "labels" not in produced_artifacts(ws, unnamed)
    produced = produced_artifacts(ws, named)
    assert "labels" in produced and "descriptions" not in produced


# ---------------------------------------------------------------------------
# Lints: fixture worlds with known defects (D3)
# ---------------------------------------------------------------------------

def _compiled_fixture():
    """Two-map world, clean by construction: chain-connected nodes, a
    connection joining the maps, resolved link tokens."""
    return {
        "maps": {
            "root": {"map_id": "root", "label": "Overworld", "nodes": [
                {"id": "a", "name": "Ashford", "importance": 8,
                 "description": "A town near ${link_b|Bell Tower (east)}."},
                {"id": "b", "name": "Bell Tower", "importance": 6, "description": "Tall."},
                {"id": "c", "name": "Cinder Field", "importance": 2, "description": ""},
            ], "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]},
            "deeps": {"map_id": "deeps", "label": "The Deeps", "nodes": [
                {"id": "d", "name": "Drowned Gate", "importance": 7, "description": "Wet."},
                {"id": "e", "name": "Echo Vault", "importance": 3, "description": ""},
            ], "edges": [{"from": "d", "to": "e"}]},
        },
        "connections": [{"id": "c1", "from": {"map_id": "root", "node_id": "b"},
                         "to": {"map_id": "deeps", "node_id": "d"},
                         "kind": "portal", "bidirectional": True}],
    }


def test_lint_clean_world_is_clean():
    report = lint_world(_compiled_fixture())
    assert report["clean"] is True
    assert report["problems"] == []
    stats = {s["map_id"]: s for s in report["stats"]}
    assert stats["root"]["nodes"] == 3 and stats["root"]["named"] == 3
    assert stats["root"]["described"] == 2


def test_lint_duplicate_names_case_and_article_tolerant():
    compiled = _compiled_fixture()
    compiled["maps"]["deeps"]["nodes"][1]["name"] = "The ashford"
    report = lint_world(compiled)
    dups = [p for p in report["problems"] if p["kind"] == "duplicate_name"]
    assert len(dups) == 1
    ids = {n["node_id"] for n in dups[0]["nodes"]}
    assert ids == {"a", "e"}


def test_lint_orphan_and_connection_exemption():
    compiled = _compiled_fixture()
    compiled["maps"]["root"]["nodes"].append(
        {"id": "x", "name": "Lost Hut", "importance": 1, "description": ""})
    report = lint_world(compiled)
    orphan = [p for p in report["problems"] if p["kind"] == "orphan_node"]
    assert [p["node_id"] for p in orphan] == ["x"]
    # An edge-less node that a connection reaches is NOT an orphan.
    compiled["connections"].append(
        {"id": "c2", "from": {"map_id": "root", "node_id": "a"},
         "to": {"map_id": "root", "node_id": "x"}, "kind": "path"})
    report = lint_world(compiled)
    assert not [p for p in report["problems"] if p["kind"] == "orphan_node"]


def test_lint_disconnected_map_components():
    compiled = _compiled_fixture()
    compiled["maps"]["root"]["nodes"] += [
        {"id": "y1", "name": "Yard", "importance": 1, "description": ""},
        {"id": "y2", "name": "Yonder", "importance": 1, "description": ""},
    ]
    compiled["maps"]["root"]["edges"].append({"from": "y1", "to": "y2"})
    report = lint_world(compiled)
    disc = [p for p in report["problems"] if p["kind"] == "disconnected_map"]
    assert len(disc) == 1
    assert disc[0]["map_id"] == "root"
    assert sorted(disc[0]["component_sizes"], reverse=True) == [3, 2]


def test_lint_unreachable_map_vs_anchored_child():
    compiled = _compiled_fixture()
    compiled["connections"] = []  # sever the realms
    report = lint_world(compiled)
    unreachable = {p["map_id"] for p in report["problems"]
                   if p["kind"] == "unreachable_map"}
    assert unreachable == {"deeps"}  # the root itself never flags
    # A child map anchored in a parent node needs no connection.
    compiled["maps"]["deeps"]["parent_map_id"] = "root"
    compiled["maps"]["deeps"]["anchor_node_id"] = "b"
    report = lint_world(compiled)
    assert not [p for p in report["problems"] if p["kind"] == "unreachable_map"]


def test_lint_link_tokens_broken_and_unresolved():
    compiled = _compiled_fixture()
    compiled["maps"]["root"]["nodes"][1]["description"] = \
        "Past ${link_zzz} and ${link_c}."
    report = lint_world(compiled)
    broken = [p for p in report["problems"] if p["kind"] == "broken_link_token"]
    unresolved = [p for p in report["problems"] if p["kind"] == "unresolved_link_token"]
    assert broken and broken[0]["targets"] == ["zzz"] and broken[0]["node_id"] == "b"
    assert unresolved and unresolved[0]["targets"] == ["c"]
    # The clean fixture's resolved token never flags.
    assert not [p for p in lint_world(_compiled_fixture())["problems"]
                if "link_token" in p["kind"]]


def test_lint_dangling_edge_and_connection():
    compiled = _compiled_fixture()
    compiled["maps"]["root"]["edges"].append({"from": "a", "to": "ghost"})
    compiled["connections"].append(
        {"id": "c9", "from": {"map_id": "root", "node_id": "a"},
         "to": {"map_id": "nowhere", "node_id": "q"}, "kind": "rift"})
    report = lint_world(compiled)
    kinds = {p["kind"] for p in report["problems"]}
    assert "dangling_edge" in kinds and "dangling_connection" in kinds


def test_lint_major_coverage_floor():
    compiled = _compiled_fixture()
    compiled["maps"]["root"]["nodes"][0]["name"] = ""       # a: importance 8, unnamed
    compiled["maps"]["deeps"]["nodes"][0]["description"] = ""  # d: named, undescribed
    report = lint_world(compiled, major_floor=6)
    unnamed = [p for p in report["problems"] if p["kind"] == "unnamed_major_nodes"]
    undescribed = [p for p in report["problems"] if p["kind"] == "undescribed_major_nodes"]
    assert unnamed and unnamed[0]["node_ids"] == ["a"]
    assert undescribed and undescribed[0]["node_ids"] == ["d"]
    # Without a floor the coverage findings are off.
    kinds = {p["kind"] for p in lint_world(compiled)["problems"]}
    assert "unnamed_major_nodes" not in kinds
    stats = {s["map_id"]: s for s in report["stats"]}
    assert stats["root"]["majors"] == 2 and stats["root"]["majors_named"] == 1


def test_lint_map_scope():
    compiled = _compiled_fixture()
    compiled["maps"]["root"]["nodes"].append(
        {"id": "x", "name": "Lost Hut", "importance": 1, "description": ""})
    report = lint_world(compiled, map_id="deeps")
    assert report["clean"] is True            # root's orphan is out of scope
    assert [s["map_id"] for s in report["stats"]] == ["deeps"]


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def test_read_world_reports_steps_maps_artifacts(builder):
    wid = _map_world(builder, named=True)
    result = run(invoke_tool(_ctx(builder, wid), "read_world"))
    steps = {s["id"]: s for s in result["steps"]}
    assert steps["map_generation"]["present"] is True
    assert steps["world_rules"]["present"] is False
    assert result["maps"] == [{"map_id": "root", "label": "World",
                               "level_type": "world", "nodes": 6, "named": 6,
                               "described": 0, "detailed": 0}]
    assert "maps" in result["artifacts"] and "labels" in result["artifacts"]
    assert "rules" not in result["artifacts"]


def test_read_step_serves_data_and_special_cases_maps(builder):
    wid = _map_world(builder)
    builder.save_step(wid, "world_rules", {"data": {"genre": "noir"}, "approved": True})
    ctx = _ctx(builder, wid)
    result = run(invoke_tool(ctx, "read_step", {"step_id": "world_rules"}))
    assert result["data"] == {"genre": "noir"} and result["present"] is True
    result = run(invoke_tool(ctx, "read_step", {"step_id": "map_generation"}))
    assert "data" not in result and result["maps"][0]["nodes"] == 6
    with pytest.raises(ToolError, match="Unknown step 'wat'"):
        run(invoke_tool(ctx, "read_step", {"step_id": "wat"}))


def test_read_map_and_read_node(builder):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)
    result = run(invoke_tool(ctx, "read_map", {"map_id": "root"}))
    assert result["nodes"] == 6 and len(result["node_list"]) == 6
    assert result["edges"][0] == ["n0", "n1"]
    assert result["node_list"][0]["name"] == "Town 0"
    with pytest.raises(ToolError, match="Unknown map 'moon'"):
        run(invoke_tool(ctx, "read_map", {"map_id": "moon"}))

    result = run(invoke_tool(ctx, "read_node", {"node_id": "n1"}))
    assert result["node"]["name"] == "Town 1" and result["map_id"] == "root"
    assert {n["id"] for n in result["neighbors"]} == {"n0", "n2"}
    with pytest.raises(ToolError, match="Unknown node 'nope'"):
        run(invoke_tool(ctx, "read_node", {"node_id": "nope"}))


def test_read_lint_and_catalog_tools(builder):
    wid = _map_world(builder)  # unnamed majors -> findings
    ctx = _ctx(builder, wid)
    report = run(invoke_tool(ctx, "read_lint"))
    kinds = {p["kind"] for p in report["problems"]}
    assert "unnamed_major_nodes" in kinds
    with pytest.raises(ToolError, match="Unknown map"):
        run(invoke_tool(ctx, "read_lint", {"map_id": "moon"}))
    doc = run(invoke_tool(ctx, "read_catalog"))
    assert "## Agent tools" in doc["markdown"]


# ---------------------------------------------------------------------------
# run_step: preconditions + happy path (mock generation)
# ---------------------------------------------------------------------------

def test_run_step_generates_and_saves(mock_builder):
    wid = _map_world(mock_builder)
    result = run(invoke_tool(_ctx(mock_builder, wid), "run_step",
                             {"step_id": "world_rules", "note": "keep it grim"}))
    assert result["saved"] is True and result["data"]
    entry = mock_builder.load_world(wid)["steps"]["world_rules"]
    assert entry["approved"] is True and entry["data"] == result["data"]


def test_run_step_precondition_rejects_missing_requires(mock_builder):
    wid = mock_builder.save_world("fresh", {"seed_prompt": "x", "steps": {}})
    with pytest.raises(ToolError) as err:
        run(invoke_tool(_ctx(mock_builder, wid), "run_step",
                        {"step_id": "map_generation"}))
    assert "'hierarchy'" in str(err.value) and "requires" in str(err.value)


def test_run_step_rejects_enrichment_steps_and_unknown(mock_builder):
    wid = _map_world(mock_builder)
    ctx = _ctx(mock_builder, wid)
    with pytest.raises(ToolError, match="run_pass"):
        run(invoke_tool(ctx, "run_step", {"step_id": "node_labeling"}))
    with pytest.raises(ToolError, match="Unknown step 'wat'"):
        run(invoke_tool(ctx, "run_step", {"step_id": "wat"}))


def test_patch_step_merges_creates_and_guards(mock_builder):
    wid = _map_world(mock_builder)
    ctx = _ctx(mock_builder, wid)
    run(invoke_tool(ctx, "patch_step",
                    {"step_id": "world_rules", "data": {"genre": "noir", "tone": "grim"}}))
    run(invoke_tool(ctx, "patch_step",
                    {"step_id": "world_rules", "data": {"tone": None, "tech_era": "1920s"}}))
    data = mock_builder.load_world(wid)["steps"]["world_rules"]["data"]
    assert data == {"genre": "noir", "tech_era": "1920s"}
    with pytest.raises(ToolError, match="not patchable"):
        run(invoke_tool(ctx, "patch_step",
                        {"step_id": "map_generation", "data": {"nodes": []}}))
    # Terrain entries are records of rasters on disk — patching them would
    # fabricate geography no raster backs (the Crucible Stars failure mode).
    with pytest.raises(ToolError, match="not patchable"):
        run(invoke_tool(ctx, "patch_step",
                        {"step_id": "terrain_generation",
                         "data": {"layers": [{"layer_id": "venus_m",
                                              "seed": 2001001001}]}}))
    with pytest.raises(ToolError, match="at least one key"):
        run(invoke_tool(ctx, "patch_step", {"step_id": "world_rules", "data": {}}))


def test_patch_step_guards_brief_rules(mock_builder):
    """C4: a custom_rules patch may extend the co-authored brief rules but
    never drop one — they are fixed design decisions (loud rejection, P7)."""
    wid = _map_world(mock_builder)
    state = mock_builder.load_world(wid)
    state["brief"] = {"prompt": "test world",
                      "rules": ["The tide is a living god."]}
    mock_builder.save_world(wid, state)
    ctx = _ctx(mock_builder, wid)
    with pytest.raises(ToolError, match="must keep every co-authored"):
        run(invoke_tool(ctx, "patch_step",
                        {"step_id": "world_rules",
                         "data": {"custom_rules": ["Something else."]}}))
    # Keeping the agreed rule (any position) passes; other fields are free.
    run(invoke_tool(ctx, "patch_step",
                    {"step_id": "world_rules",
                     "data": {"custom_rules": ["Something else.",
                                               "The tide is a living god."]}}))
    run(invoke_tool(ctx, "patch_step",
                    {"step_id": "world_rules", "data": {"genre": "myth"}}))


# ---------------------------------------------------------------------------
# run_pass: preconditions, scope validation, guidance channel
# ---------------------------------------------------------------------------

def test_run_pass_labels_nodes_and_persists(builder, monkeypatch):
    wid = _map_world(builder)
    builder._enrichment_batch_size = 1

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        return f"Name {node['id']}", f"snippet {node['id']}"

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    result = run(invoke_tool(_ctx(builder, wid), "run_pass", {"pass_id": "label"}))
    assert result["summary"]["labeled"] == 6
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert all(n["name"] for n in nodes)


def test_run_pass_requires_labels_before_describe(builder):
    wid = _map_world(builder)  # unnamed
    with pytest.raises(ToolError) as err:
        run(invoke_tool(_ctx(builder, wid), "run_pass", {"pass_id": "describe"}))
    assert "'labels'" in str(err.value)


def test_run_pass_scope_and_unit_validation(builder):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="Registered passes"):
        run(invoke_tool(ctx, "run_pass", {"pass_id": "polish"}))
    with pytest.raises(ToolError, match="Unknown map 'moon'"):
        run(invoke_tool(ctx, "run_pass", {"pass_id": "label", "map_id": "moon"}))
    with pytest.raises(ToolError, match="Unknown node id"):
        run(invoke_tool(ctx, "run_pass", {"pass_id": "label", "node_ids": ["nope"]}))
    with pytest.raises(ToolError, match="works per map"):
        run(invoke_tool(ctx, "run_pass", {"pass_id": "review", "node_ids": ["n0"]}))


def test_run_pass_guidance_reaches_single_node_calls(builder, monkeypatch):
    wid = _map_world(builder)
    builder._enrichment_batch_size = 1
    seen = []

    async def fake_label(services, node, context, used_names=None, problem_note=None):
        seen.append(context.get("guidance"))
        return f"Name {node['id']}", ""

    monkeypatch.setattr(label_pass, "generate_label", fake_label)
    run(invoke_tool(_ctx(builder, wid), "run_pass",
                    {"pass_id": "label", "guidance": "nautical names"}))
    assert seen and all(g == "nautical names" for g in seen)


def test_run_pass_guidance_reaches_batched_calls(builder, monkeypatch):
    wid = _map_world(builder)  # default batch size covers all six in one call
    seen = []

    async def fake_batch(services, batch, contexts, used_names):
        seen.extend(c.get("guidance") for c in contexts.values())
        return {"nodes": [
            {"id": n["id"], "name": f"Name {n['id']}", "label_description": ""}
            for n in batch]}

    monkeypatch.setattr(label_pass, "generate_label_batch", fake_batch)
    run(invoke_tool(_ctx(builder, wid), "run_pass",
                    {"pass_id": "label", "guidance": "nautical names"}))
    assert seen and all(g == "nautical names" for g in seen)


def test_run_pass_guidance_reaches_describe_rework(builder, monkeypatch):
    wid = _map_world(builder, named=True)
    builder._enrichment_batch_size = 1
    seen = []

    async def fake_desc(services, node, context, existing_description="",
                        existing_details=""):
        seen.append(context.get("guidance"))
        return f"Flavor for {node['name']}", f"Details for {node['name']}"

    monkeypatch.setattr(describe_pass, "generate_description", fake_desc)
    run(invoke_tool(_ctx(builder, wid), "run_pass",
                    {"pass_id": "describe", "node_ids": ["n0", "n1"],
                     "guidance": "mention the fog"}))
    assert seen == ["mention the fog", "mention the fog"]
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert nodes[0]["description"] and nodes[1]["description"]


# ---------------------------------------------------------------------------
# run_custom_pass: the pass:custom capability
# ---------------------------------------------------------------------------

def test_run_custom_pass_writes_namespaced_slots(builder, monkeypatch):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)

    async def fake_custom(services, node, context, instruction):
        assert instruction == "One local rumor."
        return f"Rumor of {node['name']}"

    monkeypatch.setattr(build_tools, "generate_custom_content", fake_custom)
    result = run(invoke_tool(ctx, "run_custom_pass",
                             {"prompt": "One local rumor.", "slot": "rumor"}))
    assert result["field"] == "custom_rumor"
    assert result["summary"]["custom_rumor"] == 6
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert all(n["custom_rumor"].startswith("Rumor of") for n in nodes)

    # Slots are per-node done-tracked: a second run finds nothing pending...
    result = run(invoke_tool(ctx, "run_custom_pass",
                             {"prompt": "One local rumor.", "slot": "rumor"}))
    assert result["summary"].get("custom_rumor", 0) == 0
    # ...and rework revisits.
    result = run(invoke_tool(ctx, "run_custom_pass",
                             {"prompt": "One local rumor.", "slot": "rumor",
                              "rework": True, "node_ids": ["n0"]}))
    assert result["summary"]["custom_rumor"] == 1


def test_run_custom_pass_validation(builder):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="Invalid slot"):
        run(invoke_tool(ctx, "run_custom_pass", {"prompt": "x", "slot": "Bad Slot"}))
    with pytest.raises(ToolError, match="instruction"):
        run(invoke_tool(ctx, "run_custom_pass", {"prompt": "   ", "slot": "ok"}))
    unnamed = _map_world(builder, world_id="agent_unnamed")
    with pytest.raises(ToolError) as err:
        run(invoke_tool(_ctx(builder, unnamed), "run_custom_pass",
                        {"prompt": "x", "slot": "ok"}))
    assert "'labels'" in str(err.value)


# ---------------------------------------------------------------------------
# edit_node: user-parity writes with explicit invariants
# ---------------------------------------------------------------------------

def test_edit_node_renames_and_persists(builder):
    wid = _map_world(builder, named=True)
    result = run(invoke_tool(_ctx(builder, wid), "edit_node",
                             {"node_id": "n2", "name": "Gullwing Quay",
                              "label_description": "A salt-bitten dock."}))
    assert result["updated"]["name"] == "Gullwing Quay"
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert nodes[2]["name"] == "Gullwing Quay"
    assert nodes[2]["label_description"] == "A salt-bitten dock."


def test_edit_node_enforces_name_dedup(builder):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="collides with node n1"):
        run(invoke_tool(ctx, "edit_node", {"node_id": "n2", "name": "The town 1"}))
    with pytest.raises(ToolError, match="cannot be empty"):
        run(invoke_tool(ctx, "edit_node", {"node_id": "n2", "name": "  "}))
    # Renaming a node to (a variant of) its own name is not a collision.
    result = run(invoke_tool(ctx, "edit_node", {"node_id": "n2", "name": "The Town 2"}))
    assert result["updated"]["name"] == "The Town 2"


def test_edit_node_validates_and_resolves_links(builder):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="nonexistent node id"):
        run(invoke_tool(ctx, "edit_node",
                        {"node_id": "n0", "description": "Near ${link_ghost}."}))
    result = run(invoke_tool(ctx, "edit_node",
                             {"node_id": "n0", "description": "Near ${link_n1}."}))
    assert result["updated"]["description"] == "Near ${link_n1|Town 1 (E)}."
    with pytest.raises(ToolError, match="nothing to change"):
        run(invoke_tool(ctx, "edit_node", {"node_id": "n0"}))
    with pytest.raises(ToolError, match="Unknown node"):
        run(invoke_tool(ctx, "edit_node", {"node_id": "zz", "name": "X"}))


# ---------------------------------------------------------------------------
# End to end: the toolbox over a seeded world
# ---------------------------------------------------------------------------

def test_toolbox_reads_a_seeded_world(mock_builder):
    seeded = run(mock_builder.seed_world("a quiet coastal duchy",
                                         world_id="seedy", total_nodes=30))
    ctx = _ctx(mock_builder, seeded["world_id"])
    overview = run(invoke_tool(ctx, "read_world"))
    assert {"rules", "hierarchy", "maps"} <= set(overview["artifacts"])
    assert overview["maps"] and all(m["nodes"] for m in overview["maps"])
    report = run(invoke_tool(ctx, "read_lint"))
    assert report["stats"]
    first_map = overview["maps"][0]["map_id"]
    detail = run(invoke_tool(ctx, "read_map", {"map_id": first_map}))
    assert detail["node_list"]


# ---------------------------------------------------------------------------
# additional_details: the storyteller-only channel (node info layering)
# ---------------------------------------------------------------------------

def test_edit_node_additional_details_validates_and_resolves(builder):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="nonexistent node id"):
        run(invoke_tool(ctx, "edit_node",
                        {"node_id": "n0",
                         "additional_details": "Secret: ${link_ghost} lies below."}))
    result = run(invoke_tool(ctx, "edit_node",
                             {"node_id": "n0",
                              "additional_details": "Secret: a vault beneath ${link_n1}."}))
    assert result["updated"]["additional_details"].startswith("Secret: a vault beneath ${link_n1|")
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert nodes[0]["additional_details"].startswith("Secret: a vault beneath")


def test_lint_link_tokens_scan_additional_details():
    compiled = _compiled_fixture()
    compiled["maps"]["root"]["nodes"][1]["additional_details"] = \
        "Secret: past ${link_zzz} and ${link_c}."
    report = lint_world(compiled)
    broken = [p for p in report["problems"] if p["kind"] == "broken_link_token"]
    unresolved = [p for p in report["problems"] if p["kind"] == "unresolved_link_token"]
    assert broken and broken[0]["targets"] == ["zzz"]
    assert broken[0]["field"] == "additional_details"
    assert unresolved and unresolved[0]["targets"] == ["c"]


def test_read_map_reports_detailed_flag(builder):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)
    run(invoke_tool(ctx, "edit_node",
                    {"node_id": "n0", "additional_details": "Secret: under the well."}))
    detail = run(invoke_tool(ctx, "read_map", {"map_id": "root"}))
    assert detail["detailed"] == 1
    flags = {n["id"]: n["detailed"] for n in detail["node_list"]}
    assert flags["n0"] is True and flags["n1"] is False
    node = run(invoke_tool(ctx, "read_node", {"node_id": "n0"}))
    assert node["node"]["additional_details"] == "Secret: under the well."


def test_evaluator_excerpts_include_storyteller_details():
    from wbworldgen.worldgen.agent.evaluator import _content_excerpts

    compiled = {"root_map_id": "root", "maps": {"root": {
        "map_id": "root", "label": "Root", "nodes": [
            {"id": "a", "name": "Keep", "type": "castle", "importance": 9,
             "description": "Grey walls.",
             "additional_details": "Secret: a tunnel below."}],
        "edges": []}}}
    text = _content_excerpts(compiled)
    assert "storyteller details: Secret: a tunnel below." in text


def test_read_map_include_prose(builder):
    wid = _map_world(builder, named=True)
    ctx = _ctx(builder, wid)
    run(invoke_tool(ctx, "edit_node",
                    {"node_id": "n0", "description": "Salt wind combs the quay.",
                     "additional_details": "Secret: a vault below."}))
    compact = run(invoke_tool(ctx, "read_map", {"map_id": "root"}))
    assert all("description" not in n for n in compact["node_list"])
    prose = run(invoke_tool(ctx, "read_map",
                            {"map_id": "root", "include_prose": True}))
    by_id = {n["id"]: n for n in prose["node_list"]}
    assert by_id["n0"]["description"] == "Salt wind combs the quay."
    assert by_id["n0"]["additional_details"] == "Secret: a vault below."
    # Undescribed nodes report empty prose explicitly, never a missing key.
    assert by_id["n1"]["description"] == ""
    assert by_id["n1"]["additional_details"] == ""
    assert by_id["n1"]["label_description"] == ""


# ---------------------------------------------------------------------------
# run_step config/steering honesty (P7 at the config layer — the Ecstasy
# Veil live run lost a finished root map to a silently-ignored invented
# config, then failed to restore it because the steering note was dropped)
# ---------------------------------------------------------------------------

def _abstract_world(builder, world_id="abstract_world"):
    """World whose designed root is authored-abstract (map_style 'abstract',
    root level on the world_map generator)."""
    return builder.save_world(world_id, {
        "seed_prompt": "a veil of worlds",
        "steps": {
            "world_form": {"data": {"world_kind": "a veil",
                                    "map_style": "abstract"}, "approved": True},
            "world_rules": {"data": {"genre": "sf", "tone": "wistful"},
                            "approved": True},
            "lore": {"data": {"world_name": "The Veil"}, "approved": True},
            "hierarchy_design": {"data": {"levels": [
                {"level_type": "world", "generator_id": "world_map"}]},
                "approved": True},
        },
    })


def _procedural_world(builder, world_id="procedural_world"):
    """World whose designed root stays procedural (terrain style)."""
    return builder.save_world(world_id, {
        "seed_prompt": "a plain land",
        "steps": {
            "world_form": {"data": {"map_style": "terrain"}, "approved": True},
            "hierarchy_design": {"data": {"levels": [
                {"level_type": "region", "generator_id": "world_map",
                 "terrain": True}]}, "approved": True},
        },
    })


def test_run_step_rejects_unknown_config_keys(mock_builder):
    wid = _map_world(mock_builder)
    ctx = _ctx(mock_builder, wid)
    # The Ecstasy Veil failure shape: invented child-map config keys must be
    # rejected loudly, never silently ignored while the root regenerates.
    with pytest.raises(ToolError) as err:
        run(invoke_tool(ctx, "run_step",
                        {"step_id": "map_generation",
                         "config": {"parent_node_id": "n1",
                                    "level_type": "planet"}}))
    msg = str(err.value)
    assert "unknown config key 'parent_node_id'" in msg
    assert "total_nodes" in msg  # the rejection names the accepted keys
    with pytest.raises(ToolError, match="accepted: none"):
        run(invoke_tool(ctx, "run_step",
                        {"step_id": "lore", "config": {"length": "long"}}))


def test_run_step_config_type_and_bounds(mock_builder):
    wid = _map_world(mock_builder)
    ctx = _ctx(mock_builder, wid)
    with pytest.raises(ToolError, match="must be a integer"):
        run(invoke_tool(ctx, "run_step",
                        {"step_id": "map_generation",
                         "config": {"total_nodes": "many"}}))
    with pytest.raises(ToolError, match=">= 30"):
        run(invoke_tool(ctx, "run_step",
                        {"step_id": "map_generation",
                         "config": {"total_nodes": 25}}))
    with pytest.raises(ToolError, match="one of"):
        run(invoke_tool(ctx, "run_step",
                        {"step_id": "terrain_generation",
                         "config": {"biome_mode": "lurid"}}))


def test_run_step_rejects_total_nodes_on_authored_root(mock_builder):
    wid = _abstract_world(mock_builder)
    with pytest.raises(ToolError, match="AUTHORED"):
        run(invoke_tool(_ctx(mock_builder, wid), "run_step",
                        {"step_id": "map_generation",
                         "config": {"total_nodes": 40}}))


def test_run_step_rejects_unread_steering_notes(mock_builder):
    wid = _procedural_world(mock_builder)
    ctx = _ctx(mock_builder, wid)
    with pytest.raises(ToolError, match="PROCEDURAL"):
        run(invoke_tool(ctx, "run_step",
                        {"step_id": "map_generation",
                         "note": "make it an archipelago"}))
    with pytest.raises(ToolError, match="terrain is procedural"):
        run(invoke_tool(ctx, "run_step",
                        {"step_id": "terrain_generation",
                         "note": "more mountains"}))


def test_run_step_abstract_result_reports_authored_names(mock_builder):
    # The mock abstract author names its nodes at generation time; the tool
    # result must say so instead of claiming a label pass is pending (the
    # Ecstasy Veil agent ran a no-op label pass off that stale note).
    wid = _abstract_world(mock_builder)
    result = run(invoke_tool(_ctx(mock_builder, wid), "run_step",
                             {"step_id": "map_generation",
                              "note": "aim for a tight cluster"}))
    assert result["saved"] is True
    assert all(m["named"] == m["nodes"] for m in result["maps"])
    assert "no label pass is pending" in result["note"]


def test_run_step_threads_note_into_abstract_author(builder):
    # The steering note must reach the authoring LLM call — the Ecstasy
    # Veil restoration note never did (all three map:abstract inputs were
    # byte-identical), which made regeneration an unsteerable re-roll.
    from wbworldgen.worldgen.expansion import maps_expand as me

    wid = _abstract_world(builder)
    captured = []

    async def fake_jrc(llm, **kw):
        captured.append(kw["messages"])
        return {"description": "a veil of worlds",
                "nodes": [{"name": "Alpha", "kind": "planet", "importance": 8,
                           "description": "First.", "adjacent": ["Beta"]},
                          {"name": "Beta", "kind": "planet", "importance": 6,
                           "description": "Second.", "adjacent": ["Alpha"]}]}

    original = me.json_retry_completion
    me.json_retry_completion = fake_jrc
    try:
        result = run(invoke_tool(_ctx(builder, wid), "run_step",
                                 {"step_id": "map_generation",
                                  "note": "restore Helios Prime as the center"}))
    finally:
        me.json_retry_completion = original
    assert result["saved"] is True and captured
    user_msg = captured[0][1]["content"]
    assert "Steering note for THIS generation" in user_msg
    assert "restore Helios Prime as the center" in user_msg


def test_catalog_renders_step_config_contract():
    text = render_catalog_markdown()
    assert "config `total_nodes`" in text
    assert "config `resolution`" in text
    # Tool text teaches that config is validated, not silently dropped.
    assert "Unknown keys are rejected" in text


# ---------------------------------------------------------------------------
# Evaluator excerpt honesty (the false "20 claimed, 12 listed" finding)
# ---------------------------------------------------------------------------

def test_evaluator_excerpt_marks_truncation():
    from wbworldgen.worldgen.agent.evaluator import (
        _EXCERPT_NODES_PER_MAP, _content_excerpts)

    n = _EXCERPT_NODES_PER_MAP + 8
    compiled = {"root_map_id": "root", "maps": {"root": {
        "map_id": "root", "label": "Root", "edges": [],
        "nodes": [{"id": f"n{i}", "name": f"Place {i}", "type": "town",
                   "importance": i % 10, "description": "d"}
                  for i in range(n)]}}}
    text = _content_excerpts(compiled)
    assert f"{n} locations, {n} named" in text
    assert (f"the {_EXCERPT_NODES_PER_MAP} highest-importance of {n} "
            "named locations are shown") in text
    small = {"root_map_id": "root", "maps": {"root": {
        "map_id": "root", "label": "R", "edges": [],
        "nodes": [{"id": "a", "name": "A", "importance": 5,
                   "description": "d"}]}}}
    assert "excerpt truncated" not in _content_excerpts(small)


# ---------------------------------------------------------------------------
# revert (v2c): build-scoped byte-exact restore of pre-action checkpoints
# ---------------------------------------------------------------------------

def _build_ctx(builder, world_id):
    """A ToolContext inside an agent build — revert is build-scoped and
    only needs the handle as a scope token."""
    return ToolContext(builder=builder, world_id=world_id,
                       build=types.SimpleNamespace())


def test_revert_requires_build_scope(builder):
    wid = _map_world(builder, named=True)
    with pytest.raises(ToolError, match="inside an agent build"):
        run(invoke_tool(_ctx(builder, wid), "revert", {"checkpoint": 1}))


def test_revert_unknown_checkpoint_lists_available(builder):
    wid = _map_world(builder, named=True)
    ctx = _build_ctx(builder, wid)
    with pytest.raises(ToolError, match="No checkpoints exist yet"):
        run(invoke_tool(ctx, "revert", {"checkpoint": 3}))
    builder.services.enrichment_store.snapshot_world(wid, "4")
    with pytest.raises(ToolError) as err:
        run(invoke_tool(ctx, "revert", {"checkpoint": 9}))
    assert "No checkpoint '9'" in str(err.value) and "4" in str(err.value)


def test_revert_restores_world_and_compiled_cache(builder):
    wid = _map_world(builder, named=True)
    store = builder.services.enrichment_store
    store.snapshot_world(wid, "2")
    run(invoke_tool(_ctx(builder, wid), "edit_node",
                    {"node_id": "n0", "name": "Renamed Keep"}))
    assert builder.services.compiled.get_node(wid, "n0")["name"] == "Renamed Keep"

    result = run(invoke_tool(_build_ctx(builder, wid), "revert",
                             {"checkpoint": 2}))
    assert result["reverted_to_before_action"] == 2
    assert result["maps"][0]["named"] == result["maps"][0]["nodes"]
    # Disk AND the compiled cache serve the restored timeline.
    nodes = builder.load_world(wid)["steps"]["map_generation"]["data"]["nodes"]
    assert nodes[0]["name"] == "Town 0"
    assert builder.services.compiled.get_node(wid, "n0")["name"] == "Town 0"
