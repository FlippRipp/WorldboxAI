"""Tests for the Core RPG skill-editing API (full character sheet editing)."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_core_rpg" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_core_rpg_backend_skill_edit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_client(mod, state=None, active_save_id="save1", cheats_enabled=True):
    saved = []

    def save_turn(save_id, st, turn):
        saved.append({"save_id": save_id, "turn": turn})

    session_manager = SimpleNamespace(
        active_save_id=active_save_id,
        state=state if state is not None else _default_state(),
        save_manager=SimpleNamespace(save_turn=save_turn),
    )
    # Manual character-sheet edits are cheat-gated server-side.
    mod.set_services({
        "session_manager": session_manager,
        "settings": {"cheats.enabled": cheats_enabled},
    })

    app = FastAPI()
    app.include_router(mod.get_router(), prefix="/api/modules/wb_core_rpg")
    return TestClient(app), session_manager, saved


def _default_state():
    return {
        "turn": 4,
        "module_data": {
            "wb_core_rpg": {
                "skills": {
                    "swordplay": {
                        "rating": 5,
                        "description": "Trained with the town guard.",
                        "trigger_words": [],
                        "type": "active",
                    },
                    "emberkiss": {
                        "rating": 3,
                        "description": "A boon from the hearth-goddess.",
                        "trigger_words": ["flame", "hearth"],
                        "type": "curse",
                    },
                },
                "practice_counters": {"swordplay": 12},
            }
        },
    }


BASE = "/api/modules/wb_core_rpg/skills"


def test_update_skill_fields():
    mod = _load_backend()
    client, sm, saved = _make_client(mod)

    res = client.put(f"{BASE}/swordplay", json={
        "rating": 8,
        "description": "A master duelist.",
        "trigger_words": ["duel", "parry", "duel"],
        "type": "passive",
    })
    assert res.status_code == 200
    skill = res.json()["skills"]["swordplay"]
    assert skill["rating"] == 8
    assert skill["description"] == "A master duelist."
    assert skill["trigger_words"] == ["duel", "parry"]  # deduped
    assert skill["type"] == "passive"
    # Persisted into the active save at the current turn.
    assert saved == [{"save_id": "save1", "turn": 4}]
    assert sm.state["module_data"]["wb_core_rpg"]["skills"]["swordplay"]["rating"] == 8


def test_partial_update_leaves_other_fields():
    mod = _load_backend()
    client, sm, _ = _make_client(mod)

    res = client.put(f"{BASE}/emberkiss", json={"rating": 6})
    assert res.status_code == 200
    skill = res.json()["skills"]["emberkiss"]
    assert skill["rating"] == 6
    assert skill["description"] == "A boon from the hearth-goddess."
    assert skill["trigger_words"] == ["flame", "hearth"]
    assert skill["type"] == "curse"


def test_rename_skill_moves_practice_counter():
    mod = _load_backend()
    client, sm, _ = _make_client(mod)

    res = client.put(f"{BASE}/swordplay", json={"name": "Fencing"})
    assert res.status_code == 200
    skills = res.json()["skills"]
    assert "swordplay" not in skills
    assert skills["fencing"]["rating"] == 5  # stored lowercase
    counters = sm.state["module_data"]["wb_core_rpg"]["practice_counters"]
    assert counters == {"fencing": 12}


def test_rename_collision_rejected():
    mod = _load_backend()
    client, _, saved = _make_client(mod)

    res = client.put(f"{BASE}/swordplay", json={"name": "Emberkiss"})
    assert res.status_code == 409
    assert saved == []


def test_lookup_is_case_insensitive():
    mod = _load_backend()
    client, _, _ = _make_client(mod)

    res = client.put(f"{BASE}/SwordPlay", json={"rating": 9})
    assert res.status_code == 200
    assert res.json()["skills"]["swordplay"]["rating"] == 9


@pytest.mark.parametrize("payload", [
    {"rating": 0},
    {"rating": 11},
    {"type": "cursed"},
    {"name": "   "},
])
def test_invalid_updates_rejected(payload):
    mod = _load_backend()
    client, _, saved = _make_client(mod)

    res = client.put(f"{BASE}/swordplay", json=payload)
    assert res.status_code == 400
    assert saved == []


def test_update_unknown_skill_404():
    mod = _load_backend()
    client, _, _ = _make_client(mod)

    res = client.put(f"{BASE}/basketweaving", json={"rating": 2})
    assert res.status_code == 404


def test_add_skill():
    mod = _load_backend()
    client, sm, saved = _make_client(mod)

    res = client.post(BASE, json={
        "name": "Stealth",
        "rating": 4,
        "description": "Moves unseen in shadow.",
        "trigger_words": ["sneak"],
        "type": "active",
    })
    assert res.status_code == 200
    skill = res.json()["skills"]["stealth"]
    assert skill == {
        "rating": 4,
        "description": "Moves unseen in shadow.",
        "trigger_words": ["sneak"],
        "type": "active",
    }
    assert len(saved) == 1


def test_add_skill_defaults_and_duplicate():
    mod = _load_backend()
    client, _, _ = _make_client(mod)

    res = client.post(BASE, json={"name": "haggling"})
    assert res.status_code == 200
    skill = res.json()["skills"]["haggling"]
    assert skill == {"rating": 3, "description": "", "trigger_words": [], "type": "active"}

    dup = client.post(BASE, json={"name": "Haggling"})
    assert dup.status_code == 409

    unnamed = client.post(BASE, json={"rating": 5})
    assert unnamed.status_code == 400


def test_delete_skill_clears_practice_counter():
    mod = _load_backend()
    client, sm, saved = _make_client(mod)

    res = client.delete(f"{BASE}/swordplay")
    assert res.status_code == 200
    assert "swordplay" not in res.json()["skills"]
    assert sm.state["module_data"]["wb_core_rpg"]["practice_counters"] == {}
    assert len(saved) == 1

    missing = client.delete(f"{BASE}/swordplay")
    assert missing.status_code == 404


def test_skill_editing_requires_cheat_mode():
    mod = _load_backend()
    client, sm, saved = _make_client(mod, cheats_enabled=False)

    assert client.put(f"{BASE}/swordplay", json={"rating": 9}).status_code == 403
    assert client.post(BASE, json={"name": "haggling"}).status_code == 403
    assert client.delete(f"{BASE}/swordplay").status_code == 403
    # State untouched, nothing persisted.
    skills = sm.state["module_data"]["wb_core_rpg"]["skills"]
    assert skills["swordplay"]["rating"] == 5
    assert "haggling" not in skills
    assert saved == []


def test_no_active_save_409():
    mod = _load_backend()
    client, _, _ = _make_client(mod, active_save_id=None)

    assert client.put(f"{BASE}/swordplay", json={"rating": 5}).status_code == 409
    assert client.post(BASE, json={"name": "x"}).status_code == 409
    assert client.delete(f"{BASE}/swordplay").status_code == 409


def test_missing_module_data_404():
    mod = _load_backend()
    client, _, _ = _make_client(mod, state={"turn": 0, "module_data": {}})

    res = client.put(f"{BASE}/swordplay", json={"rating": 5})
    assert res.status_code == 404


def test_router_mounted_on_server():
    """The real server mounts the module router under /api/modules/wb_core_rpg."""
    import backend.api.server as server

    mod = server.registry.get_modules()["wb_core_rpg"]["backend"]
    saved = []
    session_manager = SimpleNamespace(
        active_save_id="srv_save",
        state=_default_state(),
        save_manager=SimpleNamespace(save_turn=lambda sid, st, t: saved.append((sid, t))),
    )
    old_services = dict(getattr(mod, "_services", {}) or {})
    mod.set_services({
        "session_manager": session_manager,
        "settings": {"cheats.enabled": True},
    })
    try:
        client = TestClient(server.app)
        res = client.put("/api/modules/wb_core_rpg/skills/swordplay", json={"rating": 9})
        assert res.status_code == 200
        assert res.json()["skills"]["swordplay"]["rating"] == 9
        assert saved == [("srv_save", 4)]
    finally:
        mod.set_services(old_services)
