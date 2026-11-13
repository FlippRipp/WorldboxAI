#!/usr/bin/env bash
# WorldBox AI RPG Engine - Faster Qwen3-TTS server setup/launch (counterpart to tts_server.bat)
#
# Sets up and launches the standalone Faster Qwen3-TTS streaming provider server
# for the wb_tts module. Can run on the same machine as WorldBox or on a
# dedicated GPU machine over LAN / Tailscale.
#
# Usage (from the repo root):
#   ./modules/wb_tts/tts_server.sh [install_dir]
#
# Environment overrides:
#   WB_TTS_DIR        install directory (default: <repo root>/tts_server)
#   WB_TTS_MODEL_DIR  existing Qwen3-TTS-12Hz-1.7B-Base folder (skips download)
#   WB_TTS_PORT       port to listen on (default: 7864)
#   WB_TTS_LISTEN     1 (default) binds 0.0.0.0; 0 binds 127.0.0.1 only
#   WB_TTS_TOKEN      optional bearer token; paste the same value into Voice Server
#   WB_TTS_TORCH_URL  PyTorch wheel index (default: https://download.pytorch.org/whl/cu128)
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$MODULE_DIR/../.." && pwd)"
cd "$REPO_ROOT"

WB_TTS_PORT="${WB_TTS_PORT:-7864}"
WB_TTS_LISTEN="${WB_TTS_LISTEN:-1}"
WB_TTS_TORCH_URL="${WB_TTS_TORCH_URL:-https://download.pytorch.org/whl/cu128}"
TTS_DIR="${1:-${WB_TTS_DIR:-$REPO_ROOT/tts_server}}"
WB_TTS_MODEL_DIR="${WB_TTS_MODEL_DIR:-$TTS_DIR/models/Qwen3-TTS-12Hz-1.7B-Base}"

echo "=============================================="
echo "  WorldBox - Faster Qwen3-TTS Provider Server"
echo "=============================================="
echo

PY_CMD=""
for cand in python3.12 python3.11 python3.10 python3.13 python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
    case "$ver" in
        3.10|3.11|3.12|3.13) PY_CMD="$cand"; break ;;
    esac
done

if [ -z "$PY_CMD" ]; then
    echo "[ERROR] Python 3.10+ not found on PATH. Install Python 3.11 or 3.12 first."
    exit 1
fi

VENV_DIR="$TTS_DIR/venv"
VENV_PY="$VENV_DIR/bin/python"
STAMP_FILE="$VENV_DIR/.wb_tts_ready_v1"

if [ ! -x "$VENV_PY" ]; then
    echo "[wb-tts] Creating virtual environment in $VENV_DIR ..."
    mkdir -p "$TTS_DIR"
    "$PY_CMD" -m venv "$VENV_DIR"
fi

if [ ! -f "$STAMP_FILE" ]; then
    echo "[wb-tts] Installing CUDA PyTorch and Faster Qwen3-TTS dependencies..."
    "$VENV_PY" -m pip install --upgrade pip
    "$VENV_PY" -m pip install torch torchaudio --index-url "$WB_TTS_TORCH_URL"
    "$VENV_PY" -m pip install "faster-qwen3-tts>=0.3.0" fastapi uvicorn pydantic numpy soundfile huggingface_hub static-ffmpeg || \
        "$VENV_PY" -m pip install "git+https://github.com/andimarafioti/faster-qwen3-tts.git" fastapi uvicorn pydantic numpy soundfile huggingface_hub static-ffmpeg
    echo "ok" > "$STAMP_FILE"
fi

LISTEN_IP="0.0.0.0"
if [ "$WB_TTS_LISTEN" = "0" ]; then
    LISTEN_IP="127.0.0.1"
fi

echo "[wb-tts] Model dir : $WB_TTS_MODEL_DIR"
echo "[wb-tts] Listening : http://$LISTEN_IP:$WB_TTS_PORT"
echo

exec "$VENV_PY" "$MODULE_DIR/server.py" --ip "$LISTEN_IP" --port "$WB_TTS_PORT" --model-dir "$WB_TTS_MODEL_DIR"
