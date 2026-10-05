"""Model boundary, non-destructive context, persistence and real WS operations."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.engine import nsfw
from backend.engine.llm import LLMService, LLMProviderError
from backend.engine.prompt_pipeline import PromptCompiler
from backend.engine.session import GameSessionManager
from backend.sdk.llm_bridge import LLMBridge


SECRET = "RAW_SCENE_MARKER"
SAFE = "The companions resolved their disagreement and exchanged a promise."


def adventure():
    state = {"turn": 0, "history": [], "chat_messages": [], "characters": {}, "module_data": {}}
    nsfw.enable(state)
    state["chat_messages"] = [{"role": "user", "content": SECRET}, {"role": "ai", "content": SECRET + " response"}]
    nsfw.ensure(state)
    state["history"] = [SECRET + " response"]
    state["turn"] = 1
    state["characters"] = {"hero": {"id": "hero", "hp": 8, "personality": SECRET}}
    nsfw.record_turn(state, state["chat_messages"])
    return state


def fake_provider(monkeypatch, calls):
    async def completion(**kwargs):
        calls.append(deepcopy(kwargs))
        content = json.dumps(kwargs["messages"], ensure_ascii=False)
        if kwargs["model"] == "alternate":
            if kwargs["messages"][0]["content"].startswith("Rewrite"):
                content = kwargs["messages"][-1]["content"].replace(SECRET, SAFE)
                if "complete archived context and a fields list" in kwargs["messages"][0]["content"]:
                    content = json.dumps({"fields": json.loads(content)["fields"]})
            else:
                content = "{}"
        else:
            assert SECRET not in content
            content = "{}"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, reasoning_content=""), finish_reason="stop")], usage=SimpleNamespace(to_dict=lambda: {}))
    monkeypatch.setattr("backend.engine.llm.acompletion", completion)


def service():
    llm = LLMService("live")
    llm.nsfw_model = "alternate"
    llm.storyteller_model = "normal-story"
    llm.reader_model = "normal-reader"
    llm.module_fast_model = "normal-fast"
    llm._reasoning_effort = "off"
    llm.provider_retry_attempts = 1
    return llm


def test_model_routing_is_task_local_and_restores(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    llm = service()
    bridge = LLMBridge()
    bridge._set_service(llm)

    async def run():
        async def active():
            with nsfw.operation(adventure(), llm):
                await bridge.generate(SECRET, "fastest")
                await llm.extract_mutations(SECRET, {})
                await llm.generate_story_from_messages([{"role": "user", "content": SECRET}])
        async def normal():
            await llm.simple_completion([{"role": "user", "content": "ordinary"}])
        await asyncio.gather(active(), normal())
        await normal()
    asyncio.run(run())
    assert [c["model"] for c in calls].count("alternate") == 3
    assert [c["model"] for c in calls].count("normal-reader") == 2
    assert nsfw.current() is None


def test_all_outbound_context_is_non_graphic_and_originals_survive(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    before = deepcopy(state)
    llm = service()

    async def run():
        with nsfw.operation(state, llm) as context:
            await context.prepare_sections()
            await context.prepare_derived()
            context.data["enabled"] = False
        with nsfw.operation(state, llm) as context:
            compiled = PromptCompiler().compile(state)["messages"]
            assert SECRET not in json.dumps(compiled)
            # Includes a cached module prompt built from raw/clipped history,
            # character state and retrieved memory, outside the chat compiler.
            for source in ["history", "character", "memory", "plot", "image_prompt", "validation"]:
                await llm.simple_completion([{"role": "user", "content": f"{source}: {SECRET}"}])
            count = len(calls)
            await context.messages([{"role": "user", "content": "history: " + SECRET}])
            assert len(calls) == count
        nsfw.enable(state)
        assert SECRET in json.dumps(nsfw.projected_messages(state))
    asyncio.run(run())
    assert state["characters"] == before["characters"]
    assert state["history"] == before["history"]
    assert state["chat_messages"] == before["chat_messages"]
    assert all(SECRET not in json.dumps(c["messages"]) for c in calls if c["model"] != "alternate")


def test_derived_notes_are_one_contextual_batch_and_reused(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    hero = state["characters"]["hero"]
    hero.update(name="Alex", race="human", gender="female", status="active",
                personality=["friendly", SECRET], short_appearance="A weathered traveller.")
    state["module_data"] = {"wb_plot_director": {"direction": {"premise": SECRET + " premise"},
                                              "momentum": "building", "suspended": False}}
    nsfw.record_turn(state, [])
    state["turn"] = 2
    nsfw.record_turn(state, [])
    canonical = deepcopy(state)
    llm = service()

    async def run():
        with nsfw.operation(state, llm) as context:
            await context.prepare_derived()
            assert len(calls) == 1
            payload = json.loads(calls[0]["messages"][-1]["content"])
            assert payload["context"] == state["nsfw"]["sections"][0]["originals"]
            assert len(payload["context"]) == 2
            assert {f["text"] for f in payload["fields"]} == {
                "friendly", SECRET, "A weathered traveller.", SECRET + " premise"}
            assert not any(f["text"] in {"human", "female", "active", "building"} for f in payload["fields"])
            await context.prepare_derived()
            assert len(calls) == 1
        state["nsfw"]["enabled"] = False
        with nsfw.operation(state, llm) as context:
            assert (await context.messages([{"role": "system", "content": SECRET}]))[0]["content"] == SAFE
            assert len(calls) == 1  # prepared prose needs no second model call
            outgoing = await context.messages([{"role": "system", "content": "Personality: " + SECRET + "\nHP: 8"}])
            assert outgoing[0]["content"] == "Personality: " + SAFE + "\nHP: 8"
            assert SECRET not in calls[-1]["messages"][-1]["content"]
            assert "__WB_PREPARED_" in calls[-1]["messages"][-1]["content"]
            count = len(calls)
            await context.messages([{"role": "system", "content": "Personality: " + SECRET + "\nHP: 8"}])
            assert len(calls) == count
        # Persisted representations work after reload, not just within one call.
        reloaded = json.loads(json.dumps(state))
        with nsfw.operation(reloaded, llm) as context:
            assert (await context.messages([{"role": "system", "content": SECRET}]))[0]["content"] == SAFE
            assert len(calls) == count
        nsfw.enable(state)
        with nsfw.operation(state, llm) as context:
            assert (await context.messages([{"role": "system", "content": SECRET}]))[-1]["content"] == SECRET
            assert len(calls) == count
    asyncio.run(run())
    for key in ("characters", "module_data", "history", "chat_messages"):
        assert state[key] == canonical[key]


def test_labels_only_state_does_not_make_derived_calls(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    state["nsfw"]["sections"][0]["originals"] = {
        "1": {"characters": {"hero": {"id": "hero", "name": "Alex", "race": "human", "hp": 8,
                                      "status": "active", "inventory_ids": ["sword"]}}}}
    asyncio.run(nsfw.AdventureContext(state, service()).prepare_derived())
    assert calls == []


def test_derived_revision_rebuilds_and_legacy_word_cache_is_ignored(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    state["nsfw"]["representations"][nsfw.digest(["note", SECRET])] = {
        "source": SECRET, "text": "obsolete per-word rewrite", "purpose": "note"}
    async def run():
        context = nsfw.AdventureContext(state, service())
        await context.prepare_derived()
        assert len(calls) == 1
        section = state["nsfw"]["sections"][0]
        section["originals"]["1"]["characters"]["hero"]["personality"] = SECRET + " revised"
        assert not list(context.prepared_pairs())
        await context.prepare_derived()
        assert len(calls) == 2
        assert await context.prepare_prompt(SECRET + " revised") == SAFE + " revised"
        assert len(calls) == 2
        assert section["prepared_notes"]["fields"][0]["source"] == SECRET + " revised"
    asyncio.run(run())


@pytest.mark.parametrize("bad_result", [
    "not JSON", '{"fields": []}',
    '{"fields": [{"path": ["1", "characters", "hero", "hp"], "text": "9"}]}',
    '{"fields": [{"path": ["1", "characters", "hero", "personality"], "text": ""}]}',
])
def test_invalid_derived_output_is_not_cached_or_committed(bad_result):
    state = adventure()
    before = deepcopy(state)
    async def completion(*args, **kwargs):
        return bad_result
    llm = SimpleNamespace(nsfw_model="alternate", simple_completion=completion)
    with pytest.raises(nsfw.ContextPreparationError):
        asyncio.run(nsfw.AdventureContext(state, llm).prepare_derived())
    assert state == before


def test_manual_summary_is_reused_verbatim_and_placeholder_loss_fails(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    llm = service()
    async def run():
        context = nsfw.AdventureContext(state, llm)
        await context.prepare_sections()
        section = state["nsfw"]["sections"][0]
        section["summary"] = "Player's authoritative correction."
        section["manual"] = True
        text = "Story events (non-graphic summary):\n" + section["summary"]
        assert await context.prepare_prompt(text) == text
        assert section["summary"] not in calls[-1]["messages"][-1]["content"]
        async def damaged(*args, **kwargs):
            return "Model omitted the prepared context."
        llm.simple_completion = damaged
        with pytest.raises(nsfw.ContextPreparationError, match="placeholder"):
            await context.prepare_prompt("New prefix: " + section["summary"])
    asyncio.run(run())


def test_prepared_notes_preserve_json_escaping_and_do_not_replace_partial_words(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    section = state["nsfw"]["sections"][0]
    safe_note = 'An accented name: Renée.\nA promise: "Return tomorrow."'
    section["prepared_notes"] = {
        "revision": nsfw.digest(section["originals"]),
        "fields": [{"path": ["1", "characters", "hero", "personality"], "source": SECRET, "text": safe_note}],
    }
    async def run():
        context = nsfw.AdventureContext(state, service())
        assert await context.prepare_prompt(SECRET) == safe_note
        assert calls == []
        for ascii_only in (False, True):
            source = json.dumps({"notes": SECRET, "hp": 8}, ensure_ascii=ascii_only)
            result = await context.prepare_prompt(source)
            assert json.loads(result) == {"notes": safe_note, "hp": 8}
            # The fallback receives valid JSON and never the original note.
            assert json.loads(calls[-1]["messages"][-1]["content"])["hp"] == 8
        await context.prepare_prompt("prefix" + SECRET + "suffix")
        assert "__WB_PREPARED_" not in calls[-1]["messages"][-1]["content"]
    asyncio.run(run())


def test_embedding_keeps_embedding_model_but_not_original_text(monkeypatch):
    calls, embeddings = [], []
    fake_provider(monkeypatch, calls)
    llm = service()
    async def embed(**kwargs):
        embeddings.append(kwargs)
        return SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])
    monkeypatch.setattr("backend.engine.llm.aembedding", embed)
    async def run():
        with nsfw.operation(adventure(), llm):
            await llm.get_embeddings([SECRET, SECRET + " second"])
    asyncio.run(run())
    assert len(embeddings) == 2
    assert all(e["model"] == llm.embedding_model and SECRET not in e["input"] for e in embeddings)


def test_alternate_failure_never_uses_normal_fallback(monkeypatch):
    llm = service()
    llm.storyteller_fallback_models = ["normal-fallback"]
    calls = []
    async def fail(**kwargs):
        calls.append(kwargs["model"])
        raise RuntimeError("offline")
    monkeypatch.setattr("backend.engine.llm.acompletion", fail)
    async def run():
        with nsfw.operation(adventure(), llm):
            with pytest.raises(LLMProviderError):
                await llm.generate_story_from_messages([{"role": "user", "content": SECRET}])
        with nsfw.operation(adventure(), llm) as context:
            bridge = LLMBridge()
            bridge._set_service(llm)
            with pytest.raises(RuntimeError):
                await bridge.generate(SECRET)
            with pytest.raises(nsfw.ContextPreparationError):
                context.check()
    asyncio.run(run())
    assert calls == ["alternate", "alternate"]


def test_source_edit_invalidates_summary_and_keeps_message_ids(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    llm = service()
    async def run():
        with nsfw.operation(state, llm) as context:
            await context.prepare_sections()
        state["nsfw"]["enabled"] = False
        section = state["nsfw"]["sections"][0]
        section["summary"] = "Player's correction"
        section["manual"] = True
        with nsfw.operation(state, llm) as context:
            await context.prepare_sections()
        assert section["summary"] == "Player's correction"
        mid = state["chat_messages"][0]["id"]
        state["chat_messages"][0]["content"] += " changed"
        with pytest.raises(nsfw.ContextPreparationError):
            nsfw.projected_messages(state)
        with nsfw.operation(state, llm) as context:
            await context.prepare_sections()
        assert not section["manual"]
        assert state["chat_messages"][0]["id"] == mid
    asyncio.run(run())


def test_save_undo_swipe_and_branch_keep_context(tmp_path):
    session = GameSessionManager(str(tmp_path))
    session.create_save("story")
    session.save_manager.save_turn("story", session.state, 0)
    nsfw.enable(session.state)
    session.save_completed_turn({**session.state, "history": [SECRET], "turn": 1}, user_text=SECRET)
    session.begin_turn_swipes()
    original = deepcopy(session.state["nsfw"])
    session.load_save("story")
    assert session.state["nsfw"] == original
    assert session.state["nsfw"]["sections"][0]["message_ids"] == [m["id"] for m in session.state["chat_messages"]]
    session.select_swipe(0)
    assert session.state["nsfw"] == original
    session.branch_save("story", "branch")
    session.load_save("branch")
    assert session.state["nsfw"] == original
    session.undo_turn(0)
    assert not session.state["nsfw"]["enabled"]
    assert session.state["nsfw"]["sections"] == []


def test_provider_reconfiguration_clears_missing_slot():
    llm = service()
    llm.reconfigure("openrouter", {"nsfw_model": "openrouter/example", "openrouter_nsfw_provider": "upstream"})
    assert llm.nsfw_model == "openrouter/example"
    with nsfw.operation(adventure(), llm):
        assert llm._provider_route_kwargs(llm.nsfw_model)["extra_body"]["provider"]["order"] == ["upstream"]
    llm.reconfigure("gemini", {})
    assert llm.nsfw_model == ""


@pytest.fixture
def ws_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import backend.api.server as server
    session = GameSessionManager(str(tmp_path / "data"))
    session.create_save("story")
    session.save_manager.save_turn("story", session.state, 0)
    monkeypatch.setattr(server, "session_manager", session)
    monkeypatch.setattr(server, "chat_hub", server.ChatHub())
    monkeypatch.setattr(server.engine.llm, "nsfw_model", "alternate")
    async def completion(*args, **kwargs):
        if kwargs.get("inspector_ctx", {}).get("step") == "nsfw:notes_v2":
            payload = json.loads(args[0][-1]["content"])
            return json.dumps({"fields": [{**f, "text": SAFE} for f in payload["fields"]]})
        return SAFE
    monkeypatch.setattr(server.engine.llm, "simple_completion", completion)
    async def pipeline(state):
        return {**state, "turn": state["turn"] + 1, "history": state["history"] + [SECRET]}
    monkeypatch.setattr(server.engine.app, "ainvoke", pipeline)
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(server.engine, "dispatch_turn_start", noop)
    monkeypatch.setattr(server.engine, "dispatch_turn_stopped", noop)
    monkeypatch.setattr(server.engine, "ensure_memory", noop)
    monkeypatch.setattr(server, "_init_world_index_for_save", lambda *args: None)
    monkeypatch.setattr(server.engine, "rollback_memory", lambda *args: 0)
    monkeypatch.setattr(server.engine.settings, "get", lambda key: False if key == "storyteller.auto_mode" else 1)
    return TestClient(server.app), session, server


def receive(ws):
    while True:
        data = ws.receive_json()
        if data["type"] in {"done", "error", "turn_stopped"}:
            return data


def test_ws_toggle_summary_edit_retry_and_reload(ws_client):
    client, session, server = ws_client
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "turn", "text": "original request"})
        assert receive(ws)["type"] == "done"
        ws.send_json({"action": "nsfw_retry"})
        done = receive(ws)
        assert done["type"] == "done", done
        assert done["state"]["turn"] == 1
        assert done["state"]["nsfw"]["enabled"]
        assert done["state"]["nsfw"]["previous_attempts"][0]["content"] == SECRET
        assert len(done["state"]["chat_messages"]) == 2
        ws.send_json({"action": "nsfw_mode", "enabled": False})
        done = receive(ws)
        assert done["type"] == "done", done
        section = done["state"]["nsfw"]["sections"][0]
        assert not done["state"]["nsfw"]["enabled"]
        assert section["summary"] == SAFE
        ws.send_json({"action": "nsfw_summary", "section_id": section["id"], "summary": "A corrected promise."})
        assert receive(ws)["state"]["nsfw"]["sections"][0]["manual"]
    session.load_save("story")
    assert session.state["nsfw"]["sections"][0]["summary"] == "A corrected promise."


def test_ws_summary_failure_keeps_original_state(ws_client, monkeypatch):
    client, session, server = ws_client
    nsfw.enable(session.state)
    session.save_completed_turn({**session.state, "turn": 1, "history": [SECRET]}, SECRET)
    before = deepcopy(session.state)
    async def fail(*args, **kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr(server.engine.llm, "simple_completion", fail)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "nsfw_mode", "enabled": False})
        assert receive(ws)["type"] == "error"
    assert session.state == before


def test_ws_failed_request_retry_does_not_undo_previous_turn(ws_client):
    client, session, server = ws_client
    session.save_completed_turn({**session.state, "turn": 1, "history": ["Earlier success"]}, "previous")
    nsfw.ensure(session.state)["failed_input"] = "failed request"
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "nsfw_retry"})
        done = receive(ws)
    assert done["type"] == "done", done
    assert done["state"]["turn"] == 2
    assert done["state"]["chat_messages"][-2]["content"] == "failed request"
    assert done["state"]["chat_messages"][1]["content"] == "Earlier success"


def test_ws_missing_model_does_not_enable_mode(ws_client, monkeypatch):
    client, session, server = ws_client
    monkeypatch.setattr(server.engine.llm, "nsfw_model", "")
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "nsfw_mode", "enabled": True})
        error = receive(ws)
    assert error["type"] == "error"
    assert "Choose an NSFW Model" in error["message"]
    assert not session.state["nsfw"]["enabled"]


def test_ws_cancelled_transition_is_atomic_and_busy_rejected(ws_client, monkeypatch):
    client, session, server = ws_client
    nsfw.enable(session.state)
    session.save_completed_turn({**session.state, "turn": 1, "history": [SECRET]}, SECRET)
    before = deepcopy(session.state["nsfw"])
    async def wait_forever(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(server.engine.llm, "simple_completion", wait_forever)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "nsfw_mode", "enabled": False})
        while ws.receive_json()["type"] != "status":
            pass
        ws.send_json({"action": "nsfw_mode", "enabled": True})
        assert receive(ws)["code"] == "busy"
        assert client.put("/api/session/messages/0", json={"content": "racing edit"}).status_code == 409
        ws.send_json({"action": "stop"})
        assert receive(ws)["type"] == "turn_stopped"
    assert session.state["nsfw"] == before


def test_ws_failed_retry_restores_mode_and_prior_turn(ws_client, monkeypatch):
    client, session, server = ws_client
    session.save_completed_turn({**session.state, "turn": 1, "history": ["A refusal"]}, "request")
    session.begin_turn_swipes()
    before = deepcopy(session.state)
    async def fail(*args, **kwargs):
        raise RuntimeError("retry provider unavailable")
    monkeypatch.setattr(server.engine.app, "ainvoke", fail)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "nsfw_retry"})
        assert receive(ws)["type"] == "error"
    assert session.state == before
    session.load_save("story")
    assert session.state["chat_messages"] == before["chat_messages"]
    assert not session.state["nsfw"]["enabled"]


def test_multiple_sections_keep_chronology_and_empty_sections(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    llm = service()
    async def close():
        with nsfw.operation(state, llm) as context:
            await context.prepare_sections()
            context.data["sections"][-1]["closed"] = True
            context.data["enabled"] = False
    async def run():
        await close()
        state["chat_messages"].append({"role": "ai", "content": "Ordinary interlude"})
        nsfw.enable(state)
        await close()
        nsfw.enable(state)
        m = {"role": "user", "content": SECRET + " later"}
        state["chat_messages"].append(m)
        nsfw.ensure(state)
        nsfw.record_turn(state, [m])
        await close()
    asyncio.run(run())
    projected = nsfw.projected_messages(state)
    assert len(projected) == 3
    assert projected[1]["content"] == "Ordinary interlude"
    assert SECRET not in json.dumps(projected)


def test_mode_snapshot_is_inherited_by_background_task(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    llm = service()
    async def run():
        ready = asyncio.Event()
        async def background():
            await ready.wait()
            await llm.simple_completion([{"role": "user", "content": SECRET}])
        with nsfw.operation(state, llm):
            task = asyncio.create_task(background())
        state["nsfw"]["enabled"] = False
        ready.set()
        await task
    asyncio.run(run())
    assert calls[0]["model"] == "alternate"


def test_reenabled_mode_recovers_archived_note_versions():
    state = adventure()
    state["characters"]["hero"]["personality"] = "A later non-graphic description"
    async def run():
        with nsfw.operation(state, service()) as context:
            messages = await context.messages([{"role": "user", "content": "Continue."}])
            assert SECRET in messages[0]["content"]
            assert "not current game state" in messages[0]["content"]
    asyncio.run(run())
    assert state["characters"]["hero"]["personality"] == "A later non-graphic description"


def test_memory_checkpoint_restores_deleted_rows_on_failure(tmp_path):
    import sqlite3
    path = tmp_path / "memories.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE memories (id TEXT, text TEXT)")
        connection.execute("INSERT INTO memories VALUES ('original', 'prior event')")
        connection.commit()
        with pytest.raises(RuntimeError):
            with nsfw.memory_checkpoint(tmp_path):
                connection.execute("DELETE FROM memories")
                connection.execute("INSERT INTO memories VALUES ('retry', 'new event')")
                connection.commit()
                raise RuntimeError("retry failed")
        assert connection.execute("SELECT * FROM memories").fetchall() == [("original", "prior event")]


def test_same_model_slots_keep_their_provider_routes_separate():
    llm = service()
    llm.reconfigure("openrouter", {"storyteller_model": "shared", "nsfw_model": "shared",
                                   "openrouter_storyteller_provider": "ordinary-upstream",
                                   "openrouter_nsfw_provider": "alternate-upstream"})
    assert llm._provider_route_kwargs("shared")["extra_body"]["provider"]["order"] == ["ordinary-upstream"]
    with nsfw.operation(adventure(), llm):
        assert llm._provider_route_kwargs("shared")["extra_body"]["provider"]["order"] == ["alternate-upstream"]
    assert llm._provider_route_kwargs("shared")["extra_body"]["provider"]["order"] == ["ordinary-upstream"]


def test_normal_storyteller_receives_current_player_action_unchanged(monkeypatch):
    calls = []
    fake_provider(monkeypatch, calls)
    state = adventure()
    state["nsfw"]["enabled"] = False
    llm = service()
    async def run():
        with nsfw.operation(state, llm):
            await llm.generate_story_from_messages([
                {"role": "system", "content": "Historical note: " + SECRET},
                {"role": "user", "content": "CURRENT_PLAYER_ACTION stays verbatim."},
            ])
    asyncio.run(run())
    normal = [call for call in calls if call["model"] == "normal-story"]
    assert normal[0]["messages"][-1]["content"] == "CURRENT_PLAYER_ACTION stays verbatim."
    assert SECRET not in json.dumps(normal)
