"""Tests for the v2c world checkpoint store: byte-exact snapshot/restore
of a world's persisted content (steps, metadata, child-map bundles, sites,
terrain rasters), the brief carried forward through restore, and the
write-cache coherence discipline (flush before snapshot, invalidate before
restore) that keeps a later flush from resurrecting abandoned state.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_world_checkpoints.py
"""

import json
import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.persistence import CHECKPOINTS_DIRNAME


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_ckpt_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


def _world(builder, world_id="ckpt_world"):
    nodes = [
        {"id": f"n{i}", "type": "town", "importance": 6 - i,
         "x": float(i), "y": 0.0, "name": f"Town {i}",
         "description": f"Flavor {i}.", "region": ""}
        for i in range(3)
    ]
    edges = [{"from": "n0", "to": "n1"}, {"from": "n1", "to": "n2"}]
    return builder.save_world(world_id, {
        "seed_prompt": "a quiet land",
        "steps": {
            "world_rules": {"data": {"genre": "pastoral"}, "approved": True},
            "map_generation": {"data": {"nodes": nodes, "edges": edges},
                               "approved": True},
        },
    })


def _step_bytes(store, wid, step_id="map_generation"):
    return (store.world_dir(wid) / f"step_{step_id}.json").read_bytes()


def test_snapshot_restore_roundtrip_all_content_homes(builder):
    store = builder.services.enrichment_store
    wid = _world(builder)
    store.save_child_map(wid, {"map": {"map_id": "m_child", "nodes": [
        {"id": "c1", "name": "Cellar"}], "edges": []}})
    store.save_site(wid, "n0", {"parent_node_id": "n0", "rooms": 3})
    raster = store.terrain_dir(wid, "main") / "height.npz"
    raster.write_bytes(b"raster-v1")

    before = {
        "step": _step_bytes(store, wid),
        "child": store.load_child_map(wid, "m_child"),
        "site": store.load_site(wid, "n0"),
        "raster": raster.read_bytes(),
    }
    store.snapshot_world(wid, "3")

    # Mutate every content home.
    store.save_step(wid, "map_generation",
                    {"data": {"nodes": [], "edges": []}, "approved": True})
    store.save_child_map(wid, {"map": {"map_id": "m_child", "nodes": [],
                                       "edges": []}})
    store.save_site(wid, "n0", {"parent_node_id": "n0", "rooms": 99})
    raster.write_bytes(b"raster-v2")

    store.restore_world(wid, "3")
    assert _step_bytes(store, wid) == before["step"]
    assert store.load_child_map(wid, "m_child") == before["child"]
    assert store.load_site(wid, "n0") == before["site"]
    assert raster.read_bytes() == before["raster"]


def test_snapshot_excludes_history_and_restore_keeps_it(builder):
    store = builder.services.enrichment_store
    wid = _world(builder)
    artifact = store.world_dir(wid) / "agent_build.json"
    artifact.write_text('{"log": ["old history"]}')
    store.snapshot_world(wid, "1")
    store.snapshot_world(wid, "2")

    snap = store.checkpoints_dir(wid) / "2"
    assert not (snap / "agent_build.json").exists()
    assert not (snap / CHECKPOINTS_DIRNAME).exists()

    artifact.write_text('{"log": ["newer history"]}')
    store.restore_world(wid, "1")
    # History never rewinds: the artifact and the store survive restore.
    assert artifact.read_text() == '{"log": ["newer history"]}'
    assert store.list_checkpoints(wid) == ["1", "2"]


def test_restore_carries_current_brief_forward(builder):
    store = builder.services.enrichment_store
    wid = _world(builder)
    state = builder.load_world(wid)
    state["brief"] = {"prompt": "p", "rules": ["r1"],
                      "notes": [{"id": "n1", "text": "original", "subject": ""}]}
    builder.save_world(wid, state)
    store.snapshot_world(wid, "5")

    # A compromise amends the note AFTER the checkpoint: the agreement must
    # survive a world-content restore.
    state = builder.load_world(wid)
    state["brief"]["notes"][0].update(
        {"text": "amended", "status": "amended", "original_text": "original"})
    builder.save_world(wid, state)

    store.restore_world(wid, "5")
    brief = builder.load_world(wid)["brief"]
    assert brief["notes"][0]["text"] == "amended"
    assert brief["notes"][0]["status"] == "amended"
    # World content still rewound with the restore.
    assert builder.load_world(wid)["steps"]["world_rules"]["data"] == {
        "genre": "pastoral"}


def test_snapshot_flushes_pending_writes_and_restore_invalidates_cache(builder):
    store = builder.services.enrichment_store
    wid = _world(builder)

    # A pending write-cache-only enrichment must be part of the snapshot.
    store.save_node_enrichment(wid, "n0", "name", "Newtown")
    store.snapshot_world(wid, "4")
    snap_step = json.loads(
        (store.checkpoints_dir(wid) / "4" / "step_map_generation.json")
        .read_text())
    assert snap_step["data"]["nodes"][0]["name"] == "Newtown"

    # A post-snapshot cached write must NOT survive restore via a later
    # flush — invalidate-before-replace is the whole coherence contract.
    store.save_node_enrichment(wid, "n0", "name", "Aftertown")
    store.restore_world(wid, "4")
    store.flush_enrichment_cache(wid)
    on_disk = json.loads(_step_bytes(store, wid))
    assert on_disk["data"]["nodes"][0]["name"] == "Newtown"


def test_list_orders_numerically_clear_drops_all_unknown_raises(builder):
    store = builder.services.enrichment_store
    wid = _world(builder)
    for tag in ("10", "2", "7"):
        store.snapshot_world(wid, tag)
    assert store.list_checkpoints(wid) == ["2", "7", "10"]
    with pytest.raises(FileNotFoundError, match="no checkpoint '99'"):
        store.restore_world(wid, "99")
    store.clear_checkpoints(wid)
    assert store.list_checkpoints(wid) == []
    with pytest.raises(FileNotFoundError):
        store.snapshot_world("no_such_world", "1")
