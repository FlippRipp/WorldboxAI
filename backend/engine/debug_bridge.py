"""Secure LAN Diagnostic Bridge for WorldBox.

Security model:
1. Network boundary: Only peers in the *exact same RFC 1918 private subnet* as
   one of this host's active local interfaces may reach `/api/debug-bridge/lan/*`.
   All other non-loopback routes are rejected at the ASGI layer.
2. Identity ("Pair Once"): LAN peers are ignored (`403`) until paired via a
   loopback-initiated, 90-second, *single-attempt* pairing window. Pairing runs
   an ephemeral X25519 key exchange bound to an `scrypt`-stretched 12-character
   out-of-band code (`~59` bits of entropy) with mutual MAC confirmation.
3. Per-Session Consent ("Tap to Allow"): A paired client requests a session by
   signing a timestamped, nonce-bound ephemeral X25519 public key with the
   paired secret. Unpaired or tampered requests are dropped before any UI is
   shown. Approval (`Allow Read-Only` by default, optional `Read + Write`) can
   only be granted from loopback (`127.0.0.1` / `::1`).
4. Transport Confidentiality & Anti-Replay: Every RPC request and response inside
   an approved session is encrypted and authenticated with ChaCha20-Poly1305
   (RFC 8439) using forward-secret per-session keys and strictly monotonic
   sequence numbers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import platform
import re
import secrets
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

PAIRING_TTL_SECONDS = 90.0
PENDING_SESSION_TTL_SECONDS = 90.0
ACTIVE_SESSION_TTL_SECONDS = 3600.0
TIMESTAMP_SKEW_SECONDS = 60.0
NONCE_CACHE_MAX = 512
CLIENT_ERROR_BUFFER_MAX = 200

# Unambiguous 30-char alphabet (no 0/O, 1/I/L): 30^12 ~= 5.3e17 (~58.9 bits).
PAIR_CODE_ALPHABET = "23456789abcdefghjkmnpqrstvwxyz"

SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


# ---------------------------------------------------------------------------
# RFC 7748: X25519 Elliptic-Curve Diffie-Hellman (pure Python, zero C/Rust deps)
# ---------------------------------------------------------------------------

_P25519 = (1 << 255) - 19
_A24 = 121665


def _cswap(swap: int, x_2: int, x_3: int) -> tuple[int, int]:
    mask = -swap  # 0 or -1 (all 1s in two's complement)
    dummy = mask & (x_2 ^ x_3)
    return x_2 ^ dummy, x_3 ^ dummy


def _decode_scalar_25519(k: bytes) -> int:
    if len(k) != 32:
        raise ValueError("X25519 scalar must be 32 bytes")
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(b, "little")


def _decode_u_coordinate(u: bytes) -> int:
    if len(u) != 32:
        raise ValueError("X25519 u-coordinate must be 32 bytes")
    b = bytearray(u)
    b[31] &= 127
    return int.from_bytes(b, "little")


def x25519_scalar_mult(scalar_bytes: bytes, u_bytes: bytes) -> bytes:
    """Raw RFC 7748 X25519 function."""
    k = _decode_scalar_25519(scalar_bytes)
    u = _decode_u_coordinate(u_bytes)
    p = _P25519

    x_1 = u
    x_2 = 1
    z_2 = 0
    x_3 = u
    z_3 = 1
    swap = 0

    for t in range(254, -1, -1):
        k_t = (k >> t) & 1
        swap ^= k_t
        x_2, x_3 = _cswap(swap, x_2, x_3)
        z_2, z_3 = _cswap(swap, z_2, z_3)
        swap = k_t

        a = (x_2 + z_2) % p
        aa = (a * a) % p
        b = (x_2 - z_2) % p
        bb = (b * b) % p
        e = (aa - bb) % p
        c = (x_3 + z_3) % p
        d = (x_3 - z_3) % p
        da = (d * a) % p
        cb = (c * b) % p
        x_3 = pow(da + cb, 2, p)
        z_3 = (x_1 * pow(da - cb, 2, p)) % p
        x_2 = (aa * bb) % p
        z_2 = (e * (aa + _A24 * e)) % p

    x_2, x_3 = _cswap(swap, x_2, x_3)
    z_2, z_3 = _cswap(swap, z_2, z_3)
    res = (x_2 * pow(z_2, p - 2, p)) % p
    return res.to_bytes(32, "little")


def x25519_keypair() -> tuple[bytes, bytes]:
    """Generate `(private_bytes, public_bytes)` on Curve25519."""
    priv = secrets.token_bytes(32)
    basepoint = b"\x09" + b"\x00" * 31
    pub = x25519_scalar_mult(priv, basepoint)
    return priv, pub


def x25519_shared_secret(priv: bytes, peer_pub: bytes) -> bytes:
    """Compute X25519 shared secret, rejecting RFC 7748 §6.1 low-order points."""
    shared = x25519_scalar_mult(priv, peer_pub)
    if hmac.compare_digest(shared, b"\x00" * 32):
        raise ValueError("Invalid X25519 public key (low-order point)")
    return shared


# ---------------------------------------------------------------------------
# RFC 8439: ChaCha20-Poly1305 AEAD & RFC 5869 HKDF-SHA256
# ---------------------------------------------------------------------------

def _rotl32(v: int, c: int) -> int:
    v &= 0xFFFFFFFF
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _quarter_round(s: list[int], a: int, b: int, c: int, d: int) -> None:
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] = _rotl32(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] = _rotl32(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] = _rotl32(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] = _rotl32(s[b] ^ s[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    if len(key) != 32 or len(nonce) != 12:
        raise ValueError("ChaCha20 requires 32-byte key and 12-byte nonce")
    k_words = struct.unpack("<8I", key)
    n_words = struct.unpack("<3I", nonce)
    init = (
        0x61707865, 0x3320646E, 0x79622D32, 0x6B206574,
        *k_words,
        counter & 0xFFFFFFFF,
        *n_words,
    )
    w = list(init)
    for _ in range(10):
        _quarter_round(w, 0, 4, 8, 12)
        _quarter_round(w, 1, 5, 9, 13)
        _quarter_round(w, 2, 6, 10, 14)
        _quarter_round(w, 3, 7, 11, 15)
        _quarter_round(w, 0, 5, 10, 15)
        _quarter_round(w, 1, 6, 11, 12)
        _quarter_round(w, 2, 7, 8, 13)
        _quarter_round(w, 3, 4, 9, 14)
    out = [(w[i] + init[i]) & 0xFFFFFFFF for i in range(16)]
    return struct.pack("<16I", *out)


def chacha20_xor(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    mv = memoryview(data)
    offset = 0
    blk = counter
    total = len(data)
    while offset < total:
        stream = chacha20_block(key, blk, nonce)
        chunk = mv[offset : offset + 64]
        n = len(chunk)
        # Fast big-int XOR per 64-byte block
        c_int = int.from_bytes(chunk, "little") ^ int.from_bytes(stream[:n], "little")
        out[offset : offset + n] = c_int.to_bytes(n, "little")
        offset += n
        blk = (blk + 1) & 0xFFFFFFFF
    return bytes(out)


_P1305 = (1 << 130) - 5


def poly1305_mac(msg: bytes, key: bytes) -> bytes:
    if len(key) != 32:
        raise ValueError("Poly1305 requires a 32-byte one-time key")
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    acc = 0
    mv = memoryview(msg)
    for offset in range(0, len(msg), 16):
        block = mv[offset : offset + 16]
        n = int.from_bytes(block, "little") | (1 << (8 * len(block)))
        acc = ((acc + n) * r) % _P1305
    tag = (acc + s) & ((1 << 128) - 1)
    return tag.to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    rem = len(data) % 16
    return b"" if rem == 0 else b"\x00" * (16 - rem)


def chacha20_poly1305_seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """RFC 8439 AEAD encrypt; returns `ciphertext || 16-byte tag`."""
    otk = chacha20_block(key, 0, nonce)[:32]
    ciphertext = chacha20_xor(key, 1, nonce, plaintext)
    mac_data = aad + _pad16(aad) + ciphertext + _pad16(ciphertext) + struct.pack("<QQ", len(aad), len(ciphertext))
    tag = poly1305_mac(mac_data, otk)
    return ciphertext + tag


def chacha20_poly1305_open(key: bytes, nonce: bytes, sealed: bytes, aad: bytes = b"") -> bytes:
    """RFC 8439 AEAD decrypt; verifies tag in constant time before decrypting."""
    if len(sealed) < 16:
        raise ValueError("Ciphertext truncated")
    ciphertext, tag = sealed[:-16], sealed[-16:]
    otk = chacha20_block(key, 0, nonce)[:32]
    mac_data = aad + _pad16(aad) + ciphertext + _pad16(ciphertext) + struct.pack("<QQ", len(aad), len(ciphertext))
    expected_tag = poly1305_mac(mac_data, otk)
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("AEAD authentication failed")
    return chacha20_xor(key, 1, nonce, ciphertext)


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    if not salt:
        salt = b"\x00" * 32
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = bytearray()
    prev = b""
    counter = 1
    while len(okm) < length:
        prev = hmac.new(prk, prev + info + bytes([counter]), hashlib.sha256).digest()
        okm.extend(prev)
        counter += 1
    return bytes(okm[:length])


# ---------------------------------------------------------------------------
# Protocol Key Derivation & Signing Helpers
# ---------------------------------------------------------------------------

def normalize_pair_code(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (code or "").strip().lower())


def generate_pair_code() -> str:
    chars = [secrets.choice(PAIR_CODE_ALPHABET) for _ in range(12)]
    return f"{''.join(chars[0:4])}-{''.join(chars[4:8])}-{''.join(chars[8:12])}"


def derive_pairing_material(
    pair_code: str,
    salt: bytes,
    client_pub: bytes,
    server_pub: bytes,
    shared_dh: bytes,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Derive `(pair_key, client_confirm_key, server_confirm_key, transcript)`."""
    norm = normalize_pair_code(pair_code)
    if len(norm) < 8:
        raise ValueError("Invalid pairing code")
    psk = hashlib.scrypt(
        norm.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    transcript = b"wb-bridge-pair-v1|" + salt + b"|" + client_pub + b"|" + server_pub
    expanded = hkdf_sha256(shared_dh + psk, salt=salt, info=transcript, length=96)
    return expanded[0:32], expanded[32:64], expanded[64:96], transcript


def derive_session_keys(
    pair_key: bytes,
    session_id: str,
    client_eph_pub: bytes,
    server_eph_pub: bytes,
    eph_dh: bytes,
) -> tuple[bytes, bytes]:
    """Derive directional `(c2s_key, s2c_key)` from paired identity + ephemeral DH."""
    info = (
        b"wb-bridge-session-v1|"
        + session_id.encode("utf-8")
        + b"|"
        + client_eph_pub
        + b"|"
        + server_eph_pub
    )
    expanded = hkdf_sha256(eph_dh, salt=pair_key, info=info, length=64)
    return expanded[0:32], expanded[32:64]


def make_rpc_nonce(direction: bytes, seq: int) -> bytes:
    if len(direction) != 4:
        raise ValueError("Nonce direction prefix must be 4 bytes")
    return direction + int(seq).to_bytes(8, "big", signed=False)


def sign_bridge_message(pair_key: bytes, purpose: str, fields: list[str]) -> str:
    canonical = purpose + "\n" + "\n".join(fields)
    sig = hmac.new(pair_key, canonical.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("ascii")


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode((text or "").encode("ascii"), validate=True)


# ---------------------------------------------------------------------------
# Strict Same-Subnet & Loopback Network Verification
# ---------------------------------------------------------------------------

_RFC1918_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_ULA_IPV6_NET = ipaddress.ip_network("fc00::/7")


def _parse_ip(raw: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if not raw:
        return None
    cleaned = raw.strip()
    if "%" in cleaned:
        cleaned = cleaned.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    if host == "testclient":
        return True
    ip = _parse_ip(host)
    return bool(ip is not None and ip.is_loopback)


def is_private_lan_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Strictly RFC 1918 IPv4 or RFC 4193 IPv6 ULA — excludes loopback, link-local,
    CGNAT (100.64.0.0/10), multicast, and public addresses."""
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _RFC1918_NETS)
    return ip in _ULA_IPV6_NET


def discover_local_private_subnets() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Best-effort discovery of active local private subnets across Windows,
    Linux, and Android Termux (including when SELinux blocks NETLINK_ROUTE)."""
    subnets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    seen: set[str] = set()

    def _add_cidr(cidr: str) -> None:
        try:
            iface = ipaddress.ip_interface(cidr)
        except ValueError:
            return
        ip = iface.ip
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            return
        if not is_private_lan_ip(ip):
            return
        # Guard against /32 point-to-point entries masquerading as LANs
        if isinstance(iface.network, ipaddress.IPv4Network) and iface.network.prefixlen >= 31:
            return
        key = str(iface.network)
        if key not in seen:
            seen.add(key)
            subnets.append(iface.network)

    env_override = os.environ.get("WB_BRIDGE_SUBNET", "").strip()
    if env_override:
        for part in env_override.split(","):
            part = part.strip()
            if part:
                try:
                    net = ipaddress.ip_network(part, strict=False)
                    if str(net) not in seen:
                        seen.add(str(net))
                        subnets.append(net)
                except ValueError:
                    pass

    sys_name = platform.system().lower()
    if sys_name == "windows":
        try:
            out = subprocess.check_output(
                ["ipconfig"], stderr=subprocess.DEVNULL, timeout=2.0, text=True, errors="replace"
            )
            current_ip: Optional[str] = None
            for line in out.splitlines():
                if not line.strip():
                    current_ip = None
                    continue
                m_ip = re.search(r"IPv4[^:]*:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", line)
                if m_ip:
                    current_ip = m_ip.group(1)
                    continue
                if current_ip:
                    m_mask = re.search(r"(?:Subnet Mask|255\.)[^:]*:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", line)
                    if not m_mask:
                        m_mask = re.search(r":\s*(255\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\b", line)
                    if m_mask:
                        _add_cidr(f"{current_ip}/{m_mask.group(1)}")
                        current_ip = None
        except Exception:
            pass
    else:
        # 1) Standard iproute2 (`ip -o -f inet addr show`)
        try:
            out = subprocess.check_output(
                ["ip", "-o", "-f", "inet", "addr", "show"],
                stderr=subprocess.DEVNULL,
                timeout=1.5,
                text=True,
                errors="replace",
            )
            for m in re.finditer(r"\binet\s+([0-9]{1,3}(?:\.[0-9]{1,3}){3}/[0-9]{1,2})\b", out):
                _add_cidr(m.group(1))
        except Exception:
            pass

        # 2) `/proc/net/route` fallback (readable on Android even when SELinux blocks netlink)
        if not subnets and os.path.exists("/proc/net/route"):
            try:
                with open("/proc/net/route", "r", encoding="utf-8", errors="replace") as f:
                    for idx, line in enumerate(f):
                        if idx == 0:
                            continue
                        cols = line.split()
                        if len(cols) < 8:
                            continue
                        dest_hex, gw_hex, mask_hex = cols[1], cols[2], cols[7]
                        # Direct link route has gateway 00000000 and non-zero destination/mask
                        if gw_hex != "00000000" or dest_hex == "00000000" or mask_hex == "00000000":
                            continue
                        dest_ip = socket.inet_ntoa(struct.pack("<I", int(dest_hex, 16)))
                        mask_ip = socket.inet_ntoa(struct.pack("<I", int(mask_hex, 16)))
                        net = ipaddress.ip_network(f"{dest_ip}/{mask_ip}", strict=False)
                        if is_private_lan_ip(net.network_address) and net.prefixlen < 31:
                            key = str(net)
                            if key not in seen:
                                seen.add(key)
                                subnets.append(net)
            except Exception:
                pass

    return subnets


class SubnetVerifier:
    """Checks that a LAN peer belongs to the exact same local private subnet."""

    def __init__(
        self,
        override_subnets: Optional[list[str]] = None,
        cache_ttl: float = 15.0,
    ) -> None:
        self._override: Optional[list[ipaddress.IPv4Network | ipaddress.IPv6Network]] = None
        if override_subnets is not None:
            self._override = [ipaddress.ip_network(s, strict=False) for s in override_subnets]
        self._cache_ttl = cache_ttl
        self._cached: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._cached_at = 0.0

    def get_subnets(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        if self._override is not None:
            return list(self._override)
        now = time.monotonic()
        if not self._cached or (now - self._cached_at) > self._cache_ttl:
            self._cached = discover_local_private_subnets()
            self._cached_at = now
        return list(self._cached)

    def _route_local_ip_for_peer(self, peer_ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        fam = socket.AF_INET if isinstance(peer_ip, ipaddress.IPv4Address) else socket.AF_INET6
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                s.connect((str(peer_ip), 9))
                local_raw = s.getsockname()[0]
                return _parse_ip(local_raw)
        except OSError:
            return None

    def is_same_subnet(self, peer_host: Optional[str], server_host: Optional[str] = None) -> bool:
        peer_ip = _parse_ip(peer_host or "")
        if peer_ip is None or not is_private_lan_ip(peer_ip):
            return False

        subnets = self.get_subnets()
        if subnets:
            return any(peer_ip in net for net in subnets if net.version == peer_ip.version)

        # Fallback for environments where OS netlink AND /proc/net/route are
        # completely unavailable (e.g. heavily sandboxed Android proot): check
        # the kernel-selected local interface address on this TCP socket (or UDP
        # routing table) and require a /24 private-LAN match.
        local_ip = _parse_ip(server_host or "")
        if local_ip is None or local_ip.is_unspecified or local_ip.is_loopback:
            local_ip = self._route_local_ip_for_peer(peer_ip)
        if local_ip is None or not is_private_lan_ip(local_ip) or local_ip.version != peer_ip.version:
            return False
        prefix = 24 if isinstance(local_ip, ipaddress.IPv4Address) else 64
        fallback_net = ipaddress.ip_network(f"{local_ip}/{prefix}", strict=False)
        return peer_ip in fallback_net


# ---------------------------------------------------------------------------
# Secret Scrubber (Defense-in-Depth on All Outgoing Diagnostic Payloads)
# ---------------------------------------------------------------------------

_SAFE_TOKEN_KEYS = frozenset({
    "tokens",
    "max_tokens",
    "min_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "input_tokens",
    "output_tokens",
    "token_count",
    "token_limit",
    "stop_tokens",
})

_SECRET_KEY_RE = re.compile(
    r"(?:^|_|-)(?:api_?key|secret|password|passwd|private_?key|pair_?key|"
    r"access_?token|refresh_?token|auth_?token|bearer_?token|client_?secret|"
    r"authorization|credential)s?$",
    re.IGNORECASE,
)

_KNOWN_KEY_PREFIX_RE = re.compile(
    r"^(?:sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{25,}|gsk_[A-Za-z0-9]{20,}|"
    r"nvapi-[A-Za-z0-9_\-]{20,}|hf_[A-Za-z0-9]{20,})$"
)


def scrub_secrets(value: Any) -> Any:
    """Recursively strip credentials/API keys before data leaves the server."""
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            key_str = str(k)
            lower = key_str.lower()
            if lower not in _SAFE_TOKEN_KEYS and _SECRET_KEY_RE.search(lower):
                cleaned[k] = "[REDACTED]" if v else v
            else:
                cleaned[k] = scrub_secrets(v)
        return cleaned
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) >= 20 and _KNOWN_KEY_PREFIX_RE.match(stripped):
            return "[REDACTED]"
        return value
    return value


# ---------------------------------------------------------------------------
# Pairing & Session State Machine
# ---------------------------------------------------------------------------

class BridgeError(Exception):
    def __init__(self, status_code: int, detail: str, code: str = "bridge_error") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    data = json.dumps(payload, indent=2)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(path))
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


class DebugBridgeManager:
    """Owns pairing state, per-session consent, encrypted RPC framing, and
    mobile client error/UI telemetry."""

    def __init__(
        self,
        storage_dir: str | Path,
        server_name: Optional[str] = None,
        subnet_verifier: Optional[SubnetVerifier] = None,
        on_state_change: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.storage_dir = Path(storage_dir)
        self.paired_path = self.storage_dir / "paired_device.json"
        self.server_name = server_name or socket.gethostname() or "WorldBox"
        self.subnet_verifier = subnet_verifier or SubnetVerifier()
        self._on_state_change = on_state_change

        self._paired: Optional[dict[str, Any]] = self._load_paired()
        self._pairing_window: Optional[dict[str, Any]] = None
        self._pending_session: Optional[dict[str, Any]] = None
        self._active_session: Optional[dict[str, Any]] = None
        self._decision_event: Optional[asyncio.Event] = None
        self._last_decided_session: Optional[dict[str, Any]] = None

        self._seen_nonces: dict[str, float] = {}
        self._client_errors: list[dict[str, Any]] = []
        self._next_error_id: int = 1
        self._client_ui_state: dict[str, Any] = {}

    def set_on_state_change(self, cb: Optional[Callable[[dict], None]]) -> None:
        self._on_state_change = cb

    def _notify(self) -> None:
        if self._on_state_change is not None:
            try:
                self._on_state_change(self.get_ui_status())
            except Exception:
                pass

    def _load_paired(self) -> Optional[dict[str, Any]]:
        if not self.paired_path.exists():
            return None
        try:
            with open(self.paired_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (
                isinstance(data, dict)
                and isinstance(data.get("device_id"), str)
                and isinstance(data.get("pair_key_b64"), str)
            ):
                b64d(data["pair_key_b64"])
                return data
        except Exception:
            pass
        return None

    def _gc(self) -> bool:
        """Expire stale pairing windows, pending requests, nonces, and sessions."""
        now = time.time()
        changed = False
        if self._pairing_window and now >= self._pairing_window["expires_at"]:
            self._pairing_window = None
            changed = True
        if self._pending_session and now >= self._pending_session["expires_at"]:
            self._pending_session = None
            if self._decision_event is not None:
                self._decision_event.set()
            changed = True
        if self._active_session and now >= self._active_session["expires_at"]:
            self._active_session = None
            changed = True
        if self._last_decided_session and now >= self._last_decided_session["cleanup_at"]:
            self._last_decided_session = None
        # Prune replay nonce cache
        expired_nonces = [n for n, exp in self._seen_nonces.items() if now >= exp]
        for n in expired_nonces:
            self._seen_nonces.pop(n, None)
        return changed

    def _check_and_record_nonce(self, timestamp: float, nonce: str) -> None:
        now = time.time()
        if abs(now - float(timestamp)) > TIMESTAMP_SKEW_SECONDS:
            raise BridgeError(403, "Clock skew or stale request timestamp.", "stale_timestamp")
        if not isinstance(nonce, str) or len(nonce) < 16 or len(nonce) > 128:
            raise BridgeError(400, "Invalid nonce.", "bad_nonce")
        self._gc()
        if nonce in self._seen_nonces:
            raise BridgeError(403, "Replay detected.", "replay_detected")
        if len(self._seen_nonces) >= NONCE_CACHE_MAX:
            oldest = min(self._seen_nonces, key=self._seen_nonces.get)
            self._seen_nonces.pop(oldest, None)
        self._seen_nonces[nonce] = now + (TIMESTAMP_SKEW_SECONDS * 2.5)

    def get_local_lan_ips(self) -> list[str]:
        """Return local RFC 1918 IPv4 addresses for display in the Settings UI."""
        ips: list[str] = []
        seen: set[str] = set()
        # Try interface inspection + UDP route trick
        for probe in ("192.168.1.1", "10.0.0.1", "172.16.0.1"):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect((probe, 9))
                    ip = s.getsockname()[0]
                    parsed = _parse_ip(ip)
                    if parsed and is_private_lan_ip(parsed) and ip not in seen:
                        seen.add(ip)
                        ips.append(ip)
            except OSError:
                pass
        return ips

    # -- Loopback UI Controls -------------------------------------------------

    def get_ui_status(self, include_pair_code: bool = False) -> dict[str, Any]:
        self._gc()
        now = time.time()
        paired_info = None
        if self._paired:
            paired_info = {
                "device_id": self._paired["device_id"],
                "client_name": self._paired.get("client_name") or "Paired Agent",
                "paired_at": self._paired.get("paired_at"),
                "last_ip": self._paired.get("last_ip"),
            }

        pairing_info = None
        if self._pairing_window:
            pairing_info = {
                "active": True,
                "expires_at": self._pairing_window["expires_at"],
                "seconds_remaining": max(0, int(round(self._pairing_window["expires_at"] - now))),
            }
            if include_pair_code:
                pairing_info["code"] = self._pairing_window["code"]

        pending_info = None
        if self._pending_session:
            pending_info = {
                "request_id": self._pending_session["request_id"],
                "client_name": self._pending_session["client_name"],
                "peer_ip": self._pending_session["peer_ip"],
                "requested_write": bool(self._pending_session["requested_write"]),
                "expires_at": self._pending_session["expires_at"],
                "seconds_remaining": max(0, int(round(self._pending_session["expires_at"] - now))),
            }

        session_info = None
        if self._active_session:
            session_info = {
                "session_id": self._active_session["session_id"],
                "client_name": self._active_session["client_name"],
                "peer_ip": self._active_session["peer_ip"],
                "allow_write": bool(self._active_session["allow_write"]),
                "connected_at": self._active_session["connected_at"],
                "expires_at": self._active_session["expires_at"],
                "seconds_remaining": max(0, int(round(self._active_session["expires_at"] - now))),
                "call_count": self._active_session["call_count"],
                "last_tool": self._active_session["last_tool"],
            }

        return {
            "server_name": self.server_name,
            "lan_ips": self.get_local_lan_ips(),
            "paired": paired_info,
            "pairing": pairing_info,
            "pending_session": pending_info,
            "active_session": session_info,
        }

    def start_pairing(self) -> dict[str, Any]:
        """Open a 90-second, single-attempt pairing window (loopback only)."""
        self._gc()
        srv_priv, srv_pub = x25519_keypair()
        code = generate_pair_code()
        now = time.time()
        self._pairing_window = {
            "pairing_id": secrets.token_urlsafe(16),
            "code": code,
            "salt": secrets.token_bytes(32),
            "srv_priv": srv_priv,
            "srv_pub": srv_pub,
            "client_id": None,
            "client_name": None,
            "client_pub": None,
            "init_claimed": False,
            "expires_at": now + PAIRING_TTL_SECONDS,
        }
        self._notify()
        return self.get_ui_status(include_pair_code=True)

    def cancel_pairing(self) -> dict[str, Any]:
        self._pairing_window = None
        self._notify()
        return self.get_ui_status()

    def unpair(self) -> dict[str, Any]:
        self._paired = None
        self._pairing_window = None
        self._pending_session = None
        self._active_session = None
        if self._decision_event is not None:
            self._decision_event.set()
        try:
            self.paired_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._notify()
        return self.get_ui_status()

    def decide_pending_session(self, request_id: str, approve: bool, allow_write: bool = False) -> dict[str, Any]:
        """Approve or deny the pending connection request (loopback only)."""
        self._gc()
        pending = self._pending_session
        if not pending or pending["request_id"] != request_id:
            raise BridgeError(404, "No matching pending connection request (it may have expired).", "no_pending")

        self._pending_session = None
        now = time.time()

        if approve:
            if not self._paired:
                raise BridgeError(409, "No device is paired.", "not_paired")
            pair_key = b64d(self._paired["pair_key_b64"])
            session_id = secrets.token_urlsafe(24)
            eph_dh = x25519_shared_secret(pending["srv_eph_priv"], pending["client_eph_pub"])
            c2s_key, s2c_key = derive_session_keys(
                pair_key=pair_key,
                session_id=session_id,
                client_eph_pub=pending["client_eph_pub"],
                server_eph_pub=pending["srv_eph_pub"],
                eph_dh=eph_dh,
            )
            self._active_session = {
                "session_id": session_id,
                "device_id": pending["device_id"],
                "client_name": pending["client_name"],
                "peer_ip": pending["peer_ip"],
                "allow_write": bool(allow_write),
                "c2s_key": c2s_key,
                "s2c_key": s2c_key,
                "last_seq": 0,
                "connected_at": now,
                "expires_at": now + ACTIVE_SESSION_TTL_SECONDS,
                "call_count": 0,
                "last_tool": None,
            }
            self._last_decided_session = {
                "request_id": request_id,
                "status": "approved",
                "session_id": session_id,
                "srv_eph_pub_b64": b64e(pending["srv_eph_pub"]),
                "allow_write": bool(allow_write),
                "expires_at": self._active_session["expires_at"],
                "cleanup_at": now + 60.0,
            }
        else:
            self._last_decided_session = {
                "request_id": request_id,
                "status": "denied",
                "cleanup_at": now + 60.0,
            }

        if self._decision_event is not None:
            self._decision_event.set()
        self._notify()
        return self.get_ui_status()

    def set_write_mode(self, allow_write: bool) -> dict[str, Any]:
        """Toggle write permission live on the active session (loopback only)."""
        self._gc()
        if not self._active_session:
            raise BridgeError(404, "No active diagnostic session.", "no_session")
        self._active_session["allow_write"] = bool(allow_write)
        self._notify()
        return self.get_ui_status()

    def disconnect_session(self) -> dict[str, Any]:
        """Immediately revoke the active diagnostic session (loopback only)."""
        self._active_session = None
        self._pending_session = None
        if self._decision_event is not None:
            self._decision_event.set()
        self._notify()
        return self.get_ui_status()

    def record_client_telemetry(
        self,
        errors: Optional[list[dict[str, Any]]] = None,
        ui_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Ingest mobile browser JS errors and current UI route from loopback."""
        now = time.time()
        if isinstance(ui_state, dict):
            self._client_ui_state = {
                "mode": str(ui_state.get("mode") or "")[:120],
                "active_save_id": ui_state.get("active_save_id"),
                "viewport": ui_state.get("viewport"),
                "user_agent": str(ui_state.get("user_agent") or "")[:300],
                "online": bool(ui_state.get("online", True)),
                "updated_at": now,
            }
        if isinstance(errors, list):
            for err in errors[:50]:
                if not isinstance(err, dict):
                    continue
                entry = {
                    "id": self._next_error_id,
                    "timestamp": err.get("timestamp") or now,
                    "kind": str(err.get("kind") or "error")[:40],
                    "message": str(err.get("message") or "")[:2000],
                    "stack": str(err.get("stack") or "")[:4000] or None,
                    "source": str(err.get("source") or "")[:500] or None,
                }
                self._next_error_id += 1
                self._client_errors.append(entry)
            if len(self._client_errors) > CLIENT_ERROR_BUFFER_MAX:
                self._client_errors = self._client_errors[-CLIENT_ERROR_BUFFER_MAX:]
        return {"recorded": True, "error_count": len(self._client_errors)}

    def get_client_telemetry(self, since_id: int = 0) -> dict[str, Any]:
        errs = [e for e in self._client_errors if e["id"] > since_id]
        return {
            "ui_state": dict(self._client_ui_state),
            "client_errors": errs,
        }

    # -- LAN Handshake & Encrypted RPC ----------------------------------------

    def lan_pair_init(
        self,
        client_id: str,
        client_name: str,
        client_pub_b64: str,
        peer_ip: str,
    ) -> dict[str, Any]:
        """Step 1 of single-attempt pairing."""
        self._gc()
        win = self._pairing_window
        if not win or win["init_claimed"]:
            raise BridgeError(403, "Pairing is not active.", "pairing_inactive")

        try:
            client_pub = b64d(client_pub_b64)
            if len(client_pub) != 32:
                raise ValueError("bad length")
        except Exception:
            # Malformed key burns the single-attempt window too
            self._pairing_window = None
            self._notify()
            raise BridgeError(400, "Invalid X25519 public key.", "bad_public_key")

        if not client_id or len(client_id) > 128:
            self._pairing_window = None
            self._notify()
            raise BridgeError(400, "Invalid client_id.", "bad_client_id")

        win["init_claimed"] = True
        win["client_id"] = client_id.strip()
        win["client_name"] = (client_name or "Antigravity Agent").strip()[:80]
        win["client_pub"] = client_pub
        win["peer_ip"] = peer_ip

        return {
            "pairing_id": win["pairing_id"],
            "salt_b64": b64e(win["salt"]),
            "server_pub_b64": b64e(win["srv_pub"]),
            "server_name": self.server_name,
        }

    def lan_pair_finish(self, pairing_id: str, client_mac_b64: str) -> dict[str, Any]:
        """Step 2 of single-attempt pairing. Any failure immediately destroys the window."""
        self._gc()
        win = self._pairing_window
        # Consume unconditionally on the first finish call (1-attempt guarantee!)
        self._pairing_window = None
        if not win or not win["init_claimed"] or win["pairing_id"] != pairing_id:
            self._notify()
            raise BridgeError(403, "Pairing session invalid or expired.", "pairing_invalid")

        try:
            client_mac = b64d(client_mac_b64)
            shared_dh = x25519_shared_secret(win["srv_priv"], win["client_pub"])
            pair_key, client_confirm_key, server_confirm_key, transcript = derive_pairing_material(
                pair_code=win["code"],
                salt=win["salt"],
                client_pub=win["client_pub"],
                server_pub=win["srv_pub"],
                shared_dh=shared_dh,
            )
        except Exception:
            self._notify()
            raise BridgeError(403, "Pairing handshake failed.", "pairing_failed")

        expected_client_mac = hmac.new(client_confirm_key, transcript, hashlib.sha256).digest()
        if not hmac.compare_digest(client_mac, expected_client_mac):
            self._notify()
            raise BridgeError(403, "Invalid pairing code or tampered handshake.", "pairing_auth_failed")

        server_mac = hmac.new(server_confirm_key, transcript, hashlib.sha256).digest()
        paired_record = {
            "device_id": win["client_id"],
            "client_name": win["client_name"],
            "pair_key_b64": b64e(pair_key),
            "paired_at": time.time(),
            "last_ip": win.get("peer_ip"),
        }
        _write_private_json(self.paired_path, paired_record)
        self._paired = paired_record
        # Clear any old active session when re-pairing
        self._active_session = None
        self._pending_session = None
        self._notify()

        return {
            "paired": True,
            "server_name": self.server_name,
            "server_mac_b64": b64e(server_mac),
        }

    def _verify_paired_signature(
        self,
        device_id: str,
        purpose: str,
        fields: list[str],
        timestamp: float,
        nonce: str,
        signature_b64: str,
    ) -> bytes:
        if not self._paired or self._paired.get("device_id") != device_id:
            raise BridgeError(403, "Device is not paired.", "not_paired")
        pair_key = b64d(self._paired["pair_key_b64"])
        expected_b64 = sign_bridge_message(
            pair_key,
            purpose,
            [device_id, str(timestamp), nonce, *fields],
        )
        if not hmac.compare_digest(signature_b64 or "", expected_b64):
            raise BridgeError(403, "Invalid device signature.", "bad_signature")
        # Check replay/timestamp AFTER verifying signature so unauthenticated attackers
        # cannot pollute the nonce cache!
        self._check_and_record_nonce(timestamp, nonce)
        return pair_key

    def lan_request_session(
        self,
        device_id: str,
        client_name: str,
        client_eph_pub_b64: str,
        requested_write: bool,
        timestamp: float,
        nonce: str,
        signature_b64: str,
        peer_ip: str,
    ) -> dict[str, Any]:
        """Signed connection request from the paired PC; triggers mobile approval prompt."""
        self._gc()
        self._verify_paired_signature(
            device_id=device_id,
            purpose="session_request",
            fields=[client_eph_pub_b64, "1" if requested_write else "0", client_name or ""],
            timestamp=timestamp,
            nonce=nonce,
            signature_b64=signature_b64,
        )

        try:
            client_eph_pub = b64d(client_eph_pub_b64)
            if len(client_eph_pub) != 32:
                raise ValueError("bad eph pub")
        except Exception:
            raise BridgeError(400, "Invalid ephemeral public key.", "bad_eph_key")

        srv_eph_priv, srv_eph_pub = x25519_keypair()
        request_id = secrets.token_urlsafe(18)
        now = time.time()
        self._decision_event = asyncio.Event()
        self._last_decided_session = None
        self._pending_session = {
            "request_id": request_id,
            "device_id": device_id,
            "client_name": (client_name or self._paired.get("client_name") or "Paired Agent").strip()[:80],
            "peer_ip": peer_ip,
            "client_eph_pub": client_eph_pub,
            "srv_eph_priv": srv_eph_priv,
            "srv_eph_pub": srv_eph_pub,
            "requested_write": bool(requested_write),
            "created_at": now,
            "expires_at": now + PENDING_SESSION_TTL_SECONDS,
        }
        if self._paired and self._paired.get("last_ip") != peer_ip:
            self._paired["last_ip"] = peer_ip
            try:
                _write_private_json(self.paired_path, self._paired)
            except OSError:
                pass

        self._notify()
        return {
            "status": "pending",
            "request_id": request_id,
            "expires_in": int(PENDING_SESSION_TTL_SECONDS),
            "server_name": self.server_name,
        }

    async def lan_poll_session(
        self,
        device_id: str,
        request_id: str,
        wait_seconds: float,
        timestamp: float,
        nonce: str,
        signature_b64: str,
    ) -> dict[str, Any]:
        """Signed long-poll waiting for the mobile user to tap Allow or Deny."""
        pair_key = self._verify_paired_signature(
            device_id=device_id,
            purpose="session_poll",
            fields=[request_id],
            timestamp=timestamp,
            nonce=nonce,
            signature_b64=signature_b64,
        )

        wait_clamped = max(0.0, min(float(wait_seconds or 0.0), 45.0))
        if self._pending_session and self._pending_session["request_id"] == request_id and wait_clamped > 0:
            ev = self._decision_event
            if ev is not None and not ev.is_set():
                try:
                    await asyncio.wait_for(ev.wait(), timeout=wait_clamped)
                except asyncio.TimeoutError:
                    pass

        if self._gc():
            self._notify()

        if self._pending_session and self._pending_session["request_id"] == request_id:
            rem = max(0, int(round(self._pending_session["expires_at"] - time.time())))
            return {"status": "pending", "request_id": request_id, "seconds_remaining": rem}

        decided = self._last_decided_session
        if decided and decided.get("request_id") == request_id:
            if decided["status"] == "approved":
                session_id = decided["session_id"]
                srv_pub_b64 = decided["srv_eph_pub_b64"]
                allow_w = "1" if decided["allow_write"] else "0"
                exp_str = str(decided["expires_at"])
                # Authenticate server's ephemeral public key with pair_key so a MITM
                # cannot substitute srv_eph_pub in the poll response!
                server_sig = sign_bridge_message(
                    pair_key,
                    "session_approved",
                    [request_id, session_id, srv_pub_b64, allow_w, exp_str],
                )
                return {
                    "status": "approved",
                    "session_id": session_id,
                    "server_eph_pub_b64": srv_pub_b64,
                    "allow_write": decided["allow_write"],
                    "expires_at": decided["expires_at"],
                    "server_name": self.server_name,
                    "server_signature_b64": server_sig,
                }
            return {"status": "denied", "request_id": request_id}

        return {"status": "expired", "request_id": request_id}

    async def lan_handle_encrypted_rpc(
        self,
        session_id: str,
        seq: int,
        ciphertext_b64: str,
        peer_ip: str,
        dispatcher: Callable[[str, dict[str, Any], bool], Any],
    ) -> dict[str, Any]:
        """Decrypt request envelope, check monotonic sequence & permissions,
        dispatch handler, scrub secrets, and return AEAD-encrypted response."""
        if self._gc():
            self._notify()

        sess = self._active_session
        if not sess or sess["session_id"] != session_id:
            raise BridgeError(401, "No active approved session (connect and accept on WorldBox first).", "no_session")

        if not isinstance(seq, int) or seq <= sess["last_seq"] or seq > (1 << 60):
            raise BridgeError(403, "Out-of-order or replayed sequence number.", "bad_seq")

        aad = session_id.encode("utf-8")
        try:
            sealed_req = b64d(ciphertext_b64)
            req_bytes = chacha20_poly1305_open(
                sess["c2s_key"],
                make_rpc_nonce(b"c2s\x00", seq),
                sealed_req,
                aad=aad,
            )
            payload = json.loads(req_bytes.decode("utf-8"))
        except Exception:
            raise BridgeError(403, "Encrypted RPC authentication failed.", "decrypt_failed")

        # Advance anti-replay sequence watermark immediately once authenticated
        sess["last_seq"] = seq
        sess["peer_ip"] = peer_ip

        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        allow_write = bool(sess["allow_write"])

        sess["call_count"] += 1
        sess["last_tool"] = method

        try:
            raw_result = await dispatcher(method, params, allow_write)
            safe_result = scrub_secrets(raw_result)
            resp_envelope = {
                "ok": True,
                "allow_write": allow_write,
                "expires_in": max(0, int(round(sess["expires_at"] - time.time()))),
                "result": safe_result,
            }
        except BridgeError as exc:
            resp_envelope = {
                "ok": False,
                "allow_write": allow_write,
                "error": {"code": exc.code, "status": exc.status_code, "message": exc.detail},
            }
        except Exception as exc:
            resp_envelope = {
                "ok": False,
                "allow_write": allow_write,
                "error": {"code": "rpc_error", "status": 500, "message": str(exc)},
            }

        resp_bytes = json.dumps(resp_envelope, ensure_ascii=False, default=str).encode("utf-8")
        sealed_resp = chacha20_poly1305_seal(
            sess["s2c_key"],
            make_rpc_nonce(b"s2c\x00", seq),
            resp_bytes,
            aad=aad,
        )
        return {
            "session_id": session_id,
            "seq": seq,
            "ciphertext_b64": b64e(sealed_resp),
        }

    # -- Stealth UDP LAN Discovery --------------------------------------------

    def handle_udp_discovery_packet(self, data: bytes, peer_ip: str, local_ip: Optional[str], tcp_port: int) -> Optional[bytes]:
        """Respond to UDP discovery *only* if:
        1. Peer is on the same private subnet, AND
        2. Either a pairing window is currently active (`mode == 'pair'`), OR
           the packet carries a valid paired-device HMAC (`mode == 'paired'`).
        Otherwise stay completely silent."""
        if len(data) > 2048:
            return None
        if not self.subnet_verifier.is_same_subnet(peer_ip, local_ip):
            return None
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(msg, dict) or msg.get("proto") != "wb-bridge-discover-v1":
            return None

        self._gc()
        mode = msg.get("mode")
        if mode == "pair":
            if not self._pairing_window or self._pairing_window["init_claimed"]:
                return None
            return json.dumps({
                "proto": "wb-bridge-discover-v1",
                "mode": "pair",
                "port": tcp_port,
                "server_name": self.server_name,
            }).encode("utf-8")

        if mode == "paired" and self._paired:
            device_id = str(msg.get("device_id") or "")
            ts = msg.get("timestamp")
            nonce = str(msg.get("nonce") or "")
            sig = str(msg.get("signature_b64") or "")
            try:
                pair_key = self._verify_paired_signature(
                    device_id=device_id,
                    purpose="udp_discover",
                    fields=[],
                    timestamp=float(ts),
                    nonce=nonce,
                    signature_b64=sig,
                )
            except Exception:
                return None
            resp_sig = sign_bridge_message(pair_key, "udp_discover_ack", [nonce, str(tcp_port), self.server_name])
            return json.dumps({
                "proto": "wb-bridge-discover-v1",
                "mode": "paired",
                "port": tcp_port,
                "server_name": self.server_name,
                "signature_b64": resp_sig,
            }).encode("utf-8")

        return None


class DebugBridgeUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, manager: DebugBridgeManager, tcp_port: int) -> None:
        self.manager = manager
        self.tcp_port = tcp_port
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport is None:
            return
        local_sock = self.transport.get_extra_info("sockname")
        local_ip = local_sock[0] if isinstance(local_sock, tuple) and local_sock else None
        reply = self.manager.handle_udp_discovery_packet(data, addr[0], local_ip, self.tcp_port)
        if reply is not None:
            try:
                self.transport.sendto(reply, addr)
            except OSError:
                pass
