@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title WorldBox

echo ==============================================
echo       WorldBox AI RPG Engine - Startup
echo ==============================================
echo.

:: ── Preflight: Python virtual environment ──
if not exist ".\venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found at .\venv
    echo Run: python -m venv venv
    echo Then: .\venv\Scripts\activate.bat ^&^& pip install -r requirements.txt
    echo.
    pause
    exit /b 1
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
if not exist ".\frontend\node_modules" (
    echo [WARN] frontend\node_modules not found. Installing...
    cd frontend
    call npm install
    cd ..
    if errorlevel 1 (
        echo [ERROR] npm install failed.
        pause
        exit /b 1
    )
    echo.
)

:: ── Delete stale PID file ──
del .backend_pid.tmp >nul 2>&1

:: ── Start backend in background (same console) ──
echo [1/2] Starting Python Backend (port 8000)...
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
powershell -Command "try { Invoke-WebRequest http://127.0.0.1:8000/api/health -TimeoutSec 2 -UseBasicParsing | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    powershell -Command "$h = Invoke-RestMethod http://127.0.0.1:8000/api/health; Write-Host ('  Mode: ' + $h.llm_mode); Write-Host ('  Modules: ' + $h.modules.Count); Write-Host ('  Memory: ' + $h.memory.status)"
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
echo ==== Open http://localhost:5173 in your browser ====
echo ==== Press Ctrl+C to stop both servers           ====
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
