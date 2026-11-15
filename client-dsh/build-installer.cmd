@echo off
REM ===========================================================================
REM MeshMoe Node Client - build the NSIS installer from a checkout.
REM Usage:  build-installer.cmd            (full build, copies the 2.7GB model)
REM         build-installer.cmd quick      (no model copy; dev smoke build)
REM ASCII only.
REM ===========================================================================
setlocal EnableExtensions
set REPO=%~dp0..
set STAGE=%REPO%\client-dsh\_stage
set DIST=%REPO%\client-dsh\dist
set NSIS="C:\Program Files (x86)\NSIS\makensis.exe"
set QUICK=%1
if not exist "%DIST%" mkdir "%DIST%"
echo [build] repo  = %REPO%
echo [build] stage = %STAGE%
if not exist %NSIS% ( echo [build] ERROR: makensis.exe not found at %NSIS% & exit /b 1 )
if not exist "%REPO%\dsh-integration\runtime\llama-bin\llama-server.exe" ( echo [build] ERROR: missing runtime\llama-bin & exit /b 1 )
set MODEL=%REPO%\dsh-integration\runtime\models\MeshMoe-4B-Q4_K_M.gguf
if not exist "%MODEL%" set MODEL=%REPO%\node\models\NeoHorse-1-4B-Q4_K_M.gguf
if not exist "%MODEL%" set MODEL=%REPO%\eval\models\NeoHorse-1-4B-Q4_K_M.gguf
if not exist "%MODEL%" ( echo [build] ERROR: no model .gguf found. Put MeshMoe-4B-Q4_K_M.gguf in dsh-integration\runtime\models\ & exit /b 1 )
echo [build] model = %MODEL%
if not exist "%REPO%\node\edge_node.py" ( echo [build] ERROR: missing node\edge_node.py & exit /b 1 )
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%\runtime\llama-bin" 2>nul
mkdir "%STAGE%\runtime\models" 2>nul
mkdir "%STAGE%\node" 2>nul
mkdir "%STAGE%\scripts" 2>nul
mkdir "%STAGE%\python" 2>nul
robocopy "%REPO%\dsh-integration\runtime\llama-bin" "%STAGE%\runtime\llama-bin" /E /NFL /NDL /NJH /NJS /NC /NS >nul
if errorlevel 8 ( echo [build] ERROR: robocopy llama-bin failed & exit /b 1 )
REM Bundled embeddable Python (stdlib only, ~39MB): the node needs no system
REM Python. Build it once with prepare-python.cmd from a local Python 3.12.
if not exist "%REPO%\client-dsh\_python\python.exe" (
  echo [build] ERROR: bundled python missing - run prepare-python.cmd first
  exit /b 1
)
robocopy "%REPO%\client-dsh\_python" "%STAGE%\python" /E /NFL /NDL /NJH /NJS /NC /NS >nul
if errorlevel 8 ( echo [build] ERROR: robocopy python failed & exit /b 1 )
if /I "%QUICK%"=="quick" goto quick_mode
REM NSIS (32-bit) cannot mmap a single file >=2GB: split the 2.7GB gguf into
REM <1.4GB parts, package those, and reassemble them at install time.
echo [build] producing standalone GGUF shards for the release (not embedded in the installer) ...
if not exist "%DIST%\model-shards" mkdir "%DIST%\model-shards"
"%REPO%\dsh-integration\runtime\llama-bin\llama-gguf-split.exe" --split-max-size 1400M "%MODEL%" "%DIST%\model-shards\MeshMoe-4B" >nul 2>&1
if errorlevel 1 ( echo [build] ERROR: gguf-split failed & exit /b 1 )
echo [build] shards in %DIST%\model-shards - upload them to the GitHub release.
goto staged
:quick_mode
echo [build] QUICK mode: model NOT copied - dev smoke build only
:staged
copy /y "%REPO%\node\edge_node.py" "%STAGE%\node\edge_node.py" >nul
copy /y "%REPO%\client-dsh\start-node.cmd" "%STAGE%\scripts\start-node.cmd" >nul
copy /y "%REPO%\client-dsh\start-client.cmd" "%STAGE%\scripts\start-client.cmd" >nul
copy /y "%REPO%\client-dsh\uninstall-node.cmd" "%STAGE%\scripts\uninstall-node.cmd" >nul
copy /y "%REPO%\client-dsh\enable-autostart.cmd" "%STAGE%\scripts\enable-autostart.cmd" >nul
copy /y "%REPO%\client-dsh\ensure-model.cmd" "%STAGE%\scripts\ensure-model.cmd" >nul
copy /y "%REPO%\README.md" "%STAGE%\README.md" >nul
copy /y "%REPO%\LICENSE" "%STAGE%\LICENSE" >nul
if not exist "%DIST%" mkdir "%DIST%"
%NSIS% /DSTAGE="%STAGE%" /DDIST="%DIST%" "%REPO%\client-dsh\installer.nsi"
if errorlevel 1 ( echo [build] ERROR: makensis failed & exit /b 1 )
echo.
echo [build] DONE. Installer(s) in %DIST%
dir /b "%DIST%\*.exe"
endlocal
exit /b 0
