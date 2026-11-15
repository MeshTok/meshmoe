@echo off
REM ===========================================================================
REM MeshMoe Node - optional run-at-logon (user-level, reversible).
REM Run once. Undo with:  schtasks /delete /tn "MeshMoe Node" /f
REM ASCII only.
REM ===========================================================================
setlocal
set START=%LOCALAPPDATA%\MeshMoe\scripts\start-node.cmd
if not exist "%START%" ( echo [MeshMoe] ERROR: not installed: %START% & pause & exit /b 1 )
schtasks /create /tn "MeshMoe Node" /sc onlogon /rl highest /tr "cmd /c \"%START%\"" /f
if errorlevel 1 ( echo [MeshMoe] ERROR: schtasks failed. Run from an elevated command prompt. & pause & exit /b 1 )
echo [MeshMoe] Autostart enabled. Undo: schtasks /delete /tn "MeshMoe Node" /f
pause
endlocal
exit /b 0
