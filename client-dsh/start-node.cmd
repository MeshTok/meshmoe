@echo off
REM ============================================================
REM MeshMoe Share-Only Node - run this machine as a mesh node
REM   * starts llama-server (:8081) if it is not already up
REM   * starts edge_node.py (:4002) which registers, polls tasks,
REM     infers, and reports Joules to api.meshmoe.com
REM   * no DSH desktop, no UI - safe to run on a headless box
REM ASCII only. No Chinese in this file.
REM
REM Run on logon (optional, run once in an ADMIN cmd):
REM   schtasks /create /tn "MeshMoe Node" /sc onlogon /rl highest ^
REM     /tr "cmd /c \"D:\meshmoe\client-dsh\start-node.cmd\""
REM Undo:
REM   schtasks /delete /tn "MeshMoe Node" /f
REM ============================================================
setlocal enabledelayedexpansion

set ROOT=%~dp0
set REPO=%ROOT%..
set NODEDIR=%REPO%\node
set RUNTIME=%ROOT%runtime
set LLAMA=%RUNTIME%\llama-bin\llama-server.exe
set PORT=8081
set CTX=16384
set LOGDIR=%REPO%\_logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM ---- locate the model: runtime\models -> runtime -> node\models -> eval\models ----
set MODEL=
if exist "%RUNTIME%\models\NeoHorse-1-4B-Q4_K_M.gguf" set MODEL=%RUNTIME%\models\NeoHorse-1-4B-Q4_K_M.gguf
if not defined MODEL if exist "%RUNTIME%\MeshMoe-4B-Q4_K_M.gguf" set MODEL=%RUNTIME%\MeshMoe-4B-Q4_K_M.gguf
if not defined MODEL if exist "%NODEDIR%\models\NeoHorse-1-4B-Q4_K_M.gguf" set MODEL=%NODEDIR%\models\NeoHorse-1-4B-Q4_K_M.gguf
if not defined MODEL if exist "%REPO%\eval\models\NeoHorse-1-4B-Q4_K_M.gguf" set MODEL=%REPO%\eval\models\NeoHorse-1-4B-Q4_K_M.gguf
if not defined MODEL (
    echo [ERROR] MeshMoe-4B gguf not found. Put it in %RUNTIME%\models\
    pause
    exit /b 1
)
if not exist "%LLAMA%" (
    echo [ERROR] llama-server.exe not found: %LLAMA%
    echo         set MESHMOE_LLAMA_BIN to your own build, or install the full client.
    pause
    exit /b 1
)
if not exist "%NODEDIR%\edge_node.py" (
    echo [ERROR] edge_node.py not found: %NODEDIR%
    pause
    exit /b 1
)

echo [MeshMoe] model: %MODEL%

REM ---- 1. model server ----
curl -s -m 2 http://127.0.0.1:%PORT%/v1/models >nul 2>&1
if errorlevel 1 (
    echo [MeshMoe] starting llama-server on :%PORT% ...
    start "MeshMoe-llama" /min "%LLAMA%" -m "%MODEL%" --jinja -c %CTX% --reasoning-budget 0 --port %PORT% --host 127.0.0.1
    ping -n 8 127.0.0.1 >nul
) else (
    echo [MeshMoe] llama-server already running on :%PORT%
)

REM ---- 2. edge node (mesh membership + Joule metering) ----
REM If llama-server is missing entirely, edge_node falls back to spawning
REM its own llama-server (see spawn_llama_server in edge_node.py).
set MESHMOE_CONNECTOR_URL=http://127.0.0.1:%PORT%
set MESHMOE_MODEL=MeshMoe-4B-Q4_K_M
set MESHMOE_PORT=4002
if not defined MESHMOE_NODE_ID set MESHMOE_NODE_ID=%COMPUTERNAME%

echo [MeshMoe] starting edge node as %MESHMOE_NODE_ID% ...
REM ^ escapes keep the redirects for the child cmd, not for `start` itself.
start "MeshMoe-node" /min cmd /c python -u "%NODEDIR%\edge_node.py" ^>^> "%LOGDIR%\node.log" 2^>^&1

echo.
echo [MeshMoe] running. Node id: %MESHMOE_NODE_ID%
echo [MeshMoe] status : http://127.0.0.1:4002/health
echo [MeshMoe] honor  : https://meshmoe.com/claim/  (paste your node id)
echo [MeshMoe] token  : %USERPROFILE%\.meshmoe\node.json
echo [MeshMoe] log    : %LOGDIR%\node.log
endlocal
