@echo off
setlocal enabledelayedexpansion

:: WorldBox AI RPG Engine - Faster Qwen3-TTS server setup/launch (counterpart to tts_server.sh)
::
:: Sets up and launches the standalone Faster Qwen3-TTS streaming provider
:: server for the wb_tts module. Can run on the same PC as WorldBox or on a
:: separate GPU host on your LAN / Tailscale network (e.g. when playing on a
:: phone).
::
:: On first launch it creates an isolated Python venv in <repo root>\tts_server
:: (gitignored; kept outside modules\wb_tts so module packaging stays small),
:: installs CUDA PyTorch + faster-qwen3-tts, and downloads
:: Qwen/Qwen3-TTS-12Hz-1.7B-Base if not already present.
::
:: Usage (from the repo root):
::   .\modules\wb_tts\tts_server.bat [install_dir]
::
:: Environment overrides:
::   WB_TTS_DIR        install directory (default: <repo root>\tts_server)
::   WB_TTS_MODEL_DIR  existing Qwen3-TTS-12Hz-1.7B-Base folder (skips download)
::   WB_TTS_PORT       port to listen on (default: 7864)
::   WB_TTS_LISTEN     1 (default) binds 0.0.0.0 for LAN/mobile access;
::                     0 binds 127.0.0.1 only
::   WB_TTS_TOKEN      optional bearer token; paste the same value into Voice Server
::   WB_TTS_TORCH_URL  PyTorch wheel index (default: https://download.pytorch.org/whl/cu128)

set "MODULE_DIR=%~dp0"
if "%MODULE_DIR:~-1%"=="\" set "MODULE_DIR=%MODULE_DIR:~0,-1%"
for %%i in ("%MODULE_DIR%\..\..") do set "REPO_ROOT=%%~fi"
cd /d "%REPO_ROOT%"
title WorldBox TTS Server

if not defined WB_TTS_PORT set WB_TTS_PORT=7864
if not defined WB_TTS_LISTEN set WB_TTS_LISTEN=1
if not defined WB_TTS_TORCH_URL set "WB_TTS_TORCH_URL=https://download.pytorch.org/whl/cu128"

set "TTS_DIR=%~1"
if not defined TTS_DIR if defined WB_TTS_DIR set "TTS_DIR=%WB_TTS_DIR%"
if not defined TTS_DIR set "TTS_DIR=%REPO_ROOT%\tts_server"

if not defined WB_TTS_MODEL_DIR (
    if exist "%REPO_ROOT%\..\PNGTuber\.runtime\qwen3-tts\Qwen3-TTS-12Hz-1.7B-Base\config.json" (
        set "WB_TTS_MODEL_DIR=%REPO_ROOT%\..\PNGTuber\.runtime\qwen3-tts\Qwen3-TTS-12Hz-1.7B-Base"
    ) else (
        set "WB_TTS_MODEL_DIR=%TTS_DIR%\models\Qwen3-TTS-12Hz-1.7B-Base"
    )
)

echo ==============================================
echo   WorldBox - Faster Qwen3-TTS Provider Server
echo ==============================================
echo.

:: ── Locate Python 3.10 - 3.13 ──
set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    for %%v in (3.12 3.11 3.10 3.13) do (
        if not defined PY_CMD (
            py -%%v -c "import sys" >nul 2>nul && set "PY_CMD=py -%%v"
        )
    )
)
if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [ERROR] Python 3.10+ not found on PATH. Install Python 3.11 or 3.12 first.
    echo.
    pause
    exit /b 1
)

set "VENV_DIR=%TTS_DIR%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "STAMP_FILE=%VENV_DIR%\.wb_tts_ready_v2"

if not exist "%VENV_PY%" (
    echo [wb-tts] Creating virtual environment in "%VENV_DIR%"...
    if not exist "%TTS_DIR%" mkdir "%TTS_DIR%"
    %PY_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

if not exist "%STAMP_FILE%" (
    echo [wb-tts] Installing CUDA PyTorch and Faster Qwen3-TTS dependencies...
    "%VENV_PY%" -m pip install --upgrade pip setuptools wheel
    "%VENV_PY%" -m pip uninstall -y qwen-tts-hf >nul 2>nul
    "%VENV_PY%" -m pip install torch torchaudio --index-url "%WB_TTS_TORCH_URL%"
    if errorlevel 1 (
        echo [ERROR] Failed to install CUDA PyTorch from %WB_TTS_TORCH_URL%.
        pause
        exit /b 1
    )
    "%VENV_PY%" -m pip install "faster-qwen3-tts==0.3.2" "qwen-tts==0.1.1" "transformers>=4.57,<5" "huggingface_hub>=0.36,<1.0" fastapi uvicorn pydantic numpy soundfile static-ffmpeg
    if errorlevel 1 (
        echo [wb-tts] PyPI install failed; falling back to upstream v0.3.2 tag...
        "%VENV_PY%" -m pip install "git+https://github.com/andimarafioti/faster-qwen3-tts.git@a70afc0" "qwen-tts==0.1.1" "transformers>=4.57,<5" "huggingface_hub>=0.36,<1.0" fastapi uvicorn pydantic numpy soundfile static-ffmpeg
        if errorlevel 1 (
            echo [ERROR] Failed to install faster-qwen3-tts dependencies.
            pause
            exit /b 1
        )
    )
    if exist "%VENV_DIR%\.wb_tts_ready_v1" del /q "%VENV_DIR%\.wb_tts_ready_v1" >nul 2>nul
    echo ok > "%STAMP_FILE%"
)

set "LISTEN_IP=0.0.0.0"
if "%WB_TTS_LISTEN%"=="0" set "LISTEN_IP=127.0.0.1"

echo [wb-tts] Model dir : %WB_TTS_MODEL_DIR%
echo [wb-tts] Listening : http://%LISTEN_IP%:%WB_TTS_PORT%
echo.

"%VENV_PY%" "%MODULE_DIR%\server.py" --ip "%LISTEN_IP%" --port "%WB_TTS_PORT%" --model-dir "%WB_TTS_MODEL_DIR%"
if errorlevel 1 (
    echo.
    echo [ERROR] TTS server exited with an error.
    pause
    exit /b 1
)
