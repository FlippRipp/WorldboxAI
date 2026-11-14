"""FastAPI router and ASGI network-boundary middleware for the Secure Debug Bridge."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.engine.debug_bridge import (
    BridgeError,
    DebugBridgeManager,
    DebugBridgeUDPProtocol,
    is_loopback_host,
)


class PairInitRequest(BaseModel):
    client_id: str
    client_name: str = "Antigravity Agent"
    client_pub_b64: str


class PairFinishRequest(BaseModel):
    pairing_id: str
    client_mac_b64: str


class SessionRequestBody(BaseModel):
    device_id: str
    client_name: str = "Antigravity Agent"
    client_eph_pub_b64: str
    requested_write: bool = False
    timestamp: float
    nonce: str
    signature_b64: str


class SessionPollBody(BaseModel):
    device_id: str
    request_id: str
    wait_seconds: float = 30.0
    timestamp: float
    nonce: str
    signature_b64: str


class EncryptedRpcBody(BaseModel):
    session_id: str
    seq: int
    ciphertext_b64: str


class SessionDecisionBody(BaseModel):
    request_id: str
    approve: bool
    allow_write: bool = False


class WriteModeBody(BaseModel):
    allow_write: bool


class ClientTelemetryBody(BaseModel):
    errors: Optional[list[dict[str, Any]]] = None
    ui_state: Optional[dict[str, Any]] = None


class LocalOnlyExceptBridgeMiddleware:
    """Outermost ASGI firewall.

    - Loopback peers (`127.0.0.1`, `::1`, `testclient`) have normal access.
    - Non-loopback peers are *completely blocked* (`403` / WS close `1008`) from
      every route except `/api/debug-bridge/lan/*` (unless `WB_LAN_UI=1` is
      explicitly set, though `/api/debug-bridge/local/*` remains loopback-only
      unconditionally so a LAN client can never self-approve a pairing/session).
    - `/api/debug-bridge/lan/*` additionally requires:
        1. Real TCP peer IP in the exact same local RFC 1918 subnet.
        2. Custom header `X-WB-Bridge: 1`.
        3. Absence of browser `Origin` / `Sec-Fetch-Site` headers (blocks DNS
           rebinding and cross-origin browser requests).
    """

    def __init__(self, app: Any, bridge: DebugBridgeManager) -> None:
        self.app = app
        self.bridge = bridge

    async def _reject_http(self, send: Callable, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        peer_ip = client[0] if isinstance(client, (tuple, list)) and client else None
        server = scope.get("server")
        server_ip = server[0] if isinstance(server, (tuple, list)) and server else None
        path = scope.get("path") or ""

        if is_loopback_host(peer_ip):
            await self.app(scope, receive, send)
            return

        # Loopback control surface is NEVER reachable from non-loopback, even if WB_LAN_UI=1
        if path.startswith("/api/debug-bridge/local"):
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self._reject_http(send, 403, "Loopback access only.")
            return

        if path.startswith("/api/debug-bridge/lan/"):
            if scope_type != "http":
                await send({"type": "websocket.close", "code": 1008})
                return
            headers = {
                k.decode("latin1").lower(): v.decode("latin1")
                for k, v in (scope.get("headers") or [])
            }
            if "origin" in headers or "sec-fetch-site" in headers:
                await self._reject_http(send, 403, "Browser cross-origin requests are not allowed on the debug bridge.")
                return
            if headers.get("x-wb-bridge") != "1":
                await self._reject_http(send, 403, "Missing X-WB-Bridge header.")
                return
            if not self.bridge.subnet_verifier.is_same_subnet(peer_ip, server_ip):
                await self._reject_http(send, 403, "Connection allowed only from the same local private subnet.")
                return
            await self.app(scope, receive, send)
            return

        # All other HTTP / WebSocket routes are local-only unless WB_LAN_UI=1
        if os.environ.get("WB_LAN_UI") == "1":
            await self.app(scope, receive, send)
            return

        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._reject_http(send, 403, "WorldBox API is restricted to local loopback connections.")


def create_debug_bridge_router(
    *,
    bridge: DebugBridgeManager,
    engine: Any,
    registry: Any,
    session_manager: Any,
    provider_manager: Any,
    log_store: Any,
    llm_inspector: Any,
    llm_call_log: Any,
    lorebook_store: Any,
    chat_hub: Any,
    enforce_settings_locks: Callable[[dict], None],
    character_update_fields: Any,
) -> APIRouter:
    @asynccontextmanager
    async def _bridge_lifespan(_app: Any):
        transport = None
        if os.environ.get("WB_BRIDGE_UDP", "1") != "0":
            try:
                port = int(os.environ.get("WB_PORT", "8321"))
                loop = asyncio.get_running_loop()
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: DebugBridgeUDPProtocol(bridge, tcp_port=port),
                    local_addr=("0.0.0.0", port),
                )
            except OSError:
                transport = None
        try:
            yield
        finally:
            if transport is not None:
                transport.close()

    router = APIRouter(prefix="/api/debug-bridge", tags=["debug-bridge"], lifespan=_bridge_lifespan)

    def require_loopback(request: Request) -> str:
        peer = request.client.host if request.client else None
        if not is_loopback_host(peer):
            raise HTTPException(status_code=403, detail="Loopback access only.")
        return peer or "127.0.0.1"

    def require_same_subnet(request: Request) -> str:
        peer = request.client.host if request.client else None
        srv = (request.scope.get("server") or (None,))[0]
        if request.headers.get("origin") or request.headers.get("sec-fetch-site"):
            raise HTTPException(status_code=403, detail="Browser requests are forbidden on LAN bridge routes.")
        if request.headers.get("x-wb-bridge") != "1":
            raise HTTPException(status_code=403, detail="Missing X-WB-Bridge header.")
        # Allow in-process loopback tests ONLY when explicitly opted in via header for local dev testing;
        # otherwise strictly require same private LAN subnet.
        if is_loopback_host(peer) and os.environ.get("WB_BRIDGE_ALLOW_LOOPBACK_TEST") == "1":
            return peer or "127.0.0.1"
        if not bridge.subnet_verifier.is_same_subnet(peer, srv):
            raise HTTPException(status_code=403, detail="Connection allowed only from the same local private subnet.")
        return peer or ""

    def _raise_http(exc: BridgeError) -> None:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    def _require_writable(allow_write: bool) -> None:
        if not allow_write:
            raise BridgeError(
                403,
                "This diagnostic session is currently Read-Only. Toggle 'Allow making changes' on the WorldBox badge/prompt on your device first.",
                "write_not_allowed",
            )
        if chat_hub.turn_running():
            raise BridgeError(
                409,
                "A story turn is currently generating. Wait for it to finish (or stop it on the device) before modifying state.",
                "turn_running",
            )

    async def _persist_and_broadcast_active_state() -> None:
        save_id = session_manager.active_save_id
        state = session_manager.state
        if save_id:
            session_manager.save_manager.save_turn(save_id, state, state.get("turn", 0))
        await chat_hub.send({"type": "state_update", "state": dict(state)})

    # -----------------------------------------------------------------------
    # RPC Method Implementations (Read & Write)
    # -----------------------------------------------------------------------

    async def _rpc_get_overview(_params: dict[str, Any]) -> dict[str, Any]:
        modules_summary = []
        active_cfg = (session_manager.state.get("module_configs") or {}).get("__active_modules__")
        active_set = set(active_cfg) if isinstance(active_cfg, list) else None
        for mod_id, mod_data in registry.get_modules().items():
            manifest = mod_data["manifest"]
            modules_summary.append({
                "id": mod_id,
                "name": manifest.get("name", mod_id),
                "version": manifest.get("version", "unknown"),
                "active_in_story": True if active_set is None else (mod_id in active_set),
                "commands": list((manifest.get("commands") or {}).keys()),
                "produces_module_data": bool((manifest.get("produces") or {}).get("module_data")),
                "ui_slots": manifest.get("ui_slots", []),
            })

        providers_status = {
            p["id"]: {
                "active": p["active"],
                "api_key_set": provider_manager.has_api_key(p["id"]),
            }
            for p in provider_manager.get_all()
        }

        memory_info: dict[str, Any] = {"initialized": False}
        if engine.memory is not None:
            try:
                memory_info = {
                    "initialized": True,
                    "rows": engine.memory.get_memory_count(),
                    "vector_dimension": engine.memory.get_vector_dimension(),
                }
            except Exception as exc:
                memory_info = {"initialized": True, "error": str(exc)}

        telemetry = bridge.get_client_telemetry()
        state = session_manager.state or {}
        return {
            "server_name": bridge.server_name,
            "session": {
                **session_manager.get_status(),
                "turn_running": chat_hub.turn_running(),
                "turn_action": chat_hub.turn_action if chat_hub.turn_running() else None,
                "player_location_node_id": state.get("player_location_node_id"),
                "player_location_map_id": state.get("player_location_map_id"),
                "message_count": len(state.get("chat_messages") or []),
                "module_data_keys": list((state.get("module_data") or {}).keys()),
            },
            "client_ui": telemetry["ui_state"],
            "client_error_count": len(telemetry["client_errors"]),
            "llm": {
                "mode": engine.llm.mode,
                "active_provider": provider_manager.get_active(),
                "storyteller_model": engine.llm.storyteller_model,
                "storyteller_fallback_models": engine.llm.storyteller_fallback_models,
                "reader_model": engine.llm.reader_model,
                "embedding_model": engine.llm.embedding_model,
                "module_fast_model": engine.llm.module_fast_model,
            },
            "providers": providers_status,
            "modules": modules_summary,
            "memory": memory_info,
        }

    async def _rpc_get_logs(params: dict[str, Any]) -> dict[str, Any]:
        since_id = int(params.get("since_id") or 0)
        client_since_id = int(params.get("client_since_id") or 0)
        level = str(params.get("level") or "")
        limit = max(1, min(int(params.get("limit") or 200), 2000))
        server_logs = log_store.get_logs(since_id=since_id, level=level, limit=limit)
        telemetry = bridge.get_client_telemetry(since_id=client_since_id)
        return {
            "server_logs": server_logs,
            "client_errors": telemetry["client_errors"],
            "client_ui": telemetry["ui_state"],
        }

    def _matches_llm_call(
        rec: dict[str, Any],
        call_id: Optional[str],
        module_filter: str,
        errors_only: bool,
    ) -> bool:
        if call_id and rec.get("id") != call_id:
            return False
        if module_filter:
            target = module_filter.lower()
            matched = False
            for field in ("module_source", "call_type", "step", "module", "caller"):
                val = str(rec.get(field) or "").lower()
                if val and (val == target or target in val.split(":")):
                    matched = True
                    break
            if not matched:
                return False
        if errors_only and not (rec.get("error") or rec.get("status") == "error"):
            return False
        return True

    def _tail_persistent_llm_calls(
        call_id: Optional[str],
        module_filter: str,
        errors_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Read matching records newest-first from llm_calls.jsonl by scanning
        64 KiB chunks backwards from EOF instead of loading the whole file."""
        log_path = getattr(llm_call_log, "log_path", None)
        lock = getattr(llm_call_log, "_lock", None)

        def _scan() -> list[dict[str, Any]]:
            if log_path is None or not log_path.exists():
                return []
            results: list[dict[str, Any]] = []
            chunk_size = 65536
            with open(log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                carry = b""
                while pos > 0 and len(results) < limit:
                    read_len = min(chunk_size, pos)
                    pos -= read_len
                    f.seek(pos)
                    data = f.read(read_len) + carry
                    lines = data.split(b"\n")
                    if pos > 0:
                        carry = lines[0]
                        lines = lines[1:]
                    else:
                        carry = b""
                    for raw_line in reversed(lines):
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            rec = json.loads(raw_line.decode("utf-8", errors="replace"))
                        except Exception:
                            continue
                        if _matches_llm_call(rec, call_id, module_filter, errors_only):
                            results.append(rec)
                            if len(results) >= limit:
                                break
            return results

        if lock is not None:
            with lock:
                return _scan()
        return _scan()

    async def _rpc_get_llm_calls(params: dict[str, Any]) -> dict[str, Any]:
        source = str(params.get("source") or "inspector")
        limit = max(1, min(int(params.get("limit") or 20), 200))
        call_id = str(params["call_id"]).strip() if params.get("call_id") else None
        module_filter = str(params.get("module") or "").strip()
        errors_only = bool(params.get("errors_only", False))
        include_messages = bool(params.get("include_messages", False))

        if source == "persistent":
            calls = _tail_persistent_llm_calls(
                call_id=call_id,
                module_filter=module_filter,
                errors_only=errors_only,
                limit=1 if call_id else limit,
            )
        else:
            since_id = str(params.get("since_id") or "")
            ring_capacity = max(limit, len(getattr(llm_inspector, "_calls", ())) or 200)
            raw_calls = llm_inspector.get_calls(since_id=since_id, limit=ring_capacity)
            calls = [
                c for c in raw_calls
                if _matches_llm_call(c, call_id, module_filter, errors_only)
            ][: (1 if call_id else limit)]

        if not include_messages and not call_id:
            # Omit full_input in list mode (input_summary + full_output/error stay
            # intact) so listing 20 calls stays compact; passing call_id or
            # include_messages=True returns the complete untruncated full_input.
            compact = []
            for c in calls:
                item = dict(c)
                if "full_input" in item and item["full_input"] is not None:
                    full_in = item.pop("full_input")
                    item["message_count"] = len(full_in) if isinstance(full_in, list) else 1
                    item["full_input_omitted"] = True
                compact.append(item)
            calls = compact

        return {"calls": calls, "count": len(calls), "source": source}

    async def _rpc_get_session_state(params: dict[str, Any]) -> dict[str, Any]:
        target_save_id = params.get("save_id")
        include_messages = bool(params.get("include_messages", True))
        message_limit = int(params.get("message_limit") or 0)
        include_world_data = bool(params.get("include_world_data", False))

        if target_save_id and target_save_id != session_manager.active_save_id:
            try:
                raw_state = deepcopy(session_manager.save_manager.load_save(str(target_save_id)))
            except FileNotFoundError as exc:
                raise BridgeError(404, str(exc), "save_not_found")
            is_active = False
        else:
            raw_state = deepcopy(session_manager.state or {})
            is_active = True

        msgs = raw_state.get("chat_messages") or []
        if not include_messages:
            raw_state.pop("chat_messages", None)
            raw_state.pop("history", None)
            raw_state["message_count"] = len(msgs)
        elif message_limit > 0 and len(msgs) > message_limit:
            raw_state["chat_messages_total"] = len(msgs)
            raw_state["chat_messages"] = msgs[-message_limit:]
            hist = raw_state.get("history") or []
            raw_state["history"] = hist[-message_limit:]

        if not include_world_data and raw_state.get("world_data") is not None:
            wd = raw_state.pop("world_data")
            raw_state["world_data_summary"] = {
                "present": True,
                "top_level_keys": list(wd.keys()) if isinstance(wd, dict) else type(wd).__name__,
            }

        return {
            "is_active_session": is_active,
            "saves": session_manager.list_saves() if params.get("include_saves_list") else None,
            "state": raw_state,
        }

    async def _rpc_get_module_data(params: dict[str, Any]) -> dict[str, Any]:
        mod_id = str(params.get("mod_id") or "").strip()
        target_save_id = params.get("save_id")
        if target_save_id and target_save_id != session_manager.active_save_id:
            try:
                state = session_manager.save_manager.load_save(str(target_save_id))
            except FileNotFoundError as exc:
                raise BridgeError(404, str(exc), "save_not_found")
        else:
            state = session_manager.state or {}

        all_module_data = state.get("module_data") or {}
        module_configs = state.get("module_configs") or {}
        active_list = module_configs.get("__active_modules__")
        active_set = set(active_list) if isinstance(active_list, list) else None
        instructions_all = module_configs.get("__module_instructions__") or {}

        if not mod_id:
            overview = {}
            for mid, mdata in registry.get_modules().items():
                manifest = mdata["manifest"]
                m_state = all_module_data.get(mid)
                overview[mid] = {
                    "name": manifest.get("name", mid),
                    "active_in_story": True if active_set is None else (mid in active_set),
                    "commands": manifest.get("commands") or {},
                    "has_data": mid in all_module_data,
                    "data_keys": list(m_state.keys()) if isinstance(m_state, dict) else None,
                    "config": module_configs.get(mid) or {},
                    "instruction_overrides": instructions_all.get(mid) or {},
                }
            # Include any orphaned module_data keys present in the save
            for mid, m_state in all_module_data.items():
                if mid not in overview:
                    overview[mid] = {
                        "name": mid,
                        "installed": False,
                        "has_data": True,
                        "data_keys": list(m_state.keys()) if isinstance(m_state, dict) else None,
                    }
            return {
                "active_save_id": target_save_id or session_manager.active_save_id,
                "active_modules": active_list,
                "modules": overview,
            }

        mod_entry = registry.get_modules().get(mod_id)
        if not mod_entry and mod_id not in all_module_data:
            raise BridgeError(404, f"Module '{mod_id}' is not installed and has no data in this save.", "module_not_found")

        manifest = mod_entry["manifest"] if mod_entry else {}
        return {
            "mod_id": mod_id,
            "name": manifest.get("name", mod_id),
            "installed": mod_entry is not None,
            "active_in_story": True if active_set is None else (mod_id in active_set),
            "commands": manifest.get("commands") or {},
            "settings_schema": manifest.get("settings_schema") or {},
            "config": deepcopy(module_configs.get(mod_id) or {}),
            "instruction_overrides": deepcopy(instructions_all.get(mod_id) or {}),
            "module_data": deepcopy(all_module_data.get(mod_id)),
        }

    async def _rpc_get_memories(params: dict[str, Any]) -> dict[str, Any]:
        from backend.engine.memory import MemoryManager

        include_world = bool(params.get("include_world_entries", True))
        limit = max(1, min(int(params.get("limit") or 500), 2000))
        save_id = session_manager.active_save_id
        mem_path = session_manager.get_memory_path() or engine.memory_db_path
        world_index_path = (
            session_manager.data_dir / "saves" / save_id / "world_index"
            if save_id
            else None
        )

        mem = None
        temp_mem = None
        try:
            if engine.memory is not None and (not mem_path or engine.memory_db_path == mem_path):
                mem = engine.memory
            elif mem_path and engine.memory is None and engine.memory_db_path == mem_path:
                try:
                    await engine.ensure_memory()
                    mem = engine.memory
                except Exception:
                    mem = None

            if mem is not None:
                if (
                    include_world
                    and world_index_path is not None
                    and world_index_path.exists()
                    and not mem.has_world_index()
                ):
                    mem.init_world_index(str(world_index_path))
            elif mem_path:
                has_mem_db = os.path.exists(os.path.join(mem_path, "memories.db"))
                has_world_db = bool(world_index_path and (world_index_path / "world.db").exists())
                if has_mem_db or has_world_db:
                    temp_mem = MemoryManager(mem_path, embedding_dim=1)
                    if include_world and world_index_path is not None and world_index_path.exists():
                        temp_mem.init_world_index(str(world_index_path))
                    mem = temp_mem

            state = session_manager.state or {}
            if mem is None:
                return {
                    "initialized": False,
                    "memories": [],
                    "count": 0,
                    "active_memory_ids": state.get("last_retrieved_memory_ids", []),
                    "world_entries": [],
                    "world_entry_count": 0,
                    "active_world_ids": state.get("last_retrieved_world_ids", []),
                    "sticky_source_ids": state.get("sticky_world_entries", {}) or {},
                    "context_query": state.get("last_context_query", ""),
                }

            memories = mem.list_all_memories(limit=limit)
            world_entries = (
                mem.list_world_entries()
                if (include_world and mem.has_world_index())
                else []
            )
            return {
                "initialized": True,
                "memories": memories,
                "count": mem.get_memory_count(),
                "active_memory_ids": state.get("last_retrieved_memory_ids", []),
                "world_entries": world_entries,
                "world_entry_count": len(world_entries),
                "active_world_ids": state.get("last_retrieved_world_ids", []),
                "sticky_source_ids": state.get("sticky_world_entries", {}) or {},
                "context_query": state.get("last_context_query", ""),
            }
        finally:
            if temp_mem is not None:
                temp_mem.close()

    async def _rpc_get_settings_and_prompts(params: dict[str, Any]) -> dict[str, Any]:
        save_id = session_manager.active_save_id
        lorebooks = None
        story_style = None
        if save_id:
            meta = session_manager.save_manager.read_core_json(save_id, "metadata.json", None)
            if isinstance(meta, dict):
                lorebooks = {
                    "lorebook_ids": meta.get("lorebook_ids", []),
                    "story_entries": meta.get("story_lorebook_entries", []) or [],
                }
            try:
                story_style = session_manager.get_story_style(save_id)
            except Exception:
                story_style = (session_manager.state or {}).get("story_style") or {}
        state = session_manager.state or {}
        return {
            "story_settings": session_manager.get_settings_descriptors(scope="story"),
            "global_settings": session_manager.get_settings_descriptors(scope="global"),
            "prompt_pipeline": state.get("prompt_pipeline", []),
            "continue_prompt": state.get("continue_prompt", ""),
            "story_style": story_style,
            "last_prompt_trace": state.get("last_prompt_trace", []),
            "story_lorebooks": lorebooks,
            "all_lorebooks": lorebook_store.list_lorebooks() if params.get("include_all_lorebooks") else None,
        }

    # -- Write RPCs -----------------------------------------------------------

    async def _rpc_update_module_data(params: dict[str, Any], allow_write: bool) -> dict[str, Any]:
        _require_writable(allow_write)
        if not session_manager.active_save_id:
            raise BridgeError(409, "No active story save is loaded.", "no_active_save")

        mod_id = str(params.get("mod_id") or "").strip()
        if not mod_id:
            raise BridgeError(400, "mod_id is required.", "missing_mod_id")

        data = params.get("data")
        if not isinstance(data, dict):
            raise BridgeError(400, "data must be a JSON object.", "invalid_data")

        mode = str(params.get("mode") or "merge").lower()
        if mode not in ("merge", "replace"):
            raise BridgeError(400, "mode must be 'merge' or 'replace'.", "invalid_mode")

        replace_keys = params.get("replace_keys") or []
        delete_keys = params.get("delete_keys") or []

        state = session_manager.state
        module_data = state.setdefault("module_data", {})
        current = deepcopy(module_data.get(mod_id) or {})

        if mode == "replace":
            updated = deepcopy(data)
        else:
            updated = engine._deep_merge(current, deepcopy(data))
            for rk in replace_keys:
                if rk in data:
                    updated[rk] = deepcopy(data[rk])
            for dk in delete_keys:
                updated.pop(dk, None)

        module_data[mod_id] = updated
        await _persist_and_broadcast_active_state()
        return {
            "updated": True,
            "mod_id": mod_id,
            "mode": mode,
            "module_data": deepcopy(updated),
        }

    async def _rpc_run_module_command(params: dict[str, Any], allow_write: bool) -> dict[str, Any]:
        _require_writable(allow_write)
        if not session_manager.active_save_id:
            raise BridgeError(409, "No active story save is loaded.", "no_active_save")

        raw_cmd = str(params.get("command") or "").strip()
        if not raw_cmd.startswith("/"):
            raw_cmd = "/" + raw_cmd
        parts = raw_cmd.split()
        if not parts:
            raise BridgeError(400, "command is required.", "missing_command")

        cmd_name = parts[0].lower()
        args = parts[1:]
        state = session_manager.state
        active = (state.get("module_configs") or {}).get("__active_modules__")
        active_set = set(active) if isinstance(active, list) else None

        matched_mod_id = None
        matched_mod = None
        handler_name = None
        for mid, mdata in registry.get_modules().items():
            if active_set is not None and mid not in active_set:
                continue
            manifest = mdata["manifest"]
            hname = (manifest.get("commands") or {}).get(cmd_name)
            if hname:
                matched_mod_id = mid
                matched_mod = mdata
                handler_name = hname
                break

        if not matched_mod or not matched_mod_id or not handler_name:
            raise BridgeError(404, f"No active module handles command '{cmd_name}'.", "unknown_command")

        manifest = matched_mod["manifest"]
        handler = getattr(matched_mod["backend"], handler_name, None)
        if handler is None:
            raise BridgeError(500, f"Handler '{handler_name}' missing on module '{matched_mod_id}'.", "missing_handler")

        module_state = engine._build_module_state(state, matched_mod_id, manifest.get("consumes", {}))
        engine.sdk.llm._current_module = matched_mod_id
        try:
            result = await handler(args, module_state, engine.sdk)
        finally:
            engine.sdk.llm._current_module = ""

        is_error = isinstance(result, dict) and bool(result.get("error"))
        message = result.get("message", "") if isinstance(result, dict) else ""

        if isinstance(result, dict):
            revealed = result.get("revealed_node_ids")
            if isinstance(revealed, list):
                state["revealed_node_ids"] = revealed
            node_id = result.get("player_location_node_id")
            if isinstance(node_id, str) and node_id:
                state["player_location_node_id"] = node_id
                for key in ("player_location_map_id", "player_location_region"):
                    if isinstance(result.get(key), str) and result[key]:
                        state[key] = result[key]
            update = result.get("character_update")
            if isinstance(update, dict):
                player = (state.get("characters") or {}).get("default_player")
                if isinstance(player, dict):
                    applied = {k: update[k] for k in character_update_fields if update.get(k)}
                    if applied:
                        player.update(applied)
            own_data = (result.get("module_data") or {}).get(matched_mod_id)
            if isinstance(own_data, dict) and (manifest.get("produces") or {}).get("module_data"):
                mdata_root = state.setdefault("module_data", {})
                merged = engine._deep_merge(mdata_root.get(matched_mod_id) or {}, own_data)
                for key in result.get("module_data_replace") or []:
                    if key in own_data:
                        merged[key] = own_data[key]
                mdata_root[matched_mod_id] = merged

        await _persist_and_broadcast_active_state()
        return {
            "command": raw_cmd,
            "mod_id": matched_mod_id,
            "message": message,
            "error": is_error,
            "module_data": deepcopy((state.get("module_data") or {}).get(matched_mod_id)),
        }

    async def _rpc_update_module_configs(params: dict[str, Any], allow_write: bool) -> dict[str, Any]:
        _require_writable(allow_write)
        configs = params.get("module_configs")
        if not isinstance(configs, dict):
            raise BridgeError(400, "module_configs must be an object.", "invalid_configs")
        try:
            enforce_settings_locks(configs)
            updated_state = session_manager.update_module_configs(configs)
        except HTTPException as exc:
            raise BridgeError(exc.status_code, str(exc.detail), "locked")
        except ValueError as exc:
            raise BridgeError(400, str(exc), "invalid_configs")
        await chat_hub.send({"type": "state_update", "state": dict(updated_state)})
        return {
            "updated": True,
            "module_configs": deepcopy(updated_state.get("module_configs") or {}),
        }

    async def _rpc_update_story_state(params: dict[str, Any], allow_write: bool) -> dict[str, Any]:
        _require_writable(allow_write)
        if not session_manager.active_save_id:
            raise BridgeError(409, "No active story save is loaded.", "no_active_save")

        action = str(params.get("action") or "patch")
        state = session_manager.state

        if action == "undo_turn":
            target_turn = int(params["target_turn"])
            new_state = session_manager.undo_turn(target_turn)
            engine.set_memory_path(session_manager.get_memory_path())
            engine.rollback_memory(target_turn)
            new_state["swipes"] = session_manager.swipes_meta()
            await chat_hub.send({"type": "done", "state": dict(new_state)})
            return {"undone_to_turn": new_state.get("turn"), "session": session_manager.get_status()}

        if action == "edit_message":
            idx = int(params["index"])
            content = str(params["content"])
            new_state = session_manager.edit_message(idx, content)
            new_state["swipes"] = session_manager.swipes_meta()
            await chat_hub.send({"type": "done", "state": dict(new_state)})
            return {"edited_index": idx, "turn": new_state.get("turn")}

        # Default action == "patch": patch characters / location / revealed_node_ids
        char_patches = params.get("characters")
        if isinstance(char_patches, dict):
            chars = state.setdefault("characters", {})
            for cid, cpatch in char_patches.items():
                if isinstance(cpatch, dict):
                    chars[cid] = engine._deep_merge(chars.get(cid) or {}, deepcopy(cpatch))

        for loc_key in ("player_location_node_id", "player_location_map_id", "player_location_layer_id", "player_location_region"):
            if loc_key in params:
                state[loc_key] = params[loc_key]

        if "revealed_node_ids" in params and isinstance(params["revealed_node_ids"], list):
            state["revealed_node_ids"] = list(params["revealed_node_ids"])

        await _persist_and_broadcast_active_state()
        return {
            "patched": True,
            "characters": deepcopy(state.get("characters") or {}),
            "player_location_node_id": state.get("player_location_node_id"),
            "player_location_map_id": state.get("player_location_map_id"),
        }

    async def _rpc_update_settings_or_prompts(params: dict[str, Any], allow_write: bool) -> dict[str, Any]:
        _require_writable(allow_write)
        result: dict[str, Any] = {}
        if isinstance(params.get("settings"), dict):
            scope = str(params.get("scope") or "story")
            result["settings"] = await session_manager.update_settings(params["settings"], scope=scope)
        if isinstance(params.get("prompt_pipeline"), list):
            st = session_manager.update_prompt_pipeline(params["prompt_pipeline"])
            result["prompt_pipeline"] = st.get("prompt_pipeline", [])
        return {"updated": True, **result}

    async def dispatch_rpc(method: str, params: dict[str, Any], allow_write: bool) -> Any:
        if method == "ping":
            return {"pong": True, "server_name": bridge.server_name, "allow_write": allow_write}
        if method == "get_overview":
            return await _rpc_get_overview(params)
        if method == "get_logs":
            return await _rpc_get_logs(params)
        if method == "get_llm_calls":
            return await _rpc_get_llm_calls(params)
        if method == "get_session_state":
            return await _rpc_get_session_state(params)
        if method == "get_module_data":
            return await _rpc_get_module_data(params)
        if method == "get_memories":
            return await _rpc_get_memories(params)
        if method == "get_settings_and_prompts":
            return await _rpc_get_settings_and_prompts(params)
        if method == "update_module_data":
            return await _rpc_update_module_data(params, allow_write)
        if method == "run_module_command":
            return await _rpc_run_module_command(params, allow_write)
        if method == "update_module_configs":
            return await _rpc_update_module_configs(params, allow_write)
        if method == "update_story_state":
            return await _rpc_update_story_state(params, allow_write)
        if method == "update_settings_or_prompts":
            return await _rpc_update_settings_or_prompts(params, allow_write)
        raise BridgeError(404, f"Unknown bridge method '{method}'.", "unknown_method")

    # -----------------------------------------------------------------------
    # Loopback-Only Routes (WorldBox Mobile UI)
    # -----------------------------------------------------------------------

    @router.get("/local/status")
    async def local_status(_peer: str = Depends(require_loopback)):
        return bridge.get_ui_status(include_pair_code=True)

    @router.post("/local/pair/start")
    async def local_pair_start(_peer: str = Depends(require_loopback)):
        return bridge.start_pairing()

    @router.post("/local/pair/cancel")
    async def local_pair_cancel(_peer: str = Depends(require_loopback)):
        return bridge.cancel_pairing()

    @router.post("/local/unpair")
    async def local_unpair(_peer: str = Depends(require_loopback)):
        return bridge.unpair()

    @router.post("/local/session/decide")
    async def local_decide_session(body: SessionDecisionBody, _peer: str = Depends(require_loopback)):
        try:
            return bridge.decide_pending_session(
                request_id=body.request_id,
                approve=body.approve,
                allow_write=body.allow_write,
            )
        except BridgeError as exc:
            _raise_http(exc)

    @router.put("/local/session/write-mode")
    async def local_set_write_mode(body: WriteModeBody, _peer: str = Depends(require_loopback)):
        try:
            return bridge.set_write_mode(body.allow_write)
        except BridgeError as exc:
            _raise_http(exc)

    @router.post("/local/session/disconnect")
    async def local_disconnect(_peer: str = Depends(require_loopback)):
        return bridge.disconnect_session()

    @router.post("/local/client-telemetry")
    async def local_client_telemetry(body: ClientTelemetryBody, _peer: str = Depends(require_loopback)):
        return bridge.record_client_telemetry(errors=body.errors, ui_state=body.ui_state)

    # -----------------------------------------------------------------------
    # Same-Subnet LAN Routes (Paired Agent Only)
    # -----------------------------------------------------------------------

    @router.post("/lan/pair/init")
    async def lan_pair_init(body: PairInitRequest, peer_ip: str = Depends(require_same_subnet)):
        try:
            return bridge.lan_pair_init(
                client_id=body.client_id,
                client_name=body.client_name,
                client_pub_b64=body.client_pub_b64,
                peer_ip=peer_ip,
            )
        except BridgeError as exc:
            _raise_http(exc)

    @router.post("/lan/pair/finish")
    async def lan_pair_finish(body: PairFinishRequest, _peer_ip: str = Depends(require_same_subnet)):
        try:
            return bridge.lan_pair_finish(
                pairing_id=body.pairing_id,
                client_mac_b64=body.client_mac_b64,
            )
        except BridgeError as exc:
            _raise_http(exc)

    @router.post("/lan/session/request")
    async def lan_session_request(body: SessionRequestBody, peer_ip: str = Depends(require_same_subnet)):
        try:
            return bridge.lan_request_session(
                device_id=body.device_id,
                client_name=body.client_name,
                client_eph_pub_b64=body.client_eph_pub_b64,
                requested_write=body.requested_write,
                timestamp=body.timestamp,
                nonce=body.nonce,
                signature_b64=body.signature_b64,
                peer_ip=peer_ip,
            )
        except BridgeError as exc:
            _raise_http(exc)

    @router.post("/lan/session/poll")
    async def lan_session_poll(body: SessionPollBody, _peer_ip: str = Depends(require_same_subnet)):
        try:
            return await bridge.lan_poll_session(
                device_id=body.device_id,
                request_id=body.request_id,
                wait_seconds=body.wait_seconds,
                timestamp=body.timestamp,
                nonce=body.nonce,
                signature_b64=body.signature_b64,
            )
        except BridgeError as exc:
            _raise_http(exc)

    @router.post("/lan/rpc")
    async def lan_rpc(body: EncryptedRpcBody, peer_ip: str = Depends(require_same_subnet)):
        try:
            return await bridge.lan_handle_encrypted_rpc(
                session_id=body.session_id,
                seq=body.seq,
                ciphertext_b64=body.ciphertext_b64,
                peer_ip=peer_ip,
                dispatcher=dispatch_rpc,
            )
        except BridgeError as exc:
            _raise_http(exc)

    return router
