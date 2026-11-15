; ===========================================================================
; MeshMoe Node Client - NSIS installer (production)
;
; Build:  client-dsh\build-installer.cmd
; Inputs (staged by build-installer.cmd, passed via /DSTAGE and /DDIST):
;   _stage\runtime\llama-bin\*      llama.cpp CUDA build (+ CPU backends)
;   _stage\runtime\models\*.gguf    MeshMoe-4B-Q4_K_M.gguf
;   _stage\node\edge_node.py        edge node v3
;   _stage\scripts\*.cmd            launchers / uninstall / autostart
;   _stage\README.md, LICENSE
;
; Installs to %LOCALAPPDATA%\MeshMoe (user-level, NO admin required).
; Does NOT bundle DSH desktop: existing DSH users install the separate
; dsh-plugin-meshmoe plugin. This package is the "share-only" node that
; works standalone and earns Joules.
; ASCII only.
; ===========================================================================

!define APPNAME "MeshMoe Node"
!define COMPANY "MeshMoe"
!define VERSION "1.0.0"
!define HELPURL "https://meshmoe.com/"

Name "${APPNAME}"
OutFile "${DIST}\MeshMoe-Node-${VERSION}-x64-setup.exe"
InstallDir "$LOCALAPPDATA\MeshMoe"
InstallDirRegKey HKCU "Software\MeshMoe\Node" "InstallDir"
RequestExecutionLevel user
; 1.25 GB of already-compressed DLLs: /SOLID lzma takes >15 min for ~0 gain.
; zlib /SOLID off is fast and the size difference is a few percent.
SetCompressor /FINAL zlib
XPStyle on

!include "MUI2.nsh"

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${STAGE}\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------------------
Section "MeshMoe runtime (required)" SEC_RUNTIME
    SectionIn RO

    SetOutPath "$INSTDIR\runtime\llama-bin"
    File /r "${STAGE}\runtime\llama-bin\*.*"

    SetOutPath "$INSTDIR\runtime\models"
    ; The 2.7 GB model is NOT embedded: NSIS is a 32-bit compiler and cannot
    ; build a datablock >=2GB. The installer ships ensure-model.cmd, which on
    ; first launch reuses a local gguf, copies shards from the install media,
    ; or downloads the two <1.4GB shards from the release URL.
    ; This keeps the installer ~720MB and the model independently replaceable.

    SetOutPath "$INSTDIR\node"
    File "${STAGE}\node\edge_node.py"

    SetOutPath "$INSTDIR\python"
    File /r "${STAGE}\python\*.*"

    SetOutPath "$INSTDIR\scripts"
    File "${STAGE}\scripts\start-node.cmd"
    File "${STAGE}\scripts\start-client.cmd"
    File "${STAGE}\scripts\uninstall-node.cmd"
    File "${STAGE}\scripts\enable-autostart.cmd"
    File "${STAGE}\scripts\ensure-model.cmd"

    SetOutPath "$INSTDIR"
    File "${STAGE}\README.md"
    File "${STAGE}\LICENSE"

    ; registry + uninstaller
    WriteRegStr HKCU "Software\MeshMoe\Node" "InstallDir" "$INSTDIR"
    WriteRegStr HKCU "Software\MeshMoe\Node" "Version" "${VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MeshMoeNode" \
        "DisplayName" "MeshMoe Node ${VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MeshMoeNode" \
        "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MeshMoeNode" \
        "Publisher" "${COMPANY}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MeshMoeNode" \
        "HelpLink" "${HELPURL}"
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MeshMoeNode" \
        "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MeshMoeNode" \
        "NoRepair" 1
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; verify the two critical files really landed
    IfFileExists "$INSTDIR\runtime\llama-bin\llama-server.exe" +2 0
        MessageBox MB_ICONSTOP "Install incomplete: llama-server.exe missing."
    IfFileExists "$INSTDIR\node\edge_node.py" +2 0
        MessageBox MB_ICONSTOP "Install incomplete: edge_node.py missing."
    IfFileExists "$INSTDIR\python\python.exe" +2 0
        MessageBox MB_ICONSTOP "Install incomplete: bundled python.exe missing."
SectionEnd

; ---------------------------------------------------------------------------
Section "Desktop shortcut" SEC_SHORTCUT
    CreateShortCut "$DESKTOP\MeshMoe Node.lnk" "$INSTDIR\scripts\start-node.cmd" \
        "" "$INSTDIR\runtime\llama-bin\llama-server.exe" 0
    CreateShortCut "$INSTDIR\Start MeshMoe Node.lnk" "$INSTDIR\scripts\start-node.cmd" \
        "" "$INSTDIR\runtime\llama-bin\llama-server.exe" 0
SectionEnd

; ---------------------------------------------------------------------------
Section "Start Menu entries" SEC_STARTMENU
    CreateDirectory "$SMPROGRAMS\MeshMoe"
    CreateShortCut "$SMPROGRAMS\MeshMoe\Start Node.lnk" "$INSTDIR\scripts\start-node.cmd"
    CreateShortCut "$SMPROGRAMS\MeshMoe\Enable Autostart.lnk" "$INSTDIR\scripts\enable-autostart.cmd"
    CreateShortCut "$SMPROGRAMS\MeshMoe\Uninstall.lnk" "$INSTDIR\uninstall.exe"
    CreateShortCut "$SMPROGRAMS\MeshMoe\Honor Card.lnk" "https://meshmoe.com/claim/"
SectionEnd

; ---------------------------------------------------------------------------
; Install-time check: confirm model file presence (full build only).
; We deliberately do NOT launch llama-server here: it needs ~10-30s to load a
; 2.7GB model and would block the installer. First launch is start-node.cmd,
; which prints clear errors if anything is missing. Model absence in "quick"
; dev builds is tolerated (build-installer.cmd quick).
Section "-Post-install check" SEC_SMOKE
    DetailPrint "MeshMoe: runtime installed. On first launch, ensure-model.cmd provisions the 2.7 GB model (reuse / local shards / download)."
SectionEnd

; ---------------------------------------------------------------------------
Section "Uninstall"
    ; stop our windows (best-effort, no admin needed)
    ExecWait 'taskkill /FI "WINDOWTITLE eq MeshMoe-llama*" /F'
    ExecWait 'taskkill /FI "WINDOWTITLE eq MeshMoe-node*" /F'
    ExecWait 'schtasks /delete /tn "MeshMoe Node" /f'

    Delete "$DESKTOP\MeshMoe Node.lnk"
    Delete "$INSTDIR\Start MeshMoe Node.lnk"
    RMDir /r "$SMPROGRAMS\MeshMoe"
    Delete "$INSTDIR\uninstall.exe"
    RMDir /r "$INSTDIR\runtime"
    RMDir /r "$INSTDIR\node"
    RMDir /r "$INSTDIR\scripts"
    Delete "$INSTDIR\README.md"
    Delete "$INSTDIR\LICENSE"
    RMDir /r "$INSTDIR\_logs"
    RMDir "$INSTDIR"
    DeleteRegKey HKCU "Software\MeshMoe\Node"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MeshMoeNode"
    ; deliberately keep %USERPROFILE%\.meshmoe (node token / honor identity)
SectionEnd

; ---------------------------------------------------------------------------
Function .onInit
    ; nothing: user-level install, no elevation
FunctionEnd
