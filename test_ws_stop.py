import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.session import GameSessionManager


def make_client(tmp_path, monkeypatch):
    session_manager = GameSessionManager(str(tmp_path / "data"))
    session_manager.create_save("autosave")
    monkeypatch.setattr(server, "session_manager", session_manager)
    server.engine.set_memory_path(session_manager.get_memory_path())
    server.engine.llm.mode = "mock"
    return TestClient(server.app), session_manager


def receive_until(ws, types):
    """Skip stream/inspector traffic until a message of one of `types` arrives."""
    while True:
        data = ws.receive_json()
        if data["type"] in types:
            return data


def test_ws_turn_stamps_message_metadata(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "I search the room."})
        done = receive_until(ws, {"done", "error"})

    assert done["type"] == "done"
    msgs = done["state"]["chat_messages"]
    assert msgs[-2]["role"] == "user"
    assert msgs[-2]["meta"]["ts"]
    assert msgs[-1]["role"] == "ai"
    assert msgs[-1]["meta"]["ts"]
    assert msgs[-1]["meta"]["model"] == "mock"


def test_ws_sync_replays_authoritative_state(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    # Play one turn so there is a transcript, then reconnect and sync.
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "I light a torch."})
        receive_until(ws, {"done", "error"})

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "sync"})
        loaded = receive_until(ws, {"state_load"})
        done = receive_until(ws, {"done"})

    assert loaded["chat_messages"][-2]["content"] == "I light a torch."
    assert done["state"]["turn"] == 1
    assert done["state"]["swipes"] is not None


def test_ws_sync_after_restart_restores_active_save(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    # Create a story (making it active) and play a turn.
    assert client.post("/api/saves", json={"save_id": "restart_story"}).status_code == 200
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "I mark my path."})
        receive_until(ws, {"done", "error"})

    # Simulated server restart: a fresh session manager over the same data dir
    # must boot straight into the last active save, so a reconnecting client's
    # sync gets the right transcript.
    restarted = GameSessionManager(str(tmp_path / "data"))
    monkeypatch.setattr(server, "session_manager", restarted)
    server.engine.set_memory_path(restarted.get_memory_path())
    assert restarted.active_save_id == "restart_story"

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "sync"})
        loaded = receive_until(ws, {"state_load"})
        done = receive_until(ws, {"done"})

    assert done["state"]["active_save_id"] == "restart_story"
    assert done["state"]["turn"] == 1
    assert loaded["chat_messages"][-2]["content"] == "I mark my path."


def test_ws_stop_cancels_turn_and_rejects_concurrent_turns(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)
    turn_before = session_manager.state.get("turn", 0)
    history_before = list(session_manager.state.get("history", []))

    async def slow_ainvoke(state):
        await asyncio.sleep(30)
        raise AssertionError("turn should have been cancelled before finishing")

    monkeypatch.setattr(server.engine, "app", SimpleNamespace(ainvoke=slow_ainvoke))

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "I open the vault."})

        # A second action while the turn task runs is rejected, not queued.
        ws.send_json({"action": "turn", "text": "another action"})
        busy = receive_until(ws, {"error", "done", "turn_stopped"})
        assert busy["type"] == "error"
        assert busy["code"] == "busy"

        # Stop interrupts the running turn; the discarded input is echoed back.
        ws.send_json({"action": "stop"})
        stopped = receive_until(ws, {"turn_stopped", "done", "error"})
        assert stopped["type"] == "turn_stopped"
        assert stopped["input"] == "I open the vault."
        assert stopped["state"]["turn"] == turn_before

    # Nothing was saved: state is exactly as before the aborted turn.
    assert session_manager.state.get("history", []) == history_before
    assert session_manager.state.get("input_text", "") == ""


def test_ws_stop_during_regenerate_restores_last_turn(tmp_path, monkeypatch):
    # Regenerate rolls the workspace back before generating; cancelling it must
    # restore the previously active variant, not drop the last user+ai pair.
    client, session_manager = make_client(tmp_path, monkeypatch)
    # Seed the turn-0 snapshot that the intro normally creates; regenerating
    # turn 1 rolls back to it.
    session_manager.save_manager.save_turn(
        session_manager.active_save_id, session_manager.state, 0
    )

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "I open the vault."})
        done = receive_until(ws, {"done", "error"})
        assert done["type"] == "done"
        msgs_before = done["state"]["chat_messages"]
        swipes_before = done["state"]["swipes"]

        async def slow_ainvoke(state):
            await asyncio.sleep(30)
            raise AssertionError("regenerate should have been cancelled before finishing")

        monkeypatch.setattr(server.engine, "app", SimpleNamespace(ainvoke=slow_ainvoke))

        ws.send_json({"action": "regenerate"})
        ws.send_json({"action": "stop"})
        stopped = receive_until(ws, {"turn_stopped", "done", "error"})
        assert stopped["type"] == "turn_stopped"
        # No composer text to restore: the input still lives in the transcript.
        assert stopped["input"] == ""
        # The transcript reverts to the message shown before the retry.
        assert stopped["state"]["chat_messages"] == msgs_before
        assert stopped["state"]["turn"] == done["state"]["turn"]
        assert stopped["state"]["swipes"] == swipes_before

    assert session_manager.state["chat_messages"] == msgs_before
