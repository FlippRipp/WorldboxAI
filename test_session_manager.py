import os
import shutil

from backend.engine.session import GameSessionManager
from backend.engine.prompt_pipeline import default_prompt_pipeline


def test_session_manager_persists_active_state():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    session = GameSessionManager(data_dir)
    assert session.active_save_id == "autosave"
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
    assert any(save["id"] == "autosave" for save in session.list_saves())

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

    autosave_state = session.load_save("autosave")
    assert session.active_save_id == "autosave"
    assert autosave_state["turn"] == 0

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

    # Deleting the active save resets the marker: next boot falls back.
    reloaded.delete_save("story_two")
    fallback = GameSessionManager(data_dir)
    assert fallback.active_save_id == "autosave"

    shutil.rmtree(data_dir)


def test_session_manager_ignores_broken_active_marker():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_marker_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    GameSessionManager(data_dir)  # creates autosave

    marker = os.path.join(data_dir, "saves", "active_save.json")

    # Corrupt marker → autosave.
    with open(marker, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert GameSessionManager(data_dir).active_save_id == "autosave"

    # Marker pointing at a save that no longer exists → autosave.
    with open(marker, "w", encoding="utf-8") as f:
        f.write('{"save_id": "vanished_story"}')
    assert GameSessionManager(data_dir).active_save_id == "autosave"

    # Marker with an invalid id (path traversal chars) → autosave.
    with open(marker, "w", encoding="utf-8") as f:
        f.write('{"save_id": "../evil"}')
    assert GameSessionManager(data_dir).active_save_id == "autosave"

    shutil.rmtree(data_dir)


if __name__ == "__main__":
    test_session_manager_persists_active_state()
    test_session_manager_save_lifecycle()
    test_session_manager_restores_last_active_save_on_boot()
    test_session_manager_ignores_broken_active_marker()
