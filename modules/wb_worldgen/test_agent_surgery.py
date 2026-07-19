"""Tests for v2a structural surgery: the shared surgery surface, its six
agent tools, the persistence write paths (child bundle / step data /
world_connections metadata), and the S1 refuse-hard/warn-soft contract.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_agent_surgery.py
"""

import asyncio
import json
import shutil
import tempfile

import pytest

from wbworldgen.mapmodel import grow_position
from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import ToolContext, ToolError, invoke_tool
from wbworldgen.worldgen.agent.registry import describe_tools


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_surgery_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


def _ctx(builder, world_id):
    return ToolContext(builder=builder, world_id=world_id)


def _chain_nodes(n, prefix="n", named=True):
    return [
        {"id": f"{prefix}{i}", "type": "town", "importance": 5,
         "x": float(i * 2), "y": 0.0,
         "name": f"Town {prefix}{i}" if named else "",
         "description": "", "region": ""}
        for i in range(n)
    ]


def _flat_world(builder, world_id="surgery_world", n_nodes=6, named=True,
                regions=None, descriptions=None, node_fields=None):
    """Flat single-map world: a chain n0-n1-...; optional regions,
    per-node descriptions and extra node fields."""
    nodes = _chain_nodes(n_nodes, named=named)
    for nid, desc in (descriptions or {}).items():
        next(n for n in nodes if n["id"] == nid)["description"] = desc
    for nid, fields in (node_fields or {}).items():
        next(n for n in nodes if n["id"] == nid).update(fields)
    data = {"nodes": nodes,
            "edges": [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(n_nodes - 1)],
            "config": {"map_width": 100.0, "map_height": 100.0}}
    if regions is not None:
        data["regions"] = regions
    return builder.save_world(world_id, {
        "seed_prompt": "surgery test world",
        "steps": {"map_generation": {"data": data, "approved": True}},
    })


def _layered_world(builder, world_id="layered_world", extra_legacy_connection=False):
    """Two-layer world (root 'Surface' + parallel 'underdark') joined by the
    legacy layer connection lc_0000 (n2 <-> u0), the shape fresh generation
    still persists in step data."""
    root_nodes = _chain_nodes(4)
    under_nodes = _chain_nodes(3, prefix="u")
    root_nodes[2]["interlayer_connection_id"] = "lc_0000"
    under_nodes[0]["interlayer_connection_id"] = "lc_0000"
    connections = [{
        "id": "lc_0000", "from_layer_id": "root", "from_node_id": "n2",
        "to_layer_id": "underdark", "to_node_id": "u0",
        "connection_type": "passage", "name": "Sinkhole",
        "description": "", "bidirectional": True,
    }]
    if extra_legacy_connection:
        # An id-less legacy record: migrate synthesizes an id for the
        # compiled view, so no persisted record carries it.
        connections.append({
            "from_layer_id": "root", "from_node_id": "n3",
            "to_layer_id": "underdark", "to_node_id": "u2",
            "connection_type": "passage", "name": "Old Shaft",
            "description": "", "bidirectional": True,
        })
    data = {
        "layers": [
            {"layer_id": "root", "name": "Surface", "layer_type": "world",
             "index": 0, "map": {
                 "nodes": root_nodes,
                 "edges": [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(3)],
                 "config": {"map_width": 100.0, "map_height": 100.0}}},
            {"layer_id": "underdark", "name": "The Underdark",
             "layer_type": "underground", "index": 1, "map": {
                 "nodes": under_nodes,
                 "edges": [{"from": f"u{i}", "to": f"u{i + 1}"} for i in range(2)],
                 "config": {"map_width": 100.0, "map_height": 100.0}}},
        ],
        "connections": connections,
    }
    return builder.save_world(world_id, {
        "seed_prompt": "layered surgery world",
        "steps": {"map_generation": {"data": data, "approved": True}},
    })


def _with_child_map(builder, world_id):
    """Persist an interior child bundle anchored to n1, with its entrance
    connection, the way expansion stores them."""
    bundle = {
        "map": {
            "map_id": "site_n1", "label": "The Keep", "level_type": "interior",
            "description": "", "parent_map_id": "root", "anchor_node_id": "n1",
            "generator_id": "interior", "schema": 2,
            "nodes": [
                {"id": "s0", "name": "Gatehouse", "type": "room",
                 "importance": 5, "x": 5.0, "y": 5.0, "description": ""},
                {"id": "s1", "name": "Great Hall", "type": "room",
                 "importance": 6, "x": 15.0, "y": 5.0, "description": ""},
            ],
            "edges": [{"from": "s0", "to": "s1", "distance": 10.0}],
            "config": {"map_width": 40.0, "map_height": 40.0},
        },
        "connections": [{
            "id": "c_site_entry",
            "from": {"map_id": "root", "node_id": "n1"},
            "to": {"map_id": "site_n1", "node_id": "s0"},
            "kind": "entrance", "name": "Keep Gate", "description": "",
            "travel": {"mode": "instant"}, "bidirectional": True,
            "requirements": "", "hidden": False, "origin": "generated",
        }],
    }
    builder._persistence.save_child_map(world_id, bundle)
    builder.services.compiled.invalidate(world_id)
    return bundle


def _compiled(builder, world_id):
    return builder.services.compiled.load(world_id)


def _step_map_data(builder, world_id):
    return builder.load_world(world_id)["steps"]["map_generation"]["data"]


# ---------------------------------------------------------------------------
# grow_position (promoted to mapmodel)
# ---------------------------------------------------------------------------

def test_grow_position_stays_in_bounds_and_off_nodes():
    record = {"nodes": _chain_nodes(4),
              "edges": [{"from": "n0", "to": "n1", "distance": 10.0}],
              "config": {"map_width": 100.0, "map_height": 100.0}}
    x, y = grow_position(record, [record["nodes"][0]])
    assert 5.0 <= x <= 95.0 and 5.0 <= y <= 95.0
    assert all((x - n["x"]) ** 2 + (y - n["y"]) ** 2 > 1.0
               for n in record["nodes"])


# ---------------------------------------------------------------------------
# add_node
# ---------------------------------------------------------------------------

def test_add_node_unnamed_appends_and_persists(builder):
    wid = _flat_world(builder)
    result = run(invoke_tool(_ctx(builder, wid), "add_node",
                             {"map_id": "root", "near_node_id": "n0"}))
    node = result["node"]
    assert node["id"] == "root:g7"
    assert node["name"] == "" and node["type"] == "waypoint"
    assert node["importance"] == 3
    assert result["edges"][0]["from"] == "n0"
    assert result["edges"][0]["distance"] >= 1.0
    # Persisted into the step data and visible in the recompiled world.
    data = _step_map_data(builder, wid)
    assert any(n["id"] == "root:g7" for n in data["nodes"])
    compiled = _compiled(builder, wid)
    assert any(n["id"] == "root:g7"
               for n in compiled["maps"]["root"]["nodes"])


def test_add_node_inherits_region_and_links_multiple(builder):
    wid = _flat_world(builder, regions=[
        {"region_name": "Heartland", "node_ids": ["n0", "n1"],
         "center_node_id": "n0"}],
        node_fields={"n0": {"region": "Heartland"}})
    result = run(invoke_tool(_ctx(builder, wid), "add_node",
                             {"map_id": "root", "near_node_id": "n0",
                              "name": "Fort Ridge", "type": "stronghold",
                              "importance": 8,
                              "edges_to": ["n0", "n2"]}))
    node = result["node"]
    assert node["region"] == "Heartland"
    assert {e["from"] for e in result["edges"]} == {"n0", "n2"}
    data = _step_map_data(builder, wid)
    assert sum(1 for e in data["edges"]
               if e.get("to") == node["id"]) == 2
    # Partner-region membership followed the anchor.
    region = data["regions"][0]
    assert node["id"] in region["node_ids"]


def test_add_node_rejects_duplicate_name_and_bad_targets(builder):
    wid = _flat_world(builder)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="collides"):
        run(invoke_tool(ctx, "add_node",
                        {"map_id": "root", "near_node_id": "n0",
                         "name": "town N2"}))  # join_key-tolerant clash
    with pytest.raises(ToolError, match="Unknown map"):
        run(invoke_tool(ctx, "add_node",
                        {"map_id": "nowhere", "near_node_id": "n0"}))
    with pytest.raises(ToolError, match="not on map"):
        run(invoke_tool(ctx, "add_node",
                        {"map_id": "root", "near_node_id": "ghost"}))
    with pytest.raises(ToolError, match="not on map"):
        run(invoke_tool(ctx, "add_node",
                        {"map_id": "root", "near_node_id": "n0",
                         "edges_to": ["ghost"]}))


def test_add_node_validates_and_resolves_description_links(builder):
    wid = _flat_world(builder)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="nonexistent node id"):
        run(invoke_tool(ctx, "add_node",
                        {"map_id": "root", "near_node_id": "n0",
                         "description": "Watches ${link_ghost}."}))
    result = run(invoke_tool(ctx, "add_node",
                             {"map_id": "root", "near_node_id": "n0",
                              "name": "Watchpost",
                              "description": "Watches ${link_n1}."}))
    assert "${link_n1|Town n1 (" in result["node"]["description"]


# ---------------------------------------------------------------------------
# remove_node
# ---------------------------------------------------------------------------

def test_remove_node_cascades_and_warns_on_split(builder):
    wid = _flat_world(builder, descriptions={
        "n1": "Gateway to ${link_n3|Town n3 (E)}."})
    result = run(invoke_tool(_ctx(builder, wid), "remove_node",
                             {"node_id": "n3"}))
    assert result["removed"] == "n3" and result["edges_removed"] == 2
    assert result["linked_from"] == ["n1"]
    assert any("splits" in w for w in result["warnings"])
    assert any("link" in w for w in result["warnings"])
    data = _step_map_data(builder, wid)
    assert not any(n["id"] == "n3" for n in data["nodes"])
    assert not any("n3" in (e["from"], e["to"]) for e in data["edges"])
    compiled = _compiled(builder, wid)
    assert "n3" not in {n["id"] for n in compiled["maps"]["root"]["nodes"]}


def test_remove_leaf_node_reassigns_region_center_quietly(builder):
    wid = _flat_world(builder, regions=[
        {"region_name": "Heartland", "node_ids": ["n0", "n1"],
         "center_node_id": "n0"}])
    result = run(invoke_tool(_ctx(builder, wid), "remove_node",
                             {"node_id": "n0"}))
    assert result["warnings"] == []
    region = _step_map_data(builder, wid)["regions"][0]
    assert region["node_ids"] == ["n1"]
    assert region["center_node_id"] == "n1"


def test_remove_node_refuses_child_anchor_and_connection_endpoint(builder):
    wid = _flat_world(builder)
    _with_child_map(builder, wid)
    with pytest.raises(ToolError, match="anchors child map"):
        run(invoke_tool(_ctx(builder, wid), "remove_node", {"node_id": "n1"}))

    lid = _layered_world(builder)
    with pytest.raises(ToolError, match="endpoint of connection"):
        run(invoke_tool(_ctx(builder, lid), "remove_node", {"node_id": "n2"}))


def test_remove_node_in_child_bundle(builder):
    wid = _flat_world(builder)
    _with_child_map(builder, wid)
    result = run(invoke_tool(_ctx(builder, wid), "remove_node",
                             {"node_id": "s1"}))
    assert result["map_id"] == "site_n1"
    bundle = builder._persistence.load_child_map(wid, "site_n1")
    assert [n["id"] for n in bundle["map"]["nodes"]] == ["s0"]
    assert bundle["map"]["edges"] == []


# ---------------------------------------------------------------------------
# edges
# ---------------------------------------------------------------------------

def test_add_edge_computes_distance_and_rejects_duplicates(builder):
    wid = _flat_world(builder)
    ctx = _ctx(builder, wid)
    result = run(invoke_tool(ctx, "add_edge",
                             {"map_id": "root", "from_node_id": "n0",
                              "to_node_id": "n3"}))
    assert result["edge"]["distance"] == 6.0
    assert any({e.get("from"), e.get("to")} == {"n0", "n3"}
               for e in _step_map_data(builder, wid)["edges"])
    with pytest.raises(ToolError, match="already joined"):
        run(invoke_tool(ctx, "add_edge",
                        {"map_id": "root", "from_node_id": "n1",
                         "to_node_id": "n0"}))
    with pytest.raises(ToolError, match="two different nodes"):
        run(invoke_tool(ctx, "add_edge",
                        {"map_id": "root", "from_node_id": "n1",
                         "to_node_id": "n1"}))


def test_add_edge_rejects_cross_map_endpoints(builder):
    wid = _layered_world(builder)
    with pytest.raises(ToolError, match="not on map"):
        run(invoke_tool(_ctx(builder, wid), "add_edge",
                        {"map_id": "root", "from_node_id": "n0",
                         "to_node_id": "u1"}))


def test_remove_edge_warns_on_split_and_orphan(builder):
    wid = _flat_world(builder)
    result = run(invoke_tool(_ctx(builder, wid), "remove_edge",
                             {"map_id": "root", "from_node_id": "n4",
                              "to_node_id": "n5"}))
    assert result["removed_edges"] == 1
    assert any("splits" in w for w in result["warnings"])
    assert any("n5" in w for w in result["warnings"])  # orphaned
    with pytest.raises(ToolError, match="No edge joins"):
        run(invoke_tool(_ctx(builder, wid), "remove_edge",
                        {"map_id": "root", "from_node_id": "n4",
                         "to_node_id": "n5"}))


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------

def test_add_connection_between_root_and_parallel_map(builder):
    wid = _layered_world(builder)
    result = run(invoke_tool(_ctx(builder, wid), "add_connection",
                             {"from_map_id": "root", "from_node_id": "n0",
                              "to_map_id": "underdark", "to_node_id": "u2",
                              "kind": "portal", "name": "The Deep Stair"}))
    conn = result["connection"]
    assert result["stored_in"] == "world"
    assert conn["id"].startswith("c_") and conn["origin"] == "surgery"
    meta = json.loads(
        (builder._persistence.world_dir(wid) / "metadata.json").read_text(
            encoding="utf-8"))
    assert meta["world_connections"][0]["id"] == conn["id"]
    compiled = _compiled(builder, wid)
    assert any(c["id"] == conn["id"] for c in compiled["connections"])
    with pytest.raises(ToolError, match="already joins"):
        run(invoke_tool(_ctx(builder, wid), "add_connection",
                        {"from_map_id": "underdark", "from_node_id": "u2",
                         "to_map_id": "root", "to_node_id": "n0"}))


def test_add_connection_into_child_bundle(builder):
    wid = _flat_world(builder)
    _with_child_map(builder, wid)
    result = run(invoke_tool(_ctx(builder, wid), "add_connection",
                             {"from_map_id": "root", "from_node_id": "n4",
                              "to_map_id": "site_n1", "to_node_id": "s1",
                              "kind": "entrance", "name": "Back Door"}))
    assert result["stored_in"] == "child:site_n1"
    bundle = builder._persistence.load_child_map(wid, "site_n1")
    assert any(c["id"] == result["connection"]["id"]
               for c in bundle["connections"])
    assert any(c["id"] == result["connection"]["id"]
               for c in _compiled(builder, wid)["connections"])


def test_remove_connection_from_every_home(builder):
    # world_connections home
    wid = _layered_world(builder)
    added = run(invoke_tool(_ctx(builder, wid), "add_connection",
                            {"from_map_id": "root", "from_node_id": "n0",
                             "to_map_id": "underdark", "to_node_id": "u2"}))
    removed = run(invoke_tool(_ctx(builder, wid), "remove_connection",
                              {"connection_id": added["connection"]["id"]}))
    assert removed["stored_in"] == "world" and removed["warnings"] == []
    assert not any(c["id"] == added["connection"]["id"]
                   for c in _compiled(builder, wid)["connections"])

    # legacy step-data home: the underdark's only remaining link — warns
    # unreachable, clears the interlayer stamps.
    removed = run(invoke_tool(_ctx(builder, wid), "remove_connection",
                              {"connection_id": "lc_0000"}))
    assert removed["stored_in"] == "step"
    assert any("underdark" in w for w in removed["warnings"])
    data = _step_map_data(builder, wid)
    assert data["connections"] == []
    stamped = [n for layer in data["layers"]
               for n in layer["map"]["nodes"]
               if n.get("interlayer_connection_id")]
    assert stamped == []
    assert not any(c["id"] == "lc_0000"
                   for c in _compiled(builder, wid)["connections"])

    # child bundle home. No unreachable warning: an anchored child map
    # stays attached through its parent anchor even with no connections.
    cid = _flat_world(builder, world_id="child_conn_world")
    _with_child_map(builder, cid)
    removed = run(invoke_tool(_ctx(builder, cid), "remove_connection",
                              {"connection_id": "c_site_entry"}))
    assert removed["stored_in"] == "child:site_n1"
    assert removed["warnings"] == []
    bundle = builder._persistence.load_child_map(cid, "site_n1")
    assert bundle["connections"] == []


def test_remove_connection_refuses_unknown_and_unpersisted_ids(builder):
    wid = _layered_world(builder, extra_legacy_connection=True)
    ctx = _ctx(builder, wid)
    with pytest.raises(ToolError, match="Unknown connection"):
        run(invoke_tool(ctx, "remove_connection", {"connection_id": "c_nope"}))
    # The id-less legacy record compiles under a synthesized id; no
    # persisted record carries it, so removal is loudly refused.
    synthesized = next(
        c["id"] for c in _compiled(builder, wid)["connections"]
        if c.get("name") == "Old Shaft")
    with pytest.raises(ToolError, match="no persisted record"):
        run(invoke_tool(ctx, "remove_connection",
                        {"connection_id": synthesized}))


# ---------------------------------------------------------------------------
# edit_node: type / importance (v2a extension)
# ---------------------------------------------------------------------------

def test_edit_node_type_and_importance(builder):
    wid = _flat_world(builder)
    result = run(invoke_tool(_ctx(builder, wid), "edit_node",
                             {"node_id": "n2", "type": "ruin",
                              "importance": 9}))
    assert result["updated"] == {"type": "ruin", "importance": 9}
    node = next(n for n in _step_map_data(builder, wid)["nodes"]
                if n["id"] == "n2")
    assert node["type"] == "ruin" and node["importance"] == 9
    with pytest.raises(ToolError, match="must be >= 1"):
        run(invoke_tool(_ctx(builder, wid), "edit_node",
                        {"node_id": "n2", "importance": 0}))


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------

def test_structure_tools_are_in_the_catalog():
    ids = {e["id"] for e in describe_tools()}
    assert {"add_node", "remove_node", "add_edge", "remove_edge",
            "add_connection", "remove_connection"} <= ids
    entry = next(e for e in describe_tools() if e["id"] == "add_node")
    assert entry["mutates"] is True
    assert entry["params"]["map_id"]["required"] is True


# ---------------------------------------------------------------------------
# additional_details rides add_node (node info layering)
# ---------------------------------------------------------------------------

def test_add_node_carries_additional_details(builder):
    wid = _flat_world(builder)
    result = run(invoke_tool(_ctx(builder, wid), "add_node",
                             {"map_id": "root", "near_node_id": "n0",
                              "name": "Sunken Chapel", "type": "ruin",
                              "description": "A drowned nave.",
                              "additional_details": "Secret: the crypt below ${link_n0} still seals something."}))
    node = result["node"]
    assert node["additional_details"].startswith("Secret: the crypt below ${link_n0|")
    data = _step_map_data(builder, wid)
    stored = next(n for n in data["nodes"] if n["id"] == node["id"])
    assert stored["additional_details"] == node["additional_details"]
    with pytest.raises(ToolError, match="nonexistent node id"):
        run(invoke_tool(_ctx(builder, wid), "add_node",
                        {"map_id": "root", "near_node_id": "n0",
                         "additional_details": "Secret: ${link_ghost}."}))


def test_add_connection_hidden_flag(builder):
    wid = _flat_world(builder)
    result = run(invoke_tool(_ctx(builder, wid), "add_connection",
                             {"from_map_id": "root", "from_node_id": "n0",
                              "to_map_id": "root", "to_node_id": "n3",
                              "kind": "tunnel", "name": "Old Crawl",
                              "hidden": True}))
    assert result["connection"]["hidden"] is True
    compiled = _compiled(builder, wid)
    stored = next(c for c in compiled["connections"]
                  if c["id"] == result["connection"]["id"])
    assert stored["hidden"] is True


def test_read_views_mark_hidden_connections(builder):
    wid = _flat_world(builder)
    ctx = _ctx(builder, wid)
    run(invoke_tool(ctx, "add_connection",
                    {"from_map_id": "root", "from_node_id": "n0",
                     "to_map_id": "root", "to_node_id": "n3",
                     "kind": "tunnel", "name": "Old Crawl", "hidden": True}))
    run(invoke_tool(ctx, "add_connection",
                    {"from_map_id": "root", "from_node_id": "n1",
                     "to_map_id": "root", "to_node_id": "n4",
                     "kind": "bridge", "name": "High Span"}))
    detail = run(invoke_tool(ctx, "read_map", {"map_id": "root"}))
    tunnel = next(c for c in detail["connections"] if c["kind"] == "tunnel")
    bridge = next(c for c in detail["connections"] if c["kind"] == "bridge")
    assert tunnel["hidden"] is True
    assert "hidden" not in bridge  # visible ways carry no flag
    node = run(invoke_tool(ctx, "read_node", {"node_id": "n0"}))
    conn = next(c for c in node["connections"] if c["kind"] == "tunnel")
    assert conn["hidden"] is True
