import os
import shutil

import pytest

from backend.engine.session import GameSessionManager
from backend.engine.prompt_pipeline import default_prompt_pipeline


def test_session_manager_persists_active_state():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    session = GameSessionManager(data_dir)
    # No implicit default save: nothing is active until a story is created.
    assert session.active_save_id is None
    assert session.state["turn"] == 0

    session.create_save("story_one")
    assert session.active_save_id == "story_one"
    assert session.state["turn"] == 0
    assert session.state["module_data"]["wb_core_rpg"]["hp"] == 85

    session.set_input("I remember this input.")
    session.state["module_data"]["wb_core_rpg"]["hp"] = 77
    session.update_module_configs({"wb_core_rpg": {"progression_system": "practice"}})
    custom_pipeline = default_prompt_pipeline()
    custom_pipeline[0]["config"]["text"] = "You narrate with sharp, practical detail."
    session.update_prompt_pipeline(custom_pipeline)
    final_state = {
        **session.state,
        "history": ["A persistent thing happened."],
        "turn": 1,
    }
    session.save_completed_turn(final_state)

    reloaded = GameSessionManager(data_dir)
    assert reloaded.active_save_id == "story_one"
    assert reloaded.state["turn"] == 1
    assert reloaded.state["history"] == ["A persistent thing happened."]
    messages = reloaded.state["chat_messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "I remember this input."),
        ("ai", "A persistent thing happened."),
    ]
    # Both messages carry display metadata (timestamp at minimum).
    assert all(m["meta"]["ts"] for m in messages)
    assert reloaded.state["module_data"]["wb_core_rpg"]["hp"] == 77
    assert reloaded.state["module_configs"]["wb_core_rpg"]["progression_system"] == "practice"
    assert reloaded.state["prompt_pipeline"][0]["config"]["text"] == "You narrate with sharp, practical detail."

    shutil.rmtree(data_dir)
    print("Session manager persistence test passed.")


def test_session_manager_save_lifecycle():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_lifecycle_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    session = GameSessionManager(data_dir)
    # A fresh data dir has no saves at all.
    assert session.list_saves() == []

    session.create_save("first_save")
    new_state = session.create_save("second_save")
    assert session.active_save_id == "second_save"
    assert new_state["turn"] == 0
    assert any(save["id"] == "second_save" and save["active"] for save in session.list_saves())

    session.state["history"] = ["Turn one", "Turn two"]
    session.state["module_data"]["wb_core_rpg"]["hp"] = 70
    session.state["turn"] = 1
    session.save_completed_turn(session.state)
    session.state["module_data"]["wb_core_rpg"]["hp"] = 60
    session.state["turn"] = 2
    session.save_completed_turn(session.state)

    restored = session.undo_turn(1)
    assert restored["turn"] == 1
    assert restored["module_data"]["wb_core_rpg"]["hp"] == 70

    first_state = session.load_save("first_save")
    assert session.active_save_id == "first_save"
    assert first_state["turn"] == 0

    shutil.rmtree(data_dir)
    print("Session manager lifecycle test passed.")


def test_session_manager_restores_last_active_save_on_boot():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_restore_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    session = GameSessionManager(data_dir)
    session.create_save("story_two")
    session.state["history"] = ["A tale in progress."]
    session.state["turn"] = 1
    session.save_completed_turn(session.state)
    assert session.active_save_id == "story_two"

    # A fresh instance (simulated restart) lands on the same save and state.
    reloaded = GameSessionManager(data_dir)
    assert reloaded.active_save_id == "story_two"
    assert reloaded.state["turn"] == 1
    assert reloaded.state["history"] == ["A tale in progress."]

    # Deleting the active save clears the marker: next boot has no story.
    reloaded.delete_save("story_two")
    assert reloaded.active_save_id is None
    fallback = GameSessionManager(data_dir)
    assert fallback.active_save_id is None

    shutil.rmtree(data_dir)


def test_session_manager_ignores_broken_active_marker():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_marker_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    GameSessionManager(data_dir).create_save("real_story")

    marker = os.path.join(data_dir, "saves", "active_save.json")

    # Corrupt marker → no active story.
    with open(marker, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert GameSessionManager(data_dir).active_save_id is None

    # Marker pointing at a save that no longer exists → no active story.
    with open(marker, "w", encoding="utf-8") as f:
        f.write('{"save_id": "vanished_story"}')
    assert GameSessionManager(data_dir).active_save_id is None

    # Marker with an invalid id (path traversal chars) → no active story.
    with open(marker, "w", encoding="utf-8") as f:
        f.write('{"save_id": "../evil"}')
    assert GameSessionManager(data_dir).active_save_id is None

    shutil.rmtree(data_dir)


def test_update_module_configs_preserves_active_modules():
    """The in-game settings modal only sends per-module schema values. That
    write must not wipe the reserved ``__active_modules__`` key, or every
    toggled-off module silently re-enables on the next reload."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_active_modules_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    session = GameSessionManager(data_dir)
    session.create_save("story_one")

    # User toggles a module off (persisted under the reserved key).
    session.set_save_active_modules("story_one", ["wb_core_rpg"])
    assert session.get_save_active_modules("story_one") == ["wb_core_rpg"]

    # User then saves module settings from the in-game modal — a payload with
    # only per-module schema values, no reserved key.
    session.update_module_configs({"wb_core_rpg": {"progression_system": "practice"}})

    # The reserved set survives, both in memory and on disk after reload.
    assert session.get_save_active_modules("story_one") == ["wb_core_rpg"]
    session.save_completed_turn({**session.state, "turn": 1})

    reloaded = GameSessionManager(data_dir)
    assert reloaded.get_save_active_modules("story_one") == ["wb_core_rpg"]
    assert reloaded.state["module_configs"]["wb_core_rpg"]["progression_system"] == "practice"

    shutil.rmtree(data_dir)


def test_session_manager_refuses_turns_without_active_save():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_no_save_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    session = GameSessionManager(data_dir)
    assert session.get_memory_path() is None
    assert session.swipes_meta() is None
    assert session.get_status()["active_save_id"] is None

    with pytest.raises(ValueError):
        session.save_completed_turn(dict(session.state))
    with pytest.raises(ValueError):
        session.update_module_configs({})

    shutil.rmtree(data_dir)


if __name__ == "__main__":
    test_session_manager_persists_active_state()
    test_session_manager_save_lifecycle()
    test_session_manager_restores_last_active_save_on_boot()
    test_session_manager_ignores_broken_active_marker()
    test_update_module_configs_preserves_active_modules()
    test_session_manager_refuses_turns_without_active_save()
