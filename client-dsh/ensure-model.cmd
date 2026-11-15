@echo off
REM ===========================================================================
REM MeshMoe - ensure the model is available before starting.
REM Called automatically by start-node.cmd / start-client.cmd.
REM Strategy (in order):
REM   1. Reuse an existing single .gguf (dev checkout / previous install)
REM   2. Use standard GGUF shards already present in runtime\models
REM   3. Copy shards from the install media (D:\...\_model_shards or CD)
REM   4. Download the two <1.4GB shards from the GitHub release
REM Sets MESHMOE_MODEL_FILE and MESHMOE_MODEL_SHARD1 for the caller.
REM ASCII only.
REM ===========================================================================
setlocal enabledelayedexpansion
set MODELDIR=%~1
if "%MODELDIR%"=="" set MODELDIR=%~dp0..\runtime\models
set S1=%MODELDIR%\MeshMoe-4B-00001-of-00002.gguf
set S2=%MODELDIR%\MeshMoe-4B-00002-of-00002.gguf
set SINGLE=%MODELDIR%\MeshMoe-4B-Q4_K_M.gguf
set BASE=https://github.com/MeshTok/meshmoe/releases/latest/download

if exist "%S1%" if exist "%S2%" (
  echo [model] using local GGUF shards
  endlocal & set MESHMOE_MODEL_SHARD1=%S1% & exit /b 0
)
if exist "%SINGLE%" (
  echo [model] using local single-file gguf
  endlocal & set MESHMOE_MODEL_FILE=%SINGLE% & exit /b 0
)

REM dev checkout: model lives in these known places
for %%P in ("%~dp0..\..\dsh-integration\runtime\models\MeshMoe-4B-Q4_K_M.gguf" "%~dp0..\..\node\models\NeoHorse-1-4B-Q4_K_M.gguf" "%~dp0..\..\eval\models\NeoHorse-1-4B-Q4_K_M.gguf") do (
  if exist %%~P (
    echo [model] reusing dev model %%~P
    endlocal & set MESHMOE_MODEL_FILE=%%~P & exit /b 0
  )
)

REM install media shards (usb / extracted release next to installer)
for %%D in ("%~dp0..\_model_shards" "%~dp0_model_shards" "D:\_model_shards") do (
  if exist "%%~D\MeshMoe-4B-00001-of-00002.gguf" (
    echo [model] copying shards from %%~D ...
    if not exist "%MODELDIR%" mkdir "%MODELDIR%"
    copy /y "%%~D\*.gguf" "%MODELDIR%\" >nul
    endlocal & set MESHMOE_MODEL_SHARD1=%S1% & exit /b 0
  )
)

echo [model] GGUF shards not found - downloading from release (2 x ~1.3GB) ...
if not exist "%MODELDIR%" mkdir "%MODELDIR%"
where curl >nul 2>&1
if errorlevel 1 (
  echo [ERROR] curl not found. Download the two shards manually:
  echo         %BASE%/MeshMoe-4B-00001-of-00002.gguf
  echo         %BASE%/MeshMoe-4B-00002-of-00002.gguf
  echo         into %MODELDIR% and re-run.
  endlocal & exit /b 1
)
curl -fL --retry 3 -o "%S1%.part" "%BASE%/MeshMoe-4B-00001-of-00002.gguf"
if errorlevel 1 goto dl_fail
curl -fL --retry 3 -o "%S2%.part" "%BASE%/MeshMoe-4B-00002-of-00002.gguf"
if errorlevel 1 goto dl_fail
move /y "%S1%.part" "%S1%" >nul
move /y "%S2%.part" "%S2%" >nul
echo [model] download complete
endlocal & set MESHMOE_MODEL_SHARD1=%S1% & exit /b 0

:dl_fail
echo [ERROR] model download failed. Check your network, or fetch the two
echo         shards manually from %BASE% into %MODELDIR%.
endlocal & exit /b 1
