"""Probe widget endpoints on the running server."""
import os
import urllib.request
import sys

BASE = "http://localhost:%s" % os.environ.get("WB_PORT", "8321")
modules = [
    ("wb_core_rpg", "widget.jsx"),
    ("wb_core_rpg", "widget_settings.jsx"),
    ("wb_core_rpg", "character_widget.jsx"),
]

for mod_id, fname in modules:
    url = f"{BASE}/widgets/{mod_id}/{fname}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            print(f"[{resp.status}] {url} ({len(body)} bytes)")
    except urllib.error.HTTPError as e:
        print(f"[{e.code}] {url} - NOT FOUND (server needs restart?)")
    except urllib.error.URLError as e:
        print(f"[FAIL] {url} - {e.reason} (server not running?)")
        break
    except Exception as e:
        print(f"[ERR]  {url} - {e}")
