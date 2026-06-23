"""Diagnostic tool: trace module widget endpoint mounting."""
import os, json, sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(BASE_DIR, "modules")):
    BASE_DIR = os.getcwd()
MODULES_DIR = os.path.join(BASE_DIR, "modules")

print("=== Widget Endpoint Diagnostic ===\n")
print(f"Base dir:    {BASE_DIR}")
print(f"Modules dir: {MODULES_DIR}")

if not os.path.isdir(MODULES_DIR):
    print("ERROR: Modules directory not found! Aborting.")
    sys.exit(1)

for item in sorted(os.listdir(MODULES_DIR)):
    mod_path = os.path.join(MODULES_DIR, item)
    if not os.path.isdir(mod_path):
        continue
    manifest_path = os.path.join(mod_path, "manifest.json")
    if not os.path.exists(manifest_path):
        continue
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
    mod_id = manifest["id"]
    print(f"\n--- Module: {mod_id} (dir: {item}) ---")
    print(f"  Registry path:  {mod_path}")

    old_path = os.path.join(MODULES_DIR, mod_id.replace("wb_", ""))
    print(f"  OLD broken path: {old_path}")
    
    for fname in ["widget.jsx", "character_widget.jsx", "widget_settings.jsx"]:
        old_exists = os.path.exists(os.path.join(old_path, fname))
        new_exists = os.path.exists(os.path.join(mod_path, fname))
        mark_old = "[" + ("OK" if old_exists else "MISS") + "]"
        mark_new = "[" + ("OK" if new_exists else "MISS") + "]"
        print(f"  {fname:30s} OLD {mark_old}  NEW {mark_new}")

print("\n=== Analysis ===")
print("If OLD=MISS and NEW=OK for widget.jsx -> server.py fix IS needed. Restart server.")
print("If OLD=OK and NEW=OK -> path logic is fine, issue is elsewhere (check DynamicWidget).")
print("Settings widget uses catch-all endpoint (unaffected by the bug).")
