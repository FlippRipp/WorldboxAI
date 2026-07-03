"""Drop-in substitute for the `sqlite_vec` PyPI package on platforms
without a wheel — in practice Termux/Android, where pip fails with
"No matching distribution found for sqlite-vec".

The repo bundles the official prebuilt Android loadable extensions from
the upstream GitHub release under vendor/sqlite-vec/ (see the README
there for provenance and checksums). memory.py imports this module when
the real package is unavailable.

Set SQLITE_VEC_PATH to a vec0 shared library to override the bundled
binaries on any platform.
"""

import os
import platform
import struct
import sys

_VENDOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "vendor",
    "sqlite-vec",
)


def _extension_path() -> str:
    override = os.environ.get("SQLITE_VEC_PATH")
    if override:
        return override

    # sys.getandroidapilevel only exists in CPython builds targeting
    # Android (Termux, proot does not need this fallback).
    if not hasattr(sys, "getandroidapilevel"):
        raise ImportError(
            "The sqlite-vec package is not installed and no bundled build "
            "matches this platform. Install it with "
            "'pip install sqlite-vec' or point SQLITE_VEC_PATH at a vec0 "
            "shared library."
        )

    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        arch = "android-aarch64"
    elif machine.startswith("arm"):
        arch = "android-armv7a"
    elif machine == "x86_64":
        arch = "android-x86_64"
    else:
        arch = "android-i686"

    path = os.path.join(_VENDOR_DIR, arch, "vec0.so")
    if not os.path.isfile(path):
        raise ImportError(f"Bundled sqlite-vec extension not found: {path}")
    return path


def load(conn) -> None:
    """Load the vec0 extension into a sqlite3 connection.
    The caller must have enable_load_extension(True) active."""
    conn.load_extension(_extension_path())


def serialize_float32(vector) -> bytes:
    return struct.pack("%sf" % len(vector), *vector)
