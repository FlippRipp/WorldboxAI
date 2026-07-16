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


def test_story_style_seeded_from_scenario_and_editable(tmp_path, monkeypatch):
    # Scenario themes/tags/pacing round-trip through the store, seed the
    # created save's story_style, and stay editable per save afterwards.
    client, session_manager = make_client(tmp_path, monkeypatch)

    from backend.engine.scenario import ScenarioStore
    store = ScenarioStore(str(tmp_path / "data"))
    monkeypatch.setattr(server, "scenario_store", store)

    create = client.post("/api/scenarios", json={
        "name": "Styled",
        "scenario_description": "A city of thieves.",
        "themes": "betrayal",
        "tags": "noir, heist",
        "pacing": "breakneck",
    })
    assert create.status_code == 200
    scenario_id = create.json()["scenario"]["id"]
    loaded = store.load_scenario(scenario_id)
    assert (loaded["themes"], loaded["tags"], loaded["pacing"]) == ("betrayal", "noir, heist", "breakneck")

    resp = client.post("/api/saves", json={"save_id": "styled_save", "scenario_id": scenario_id})
    assert resp.status_code == 200
    expected = {"themes": "betrayal", "tags": "noir, heist", "pacing": "breakneck"}
    assert session_manager.state["story_style"] == expected

    get_resp = client.get("/api/saves/styled_save/story-style")
    assert get_resp.status_code == 200
    assert get_resp.json()["story_style"] == expected

    put_resp = client.put("/api/saves/styled_save/story-style", json={
        "themes": "hope", "tags": "", "pacing": "slow and atmospheric",
    })
    assert put_resp.status_code == 200
    assert session_manager.state["story_style"]["themes"] == "hope"
    metadata = session_manager.save_manager.read_core_json("styled_save", "metadata.json", {})
    assert metadata["story_style"] == {"themes": "hope", "tags": "", "pacing": "slow and atmospheric"}


def test_story_style_defaults_empty_and_missing_save_404(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    # A save that never set a style (or predates the feature) reads as empty.
    resp = client.get("/api/saves/autosave/story-style")
    assert resp.status_code == 200
    assert resp.json()["story_style"] == {"themes": "", "tags": "", "pacing": ""}

    assert client.get("/api/saves/no_such_save/story-style").status_code == 404
    assert client.put("/api/saves/no_such_save/story-style", json={"themes": "x"}).status_code == 404


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
        command_result = websocket.receive_json()
        state_update = websocket.receive_json()

    # Command output is ephemeral: a popup plus a state refresh for widgets,
    # nothing written into the transcript.
    assert command_result["type"] == "command_result"
    assert command_result["command"] == "/plot"
    assert command_result["message"].startswith("[Plot]")
    assert command_result["error"] is False

    assert state_update["type"] == "state_update"
    state = state_update["state"]
    assert all(
        message.get("content") != "/plot" for message in state.get("chat_messages", [])
    )

    # The story pipeline never ran: no turn consumed, no narration produced.
    assert state["turn"] == 0
    assert session_manager.state["history"] == []


def test_websocket_button_command_suppresses_popup_on_success(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/chat") as websocket:
        # A module UI button tags its command with source=button: the widget
        # shows the outcome via state_update, so no popup on success.
        websocket.send_json({"text": "/plot", "source": "button"})
        message = websocket.receive_json()

    assert message["type"] == "state_update"


def test_websocket_button_command_still_pops_up_on_error(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/chat") as websocket:
        # `/plot profile` without arguments fails (usage / not-ready), and a
        # failed button command must still surface its popup.
        websocket.send_json({"text": "/plot profile", "source": "button"})
        command_result = websocket.receive_json()
        state_update = websocket.receive_json()

    assert command_result["type"] == "command_result"
    assert command_result["error"] is True
    assert command_result["message"].startswith("[Plot]")
    assert state_update["type"] == "state_update"


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

    def stored_embedding():
        return server.engine.memory.conn.execute(
            "SELECT embedding FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()["embedding"]

    # A summary edit changes what retrieval matches on, so it must re-embed.
    before = stored_embedding()
    resp = client.put(f"/api/session/memories/{memory_id}", json={
        "summary": "Concise version", "importance": 12, "permanent": True,
    })
    assert resp.status_code == 200
    memory = resp.json()["memory"]
    assert memory["summary"] == "Concise version"
    assert memory["importance"] == 10  # clamped
    assert memory["permanent"] is True
    assert memory["text"] == "Original memory"
    assert stored_embedding() != before
    # The new vector is the embedding of the edited summary, not the text.
    from backend.engine.memory import _serialize
    expected = asyncio.run(server.engine.llm.get_embedding("Concise version"))
    assert stored_embedding() == _serialize(expected)

    # A text-only edit on a row with a distinct summary changes what gets
    # injected but not the retrieval key, so the embedding stays put.
    before = stored_embedding()
    resp = client.put(f"/api/session/memories/{memory_id}", json={"text": "Rewritten memory"})
    assert resp.status_code == 200
    updated = resp.json()["memory"]
    assert updated["text"] == "Rewritten memory"
    assert updated["summary"] == "Concise version"
    assert stored_embedding() == before

    assert client.put("/api/session/memories/no-such-id",
                      json={"importance": 3}).status_code == 404
    assert client.put(f"/api/session/memories/{memory_id}",
                      json={"text": "   "}).status_code == 400


def test_memory_update_syncs_summary_for_bridge_rows(tmp_path, monkeypatch):
    # Module/bridge memories are stored with summary == text; editing the text
    # must carry the summary along and re-embed, or the browser keeps showing
    # (and RAG keeps matching) the old content.
    import asyncio

    client, _ = make_client(tmp_path, monkeypatch)

    asyncio.run(server.engine.ensure_memory())
    vector = asyncio.run(server.engine.llm.get_embedding("Borin owes the player a debt."))
    memory_id = server.engine.memory.add_memory(
        vector, "Borin owes the player a debt.", turn=1, importance=8,
        entities=["npc:npc_1", "profile"], permanent=True)

    def stored_embedding():
        return server.engine.memory.conn.execute(
            "SELECT embedding FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()["embedding"]

    before = stored_embedding()
    resp = client.put(f"/api/session/memories/{memory_id}",
                      json={"text": "Borin has repaid his debt to the player."})
    assert resp.status_code == 200
    memory = resp.json()["memory"]
    assert memory["text"] == "Borin has repaid his debt to the player."
    assert memory["summary"] == "Borin has repaid his debt to the player."
    assert stored_embedding() != before


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


def test_rag_debug_endpoint(tmp_path, monkeypatch):
    import asyncio

    client, _ = make_client(tmp_path, monkeypatch)

    asyncio.run(server.engine.ensure_memory())
    vector = asyncio.run(server.engine.llm.get_embedding("The dragon attacked the village"))
    server.engine.memory.add_memory(
        vector, "The dragon attacked the village", turn=0, importance=7,
        entities=["Dragon", "Village"], topics=["combat"],
    )

    resp = client.post("/api/session/memories/rag-debug", json={"query": "dragon attack"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "dragon attack"
    assert data["world_query"] == "dragon attack"  # no location hints in a fresh save
    assert len(data["memories"]) == 1
    memory = data["memories"][0]
    assert memory["text"] == "The dragon attacked the village"
    assert isinstance(memory["dist"], float)
    assert memory["entities"] == ["Dragon", "Village"]  # parsed, not JSON strings
    assert memory["topics"] == ["combat"]
    assert data["world_entries"] == []  # no world index for this save

    assert client.post("/api/session/memories/rag-debug",
                       json={"query": "   "}).status_code == 400


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


def test_hardcore_mode_locks_rpg_module_configs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    client, _ = make_client(tmp_path, monkeypatch)
    cheats = {"on": False}
    monkeypatch.setattr(
        server, "backend_settings",
        SimpleNamespace(get=lambda key: cheats["on"] if key == "cheats.enabled" else None),
    )

    def put(configs):
        return client.put("/api/session/module-configs", json={"module_configs": configs})

    # Unlocked: normal edits go through, including the flip that enables the
    # lock (current values are saved along with it).
    assert put({"wb_core_rpg": {"xp_per_action": 15}}).status_code == 200
    assert put({"wb_core_rpg": {"xp_per_action": 15, "hardcore_mode": True}}).status_code == 200

    # Locked: any change to the section is rejected — other values and the
    # lock itself.
    assert put({"wb_core_rpg": {"xp_per_action": 20, "hardcore_mode": True}}).status_code == 403
    assert put({"wb_core_rpg": {"xp_per_action": 15, "hardcore_mode": False}}).status_code == 403

    # An unchanged section still passes (defaults applied on both sides), so
    # saving other modules' settings keeps working.
    res = put({
        "wb_core_rpg": {"xp_per_action": 15, "hardcore_mode": True, "xp_curve_steepness": 2},
        "wb_time_tracker": {},
    })
    assert res.status_code == 200

    # Cheats bypass the lock: values can change and the lock can come off.
    cheats["on"] = True
    res = put({"wb_core_rpg": {"xp_per_action": 25, "hardcore_mode": False}})
    assert res.status_code == 200

    # And once unlocked, editing works again without cheats.
    cheats["on"] = False
    assert put({"wb_core_rpg": {"xp_per_action": 30}}).status_code == 200


# ---------------------------------------------------------------------------
# module instruction slots + per-scenario/per-save custom instructions
# ---------------------------------------------------------------------------

def test_instruction_slots_endpoint_and_modules_flag(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    modules = {m["id"]: m for m in client.get("/api/modules").json()["modules"]}
    assert modules["wb_core_rpg"]["has_instruction_slots"] is True

    res = client.get("/api/modules/wb_core_rpg/instruction-slots")
    assert res.status_code == 200
    slots = res.json()["slots"]
    assert [s["id"] for s in slots] == [
        "action_assessment", "skill_categories", "skill_options",
        "skill_refine", "evolution_options", "evolve",
    ]
    assert all(s["label"] and s["description"] and s["default"] for s in slots)

    assert client.get("/api/modules/no_such_module/instruction-slots").status_code == 404
    # Any module without the contract 404s (and is flagged accordingly).
    slotless = next((mid for mid, m in modules.items() if not m["has_instruction_slots"]), None)
    if slotless:
        assert client.get(f"/api/modules/{slotless}/instruction-slots").status_code == 404


def test_scenario_module_fields_roundtrip_and_legacy_load(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    from backend.engine.scenario import ScenarioStore
    store = ScenarioStore(str(tmp_path / "data"))
    monkeypatch.setattr(server, "scenario_store", store)

    create = client.post("/api/scenarios", json={
        "name": "Culinary Court",
        "scenario_description": "A palace kitchen at war.",
        "skip_skill_categories": True,
        "disable_skill_progression": True,
        "skill_points_per_level": 2,
        "active_modules": ["wb_core_rpg"],
        "module_instructions": {
            "wb_core_rpg": {"skill_categories": "Culinary disciplines only.", "skill_options": "  "},
            "junk": "not a dict",
        },
    })
    assert create.status_code == 200
    scenario_id = create.json()["scenario"]["id"]
    loaded = client.get(f"/api/scenarios/{scenario_id}").json()["scenario"]
    assert loaded["active_modules"] == ["wb_core_rpg"]
    assert loaded["skip_skill_categories"] is True
    assert loaded["disable_skill_progression"] is True
    assert loaded["skill_points_per_level"] == 2
    # Blank slots and malformed modules are sanitized out at save time.
    assert loaded["module_instructions"] == {"wb_core_rpg": {"skill_categories": "Culinary disciplines only."}}

    # A scenario saved without module fields (legacy shape) loads with defaults.
    legacy = store.save_scenario({"name": "Old One", "scenario_description": "Plain."})
    loaded_legacy = client.get(f"/api/scenarios/{legacy['id']}").json()["scenario"]
    assert loaded_legacy["active_modules"] is None
    assert loaded_legacy["module_instructions"] == {}
    assert loaded_legacy["skip_skill_categories"] is False
    assert loaded_legacy["disable_skill_progression"] is False
    assert loaded_legacy["skill_points_per_level"] is None


def test_create_save_seeds_module_instructions_from_scenario(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    from backend.engine.scenario import ScenarioStore
    store = ScenarioStore(str(tmp_path / "data"))
    monkeypatch.setattr(server, "scenario_store", store)

    record = store.save_scenario({
        "name": "Seeded",
        "scenario_description": "A city of chefs.",
        "active_modules": ["wb_core_rpg", "wb_time_tracker"],
        "module_instructions": {"wb_core_rpg": {"skill_categories": "Culinary only."}},
    })

    # Request omits both: the scenario's module defaults seed the save.
    resp = client.post("/api/saves", json={"save_id": "seeded_save", "scenario_id": record["id"]})
    assert resp.status_code == 200
    cfgs = session_manager.state["module_configs"]
    assert cfgs["__active_modules__"] == ["wb_core_rpg", "wb_time_tracker"]
    assert cfgs["__module_instructions__"] == {"wb_core_rpg": {"skill_categories": "Culinary only."}}

    # The GET endpoint reports both the live overrides and the frozen
    # scenario defaults (the reset baseline).
    res = client.get("/api/saves/seeded_save/module-instructions")
    assert res.status_code == 200
    body = res.json()
    assert body["module_instructions"] == {"wb_core_rpg": {"skill_categories": "Culinary only."}}
    assert body["scenario_module_instructions"] == {"wb_core_rpg": {"skill_categories": "Culinary only."}}

    # Request-supplied values (the user edited the pre-filled form) win.
    resp = client.post("/api/saves", json={
        "save_id": "edited_save",
        "scenario_id": record["id"],
        "active_modules": ["wb_core_rpg"],
        "module_instructions": {"wb_core_rpg": {"skill_categories": "Edited by user."}},
    })
    assert resp.status_code == 200
    cfgs = session_manager.state["module_configs"]
    assert cfgs["__active_modules__"] == ["wb_core_rpg"]
    assert cfgs["__module_instructions__"] == {"wb_core_rpg": {"skill_categories": "Edited by user."}}
    # The frozen scenario copy still holds the scenario's own values for reset.
    body = client.get("/api/saves/edited_save/module-instructions").json()
    assert body["scenario_module_instructions"] == {"wb_core_rpg": {"skill_categories": "Culinary only."}}


def test_create_save_seeds_rpg_config_from_scenario(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    from backend.engine.scenario import ScenarioStore
    store = ScenarioStore(str(tmp_path / "data"))
    monkeypatch.setattr(server, "scenario_store", store)

    record = store.save_scenario({
        "name": "Frozen Skills",
        "scenario_description": "No grinding here.",
        "disable_skill_progression": True,
        "skill_points_per_level": 3,
    })
    resp = client.post("/api/saves", json={"save_id": "frozen_save", "scenario_id": record["id"]})
    assert resp.status_code == 200
    rpg_cfg = session_manager.state["module_configs"]["wb_core_rpg"]
    assert rpg_cfg["skill_progression_enabled"] is False
    assert rpg_cfg["skill_points_per_level"] == 3

    # A scenario that leaves both unset seeds nothing (module defaults apply).
    plain = store.save_scenario({"name": "Plain", "scenario_description": "Defaults."})
    resp = client.post("/api/saves", json={"save_id": "plain_save", "scenario_id": plain["id"]})
    assert resp.status_code == 200
    assert "wb_core_rpg" not in session_manager.state["module_configs"]

    # Out-of-range points are clamped by the scenario store.
    clamped = store.save_scenario({"name": "Clamped", "skill_points_per_level": 99})
    assert clamped["skill_points_per_level"] == 3


def test_save_module_instructions_get_put(tmp_path, monkeypatch):
    client, session_manager = make_client(tmp_path, monkeypatch)

    # A save with no scenario and no overrides reads as empty on both sides.
    res = client.get("/api/saves/autosave/module-instructions")
    assert res.status_code == 200
    assert res.json() == {"module_instructions": {}, "scenario_module_instructions": {}}

    put = client.put("/api/saves/autosave/module-instructions", json={
        "module_instructions": {"wb_core_rpg": {"evolve": "Blood price.", "skill_refine": "   "}},
    })
    assert put.status_code == 200
    assert put.json()["module_instructions"] == {"wb_core_rpg": {"evolve": "Blood price."}}
    assert session_manager.state["module_configs"]["__module_instructions__"] == {
        "wb_core_rpg": {"evolve": "Blood price."}
    }

    # Clearing works: an empty payload resets everything to defaults.
    put = client.put("/api/saves/autosave/module-instructions", json={"module_instructions": {}})
    assert put.status_code == 200
    assert client.get("/api/saves/autosave/module-instructions").json()["module_instructions"] == {}

    assert client.get("/api/saves/no_such_save/module-instructions").status_code == 404
    assert client.put(
        "/api/saves/no_such_save/module-instructions", json={"module_instructions": {}}
    ).status_code == 404


def test_hardcore_mode_locks_module_instructions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    client, _ = make_client(tmp_path, monkeypatch)
    cheats = {"on": False}
    monkeypatch.setattr(
        server, "backend_settings",
        SimpleNamespace(get=lambda key: cheats["on"] if key == "cheats.enabled" else None),
    )

    def put(instructions):
        return client.put(
            "/api/saves/autosave/module-instructions", json={"module_instructions": instructions}
        )

    # Unlocked: overrides can be set freely.
    assert put({"wb_core_rpg": {"action_assessment": "Be kind."}}).status_code == 200

    # Turn on Hardcore Mode (the lock) via the settings endpoint.
    res = client.put("/api/session/module-configs", json={
        "module_configs": {"wb_core_rpg": {"hardcore_mode": True}},
    })
    assert res.status_code == 200

    # Locked: changing the RPG module's instructions is rejected...
    assert put({"wb_core_rpg": {"action_assessment": "Everything succeeds."}}).status_code == 403
    assert put({}).status_code == 403  # ...including clearing them.
    # ...but an unchanged section still passes (e.g. editing another module).
    assert put({"wb_core_rpg": {"action_assessment": "Be kind."}}).status_code == 200

    # Cheats bypass the lock.
    cheats["on"] = True
    assert put({"wb_core_rpg": {"action_assessment": "Everything succeeds."}}).status_code == 200


def test_rewrite_instruction_endpoint(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    seen = {}

    async def fake_completion(messages, model=None, response_format=None, inspector_ctx=None, **kwargs):
        seen["messages"] = messages
        seen["model"] = model
        return '{"instruction": "All categories must be culinary disciplines."}'

    monkeypatch.setattr(server.engine.llm, "simple_completion", fake_completion)

    res = client.post(
        "/api/modules/wb_core_rpg/instructions/skill_categories/rewrite",
        json={"request": "make everything about cooking"},
    )
    assert res.status_code == 200
    assert res.json()["instruction"] == "All categories must be culinary disciplines."
    # The default directive is always the rewrite base; the user request rides along.
    user_msg = seen["messages"][1]["content"]
    assert "BROAD domains of ability" in user_msg
    assert "make everything about cooking" in user_msg
    assert "<current_instruction>" not in user_msg
    assert seen["model"] == server.engine.llm.storyteller_model

    # Iterating: a current text different from the default is passed as context.
    res = client.post(
        "/api/modules/wb_core_rpg/instructions/skill_categories/rewrite",
        json={"request": "more baking", "current_text": "All categories must be culinary."},
    )
    assert res.status_code == 200
    assert "<current_instruction>" in seen["messages"][1]["content"]

    # A scenario context makes the rewrite aware of the story it belongs to;
    # empty context fields are dropped.
    res = client.post(
        "/api/modules/wb_core_rpg/instructions/skill_categories/rewrite",
        json={
            "request": "fit the setting",
            "scenario_context": {
                "name": "Frostspire Siege",
                "scenario_description": "A frozen citadel under endless winter war.",
                "themes": "survival, sacrifice",
                "tags": "",
            },
        },
    )
    assert res.status_code == 200
    ctx_msg = seen["messages"][1]["content"]
    assert "<scenario_context>" in ctx_msg
    assert "Frostspire Siege" in ctx_msg
    assert "A frozen citadel under endless winter war." in ctx_msg
    assert "survival, sacrifice" in ctx_msg
    assert "<tags>" not in ctx_msg  # empty fields omitted

    # No scenario context → no scenario_context block.
    res = client.post(
        "/api/modules/wb_core_rpg/instructions/skill_categories/rewrite",
        json={"request": "plain rewrite"},
    )
    assert res.status_code == 200
    assert "<scenario_context>" not in seen["messages"][1]["content"]

    assert client.post(
        "/api/modules/wb_core_rpg/instructions/no_such_slot/rewrite",
        json={"request": "x"},
    ).status_code == 404
    assert client.post(
        "/api/modules/wb_core_rpg/instructions/skill_categories/rewrite",
        json={"request": "   "},
    ).status_code == 400

    async def garbage_completion(messages, **kwargs):
        return "not json"

    monkeypatch.setattr(server.engine.llm, "simple_completion", garbage_completion)
    assert client.post(
        "/api/modules/wb_core_rpg/instructions/skill_categories/rewrite",
        json={"request": "x"},
    ).status_code == 502


def test_rewrite_scenario_prompt_endpoint(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    seen = {}

    async def fake_completion(messages, model=None, response_format=None, inspector_ctx=None, **kwargs):
        seen["messages"] = messages
        seen["model"] = model
        seen["inspector_ctx"] = inspector_ctx
        return '{"text": "The storm swallows the road behind you as the tavern door slams shut."}'

    monkeypatch.setattr(server.engine.llm, "simple_completion", fake_completion)

    # Editing the starting prompt sees every other scenario field, but not the
    # field being edited (that arrives as current_text, not context).
    res = client.post(
        "/api/scenarios/rewrite-prompt",
        json={
            "request": "make it darker and more tense",
            "current_text": "You walk into the tavern.",
            "field": "starting_prompt",
            "name": "The Lonely Tavern",
            "scenario_description": "A rain-soaked frontier town.",
            "starting_prompt": "You walk into the tavern.",
            "themes": "isolation, dread",
            "tags": "horror, slow burn",
            "pacing": "slow and atmospheric",
        },
    )
    assert res.status_code == 200
    assert res.json()["text"].startswith("The storm swallows")
    user_msg = seen["messages"][1]["content"]
    assert "make it darker and more tense" in user_msg
    assert "You walk into the tavern." in user_msg
    assert "A rain-soaked frontier town." in user_msg
    assert "isolation, dread" in user_msg
    assert "horror, slow burn" in user_msg
    assert "slow and atmospheric" in user_msg
    # The edited field is not echoed back as a <starting_prompt> context block.
    assert "<starting_prompt>" not in user_msg
    assert seen["model"] == server.engine.llm.storyteller_model
    assert seen["inspector_ctx"]["step"] == "scenario:starting_prompt"

    # Editing the description sees the starting prompt as context (and excludes
    # its own field); an empty current_text is allowed (draft from scratch).
    res = client.post(
        "/api/scenarios/rewrite-prompt",
        json={
            "request": "add a sense of dread",
            "field": "scenario_description",
            "scenario_description": "should be excluded",
            "starting_prompt": "The door groans shut.",
        },
    )
    assert res.status_code == 200
    desc_msg = seen["messages"][1]["content"]
    assert "draft the text from scratch" in desc_msg
    assert "The door groans shut." in desc_msg
    assert "<scenario_description>" not in desc_msg  # edited field excluded
    assert "should be excluded" not in desc_msg
    assert seen["inspector_ctx"]["step"] == "scenario:scenario_description"

    # An unknown field falls back to starting_prompt framing rather than erroring.
    res = client.post(
        "/api/scenarios/rewrite-prompt",
        json={"request": "x", "field": "bogus"},
    )
    assert res.status_code == 200
    assert seen["inspector_ctx"]["step"] == "scenario:starting_prompt"

    # A blank request is rejected.
    assert client.post(
        "/api/scenarios/rewrite-prompt",
        json={"request": "   "},
    ).status_code == 400

    async def garbage_completion(messages, **kwargs):
        return "not json"

    monkeypatch.setattr(server.engine.llm, "simple_completion", garbage_completion)
    assert client.post(
        "/api/scenarios/rewrite-prompt",
        json={"request": "x"},
    ).status_code == 502
