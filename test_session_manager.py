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


def test_module_instructions_persist_and_survive_settings_saves():
    """Instruction overrides live under the reserved __module_instructions__
    key: they must survive the settings modal's schema-only rebuild, work on
    unloaded saves, sanitize away blanks, and persist across reloads."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_session_module_instructions_data")

    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)

    session = GameSessionManager(data_dir)
    session.create_save("story_one")

    # Never set: reads empty.
    assert session.get_save_module_instructions("story_one") == {}

    # Set on the active save; blanks and malformed entries are dropped.
    stored = session.set_save_module_instructions("story_one", {
        "wb_core_rpg": {"skill_categories": "  Culinary only.  ", "skill_options": "   ", "evolve": 7},
        "bad_module": "not a dict",
    })
    assert stored == {"wb_core_rpg": {"skill_categories": "Culinary only."}}
    assert session.get_save_module_instructions("story_one") == stored

    # The settings modal's per-module payload must not wipe the overrides.
    session.update_module_configs({"wb_core_rpg": {"progression_system": "practice"}})
    assert session.get_save_module_instructions("story_one") == stored

    # Editable on a save that is not the active one.
    session.create_save("story_two")
    assert session.active_save_id == "story_two"
    session.set_save_module_instructions("story_one", {"wb_core_rpg": {"evolve": "Blood price."}})
    assert session.get_save_module_instructions("story_one") == {"wb_core_rpg": {"evolve": "Blood price."}}
    assert session.get_save_module_instructions("story_two") == {}

    # Survives a reload from disk.
    session.load_save("story_one")
    assert session.get_save_module_instructions("story_one") == {"wb_core_rpg": {"evolve": "Blood price."}}
    reloaded = GameSessionManager(data_dir)
    assert reloaded.get_save_module_instructions("story_one") == {"wb_core_rpg": {"evolve": "Blood price."}}

    shutil.rmtree(data_dir)


def _seed_travel_turns(tmp_path):
    """A save where turn 1's storyteller message moved the player: turn 0 has
    the player at the village, turn 1 mid-journey at a waypoint with extra fog
    revealed and an active travel record."""
    session = GameSessionManager(str(tmp_path / "data"))
    session.create_save("autosave")
    sm, sid = session.save_manager, session.active_save_id

    session.state["chat_messages"] = [{"role": "ai", "content": "Intro."}]
    session.state["history"] = ["Intro."]
    session.state["turn"] = 0
    session.state["player_location_node_id"] = "n_village"
    session.state["player_location_map_id"] = "m_root"
    session.state["player_location_region"] = "Vale"
    session.state["revealed_node_ids"] = ["n_village"]
    sm.save_turn(sid, session.state, 0)

    session.state["chat_messages"] += [
        {"role": "user", "content": "Travel to the citadel."},
        {"role": "ai", "content": "You set out along the high road."},
    ]
    session.state["history"] += ["You set out along the high road."]
    session.state["turn"] = 1
    session.state["player_location_node_id"] = "n_waypoint"
    session.state["player_location_region"] = "High Road"
    session.state["revealed_node_ids"] = ["n_village", "n_waypoint"]
    session.state["module_data"]["wb_worldgen"] = {
        "travel": {"phase": "journey", "destination_node_id": "n_citadel"}}
    sm.save_turn(sid, session.state, 1)
    session.begin_turn_swipes()
    return session


def test_delete_last_turn_rolls_back_location_fog_and_travel(tmp_path):
    """Regression: turn snapshots didn't include Core/metadata.json and
    undo_turn preserved it wholesale, so deleting a travel turn reverted the
    journey record (Module_States) but left the player standing at the
    advanced waypoint with fog already opened."""
    session = _seed_travel_turns(tmp_path)
    state = session.delete_message(2)
    assert state["turn"] == 0
    assert state["player_location_node_id"] == "n_village"
    assert state["player_location_region"] == "Vale"
    assert state["revealed_node_ids"] == ["n_village"]
    assert (state["module_data"].get("wb_worldgen") or {}).get("travel") is None


def test_prepare_regenerate_rolls_back_location(tmp_path):
    """Same gap on the regenerate path: the fresh generation must start from
    where the player stood before the turn, not where the discarded turn
    left them."""
    session = _seed_travel_turns(tmp_path)
    assert session.prepare_regenerate() == 1
    assert session.state["player_location_node_id"] == "n_village"
    assert session.state["revealed_node_ids"] == ["n_village"]


def test_undo_with_legacy_snapshot_keeps_live_metadata(tmp_path):
    """Snapshots created before metadata.json was included must keep today's
    behavior: the live metadata (location included) survives untouched."""
    import zipfile

    session = _seed_travel_turns(tmp_path)
    snap = tmp_path / "data" / "saves" / session.active_save_id / "Snapshots" / "turn_0.zip"
    with zipfile.ZipFile(snap, "r") as zf:
        members = {n: zf.read(n) for n in zf.namelist()
                   if n.replace("\\", "/") != "Core/metadata.json"}
    with zipfile.ZipFile(snap, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)

    state = session.delete_message(2)
    assert state["turn"] == 0
    assert state["player_location_node_id"] == "n_waypoint"
    assert state["revealed_node_ids"] == ["n_village", "n_waypoint"]


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
