@echo off
rem ============================================================
rem meshmoe Desktop launcher - local model + DSH, no registration
rem Usage:
rem   run-meshmoe-dsh.cmd headless "task..."   one-shot task mode
rem   run-meshmoe-dsh.cmd desktop              open DSH desktop app
rem   run-meshmoe-dsh.cmd serve                only start local model server
rem ============================================================
setlocal EnableExtensions

set "MESHMOE_ROOT=D:\meshmoe\dsh-integration"
set "MESHMOE_RUNTIME=%MESHMOE_ROOT%\runtime"
set "MESHMOE_MODEL=%MESHMOE_RUNTIME%\models\MeshMoe-4B-Q4_K_M.gguf"
set "MESHMOE_PORT=8081"
set "DSH_CMD=D:\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"
set "LLAMA_SERVER=%MESHMOE_RUNTIME%\llama-bin\llama-server.exe"
rem DSH pi-ai requires apiKeyEnv resolution; local server does not check it,
rem any non-empty value works => zero-registration local AI
set "MESHMOE_LOCAL_KEY=local"

if not exist "%LLAMA_SERVER%" (
  echo [meshmoe] llama-server not found: %LLAMA_SERVER%
  echo [meshmoe] Run install.cmd first to deploy the runtime.
  exit /b 1
)

rem ---- start or reuse local model server ----
curl -s --max-time 2 http://127.0.0.1:%MESHMOE_PORT%/health >nul 2>&1
if not errorlevel 1 goto ready

echo [meshmoe] Starting local model server on port %MESHMOE_PORT% ...
start "meshmoe-llama-server" /min "%LLAMA_SERVER%" -m "%MESHMOE_MODEL%" --port %MESHMOE_PORT% -ngl 99 -c 32768 --reasoning-budget 0 --jinja --host 127.0.0.1

set /a tries=0
:waitloop
ping -n 3 127.0.0.1 >nul
curl -s --max-time 2 http://127.0.0.1:%MESHMOE_PORT%/health >nul 2>&1
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% lss 30 goto waitloop
echo [meshmoe] Model server failed to start within 60s.
exit /b 1

:ready
echo [meshmoe] Local model ready: MeshMoe-4B Q4_K_M at 127.0.0.1:%MESHMOE_PORT%

rem ---- dispatch to target mode ----
if "%~1"=="serve" exit /b 0
if "%~1"=="desktop" (
  echo [meshmoe] Opening DSH desktop...
  start "" "D:\DeepSeek Harness\DeepSeek Harness.exe"
  exit /b 0
)
if "%~1"=="headless" (
  shift
  "%DSH_CMD%" headless --patch "%MESHMOE_ROOT%\meshmoe-patch.yml" %2 %3 %4 %5 %6 %7 %8 %9
  exit /b %errorlevel%
)

echo Usage: run-meshmoe-dsh.cmd [headless "task" ^| desktop ^| serve]
exit /b 1
