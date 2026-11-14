@echo off
REM ============================================================
REM MeshMoe Client (DSH-based) - one-click starter
REM Boot order: llama-server (:8081) -> edge node -> DSH desktop
REM ASCII only. No Chinese in this file.
REM ============================================================
setlocal enabledelayedexpansion

set ROOT=%~dp0
set RUNTIME=%ROOT%runtime
set LLAMA=%RUNTIME%\llama-bin\llama-server.exe
set MODEL=%RUNTIME%\MeshMoe-4B-Q4_K_M.gguf
set PORT=8081
set CTX=32768

if not exist "%LLAMA%" (
    echo [ERROR] llama-server.exe not found: %LLAMA%
    pause
    exit /b 1
)
if not exist "%MODEL%" (
    echo [ERROR] model file not found: %MODEL%
    pause
    exit /b 1
)

REM --- 1. start model server if not already up ---
curl -s -m 2 http://127.0.0.1:%PORT%/health >nul 2>&1
if errorlevel 1 (
    echo [MeshMoe] starting llama-server on :%PORT% ...
    start "MeshMoe-llama" /min "%LLAMA%" -m "%MODEL%" --jinja -c %CTX% --reasoning-budget 0 --port %PORT% --host 127.0.0.1
    ping -n 6 127.0.0.1 >nul
) else (
    echo [MeshMoe] llama-server already running on :%PORT%
)

REM --- 2. start edge node (mesh membership + Joule metering) ---
if exist "%ROOT%node\edge_node.py" (
    echo [MeshMoe] starting edge node ...
    start "MeshMoe-node" /min python "%ROOT%node\edge_node.py" --port 4002
) else (
    echo [MeshMoe] edge_node.py not found, skip mesh membership
)

REM --- 3. launch DSH desktop ---
echo [MeshMoe] launching DSH ...
call dsh desktop
if errorlevel 1 (
    echo [ERROR] dsh not found in PATH. Install DeepSeek Harness first.
    pause
    exit /b 1
)
endlocal
