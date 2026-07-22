#!/usr/bin/env python3
"""WorldBox install helper -- a tiny companion server for a remote SD WebUI.

Runs on the machine that hosts the A1111/Forge WebUI (image_server.sh/.bat
start it automatically alongside the WebUI) and gives the WorldBox app two
things its own backend cannot do across machines:

- one-click installs: POST /wb-helper/downloads streams a Civitai/Hugging
  Face file straight into the WebUI's checkpoint or LoRA folder, with byte
  progress, SHA256 verification, and cancel support -- mirroring the app's
  local download pipeline;
- exact availability badges: GET /wb-helper/hashes serves a SHA256 index of
  both folders (cached by size+mtime, rescanned in the background), which the
  app matches against Civitai/Hub file hashes.

Standard library only -- no pip installs on the server machine. Written for
the WebUI's own Python (3.10+); any Python 3.8+ works.

Security model: the helper only ever WRITES whitelisted model files
(.safetensors/.ckpt) into the two folders fixed at startup, and only READS
those folders back as hashes. Folders are not settable over HTTP. On an
untrusted network, set WB_HELPER_TOKEN (or --token) and paste the same token
into the Image Studio -- every request must then carry it as a bearer token.

Usage:
  python3 helper_server.py --checkpoint-dir <models/Stable-diffusion> \
                           --lora-dir <models/Lora> [--upscaler-dir <models/ESRGAN>] \
                           [--text-encoder-dir <models/text_encoder>] \
                           [--vae-dir <models/VAE>] \
                           [--port 7861] [--token T]

Environment overrides (flags win): WB_HELPER_CKPT_DIR, WB_HELPER_LORA_DIR,
WB_HELPER_UPSCALER_DIR, WB_HELPER_TE_DIR, WB_HELPER_VAE_DIR (the last three
derived from the checkpoint dir when unset — text encoder and VAE folders
hold Anima's Qwen modules on Forge Neo), WB_HELPER_PORT (default 7861),
WB_HELPER_LISTEN (0 binds to 127.0.0.1 only), WB_HELPER_TOKEN,
WB_HELPER_CACHE (hash-cache file path).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HELPER_VERSION = 4
DEFAULT_PORT = 7861
DOWNLOAD_CHUNK = 1 << 20
DOWNLOADS_KEEP_FINISHED = 20
MAX_BODY_BYTES = 1 << 16
INSTALL_EXTS = (".safetensors", ".ckpt")
# Hires-fix upscalers are .pth/.pt ESRGAN files; VAEs may be legacy .pt.
# Each such kind gets its own whitelist (same rules as the app's local
# installs); everything else uses INSTALL_EXTS.
KIND_INSTALL_EXTS = {"upscaler": (".pth", ".pt", ".safetensors"),
                     "vae": (".safetensors", ".ckpt", ".pt")}
KIND_DEFAULT_EXT = {"upscaler": ".pth"}
SCAN_EXTS = (".safetensors", ".ckpt", ".pt", ".pth")
# Same parsing as the app's backend.py (kept in sync): prefer the plain
# filename="x" parameter, decode the RFC 5987 filename*=charset''… form —
# naively capturing the starred form used to install Hugging Face files
# under a mangled "UTF-8_" prefix.
_CD_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";]+)', re.IGNORECASE)
_CD_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*[^;']*'[^;']*'([^;]+)",
                                  re.IGNORECASE)

# Populated by main() before the server starts.
KIND_DIRS: dict = {}        # "checkpoint"/"lora"/"upscaler" -> Path | None
AUTH_TOKEN = ""
CACHE_PATH: "Path | None" = None

_downloads: dict = {}       # id -> status dict (same shape as the app's)
_cancel_events: dict = {}   # id -> threading.Event
_downloads_lock = threading.Lock()

_hash_cache = {"files": {}}      # path -> {size, mtime, sha256, kind}
_hash_lock = threading.Lock()    # guards _hash_cache and CACHE_PATH writes
_scan_state = {"running": False, "done_once": False}


def log(msg: str) -> None:
    print(f"[wb-helper] {msg}", flush=True)


# --------------------------------------------------------------------------
# Hash index (mirrors the app's size+mtime-cached folder scan)
# --------------------------------------------------------------------------

def _load_cache() -> None:
    if CACHE_PATH is None or not CACHE_PATH.exists():
        return
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            _hash_cache["files"] = data["files"]
    except (json.JSONDecodeError, OSError) as e:
        log(f"could not read hash cache: {e}")


def _save_cache() -> None:
    if CACHE_PATH is None:
        return
    try:
        tmp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"files": _hash_cache["files"]}, f)
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        log(f"could not write hash cache: {e}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_folders() -> None:
    """Hash every model file under both folders, reusing cached digests for
    files whose size+mtime are unchanged. Multi-GB checkpoints make the first
    scan slow, which is why this always runs on a background thread."""
    files: dict = {}
    for kind, folder in KIND_DIRS.items():
        if folder is None or not folder.is_dir():
            continue
        for path in sorted(p for p in folder.rglob("*")
                           if p.is_file() and p.suffix.lower() in SCAN_EXTS):
            try:
                stat = path.stat()
                with _hash_lock:
                    prev = _hash_cache["files"].get(str(path))
                if (isinstance(prev, dict) and prev.get("size") == stat.st_size
                        and prev.get("mtime") == stat.st_mtime):
                    files[str(path)] = dict(prev, kind=kind)
                    continue
                digest = _sha256_file(path)
            except OSError as e:
                log(f"could not hash {path}: {e}")
                continue
            files[str(path)] = {"size": stat.st_size, "mtime": stat.st_mtime,
                                "sha256": digest, "kind": kind}
    with _hash_lock:
        _hash_cache["files"] = files
        _save_cache()
    _scan_state["done_once"] = True


def _spawn_scan() -> None:
    if _scan_state["running"]:
        return
    _scan_state["running"] = True

    def run():
        try:
            _scan_folders()
        finally:
            _scan_state["running"] = False

    threading.Thread(target=run, daemon=True, name="wb-helper-scan").start()


def _register_file(path: Path, kind: str) -> None:
    """Add one just-downloaded file to the cache without a full rescan."""
    try:
        stat = path.stat()
        digest = _sha256_file(path)
    except OSError as e:
        log(f"could not hash {path}: {e}")
        return
    with _hash_lock:
        _hash_cache["files"][str(path)] = {
            "size": stat.st_size, "mtime": stat.st_mtime,
            "sha256": digest, "kind": kind}
        _save_cache()


def _hash_indexes() -> dict:
    """{"checkpoint": {sha256 -> file stem}, "lora": {...}, "upscaler":
    {...}, "text_encoder": {...}, "vae": {...}} from the cache."""
    out: dict = {"checkpoint": {}, "lora": {}, "upscaler": {},
                 "text_encoder": {}, "vae": {}}
    with _hash_lock:
        entries = list(_hash_cache["files"].items())
    for file_path, meta in entries:
        kind = (meta or {}).get("kind")
        sha = str((meta or {}).get("sha256") or "").lower()
        if kind in out and sha:
            out[kind][sha] = Path(file_path).stem
    return out


# --------------------------------------------------------------------------
# Downloads (mirrors the app's _download_file_pipeline, with threads)
# --------------------------------------------------------------------------

def _content_disposition_filename(disposition: str) -> str:
    """The filename a Content-Disposition header carries, or ""."""
    match = _CD_FILENAME_RE.search(disposition or "")
    if match:
        return match.group(1).strip()
    match = _CD_FILENAME_STAR_RE.search(disposition or "")
    if match:
        return urllib.parse.unquote(match.group(1).strip().strip('"'))
    return ""


def _safe_filename(raw: str, fallback: str, kind: str = "lora") -> str:
    """A bare, whitelisted-extension filename that cannot escape the install
    folder (same rules as the app's local installs, per kind)."""
    exts = KIND_INSTALL_EXTS.get(kind, INSTALL_EXTS)
    name = Path(str(raw or "").strip().replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if not name or not name.lower().endswith(exts):
        base = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(fallback or "model")).strip(" .")
        if base.lower().endswith(exts):
            name = base
        else:
            name = (base or "model") + KIND_DEFAULT_EXT.get(kind, ".safetensors")
    return name


def _prune_downloads() -> None:
    finished = sorted(
        (d for d in _downloads.values() if d["status"] != "downloading"),
        key=lambda d: float(d.get("completed_at") or 0))
    if len(finished) > DOWNLOADS_KEEP_FINISHED:
        for stale in finished[:-DOWNLOADS_KEEP_FINISHED]:
            _downloads.pop(stale["id"], None)
            _cancel_events.pop(stale["id"], None)


def _download_worker(dl_id: str, url: str, dest_dir: Path, fallback_name: str,
                     expected_hashes: list, kind: str) -> None:
    status = _downloads[dl_id]
    cancel = _cancel_events[dl_id]
    part_path = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "worldbox-helper"})
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 401 and "civitai.com" in url:
                raise RuntimeError("Civitai requires an API key for this "
                                   "download — add one in Image Studio")
            raise RuntimeError(f"Download failed (HTTP {e.code})")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Download failed: {e.reason}")
        with resp:
            disposition = resp.headers.get("content-disposition", "")
            filename = _safe_filename(_content_disposition_filename(disposition),
                                      fallback_name, kind=kind)
            final_path = (dest_dir / filename).resolve()
            if dest_dir.resolve() not in final_path.parents:
                raise RuntimeError("Refusing a filename outside the install folder")
            status["filename"] = filename
            status["total_bytes"] = int(resp.headers.get("content-length") or 0)

            part_path = final_path.with_suffix(final_path.suffix + ".part")
            digest = hashlib.sha256()
            received = 0
            with open(part_path, "wb") as f:
                while True:
                    if cancel.is_set():
                        raise RuntimeError("cancelled")
                    chunk = resp.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    status["received_bytes"] = received

        expected = [str(h).lower() for h in expected_hashes if h]
        if expected and digest.hexdigest().lower() not in expected:
            raise RuntimeError("Downloaded file failed its SHA256 check — "
                               "the source may have served the wrong file")
        os.replace(part_path, final_path)
        part_path = None
        _register_file(final_path, kind)
        status["status"] = "done"
        log(f"installed {kind} {filename}")
    except Exception as e:
        status["status"] = "error"
        status["error"] = str(e)[:300]
        log(f"install {dl_id} failed: {e}")
    finally:
        status["completed_at"] = time.time()
        if part_path is not None:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
        with _downloads_lock:
            _prune_downloads()


def _public_download(status: dict) -> dict:
    # The URL may carry the user's Civitai token — never echo it back.
    return {k: v for k, v in status.items() if k != "url"}


def _start_download(body: dict) -> dict:
    kind = body.get("kind") if body.get("kind") in KIND_DIRS else "lora"
    dest_dir = KIND_DIRS.get(kind)
    if dest_dir is None:
        raise ValueError(
            f"No {kind} folder configured on the helper"
            + (" — update the repo and restart the image_server launcher "
               "(or pass --upscaler-dir)" if kind == "upscaler" else ""))
    if not dest_dir.is_dir():
        # models/ESRGAN often doesn't exist on a fresh WebUI; creating it is
        # exactly what a manual install would do.
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(f"Cannot create {kind} folder {dest_dir}: {e}")
    url = str(body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("Installs need an http(s) URL")

    lora_id = str(body.get("lora_id") or "").strip() or None
    item_id = str(body.get("item_id") or "").strip() or None
    label = str(body.get("label") or "").strip()
    fallback_name = str(body.get("filename") or "").strip() or label or kind
    hashes = body.get("expected_hashes")
    expected = [str(h) for h in hashes] if isinstance(hashes, list) else []

    with _downloads_lock:
        # A second command for the same in-flight file returns the existing
        # download instead of racing it.
        for d in _downloads.values():
            if d["status"] == "downloading" and (
                    (lora_id and d.get("lora_id") == lora_id)
                    or (item_id and d.get("item_id") == item_id)
                    or d.get("url") == url):
                return _public_download(d)

        dl_id = uuid.uuid4().hex[:12]
        _downloads[dl_id] = {
            "id": dl_id, "kind": kind, "label": label, "filename": "",
            "dest_dir": str(dest_dir), "url": url,
            "total_bytes": 0, "received_bytes": 0,
            "status": "downloading", "error": None, "lora_id": lora_id,
            "item_id": item_id,
            "base_model": str(body.get("base_model") or "").strip(),
            "started_at": time.time(), "completed_at": None,
        }
        _cancel_events[dl_id] = threading.Event()
    threading.Thread(
        target=_download_worker,
        args=(dl_id, url, dest_dir, fallback_name, expected, kind),
        daemon=True, name=f"wb-helper-dl-{dl_id}").start()
    return _public_download(_downloads[dl_id])


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"wb-helper/{HELPER_VERSION}"

    def log_message(self, fmt, *args):   # quiet the default per-request lines
        pass

    def _send_json(self, payload, code=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {AUTH_TOKEN}"

    def _route(self, method: str):
        if not self._authorized():
            return self._send_json({"detail": "Invalid or missing helper token"}, 401)
        path = self.path.split("?", 1)[0].rstrip("/")

        if method == "GET" and path == "/wb-helper/health":
            return self._send_json({
                "ok": True, "service": "wb_image_gen_helper",
                "version": HELPER_VERSION, "auth": bool(AUTH_TOKEN),
                "kinds": {
                    kind: {"dir": str(folder) if folder else "",
                           "exists": bool(folder and folder.is_dir())}
                    for kind, folder in KIND_DIRS.items()
                },
            })

        if method == "GET" and path == "/wb-helper/hashes":
            if not _scan_state["done_once"]:
                _spawn_scan()
            return self._send_json({
                "scanning": _scan_state["running"] or not _scan_state["done_once"],
                "kinds": _hash_indexes(),
            })

        if method == "GET" and path == "/wb-helper/downloads":
            with _downloads_lock:
                items = sorted(_downloads.values(),
                               key=lambda d: float(d.get("started_at") or 0),
                               reverse=True)
                return self._send_json(
                    {"downloads": [_public_download(d) for d in items]})

        if method == "POST" and path == "/wb-helper/downloads":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self._send_json({"detail": "Body too large"}, 413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Body must be a JSON object")
                return self._send_json({"download": _start_download(body)})
            except ValueError as e:
                return self._send_json({"detail": str(e)}, 400)
            except json.JSONDecodeError:
                return self._send_json({"detail": "Invalid JSON body"}, 400)

        if method == "DELETE" and path.startswith("/wb-helper/downloads/"):
            dl_id = path.rsplit("/", 1)[-1]
            status = _downloads.get(dl_id)
            if status is None:
                return self._send_json({"detail": "No such download"}, 404)
            event = _cancel_events.get(dl_id)
            if status["status"] == "downloading" and event is not None:
                event.set()
                # The worker notices within one chunk read; report it now.
                status["status"] = "error"
                status["error"] = "cancelled"
            return self._send_json({"download": _public_download(status)})

        return self._send_json({"detail": "Not found"}, 404)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


def main() -> int:
    global AUTH_TOKEN, CACHE_PATH
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--checkpoint-dir",
                        default=os.environ.get("WB_HELPER_CKPT_DIR", ""))
    parser.add_argument("--lora-dir",
                        default=os.environ.get("WB_HELPER_LORA_DIR", ""))
    parser.add_argument("--upscaler-dir",
                        default=os.environ.get("WB_HELPER_UPSCALER_DIR", ""))
    parser.add_argument("--text-encoder-dir",
                        default=os.environ.get("WB_HELPER_TE_DIR", ""))
    parser.add_argument("--vae-dir",
                        default=os.environ.get("WB_HELPER_VAE_DIR", ""))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("WB_HELPER_PORT", DEFAULT_PORT)))
    parser.add_argument("--listen",
                        default=os.environ.get("WB_HELPER_LISTEN", "1"))
    parser.add_argument("--token",
                        default=os.environ.get("WB_HELPER_TOKEN", ""))
    parser.add_argument("--cache",
                        default=os.environ.get("WB_HELPER_CACHE", ""))
    args = parser.parse_args()

    for kind, raw in (("checkpoint", args.checkpoint_dir), ("lora", args.lora_dir),
                      ("upscaler", args.upscaler_dir),
                      ("text_encoder", args.text_encoder_dir),
                      ("vae", args.vae_dir)):
        folder = Path(os.path.expanduser(raw)).resolve() if str(raw).strip() else None
        KIND_DIRS[kind] = folder
        if folder is not None and not folder.is_dir():
            log(f"warning: {kind} folder does not exist yet: {folder}")
    # Unset upscaler / text-encoder / VAE folders derive the WebUI-standard
    # siblings of the checkpoint folder (models/Stable-diffusion ->
    # models/ESRGAN, models/text_encoder, models/VAE), same as the app does
    # for its own local installs.
    ckpt = KIND_DIRS.get("checkpoint")
    if ckpt is not None and ckpt.name.lower() == "stable-diffusion":
        for kind, sibling in (("upscaler", "ESRGAN"),
                              ("text_encoder", "text_encoder"),
                              ("vae", "VAE")):
            if KIND_DIRS.get(kind) is None:
                KIND_DIRS[kind] = ckpt.parent / sibling
    if all(v is None for v in KIND_DIRS.values()):
        log("error: pass --checkpoint-dir and/or --lora-dir")
        return 1

    AUTH_TOKEN = str(args.token or "").strip()
    if args.cache.strip():
        CACHE_PATH = Path(os.path.expanduser(args.cache.strip()))
    else:
        anchor = KIND_DIRS.get("checkpoint") or KIND_DIRS.get("lora")
        CACHE_PATH = anchor.parent / "wb-helper-cache.json"
    _load_cache()
    _spawn_scan()

    host = "127.0.0.1" if str(args.listen) == "0" else "0.0.0.0"
    server = ThreadingHTTPServer((host, args.port), Handler)
    log(f"listening on http://{host}:{args.port} "
        f"(auth {'on' if AUTH_TOKEN else 'off'})")
    for kind, folder in KIND_DIRS.items():
        log(f"{kind} folder: {folder or '(not set)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
