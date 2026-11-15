@echo off
REM ===========================================================================
REM MeshMoe Node - stop running processes and remove installed files.
REM Does NOT touch ~/.meshmoe (node token / honor identity is preserved).
REM ASCII only.
REM ===========================================================================
setlocal
set DEST=%LOCALAPPDATA%\MeshMoe
echo [MeshMoe] Stopping processes...
taskkill /FI "WINDOWTITLE eq MeshMoe-llama*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq MeshMoe-node*" /F >nul 2>&1
for /f "tokens=2 delims=," %%P in ('tasklist /fi "imagename eq python.exe" /fo csv /nh 2^>nul ^| findstr /i "python"') do (
  wmic process where "ProcessId=%%~P" get CommandLine /value 2>nul | findstr /i "edge_node.py" >nul && taskkill /PID %%~P /F >nul 2>&1
)
echo [MeshMoe] Removing scheduled task (if present)...
schtasks /delete /tn "MeshMoe Node" /f >nul 2>&1
echo [MeshMoe] Removing files at %DEST% ...
if exist "%DEST%" rmdir /s /q "%DEST%"
echo [MeshMoe] Uninstalled. Your node identity at %USERPROFILE%\.meshmoe is kept.
echo [MeshMoe] To delete that too, remove the folder manually.
pause
endlocal
exit /b 0
