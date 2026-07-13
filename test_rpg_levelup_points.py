"""Tests for level-up attribute/skill point banking and the /levelup/spend API."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

STATS = ["power", "agility", "vitality", "intelligence", "spirit", "charm"]


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_core_rpg" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_core_rpg_backend_levelup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _char(**overrides):
    base = {
        "stats": {s: 10 for s in STATS},
        "skills": {},
        "level": 1,
        "xp": 0,
        "hp": 85,
        "max_hp": 85,
    }
    base.update(overrides)
    return base


def _state(config=None, char=None):
    return {
        "module_configs": {"wb_core_rpg": config or {}},
        "module_data": {"wb_core_rpg": char or _char()},
    }


def _run(backend, mutation, config, char):
    return asyncio.run(backend.on_mutate_state(mutation, _state(config, char), None))


# ---------------------------------------------------------------------------
# Level-up banking (on_mutate_state)
# ---------------------------------------------------------------------------

def test_level_up_banks_points_and_skips_auto_stats():
    backend = _load_backend()
    char = _char(xp=45, action_assessment={"feasibility": 8, "difficulty": "moderate"})
    result = _run(backend, {}, {"xp_per_action": 10}, char)
    rpg = result["module_data"]["wb_core_rpg"]
    assert rpg["level"] == 2
    assert rpg["unspent_attribute_points"] == 2
    assert rpg["unspent_skill_points"] == 1
    # No automatic stat assignment anymore.
    assert all(rpg["stats"][s] == 10 for s in STATS)
    assert rpg["level_up_history"][-1]["level"] == 2


def test_points_per_level_are_configurable():
    backend = _load_backend()
    char = _char(xp=45, action_assessment={"feasibility": 8, "difficulty": "moderate"})
    config = {"xp_per_action": 10, "attribute_points_per_level": 4, "skill_points_per_level": 3}
    rpg = _run(backend, {}, config, char)["module_data"]["wb_core_rpg"]
    assert rpg["unspent_attribute_points"] == 4
    assert rpg["unspent_skill_points"] == 3


def test_multi_level_up_in_one_turn_accumulates_points():
    backend = _load_backend()
    # 50 (L2) + 200 (L3) = 250 total XP needed; start just below and gain enough
    # for two levels at once.
    char = _char(xp=240, action_assessment={"feasibility": 8, "difficulty": "extreme"})
    rpg = _run(backend, {}, {"xp_per_action": 10}, char)["module_data"]["wb_core_rpg"]
    assert rpg["level"] == 3
    assert rpg["unspent_attribute_points"] == 4
    assert rpg["unspent_skill_points"] == 2


def test_milestone_mode_banks_points():
    backend = _load_backend()
    char = _char()
    rpg = _run(backend, {"hp_change": -25}, {"progression_system": "milestone"}, char)["module_data"]["wb_core_rpg"]
    assert rpg["level"] == 2
    assert rpg["unspent_attribute_points"] == 2
    assert rpg["unspent_skill_points"] == 1


def test_practice_mode_never_banks_points():
    backend = _load_backend()
    char = _char(
        skills={"swordplay": {"rating": 3, "description": "", "trigger_words": [], "type": "active"}},
        practice_counters={"swordplay": 100},
    )
    rpg = _run(backend, {}, {"progression_system": "practice"}, char)["module_data"]["wb_core_rpg"]
    assert rpg["unspent_attribute_points"] == 0
    assert rpg["unspent_skill_points"] == 0


def test_points_and_evolution_fields_survive_round_trip():
    backend = _load_backend()
    data = _char(
        unspent_attribute_points=3,
        unspent_skill_points=2,
        pending_evolutions=[{"skill": "swordplay", "options": None, "status": "deferred"}],
        level_up_history=[{"level": 2}],
        skills={
            "swordplay": {
                "rating": 10,
                "description": "d",
                "trigger_words": ["x"],
                "type": "active",
                "tier": 3,
                "lineage": ["blades", "sharp blades"],
                "evolution_theme": "Brutal",
            }
        },
    )
    out = backend.Character.from_dict(data).to_dict()
    assert out["unspent_attribute_points"] == 3
    assert out["unspent_skill_points"] == 2
    assert out["pending_evolutions"] == [{"skill": "swordplay", "options": None, "status": "deferred"}]
    assert out["level_up_history"] == [{"level": 2}]
    skill = out["skills"]["swordplay"]
    # Regression: from_dict used to rebuild skills with hard-coded keys, which
    # would silently erase tier/lineage/evolution_theme every turn.
    assert skill["tier"] == 3
    assert skill["lineage"] == ["blades", "sharp blades"]
    assert skill["evolution_theme"] == "Brutal"


def test_max_rating_skill_is_queued_for_evolution():
    backend = _load_backend()
    char = _char(skills={"swordplay": {"rating": 9, "description": "", "trigger_words": [], "type": "active"}})
    rpg = _run(backend, {"skill_changes": {"swordplay": 1}}, {}, char)["module_data"]["wb_core_rpg"]
    assert rpg["skills"]["swordplay"]["rating"] == 10
    assert rpg["pending_evolutions"] == [{"skill": "swordplay", "options": None, "status": "pending"}]


def test_maxed_curse_is_never_queued():
    backend = _load_backend()
    char = _char(skills={"hexed": {"rating": 9, "description": "", "trigger_words": [], "type": "curse"}})
    rpg = _run(backend, {"skill_changes": {"hexed": 1}}, {}, char)["module_data"]["wb_core_rpg"]
    assert rpg["skills"]["hexed"]["rating"] == 10
    assert rpg["pending_evolutions"] == []


def test_stale_pending_entry_is_pruned_when_rating_drops():
    backend = _load_backend()
    char = _char(
        skills={"swordplay": {"rating": 10, "description": "", "trigger_words": [], "type": "active"}},
        pending_evolutions=[{"skill": "swordplay", "options": None, "status": "pending"}],
    )
    rpg = _run(backend, {"skill_changes": {"swordplay": -2}}, {}, char)["module_data"]["wb_core_rpg"]
    assert rpg["skills"]["swordplay"]["rating"] == 8
    assert rpg["pending_evolutions"] == []


# ---------------------------------------------------------------------------
# POST /levelup/spend
# ---------------------------------------------------------------------------

BASE = "/api/modules/wb_core_rpg"


def _make_client(mod, rpg, config=None):
    saved = []

    def save_turn(save_id, st, turn):
        saved.append({"save_id": save_id, "turn": turn})

    session_manager = SimpleNamespace(
        active_save_id="save1",
        state={
            "turn": 4,
            "module_configs": {"wb_core_rpg": config or {}},
            "module_data": {"wb_core_rpg": rpg},
        },
        save_manager=SimpleNamespace(save_turn=save_turn),
    )
    mod.set_services({"session_manager": session_manager})

    app = FastAPI()
    app.include_router(mod.get_router(), prefix=BASE)
    return TestClient(app), session_manager, saved


def _spend_rpg(**overrides):
    base = _char(
        level=3,
        unspent_attribute_points=3,
        unspent_skill_points=4,
        skills={
            "swordplay": {"rating": 5, "description": "", "trigger_words": [], "type": "active"},
            "hexed": {"rating": 4, "description": "", "trigger_words": [], "type": "curse"},
        },
        practice_counters={},
        pending_evolutions=[],
    )
    base.update(overrides)
    return base


def test_spend_happy_path():
    mod = _load_backend()
    client, sm, saved = _make_client(mod, _spend_rpg())

    res = client.post(f"{BASE}/levelup/spend", json={
        "stat_allocations": {"power": 2, "agility": 1},
        "skill_allocations": {"swordplay": 2},
    })
    assert res.status_code == 200
    rpg = res.json()
    assert rpg["stats"]["power"] == 12
    assert rpg["stats"]["agility"] == 11
    assert rpg["skills"]["swordplay"]["rating"] == 7
    assert rpg["unspent_attribute_points"] == 0
    assert rpg["unspent_skill_points"] == 2
    assert saved  # persisted


def test_spend_is_partial_and_banks_leftovers():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _spend_rpg())
    res = client.post(f"{BASE}/levelup/spend", json={"stat_allocations": {"spirit": 1}})
    assert res.status_code == 200
    assert res.json()["unspent_attribute_points"] == 2
    assert res.json()["unspent_skill_points"] == 4


def test_overspend_attribute_points_rejected():
    mod = _load_backend()
    client, sm, _ = _make_client(mod, _spend_rpg())
    res = client.post(f"{BASE}/levelup/spend", json={"stat_allocations": {"power": 4}})
    assert res.status_code == 400
    # Nothing was mutated.
    assert sm.state["module_data"]["wb_core_rpg"]["stats"]["power"] == 10
    assert sm.state["module_data"]["wb_core_rpg"]["unspent_attribute_points"] == 3


def test_stat_cap_respected():
    mod = _load_backend()
    rpg = _spend_rpg()
    rpg["stats"]["power"] = 19
    client, _, _ = _make_client(mod, rpg, config={"max_stat_value": 20})
    res = client.post(f"{BASE}/levelup/spend", json={"stat_allocations": {"power": 2}})
    assert res.status_code == 400


def test_skill_rating_cap_respected():
    mod = _load_backend()
    rpg = _spend_rpg()
    rpg["skills"]["swordplay"]["rating"] = 9
    client, _, _ = _make_client(mod, rpg)
    res = client.post(f"{BASE}/levelup/spend", json={"skill_allocations": {"swordplay": 2}})
    assert res.status_code == 400


def test_curse_cannot_be_raised_with_points():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _spend_rpg())
    res = client.post(f"{BASE}/levelup/spend", json={"skill_allocations": {"hexed": 1}})
    assert res.status_code == 400


def test_new_skill_purchase():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _spend_rpg(), config={"new_skill_cost": 3})
    res = client.post(f"{BASE}/levelup/spend", json={
        "new_skill": {"name": "Herbalism", "type": "active", "description": "Knows the woods."},
    })
    assert res.status_code == 200
    rpg = res.json()
    assert rpg["skills"]["herbalism"]["rating"] == 3
    assert rpg["unspent_skill_points"] == 1


def test_new_skill_needs_enough_points():
    mod = _load_backend()
    rpg = _spend_rpg(unspent_skill_points=2)
    client, _, _ = _make_client(mod, rpg, config={"new_skill_cost": 3})
    res = client.post(f"{BASE}/levelup/spend", json={"new_skill": {"name": "Herbalism"}})
    assert res.status_code == 400


def test_new_skill_collision_rejected():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _spend_rpg())
    res = client.post(f"{BASE}/levelup/spend", json={"new_skill": {"name": "Swordplay"}})
    assert res.status_code == 409


def test_vitality_spend_recalcs_max_hp_preserving_ratio():
    mod = _load_backend()
    rpg = _spend_rpg()
    rpg["hp"] = 38  # half of 76
    rpg["max_hp"] = 76  # vit 10 * 7 + level 3 * 2
    client, _, _ = _make_client(mod, rpg)
    res = client.post(f"{BASE}/levelup/spend", json={"stat_allocations": {"vitality": 2}})
    assert res.status_code == 200
    out = res.json()
    assert out["max_hp"] == 12 * 7 + 3 * 2  # 90
    assert out["hp"] == 45  # ratio preserved


def test_spend_to_max_queues_evolution():
    mod = _load_backend()
    rpg = _spend_rpg()
    rpg["skills"]["swordplay"]["rating"] = 8
    client, _, _ = _make_client(mod, rpg)
    res = client.post(f"{BASE}/levelup/spend", json={"skill_allocations": {"swordplay": 2}})
    assert res.status_code == 200
    out = res.json()
    assert out["skills"]["swordplay"]["rating"] == 10
    assert out["pending_evolutions"] == [{"skill": "swordplay", "options": None, "status": "pending"}]


def test_no_active_save_conflict():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _spend_rpg())
    mod.set_services({"session_manager": SimpleNamespace(active_save_id=None)})
    res = client.post(f"{BASE}/levelup/spend", json={"stat_allocations": {"power": 1}})
    assert res.status_code == 409
