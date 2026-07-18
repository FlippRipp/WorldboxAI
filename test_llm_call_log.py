import asyncio
import json
import types

from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.llm_call_log import LLMCallLog
from backend.engine.llm_inspector import LLMInspector


# ---------------------------------------------------------------- LLMCallLog

def test_log_call_appends_jsonl(tmp_path):
    log = LLMCallLog(str(tmp_path / "logs"))
    log.log_call({"id": "a1", "model": "mock", "full_output": "hello"})
    log.log_call({"id": "b2", "model": "mock", "full_output": "wörld"})

    lines = log.read_all().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"id": "a1", "model": "mock", "full_output": "hello"}
    assert json.loads(lines[1])["full_output"] == "wörld"


def test_read_all_empty_without_file(tmp_path):
    log = LLMCallLog(str(tmp_path / "logs"))
    assert log.read_all() == ""
    # The directory is created lazily, only on the first write.
    assert not (tmp_path / "logs").exists()


def test_log_call_survives_unserializable_record(tmp_path):
    log = LLMCallLog(str(tmp_path))
    circular = {}
    circular["self"] = circular
    log.log_call({"id": "c3", "full_input": circular})

    record = json.loads(log.read_all())
    assert record == {"id": "c3", "error": "unserializable record"}


# ------------------------------------------------------- inspector wiring

def test_inspector_logs_completed_calls(tmp_path):
    log = LLMCallLog(str(tmp_path))
    inspector = LLMInspector()
    inspector.set_call_logger(log)

    async def run():
        messages = [{"role": "user", "content": "hi"}]
        cid = await inspector.start_call("reader", "mock", "step1", input_data=messages)
        await inspector.end_call(cid, messages, "output text", tokens_in=3, tokens_out=5)

    asyncio.run(run())

    record = json.loads(log.read_all())
    assert record["call_type"] == "reader"
    assert record["status"] == "complete"
    assert record["full_input"] == [{"role": "user", "content": "hi"}]
    assert record["full_output"] == "output text"
    assert record["tokens_in"] == 3
    assert record["tokens_out"] == 5


def test_inspector_logs_errors_and_cancellations(tmp_path):
    log = LLMCallLog(str(tmp_path))
    inspector = LLMInspector()
    inspector.set_call_logger(log)

    async def run():
        cid = await inspector.start_call("storyteller", "mock", "s", input_data="in")
        await inspector.end_call(cid, "in", "", error="boom")
        cid = await inspector.start_call("storyteller", "mock", "s", input_data="in")
        await inspector.end_call(cid, "in", cancelled=True)

    asyncio.run(run())

    statuses = [json.loads(line)["status"] for line in log.read_all().splitlines()]
    assert statuses == ["error", "cancelled"]


def test_inspector_survives_broken_logger():
    class BrokenLogger:
        def log_call(self, record):
            raise OSError("disk full")

    inspector = LLMInspector()
    inspector.set_call_logger(BrokenLogger())

    async def run():
        cid = await inspector.start_call("reader", "mock", "s", input_data="in")
        await inspector.end_call(cid, "in", "out")

    asyncio.run(run())
    assert inspector.get_calls()[0]["status"] == "complete"


# ------------------------------------------------------------- endpoints

def test_dump_llm_log_endpoint(tmp_path, monkeypatch):
    log = LLMCallLog(str(tmp_path))
    log.log_call({"id": "a1", "full_output": "hello"})
    monkeypatch.setattr(server, "llm_call_log", log)

    client = TestClient(server.app)
    resp = client.get("/api/llm-log/dump")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert ".jsonl" in resp.headers["content-disposition"]
    assert json.loads(resp.text)["id"] == "a1"


def test_dump_save_state_endpoint(monkeypatch):
    state = {"core": {"metadata": {"turn": 7}}}

    class StubSaveManager:
        def load_save(self, save_id):
            if save_id != "demo save":
                raise FileNotFoundError(f"Save {save_id} not found.")
            return state

    monkeypatch.setattr(server, "session_manager",
                        types.SimpleNamespace(save_manager=StubSaveManager()))

    client = TestClient(server.app)
    resp = client.get("/api/saves/demo save/dump")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    # The download filename uses a filesystem-safe id and a timestamp.
    assert 'filename="save_dump_demo_save_' in disposition
    assert resp.json() == state

    resp = client.get("/api/saves/missing/dump")
    assert resp.status_code == 404
