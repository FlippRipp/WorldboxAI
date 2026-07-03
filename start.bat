@echo off
setlocal enabledelayedexpansion

:: ── Re-launch from a temp copy so git pull can safely rewrite this file ──
:: cmd.exe re-reads a running batch file from disk by byte offset after each
:: command, so executing the original while the pull updates it can run
:: garbled commands. The temp copy is never touched by the pull.
if /i not "%~f0"=="%TEMP%\worldbox_start.bat" (
    set "WORLDBOX_DIR=%~dp0"
    copy /y "%~f0" "%TEMP%\worldbox_start.bat" >nul
    "%TEMP%\worldbox_start.bat"
    exit /b
)

cd /d "%WORLDBOX_DIR%"
title WorldBox

:: Backend port; inherited by main.py and the Vite proxy.
if not defined WB_PORT set WB_PORT=8321

echo ==============================================
echo       WorldBox AI RPG Engine - Startup
echo ==============================================
echo.

:: ── Update project from git ──
set UPDATED=0
where git >nul 2>nul
if errorlevel 1 (
    echo [WARN] git not found on PATH. Skipping update check.
    echo.
) else (
    echo Checking for updates...
    set BEFORE=
    for /f %%h in ('git rev-parse HEAD 2^>nul') do set BEFORE=%%h
    git pull --ff-only
    if errorlevel 1 (
        echo [WARN] git pull failed. Starting with current version.
    ) else (
        for /f %%h in ('git rev-parse HEAD 2^>nul') do set AFTER=%%h
        if not "!BEFORE!"=="!AFTER!" (
            set UPDATED=1
            echo Project updated. Dependencies will be refreshed.
        )
    )
    echo.
)

:: ── Preflight: Python virtual environment ──
if not exist ".\venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found at .\venv
    echo Run: python -m venv venv
    echo Then: .\venv\Scripts\activate.bat ^&^& pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

:: ── Install/refresh Python dependencies ──
set NEED_PIP=%UPDATED%
".\venv\Scripts\python.exe" -c "import fastapi, uvicorn, langgraph, litellm, numpy" >nul 2>&1
if errorlevel 1 set NEED_PIP=1
if "%NEED_PIP%"=="1" (
    echo Installing/refreshing Python dependencies...
    ".\venv\Scripts\python.exe" -m pip install -r requirements.txt
    echo.
)

:: ── Preflight: backend .env ──
if not exist ".\backend\.env" (
    echo [WARN] backend\.env not found.
    echo Copy backend\.env.example to backend\.env and set GEMINI_API_KEY for live LLM calls.
    echo.
)

:: ── Preflight: npm ──
where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] npm not found on PATH. Install Node.js first.
    echo.
    pause
    exit /b 1
)

:: ── Preflight: frontend dependencies ──
set NEED_NPM_INSTALL=0
if not exist ".\frontend\node_modules" set NEED_NPM_INSTALL=1
if "%UPDATED%"=="1" set NEED_NPM_INSTALL=1
if "%NEED_NPM_INSTALL%"=="1" (
    echo Installing/refreshing frontend dependencies...
    cd frontend
    call npm install
    if errorlevel 1 (
        echo [ERROR] npm install failed.
        cd ..
        pause
        exit /b 1
    )
    cd ..
    echo.
)

:: ── Delete stale PID file ──
del .backend_pid.tmp >nul 2>&1

:: ── Start backend in background (same console) ──
echo [1/2] Starting Python Backend (port %WB_PORT%)...
set BACKEND_LOG=backend_output.log
start /b "" ".\venv\Scripts\python.exe" main.py > %BACKEND_LOG% 2>&1

:: ── Wait for backend to write its PID file ──
set RETRIES=0
:wait_pid
if exist .backend_pid.tmp goto health_check
set /a RETRIES+=1
if %RETRIES% gtr 10 (
    echo [ERROR] Backend process failed to start.
    type %BACKEND_LOG% 2>nul
    pause
    exit /b 1
)
timeout /t 1 >nul
goto wait_pid

:: ── Wait for backend health endpoint ──
:health_check
echo Waiting for backend to be ready...

set RETRIES=0
:wait_backend
powershell -Command "try { Invoke-WebRequest http://127.0.0.1:%WB_PORT%/api/health -TimeoutSec 2 -UseBasicParsing | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    powershell -Command "$h = Invoke-RestMethod http://127.0.0.1:%WB_PORT%/api/health; Write-Host ('  Mode: ' + $h.llm_mode); Write-Host ('  Modules: ' + $h.modules.Count); Write-Host ('  Memory: ' + $h.memory.status)"
    goto backend_ready
)

set /a RETRIES+=1
if %RETRIES% gtr 30 (
    echo.
    echo [ERROR] Backend failed to respond after 30 seconds.
    echo Last backend output:
    type %BACKEND_LOG% 2>nul
    call :cleanup
    pause
    exit /b 1
)
<nul set /p "=."
timeout /t 1 >nul
goto wait_backend

:backend_ready
echo Backend ready!
echo.

:: ── Start frontend in foreground ──
echo [2/2] Starting React Frontend (Vite)...
echo.
echo ==== Open http://localhost:5173 in your browser        ====
echo ==== On the same network, other devices can connect at: ====
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=* delims= " %%b in ("%%a") do echo ====   http://%%b:5173
)
echo ==== Press Ctrl+C to stop both servers                  ====
echo.

cd frontend
call npm run dev

:: ── Cleanup when frontend exits ──
cd ..
echo.
echo Shutting down backend...
call :cleanup
echo WorldBox stopped.
exit /b 0


:cleanup
if exist .backend_pid.tmp (
    set /p KILL_PID=<.backend_pid.tmp
    if defined KILL_PID (
        taskkill /pid !KILL_PID! /f >nul 2>&1
        echo Backend process (!KILL_PID!^) stopped.
    )
    del .backend_pid.tmp >nul 2>&1
)
if exist %BACKEND_LOG% del %BACKEND_LOG% >nul 2>&1
exit /b
