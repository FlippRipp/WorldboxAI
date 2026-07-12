"""Generation must survive the client: a disconnect (closed tab, sleeping
laptop, network blip) mid-turn must not cancel the running generation. The
turn finishes headless, saves normally, and a returning client either
re-attaches to the live stream (`generation_snapshot`) or finds the finished
turn in the replayed transcript.

These tests drive `websocket_endpoint` directly with fake sockets inside one
event loop. TestClient can't express this scenario: it gives every websocket
session its own portal/event loop, so the headless turn task would die with
the first session's loop — exactly the coupling this feature removes.
"""

import asyncio
from types import SimpleNamespace

from starlette.websockets import WebSocketDisconnect

import backend.api.server as server
from backend.engine.session import GameSessionManager


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self._cursor = 0
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def accept(self):
        pass

    async def send_json(self, payload):
        if self.closed:
            raise RuntimeError("websocket is closed")
        self.sent.append(payload)

    async def receive_json(self):
        item = await self._incoming.get()
        if item is None:
            raise WebSocketDisconnect(1000)
        return item

    def client_send(self, payload):
        self._incoming.put_nowait(payload)

    def disconnect(self):
        self.closed = True
        self._incoming.put_nowait(None)

    async def wait_for(self, types, timeout=5.0):
        """Next sent message of one of `types`, consuming everything before it."""
        async def _scan():
            while True:
                while self._cursor < len(self.sent):
                    msg = self.sent[self._cursor]
                    self._cursor += 1
                    if msg["type"] in types:
                        return msg
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(_scan(), timeout)


class GatedInvoke:
    """Stand-in for engine.app: streams two tokens, then holds the turn open
    until the test releases `gate` — simulating a long generation the test can
    disconnect in the middle of."""

    STORY = "The vault creaks open."

    def __init__(self, error=None):
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.error = error

    async def ainvoke(self, state):
        await server.engine.sdk.ui.emit_token("The vault ")
        await server.engine.sdk.ui.emit_token("creaks open.")
        self.started.set()
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        return {
            **state,
            "history": list(state.get("history", [])) + [self.STORY],
            "turn": state.get("turn", 0) + 1,
        }


def setup_session(tmp_path, monkeypatch):
    session_manager = GameSessionManager(str(tmp_path / "data"))
    session_manager.create_save("autosave")
    monkeypatch.setattr(server, "session_manager", session_manager)
    server.engine.set_memory_path(session_manager.get_memory_path())
    server.engine.llm.mode = "mock"
    # Fresh hub so turn/buffer state can't leak between tests.
    monkeypatch.setattr(server, "chat_hub", server.ChatHub())
    return session_manager


async def start_turn_and_disconnect(gated, text="I open the vault."):
    """Connect, start a turn, and drop the client once generation is running.
    Returns after the endpoint has fully unwound."""
    ws = FakeWebSocket()
    endpoint = asyncio.create_task(server.websocket_endpoint(ws))
    ws.client_send({"action": "turn", "text": text})
    await asyncio.wait_for(gated.started.wait(), 5)
    ws.disconnect()
    await asyncio.wait_for(endpoint, 5)
    return ws


def test_turn_survives_disconnect_and_is_saved(tmp_path, monkeypatch):
    session_manager = setup_session(tmp_path, monkeypatch)
    gated = GatedInvoke()
    monkeypatch.setattr(server.engine, "app", SimpleNamespace(ainvoke=gated.ainvoke))

    async def scenario():
        await start_turn_and_disconnect(gated)
        # The socket is gone but the turn is still generating.
        assert server.chat_hub.turn_running()
        gated.gate.set()
        await asyncio.wait_for(server.chat_hub.turn_task, 5)

    asyncio.run(scenario())

    # The finished turn was persisted despite nobody being connected.
    msgs = session_manager.state["chat_messages"]
    assert msgs[-2]["role"] == "user"
    assert msgs[-2]["content"] == "I open the vault."
    assert msgs[-1]["role"] == "ai"
    assert msgs[-1]["content"] == GatedInvoke.STORY
    assert session_manager.state["turn"] == 1


def test_reconnect_mid_turn_reattaches_stream(tmp_path, monkeypatch):
    setup_session(tmp_path, monkeypatch)
    gated = GatedInvoke()
    monkeypatch.setattr(server.engine, "app", SimpleNamespace(ainvoke=gated.ainvoke))

    async def scenario():
        await start_turn_and_disconnect(gated)

        # Reconnect while the turn is still running: sync must repaint the
        # in-flight turn instead of pretending it never happened.
        ws2 = FakeWebSocket()
        endpoint2 = asyncio.create_task(server.websocket_endpoint(ws2))
        ws2.client_send({"action": "sync"})
        await ws2.wait_for({"state_load"})
        snap = await ws2.wait_for({"generation_snapshot", "done"})
        assert snap["type"] == "generation_snapshot"
        assert snap["action"] == "turn"
        assert snap["input"] == "I open the vault."
        assert snap["story"] == "The vault creaks open."
        assert snap["narration_complete"] is False
        # No `done` yet — the client must stay in its generating state.
        assert not any(m["type"] == "done" for m in ws2.sent)

        # The turn finishes and its `done` lands on the new socket.
        gated.gate.set()
        done = await ws2.wait_for({"done"})
        assert done["state"]["chat_messages"][-1]["content"] == GatedInvoke.STORY

        ws2.disconnect()
        await asyncio.wait_for(endpoint2, 5)

    asyncio.run(scenario())


def test_reload_intro_mid_turn_reattaches_instead_of_busy(tmp_path, monkeypatch):
    # A page reload boots with a quiet `intro`. Mid-generation that must act
    # like a sync (replay + snapshot), not reject the client with `busy`.
    setup_session(tmp_path, monkeypatch)
    gated = GatedInvoke()
    monkeypatch.setattr(server.engine, "app", SimpleNamespace(ainvoke=gated.ainvoke))

    async def scenario():
        await start_turn_and_disconnect(gated)

        ws2 = FakeWebSocket()
        endpoint2 = asyncio.create_task(server.websocket_endpoint(ws2))
        ws2.client_send({"action": "intro"})
        await ws2.wait_for({"state_load"})
        snap = await ws2.wait_for({"generation_snapshot", "error"})
        assert snap["type"] == "generation_snapshot"
        assert snap["story"] == "The vault creaks open."
        assert not any(m["type"] == "error" for m in ws2.sent)

        gated.gate.set()
        await ws2.wait_for({"done"})
        ws2.disconnect()
        await asyncio.wait_for(endpoint2, 5)

    asyncio.run(scenario())


def test_stop_still_works_from_reconnected_socket(tmp_path, monkeypatch):
    session_manager = setup_session(tmp_path, monkeypatch)
    history_before = list(session_manager.state.get("history", []))
    gated = GatedInvoke()
    monkeypatch.setattr(server.engine, "app", SimpleNamespace(ainvoke=gated.ainvoke))

    async def scenario():
        await start_turn_and_disconnect(gated)

        ws2 = FakeWebSocket()
        endpoint2 = asyncio.create_task(server.websocket_endpoint(ws2))
        ws2.client_send({"action": "sync"})
        await ws2.wait_for({"generation_snapshot"})

        # The hub owns the task, so stop works from the new socket too.
        ws2.client_send({"action": "stop"})
        stopped = await ws2.wait_for({"turn_stopped", "done", "error"})
        assert stopped["type"] == "turn_stopped"
        assert stopped["input"] == "I open the vault."

        ws2.disconnect()
        await asyncio.wait_for(endpoint2, 5)

    asyncio.run(scenario())

    assert session_manager.state.get("history", []) == history_before
    assert not server.chat_hub.turn_running()


def test_error_while_disconnected_is_delivered_on_sync(tmp_path, monkeypatch):
    setup_session(tmp_path, monkeypatch)
    gated = GatedInvoke(error=RuntimeError("provider exploded"))
    monkeypatch.setattr(server.engine, "app", SimpleNamespace(ainvoke=gated.ainvoke))

    async def scenario():
        await start_turn_and_disconnect(gated)
        gated.gate.set()
        await asyncio.wait_for(server.chat_hub.turn_task, 5)

        # The failure happened with nobody connected; state replay can't
        # convey it, so sync must hand over the queued error before `done`.
        ws2 = FakeWebSocket()
        endpoint2 = asyncio.create_task(server.websocket_endpoint(ws2))
        ws2.client_send({"action": "sync"})
        await ws2.wait_for({"state_load"})
        err = await ws2.wait_for({"error", "done"})
        assert err["type"] == "error"
        assert err["code"] == "turn_failed"
        await ws2.wait_for({"done"})

        # Delivered once: a second sync doesn't replay the error again.
        ws2.client_send({"action": "sync"})
        await ws2.wait_for({"state_load"})
        nxt = await ws2.wait_for({"error", "done"})
        assert nxt["type"] == "done"

        ws2.disconnect()
        await asyncio.wait_for(endpoint2, 5)

    asyncio.run(scenario())
