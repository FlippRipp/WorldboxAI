"""Tests for the Secure LAN Diagnostic Bridge, ASGI network boundary,
cryptographic pairing/session protocol, Plot Director module interaction,
and stdio MCP server."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api.routers.debug_bridge import (
    LocalOnlyExceptBridgeMiddleware,
    create_debug_bridge_router,
)
from backend.engine.debug_bridge import (
    DebugBridgeManager,
    SubnetVerifier,
    b64d,
    b64e,
    chacha20_poly1305_open,
    chacha20_poly1305_seal,
    make_rpc_nonce,
    scrub_secrets,
    sign_bridge_message,
    x25519_keypair,
    x25519_scalar_mult,
    x25519_shared_secret,
)
from backend.engine.llm_call_log import LLMCallLog
from backend.engine.llm_inspector import LLMInspector
from backend.engine.lorebook import LorebookStore
from backend.engine.memory import MemoryManager, _serialize
from backend.engine.session import GameSessionManager
from backend.engine.settings_registry import SettingsRegistry
import backend.api.server as wb_server
from tools.mobile_bridge_mcp import (
    BRIDGE_HEADERS,
    TOOLS_SPEC,
    WorldBoxBridgeClient,
    ClientBridgeError,
    _read_mcp_stream,
    _write_mcp_response,
    handle_jsonrpc_message,
)


# ---------------------------------------------------------------------------
# 1. Cryptographic Primitives & Secret Scrubbing
# ---------------------------------------------------------------------------

def test_x25519_rfc7748_vector_and_low_order_rejection():
    alice_priv = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    bob_pub = bytes.fromhex("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
    expected = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
    assert x25519_scalar_mult(alice_priv, bob_pub).hex() == expected

    # All-zero public point produces all-zero shared secret -> must be rejected
    with pytest.raises(ValueError, match="low-order"):
        x25519_shared_secret(alice_priv, b"\x00" * 32)


def test_chacha20_poly1305_rfc8439_vector_and_tamper_rejection():
    key = bytes.fromhex("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
    nonce = bytes.fromhex("070000004041424344454647")
    aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
    pt = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it."

    sealed = chacha20_poly1305_seal(key, nonce, pt, aad)
    assert sealed[-16:].hex() == "1ae10b594f09e26a7e902ecbd0600691"
    assert chacha20_poly1305_open(key, nonce, sealed, aad) == pt

    # Tampered ciphertext byte
    tampered = bytearray(sealed)
    tampered[5] ^= 0x01
    with pytest.raises(ValueError, match="AEAD authentication failed"):
        chacha20_poly1305_open(key, nonce, bytes(tampered), aad)

    # Tampered AAD
    with pytest.raises(ValueError, match="AEAD authentication failed"):
        chacha20_poly1305_open(key, nonce, sealed, aad + b"x")


def test_secret_scrubber_strips_keys_but_preserves_llm_token_counts():
    raw = {
        "provider": {
            "api_key": "sk-secret-value-12345678901234567890",
            "client_secret": "top-secret",
            "empty_secret": "",
        },
        "leaked_inline": "AIzaSyA123456789012345678901234567890",
        "llm_usage": {
            "prompt_tokens": 1250,
            "completion_tokens": 340,
            "max_tokens": 4096,
            "tokens": 1590,
        },
        "story_text": "The rogue whispered the password to the gatekeeper.",
    }
    cleaned = scrub_secrets(raw)
    assert cleaned["provider"]["api_key"] == "[REDACTED]"
    assert cleaned["provider"]["client_secret"] == "[REDACTED]"
    assert cleaned["provider"]["empty_secret"] == ""
    assert cleaned["leaked_inline"] == "[REDACTED]"
    assert cleaned["llm_usage"] == {
        "prompt_tokens": 1250,
        "completion_tokens": 340,
        "max_tokens": 4096,
        "tokens": 1590,
    }
    assert cleaned["story_text"] == raw["story_text"]


# ---------------------------------------------------------------------------
# 2. Isolated Harness for Subnet Firewall + Handshake Security
# ---------------------------------------------------------------------------

def _build_isolated_app(
    tmp_path: Path,
    allowed_subnets: list[str] | None = None,
    return_deps: bool = False,
):
    verifier = SubnetVerifier(override_subnets=allowed_subnets or ["192.168.1.0/24"])
    bridge = DebugBridgeManager(
        storage_dir=tmp_path / "server_bridge",
        server_name="Pixel-Termux",
        subnet_verifier=verifier,
    )
    isolated_settings = SettingsRegistry()
    isolated_settings.bind_global(str(tmp_path / "wb_data" / "global_engine_settings.json"))
    isolated_settings.register("memory.rag_limit", "slider", 3, label="RAG Limit", category="Memory", min=1, max=10)
    isolated_settings.register("cheats.enabled", "toggle", False, label="Cheats", category="Cheats", is_global=True)

    isolated_sm = GameSessionManager(str(tmp_path / "wb_data"), settings=isolated_settings)
    isolated_sm.create_save("save1")

    isolated_inspector = LLMInspector()
    isolated_call_log = LLMCallLog(str(tmp_path / "wb_data" / "logs"))
    isolated_inspector.set_call_logger(isolated_call_log)
    isolated_lorebooks = LorebookStore(str(tmp_path / "wb_data"))

    app = FastAPI()

    @app.get("/api/health")
    async def dummy_health():
        return {"ok": True}

    @app.get("/api/providers/gemini/config")
    async def dummy_provider():
        return {"api_key": "REAL_KEY_SHOULD_BE_UNREACHABLE"}

    router = create_debug_bridge_router(
        bridge=bridge,
        engine=wb_server.engine,
        registry=wb_server.registry,
        session_manager=isolated_sm,
        provider_manager=wb_server.provider_manager,
        log_store=wb_server.log_store,
        llm_inspector=isolated_inspector,
        llm_call_log=isolated_call_log,
        lorebook_store=isolated_lorebooks,
        chat_hub=wb_server.chat_hub,
        enforce_settings_locks=wb_server._enforce_settings_locks,
        character_update_fields=wb_server.CHARACTER_UPDATE_FIELDS,
    )
    app.include_router(router)
    app.add_middleware(LocalOnlyExceptBridgeMiddleware, bridge=bridge)
    if return_deps:
        return app, bridge, isolated_sm, isolated_inspector, isolated_call_log, isolated_lorebooks
    return app, bridge, isolated_sm


def test_network_boundary_blocks_non_loopback_and_wrong_subnets(tmp_path: Path):
    app, _bridge, _sm = _build_isolated_app(tmp_path, allowed_subnets=["192.168.1.0/24"])

    local_client = TestClient(app, client=("127.0.0.1", 51000))
    same_lan_client = TestClient(app, client=("192.168.1.42", 51001))
    other_subnet_client = TestClient(app, client=("192.168.2.42", 51002))
    cgnat_client = TestClient(app, client=("100.64.0.10", 51003))
    public_client = TestClient(app, client=("8.8.8.8", 51004))

    # Loopback accesses normal API and local bridge controls normally
    assert local_client.get("/api/health").status_code == 200
    assert local_client.get("/api/debug-bridge/local/status").status_code == 200

    # Same-subnet LAN peer CANNOT access normal /api/* or /api/debug-bridge/local/*
    assert same_lan_client.get("/api/health").status_code == 403
    assert same_lan_client.get("/api/providers/gemini/config").status_code == 403
    assert same_lan_client.get("/api/debug-bridge/local/status").status_code == 403
    assert same_lan_client.post(
        "/api/debug-bridge/local/session/decide",
        json={"request_id": "x", "approve": True, "allow_write": True},
    ).status_code == 403

    # Browser Origin / missing X-WB-Bridge header rejected on LAN bridge routes
    assert same_lan_client.post(
        "/api/debug-bridge/lan/pair/init",
        headers={"X-WB-Bridge": "1", "Origin": "http://evil.example"},
        json={"client_id": "c", "client_pub_b64": b64e(b"\x01" * 32)},
    ).status_code == 403
    assert same_lan_client.post(
        "/api/debug-bridge/lan/pair/init",
        json={"client_id": "c", "client_pub_b64": b64e(b"\x01" * 32)},
    ).status_code == 403

    # Different private subnet, CGNAT, and public IPs are blocked even with X-WB-Bridge
    for bad_client in (other_subnet_client, cgnat_client, public_client):
        res = bad_client.post(
            "/api/debug-bridge/lan/pair/init",
            headers=BRIDGE_HEADERS,
            json={"client_id": "c", "client_pub_b64": b64e(b"\x01" * 32)},
        )
        assert res.status_code == 403


def test_pairing_single_attempt_burn_and_anti_interception(tmp_path: Path, monkeypatch):
    app, bridge, _sm = _build_isolated_app(tmp_path, allowed_subnets=["192.168.1.0/24"])
    monkeypatch.setattr(
        "tools.mobile_bridge_mcp.discover_local_private_subnets",
        lambda: bridge.subnet_verifier.get_subnets(),
    )

    local_ui = TestClient(app, client=("127.0.0.1", 52000))
    lan_http = TestClient(app, client=("192.168.1.25", 52001))
    attacker_http = TestClient(app, client=("192.168.1.99", 52002))

    # 1. Pairing fails when window is not opened on phone
    client = WorldBoxBridgeClient(state_dir=tmp_path / "pc_state")
    with pytest.raises(ClientBridgeError, match="403"):
        client.pair(code="abcd-efgh-jkm2", host="192.168.1.10", http_client=lan_http)

    # 2. Wrong code immediately burns the single-attempt pairing window
    started = local_ui.post("/api/debug-bridge/local/pair/start").json()
    real_code = started["pairing"]["code"]
    with pytest.raises(ClientBridgeError, match="Invalid pairing code"):
        client.pair(code="2222-3333-4444", host="192.168.1.10", http_client=attacker_http)

    # Window is now burned — even the legitimate client with the real code is rejected
    with pytest.raises(ClientBridgeError, match="Pairing is not active"):
        client.pair(code=real_code, host="192.168.1.10", http_client=lan_http)

    # 3. Fresh window + legitimate code succeeds
    started2 = local_ui.post("/api/debug-bridge/local/pair/start").json()
    res = client.pair(code=started2["pairing"]["code"], host="192.168.1.10", http_client=lan_http)
    assert res["paired"] is True

    # 4. Unpaired attacker trying to race a connection request is dropped silently (zero popup!)
    _, fake_pub = x25519_keypair()
    r_att = attacker_http.post(
        "/api/debug-bridge/lan/session/request",
        headers=BRIDGE_HEADERS,
        json={
            "device_id": "attacker-id",
            "client_name": "Antigravity (Fake)",
            "client_eph_pub_b64": b64e(fake_pub),
            "requested_write": True,
            "timestamp": time.time(),
            "nonce": "a" * 24,
            "signature_b64": b64e(b"\x00" * 32),
        },
    )
    assert r_att.status_code == 403
    assert local_ui.get("/api/debug-bridge/local/status").json()["pending_session"] is None

    # 5. Replay of a legitimately signed session request is rejected
    paired_json = json.loads(client.paired_file.read_text(encoding="utf-8"))
    pair_key = b64d(paired_json["pair_key_b64"])
    _, eph_pub = x25519_keypair()
    eph_b64 = b64e(eph_pub)
    ts = time.time()
    nonce = "unique_nonce_1234567890"
    sig = sign_bridge_message(
        pair_key,
        "session_request",
        [paired_json["device_id"], str(ts), nonce, eph_b64, "0", "PC"],
    )
    req_body = {
        "device_id": paired_json["device_id"],
        "client_name": "PC",
        "client_eph_pub_b64": eph_b64,
        "requested_write": False,
        "timestamp": ts,
        "nonce": nonce,
        "signature_b64": sig,
    }
    assert lan_http.post("/api/debug-bridge/lan/session/request", headers=BRIDGE_HEADERS, json=req_body).status_code == 200
    assert lan_http.post("/api/debug-bridge/lan/session/request", headers=BRIDGE_HEADERS, json=req_body).status_code == 403


def test_end_to_end_session_read_write_plot_module_and_disconnect(tmp_path: Path, monkeypatch):
    app, bridge, _sm = _build_isolated_app(tmp_path, allowed_subnets=["192.168.1.0/24"])
    monkeypatch.setattr(
        "tools.mobile_bridge_mcp.discover_local_private_subnets",
        lambda: bridge.subnet_verifier.get_subnets(),
    )

    local_ui = TestClient(app, client=("127.0.0.1", 53000))
    lan_http = TestClient(app, client=("192.168.1.55", 53001))
    client = WorldBoxBridgeClient(state_dir=tmp_path / "pc_state")

    # Pair device
    code = local_ui.post("/api/debug-bridge/local/pair/start").json()["pairing"]["code"]
    client.pair(code=code, host="192.168.1.10", http_client=lan_http)

    # Mobile browser pushes a JS console error + UI route
    local_ui.post(
        "/api/debug-bridge/local/client-telemetry",
        json={
            "ui_state": {"mode": "storyteller-game", "active_save_id": "save1", "viewport": {"width": 412, "height": 915}},
            "errors": [{"kind": "uncaught_error", "message": "TypeError: Cannot read properties of null"}],
        },
    )

    # Request connection (wait_seconds=0 so it returns pending immediately in single-threaded TestClient)
    pending_res = client.connect(host="192.168.1.10", wait_seconds=0.0, http_client=lan_http)
    assert pending_res["status"] == "pending"

    ui_state = local_ui.get("/api/debug-bridge/local/status").json()
    req_id = ui_state["pending_session"]["request_id"]

    # Approve on phone in default Read-Only mode
    local_ui.post(
        "/api/debug-bridge/local/session/decide",
        json={"request_id": req_id, "approve": True, "allow_write": False},
    )
    conn_res = client.poll_pending(wait_seconds=1.0, http_client=lan_http)
    assert conn_res["status"] == "connected"
    assert conn_res["allow_write"] is False

    # Read tools work over encrypted ChaCha20-Poly1305 channel
    overview = client.rpc("get_overview", {}, http_client=lan_http)
    assert overview["session"]["active_save_id"] == "save1"
    assert overview["client_ui"]["mode"] == "storyteller-game"
    assert overview["client_error_count"] == 1

    logs = client.rpc("get_logs", {"limit": 20}, http_client=lan_http)
    assert any("TypeError" in e["message"] for e in logs["client_errors"])

    mod_overview = client.rpc("get_module_data", {}, http_client=lan_http)
    assert "wb_plot_director" in mod_overview["modules"]

    # Replay of an encrypted RPC packet (seq=1) is rejected with 403
    sess_file = json.loads(client.session_file.read_text(encoding="utf-8"))
    replayed_body = {
        "session_id": sess_file["session_id"],
        "seq": 1,
        "ciphertext_b64": b64e(
            chacha20_poly1305_seal(
                b64d(sess_file["c2s_key_b64"]),
                make_rpc_nonce(b"c2s\x00", 1),
                b'{"method":"ping","params":{}}',
                aad=sess_file["session_id"].encode("utf-8"),
            )
        ),
    }
    assert lan_http.post("/api/debug-bridge/lan/rpc", headers=BRIDGE_HEADERS, json=replayed_body).status_code == 403

    # Write operations are rejected while session is Read-Only
    with pytest.raises(ClientBridgeError, match="write_not_allowed"):
        client.rpc(
            "update_module_data",
            {"mod_id": "wb_plot_director", "data": {"momentum": "building"}},
            http_client=lan_http,
        )

    # Toggle write mode ON live from the phone's session badge
    assert local_ui.put("/api/debug-bridge/local/session/write-mode", json={"allow_write": True}).status_code == 200

    # Now updating Plot Director module_data and running /plot slash commands work!
    upd = client.rpc(
        "update_module_data",
        {
            "mod_id": "wb_plot_director",
            "data": {
                "direction": {"premise": "A haunted ironwatch lighthouse", "north_star": "Uncover the sunken signal"},
                "active_thread": {"id": "t1", "goal": "Inspect the lantern room"},
            },
        },
        http_client=lan_http,
    )
    assert upd["updated"] is True
    assert upd["module_data"]["direction"]["north_star"] == "Uncover the sunken signal"

    plot_read = client.rpc("get_module_data", {"mod_id": "wb_plot_director"}, http_client=lan_http)
    assert plot_read["module_data"]["active_thread"]["goal"] == "Inspect the lantern room"

    cmd_res = client.rpc("run_module_command", {"command": "/plot"}, http_client=lan_http)
    assert cmd_res["mod_id"] == "wb_plot_director"
    assert cmd_res["error"] is False

    # Toggling write mode back OFF immediately blocks further writes
    local_ui.put("/api/debug-bridge/local/session/write-mode", json={"allow_write": False})
    with pytest.raises(ClientBridgeError, match="write_not_allowed"):
        client.rpc(
            "update_module_data",
            {"mod_id": "wb_plot_director", "data": {"momentum": "steady"}},
            http_client=lan_http,
        )

    # One-tap disconnect on phone immediately invalidates the encrypted session
    local_ui.post("/api/debug-bridge/local/session/disconnect")
    with pytest.raises(ClientBridgeError, match="Session ended"):
        client.rpc("get_overview", {}, http_client=lan_http)


def test_stealth_udp_discovery_and_mcp_stdio_protocol(tmp_path: Path):
    verifier = SubnetVerifier(override_subnets=["192.168.1.0/24"])
    bridge = DebugBridgeManager(storage_dir=tmp_path / "udp_srv", server_name="PhoneWB", subnet_verifier=verifier)

    pair_probe = json.dumps({"proto": "wb-bridge-discover-v1", "mode": "pair"}).encode("utf-8")
    # Silent when idle (no pairing window open)
    assert bridge.handle_udp_discovery_packet(pair_probe, "192.168.1.20", "192.168.1.10", 8321) is None

    # Answers same-subnet probe during active pairing window, ignores other subnets
    bridge.start_pairing()
    assert bridge.handle_udp_discovery_packet(pair_probe, "192.168.2.20", "192.168.1.10", 8321) is None
    ans = bridge.handle_udp_discovery_packet(pair_probe, "192.168.1.20", "192.168.1.10", 8321)
    assert ans is not None
    assert json.loads(ans.decode("utf-8"))["server_name"] == "PhoneWB"

    # MCP JSON-RPC 2.0 protocol framing (both newline-JSON and Content-Length)
    client = WorldBoxBridgeClient(state_dir=tmp_path / "mcp_cli")
    init_resp = handle_jsonrpc_message(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
    )
    assert init_resp["result"]["protocolVersion"] == "2025-03-26"

    list_resp = handle_jsonrpc_message(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool_names = {t["name"] for t in list_resp["result"]["tools"]}
    assert len(TOOLS_SPEC) == 15
    assert {"worldbox_pair", "worldbox_connect", "worldbox_get_module_data", "worldbox_update_module_data", "worldbox_run_module_command"} <= tool_names

    # Content-Length stream roundtrip
    buf = io.BytesIO()
    _write_mcp_response(buf, init_resp, use_content_length=True)
    buf.seek(0)
    parsed, was_framed = _read_mcp_stream(buf)
    assert was_framed is True
    assert parsed["id"] == 1


def test_all_rpc_methods_memories_settings_llm_calls_and_story_writes(tmp_path: Path, monkeypatch):
    import asyncio

    app, bridge, sm, inspector, _call_log, lorebooks = _build_isolated_app(
        tmp_path,
        allowed_subnets=["192.168.1.0/24"],
        return_deps=True,
    )
    monkeypatch.setattr(
        "tools.mobile_bridge_mcp.discover_local_private_subnets",
        lambda: bridge.subnet_verifier.get_subnets(),
    )

    local_ui = TestClient(app, client=("127.0.0.1", 54000))
    lan_http = TestClient(app, client=("192.168.1.77", 54001))
    client = WorldBoxBridgeClient(state_dir=tmp_path / "pc_state")

    code = local_ui.post("/api/debug-bridge/local/pair/start").json()["pairing"]["code"]
    client.pair(code=code, host="192.168.1.10", http_client=lan_http)
    client.connect(host="192.168.1.10", wait_seconds=0.0, http_client=lan_http)
    req_id = local_ui.get("/api/debug-bridge/local/status").json()["pending_session"]["request_id"]
    local_ui.post(
        "/api/debug-bridge/local/session/decide",
        json={"request_id": req_id, "approve": True, "allow_write": True},
    )
    conn_res = client.poll_pending(wait_seconds=1.0, http_client=lan_http)
    assert conn_res["allow_write"] is True

    # 1. get_memories before any DB exists -> initialized=False, no crash
    mem_empty = client.rpc("get_memories", {}, http_client=lan_http)
    assert mem_empty["initialized"] is False
    assert mem_empty["memories"] == []
    assert mem_empty["world_entries"] == []

    # 2. get_memories with memories.db present, BEFORE world_index exists (regression for
    #    both `list_all_memories` and `has_world_index()` guard)
    mem_dir = sm.get_memory_path()
    mm = MemoryManager(mem_dir, embedding_dim=4)
    try:
        mid = mm.add_memory(
            [0.1, 0.2, 0.3, 0.4],
            "The Harbour Warden gave Lyra a brass lantern key.",
            turn=1,
            importance=8,
            summary="Lyra received the brass lantern key.",
            entities=["Lyra", "Harbour Warden"],
            topics=["key", "lantern"],
        )
    finally:
        mm.close()

    sm.state["last_retrieved_memory_ids"] = [mid]
    sm.state["last_context_query"] = "unlock the lantern room"
    mem_only = client.rpc("get_memories", {}, http_client=lan_http)
    assert mem_only["initialized"] is True
    assert mem_only["count"] == 1
    assert mem_only["memories"][0]["id"] == mid
    assert mem_only["memories"][0]["entities"] == ["Lyra", "Harbour Warden"]
    assert mem_only["active_memory_ids"] == [mid]
    assert mem_only["world_entries"] == []

    # 3. Seed world_index as well and verify world_entries + include_world_entries=False
    world_dir = sm.data_dir / "saves" / "save1" / "world_index"
    mm2 = MemoryManager(mem_dir, embedding_dim=4)
    try:
        mm2.init_world_index(str(world_dir))
        mm2._world_conn.execute(
            "INSERT INTO world_entries (id, embedding, text, source_type, source_id, region) VALUES (?, ?, ?, ?, ?, ?)",
            ("we_1", _serialize([0.2, 0.1, 0.4, 0.3]), "Ironwatch Lighthouse guards the shoals.", "node", "node_lighthouse", "Coast"),
        )
        mm2._world_conn.commit()
    finally:
        mm2.close()

    mem_with_world = client.rpc("get_memories", {"include_world_entries": True}, http_client=lan_http)
    assert mem_with_world["world_entry_count"] == 1
    assert mem_with_world["world_entries"][0]["source_id"] == "node_lighthouse"

    mem_no_world = client.rpc("get_memories", {"include_world_entries": False}, http_client=lan_http)
    assert mem_no_world["world_entries"] == []
    assert mem_no_world["count"] == 1

    # 4. get_settings_and_prompts + update_settings_or_prompts (regression for story_lorebooks)
    imported_lb = lorebooks.import_lorebook(
        {"entries": {"0": {"key": ["ironwatch"], "content": "Built of meteoric iron."}}},
        name="Ironwatch Lore",
    )
    lb_id = imported_lb["lorebook"]["id"]
    sm.save_manager.update_metadata(
        "save1",
        {
            "lorebook_ids": [lb_id],
            "story_lorebook_entries": [{"uid": "se1", "keys": ["warden"], "content": "Never sleeps."}],
        },
    )
    sm.set_story_style("save1", {"themes": "gothic mystery", "pacing": "measured"})

    sp = client.rpc("get_settings_and_prompts", {"include_all_lorebooks": True}, http_client=lan_http)
    assert sp["story_lorebooks"]["lorebook_ids"] == [lb_id]
    assert sp["story_lorebooks"]["story_entries"][0]["uid"] == "se1"
    assert sp["story_style"]["themes"] == "gothic mystery"
    assert any(b["id"] == lb_id for b in sp["all_lorebooks"])

    upd_sp = client.rpc(
        "update_settings_or_prompts",
        {"settings": {"memory.rag_limit": 6}, "scope": "story"},
        http_client=lan_http,
    )
    assert upd_sp["updated"] is True
    rag_desc = next(d for d in upd_sp["settings"]["Memory"] if d["key"] == "memory.rag_limit")
    assert rag_desc["value"] == 6

    # 5. get_llm_calls across both `inspector` and `persistent` (regression for
    #    module_source/call_type/step, newest-first slicing, and full_input compaction)
    async def _seed_calls():
        cid1 = await inspector.start_call(
            call_type="storyteller",
            model="gemini-pro",
            step="storyteller",
            module_source="",
            input_data=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "Open the door"}],
        )
        await inspector.end_call(cid1, output_data="The heavy oak door groans open.", tokens_in=120, tokens_out=30)
        await asyncio.sleep(0.005)

        cid2 = await inspector.start_call(
            call_type="module_fast",
            model="gemini-flash",
            step="plot_beat",
            module_source="wb_plot_director",
            input_data=[{"role": "user", "content": "UNTRUNCATED PLOT PROMPT"}],
        )
        await inspector.end_call(cid2, output_data='{"tension": "rising"}', tokens_in=80, tokens_out=12)
        await asyncio.sleep(0.005)

        cid3 = await inspector.start_call(
            call_type="reader",
            model="gemini-flash",
            step="reader",
            module_source="",
            input_data=[{"role": "user", "content": "EXTRACT STATE"}],
        )
        await inspector.end_call(cid3, error="Upstream 503 overloaded")
        return cid1, cid2, cid3

    cid1, cid2, cid3 = asyncio.run(_seed_calls())

    # Newest-first slicing: limit=2 returns [cid3, cid2], NOT [cid2, cid1]
    latest_two = client.rpc("get_llm_calls", {"source": "inspector", "limit": 2}, http_client=lan_http)
    assert [c["id"] for c in latest_two["calls"]] == [cid3, cid2]
    assert "full_input" not in latest_two["calls"][0]
    assert latest_two["calls"][0]["full_input_omitted"] is True
    assert latest_two["calls"][0]["message_count"] == 1

    # Filtering by module_source ("wb_plot_director") and call_type ("storyteller")
    plot_calls = client.rpc("get_llm_calls", {"module": "wb_plot_director"}, http_client=lan_http)
    assert [c["id"] for c in plot_calls["calls"]] == [cid2]

    st_calls = client.rpc("get_llm_calls", {"module": "storyteller"}, http_client=lan_http)
    assert [c["id"] for c in st_calls["calls"]] == [cid1]

    # Single call_id returns complete untruncated full_input
    single_call = client.rpc("get_llm_calls", {"call_id": cid2}, http_client=lan_http)
    assert single_call["calls"][0]["full_input"] == [{"role": "user", "content": "UNTRUNCATED PLOT PROMPT"}]

    # Clear RAM inspector to simulate app restart -> persistent JSONL still serves full history!
    inspector.clear()
    assert client.rpc("get_llm_calls", {"source": "inspector"}, http_client=lan_http)["count"] == 0

    persisted_all = client.rpc("get_llm_calls", {"source": "persistent", "limit": 2}, http_client=lan_http)
    assert [c["id"] for c in persisted_all["calls"]] == [cid3, cid2]

    persisted_err = client.rpc("get_llm_calls", {"source": "persistent", "errors_only": True}, http_client=lan_http)
    assert [c["id"] for c in persisted_err["calls"]] == [cid3]

    persisted_plot = client.rpc(
        "get_llm_calls",
        {"source": "persistent", "module": "wb_plot_director", "include_messages": True},
        http_client=lan_http,
    )
    assert [c["id"] for c in persisted_plot["calls"]] == [cid2]
    assert persisted_plot["calls"][0]["full_input"] == [{"role": "user", "content": "UNTRUNCATED PLOT PROMPT"}]

    # 6. get_session_state, update_module_configs, and update_story_state (patch / edit / undo)
    sm.state["history"] = ["Opening scene."]
    sm.state["chat_messages"] = [{"role": "ai", "content": "Opening scene."}]
    sm.save_manager.save_turn("save1", sm.state, 0)
    sm.save_completed_turn(
        {"history": ["Opening scene.", "Turn 1 narration."], "turn": 1},
        user_text="Step into the tower",
    )

    sess_state = client.rpc(
        "get_session_state",
        {"include_saves_list": True, "message_limit": 2},
        http_client=lan_http,
    )
    assert sess_state["is_active_session"] is True
    assert sess_state["state"]["chat_messages_total"] == 3
    assert len(sess_state["state"]["chat_messages"]) == 2
    assert any(s["id"] == "save1" for s in sess_state["saves"])

    cfg_res = client.rpc(
        "update_module_configs",
        {"module_configs": {"__active_modules__": ["wb_plot_director"], "wb_plot_director": {"custom_flag": True}}},
        http_client=lan_http,
    )
    assert cfg_res["module_configs"]["wb_plot_director"]["custom_flag"] is True

    patch_res = client.rpc(
        "update_story_state",
        {
            "action": "patch",
            "characters": {"default_player": {"name": "Lyra"}},
            "player_location_node_id": "node_lighthouse",
        },
        http_client=lan_http,
    )
    assert patch_res["characters"]["default_player"]["name"] == "Lyra"
    assert patch_res["player_location_node_id"] == "node_lighthouse"

    edit_res = client.rpc(
        "update_story_state",
        {"action": "edit_message", "index": 2, "content": "Turn 1 narration (edited)."},
        http_client=lan_http,
    )
    assert edit_res["edited_index"] == 2
    assert sm.state["chat_messages"][2]["content"] == "Turn 1 narration (edited)."

    undo_res = client.rpc(
        "update_story_state",
        {"action": "undo_turn", "target_turn": 0},
        http_client=lan_http,
    )
    assert undo_res["undone_to_turn"] == 0
    assert len(sm.state["chat_messages"]) == 1
