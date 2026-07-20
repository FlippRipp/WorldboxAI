#!/usr/bin/env bash
# WorldBox AI RPG Engine - migrate a local image server install to Forge Neo
# (counterpart to migrate_image_server.bat)
#
# Moves an existing SD WebUI install (the previous default, lllyasviel's
# SD WebUI Forge, or classic AUTOMATIC1111) to SD WebUI Forge Neo -- the
# current image_server.sh/.bat default and the WebUI that runs Anima
# checkpoints -- WITHOUT re-downloading your models:
#
#   1. the old install is renamed aside as a backup (nothing is deleted);
#   2. Forge Neo is cloned into the old install's place, so image_server.sh
#      and the Image Studio's folder settings keep working unchanged;
#   3. your model files move from the backup into the new install:
#      checkpoints, LoRAs, upscalers, VAEs, text encoders, embeddings, and
#      the WorldBox install helper's hash cache. Moves are instant renames
#      on the same drive, whatever the file sizes.
#
# The backup keeps the old WebUI's code, venv, and settings (its models
# folders are left empty on purpose -- the files now live in the new
# install). Once the new WebUI works, delete the backup to reclaim the old
# venv's several GB.
#
# Usage (from the repo root):
#   ./modules/wb_image_gen/migrate_image_server.sh [install_dir]
#   # default install dir: <repo root>/image_server, same as image_server.sh
#
# Environment overrides:
#   WB_WEBUI_DIR     install directory (same as the positional argument)
#   WB_MIGRATE_YES   1 skips the confirmation prompt (for scripted use)
set -u
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

NEO_REPO="https://github.com/Haoming02/sd-webui-forge-classic.git"
NEO_BRANCH="neo"
WEBUI_DIR="${1:-${WB_WEBUI_DIR:-$REPO_ROOT/image_server}}"
# Strip a trailing slash so the backup path lands next to the install.
WEBUI_DIR="${WEBUI_DIR%/}"

echo "=============================================="
echo "   WorldBox - Image Server -> Forge Neo"
echo "=============================================="
echo

if ! command -v git >/dev/null 2>&1; then
    echo "[ERROR] git not found on PATH. Install git first."
    exit 1
fi
if [ ! -d "$WEBUI_DIR" ]; then
    echo "[ERROR] No install at $WEBUI_DIR -- nothing to migrate."
    echo "        (A fresh ./modules/wb_image_gen/image_server.sh run installs"
    echo "        Forge Neo directly.)"
    exit 1
fi
ORIGIN="$(git -C "$WEBUI_DIR" remote get-url origin 2>/dev/null || true)"
case "$ORIGIN" in
    *forge-classic*)
        echo "$WEBUI_DIR already is Forge Neo ($ORIGIN) -- nothing to migrate."
        exit 0
        ;;
esac
if [ ! -f "$WEBUI_DIR/webui.sh" ] && [ ! -f "$WEBUI_DIR/webui.bat" ]; then
    echo "[ERROR] $WEBUI_DIR does not look like an A1111-family WebUI install"
    echo "        (no webui.sh/webui.bat)."
    exit 1
fi

BACKUP_DIR="$WEBUI_DIR-forge-backup"
n=2
while [ -e "$BACKUP_DIR" ]; do
    BACKUP_DIR="$WEBUI_DIR-forge-backup-$n"
    n=$((n + 1))
done

echo "This will:"
echo "  - rename  $WEBUI_DIR"
echo "    to      $BACKUP_DIR  (kept as a backup)"
echo "  - clone   Forge Neo into $WEBUI_DIR"
echo "  - move your checkpoints, LoRAs, upscalers, VAEs, text encoders and"
echo "    embeddings from the backup into the new install"
echo
echo "Stop the image server (and its install helper) before continuing."
if [ "${WB_MIGRATE_YES:-0}" != "1" ]; then
    printf "Continue? [y/N] "
    read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) echo "Aborted -- nothing was changed."; exit 1 ;;
    esac
fi
echo

# ── 1. Old install aside ──
if ! mv "$WEBUI_DIR" "$BACKUP_DIR"; then
    echo "[ERROR] Could not rename $WEBUI_DIR (is the WebUI still running?)."
    exit 1
fi
echo "Old install moved to $BACKUP_DIR"

# ── 2. Forge Neo into its place ──
echo "Cloning Forge Neo (branch $NEO_BRANCH) ..."
if ! git clone -b "$NEO_BRANCH" "$NEO_REPO" "$WEBUI_DIR"; then
    echo "[ERROR] git clone failed -- restoring the old install."
    mv "$BACKUP_DIR" "$WEBUI_DIR"
    exit 1
fi
echo

# ── 3. Models across ──
# src (relative to the old install) -> dst (relative to the new one). The
# WebUI-standard folders keep their names; classic top-level embeddings/
# lands in Forge Neo's models/embeddings. Contents move entry by entry so
# user-made subfolders survive; name collisions with the fresh clone's
# stock files keep the user's copy aside untouched in the backup.
move_contents() {
    src="$BACKUP_DIR/$1"
    dst="$WEBUI_DIR/$2"
    [ -d "$src" ] || return 0
    count=0
    mkdir -p "$dst"
    for entry in "$src"/* "$src"/.[!.]*; do
        [ -e "$entry" ] || continue
        name="$(basename "$entry")"
        if [ -e "$dst/$name" ]; then
            echo "  [skip] $2/$name already exists in the new install"
            continue
        fi
        if mv "$entry" "$dst/$name"; then
            count=$((count + 1))
        else
            echo "  [WARN] could not move $1/$name"
        fi
    done
    [ "$count" -gt 0 ] && echo "  moved $count item(s): $1 -> $2"
    return 0
}

echo "Moving model files ..."
move_contents "models/Stable-diffusion" "models/Stable-diffusion"
move_contents "models/Lora"             "models/Lora"
move_contents "models/LyCORIS"          "models/Lora"
move_contents "models/ESRGAN"           "models/ESRGAN"
move_contents "models/VAE"              "models/VAE"
move_contents "models/text_encoder"     "models/text_encoder"
move_contents "embeddings"              "models/embeddings"
move_contents "models/embeddings"       "models/embeddings"
# The install helper's hash index (models/wb-helper-cache.json) rides along
# so nothing gets re-hashed after the move.
if [ -f "$BACKUP_DIR/models/wb-helper-cache.json" ] \
        && [ ! -e "$WEBUI_DIR/models/wb-helper-cache.json" ]; then
    mv "$BACKUP_DIR/models/wb-helper-cache.json" "$WEBUI_DIR/models/" \
        && echo "  moved the install helper's hash cache"
fi
echo

echo "=============================================="
echo "Done. Next steps:"
echo "  1. Start the server: ./modules/wb_image_gen/image_server.sh"
echo "     (first Forge Neo launch installs its dependencies -- several GB,"
echo "     one time; your models are already in place)"
echo "  2. For Anima checkpoints, install the Qwen text encoder + VAE from"
echo "     the Image Studio's Setup tab (one click) if you haven't yet."
echo "  3. Once everything works, reclaim the old WebUI's disk space:"
echo "     rm -rf \"$BACKUP_DIR\""
echo "     (it still holds the old code, venv, and WebUI settings -- but"
echo "     none of your models; those moved with you)"
echo "=============================================="
