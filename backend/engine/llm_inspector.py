import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class LLMCallRecord:
    id: str
    timestamp: float
    call_type: str
    model: str
    step: str
    module_source: str
    streaming: bool
    status: str = "running"  # running | complete | error | cancelled
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    input_summary: str = ""
    output_summary: str = ""
    full_input: Any = None
    full_output: str = ""
    error: str = ""


CALL_TYPE_LABELS: dict[str, str] = {
    "storyteller": "Storyteller",
    "reader": "Reader",
    "embedding": "Embedding",
    "librarian": "Librarian",
    "world_build": "World Build",
    "character_build": "Character Build",
    "module_fast": "Module LLM",
    "diagnostic": "Diagnostic",
}


class LLMInspector:
    def __init__(self, max_records: int = 200):
        self._calls: deque[LLMCallRecord] = deque(maxlen=max_records)
        self._ws_broadcast: Optional[Callable] = None
        # In-flight calls awaiting completion, keyed by call id. The record
        # object is shared with self._calls so updates are reflected in both.
        self._records: dict[str, LLMCallRecord] = {}

    def set_ws_broadcast(self, fn):
        self._ws_broadcast = fn

    async def _broadcast(self, record: LLMCallRecord):
        if self._ws_broadcast:
            try:
                if asyncio.iscoroutinefunction(self._ws_broadcast):
                    await self._ws_broadcast(record)
                else:
                    self._ws_broadcast(record)
            except Exception:
                pass

    async def start_call(
        self,
        call_type: str,
        model: str,
        step: str,
        module_source: str = "",
        streaming: bool = False,
        input_data: Any = None,
    ) -> str:
        call_id = uuid.uuid4().hex[:8]
        record = LLMCallRecord(
            id=call_id,
            timestamp=time.time(),
            call_type=call_type,
            model=model,
            step=step,
            module_source=module_source,
            streaming=streaming,
            status="running",
            input_summary=self._summarize(input_data, 200),
            full_input=input_data,
        )
        self._records[call_id] = record
        self._calls.append(record)
        await self._broadcast(record)
        return call_id

    async def end_call(
        self,
        call_id: str,
        input_data: Any = None,
        output_data: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        error: str = "",
        cancelled: bool = False,
    ):
        record = self._records.pop(call_id, None)
        if record is None:
            return

        record.duration_ms = max(1, int((time.time() - record.timestamp) * 1000))
        # Input is captured at start_call; only overwrite if provided here.
        if input_data is not None:
            record.full_input = input_data
            record.input_summary = self._summarize(input_data, 200)
        record.full_output = str(output_data) if output_data else ""
        record.output_summary = self._summarize(output_data, 200)
        record.tokens_in = tokens_in
        record.tokens_out = tokens_out
        record.error = error
        record.status = "cancelled" if cancelled else ("error" if error else "complete")

        await self._broadcast(record)

    def get_calls(self, since_id: str = "", limit: int = 50) -> list[dict]:
        calls = list(self._calls)
        calls.sort(key=lambda c: c.timestamp, reverse=True)

        if since_id:
            found = False
            results = []
            for c in calls:
                if not found and c.id == since_id:
                    found = True
                if found:
                    results.append(c)
            calls = results

        return [self._record_to_dict(c) for c in calls[:limit]]

    def get_call(self, call_id: str) -> Optional[dict]:
        for c in self._calls:
            if c.id == call_id:
                return self._record_to_dict(c)
        return None

    def clear(self):
        self._calls.clear()
        self._records.clear()

    def _summarize(self, data: Any, max_chars: int) -> str:
        if data is None:
            return ""
        if isinstance(data, str):
            text = data.strip()
        elif isinstance(data, list):
            text = ""
            for m in data:
                if isinstance(m, dict):
                    role = m.get("role", "")
                    content = str(m.get("content", ""))
                    if len(content) > 150:
                        content = content[:150] + "..."
                    text += f"[{role}] {content}\n"
            text = text.strip()
        elif isinstance(data, dict):
            text = str(data)
        else:
            text = str(data)

        if len(text) > max_chars:
            text = text[:max_chars - 3] + "..."
        return text

    def _record_to_dict(self, r: LLMCallRecord) -> dict:
        return {
            "id": r.id,
            "timestamp": r.timestamp,
            "call_type": r.call_type,
            "call_label": CALL_TYPE_LABELS.get(r.call_type, r.call_type),
            "model": r.model,
            "step": r.step,
            "module_source": r.module_source,
            "streaming": r.streaming,
            "status": r.status,
            "duration_ms": r.duration_ms,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "input_summary": r.input_summary,
            "output_summary": r.output_summary,
            "full_input": r.full_input,
            "full_output": r.full_output,
            "error": r.error,
        }
