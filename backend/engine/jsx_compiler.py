"""Server-side dependency bundler and Babel JSX compiler for module UIs.

Walks a module entry `.jsx` file and all of its relative `.jsx`/`.js` imports in
one pass, compiles uncached files to CommonJS via Node + `@babel/standalone`
(matching `moduleLoader.js`'s exact Babel presets), and caches compiled output
in memory keyed by `(realpath, mtime_ns, size)` with an aggregate ETag so mobile
clients never need to download or run the 2.8 MB `@babel/standalone` bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from typing import Any, Optional

_CACHE_VERSION = "v1"

# (file_real, mtime_ns, size) -> compiled CommonJS string
_COMPILED_CACHE: dict[tuple[str, int, int], str] = {}
_DISK_CACHE_DIR = os.path.join(tempfile.gettempdir(), f"worldbox_jsx_cache_{_CACHE_VERSION}")

_COMMENT_RE = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/")
_SPECIFIER_RE = re.compile(
    r"""(?:import[^'"]*?from\s*|import\s*|require\s*\(\s*|export[^'"]*?from\s*)['"]([^'"]+)['"]"""
)

_NODE_TRANSFORM_SCRIPT = r"""
const fs = require('fs');
const path = require('path');
const babelPath = process.argv[1];
const Babel = require(babelPath);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
for (const [key, src] of Object.entries(input)) {
  out[key] = Babel.transform(src, {
    presets: [
      ['env', { modules: 'commonjs' }],
      ['react', { runtime: 'classic' }],
    ],
  }).code;
}
process.stdout.write(JSON.stringify(out));
"""


def find_module_dir(modules_dir: str, mod_id: str) -> Optional[str]:
    """Locate the on-disk directory for `mod_id`, matching `get_module_jsx`."""
    if not os.path.isdir(modules_dir):
        return None
    for item in os.listdir(modules_dir):
        candidate = os.path.join(modules_dir, item)
        if not os.path.isdir(candidate):
            continue
        manifest_path = os.path.join(candidate, "manifest.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                if manifest.get("id") == mod_id:
                    return candidate
            except Exception:
                pass
        base = os.path.basename(candidate)
        if base == mod_id or base.replace("wb_", "") == mod_id:
            return candidate
    return None


def _resolve_relative(from_path: str, spec: str) -> str:
    from_dir = from_path[: from_path.rfind("/")] if "/" in from_path else ""
    segments = [s for s in from_dir.split("/") if s]
    for part in spec.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if segments:
                segments.pop()
        else:
            segments.append(part)
    joined = "/".join(segments)
    return re.sub(r"\.jsx?$", "", joined)


def _find_specifiers(source: str) -> list[str]:
    stripped = _COMMENT_RE.sub("", source)
    seen: list[str] = []
    for m in _SPECIFIER_RE.finditer(stripped):
        spec = m.group(1)
        if spec not in seen:
            seen.append(spec)
    return seen


def _resolve_contained_file(mod_dir: str, normalized_path: str) -> Optional[str]:
    if not normalized_path or ".." in normalized_path.split("/") or "\\" in normalized_path:
        return None
    mod_real = os.path.realpath(mod_dir)
    for ext in (".jsx", ".js"):
        rel = f"{normalized_path}{ext}" if not normalized_path.endswith((".jsx", ".js")) else normalized_path
        candidate = os.path.join(mod_dir, *rel.split("/"))
        file_real = os.path.realpath(candidate)
        if not (file_real == mod_real or file_real.startswith(mod_real + os.sep)):
            return None
        if os.path.isfile(file_real):
            return file_real
    return None


def _batch_compile_jsx(uncached: dict[str, str], base_dir: str) -> Optional[dict[str, str]]:
    """Compile `{key: jsx_source}` via Node + `@babel/standalone`. Returns None if unavailable."""
    if not uncached:
        return {}
    babel_pkg = os.path.join(base_dir, "frontend", "node_modules", "@babel", "standalone")
    if not os.path.exists(babel_pkg):
        return None
    try:
        proc = subprocess.run(
            ["node", "-e", _NODE_TRANSFORM_SCRIPT, babel_pkg],
            input=json.dumps(uncached),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


def build_widget_bundle(
    modules_dir: str,
    base_dir: str,
    mod_id: str,
    entry_file: str = "widget.jsx",
) -> tuple[str, dict[str, Any]]:
    """Collect and compile `entry_file` and its relative imports for `mod_id`.

    Raises `FileNotFoundError` if the module or entry file does not exist.
    Returns `(etag, payload_dict)`.
    """
    if ".." in entry_file or "\\" in entry_file or entry_file.startswith("/"):
        raise FileNotFoundError("Invalid entry filename.")

    mod_dir = find_module_dir(modules_dir, mod_id)
    if not mod_dir:
        raise FileNotFoundError(f"Module {mod_id} not found.")

    entry_norm = re.sub(r"\.jsx?$", "", entry_file)
    if _resolve_contained_file(mod_dir, entry_norm) is None:
        raise FileNotFoundError(f"Entry {entry_file} not found for module {mod_id}.")

    # normalized_path -> (file_real, mtime_ns, size, raw_source, deps_map)
    discovered: dict[str, tuple[str, int, int, str, dict[str, str]]] = {}

    def visit(norm_path: str) -> None:
        if norm_path in discovered:
            return
        file_real = _resolve_contained_file(mod_dir, norm_path)
        if file_real is None:
            return
        st = os.stat(file_real)
        with open(file_real, "r", encoding="utf-8") as f:
            source = f.read()
        rel_deps: dict[str, str] = {}
        for spec in _find_specifiers(source):
            if spec.startswith("."):
                target = _resolve_relative(norm_path, spec)
                if _resolve_contained_file(mod_dir, target) is not None:
                    rel_deps[spec] = target
        discovered[norm_path] = (file_real, st.st_mtime_ns, st.st_size, source, rel_deps)
        for target in rel_deps.values():
            visit(target)

    visit(entry_norm)

    # Aggregate ETag across every file in the dependency graph.
    hasher = hashlib.sha1(f"{_CACHE_VERSION}:{mod_id}:{entry_norm}\n".encode("utf-8"))
    for norm_path in sorted(discovered.keys()):
        _, mtime_ns, size, _, _ = discovered[norm_path]
        hasher.update(f"{norm_path}:{mtime_ns}:{size}\n".encode("utf-8"))
    etag = f'"{hasher.hexdigest()[:20]}"'

    # Check in-memory cache first, then content-addressed disk cache in tempdir
    # so server restarts stay instant without re-spawning Node/Babel.
    uncached: dict[str, str] = {}
    uncached_hashes: dict[str, str] = {}
    for norm_path, (file_real, mtime_ns, size, source, _) in discovered.items():
        mem_key = (file_real, mtime_ns, size)
        if mem_key in _COMPILED_CACHE:
            continue
        src_hash = hashlib.sha1(source.encode("utf-8")).hexdigest()
        disk_file = os.path.join(_DISK_CACHE_DIR, f"{src_hash}.cjs")
        if os.path.isfile(disk_file):
            try:
                with open(disk_file, "r", encoding="utf-8") as df:
                    _COMPILED_CACHE[mem_key] = df.read()
                continue
            except OSError:
                pass
        uncached[norm_path] = source
        uncached_hashes[norm_path] = src_hash

    compiled_batch = _batch_compile_jsx(uncached, base_dir) if uncached else {}
    if compiled_batch is not None:
        if compiled_batch:
            try:
                os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
            except OSError:
                pass
        for norm_path, code in compiled_batch.items():
            file_real, mtime_ns, size, _, _ = discovered[norm_path]
            _COMPILED_CACHE[(file_real, mtime_ns, size)] = code
            src_hash = uncached_hashes.get(norm_path)
            if src_hash:
                try:
                    with open(os.path.join(_DISK_CACHE_DIR, f"{src_hash}.cjs"), "w", encoding="utf-8") as df:
                        df.write(code)
                except OSError:
                    pass

    files_payload: dict[str, dict[str, Any]] = {}
    for norm_path, (file_real, mtime_ns, size, source, rel_deps) in discovered.items():
        cached_code = _COMPILED_CACHE.get((file_real, mtime_ns, size))
        if cached_code is not None:
            files_payload[norm_path] = {"code": cached_code, "deps": rel_deps}
        else:
            files_payload[norm_path] = {"source": source, "deps": rel_deps}

    return etag, {
        "module": mod_id,
        "entry": entry_norm,
        "files": files_payload,
    }
