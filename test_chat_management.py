import json
import os
import shutil

import pytest

from backend.engine.session import GameSessionManager


@pytest.fixture()
def session(tmp_path):
    return GameSessionManager(str(tmp_path / "data"))


def _play_turn(session, user_text, narration, turn):
    session.set_input(user_text)
    final_state = {
        **session.state,
        "history": session.state.get("history", []) + [narration],
        "turn": turn,
    }
    session.save_completed_turn(final_state)


def test_list_saves_includes_display_metadata(session):
    _play_turn(session, "I look around.", "You see a room.", 1)
    saves = session.list_saves()
    autosave = next(s for s in saves if s["id"] == "autosave")
    assert autosave["display_name"] == "autosave"
    assert autosave["turn"] == 1
    assert autosave["last_played"]


def test_rename_save(session):
    result = session.rename_save("autosave", "  The Sunken City  ")
    assert result["display_name"] == "The Sunken City"
    saves = session.list_saves()
    autosave = next(s for s in saves if s["id"] == "autosave")
    assert autosave["display_name"] == "The Sunken City"
    with pytest.raises(ValueError):
        session.rename_save("autosave", "   ")


def test_branch_at_current_turn(session):
    _play_turn(session, "I look around.", "You see a room.", 1)
    _play_turn(session, "I open the door.", "The door creaks open.", 2)

    branch = session.branch_save("autosave")
    assert branch["id"] == "autosave-b1"
    assert branch["turn"] == 2
    assert "branch @ turn 2" in branch["display_name"]

    # Branch has the full transcript; the source is untouched and still active.
    branch_state = session.save_manager.load_save("autosave-b1")
    assert branch_state["core"]["metadata"]["turn"] == 2
    assert len(branch_state["core"]["chat_history"]) == 2
    assert session.active_save_id == "autosave"
    assert session.state["turn"] == 2

    # A second branch gets the next free id.
    assert session.branch_save("autosave")["id"] == "autosave-b2"


def test_branch_at_earlier_turn_rolls_back(session):
    _play_turn(session, "I look around.", "You see a room.", 1)
    _play_turn(session, "I open the door.", "The door creaks open.", 2)

    branch = session.branch_save("autosave", target_turn=1)
    branch_state = session.save_manager.load_save(branch["id"])
    assert branch_state["core"]["metadata"]["turn"] == 1
    assert branch_state["core"]["chat_history"] == ["You see a room."]
    # Source keeps both turns.
    assert session.state["history"] == ["You see a room.", "The door creaks open."]


def test_branch_rejects_bad_turn_and_duplicate_id(session):
    _play_turn(session, "I look around.", "You see a room.", 1)
    with pytest.raises(ValueError):
        session.branch_save("autosave", target_turn=5)
    session.branch_save("autosave", new_save_id="fork_one")
    with pytest.raises(FileExistsError):
        session.branch_save("autosave", new_save_id="fork_one")


def test_export_transcript_formats(session):
    _play_turn(session, "I shout hello.", "The echo answers.", 1)
    session.rename_save("autosave", "Echo Cave")

    md, md_type, md_name = session.export_transcript("autosave", "md")
    assert md.startswith("# Echo Cave")
    assert "**You:** I shout hello." in md
    assert "The echo answers." in md
    assert md_name == "autosave.md"
    assert "markdown" in md_type

    txt, _, txt_name = session.export_transcript("autosave", "txt")
    assert "You: I shout hello." in txt
    assert txt_name == "autosave.txt"

    jsonl, jsonl_type, _ = session.export_transcript("autosave", "jsonl")
    lines = [json.loads(line) for line in jsonl.strip().splitlines()]
    assert lines[0]["role"] == "user"
    assert lines[1]["role"] == "ai"
    assert "ndjson" in jsonl_type

    with pytest.raises(ValueError):
        session.export_transcript("autosave", "docx")
    with pytest.raises(FileNotFoundError):
        session.export_transcript("no_such_save", "md")
