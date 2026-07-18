"""Persistent on-disk log of every LLM call.

The in-memory LLMInspector keeps only the most recent calls and forgets
them on restart. This log receives every completed call from the inspector
and appends it — full untruncated input/output — as one JSON line to
data/logs/llm_calls.jsonl, so the complete history can be dumped from the
settings screen for debugging.

The same logs directory also receives save-state dumps (see
write_save_dump), so everything written for offline inspection lives in
one place.
"""

import json
import re
import threading
import time
from pathlib import Path

LLM_LOG_FILENAME = "llm_calls.jsonl"


class LLMCallLog:
    """Append-only JSONL file of completed LLM calls. Thread-safe; the file
    and its directory are created lazily on first write."""

    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)
        self.log_path = self.log_dir / LLM_LOG_FILENAME
        self._lock = threading.Lock()

    def log_call(self, record: dict):
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            line = json.dumps({"id": record.get("id", ""), "error": "unserializable record"})
        with self._lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read_all(self) -> str:
        with self._lock:
            if not self.log_path.exists():
                return ""
            return self.log_path.read_text(encoding="utf-8")


def write_save_dump(log_dir: str, save_id: str, state: dict) -> Path:
    """Write a save's full loaded state as pretty JSON into the logs
    directory. Returns the path of the file written."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", save_id) or "save"
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"save_dump_{safe_id}_{timestamp}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    return path
