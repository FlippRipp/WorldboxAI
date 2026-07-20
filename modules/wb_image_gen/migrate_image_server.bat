@echo off
setlocal enabledelayedexpansion

:: WorldBox AI RPG Engine - migrate a local image server install to Forge Neo
:: (counterpart to migrate_image_server.sh)
::
:: Moves an existing SD WebUI install (the previous default, lllyasviel's
:: SD WebUI Forge, or classic AUTOMATIC1111) to SD WebUI Forge Neo -- the
:: current image_server.bat default and the WebUI that runs Anima
:: checkpoints -- WITHOUT re-downloading your models:
::
::   1. the old install is renamed aside as a backup (nothing is deleted);
::   2. Forge Neo is cloned into the old install's place, so image_server.bat
::      and the Image Studio's folder settings keep working unchanged;
::   3. your model files move from the backup into the new install:
::      checkpoints, LoRAs, upscalers, VAEs, text encoders, embeddings, and
::      the WorldBox install helper's hash cache. Moves are instant renames
::      on the same drive, whatever the file sizes.
::
:: The backup keeps the old WebUI's code, venv, and settings (its models
:: folders are left empty on purpose -- the files now live in the new
:: install). Once the new WebUI works, delete the backup to reclaim the old
:: venv's several GB.
::
:: Usage (from the repo root):
::   .\modules\wb_image_gen\migrate_image_server.bat [install_dir]
::   (default install dir: <repo root>\image_server, same as image_server.bat)
::
:: Environment overrides:
::   WB_WEBUI_DIR     install directory (same as the argument)
::   WB_MIGRATE_YES   1 skips the confirmation prompt (for scripted use)

for %%i in ("%~dp0..\..") do set "REPO_ROOT=%%~fi"
title WorldBox Image Server Migration

set "NEO_REPO=https://github.com/Haoming02/sd-webui-forge-classic.git"
set "NEO_BRANCH=neo"
set "WEBUI_DIR=%~1"
if not defined WEBUI_DIR if defined WB_WEBUI_DIR set "WEBUI_DIR=%WB_WEBUI_DIR%"
if not defined WEBUI_DIR set "WEBUI_DIR=%REPO_ROOT%\image_server"
if "%WEBUI_DIR:~-1%"=="\" set "WEBUI_DIR=%WEBUI_DIR:~0,-1%"

echo ==============================================
echo    WorldBox - Image Server -^> Forge Neo
echo ==============================================
echo.

where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] git not found on PATH. Install git first.
    echo.
    pause
    exit /b 1
)
if not exist "%WEBUI_DIR%" (
    echo [ERROR] No install at %WEBUI_DIR% -- nothing to migrate.
    echo         ^(A fresh .\modules\wb_image_gen\image_server.bat run installs
    echo         Forge Neo directly.^)
    echo.
    pause
    exit /b 1
)
set "ORIGIN="
for /f "usebackq delims=" %%u in (`git -C "%WEBUI_DIR%" remote get-url origin 2^>nul`) do set "ORIGIN=%%u"
echo !ORIGIN! | findstr /c:"forge-classic" >nul
if not errorlevel 1 (
    echo %WEBUI_DIR% already is Forge Neo ^(!ORIGIN!^) -- nothing to migrate.
    echo.
    pause
    exit /b 0
)
if not exist "%WEBUI_DIR%\webui.bat" if not exist "%WEBUI_DIR%\webui.sh" (
    echo [ERROR] %WEBUI_DIR% does not look like an A1111-family WebUI install
    echo         ^(no webui.bat/webui.sh^).
    echo.
    pause
    exit /b 1
)

set "BACKUP_DIR=%WEBUI_DIR%-forge-backup"
set n=2
:backup_name
if exist "!BACKUP_DIR!" (
    set "BACKUP_DIR=%WEBUI_DIR%-forge-backup-!n!"
    set /a n+=1
    goto backup_name
)

echo This will:
echo   - rename  %WEBUI_DIR%
echo     to      !BACKUP_DIR!  ^(kept as a backup^)
echo   - clone   Forge Neo into %WEBUI_DIR%
echo   - move your checkpoints, LoRAs, upscalers, VAEs, text encoders and
echo     embeddings from the backup into the new install
echo.
echo Stop the image server (and its install helper) before continuing.
if not "%WB_MIGRATE_YES%"=="1" (
    set /p answer="Continue? [y/N] "
    if /i not "!answer!"=="y" if /i not "!answer!"=="yes" (
        echo Aborted -- nothing was changed.
        exit /b 1
    )
)
echo.

:: ── 1. Old install aside ──
move "%WEBUI_DIR%" "!BACKUP_DIR!" >nul
if errorlevel 1 (
    echo [ERROR] Could not rename %WEBUI_DIR% ^(is the WebUI still running?^).
    echo.
    pause
    exit /b 1
)
echo Old install moved to !BACKUP_DIR!

:: ── 2. Forge Neo into its place ──
echo Cloning Forge Neo (branch %NEO_BRANCH%) ...
git clone -b "%NEO_BRANCH%" "%NEO_REPO%" "%WEBUI_DIR%"
if errorlevel 1 (
    echo [ERROR] git clone failed -- restoring the old install.
    move "!BACKUP_DIR!" "%WEBUI_DIR%" >nul
    echo.
    pause
    exit /b 1
)
echo.

:: ── 3. Models across ──
:: The WebUI-standard folders keep their names; classic top-level embeddings\
:: lands in Forge Neo's models\embeddings. robocopy /E /MOVE carries
:: user-made subfolders along; /XC /XN /XO leaves any same-named stock file
:: of the fresh clone alone (the user's copy stays in the backup).
echo Moving model files ...
call :move_dir "models\Stable-diffusion" "models\Stable-diffusion"
call :move_dir "models\Lora"             "models\Lora"
call :move_dir "models\LyCORIS"          "models\Lora"
call :move_dir "models\ESRGAN"           "models\ESRGAN"
call :move_dir "models\VAE"              "models\VAE"
call :move_dir "models\text_encoder"     "models\text_encoder"
call :move_dir "embeddings"              "models\embeddings"
call :move_dir "models\embeddings"       "models\embeddings"
:: The install helper's hash index rides along so nothing gets re-hashed.
if exist "!BACKUP_DIR!\models\wb-helper-cache.json" if not exist "%WEBUI_DIR%\models\wb-helper-cache.json" (
    move "!BACKUP_DIR!\models\wb-helper-cache.json" "%WEBUI_DIR%\models\" >nul
    if not errorlevel 1 echo   moved the install helper's hash cache
)
echo.

echo ==============================================
echo Done. Next steps:
echo   1. Start the server: .\modules\wb_image_gen\image_server.bat
echo      ^(first Forge Neo launch installs its dependencies -- several GB,
echo      one time; your models are already in place^)
echo   2. For Anima checkpoints, install the Qwen text encoder + VAE from
echo      the Image Studio's Setup tab ^(one click^) if you haven't yet.
echo   3. Once everything works, reclaim the old WebUI's disk space by
echo      deleting the backup folder:
echo      !BACKUP_DIR!
echo      ^(it still holds the old code, venv, and WebUI settings -- but
echo      none of your models; those moved with you^)
echo ==============================================
echo.
pause
exit /b 0

:move_dir
:: %1 = source relative to the backup, %2 = destination relative to the new
:: install. robocopy exit codes 0-7 mean success; 8+ are real errors.
if not exist "!BACKUP_DIR!\%~1" exit /b 0
robocopy "!BACKUP_DIR!\%~1" "%WEBUI_DIR%\%~2" /E /MOVE /XC /XN /XO /NFL /NDL /NJH /NJS >nul
if %errorlevel% geq 8 (
    echo   [WARN] could not fully move %~1
) else (
    echo   moved %~1 -^> %~2
)
exit /b 0
