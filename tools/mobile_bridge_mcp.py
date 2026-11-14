#!/usr/bin/env python3
"""Stdio MCP server & client for connecting Antigravity to a mobile WorldBox
instance on the same local private subnet.

All network traffic uses:
- Exact local private subnet verification
- One-time X25519 + scrypt mutual pairing (`worldbox_pair`)
- Signed per-session approval requests (`worldbox_connect`)
- ChaCha20-Poly1305 AEAD encrypted RPC envelopes with monotonic sequence numbers
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from backend.engine.debug_bridge import (  # noqa: E402
     _write_private_json,
    b64d,
    b64e,
    chacha20_poly1305_open,
    chacha20_poly1305_seal,
    derive_pairing_material,
    derive_session_keys,
    discover_local_private_subnets,
    is_private_lan_ip,
    make_rpc_nonce,
    sign_bridge_message,
    x25519_keypair,
    x25519_shared_secret,
)

BRIDGE_HEADERS = {"X-WB-Bridge": "1", "Content-Type": "application/json"}
DEFAULT_PORT = 8321


class ClientBridgeError(Exception):
    pass


class WorldBoxBridgeClient:
    """Manages local paired credentials, UDP subnet discovery, session approval,
    and AEAD-encrypted RPC calls to a mobile WorldBox instance."""

    def __init__(self, state_dir: Optional[str | Path] = None) -> None:
        self.state_dir = Path(state_dir) if state_dir else (REPO_ROOT / "data" / "debug_bridge")
        self.paired_file = self.state_dir / "client_paired.json"
        self.session_file = self.state_dir / "client_session.json"
        self.pending_file = self.state_dir / "client_pending.json"
        self.default_client_name = f"Antigravity ({socket.gethostname() or 'PC'})"

    def _read_json(self, path: Path) -> Optional[dict[str, Any]]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _delete_file(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _check_target_same_subnet(self, host: str) -> None:
        """Client-side belt-and-suspenders check: never send paired traffic
        outside our own local RFC 1918 private subnet(s)."""
        if os.environ.get("WB_BRIDGE_ALLOW_LOOPBACK_TEST") == "1" and host in ("127.0.0.1", "localhost", "testclient"):
            return
        try:
            ip = ipaddress.ip_address(host.strip())
        except ValueError:
            raise ClientBridgeError(f"Host '{host}' must be a numeric local LAN IPv4/IPv6 address.")
        if not is_private_lan_ip(ip):
            raise ClientBridgeError(f"Refusing connection to '{host}': not a private local network address.")
        subnets = discover_local_private_subnets()
        if subnets and not any(ip in net for net in subnets if net.version == ip.version):
            subnet_str = ", ".join(str(n) for n in subnets)
            raise ClientBridgeError(
                f"Refusing connection to '{host}': not in this machine's local subnet(s) ({subnet_str})."
            )

    def discover_lan_host(self, mode: str, port: int = DEFAULT_PORT, timeout: float = 1.5) -> Optional[tuple[str, int, str]]:
        """Send a stealth UDP broadcast probe on each local private subnet's
        broadcast address. Returns `(ip, tcp_port, server_name)` if answered."""
        subnets = discover_local_private_subnets()
        targets: list[str] = []
        for net in subnets:
            if isinstance(net, ipaddress.IPv4Network):
                targets.append(str(net.broadcast_address))
        if not targets:
            targets.append("255.255.255.255")

        paired = self._read_json(self.paired_file)
        nonce = secrets.token_urlsafe(18)
        if mode == "paired":
            if not paired:
                return None
            pair_key = b64d(paired["pair_key_b64"])
            ts = time.time()
            sig = sign_bridge_message(pair_key, "udp_discover", [paired["device_id"], str(ts), nonce])
            packet = json.dumps({
                "proto": "wb-bridge-discover-v1",
                "mode": "paired",
                "device_id": paired["device_id"],
                "timestamp": ts,
                "nonce": nonce,
                "signature_b64": sig,
            }).encode("utf-8")
        else:
            pair_key = b""
            packet = json.dumps({"proto": "wb-bridge-discover-v1", "mode": "pair"}).encode("utf-8")

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(timeout)
                for bcast in targets:
                    try:
                        sock.sendto(packet, (bcast, port))
                    except OSError:
                        continue
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    rem = max(0.05, deadline - time.monotonic())
                    sock.settimeout(rem)
                    try:
                        data, addr = sock.recvfrom(2048)
                    except OSError:
                        break
                    try:
                        msg = json.loads(data.decode("utf-8"))
                    except Exception:
                        continue
                    if msg.get("proto") != "wb-bridge-discover-v1" or msg.get("mode") != mode:
                        continue
                    resp_port = int(msg.get("port") or port)
                    srv_name = str(msg.get("server_name") or "WorldBox")
                    if mode == "paired":
                        expected_ack = sign_bridge_message(
                            pair_key, "udp_discover_ack", [nonce, str(resp_port), srv_name]
                        )
                        if not hmac.compare_digest(str(msg.get("signature_b64") or ""), expected_ack):
                            continue
                    return addr[0], resp_port, srv_name
        except OSError:
            return None
        return None

    def pair(
        self,
        code: str,
        host: Optional[str] = None,
        port: int = DEFAULT_PORT,
        client_name: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> dict[str, Any]:
        """Run the one-time mutual X25519 + scrypt pairing handshake."""
        resolved_host = (host or "").strip()
        resolved_port = int(port or DEFAULT_PORT)
        if not resolved_host:
            found = self.discover_lan_host(mode="pair", port=resolved_port)
            if not found:
                raise ClientBridgeError(
                    "Could not auto-discover a WorldBox instance in pairing mode on your subnet. "
                    "Make sure 'Pair Device' is open in WorldBox Settings -> Remote Bridge on your phone, "
                    "or pass the phone's LAN IP via `host` (shown at the top of Remote Bridge settings)."
                )
            resolved_host, resolved_port, _ = found

        self._check_target_same_subnet(resolved_host)
        base_url = f"http://{resolved_host}:{resolved_port}"
        existing = self._read_json(self.paired_file)
        device_id = (existing or {}).get("device_id") or f"agy-{secrets.token_hex(12)}"
        cname = (client_name or self.default_client_name).strip()
        cli_priv, cli_pub = x25519_keypair()

        own_client = http_client is None
        client = http_client or httpx.Client(timeout=15.0)
        try:
            r1 = client.post(
                f"{base_url}/api/debug-bridge/lan/pair/init",
                headers=BRIDGE_HEADERS,
                json={
                    "client_id": device_id,
                    "client_name": cname,
                    "client_pub_b64": b64e(cli_pub),
                },
            )
            if r1.status_code != 200:
                detail = r1.json().get("detail", r1.text) if r1.headers.get("content-type", "").startswith("application/json") else r1.text
                raise ClientBridgeError(f"Pair init failed ({r1.status_code}): {detail}")
            init_data = r1.json()

            salt = b64d(init_data["salt_b64"])
            srv_pub = b64d(init_data["server_pub_b64"])
            shared_dh = x25519_shared_secret(cli_priv, srv_pub)
            pair_key, client_confirm_key, server_confirm_key, transcript = derive_pairing_material(
                pair_code=code,
                salt=salt,
                client_pub=cli_pub,
                server_pub=srv_pub,
                shared_dh=shared_dh,
            )
            client_mac = hmac.new(client_confirm_key, transcript, "sha256").digest()

            r2 = client.post(
                f"{base_url}/api/debug-bridge/lan/pair/finish",
                headers=BRIDGE_HEADERS,
                json={
                    "pairing_id": init_data["pairing_id"],
                    "client_mac_b64": b64e(client_mac),
                },
            )
            if r2.status_code != 200:
                detail = r2.json().get("detail", r2.text) if r2.headers.get("content-type", "").startswith("application/json") else r2.text
                raise ClientBridgeError(f"Pair finish failed ({r2.status_code}): {detail}")
            fin_data = r2.json()

            expected_srv_mac = hmac.new(server_confirm_key, transcript, "sha256").digest()
            got_srv_mac = b64d(fin_data["server_mac_b64"])
            if not hmac.compare_digest(got_srv_mac, expected_srv_mac):
                raise ClientBridgeError("Server MAC verification failed — possible MITM on the network! Pairing aborted.")

            paired_state = {
                "device_id": device_id,
                "client_name": cname,
                "pair_key_b64": b64e(pair_key),
                "host": resolved_host,
                "port": resolved_port,
                "server_name": fin_data.get("server_name") or init_data.get("server_name") or "WorldBox",
                "paired_at": time.time(),
            }
            _write_private_json(self.paired_file, paired_state)
            self._delete_file(self.session_file)
            self._delete_file(self.pending_file)
            return {
                "paired": True,
                "host": resolved_host,
                "port": resolved_port,
                "server_name": paired_state["server_name"],
                "message": "Device paired successfully. Call `worldbox_connect` next to open an approved diagnostic session.",
            }
        finally:
            if own_client:
                client.close()

    def _resolve_paired_endpoint(self, host: Optional[str], port: Optional[int]) -> tuple[dict[str, Any], str, int]:
        paired = self._read_json(self.paired_file)
        if not paired or not paired.get("pair_key_b64"):
            raise ClientBridgeError(
                "Not paired with a WorldBox device yet. Open Settings -> Remote Bridge -> Pair Device on your phone "
                "and call `worldbox_pair` with the one-time code first."
            )
        target_port = int(port or paired.get("port") or DEFAULT_PORT)
        target_host = (host or "").strip()
        if not target_host:
            # Try UDP signed discovery first in case DHCP changed the phone's IP, falling back to cached IP
            found = self.discover_lan_host(mode="paired", port=target_port, timeout=0.8)
            if found:
                target_host, target_port, _ = found
            else:
                target_host = str(paired.get("host") or "").strip()
        if not target_host:
            raise ClientBridgeError("No target LAN IP known; pass `host` (shown in Settings -> Remote Bridge on your phone).")
        self._check_target_same_subnet(target_host)
        return paired, target_host, target_port

    def connect(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        request_write: bool = False,
        wait_seconds: float = 40.0,
        http_client: Optional[httpx.Client] = None,
    ) -> dict[str, Any]:
        """Request a signed diagnostic session (or reuse a live one) and wait for
        the user to tap Allow in WorldBox."""
        # Check if an existing session is still alive
        existing_sess = self._read_json(self.session_file)
        if existing_sess and time.time() < float(existing_sess.get("expires_at") or 0) - 5:
            try:
                ping_res = self.rpc("ping", {}, http_client=http_client)
                if not request_write or ping_res.get("allow_write"):
                    return {
                        "status": "connected",
                        "reused_existing_session": True,
                        "host": existing_sess["host"],
                        "port": existing_sess["port"],
                        "server_name": ping_res.get("server_name"),
                        "allow_write": bool(ping_res.get("allow_write")),
                    }
            except Exception:
                self._delete_file(self.session_file)

        paired, target_host, target_port = self._resolve_paired_endpoint(host, port)
        pair_key = b64d(paired["pair_key_b64"])
        device_id = paired["device_id"]
        cname = paired.get("client_name") or self.default_client_name
        cli_eph_priv, cli_eph_pub = x25519_keypair()
        cli_eph_pub_b64 = b64e(cli_eph_pub)

        ts = time.time()
        nonce = secrets.token_urlsafe(18)
        sig = sign_bridge_message(
            pair_key,
            "session_request",
            [device_id, str(ts), nonce, cli_eph_pub_b64, "1" if request_write else "0", cname],
        )

        own_client = http_client is None
        client = http_client or httpx.Client(timeout=max(20.0, float(wait_seconds) + 10.0))
        base_url = f"http://{target_host}:{target_port}"
        try:
            r = client.post(
                f"{base_url}/api/debug-bridge/lan/session/request",
                headers=BRIDGE_HEADERS,
                json={
                    "device_id": device_id,
                    "client_name": cname,
                    "client_eph_pub_b64": cli_eph_pub_b64,
                    "requested_write": bool(request_write),
                    "timestamp": ts,
                    "nonce": nonce,
                    "signature_b64": sig,
                },
            )
            if r.status_code != 200:
                detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
                raise ClientBridgeError(f"Session request rejected ({r.status_code}): {detail}")
            req_data = r.json()
            request_id = req_data["request_id"]

            if paired.get("host") != target_host or paired.get("port") != target_port:
                paired["host"] = target_host
                paired["port"] = target_port
                _write_private_json(self.paired_file, paired)

            pending_record = {
                "request_id": request_id,
                "host": target_host,
                "port": target_port,
                "cli_eph_priv_b64": b64e(cli_eph_priv),
                "cli_eph_pub_b64": cli_eph_pub_b64,
                "created_at": time.time(),
            }
            _write_private_json(self.pending_file, pending_record)
            return self.poll_pending(wait_seconds=wait_seconds, http_client=client)
        finally:
            if own_client:
                client.close()

    def poll_pending(
        self,
        wait_seconds: float = 30.0,
        http_client: Optional[httpx.Client] = None,
    ) -> dict[str, Any]:
        pending = self._read_json(self.pending_file)
        if not pending:
            raise ClientBridgeError("No pending connection request; call `worldbox_connect` first.")
        paired = self._read_json(self.paired_file)
        if not paired:
            raise ClientBridgeError("Device is not paired.")

        target_host = pending["host"]
        target_port = int(pending["port"])
        self._check_target_same_subnet(target_host)
        pair_key = b64d(paired["pair_key_b64"])
        device_id = paired["device_id"]
        request_id = pending["request_id"]

        ts = time.time()
        nonce = secrets.token_urlsafe(18)
        sig = sign_bridge_message(pair_key, "session_poll", [device_id, str(ts), nonce, request_id])

        own_client = http_client is None
        client = http_client or httpx.Client(timeout=max(15.0, float(wait_seconds) + 10.0))
        base_url = f"http://{target_host}:{target_port}"
        try:
            r = client.post(
                f"{base_url}/api/debug-bridge/lan/session/poll",
                headers=BRIDGE_HEADERS,
                json={
                    "device_id": device_id,
                    "request_id": request_id,
                    "wait_seconds": float(wait_seconds),
                    "timestamp": ts,
                    "nonce": nonce,
                    "signature_b64": sig,
                },
            )
            if r.status_code != 200:
                detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
                raise ClientBridgeError(f"Session poll failed ({r.status_code}): {detail}")
            data = r.json()
            status = data.get("status")

            if status == "pending":
                return {
                    "status": "pending",
                    "seconds_remaining": data.get("seconds_remaining"),
                    "message": "Waiting for approval on WorldBox mobile UI. Ask the user to tap Allow on their phone, then call `worldbox_connect_poll`.",
                }

            self._delete_file(self.pending_file)
            if status == "denied":
                raise ClientBridgeError("The user denied the connection request on the WorldBox device.")
            if status != "approved":
                raise ClientBridgeError("The connection request expired before it was approved on the device.")

            session_id = data["session_id"]
            srv_eph_pub_b64 = data["server_eph_pub_b64"]
            allow_write = bool(data.get("allow_write"))
            expires_at = float(data["expires_at"])
            expected_srv_sig = sign_bridge_message(
                pair_key,
                "session_approved",
                [request_id, session_id, srv_eph_pub_b64, "1" if allow_write else "0", str(data["expires_at"])],
            )
            if not hmac.compare_digest(str(data.get("server_signature_b64") or ""), expected_srv_sig):
                raise ClientBridgeError("Server approval signature invalid — possible MITM! Aborting session.")

            cli_eph_priv = b64d(pending["cli_eph_priv_b64"])
            cli_eph_pub = b64d(pending["cli_eph_pub_b64"])
            srv_eph_pub = b64d(srv_eph_pub_b64)
            eph_dh = x25519_shared_secret(cli_eph_priv, srv_eph_pub)
            c2s_key, s2c_key = derive_session_keys(
                pair_key=pair_key,
                session_id=session_id,
                client_eph_pub=cli_eph_pub,
                server_eph_pub=srv_eph_pub,
                eph_dh=eph_dh,
            )

            sess_record = {
                "session_id": session_id,
                "host": target_host,
                "port": target_port,
                "c2s_key_b64": b64e(c2s_key),
                "s2c_key_b64": b64e(s2c_key),
                "next_seq": 1,
                "allow_write": allow_write,
                "expires_at": expires_at,
                "server_name": data.get("server_name") or "WorldBox",
            }
            _write_private_json(self.session_file, sess_record)
            return {
                "status": "connected",
                "host": target_host,
                "port": target_port,
                "server_name": sess_record["server_name"],
                "allow_write": allow_write,
                "expires_in_minutes": max(1, int(round((expires_at - time.time()) / 60))),
            }
        finally:
            if own_client:
                client.close()

    def rpc(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> Any:
        sess = self._read_json(self.session_file)
        if not sess:
            raise ClientBridgeError("Not connected to WorldBox. Call `worldbox_connect` first so the user can approve the session.")
        if time.time() >= float(sess.get("expires_at") or 0):
            self._delete_file(self.session_file)
            raise ClientBridgeError("Diagnostic session expired. Call `worldbox_connect` to open a new session.")

        target_host = sess["host"]
        target_port = int(sess["port"])
        self._check_target_same_subnet(target_host)

        seq = int(sess.get("next_seq") or 1)
        sess["next_seq"] = seq + 1
        _write_private_json(self.session_file, sess)

        session_id = sess["session_id"]
        aad = session_id.encode("utf-8")
        c2s_key = b64d(sess["c2s_key_b64"])
        s2c_key = b64d(sess["s2c_key_b64"])

        req_bytes = json.dumps({"method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8")
        sealed_req = chacha20_poly1305_seal(
            c2s_key,
            make_rpc_nonce(b"c2s\x00", seq),
            req_bytes,
            aad=aad,
        )

        own_client = http_client is None
        client = http_client or httpx.Client(timeout=30.0)
        base_url = f"http://{target_host}:{target_port}"
        try:
            r = client.post(
                f"{base_url}/api/debug-bridge/lan/rpc",
                headers=BRIDGE_HEADERS,
                json={
                    "session_id": session_id,
                    "seq": seq,
                    "ciphertext_b64": b64e(sealed_req),
                },
            )
            if r.status_code == 401:
                self._delete_file(self.session_file)
                raise ClientBridgeError("Session ended or was disconnected on the mobile device. Call `worldbox_connect` to reconnect.")
            if r.status_code != 200:
                detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
                raise ClientBridgeError(f"RPC transport error ({r.status_code}): {detail}")

            resp_json = r.json()
            if resp_json.get("seq") != seq or resp_json.get("session_id") != session_id:
                raise ClientBridgeError("RPC response sequence mismatch.")
            plain_resp = chacha20_poly1305_open(
                s2c_key,
                make_rpc_nonce(b"s2c\x00", seq),
                b64d(resp_json["ciphertext_b64"]),
                aad=aad,
            )
            envelope = json.loads(plain_resp.decode("utf-8"))
        finally:
            if own_client:
                client.close()

        # Keep cached allow_write in sync with live toggles on the phone
        if "allow_write" in envelope and sess.get("allow_write") != bool(envelope["allow_write"]):
            sess["allow_write"] = bool(envelope["allow_write"])
            _write_private_json(self.session_file, sess)

        if not envelope.get("ok"):
            err = envelope.get("error") or {}
            raise ClientBridgeError(f"[{err.get('code', 'rpc_error')}] {err.get('message', 'RPC failed')}")
        res = envelope.get("result")
        if isinstance(res, dict) and method == "ping":
            res = {**res, "allow_write": bool(envelope.get("allow_write"))}
        return res


# ---------------------------------------------------------------------------
# MCP Tool Definitions & JSON-RPC 2.0 Stdio Loop
# ---------------------------------------------------------------------------

TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "worldbox_pair",
        "description": (
            "One-time mutual pairing with the user's mobile WorldBox instance on the same local subnet. "
            "Requires the user to first tap 'Pair Device' in WorldBox Settings -> Remote Bridge on their phone "
            "and share the one-time code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "One-time pairing code displayed on the WorldBox mobile screen (e.g. 'k9x4-m2p7-v8q1')."},
                "host": {"type": "string", "description": "Optional LAN IPv4 address of the phone (auto-discovered via UDP on the local subnet if omitted)."},
                "port": {"type": "integer", "description": "WorldBox backend port (default 8321)."},
            },
            "required": ["code"],
        },
    },
    {
        "name": "worldbox_connect",
        "description": (
            "Request an encrypted diagnostic session with the paired mobile WorldBox instance on the same local subnet. "
            "Triggers an approval prompt on the user's WorldBox client (Read-Only by default) and waits up to `wait_seconds` for them to tap Allow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Optional phone LAN IP override (uses signed UDP discovery / last known IP if omitted)."},
                "port": {"type": "integer", "description": "Optional port override (default 8321)."},
                "request_write": {"type": "boolean", "description": "Set true only if the user wants you to make changes; the user still controls the final switch on their phone."},
                "wait_seconds": {"type": "number", "description": "Seconds to wait for the user to tap Allow on their phone (default 40)."},
            },
        },
    },
    {
        "name": "worldbox_connect_poll",
        "description": "Continue waiting for the user to tap Allow on a pending `worldbox_connect` request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wait_seconds": {"type": "number", "description": "Seconds to wait (default 30)."},
            },
        },
    },
    {
        "name": "worldbox_get_overview",
        "description": "Read high-level status from the connected mobile WorldBox instance: current UI screen/viewport, active save & turn, LLM models, active modules, memory row count, and error count.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "worldbox_get_logs",
        "description": "Read backend server logs AND captured mobile browser JS errors (`window.onerror`, `unhandledrejection`, `console.error`) from the connected WorldBox device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "description": "Optional log level filter (e.g. 'ERROR', 'WARNING')."},
                "limit": {"type": "integer", "description": "Max server log entries to return (default 200)."},
                "since_id": {"type": "integer", "description": "Return server logs after this ID."},
                "client_since_id": {"type": "integer", "description": "Return mobile browser errors after this ID."},
            },
        },
    },
    {
        "name": "worldbox_get_llm_calls",
        "description": "Inspect recent LLM calls (model, module_source/call_type/step, token usage, latency, response, errors, and full untruncated prompts when `call_id` or `include_messages=true` is passed).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max calls to return, newest-first (default 20)."},
                "source": {"type": "string", "enum": ["inspector", "persistent"], "description": "'inspector' (in-memory ring buffer) or 'persistent' (on-disk llm_calls.jsonl across restarts)."},
                "since_id": {"type": "string", "description": "For 'inspector' source: return calls strictly newer than this call ID."},
                "call_id": {"type": "string", "description": "Specific LLM call ID to fetch with full untruncated input messages."},
                "module": {"type": "string", "description": "Filter by module_source, call_type, or step (e.g. 'wb_plot_director', 'storyteller', 'reader')."},
                "errors_only": {"type": "boolean", "description": "Only return failed LLM calls."},
                "include_messages": {"type": "boolean", "description": "Include full untruncated `full_input` prompts in list results."},
            },
        },
    },
    {
        "name": "worldbox_get_session_state",
        "description": "Inspect the active story save state (or another save by `save_id`), including chat messages, characters, location, swipes, and module configs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "save_id": {"type": "string", "description": "Optional save_id to inspect; omit for the active session."},
                "include_messages": {"type": "boolean", "description": "Include chat transcript messages (default true)."},
                "message_limit": {"type": "integer", "description": "Return the last N messages (default 20; pass 0 for all messages)."},
                "include_world_data": {"type": "boolean", "description": "Include full world_data payload (default false; summary returned otherwise)."},
                "include_saves_list": {"type": "boolean", "description": "Also list all available saves on the device."},
            },
        },
    },
    {
        "name": "worldbox_get_module_data",
        "description": (
            "Inspect story module data, configs, commands, and instruction overrides. "
            "Omit `mod_id` for an overview of all modules, or pass a `mod_id` such as 'wb_plot_director' "
            "(plot profile, narrative direction, active thread, thread history, log), 'wb_npc_system', "
            "'wb_core_rpg', 'wb_inventory', 'wb_time_tracker', or 'wb_worldgen'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mod_id": {"type": "string", "description": "Module ID (e.g. 'wb_plot_director'); omit to list all modules."},
                "save_id": {"type": "string", "description": "Optional save_id; omit for the active session."},
            },
        },
    },
    {
        "name": "worldbox_get_memories",
        "description": "Read story vector memories and world lore entries from the active session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max memories to return (default 500)."},
                "include_world_entries": {"type": "boolean", "description": "Include world lore vector entries (default true)."},
            },
        },
    },
    {
        "name": "worldbox_get_settings_and_prompts",
        "description": "Read story & global engine settings, the story prompt pipeline, last prompt trace, and linked lorebooks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_all_lorebooks": {"type": "boolean", "description": "Also list all installed lorebooks."},
            },
        },
    },
    {
        "name": "worldbox_update_module_data",
        "description": (
            "WRITE MODE ONLY (requires user to enable 'Allow making changes' on their phone): "
            "Update `state['module_data'][mod_id]` (e.g. `wb_plot_director` plot threads, direction, or profile), "
            "persist to the save + snapshot, and live-refresh the mobile UI."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mod_id": {"type": "string", "description": "Module ID to update (e.g. 'wb_plot_director')."},
                "data": {"type": "object", "description": "JSON object to merge into (or replace) the module's state."},
                "mode": {"type": "string", "enum": ["merge", "replace"], "description": "'merge' (deep merge, default) or 'replace' (wholesale overwrite)."},
                "replace_keys": {"type": "array", "items": {"type": "string"}, "description": "In 'merge' mode, top-level keys of `data` to overwrite wholesale rather than deep-merging."},
                "delete_keys": {"type": "array", "items": {"type": "string"}, "description": "In 'merge' mode, top-level keys to delete from the module's state."},
            },
            "required": ["mod_id", "data"],
        },
    },
    {
        "name": "worldbox_run_module_command",
        "description": (
            "WRITE MODE ONLY: Execute an active module's slash command (e.g. '/plot ...', '/npc ...', '/recall ...') "
            "through WorldBox's native module command pipeline, persisting writebacks and refreshing mobile widgets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Slash command string, e.g. '/plot status'."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "worldbox_update_module_configs",
        "description": "WRITE MODE ONLY: Update per-story `module_configs` (module settings, `__active_modules__`, or `__module_instructions__`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "module_configs": {"type": "object", "description": "Updated module_configs dictionary."},
            },
            "required": ["module_configs"],
        },
    },
    {
        "name": "worldbox_update_story_state",
        "description": "WRITE MODE ONLY: Patch player/character state or location, edit a chat message, or undo to an earlier turn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["patch", "undo_turn", "edit_message"], "description": "Operation to perform (default 'patch')."},
                "characters": {"type": "object", "description": "For 'patch': character dictionary patches keyed by character ID (e.g. 'default_player')."},
                "player_location_node_id": {"type": "string"},
                "player_location_map_id": {"type": "string"},
                "revealed_node_ids": {"type": "array", "items": {"type": "string"}},
                "target_turn": {"type": "integer", "description": "For 'undo_turn': turn number to roll back to."},
                "index": {"type": "integer", "description": "For 'edit_message': message index."},
                "content": {"type": "string", "description": "For 'edit_message': replacement text."},
            },
        },
    },
    {
        "name": "worldbox_update_settings_or_prompts",
        "description": "WRITE MODE ONLY: Update engine/story settings or the active story's prompt pipeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "settings": {"type": "object", "description": "Key-value settings updates."},
                "scope": {"type": "string", "enum": ["story", "global"], "description": "Settings scope (default 'story')."},
                "prompt_pipeline": {"type": "array", "items": {"type": "object"}, "description": "Replacement prompt pipeline block list."},
            },
        },
    },
]


def execute_tool(client: WorldBoxBridgeClient, name: str, args: dict[str, Any]) -> Any:
    if name == "worldbox_pair":
        return client.pair(
            code=str(args.get("code") or ""),
            host=args.get("host"),
            port=int(args.get("port") or DEFAULT_PORT),
        )
    if name == "worldbox_connect":
        return client.connect(
            host=args.get("host"),
            port=args.get("port"),
            request_write=bool(args.get("request_write", False)),
            wait_seconds=float(args.get("wait_seconds", 40.0)),
        )
    if name == "worldbox_connect_poll":
        return client.poll_pending(wait_seconds=float(args.get("wait_seconds", 30.0)))
    if name == "worldbox_get_overview":
        return client.rpc("get_overview", {})
    if name == "worldbox_get_logs":
        return client.rpc("get_logs", args)
    if name == "worldbox_get_llm_calls":
        return client.rpc("get_llm_calls", args)
    if name == "worldbox_get_session_state":
        params = dict(args)
        if "message_limit" not in params:
            params["message_limit"] = 20
        return client.rpc("get_session_state", params)
    if name == "worldbox_get_module_data":
        return client.rpc("get_module_data", args)
    if name == "worldbox_get_memories":
        return client.rpc("get_memories", args)
    if name == "worldbox_get_settings_and_prompts":
        return client.rpc("get_settings_and_prompts", args)
    if name == "worldbox_update_module_data":
        return client.rpc("update_module_data", args)
    if name == "worldbox_run_module_command":
        return client.rpc("run_module_command", args)
    if name == "worldbox_update_module_configs":
        return client.rpc("update_module_configs", args)
    if name == "worldbox_update_story_state":
        return client.rpc("update_story_state", args)
    if name == "worldbox_update_settings_or_prompts":
        return client.rpc("update_settings_or_prompts", args)
    raise ClientBridgeError(f"Unknown tool: {name}")


def handle_jsonrpc_message(client: WorldBoxBridgeClient, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    # Notifications have no `id` and expect no reply
    if msg_id is None:
        return None

    if method == "initialize":
        req_ver = str(params.get("protocolVersion") or "2024-11-05")
        supported = ("2024-11-05", "2025-03-26", "2025-06-18")
        proto_ver = req_ver if req_ver in supported else "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": proto_ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "worldbox-mobile-bridge", "version": "1.0.0"},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS_SPEC}}

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            out = execute_tool(client, tool_name, tool_args)
            text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True},
            }

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_mcp_stream(stdin_buf) -> tuple[Optional[dict[str, Any]], bool]:
    """Read one JSON-RPC message from stdin, supporting both newline-delimited
    JSON (MCP 2024-11-05 / 2025-03-26) and Content-Length framing."""
    while True:
        line = stdin_buf.readline()
        if not line:
            return None, False
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            # Consume remaining headers up to blank line
            while True:
                hline = stdin_buf.readline()
                if not hline or hline in (b"\r\n", b"\n"):
                    break
            body = stdin_buf.read(length)
            if not body:
                return None, True
            return json.loads(body.decode("utf-8")), True
        return json.loads(stripped.decode("utf-8")), False


def _write_mcp_response(stdout_buf, resp: dict[str, Any], use_content_length: bool) -> None:
    raw = json.dumps(resp, ensure_ascii=False).encode("utf-8")
    if use_content_length:
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        stdout_buf.write(header + raw)
    else:
        stdout_buf.write(raw + b"\n")
    stdout_buf.flush()


def main() -> None:
    client = WorldBoxBridgeClient()
    stdin_buf = sys.stdin.buffer
    stdout_buf = sys.stdout.buffer
    while True:
        try:
            msg, framed = _read_mcp_stream(stdin_buf)
        except Exception:
            continue
        if msg is None:
            break
        resp = handle_jsonrpc_message(client, msg)
        if resp is not None:
            _write_mcp_response(stdout_buf, resp, framed)


if __name__ == "__main__":
    main()
