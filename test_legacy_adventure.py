"""Former alternate-mode saves must behave like ordinary adventures."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

from backend.engine.llm import LLMService
from backend.engine.prompt_pipeline import PromptCompiler
from backend.engine.providers import PROVIDERS
from backend.engine.session import GameSessionManager
from backend.sdk.llm_bridge import LLMBridge


ORIGINAL = "ORIGINAL_STORY_FACTS"
SUMMARY = "OBSOLETE_REPLACEMENT_SUMMARY"


def legacy_data(enabled=True):
    return {"version": 1, "enabled": enabled, "preparing": True,
            "sections": [{"id": "old-section", "message_ids": ["u1", "a1"],
                          "summary": SUMMARY, "source_revision": "stale", "closed": not enabled}],
            "representations": {}, "failed_input": "Old failed request"}


def saved_adventure(tmp_path, payload):
    session = GameSessionManager(str(tmp_path / "data"))
    session.create_save("legacy")
    session.state["characters"] = {"default_player": {"name": "Mira", "personality": ORIGINAL, "hp": 8}}
    session.state["module_data"] = {"wb_npc_system": {"notes": ORIGINAL}}
    session.save_completed_turn({**session.state, "turn": 1, "history": [ORIGINAL]}, "First action")
    archive = session.save_manager.saves_dir / "legacy" / "Core" / "nsfw.json"
    archive.write_text(payload, encoding="utf-8")
    session.save_manager.save_turn("legacy", session.state, 1)
    session.load_save("legacy")
    return session, archive


@pytest.mark.parametrize("payload", [json.dumps(legacy_data()), json.dumps(legacy_data(False)), "{incomplete legacy data"])
def test_legacy_modes_and_broken_summaries_do_not_affect_loading(tmp_path, payload):
    session, archive = saved_adventure(tmp_path, payload)
    assert "nsfw" not in session.state
    assert session.state["history"] == [ORIGINAL]
    assert session.state["characters"]["default_player"]["personality"] == ORIGINAL
    assert session.state["module_data"]["wb_npc_system"]["notes"] == ORIGINAL
    messages = PromptCompiler().compile(session.state)["messages"]
    assert ORIGINAL in json.dumps(messages)
    assert SUMMARY not in json.dumps(messages)
    # Preserve existing archive bytes without interpreting them or requiring repair.
    assert archive.read_text(encoding="utf-8") == payload
    restarted = GameSessionManager(str(tmp_path / "data"))
    assert restarted.state["chat_messages"] == session.state["chat_messages"]
    assert "nsfw" not in restarted.state


def test_undo_swipe_branch_and_export_keep_originals_without_restoring_mode(tmp_path):
    payload = json.dumps(legacy_data())
    session, archive = saved_adventure(tmp_path, payload)
    first_turn = deepcopy(session.state["chat_messages"])
    session.save_completed_turn({**session.state, "turn": 2, "history": [ORIGINAL, "Next scene"]}, "Next action")
    session.begin_turn_swipes()
    session.prepare_regenerate()
    assert session.state["chat_messages"] == first_turn
    assert "nsfw" not in session.state
    session.save_completed_turn({**session.state, "turn": 2, "history": [ORIGINAL, "Other scene"]}, "Next action")
    session.add_regenerated_swipe()
    session.select_swipe(0)
    assert session.state["history"] == [ORIGINAL, "Next scene"]
    assert "nsfw" not in session.state
    session.undo_turn(1)
    assert session.state["chat_messages"] == first_turn
    assert session.state["characters"]["default_player"]["hp"] == 8
    assert "nsfw" not in session.state
    assert archive.read_text(encoding="utf-8") == payload
    session.branch_save("legacy", "branch")
    session.load_save("branch")
    assert session.state["chat_messages"] == first_turn
    assert "nsfw" not in session.state
    with zipfile.ZipFile(session.save_manager.saves_dir / "branch.wbx") as exported:
        assert exported.read("Core/nsfw.json").decode() == payload
        assert ORIGINAL in exported.read("Core/chat_messages.json").decode()


def test_compiler_ignores_legacy_context_even_if_passed_in_memory():
    state = {"nsfw": legacy_data(False), "chat_messages": [
        {"id": "u1", "role": "user", "content": "Original request"},
        {"id": "a1", "role": "ai", "content": ORIGINAL}]}
    messages = PromptCompiler().compile(state)["messages"]
    assert ORIGINAL in json.dumps(messages)
    assert "Original request" in json.dumps(messages)
    assert SUMMARY not in json.dumps(messages)


def test_removed_provider_slot_is_ignored_and_requests_are_not_rewritten(monkeypatch):
    for provider in PROVIDERS.values():
        assert "nsfw_model" not in provider["fields"]
        assert "openrouter_nsfw_provider" not in provider["fields"]
    llm = LLMService("live")
    llm.reconfigure("openrouter", {
        "storyteller_model": "openrouter/story", "reader_model": "openrouter/reader",
        "module_fast_model": "openrouter/fast", "embedding_model": "openai/embed",
        "nsfw_model": "removed/alternate", "openrouter_nsfw_provider": "removed-upstream",
        "openrouter_storyteller_provider": "ordinary-upstream",
    })
    llm._reasoning_effort = "off"
    llm.provider_retry_attempts = 1
    calls, embeddings = [], []

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="{}", reasoning_content=""), finish_reason="stop")],
            usage=SimpleNamespace(to_dict=lambda: {}))

    async def embed(**kwargs):
        embeddings.append(kwargs)
        return SimpleNamespace(data=[{"embedding": [0.1]} for _ in kwargs["input"]])

    monkeypatch.setattr("backend.engine.llm.acompletion", complete)
    monkeypatch.setattr("backend.engine.llm.aembedding", embed)
    bridge = LLMBridge()
    bridge._set_service(llm)

    async def run():
        await llm.generate_story_from_messages([{"role": "user", "content": ORIGINAL}])
        await llm.simple_completion([{"role": "user", "content": ORIGINAL}])
        await bridge.generate(ORIGINAL, "fastest")
        await llm.get_embeddings([ORIGINAL, "Second original memory"])

    asyncio.run(run())
    assert [c["model"] for c in calls] == ["openrouter/story", "openrouter/reader", "openrouter/fast"]
    assert all(c["messages"] == [{"role": "user", "content": ORIGINAL}] for c in calls)
    assert calls[0]["extra_body"]["provider"]["order"] == ["ordinary-upstream"]
    assert len(embeddings) == 1
    assert embeddings[0]["input"] == [ORIGINAL, "Second original memory"]
    assert embeddings[0]["model"] == "openai/embed"


@pytest.mark.parametrize("action", ["nsfw_mode", "nsfw_retry", "nsfw_summary"])
def test_stale_client_controls_cannot_generate_a_turn(tmp_path, monkeypatch, action):
    from fastapi.testclient import TestClient
    import backend.api.server as server
    session, _ = saved_adventure(tmp_path, json.dumps(legacy_data()))
    before = deepcopy(session.state)
    monkeypatch.setattr(server, "session_manager", session)
    monkeypatch.setattr(server, "chat_hub", server.ChatHub())
    pipeline = AsyncMock()
    monkeypatch.setattr(server.engine.app, "ainvoke", pipeline)
    with TestClient(server.app).websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": action, "enabled": True})
        while True:
            result = ws.receive_json()
            if result["type"] == "error":
                break
    assert result["code"] == "unsupported_action"
    pipeline.assert_not_awaited()
    assert session.state == before


def test_legacy_active_adventure_can_continue_without_summary_model(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import backend.api.server as server
    session, _ = saved_adventure(tmp_path, json.dumps(legacy_data()))
    monkeypatch.setattr(server, "session_manager", session)
    monkeypatch.setattr(server, "chat_hub", server.ChatHub())
    monkeypatch.setattr(server.engine.llm, "simple_completion", AsyncMock(side_effect=AssertionError("Unexpected summary request")))
    for name in ("dispatch_turn_start", "dispatch_turn_stopped", "ensure_memory"):
        monkeypatch.setattr(server.engine, name, AsyncMock())
    monkeypatch.setattr(server, "_init_world_index_for_save", lambda *args: None)
    monkeypatch.setattr(server.engine.settings, "get", lambda key: False)

    async def pipeline(state):
        assert ORIGINAL in json.dumps(PromptCompiler().compile(state)["messages"])
        return {**state, "turn": 2, "history": [*state["history"], "Continued normally"]}

    monkeypatch.setattr(server.engine.app, "ainvoke", pipeline)
    with TestClient(server.app).websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "Continue normally"})
        while True:
            result = ws.receive_json()
            if result["type"] in ("done", "error"):
                break
    assert result["type"] == "done", result
    assert result["state"]["history"] == [ORIGINAL, "Continued normally"]
    assert "nsfw" not in result["state"]
