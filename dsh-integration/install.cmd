@echo off
rem ============================================================
rem meshmoe Desktop installer prototype (v0.1)
rem Steps:
rem   1. deploy runtime (llama-bin + model) from install media
rem   2. verify local model endpoint
rem   3. print DSH integration instructions
rem NOTE: This is the packaging skeleton. The production installer
rem       will ship as an NSIS/MSI bundle containing runtime\ and
rem       a portable DSH, and register the mesh node service.
rem ============================================================
setlocal EnableExtensions

set "SRC=%~dp0"
set "DEST=%LOCALAPPDATA%\MeshMoe"

echo [meshmoe] Installing to %DEST% ...
if not exist "%SRC%runtime\llama-bin\llama-server.exe" (
  echo [meshmoe] ERROR: install media incomplete - runtime\llama-bin missing
  exit /b 1
)
if not exist "%SRC%runtime\models\NeoHorse-1-4B-Q4_K_M.gguf" (
  echo [meshmoe] ERROR: install media incomplete - model file missing
  exit /b 1
)

robocopy "%SRC%runtime\llama-bin" "%DEST%\runtime\llama-bin" /E /NFL /NDL /NJH /NJS >nul
robocopy "%SRC%runtime\models" "%DEST%\runtime\models" /E /NFL /NDL /NJH /NJS >nul
copy /y "%SRC%meshmoe-patch.yml" "%DEST%\meshmoe-patch.yml" >nul
copy /y "%SRC%run-meshmoe-dsh.cmd" "%DEST%\run-meshmoe-dsh.cmd" >nul
echo [meshmoe] Runtime deployed (%DEST%\runtime)

echo [meshmoe] Verifying model endpoint...
start "meshmoe-llama-server" /min "%DEST%\runtime\llama-bin\llama-server.exe" -m "%DEST%\runtime\models\NeoHorse-1-4B-Q4_K_M.gguf" --port 8081 -ngl 99 -c 32768 --reasoning-budget 0 --jinja --host 127.0.0.1
ping -n 6 127.0.0.1 >nul
curl -s --max-time 3 http://127.0.0.1:8081/health | findstr ok >nul
if errorlevel 1 (
  echo [meshmoe] WARNING: model server did not respond yet; it may still be loading.
) else (
  echo [meshmoe] Local model server is healthy on port 8081.
)

echo.
echo [meshmoe] Done. Integration options:
echo   A. Bundled DSH: run %DEST%\run-meshmoe-dsh.cmd desktop
echo   B. Existing DSH: copy meshmoe-patch.yml and launch with
echo      dsh headless --patch "%DEST%\meshmoe-patch.yml" "task"
echo   C. Mesh node membership: edge_node v3 integration (P0 item,
echo      tool-calling passthrough + node_token ownership) - next release.
exit /b 0
