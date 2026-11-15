@echo off
REM ===========================================================================
REM MeshMoe Client (DSH-based) - one-click starter
REM Boot order: llama-server (:8081) -> edge node (:4002) -> DSH desktop
REM Requires: an existing DeepSeek Harness install (dsh.exe on PATH) and the
REM           dsh-plugin-meshmoe plugin. The standalone node package does not
REM           need this file - use start-node.cmd instead.
REM Works in installed layout (%LOCALAPPDATA%\MeshMoe\scripts) and checkout.
REM ASCII only.
REM ===========================================================================
setlocal enabledelayedexpansion
set SCRIPTDIR=%~dp0
set INST=%SCRIPTDIR%..
if not exist "%INST%\node\edge_node.py" set INST=%INST%\..
if not exist "%INST%\node\edge_node.py" ( echo [ERROR] MeshMoe install not found & pause & exit /b 1 )
set NODEDIR=%INST%\node
set RUNTIME=%INST%\runtime
set LLAMA=%RUNTIME%\llama-bin\llama-server.exe
REM --- model provisioning (reuse / local shards / install media / download) ---
call "%SCRIPTDIR%ensure-model.cmd" "%RUNTIME%\models"
if errorlevel 1 ( echo [ERROR] model unavailable & pause & exit /b 1 )
if defined MESHMOE_MODEL_SHARD1 set MODEL=%MESHMOE_MODEL_SHARD1%
if defined MESHMOE_MODEL_FILE set MODEL=%MESHMOE_MODEL_FILE%
if not exist "%LLAMA%" if exist "%INST%\dsh-integration\runtime\llama-bin\llama-server.exe" set LLAMA=%INST%\dsh-integration\runtime\llama-bin\llama-server.exe
if not exist "%LLAMA%" if exist "%INST%\eval\tools\llama-bin\llama-server.exe" set LLAMA=%INST%\eval\tools\llama-bin\llama-server.exe
set PORT=8081
set CTX=32768
set LOGDIR=%INST%\_logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if not exist "%LLAMA%" ( echo [ERROR] llama-server.exe not found & pause & exit /b 1 )
if not exist "%MODEL%" ( echo [ERROR] model file not found & pause & exit /b 1 )

REM --- 1. start model server if not already up (llama-server serves /v1/models) ---
curl -s -m 2 http://127.0.0.1:%PORT%/v1/models >nul 2>&1
if errorlevel 1 (
  echo [MeshMoe] starting llama-server on :%PORT% ...
  if defined MESHMOE_NO_GPU ( set NGL=0 ) else ( set NGL=99 )
  start "MeshMoe-llama" /min "%LLAMA%" -m "%MODEL%" --jinja -c %CTX% --reasoning-budget 0 -ngl !NGL! --port %PORT% --host 127.0.0.1
  ping -n 8 127.0.0.1 >nul
) else ( echo [MeshMoe] llama-server already running on :%PORT% )

REM --- 2. start edge node (env-driven; edge_node.py takes NO CLI args) ---
echo [MeshMoe] starting edge node ...
set MESHMOE_CONNECTOR_URL=http://127.0.0.1:%PORT%
set MESHMOE_MODEL=MeshMoe-4B-Q4_K_M
set MESHMOE_MODEL_FILE=%MODEL%
set MESHMOE_PORT=4002
if not defined MESHMOE_NODE_ID set MESHMOE_NODE_ID=%COMPUTERNAME%
set PYEXE=%INST%\python\python.exe
if not exist "%PYEXE%" set PYEXE=python
start "MeshMoe-node" /min cmd /c "%PYEXE%" -u "%NODEDIR%\edge_node.py" ^>^> "%LOGDIR%\node.log" 2^>^&1

REM --- 3. launch DSH desktop (pre-configured via dsh-plugin-meshmoe) ---
echo [MeshMoe] launching DSH ...
where dsh >nul 2>&1
if errorlevel 1 (
  echo [WARN] dsh not found in PATH. Install DeepSeek Harness + dsh-plugin-meshmoe.
  echo         The local model + mesh node are already running on :8081 / :4002.
  echo         Honor card: https://meshmoe.com/claim/
  pause
  exit /b 0
)
call dsh desktop
endlocal
exit /b 0
