@echo off
setlocal enabledelayedexpansion

:: WorldBox AI RPG Engine - local image server setup/launch (counterpart to image_server.sh)
::
:: Sets up and starts a local Stable Diffusion WebUI as the "local" provider
:: of the wb_image_gen module: clones SD WebUI Forge (updates it on later
:: runs), then launches it with --api on http://127.0.0.1:7860 -- the address
:: the Image Studio expects by default. The WebUI's own launcher creates its
:: private venv and installs its requirements (torch etc.) on first run, so
:: the first launch downloads several GB.
::
:: Usage (from the repo root):
::   .\modules\wb_image_gen\image_server.bat [install_dir]
::   (default install dir: <repo root>\image_server -- gitignored; kept out of
::   the module folder so module packaging never picks up a multi-GB WebUI)
::
:: Environment overrides:
::   WB_WEBUI_DIR      install directory (same as the argument)
::   WB_WEBUI_REPO     git URL of the WebUI to install (default: SD WebUI
::                     Forge; any A1111-compatible fork with the standard
::                     webui.bat launcher works)
::   WB_WEBUI_PORT     port to listen on (default 7860; if you change it,
::                     change the server address in the Image Studio too)
::   WB_WEBUI_LISTEN   1 (default) also accepts connections from other devices
::                     on the network (--listen); 0 binds to 127.0.0.1 only
::   WEBUI_EXTRA_ARGS  extra launch flags, e.g. "--api-auth user:pass" or,
::                     without an NVIDIA GPU, "--skip-torch-cuda-test --use-cpu all"
::   WB_HELPER         1 (default) also starts the WorldBox install helper
::                     (helper_server.py) next to the WebUI, so the app can
::                     one-click install checkpoints/LoRAs and read exact
::                     installed-model badges even from another machine;
::                     0 disables it
::   WB_HELPER_PORT    helper port (default 7861)
::   WB_HELPER_LISTEN  like WB_WEBUI_LISTEN, for the helper (defaults to
::                     WB_WEBUI_LISTEN)
::   WB_HELPER_TOKEN   optional shared secret; paste the same value into the
::                     Image Studio's helper token field

:: The script lives in modules\wb_image_gen\; the default install dir sits at
:: the repo root.
for %%i in ("%~dp0..\..") do set "REPO_ROOT=%%~fi"
cd /d "%REPO_ROOT%"
title WorldBox Image Server

if not defined WB_WEBUI_PORT set WB_WEBUI_PORT=7860
if not defined WB_WEBUI_REPO set WB_WEBUI_REPO=https://github.com/lllyasviel/stable-diffusion-webui-forge.git
set "WEBUI_DIR=%~1"
if not defined WEBUI_DIR if defined WB_WEBUI_DIR set "WEBUI_DIR=%WB_WEBUI_DIR%"
if not defined WEBUI_DIR set "WEBUI_DIR=%REPO_ROOT%\image_server"

echo ==============================================
echo    WorldBox - Local Image Server (SD WebUI)
echo ==============================================
echo.

:: ── Preflight: git ──
where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] git not found on PATH. Install git first.
    echo.
    pause
    exit /b 1
)

:: ── Preflight: Python (the WebUI officially supports 3.10; 3.11 usually works) ──
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found on PATH. Install Python 3.10 from python.org
    echo         and check "Add python to PATH" in its installer.
    echo.
    pause
    exit /b 1
)
set PYV=
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYV=%%v
echo !PYV! | findstr /b /c:"3.10." /c:"3.11." >nul
if errorlevel 1 (
    echo [WARN] Python !PYV! detected. The WebUI officially supports Python 3.10.
    echo        If the first launch fails installing torch, install Python 3.10
    echo        and point the WebUI at it, e.g.:
    echo        set PYTHON=C:\Path\To\Python310\python.exe
    echo.
)

:: ── Preflight: GPU ──
where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo [WARN] No NVIDIA GPU detected. The WebUI defaults to CUDA torch.
    echo        For CPU-only rendering ^(very slow^) run:
    echo        set WEBUI_EXTRA_ARGS=--skip-torch-cuda-test --use-cpu all
    echo        For AMD GPUs see the WebUI's DirectML/ZLUDA install docs.
    echo.
)

:: ── Clone or update the WebUI ──
if not exist "%WEBUI_DIR%\.git" (
    echo Installing SD WebUI into %WEBUI_DIR% ...
    echo ^(repo: %WB_WEBUI_REPO%^)
    git clone "%WB_WEBUI_REPO%" "%WEBUI_DIR%"
    if errorlevel 1 (
        echo [ERROR] git clone failed.
        echo.
        pause
        exit /b 1
    )
    echo.
) else (
    echo Checking for WebUI updates...
    git -C "%WEBUI_DIR%" pull --ff-only
    if errorlevel 1 (
        echo [WARN] git pull failed. Starting current version.
    )
    echo.
)

if not exist "%WEBUI_DIR%\webui.bat" (
    echo [ERROR] %WEBUI_DIR%\webui.bat not found -- %WB_WEBUI_REPO% does not
    echo         look like an A1111-compatible WebUI.
    echo.
    pause
    exit /b 1
)

:: ── WorldBox prompt-batch script ──
:: Lets a multi-image generation render as one GPU batch of different prompts
:: (much faster than the WebUI queueing them one by one). WorldBox detects it
:: via the API and falls back to serial requests when it's missing.
if not exist "%WEBUI_DIR%\scripts" mkdir "%WEBUI_DIR%\scripts"
copy /Y "%~dp0wb_prompt_batch.py" "%WEBUI_DIR%\scripts\" >nul
if errorlevel 1 echo [WARN] Could not install wb_prompt_batch.py; multi-image generations render serially.

:: ── Launch flags ──
:: On Windows the WebUI's own entry point is webui-user.bat, which just sets
:: these variables and calls webui.bat -- this script takes its place, so a
:: stock webui-user.bat never overrides us.
:: --listen accepts connections from the local network (the WebUI then blocks
:: installing ITS OWN extensions from its UI as a precaution; pass
:: WEBUI_EXTRA_ARGS=--enable-insecure-extension-access if you need that --
:: WorldBox's Studio installs don't go through the WebUI and are unaffected).
if not defined WB_WEBUI_LISTEN set WB_WEBUI_LISTEN=1
set "COMMANDLINE_ARGS=--api --port %WB_WEBUI_PORT%"
if not "%WB_WEBUI_LISTEN%"=="0" set "COMMANDLINE_ARGS=%COMMANDLINE_ARGS% --listen"
if defined WEBUI_EXTRA_ARGS set "COMMANDLINE_ARGS=%COMMANDLINE_ARGS% %WEBUI_EXTRA_ARGS%"
:: The WebUI auto-opens a browser tab on start (its auto_launch_browser
:: setting defaults to "Local"). WorldBox only needs the API, so suppress it
:: with the same switch the WebUI's own in-place restarts use; the interface
:: stays reachable at the printed address.
set SD_WEBUI_RESTARTING=1

:: ── Pin packages for the WebUI's pip installs ──
:: setuptools<81: the launcher builds openai/CLIP from an old source zip
::   whose setup.py imports pkg_resources, removed in setuptools 81. The
::   build runs in pip's isolated build environment (which installs the
::   newest setuptools), so the pin must travel via PIP_CONSTRAINT -- the one
::   channel that reaches build environments.
:: numpy<2: the WebUI's torch/scikit-image builds are compiled against
::   NumPy 1.x; a stray install step upgrading to NumPy 2 crashes startup
::   with "_ARRAY_API not found" / "numpy.dtype size changed".
:: A PIP_CONSTRAINT the user already set is left alone.
if not defined PIP_CONSTRAINT (
    > "%WEBUI_DIR%\worldbox-pip-constraints.txt" (
        echo setuptools^<81
        echo numpy^<2
    )
    set "PIP_CONSTRAINT=%WEBUI_DIR%\worldbox-pip-constraints.txt"
)

:: ── Repair: a venv that already picked up NumPy 2 gets downgraded ──
set "VENV_PY=%WEBUI_DIR%\venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import numpy" >nul 2>&1
    if not errorlevel 1 (
        "%VENV_PY%" -c "import numpy, sys; sys.exit(int(numpy.__version__.split('.')[0]) >= 2)" >nul 2>&1
        if errorlevel 1 (
            echo Repairing the WebUI venv: downgrading NumPy 2 to 1.x ...
            "%VENV_PY%" -m pip install "numpy<2"
            echo.
        )
    )
)

:: ── WorldBox install helper (one-click installs + badges from the app) ──
:: A tiny stdlib-only companion server; the app sends it download commands
:: and reads its hash index, which is what makes the Studio's model/LoRA
:: browsers fully work when the app runs on a different machine than this
:: WebUI. It shares this console and stops with it.
if not defined WB_HELPER set WB_HELPER=1
if not defined WB_HELPER_PORT set WB_HELPER_PORT=7861
if not defined WB_HELPER_LISTEN set WB_HELPER_LISTEN=%WB_WEBUI_LISTEN%
if not exist "%WEBUI_DIR%\models\ESRGAN" mkdir "%WEBUI_DIR%\models\ESRGAN"
if not "%WB_HELPER%"=="0" (
    if not exist "%WEBUI_DIR%\models\Stable-diffusion" mkdir "%WEBUI_DIR%\models\Stable-diffusion"
    if not exist "%WEBUI_DIR%\models\Lora" mkdir "%WEBUI_DIR%\models\Lora"
    set "WB_HELPER_CKPT_DIR=%WEBUI_DIR%\models\Stable-diffusion"
    set "WB_HELPER_LORA_DIR=%WEBUI_DIR%\models\Lora"
    set "WB_HELPER_UPSCALER_DIR=%WEBUI_DIR%\models\ESRGAN"
    start "WorldBox Install Helper" /b python "%REPO_ROOT%\modules\wb_image_gen\helper_server.py"
)

:: ── First-run hints + the values the Image Studio needs ──
if not exist "%WEBUI_DIR%\venv" (
    echo First launch: the WebUI now installs its own dependencies ^(several GB,
    echo one time^). Later launches start directly.
    echo.
)
echo ==== WorldBox Image Studio settings (Setup tab, provider 'Local'):  ====
echo ====   Server address:    http://127.0.0.1:%WB_WEBUI_PORT%
echo ====   (also the WebUI's own interface -- open it manually if      ====
echo ====   needed; no browser tab is auto-opened)                      ====
if not "%WB_WEBUI_LISTEN%"=="0" (
    echo ====   From other devices on the same network:                      ====
    for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
        for /f "tokens=* delims= " %%b in ("%%a") do echo ====     http://%%b:%WB_WEBUI_PORT%
    )
)
echo ====   Checkpoint folder: %WEBUI_DIR%\models\Stable-diffusion
echo ====   LoRA folder:       %WEBUI_DIR%\models\Lora
echo ====   Upscaler folder:   %WEBUI_DIR%\models\ESRGAN
echo ====     (optional -- derived from the checkpoint folder if empty)
if not "%WB_HELPER%"=="0" (
    echo ====   Install helper:    http://127.0.0.1:%WB_HELPER_PORT%
    echo ====   When WorldBox runs on ANOTHER machine, leave the folder      ====
    echo ====   fields empty and paste the helper address instead -- it      ====
    echo ====   downloads models here and reports installed ones back.       ====
)
echo ==== No checkpoints ship with the WebUI -- set the values above in  ====
echo ==== the Studio and install models from its Civitai/HF browser, or  ====
echo ==== drop .safetensors files into the folders manually.             ====
echo ==== Press Ctrl+C to stop the server.                               ====
echo.

cd /d "%WEBUI_DIR%"
call webui.bat
exit /b %errorlevel%
