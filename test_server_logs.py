import io
import logging

from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.log_store import LogStore, LogStoreHandler, StreamTee


def make_client(monkeypatch):
    store = LogStore()
    monkeypatch.setattr(server, "log_store", store)
    return TestClient(server.app), store


# ---------------------------------------------------------------- endpoints

def test_get_logs_empty(monkeypatch):
    client, _ = make_client(monkeypatch)
    resp = client.get("/api/logs")
    assert resp.status_code == 200
    assert resp.json() == {"logs": []}


def test_get_logs_returns_records_oldest_first(monkeypatch):
    client, store = make_client(monkeypatch)
    store.add("INFO", "stdout", "first")
    store.add("ERROR", "stderr", "second")

    logs = client.get("/api/logs").json()["logs"]
    assert [l["message"] for l in logs] == ["first", "second"]
    assert logs[0]["id"] < logs[1]["id"]
    assert logs[0]["level"] == "INFO"
    assert logs[1]["level"] == "ERROR"
    assert all("timestamp" in l and "source" in l for l in logs)


def test_get_logs_since_id_is_incremental(monkeypatch):
    client, store = make_client(monkeypatch)
    store.add("INFO", "stdout", "old")
    anchor = client.get("/api/logs").json()["logs"][-1]["id"]

    store.add("INFO", "stdout", "new")
    logs = client.get(f"/api/logs?since_id={anchor}").json()["logs"]
    assert [l["message"] for l in logs] == ["new"]

    # Nothing new since the latest id -> empty poll.
    latest = logs[-1]["id"]
    assert client.get(f"/api/logs?since_id={latest}").json()["logs"] == []


def test_get_logs_error_filter(monkeypatch):
    client, store = make_client(monkeypatch)
    store.add("INFO", "stdout", "fine")
    store.add("WARNING", "backend.engine", "hmm")
    store.add("ERROR", "stderr", "boom")
    store.add("CRITICAL", "backend.engine", "meltdown")

    logs = client.get("/api/logs?level=error").json()["logs"]
    assert [l["message"] for l in logs] == ["boom", "meltdown"]


def test_get_logs_limit(monkeypatch):
    client, store = make_client(monkeypatch)
    for i in range(5):
        store.add("INFO", "stdout", f"line {i}")

    logs = client.get("/api/logs?limit=2").json()["logs"]
    # The most recent records win when limited.
    assert [l["message"] for l in logs] == ["line 3", "line 4"]


def test_clear_logs(monkeypatch):
    client, store = make_client(monkeypatch)
    store.add("INFO", "stdout", "gone soon")

    resp = client.delete("/api/logs")
    assert resp.status_code == 200
    assert resp.json() == {"cleared": True}
    assert client.get("/api/logs").json()["logs"] == []

    # Ids keep increasing after a clear so since_id polling stays valid.
    store.add("INFO", "stdout", "after clear")
    logs = client.get("/api/logs").json()["logs"]
    assert logs[0]["id"] > 1


# ---------------------------------------------------------------- capture

def test_ring_buffer_evicts_oldest():
    store = LogStore(max_records=3)
    for i in range(5):
        store.add("INFO", "stdout", f"line {i}")

    logs = store.get_logs()
    assert [l["message"] for l in logs] == ["line 2", "line 3", "line 4"]


def test_logging_handler_captures_records():
    store = LogStore()
    log = logging.getLogger("test_server_logs.handler")
    log.setLevel(logging.INFO)
    log.addHandler(LogStoreHandler(store))
    try:
        log.info("hello %s", "world")
        log.error("kaboom")
    finally:
        log.handlers.clear()

    logs = store.get_logs()
    assert [(l["level"], l["message"]) for l in logs] == [
        ("INFO", "hello world"),
        ("ERROR", "kaboom"),
    ]
    assert logs[0]["source"] == "test_server_logs.handler"


def test_logging_handler_ignores_debug():
    store = LogStore()
    log = logging.getLogger("test_server_logs.debug")
    log.setLevel(logging.DEBUG)
    log.addHandler(LogStoreHandler(store))
    try:
        log.debug("too chatty")
    finally:
        log.handlers.clear()
    assert store.get_logs() == []


def test_stream_tee_writes_through_and_captures_lines():
    store = LogStore()
    underlying = io.StringIO()
    tee = StreamTee(underlying, store, "stdout")

    tee.write("partial")
    assert store.get_logs() == []  # incomplete line is buffered

    tee.write(" line\nsecond line\n")
    assert underlying.getvalue() == "partial line\nsecond line\n"
    assert [l["message"] for l in store.get_logs()] == ["partial line", "second line"]
    assert all(l["level"] == "INFO" for l in store.get_logs())


def test_stream_tee_classifies_levels():
    store = LogStore()
    tee = StreamTee(io.StringIO(), store, "stdout")
    tee.write("all good\n")
    tee.write("Turn failed with error: boom\n")
    tee.write("Warning: model is deprecated\n")

    err_tee = StreamTee(io.StringIO(), store, "stderr")
    err_tee.write('  File "x.py", line 1, in <module>\n')

    assert [l["level"] for l in store.get_logs()] == ["INFO", "ERROR", "WARNING", "ERROR"]

    # The errors-only view picks up print()-ed failures and stderr output.
    errors = store.get_logs(level="error")
    assert [l["message"] for l in errors] == [
        "Turn failed with error: boom",
        '  File "x.py", line 1, in <module>',
    ]
