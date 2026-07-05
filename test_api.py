from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.llm import LLMProviderError
from backend.engine.session import GameSessionManager


def make_client(tmp_path, monkeypatch):
    session_manager = GameSessionManager(str(tmp_path / "data"))
    session_manager.create_save("autosave")
    monkeypatch.setattr(server, "session_manager", session_manager)
    server.engine.set_memory_path(session_manager.get_memory_path())
    server.engine.llm.mode = "mock"
    return TestClient(server.app), session_manager


def test_health_and_session_endpoints(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    health = client.get("/api/health")
    assert health.status_code == 200
    health_data = health.json()
    assert health_data["status"] == "ok"
    assert health_data["llm"]["LLM_MODE"] == "mock"
    assert health_data["session"]["active_save_id"] == session_manager.active_save_id

    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.json()["active_save_id"] == "autosave"

    modules = client.get("/api/modules")
    assert modules.status_code == 200
    assert any(module["id"] == "wb_core_rpg" for module in modules.json()["modules"])


def test_save_and_prompt_pipeline_endpoints(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    create_response = client.post("/api/saves", json={"save_id": "pytest_save"})
    assert create_response.status_code == 200
    assert create_response.json()["session"]["active_save_id"] == "pytest_save"

    saves = client.get("/api/saves")
    assert saves.status_code == 200
    assert any(save["id"] == "pytest_save" and save["active"] for save in saves.json()["saves"])

    pipeline_response = client.get("/api/session/prompt-pipeline")
    assert pipeline_response.status_code == 200
    pipeline = pipeline_response.json()["prompt_pipeline"]
    assert pipeline[0]["id"] == "core_narrator_rules"

    pipeline[0]["config"]["text"] = "Pytest narrator rules."
    update_response = client.put("/api/session/prompt-pipeline", json={"prompt_pipeline": pipeline})
    assert update_response.status_code == 200
    assert update_response.json()["prompt_pipeline"][0]["config"]["text"] == "Pytest narrator rules."

    preview_response = client.post("/api/session/prompt-pipeline/preview", json={"prompt_pipeline": pipeline})
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["messages"][0]["content"] == "Pytest narrator rules."
    assert any(entry["id"] == "wb_core_rpg:character_sheet" for entry in preview["trace"])

    load_response = client.post("/api/saves/autosave/load")
    assert load_response.status_code == 200
    assert load_response.json()["session"]["active_save_id"] == "autosave"


def test_create_save_with_world_and_scenario(tmp_path, monkeypatch):
    # World + scenario together: world data is used as the setting, the
    # scenario is persisted alongside it so its starting_prompt overrides
    # the opening message.
    client, session_manager = make_client(tmp_path, monkeypatch)

    from backend.engine.scenario import ScenarioStore
    store = ScenarioStore(str(tmp_path / "data"))
    record = store.save_scenario({
        "name": "Ambush",
        "scenario_description": "Bandits stalk the mountain road.",
        "starting_prompt": "The wagon wheel snaps at dusk.",
    })
    monkeypatch.setattr(server, "scenario_store", store)

    async def fake_world_provider(*, save_id, source_id, start_preference,
                                  session_manager, engine,
                                  start_location_node_id=None,
                                  character_module_data=None, character_data=None):
        state = session_manager.create_save(save_id)
        session_manager.state["world_data"] = {"id": source_id}
        return {"state": state, "start_location": None}

    monkeypatch.setitem(server.engine.story_sources, "world", fake_world_provider)

    resp = client.post("/api/saves", json={
        "save_id": "combo_save",
        "world_id": "test_world",
        "scenario_id": record["id"],
    })
    assert resp.status_code == 200

    scenario_file = tmp_path / "data" / "saves" / "combo_save" / "Scenario" / "scenario.json"
    assert scenario_file.exists()
    assert session_manager.state["world_data"]["id"] == "test_world"
    assert session_manager.state["scenario_data"]["starting_prompt"] == "The wagon wheel snaps at dusk."


def test_create_save_passes_picked_start_location(tmp_path, monkeypatch):
    # The start screen's "Pick for me" result is sent as a node id and must
    # reach the world story-source provider (it used to be dropped, giving
    # the player a random start instead of the previewed one).
    client, session_manager = make_client(tmp_path, monkeypatch)

    seen = {}

    async def fake_world_provider(*, save_id, source_id, start_preference,
                                  session_manager, engine,
                                  start_location_node_id=None,
                                  character_module_data=None, character_data=None):
        seen["start_location_node_id"] = start_location_node_id
        seen["start_preference"] = start_preference
        state = session_manager.create_save(save_id)
        return {"state": state, "start_location": {"node_id": start_location_node_id}}

    monkeypatch.setitem(server.engine.story_sources, "world", fake_world_provider)

    resp = client.post("/api/saves", json={
        "save_id": "picked_start_save",
        "world_id": "test_world",
        "start_location_node_id": "node_42",
    })
    assert resp.status_code == 200
    assert seen["start_location_node_id"] == "node_42"
    assert resp.json()["start_location"]["node_id"] == "node_42"


def test_create_save_with_missing_scenario_returns_404(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    from backend.engine.scenario import ScenarioStore
    monkeypatch.setattr(server, "scenario_store", ScenarioStore(str(tmp_path / "data")))

    resp = client.post("/api/saves", json={"save_id": "orphan", "scenario_id": "does_not_exist"})
    assert resp.status_code == 404
    # The scenario is loaded before any save is created, so nothing is left behind.
    assert not (tmp_path / "data" / "saves" / "orphan").exists()


def test_websocket_mock_turn_returns_done_state(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/chat") as websocket:
        websocket.send_json({"text": "I test the websocket turn."})

        received_done = None
        status_stages = []
        for _ in range(64):
            message = websocket.receive_json()
            if message["type"] == "done":
                received_done = message
                break
            assert message["type"] != "error", f"turn failed: {message.get('detail')}"
            if message["type"] == "llm_call":
                continue
            if message["type"] == "status":
                assert message["label"]
                status_stages.append(message["stage"])
                continue
            assert message["type"] in ("token", "reasoning_token", "message_complete")

    assert received_done is not None
    # Each pipeline node reports itself so the client can show progress.
    assert status_stages == ["gather_context", "storyteller", "reader", "librarian"]
    state = received_done["state"]
    assert state["turn"] == 1
    assert state["chat_messages"][-2]["role"] == "user"
    assert state["chat_messages"][-2]["content"] == "I test the websocket turn."
    assert state["chat_messages"][-1]["role"] == "ai"
    assert state["history"][-1].startswith("Mock outcome: I test the websocket turn.")


def test_websocket_slash_command_bypasses_pipeline(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/chat") as websocket:
        websocket.send_json({"text": "/plot"})
        state_load = websocket.receive_json()
        done = websocket.receive_json()

    assert state_load["type"] == "state_load"
    assert done["type"] == "done"

    messages = state_load["chat_messages"]
    assert messages[-2]["role"] == "user"
    assert messages[-2]["content"] == "/plot"
    assert messages[-2]["meta"]["command"] is True
    assert messages[-1]["role"] == "system"
    assert messages[-1]["content"].startswith("[Plot]")
    assert messages[-1]["meta"]["command"] is True

    # The story pipeline never ran: no turn consumed, no narration produced.
    assert done["state"]["turn"] == 0
    assert session_manager.state["history"] == []


def test_websocket_unknown_or_inactive_command_falls_through_to_turn(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)
    # No modules active for this save: /plot must be treated as normal input.
    session_manager.state.setdefault("module_configs", {})["__active_modules__"] = []

    with client.websocket_connect("/ws/chat") as websocket:
        websocket.send_json({"text": "/plot"})
        received_done = None
        for _ in range(64):
            message = websocket.receive_json()
            assert message["type"] != "error", f"turn failed: {message.get('detail')}"
            if message["type"] == "done":
                received_done = message
                break

    assert received_done is not None
    assert received_done["state"]["turn"] == 1
    assert received_done["state"]["history"][-1].startswith("Mock outcome: /plot")


def test_save_undo_endpoint_restores_prior_turn(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    for text in ["I take the first action.", "I take the second action."]:
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"text": text})
            for _ in range(64):
                message = websocket.receive_json()
                assert message["type"] != "error", f"turn failed: {message.get('detail')}"
                if message["type"] == "done":
                    break

    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.json()["turn"] == 2

    undo_response = client.post("/api/saves/autosave/undo", json={"target_turn": 1})
    assert undo_response.status_code == 200
    undo_data = undo_response.json()
    assert undo_data["session"]["turn"] == 1
    assert undo_data["state"]["turn"] == 1
    assert undo_data["state"]["chat_messages"][-2]["role"] == "user"
    assert undo_data["state"]["chat_messages"][-2]["content"] == "I take the first action."
    assert undo_data["state"]["chat_messages"][-1]["role"] == "ai"
    assert "second action" not in "\n".join(message["content"] for message in undo_data["state"]["chat_messages"])


def test_world_entries_endpoint_and_edits(tmp_path, monkeypatch):
    import asyncio

    client, session_manager = make_client(tmp_path, monkeypatch)

    # No world index yet: graceful empty shape.
    empty = client.get("/api/session/world-entries").json()
    assert empty == {"entries": [], "count": 0, "active_ids": [], "context_query": ""}

    asyncio.run(server.engine.ensure_memory())
    server.engine.memory.init_world_index(
        str(tmp_path / "data" / "saves" / "autosave" / "world_index"))
    asyncio.run(server.engine.memory.embed_world(
        {"lore": {"premise": "A quiet harbor town."}}, server.engine.llm))
    asyncio.run(server.engine.memory.embed_lorebooks([{
        "id": "realm_lore",
        "entries": [{"uid": "0", "title": "Dragon Peak", "keys": ["dragon"],
                     "secondary_keys": [], "content": "A dragon sleeps beneath the peak.",
                     "constant": False, "enabled": True}],
    }], server.engine.llm))

    listed = client.get("/api/session/world-entries").json()
    assert listed["count"] == 2
    by_type = {e["source_type"]: e for e in listed["entries"]}
    assert set(by_type) == {"lore", "lorebook"}

    # Retrieval tracking surfaces as active_ids.
    lore_id = by_type["lore"]["id"]
    session_manager.state["last_retrieved_world_ids"] = [lore_id]
    session_manager.state["last_context_query"] = "I sail into the harbor"
    tracked = client.get("/api/session/world-entries").json()
    assert tracked["active_ids"] == [lore_id]
    assert tracked["context_query"] == "I sail into the harbor"

    # World-derived rows are editable in place (re-embedded via mock provider).
    updated = client.put(f"/api/session/world-entries/{lore_id}",
                         json={"text": "A bustling harbor town at war."})
    assert updated.status_code == 200
    assert updated.json()["entry"]["text"] == "A bustling harbor town at war."
    refetched = client.get("/api/session/world-entries").json()
    assert any(e["text"] == "A bustling harbor town at war." for e in refetched["entries"])

    # Lorebook rows are rejected (edited through the lorebook instead).
    lorebook_id = by_type["lorebook"]["id"]
    resp = client.put(f"/api/session/world-entries/{lorebook_id}", json={"text": "hijack"})
    assert resp.status_code == 400
    assert client.put("/api/session/world-entries/no-such-id",
                      json={"text": "x"}).status_code == 404
    assert client.put(f"/api/session/world-entries/{lore_id}",
                      json={"text": "  "}).status_code == 400


def test_memory_update_endpoint(tmp_path, monkeypatch):
    import asyncio

    client, _ = make_client(tmp_path, monkeypatch)

    asyncio.run(server.engine.ensure_memory())
    vector = asyncio.run(server.engine.llm.get_embedding("Original memory"))
    memory_id = server.engine.memory.add_memory(vector, "Original memory", turn=1, importance=5)

    resp = client.put(f"/api/session/memories/{memory_id}", json={
        "summary": "Concise version", "importance": 12, "permanent": True,
    })
    assert resp.status_code == 200
    memory = resp.json()["memory"]
    assert memory["summary"] == "Concise version"
    assert memory["importance"] == 10  # clamped
    assert memory["permanent"] is True
    assert memory["text"] == "Original memory"

    # Text edit re-embeds with the mock provider.
    resp = client.put(f"/api/session/memories/{memory_id}", json={"text": "Rewritten memory"})
    assert resp.status_code == 200
    assert resp.json()["memory"]["text"] == "Rewritten memory"

    assert client.put("/api/session/memories/no-such-id",
                      json={"importance": 3}).status_code == 404
    assert client.put(f"/api/session/memories/{memory_id}",
                      json={"text": "   "}).status_code == 400


def test_memory_browser_works_before_first_turn(tmp_path, monkeypatch):
    # Loading a save resets engine.memory to None; the browser endpoints must
    # re-bind the store themselves instead of showing an empty library until
    # the first generation initializes it.
    import asyncio

    client, _ = make_client(tmp_path, monkeypatch)

    asyncio.run(server.engine.ensure_memory())
    vector = asyncio.run(server.engine.llm.get_embedding("A stored event"))
    server.engine.memory.add_memory(vector, "A stored event", turn=1, importance=5)

    server.engine.close_memory()  # simulate save load / server restart
    resp = client.get("/api/session/memories").json()
    assert resp["count"] == 1
    assert resp["memories"][0]["text"] == "A stored event"


def test_websocket_llm_provider_error_returns_structured_error(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    async def fail_turn(state):
        raise LLMProviderError("simulated provider outage")

    monkeypatch.setattr(server.engine.app, "ainvoke", fail_turn)

    with client.websocket_connect("/ws/chat") as websocket:
        websocket.send_json({"text": "This should fail before save."})
        # The turn initializes the memory store first, which can emit inspector
        # (llm_call) events before the failure surfaces; skip past those.
        message = websocket.receive_json()
        while message["type"] == "llm_call":
            message = websocket.receive_json()

    assert message["type"] == "error"
    assert message["code"] == "llm_provider_unavailable"
    assert message["message"] == "The AI provider is temporarily unavailable. Please try again in a moment."
    assert "simulated provider outage" in message["detail"]
    assert message["state"]["turn"] == 0
    assert message["state"]["input_text"] == ""
    assert session_manager.state["history"] == []
    assert session_manager.state["chat_messages"] == []
