#!/usr/bin/env bash
# WorldBox AI RPG Engine - local image server setup/launch (counterpart to image_server.bat)
#
# Sets up and starts a local Stable Diffusion WebUI as the "local" provider of
# the wb_image_gen module: clones SD WebUI Forge (updates it on later runs),
# then launches it with --api on http://127.0.0.1:7860 -- the address the
# Image Studio expects by default. The WebUI's own launcher creates its
# private venv and installs its requirements (torch etc.) on first run, so
# the first launch downloads several GB.
#
# Usage (from the repo root):
#   ./modules/wb_image_gen/image_server.sh [install_dir]
#   # default install dir: <repo root>/image_server (gitignored; kept out of
#   # the module folder so module packaging never picks up a multi-GB WebUI)
#
# Environment overrides:
#   WB_WEBUI_DIR      install directory (same as the positional argument)
#   WB_WEBUI_REPO     git URL of the WebUI to install
#                     (default: SD WebUI Forge; any A1111-compatible fork
#                     with the standard webui.sh launcher works, e.g.
#                     https://github.com/AUTOMATIC1111/stable-diffusion-webui.git)
#   WB_WEBUI_PORT     port to listen on (default 7860; if you change it,
#                     change the server address in the Image Studio too)
#   WB_WEBUI_LISTEN   1 (default) also accepts connections from other devices
#                     on the network (--listen); 0 binds to 127.0.0.1 only
#   WEBUI_EXTRA_ARGS  extra launch flags, e.g. "--api-auth user:pass" or,
#                     without an NVIDIA GPU, "--skip-torch-cuda-test --use-cpu all"
#   WB_HELPER         1 (default) also starts the WorldBox install helper
#                     (helper_server.py) next to the WebUI, so the app can
#                     one-click install checkpoints/LoRAs and read exact
#                     installed-model badges even from another machine;
#                     0 disables it
#   WB_HELPER_PORT    helper port (default 7861)
#   WB_HELPER_LISTEN  like WB_WEBUI_LISTEN, for the helper (defaults to
#                     WB_WEBUI_LISTEN)
#   WB_HELPER_TOKEN   optional shared secret; paste the same value into the
#                     Image Studio's helper token field
set -u
# The script lives in modules/wb_image_gen/; the default install dir sits at
# the repo root.
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

WB_WEBUI_REPO="${WB_WEBUI_REPO:-https://github.com/lllyasviel/stable-diffusion-webui-forge.git}"
WB_WEBUI_PORT="${WB_WEBUI_PORT:-7860}"
WEBUI_DIR="${1:-${WB_WEBUI_DIR:-$REPO_ROOT/image_server}}"

echo "=============================================="
echo "   WorldBox - Local Image Server (SD WebUI)"
echo "=============================================="
echo

# ── Preflight: git ──
if ! command -v git >/dev/null 2>&1; then
    echo "[ERROR] git not found on PATH. Install git first."
    exit 1
fi

# ── Preflight: Python (the WebUI officially supports 3.10; 3.11 usually works) ──
PY_CMD=""
for cand in python3.10 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        PY_CMD="$cand"
        break
    fi
done
if [ -z "$PY_CMD" ]; then
    echo "[ERROR] python3 not found on PATH. Install Python 3.10 first."
    exit 1
fi
PY_VER=$("$PY_CMD" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
case "$PY_VER" in
    3.10|3.11) ;;
    *)
        echo "[WARN] Using $PY_CMD (Python ${PY_VER:-unknown}). The WebUI officially"
        echo "       supports Python 3.10 -- if the first launch fails installing"
        echo "       torch, install python3.10 and re-run this script."
        echo
        ;;
esac
if ! "$PY_CMD" -c "import venv" >/dev/null 2>&1; then
    echo "[WARN] The 'venv' module is missing for $PY_CMD. On Debian/Ubuntu run:"
    echo "       sudo apt install ${PY_CMD}-venv"
    echo
fi

# ── Preflight: GPU ──
if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "[WARN] No NVIDIA GPU detected (nvidia-smi not found). The WebUI"
    echo "       defaults to CUDA torch. For CPU-only rendering (very slow) run:"
    echo "       WEBUI_EXTRA_ARGS=\"--skip-torch-cuda-test --use-cpu all\" $0"
    echo "       For AMD GPUs see the WebUI's ROCm install docs."
    echo
fi

# ── Clone or update the WebUI ──
if [ ! -d "$WEBUI_DIR/.git" ]; then
    echo "Installing SD WebUI into $WEBUI_DIR ..."
    echo "(repo: $WB_WEBUI_REPO)"
    if ! git clone "$WB_WEBUI_REPO" "$WEBUI_DIR"; then
        echo "[ERROR] git clone failed."
        exit 1
    fi
    echo
else
    echo "Checking for WebUI updates..."
    if ! git -C "$WEBUI_DIR" pull --ff-only; then
        echo "[WARN] git pull failed (offline, local changes, or diverged branch). Starting current version."
    fi
    echo
fi

if [ ! -f "$WEBUI_DIR/webui.sh" ]; then
    echo "[ERROR] $WEBUI_DIR/webui.sh not found -- $WB_WEBUI_REPO does not look"
    echo "        like an A1111-compatible WebUI."
    exit 1
fi

# ── WorldBox prompt-batch script ──
# Lets a multi-image generation render as one GPU batch of different prompts
# (much faster than the WebUI queueing them one by one). WorldBox detects it
# via the API and falls back to serial requests when it's missing.
mkdir -p "$WEBUI_DIR/scripts"
cp -f "$REPO_ROOT/modules/wb_image_gen/wb_prompt_batch.py" "$WEBUI_DIR/scripts/" \
    || echo "[WARN] Could not install wb_prompt_batch.py; multi-image generations render serially."

# ── Launch flags ──
# webui.sh sources webui-user.sh AFTER inheriting the environment, so a
# customized webui-user.sh that sets COMMANDLINE_ARGS would override ours.
# --listen accepts connections from the local network (the WebUI then blocks
# installing ITS OWN extensions from its UI as a precaution; pass
# WEBUI_EXTRA_ARGS="--enable-insecure-extension-access" if you need that --
# WorldBox's Studio installs don't go through the WebUI and are unaffected).
LISTEN_ARG=""
[ "${WB_WEBUI_LISTEN:-1}" != "0" ] && LISTEN_ARG=" --listen"
export COMMANDLINE_ARGS="--api --port $WB_WEBUI_PORT$LISTEN_ARG${WEBUI_EXTRA_ARGS:+ $WEBUI_EXTRA_ARGS}"
if [ -f "$WEBUI_DIR/webui-user.sh" ] \
        && grep -Eq '^[[:space:]]*(export[[:space:]]+)?COMMANDLINE_ARGS=' "$WEBUI_DIR/webui-user.sh" \
        && ! grep -q -- "--api" "$WEBUI_DIR/webui-user.sh"; then
    echo "[WARN] $WEBUI_DIR/webui-user.sh sets COMMANDLINE_ARGS without --api,"
    echo "       overriding this script. Add --api there or WorldBox cannot connect."
    echo
fi
export python_cmd="$PY_CMD"
# The WebUI auto-opens a browser tab on start (its auto_launch_browser
# setting defaults to "Local"). WorldBox only needs the API, so suppress it
# with the same switch the WebUI's own in-place restarts use; the interface
# stays reachable at the printed address.
export SD_WEBUI_RESTARTING=1

# ── Pin packages for the WebUI's pip installs ──
# setuptools<81: the launcher builds openai/CLIP from an old source zip
#   whose setup.py imports pkg_resources, removed in setuptools 81. The
#   build runs in pip's isolated build environment (which installs the
#   newest setuptools), so the pin must travel via PIP_CONSTRAINT -- the one
#   channel that reaches build environments.
# numpy<2: the WebUI's torch/scikit-image builds are compiled against
#   NumPy 1.x; a stray install step upgrading to NumPy 2 crashes startup
#   with "_ARRAY_API not found" / "numpy.dtype size changed".
# A PIP_CONSTRAINT the user already set is left alone.
if [ -z "${PIP_CONSTRAINT:-}" ]; then
    printf 'setuptools<81\nnumpy<2\n' > "$WEBUI_DIR/worldbox-pip-constraints.txt"
    export PIP_CONSTRAINT="$WEBUI_DIR/worldbox-pip-constraints.txt"
fi

# ── Repair: a venv that already picked up NumPy 2 gets downgraded ──
VENV_PY="$WEBUI_DIR/venv/bin/python"
if [ -x "$VENV_PY" ] && "$VENV_PY" -c "import numpy" >/dev/null 2>&1 \
        && ! "$VENV_PY" -c "import numpy, sys; sys.exit(int(numpy.__version__.split('.')[0]) >= 2)" >/dev/null 2>&1; then
    echo "Repairing the WebUI venv: downgrading NumPy 2 to 1.x ..."
    "$VENV_PY" -m pip install "numpy<2"
    echo
fi

# ── WorldBox install helper (one-click installs + badges from the app) ──
# A tiny stdlib-only companion server; the app sends it download commands and
# reads its hash index, which is what makes the Studio's model/LoRA browsers
# fully work when the app runs on a different machine than this WebUI.
WB_HELPER_PORT="${WB_HELPER_PORT:-7861}"
HELPER_PID=""
mkdir -p "$WEBUI_DIR/models/ESRGAN"
if [ "${WB_HELPER:-1}" != "0" ]; then
    mkdir -p "$WEBUI_DIR/models/Stable-diffusion" "$WEBUI_DIR/models/Lora"
    WB_HELPER_CKPT_DIR="$WEBUI_DIR/models/Stable-diffusion" \
    WB_HELPER_LORA_DIR="$WEBUI_DIR/models/Lora" \
    WB_HELPER_UPSCALER_DIR="$WEBUI_DIR/models/ESRGAN" \
    WB_HELPER_PORT="$WB_HELPER_PORT" \
    WB_HELPER_LISTEN="${WB_HELPER_LISTEN:-${WB_WEBUI_LISTEN:-1}}" \
        "$PY_CMD" "$REPO_ROOT/modules/wb_image_gen/helper_server.py" &
    HELPER_PID=$!
    trap '[ -n "$HELPER_PID" ] && kill "$HELPER_PID" 2>/dev/null' EXIT INT TERM
fi

# ── First-run hints + the values the Image Studio needs ──
if [ ! -d "$WEBUI_DIR/venv" ]; then
    echo "First launch: the WebUI now installs its own dependencies (several GB,"
    echo "one time). Later launches start directly."
    echo
fi
echo "==== WorldBox Image Studio settings (Setup tab, provider 'Local'):  ===="
echo "====   Server address:    http://127.0.0.1:$WB_WEBUI_PORT"
echo "====   (also the WebUI's own interface -- open it manually if      ===="
echo "====   needed; no browser tab is auto-opened)                      ===="
if [ "${WB_WEBUI_LISTEN:-1}" != "0" ]; then
    LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [ -n "${LAN_IP:-}" ]; then
        echo "====   From other devices on the same network:                      ===="
        echo "====     http://$LAN_IP:$WB_WEBUI_PORT"
    fi
fi
echo "====   Checkpoint folder: $WEBUI_DIR/models/Stable-diffusion"
echo "====   LoRA folder:       $WEBUI_DIR/models/Lora"
echo "====   Upscaler folder:   $WEBUI_DIR/models/ESRGAN"
echo "====     (optional -- derived from the checkpoint folder if empty)"
if [ "${WB_HELPER:-1}" != "0" ]; then
    echo "====   Install helper:    http://127.0.0.1:$WB_HELPER_PORT"
    if [ -n "${LAN_IP:-}" ]; then
        echo "====     (from other devices: http://$LAN_IP:$WB_HELPER_PORT)"
    fi
    echo "====   When WorldBox runs on ANOTHER machine, leave the folder      ===="
    echo "====   fields empty and paste the helper address instead -- it      ===="
    echo "====   downloads models here and reports installed ones back.       ===="
fi
echo "==== No checkpoints ship with the WebUI -- set the values above in  ===="
echo "==== the Studio and install models from its Civitai/HF browser, or  ===="
echo "==== drop .safetensors files into the folders manually.             ===="
echo "==== Press Ctrl+C to stop the server.                               ===="
echo

cd "$WEBUI_DIR"
# webui.sh refuses to run as root unless -f is passed. Not exec'd so the
# EXIT trap can stop the install helper when the WebUI exits.
if [ "$(id -u)" = "0" ]; then
    ./webui.sh -f
else
    ./webui.sh
fi
