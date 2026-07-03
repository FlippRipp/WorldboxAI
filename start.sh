#!/usr/bin/env bash
# WorldBox AI RPG Engine - Linux/macOS startup (counterpart to start.bat)
set -u
cd "$(dirname "$0")"

# Backend port; exported so main.py and the Vite proxy pick it up.
WB_PORT="${WB_PORT:-8321}"
export WB_PORT

echo "=============================================="
echo "      WorldBox AI RPG Engine - Startup"
echo "=============================================="
echo

# ── Update project from git ──
UPDATED=0
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Checking for updates..."
    BEFORE=$(git rev-parse HEAD 2>/dev/null || true)
    if git pull --ff-only; then
        AFTER=$(git rev-parse HEAD 2>/dev/null || true)
        if [ -n "$BEFORE" ] && [ "$BEFORE" != "$AFTER" ]; then
            UPDATED=1
            echo "Project updated. Dependencies will be refreshed."
        fi
    else
        echo "[WARN] git pull failed (offline, local changes, or diverged branch). Starting current version."
    fi
    echo
else
    echo "[WARN] git not found or not a repository. Skipping update check."
    echo
fi

# ── Preflight: Python virtual environment ──
IS_ANDROID=0
[ "$(uname -o 2>/dev/null)" = "Android" ] && IS_ANDROID=1

PY=./venv/bin/python
if [ ! -x "$PY" ]; then
    if [ "$IS_ANDROID" = "1" ] && command -v python >/dev/null 2>&1; then
        # Bare Termux: system python is the supported setup (see
        # docs/SETUP_ANDROID_TERMUX.md, Route B).
        PY=python
    else
        echo "[ERROR] Python virtual environment not found at ./venv"
        echo "Run:  python3 -m venv venv"
        echo "Then: ./venv/bin/pip install -r requirements.txt"
        exit 1
    fi
fi

# ── Install/refresh Python dependencies ──
install_python_deps() {
    if [ "$IS_ANDROID" = "1" ]; then
        # No Android builds exist for sqlite-vec (bundled in vendor/) or
        # numba (worldgen falls back to numpy). TUR provides prebuilt
        # wheels for the heavy packages; --prefer-binary stops pip from
        # picking a newer source-only release over a TUR wheel, which
        # would mean an hours-long (or failing) on-device compile.
        if ! "$PY" -c "import numpy" >/dev/null 2>&1; then
            echo "[HINT] 'pkg install python-numpy python-scipy python-pillow'"
            echo "       installs the heavy packages prebuilt in seconds;"
            echo "       pip may otherwise try to compile them from source."
        fi
        REQ_TMP=$(mktemp)
        grep -vE '^(sqlite-vec|numba)' requirements.txt > "$REQ_TMP"
        "$PY" -m pip install --prefer-binary \
            --extra-index-url https://termux-user-repository.github.io/pypi/ \
            -r "$REQ_TMP"
        rm -f "$REQ_TMP"
    else
        "$PY" -m pip install -r requirements.txt
    fi
}

if [ "$UPDATED" = "1" ]; then
    echo "Refreshing Python dependencies..."
    install_python_deps
    echo
elif ! "$PY" -c "import fastapi, uvicorn, langgraph, litellm, numpy" >/dev/null 2>&1; then
    echo "Python dependencies missing or incomplete. Installing..."
    install_python_deps
    echo
fi

# ── Preflight: backend .env ──
if [ ! -f ./backend/.env ]; then
    echo "[WARN] backend/.env not found."
    echo "Copy backend/.env.example to backend/.env and set GEMINI_API_KEY for live LLM calls."
    echo
fi

# ── Preflight: npm ──
if ! command -v npm >/dev/null 2>&1; then
    echo "[ERROR] npm not found on PATH. Install Node.js first."
    exit 1
fi

# ── Preflight: frontend dependencies ──
if [ ! -d ./frontend/node_modules ] || [ "$UPDATED" = "1" ]; then
    echo "Installing/refreshing frontend dependencies..."
    (cd frontend && npm install) || { echo "[ERROR] npm install failed."; exit 1; }
    echo
fi

# ── Cleanup handler (idempotent; runs on exit and Ctrl+C) ──
BACKEND_LOG=backend_output.log
cleanup() {
    if [ -f .backend_pid.tmp ]; then
        KILL_PID=$(cat .backend_pid.tmp 2>/dev/null)
        if [ -n "$KILL_PID" ]; then
            kill "$KILL_PID" 2>/dev/null && echo "Backend process ($KILL_PID) stopped."
        fi
        rm -f .backend_pid.tmp
    fi
    rm -f "$BACKEND_LOG"
}
trap cleanup EXIT

# ── Delete stale PID file, start backend in background ──
rm -f .backend_pid.tmp
echo "[1/2] Starting Python Backend (port $WB_PORT)..."
"$PY" main.py > "$BACKEND_LOG" 2>&1 &

# ── Wait for backend to write its PID file ──
RETRIES=0
while [ ! -f .backend_pid.tmp ]; do
    RETRIES=$((RETRIES + 1))
    if [ "$RETRIES" -gt 10 ]; then
        echo "[ERROR] Backend process failed to start."
        cat "$BACKEND_LOG" 2>/dev/null
        exit 1
    fi
    sleep 1
done

# ── Wait for backend health endpoint ──
echo "Waiting for backend to be ready..."
health_ok() {
    "$PY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$WB_PORT/api/health', timeout=2)" >/dev/null 2>&1
}
RETRIES=0
until health_ok; do
    RETRIES=$((RETRIES + 1))
    if [ "$RETRIES" -gt 30 ]; then
        echo
        echo "[ERROR] Backend failed to respond after 30 seconds."
        echo "Last backend output:"
        cat "$BACKEND_LOG" 2>/dev/null
        exit 1
    fi
    printf "."
    sleep 1
done

"$PY" - <<'EOF'
import json, os, urllib.request
port = os.environ.get("WB_PORT", "8321")
h = json.load(urllib.request.urlopen("http://127.0.0.1:%s/api/health" % port, timeout=5))
print("  Mode: " + str(h.get("llm_mode")))
print("  Modules: " + str(len(h.get("modules", []))))
print("  Memory: " + str(h.get("memory", {}).get("status")))
EOF
echo "Backend ready!"
echo

# ── Start frontend in foreground ──
echo "[2/2] Starting React Frontend (Vite)..."
echo
echo "==== Open http://localhost:5173 in your browser        ===="
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "${LAN_IP:-}" ]; then
    echo "==== On the same network, other devices can connect at: ===="
    echo "====   http://$LAN_IP:5173"
fi
echo "==== Press Ctrl+C to stop both servers                  ===="
echo

(cd frontend && npm run dev)

echo
echo "Shutting down backend..."
trap - EXIT
cleanup
echo "WorldBox stopped."
