import asyncio
from types import SimpleNamespace

import pytest
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


def test_intro_skips_module_reinit_for_existing_story(tmp_path, monkeypatch):
    # Opening an already-started story must not re-run the module context pass
    # (initialize_module_data -> every module's on_gather_context). That pass
    # fires the NPC system's introduction + scene-presence LLM calls, and on an
    # existing story the result is thrown away (the transcript is just
    # replayed), so running it on every load/resume/branch burns tokens for
    # nothing.
    client, session_manager = make_client(tmp_path, monkeypatch)

    calls = {"n": 0}
    original = server.engine.initialize_module_data

    async def counting_init(state):
        calls["n"] += 1
        return await original(state)

    monkeypatch.setattr(server.engine, "initialize_module_data", counting_init)

    # New story (no transcript yet): intro seeds module_data before generating
    # the opening scene.
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "intro"})
        receive_until(ws, {"done", "error"})
    assert calls["n"] == 1

    # Ensure a transcript exists regardless of the mock intro's outcome.
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "I press on."})
        receive_until(ws, {"done", "error"})
    assert session_manager.state.get("history")

    # Re-opening the now-existing story replays the transcript without the
    # wasteful re-init.
    before = calls["n"]
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "intro"})
        loaded = receive_until(ws, {"state_load"})
        done = receive_until(ws, {"done"})
    assert calls["n"] == before
    assert loaded["chat_messages"]
    assert done["state"]["swipes"] is not None


def test_intro_runs_post_storyteller_pipeline(tmp_path, monkeypatch):
    # The opening message must feed the same post-storyteller phases as any
    # later turn (reader extraction + librarian): nothing used to run after
    # the intro, so state generated from the opening scene (NPCs, time,
    # memories) didn't exist until turn 1.
    client, session_manager = make_client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "intro"})
        # The opening finalizes on the client before the post pass runs,
        # exactly like a normal turn's narration.
        first = receive_until(ws, {"message_complete", "done", "error"})
        assert first["type"] == "message_complete"
        done = receive_until(ws, {"done", "error"})

    assert done["type"] == "done"
    # The opening keeps its turn-0 identity — swipes, regenerate and delete
    # all rely on it — even though the reader normally advances the counter.
    assert done["state"]["turn"] == 0
    # The librarian ran over the opening scene and memorized it.
    assert done["state"].get("last_stored_memory_id")
    memories = server.engine.memory.list_all_memories()
    assert any(m["turn_generated"] == 0 for m in memories)


def test_npc_delete_command_removes_character_end_to_end(tmp_path, monkeypatch):
    # Deleting a character must survive the command write-back. module_data is
    # deep-merged (additive), so without the module_data_replace opt-in the
    # removed key would linger; this exercises the full WS -> handler -> replace
    # path and asserts the bank actually shrinks.
    client, session_manager = make_client(tmp_path, monkeypatch)
    session_manager.state.setdefault("module_data", {})["wb_npc_system"] = {
        "characters": {
            "npc_keep0001": {"id": "npc_keep0001", "name": "Keeper", "role": "ally",
                             "introduced": True, "status": "active", "relationships": []},
            "npc_gone0002": {"id": "npc_gone0002", "name": "Doomed", "role": "neutral",
                             "introduced": True, "status": "active", "relationships": []},
        }
    }

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "/npc delete npc_gone0002"})
        upd = receive_until(ws, {"state_update", "error"})

    assert upd["type"] == "state_update"
    chars = upd["state"]["module_data"]["wb_npc_system"]["characters"]
    assert "npc_gone0002" not in chars
    assert "npc_keep0001" in chars
    # The removal is also persisted to the live session state.
    assert "npc_gone0002" not in session_manager.state["module_data"]["wb_npc_system"]["characters"]


def test_npc_add_command_creates_character_end_to_end(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    import urllib.parse
    import json as _json
    payload = urllib.parse.quote(_json.dumps({"name": "Fresh Face", "role": "wildcard"}))

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": f"/npc add {payload}"})
        upd = receive_until(ws, {"state_update", "error"})

    assert upd["type"] == "state_update"
    chars = upd["state"]["module_data"]["wb_npc_system"]["characters"]
    assert any(c["name"] == "Fresh Face" for c in chars.values())


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


def test_stop_marks_inflight_llm_call_cancelled(monkeypatch):
    # CancelledError is a BaseException, so the `except Exception` cleanup in
    # LLMService never sees it — a stopped turn must still close the inspector
    # record, otherwise it shows as "running" forever in the LLM inspector.
    import backend.engine.llm as llm_mod
    from backend.engine.llm import LLMService
    from backend.engine.llm_inspector import LLMInspector

    async def hang_forever(**kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(llm_mod, "acompletion", hang_forever)

    async def scenario():
        service = LLMService(mode="live")
        inspector = LLMInspector()
        service.set_inspector(inspector)

        call = asyncio.create_task(
            service.simple_completion(
                [{"role": "user", "content": "hi"}], model="test/model"
            )
        )
        # Let the call reach the (hung) provider await before cancelling.
        while not inspector.get_calls():
            await asyncio.sleep(0.01)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        records = inspector.get_calls()
        assert len(records) == 1
        assert records[0]["status"] == "cancelled"

    asyncio.run(scenario())


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
