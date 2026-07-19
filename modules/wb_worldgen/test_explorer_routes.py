"""World-explorer server surface: /compiled serves the merged world (child
bundles, surgery connections and the brief included; private keys stripped),
/regenerate-step regenerates one step of a saved world in place — never
through the session-draft machinery, so no phantom draft copies appear and
completion status is untouched — and the world list carries has_agent_build
so an in-progress world's recovery routes to the observer (artifact exists)
or to a fresh adopt run (none does).
"""

import asyncio
import json

import pytest
from fastapi import HTTPException

import routes as world_routes
from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import harness as agent_harness


@pytest.fixture
def builder(tmp_path):
    return register_default_steps(WorldBuilder(worlds_dir=str(tmp_path)))


@pytest.fixture
def routed(builder):
    old = world_routes.world_builder
    world_routes.world_builder = builder
    yield builder
    world_routes.world_builder = old


def _saved_world(builder, world_id="explored"):
    nodes = [
        {"id": "n0", "type": "town", "importance": 8, "x": 0.0, "y": 0.0,
         "name": "Harbor", "description": "", "region": ""},
        {"id": "n1", "type": "landmark", "importance": 4, "x": 1.0, "y": 0.0,
         "name": "", "description": "", "region": ""},
    ]
    return builder.save_world(world_id, {
        "seed_prompt": "an island world",
        "brief": {"prompt": "an island world", "rules": ["always islands"],
                  "notes": []},
        "steps": {
            "lore": {"data": {"world_name": "Isles", "description": "Salt."},
                     "approved": True},
            # Step data stays legacy-shaped on disk (fresh worlds included);
            # compile migrates it to the v2 maps/root_map_id shape.
            "map_generation": {
                "data": {"nodes": nodes,
                         "edges": [{"from": "n0", "to": "n1"}]},
                "approved": True,
            },
        },
    })


def test_compiled_serves_merged_world(routed):
    wid = _saved_world(routed)
    # A post-generation child map: lives in its own bundle file, not in the
    # map_generation step data — exactly what the old step-data review view
    # could never show.
    routed.services.enrichment_store.save_child_map(wid, {
        "map": {"map_id": "harbor_interior", "label": "Harbor Interior",
                "parent_map_id": "root", "anchor_node_id": "n0",
                "nodes": [], "edges": []},
        "connections": [],
    })

    resp = asyncio.run(world_routes.get_compiled_world(wid))
    compiled = resp["compiled"]

    assert compiled["root_map_id"] == "root"
    assert set(compiled["maps"]) == {"root", "harbor_interior"}
    assert compiled["lore"]["world_name"] == "Isles"
    assert compiled["brief"]["rules"] == ["always islands"]
    assert not any(str(k).startswith("_") for k in compiled)


def test_compiled_404s_for_unknown_world(routed):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(world_routes.get_compiled_world("nope"))
    assert exc.value.status_code == 404


def test_regenerate_step_persists_in_place(routed):
    wid = _saved_world(routed)
    before = routed.load_world(wid)
    assert before["complete"] is True

    resp = asyncio.run(world_routes.regenerate_saved_world_step(
        wid, "lore",
        world_routes.RegenerateStepRequest(note="saltier")))

    assert resp["step"] == "lore"
    after = routed.load_world(wid)
    assert after["steps"]["lore"]["data"] == resp["data"]
    assert after["steps"]["lore"]["note"] == "saltier"
    # World-scoped on purpose: completion survives and the session-draft
    # machinery never runs (the session route used to phantom-draft here).
    assert after["complete"] is True
    assert [w["id"] for w in routed.list_worlds()] == [wid]
    assert not world_routes.world_draft_ids


def test_regenerate_step_refusals(routed):
    wid = _saved_world(routed)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(world_routes.regenerate_saved_world_step(
            wid, "map_generation", None))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(world_routes.regenerate_saved_world_step(
            wid, "not_a_step", None))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        asyncio.run(world_routes.regenerate_saved_world_step(
            "nope", "lore", None))
    assert exc.value.status_code == 404


def test_list_carries_has_agent_build(routed):
    wid = _saved_world(routed)
    worlds = asyncio.run(world_routes.list_worlds())["worlds"]
    assert worlds[0]["has_agent_build"] is False

    artifact = agent_harness._artifact_path(routed, wid)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"status": "cancelled"}), encoding="utf-8")

    worlds = asyncio.run(world_routes.list_worlds())["worlds"]
    assert worlds[0]["has_agent_build"] is True
    # Builders without a store (test fakes) just report no artifact.
    assert agent_harness.has_build_artifact(object(), wid) is False


def test_agent_build_route_passes_world_id_through(routed, monkeypatch):
    captured = {}

    def fake_start(builder, seed_prompt, **kwargs):
        captured.update(kwargs, seed_prompt=seed_prompt)

        class Handle:
            world_id = kwargs.get("world_id") or "fresh"
            status = "running"

        return Handle()

    monkeypatch.setattr(agent_harness, "start_agent_build", fake_start)
    resp = asyncio.run(world_routes.agent_build_start(
        world_routes.AgentBuildRequest(seed_prompt="finish it",
                                       world_id="explored")))
    assert resp["world_id"] == "explored"
    assert captured["world_id"] == "explored"

    def busy_start(builder, seed_prompt, **kwargs):
        raise ValueError("An agent build is already running for 'explored'")

    monkeypatch.setattr(agent_harness, "start_agent_build", busy_start)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(world_routes.agent_build_start(
            world_routes.AgentBuildRequest(seed_prompt="finish it",
                                           world_id="explored")))
    assert exc.value.status_code == 409
