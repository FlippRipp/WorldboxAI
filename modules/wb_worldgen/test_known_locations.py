"""Story-start known-locations pass tests.

After the opening message, ``on_intro_complete`` sends every detailed
still-hidden node to the LLM in batches and adds the ids it returns to the
fog-of-war reveal set. Run by path with the venv python:

    .venv/Scripts/python -m pytest modules/wb_worldgen/test_known_locations.py
"""
import asyncio
import importlib.util
import json
import os

import pytest

# The module file is named backend.py, which collides with the core `backend`
# package — load it explicitly by path under a private name.
_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
_spec = importlib.util.spec_from_file_location(
    "wb_worldgen_backend_under_test", os.path.join(_MOD_DIR, "backend.py")
)
wbg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wbg)


class ScriptedLLM:
    mode = "live"
    reader_model = "reader-model"

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def simple_completion(self, messages=None, **kwargs):
        self.calls.append(messages)
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload)


class FakeEngine:
    def __init__(self, llm):
        self.llm = llm


class FakeSettings:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key, 2)


def make_world():
    return {
        "world_format": 2,
        "root_map_id": "root",
        "maps": {
            "root": {
                "map_id": "root",
                "label": "Overworld",
                "nodes": [
                    {"id": "n_home", "name": "Thornwick", "type": "settlement",
                     "region": "West", "importance": 4, "x": 0, "y": 0,
                     "description": "A quiet farming village."},
                    {"id": "n_capital", "name": "Aurelia", "type": "settlement",
                     "region": "East", "importance": 9, "x": 30, "y": 0,
                     "description": "The gleaming capital every child hears tales of."},
                    {"id": "n_secret", "name": "Hollow Vault", "type": "landmark",
                     "region": "East", "importance": 2, "x": 40, "y": 10,
                     "description": "A sealed vault lost to living memory."},
                    {"id": "n_unnamed", "type": "waypoint", "x": 10, "y": 0},
                    {"id": "n_nodesc", "name": "Milford", "type": "settlement",
                     "x": 20, "y": 0},
                ],
                "edges": [
                    {"from": "n_home", "to": "n_unnamed"},
                    {"from": "n_unnamed", "to": "n_nodesc"},
                    {"from": "n_nodesc", "to": "n_capital"},
                ],
            }
        },
        "connections": [],
        "lore": {"world_name": "Aeria", "premise": "A realm of drifting isles."},
    }


def make_state(world):
    return {
        "world_data": world,
        "player_location_node_id": "n_home",
        "player_location_region": "West",
        "revealed_node_ids": ["n_home"],
        "characters": {"default_player": {"name": "Rin", "race": "human"}},
        "module_data": {"wb_core_rpg": {
            "backstory": "Raised in Thornwick, Rin dreamed of the capital.",
        }},
        "history": ["Dawn breaks over Thornwick as Rin packs for Aurelia."],
    }


@pytest.fixture(autouse=True)
def services():
    yield
    wbg._services = None


def run_pass(state, llm):
    wbg._services = {"engine": FakeEngine(llm),
                     "settings": FakeSettings({"world.enrichment_concurrency": 2})}
    return asyncio.run(wbg.on_intro_complete(state, None))


def test_reveals_only_valid_ids_and_keeps_existing_reveals():
    llm = ScriptedLLM([{"known_node_ids": ["n_capital", "n_bogus"]}])
    state = make_state(make_world())

    result = run_pass(state, llm)

    assert result["revealed_node_ids"] == ["n_home", "n_capital"]
    assert len(llm.calls) == 1


def test_prompt_carries_character_world_and_only_candidate_nodes():
    llm = ScriptedLLM([{"known_node_ids": []}])
    state = make_state(make_world())

    result = run_pass(state, llm)

    assert result == {}  # nothing new to reveal
    user_msg = llm.calls[0][1]["content"]
    # Candidates: detailed and still hidden.
    assert "n_capital" in user_msg and "Hollow Vault" in user_msg
    # Excluded: already revealed, unnamed, undescribed.
    assert "n_home" not in user_msg
    assert "n_unnamed" not in user_msg and "n_nodesc" not in user_msg
    # Judgement context: character, backstory, start, premise, opening scene.
    assert "Rin" in user_msg
    assert "dreamed of the capital" in user_msg
    assert "The story begins at: Thornwick in West." in user_msg
    assert "drifting isles" in user_msg
    assert "Dawn breaks over Thornwick" in user_msg
    # Full node description included, importance surfaced for the LLM.
    assert "every child hears tales of" in user_msg
    assert "importance 9/10" in user_msg


def test_candidates_are_batched_and_results_merged(monkeypatch):
    monkeypatch.setattr(wbg._rt_known, "BATCH_SIZE", 1)
    llm = ScriptedLLM([
        {"known_node_ids": ["n_capital"]},
        {"known_node_ids": ["n_secret"]},
    ])
    state = make_state(make_world())

    result = run_pass(state, llm)

    assert len(llm.calls) == 2
    assert sorted(result["revealed_node_ids"]) == ["n_capital", "n_home", "n_secret"]


def test_failed_batch_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(wbg._rt_known, "BATCH_SIZE", 1)
    llm = ScriptedLLM([
        RuntimeError("provider down"),
        {"known_node_ids": ["n_secret"]},
    ])
    state = make_state(make_world())

    result = run_pass(state, llm)

    assert result["revealed_node_ids"] == ["n_home", "n_secret"]


def test_mock_llm_mode_skips_the_pass():
    llm = ScriptedLLM([{"known_node_ids": ["n_capital"]}])
    llm.mode = "mock"
    state = make_state(make_world())

    assert run_pass(state, llm) == {}
    assert llm.calls == []


def test_no_detailed_hidden_nodes_means_no_llm_call():
    world = make_world()
    state = make_state(world)
    state["revealed_node_ids"] = ["n_home", "n_capital", "n_secret"]
    llm = ScriptedLLM([])

    assert run_pass(state, llm) == {}
    assert llm.calls == []
