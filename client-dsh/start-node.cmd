@echo off
REM ===========================================================================
REM MeshMoe Share-Only Node - run this machine as a mesh node.
REM   * starts llama-server (:8081) if not already up
REM   * starts edge_node.py (:4002): registers, polls tasks, infers, reports Joules
REM   * no DSH desktop, no UI - safe on a headless box
REM Works in BOTH layouts:
REM   installed : %LOCALAPPDATA%\MeshMoe\scripts\start-node.cmd
REM   checkout  : D:\meshmoe\client-dsh\start-node.cmd
REM CPU-only: set MESHMOE_NO_GPU=1 before running (uses -ngl 0).
REM Autostart:  enable-autostart.cmd   Undo: schtasks /delete /tn "MeshMoe Node" /f
REM ASCII only.
REM ===========================================================================
setlocal enabledelayedexpansion
set SCRIPTDIR=%~dp0
set INST=%SCRIPTDIR%..
if not exist "%INST%\node\edge_node.py" set INST=%INST%\..
if exist "%INST%\node\edge_node.py" goto inst_ok
echo [ERROR] Cannot locate MeshMoe installation - node\edge_node.py not found
echo         Tried %SCRIPTDIR%.. and %SCRIPTDIR%..\..
exit /b 1
:inst_ok
set NODEDIR=%INST%\node
set RUNTIME=%INST%\runtime
set LLAMA=%RUNTIME%\llama-bin\llama-server.exe
REM --- model provisioning (reuse / local shards / install media / download) ---
call "%SCRIPTDIR%ensure-model.cmd" "%RUNTIME%\models"
if errorlevel 1 ( echo [ERROR] model unavailable & exit /b 1 )
if defined MESHMOE_MODEL_SHARD1 set MODEL=%MESHMOE_MODEL_SHARD1%
if defined MESHMOE_MODEL_FILE set MODEL=%MESHMOE_MODEL_FILE%
if not exist "%MODEL%" ( echo [ERROR] model not found: %MODEL% & exit /b 1 )
echo [MeshMoe] model: %MODEL%
if not exist "%LLAMA%" if exist "%INST%\dsh-integration\runtime\llama-bin\llama-server.exe" set LLAMA=%INST%\dsh-integration\runtime\llama-bin\llama-server.exe
if not exist "%LLAMA%" if exist "%INST%\eval\tools\llama-bin\llama-server.exe" set LLAMA=%INST%\eval\tools\llama-bin\llama-server.exe
set PORT=8081
if defined MESHMOE_LLAMA_PORT set PORT=%MESHMOE_LLAMA_PORT%
set CTX=16384
set LOGDIR=%INST%\_logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if not exist "%LLAMA%" ( echo [ERROR] llama-server.exe not found: %LLAMA% & exit /b 1 )
curl -s -m 2 http://127.0.0.1:%PORT%/v1/models >nul 2>&1
if errorlevel 1 (
  echo [MeshMoe] starting llama-server on :%PORT% ...
  if defined MESHMOE_NO_GPU ( set NGL=0 ) else ( set NGL=99 )
  start "MeshMoe-llama" /min "%LLAMA%" -m "%MODEL%" --jinja -c %CTX% --reasoning-budget 0 -ngl !NGL! --port %PORT% --host 127.0.0.1
  ping -n 8 127.0.0.1 >nul
) else ( echo [MeshMoe] llama-server already running on :%PORT% )
set MESHMOE_CONNECTOR_URL=http://127.0.0.1:%PORT%
set MESHMOE_MODEL=MeshMoe-4B-Q4_K_M
set MESHMOE_MODEL_FILE=%MODEL%
set MESHMOE_PORT=4002
if defined MESHMOE_PORT_OVERRIDE set MESHMOE_PORT=%MESHMOE_PORT_OVERRIDE%
if not defined MESHMOE_NODE_ID set MESHMOE_NODE_ID=%COMPUTERNAME%
REM Prefer bundled embeddable Python; fall back to system python / py launcher.
set PYEXE=%INST%\python\python.exe
if not exist "%PYEXE%" set PYEXE=python
if "%PYEXE%"=="python" (
  where python >nul 2>&1
  if errorlevel 1 set PYEXE=py
)
echo [MeshMoe] starting edge node as %MESHMOE_NODE_ID% using %PYEXE% ...
start "MeshMoe-node" /min cmd /c "%PYEXE%" -u "%NODEDIR%\edge_node.py" ^>^> "%LOGDIR%\node.log" 2^>^&1
echo.
echo [MeshMoe] running. Node id: %MESHMOE_NODE_ID%
echo [MeshMoe] status : http://127.0.0.1:%MESHMOE_PORT%/health
echo [MeshMoe] honor  : https://meshmoe.com/claim/  (paste your node id)
echo [MeshMoe] token  : %USERPROFILE%\.meshmoe\node.json
echo [MeshMoe] log    : %LOGDIR%\node.log
endlocal
exit /b 0
