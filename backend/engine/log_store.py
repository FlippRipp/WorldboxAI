"""In-memory server log capture for the frontend log viewer.

Nothing else in the codebase configures Python logging (module loggers
propagate to an unconfigured root) and most runtime output is plain
print(). To give the frontend a complete picture, three sources feed one
ring buffer:

  - logging records, via a handler on the root logger (plus the uvicorn
    loggers, which don't propagate to root),
  - print() output, by teeing sys.stdout,
  - tracebacks and other raw stderr writes, by teeing sys.stderr.
"""

import logging
import re
import sys
import threading
import time

from collections import deque

ERROR_LEVELS = {"ERROR", "CRITICAL"}

# Raw stream lines carry no log level; infer one so the errors-only filter
# also catches print()-ed failures and tracebacks.
_ERROR_PAT = re.compile(r"\b(error|errors|exception|traceback|critical|failed|failure)\b", re.IGNORECASE)
_WARNING_PAT = re.compile(r"\b(warn|warning|deprecated|deprecationwarning)\b", re.IGNORECASE)


class LogStore:
    """Thread-safe ring buffer of log records with monotonically increasing
    ids, so clients can poll incrementally with since_id."""

    def __init__(self, max_records: int = 2000):
        self._records: deque[dict] = deque(maxlen=max_records)
        self._lock = threading.Lock()
        self._next_id = 1

    def add(self, level: str, source: str, message: str):
        with self._lock:
            self._records.append({
                "id": self._next_id,
                "timestamp": time.time(),
                "level": level,
                "source": source,
                "message": message,
            })
            self._next_id += 1

    def get_logs(self, since_id: int = 0, level: str = "", limit: int = 1000) -> list[dict]:
        """Oldest-first records newer than since_id. level="error" keeps only
        ERROR/CRITICAL; any other non-empty level matches exactly."""
        with self._lock:
            records = list(self._records)
        if since_id:
            records = [r for r in records if r["id"] > since_id]
        if level == "error":
            records = [r for r in records if r["level"] in ERROR_LEVELS]
        elif level:
            records = [r for r in records if r["level"] == level.upper()]
        return records[-limit:]

    def clear(self):
        # The id counter is not reset: clients polling with since_id must
        # never see an id they already consumed reused for a new record.
        with self._lock:
            self._records.clear()


class LogStoreHandler(logging.Handler):
    def __init__(self, store: LogStore):
        super().__init__(level=logging.INFO)
        self._store = store
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            self._store.add(record.levelname, record.name, self.format(record))
        except Exception:
            pass


class StreamTee:
    """File-like wrapper that writes through to the real stream and mirrors
    complete lines into the LogStore."""

    def __init__(self, stream, store: LogStore, source: str):
        self._stream = stream
        self._store = store
        self._source = source
        self._buf = ""

    def write(self, text):
        try:
            written = self._stream.write(text)
        except Exception:
            written = None
        if isinstance(text, str):
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._store.add(self._classify(line), self._source, line.rstrip())
        return written if written is not None else len(text)

    def _classify(self, line: str) -> str:
        if _WARNING_PAT.search(line) and not _ERROR_PAT.search(line):
            return "WARNING"
        if self._source == "stderr" or _ERROR_PAT.search(line):
            return "ERROR"
        return "INFO"

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_log_capture(store: LogStore) -> bool:
    """Wire the store into logging and stdout/stderr. Idempotent: returns
    False without touching anything if a tee is already installed (e.g. the
    app module gets re-imported in the same process)."""
    if isinstance(sys.stdout, StreamTee) or isinstance(sys.stderr, StreamTee):
        return False

    handler = LogStoreHandler(store)

    root = logging.getLogger()
    root.addHandler(handler)
    if root.getEffectiveLevel() > logging.INFO:
        root.setLevel(logging.INFO)

    # Attaching a root handler disables logging's "lastResort" fallback that
    # used to print WARNING+ records to stderr; keep them visible on the real
    # console. This grabs the pre-tee stream, so nothing is captured twice.
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    root.addHandler(console)

    # uvicorn's loggers don't propagate to root, and their own handlers hold
    # the stderr/stdout objects that existed before this module loaded (so
    # the tees below never see them) — hook them directly. "uvicorn.error"
    # is deliberately absent: it propagates to "uvicorn", so hooking both
    # would record every entry twice.
    for name in ("uvicorn", "uvicorn.access"):
        logging.getLogger(name).addHandler(handler)

    sys.stdout = StreamTee(sys.stdout, store, "stdout")
    sys.stderr = StreamTee(sys.stderr, store, "stderr")
    return True
