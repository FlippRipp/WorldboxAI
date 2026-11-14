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


def test_delete_turn_removes_summary_and_introduced_npc_memories(tmp_path, monkeypatch):
    """Regression: generating turn 3, deleting it, and rewriting the response
    previously kept BOTH:
    1) the character encounter + permanent profile memories from turn 3, because
       reader_node ran on_mutate_state before incrementing `turn` (stamping them
       with turn_generated=2 instead of 3), and
    2) the turns 1-3 summary whenever engine.memory was unopened at delete time
       (e.g. after reload/restart) or inside the repacked .wbx archive."""
    import json
    import zipfile
    from backend.engine.memory import MemoryManager

    client, session_manager = make_client(tmp_path, monkeypatch)

    try:
        # Opening scene (turn 0) + turns 1 and 2.
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"action": "intro"})
            assert receive_until(ws, {"done", "error"})["type"] == "done"
            ws.send_json({"action": "turn", "text": "I enter the market."})
            assert receive_until(ws, {"done", "error"})["type"] == "done"
            ws.send_json({"action": "turn", "text": "I inspect the stalls."})
            assert receive_until(ws, {"done", "error"})["type"] == "done"

        assert session_manager.state["turn"] == 2

        # On turn 3, the reader reports a newly introduced story character ("Seraphine")
        # and wb_npc_system's profiler fills out her record; librarian_node also fires
        # its turns 1-3 summary (default frequency=3).
        orig_extract = server.engine.llm.extract_mutations
        orig_sdk_generate = server.engine.sdk.llm.generate

        async def turn3_extract(story_text, schema, context="", inspector_ctx=None):
            res = await orig_extract(story_text, schema, context=context, inspector_ctx=inspector_ctx)
            if "wb_npc_system" in schema:
                res = dict(res)
                res["wb_npc_system"] = {
                    "story_characters": [{
                        "name": "Seraphine",
                        "descriptor": "Met Seraphine, a masked oracle at the fountain.",
                        "evidence": "A masked oracle named Seraphine stepped forward.",
                    }]
                }
            return res

        async def turn3_sdk_generate(prompt, model_preference="balanced", max_tokens=None):
            if "Seraphine" in prompt and "CHARACTERS TO PROFILE" in prompt:
                return json.dumps({"npcs": [{
                    "name": "Seraphine", "race": "human", "gender": "female",
                    "appearance": "Masked in silver silk, blue eyes, appears mid-twenties.",
                    "archetype": "oracle",
                    "pitch": "A cryptic fountain oracle.",
                    "personality": ["cryptic", "calm", "watchful"], "role": "informant",
                }]})
            return await orig_sdk_generate(prompt, model_preference=model_preference, max_tokens=max_tokens)

        monkeypatch.setattr(server.engine.llm, "extract_mutations", turn3_extract)
        monkeypatch.setattr(server.engine.sdk.llm, "generate", turn3_sdk_generate)

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"action": "turn", "text": "I approach the fountain."})
            done_t3 = receive_until(ws, {"done", "error"})
            assert done_t3["type"] == "done"

        assert session_manager.state["turn"] == 3
        before_delete = server.engine.memory.list_all_memories(limit=100)
        seraphine_mems = [m for m in before_delete if "Seraphine" in m["text"]]
        summaries_1_3 = [m for m in before_delete if m["turn_range"] == "turns 1-3"]
        assert len(seraphine_mems) >= 2  # encounter memory + permanent profile
        assert all(m["turn_generated"] == 3 for m in seraphine_mems)
        assert len(summaries_1_3) == 1
        first_summary_id = summaries_1_3[0]["id"]

        # Simulate lazy unopened memory store (e.g. after server restart/reload)
        # before deleting turn 3.
        server.engine.close_memory()
        assert server.engine.memory is None

        last_idx = len(session_manager.state["chat_messages"]) - 1
        del_resp = client.delete(f"/api/session/messages/{last_idx}")
        assert del_resp.status_code == 200
        assert del_resp.json()["state"]["turn"] == 2

        # Both the turns 1-3 summary AND Seraphine's memories must be gone on disk...
        check_mm = MemoryManager(session_manager.get_memory_path(), embedding_dim=3)
        try:
            after_delete = check_mm.list_all_memories(limit=100)
        finally:
            check_mm.close()
        assert not any("Seraphine" in m["text"] for m in after_delete)
        assert not any(m["turn_range"] == "turns 1-3" for m in after_delete)

        # ...and inside the repacked .wbx archive too.
        wbx_path = session_manager.data_dir / "saves" / "autosave.wbx"
        extracted_dir = tmp_path / "wbx_check"
        with zipfile.ZipFile(wbx_path, "r") as zf:
            zf.extractall(extracted_dir)
        wbx_mm = MemoryManager(str(extracted_dir / "vector_index"), embedding_dim=3)
        try:
            wbx_mems = wbx_mm.list_all_memories(limit=100)
        finally:
            wbx_mm.close()
        assert not any("Seraphine" in m["text"] for m in wbx_mems)
        assert not any(m["turn_range"] == "turns 1-3" for m in wbx_mems)

        # Rewrite turn 3 with different action (no Seraphine): old memories must
        # not leak into retrieval or coexist with the new turns 1-3 summary.
        monkeypatch.setattr(server.engine.llm, "extract_mutations", orig_extract)
        monkeypatch.setattr(server.engine.sdk.llm, "generate", orig_sdk_generate)

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"action": "turn", "text": "I leave the market for the docks."})
            done_rewrite = receive_until(ws, {"done", "error"})
            assert done_rewrite["type"] == "done"

        final_mems = server.engine.memory.list_all_memories(limit=100)
        assert not any("Seraphine" in m["text"] for m in final_mems)
        final_summaries = [m for m in final_mems if m["turn_range"] == "turns 1-3"]
        assert len(final_summaries) == 1
        assert final_summaries[0]["id"] != first_summary_id
    finally:
        server.engine.close_memory()


def test_ws_stop_rolls_back_memories_written_before_cancel(tmp_path, monkeypatch):
    """If a turn is stopped during post-processing after reader/librarian already
    inserted rows for the pending turn, cancelling must roll those rows back."""
    client, session_manager = make_client(tmp_path, monkeypatch)

    try:
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"action": "turn", "text": "Turn one."})
            assert receive_until(ws, {"done", "error"})["type"] == "done"

            assert session_manager.state["turn"] == 1
            wrote_event = False

            async def write_memory_then_hang(state):
                nonlocal wrote_event
                await server.engine.ensure_memory()
                vec = await server.engine.llm.get_embedding("leaked memory")
                server.engine.memory.add_memory(
                    vec, "Leaked turn-2 memory", turn=2, importance=8, permanent=True
                )
                wrote_event = True
                await chat_hub_marker()
                await asyncio.sleep(30)

            async def chat_hub_marker():
                await server.chat_hub.send({"type": "status", "stage": "librarian", "label": "Recording memories…"})

            monkeypatch.setattr(server.engine, "app", SimpleNamespace(ainvoke=write_memory_then_hang))

            ws.send_json({"action": "turn", "text": "Turn two."})
            receive_until(ws, {"status"})
            assert wrote_event is True
            ws.send_json({"action": "stop"})
            stopped = receive_until(ws, {"turn_stopped", "done", "error"})
            assert stopped["type"] == "turn_stopped"
            assert stopped["state"]["turn"] == 1

        remaining = server.engine.memory.list_all_memories(limit=50)
        assert not any(m["text"] == "Leaked turn-2 memory" for m in remaining)
    finally:
        server.engine.close_memory()

