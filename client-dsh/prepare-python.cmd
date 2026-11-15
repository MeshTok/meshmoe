@echo off
REM ===========================================================================
REM Build the bundled embeddable Python runtime (stdlib only, ~39MB) from a
REM local Python 3.12 install. Run once on a build machine, then
REM build-installer.cmd picks up client-dsh\_python automatically.
REM Usage: prepare-python.cmd [path-to-python-install]   (default D:\Python312)
REM ASCII only.
REM ===========================================================================
setlocal
set SRC=%1
if "%SRC%"=="" set SRC=D:\Python312
set DST=%~dp0_python
if not exist "%SRC%\python.exe" ( echo [py] ERROR: %SRC%\python.exe not found & exit /b 1 )
if exist "%DST%" rmdir /s /q "%DST%"
mkdir "%DST%" 2>nul
for %%F in (python.exe pythonw.exe python312.dll python3.dll vcruntime140.dll vcruntime140_1.dll LICENSE.txt) do copy /y "%SRC%\%%F" "%DST%\%%F" >nul
robocopy "%SRC%\DLLs" "%DST%\DLLs" /E /NFL /NDL /NJH /NJS /NC /NS >nul
robocopy "%SRC%\Lib" "%DST%\Lib" /E /NFL /NDL /NJH /NJS /NC /NS >nul
for %%D in (site-packages test idlelib tkinter turtledemo ensurepip) do if exist "%DST%\Lib\%%D" rmdir /s /q "%DST%\Lib\%%D"
"%DST%\python.exe" -c "import json,urllib.request,http.server,socket,threading,platform,ctypes,sqlite3;print('[py] stdlib OK')"
echo [py] bundled runtime built at %DST%
endlocal
exit /b 0
