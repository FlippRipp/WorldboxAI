from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.llm import LLMProviderError
from backend.engine.session import GameSessionManager


def make_client(tmp_path, monkeypatch):
    session_manager = GameSessionManager(str(tmp_path / "data"))
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


def test_websocket_llm_provider_error_returns_structured_error(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    async def fail_turn(state):
        raise LLMProviderError("simulated provider outage")

    monkeypatch.setattr(server.engine.app, "ainvoke", fail_turn)

    with client.websocket_connect("/ws/chat") as websocket:
        websocket.send_json({"text": "This should fail before save."})
        message = websocket.receive_json()

    assert message["type"] == "error"
    assert message["code"] == "llm_provider_unavailable"
    assert message["message"] == "The AI provider is temporarily unavailable. Please try again in a moment."
    assert "simulated provider outage" in message["detail"]
    assert message["state"]["turn"] == 0
    assert message["state"]["input_text"] == ""
    assert session_manager.state["history"] == []
    assert session_manager.state["chat_messages"] == []
