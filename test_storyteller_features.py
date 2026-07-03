"""Tests for swipe-regenerate bookkeeping, edit/delete, and reasoning capture.

Swipe/edit/delete logic is exercised directly at the session/save-manager layer
(pure file ops) rather than through the websocket + full LLM pipeline: TestClient
runs websocket handlers across portal threads, and the LanceDB/SQLite memory store
is thread-affine, which is a harness artifact (uvicorn uses a single event loop),
not a product concern. Reasoning is verified end-to-end over a single-turn socket.
"""
from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.session import GameSessionManager
from backend.engine.prompt_pipeline import PromptCompiler, DEFAULT_CONTINUE_PROMPT


def make_client(tmp_path, monkeypatch):
    session_manager = GameSessionManager(str(tmp_path / "data"))
    monkeypatch.setattr(server, "session_manager", session_manager)
    server.engine.memory = None
    server.engine.set_memory_path(session_manager.get_memory_path())
    server.engine.llm.mode = "mock"
    return TestClient(server.app), session_manager


def _seed_two_turns(session):
    """Build a save with an intro (turn 0) and one player turn (turn 1), each with
    a snapshot, and a fresh swipe set for turn 1 — the state right after playing."""
    sm, sid = session.save_manager, session.active_save_id
    session.state["chat_messages"] = [{"role": "ai", "content": "Intro."}]
    session.state["history"] = ["Intro."]
    session.state["turn"] = 0
    sm.save_turn(sid, session.state, 0)

    session.state["chat_messages"] += [
        {"role": "user", "content": "A1"},
        {"role": "ai", "content": "R1"},
    ]
    session.state["history"] += ["R1"]
    session.state["turn"] = 1
    sm.save_turn(sid, session.state, 1)
    session.begin_turn_swipes()


def test_reasoning_is_captured_and_streamed(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"text": "I look around."})
        saw_reasoning_token = False
        done = None
        for _ in range(256):
            msg = ws.receive_json()
            if msg["type"] == "reasoning_token":
                saw_reasoning_token = True
            if msg["type"] == "done":
                done = msg
                break
            if msg["type"] == "error":
                raise AssertionError(f"error: {msg.get('detail')}")
    assert saw_reasoning_token, "expected a reasoning_token stream"
    ai = done["state"]["chat_messages"][-1]
    assert ai["role"] == "ai" and ai.get("reasoning"), "ai message should carry reasoning"


def test_swipe_manifest_and_roundtrip(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    _seed_two_turns(session)
    sm, sid = session.save_manager, session.active_save_id

    manifest = sm.load_swipe_manifest(sid)
    assert manifest == {"turn": 1, "user_input": "A1", "active": 0, "count": 1}

    # Simulate a regenerated variant: change the last ai text, persist, add a swipe.
    session.state["chat_messages"][-1] = {"role": "ai", "content": "R1-v2"}
    session.state["history"][-1] = "R1-v2"
    sm.save_turn(sid, session.state, 1)
    m2 = session.add_regenerated_swipe()
    assert m2["count"] == 2 and m2["active"] == 1

    # Swiping restores each variant's full state (turn stays 1).
    s0 = session.select_swipe(0)
    assert s0["turn"] == 1
    assert s0["chat_messages"][-1]["content"] == "R1"
    s1 = session.select_swipe(1)
    assert s1["chat_messages"][-1]["content"] == "R1-v2"


def test_prepare_regenerate_rolls_back_to_previous_turn(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    _seed_two_turns(session)
    regen_turn = session.prepare_regenerate()
    assert regen_turn == 1
    # Workspace is rolled back to the end of turn 0 (intro only), with the user
    # input re-seated so the pipeline can produce a fresh generation.
    assert session.state["turn"] == 0
    assert session.state["chat_messages"] == [{"role": "ai", "content": "Intro."}]
    assert session.state["input_text"] == "A1"


def test_edit_message_updates_content_and_history(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    _seed_two_turns(session)
    state = session.edit_message(2, "Edited narration.")
    assert state["chat_messages"][2]["content"] == "Edited narration."
    assert state["history"][-1] == "Edited narration."


def test_delete_last_turn_rolls_back(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    _seed_two_turns(session)
    # chat = [intro(0), user(1), ai(2)], turn 1 → deleting the last ai rolls back.
    state = session.delete_message(2)
    assert state["turn"] == 0
    assert state["chat_messages"] == [{"role": "ai", "content": "Intro."}]


def test_delete_last_turn_reseats_swipes_for_new_last_turn(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    _seed_two_turns(session)
    sm, sid = session.save_manager, session.active_save_id
    # Add a second player turn (turn 2) with its own swipe set.
    session.state["chat_messages"] += [
        {"role": "user", "content": "A2"},
        {"role": "ai", "content": "R2"},
    ]
    session.state["history"] += ["R2"]
    session.state["turn"] = 2
    sm.save_turn(sid, session.state, 2)
    session.begin_turn_swipes()

    # Delete the last ai (turn 2) → rolls back to turn 1, which must remain
    # regeneratable: swipe meta is present and points at turn 1.
    state = session.delete_message(len(session.state["chat_messages"]) - 1)
    assert state["turn"] == 1
    assert state["swipes"] == {"turn": 1, "active": 0, "count": 1}


def test_edit_last_turn_keeps_swipes(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    _seed_two_turns(session)
    state = session.edit_message(2, "Edited narration.")
    # The edited generation becomes v0 and stays regeneratable.
    assert state["swipes"] == {"turn": 1, "active": 0, "count": 1}


def _compile_last_user(state):
    compiler = PromptCompiler()
    messages = compiler.compile(state)["messages"]
    users = [m for m in messages if m["role"] == "user"]
    return users[-1]["content"] if users else None


def test_nonempty_input_is_the_final_user_message(tmp_path):
    state = {"chat_messages": [{"role": "ai", "content": "Intro."}],
             "input_text": "I open the door.", "continue_prompt": "Keep going."}
    assert _compile_last_user(state) == "I open the door."


def test_empty_input_uses_continue_prompt_as_user_turn(tmp_path):
    state = {"chat_messages": [{"role": "ai", "content": "Intro."}],
             "input_text": "", "continue_prompt": "Advance the scene now."}
    # No player message, the continue prompt takes the final user slot.
    assert _compile_last_user(state) == "Advance the scene now."


def test_empty_input_without_configured_prompt_falls_back_to_default(tmp_path):
    state = {"chat_messages": [{"role": "ai", "content": "Intro."}], "input_text": ""}
    assert _compile_last_user(state) == DEFAULT_CONTINUE_PROMPT


def test_continue_prompt_store_roundtrip_and_state(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    # Default when nothing is saved.
    assert session.save_manager.load_continue_prompt() == DEFAULT_CONTINUE_PROMPT
    session.update_continue_prompt("Time skips forward.")
    assert session.save_manager.load_continue_prompt() == "Time skips forward."
    # The live session picks it up so an empty send continues with the new text.
    assert session.state["continue_prompt"] == "Time skips forward."


def test_continue_turn_regenerates_as_continue(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    _seed_two_turns(session)
    # A continue turn appends only an ai message (no user message).
    session.state["chat_messages"].append({"role": "ai", "content": "R-cont"})
    session.state["history"].append("R-cont")
    session.state["turn"] = 2
    session.save_manager.save_turn(session.active_save_id, session.state, 2)
    session.begin_turn_swipes()
    # The swipe manifest must record an empty input so regenerate stays a continue.
    manifest = session.save_manager.load_swipe_manifest(session.active_save_id)
    assert manifest["turn"] == 2 and manifest["user_input"] == ""


def test_delete_opening_scene_is_rejected(tmp_path):
    session = GameSessionManager(str(tmp_path / "data"))
    session.state["chat_messages"] = [{"role": "ai", "content": "Intro."}]
    session.state["history"] = ["Intro."]
    session.state["turn"] = 0
    session.save_manager.save_turn(session.active_save_id, session.state, 0)
    try:
        session.delete_message(0)
        assert False, "expected deletion of the opening scene to be rejected"
    except ValueError:
        pass
