; ============================================================
; MeshMoe Client (DSH-based) - NSIS installer skeleton
; Build:  makensis client-dsh\installer.nsi
; Layout: CLIENT-PIVOT.md "What ships" section
; ASCII only.
; ============================================================

!define APPNAME "MeshMoe Client"
!define COMPANY "MeshMoe"
!define VERSION "4.0.0"

Name "${APPNAME}"
OutFile "dist\MeshMoe-Client-${VERSION}-x64-setup.exe"
InstallDir "$LOCALAPPDATA\MeshMoe"
RequestExecutionLevel user
XPStyle on

Page directory
Page instfiles

Section "Runtime (llama.cpp + MeshMoe-4B)"
    SetOutPath "$INSTDIR\runtime\llama-bin"
    File /r "..\dsh-integration\runtime\llama-bin\*.*"

    SetOutPath "$INSTDIR\runtime"
    ; ~2.7 GB model: excluded from git, provide via MESHSRC dir at build time
    File "..\dsh-integration\runtime\MeshMoe-4B-Q4_K_M.gguf"
SectionEnd

Section "DSH Harness"
    SetOutPath "$INSTDIR\dsh"
    ; DSH app bundle goes here (npm pack of dsh desktop, or copy of installed dir)
    File /r "bundle\dsh\*.*"
SectionEnd

Section "dsh-plugin-meshmoe"
    SetOutPath "$INSTDIR\dsh-plugin-meshmoe"
    File /r "..\dsh-meshmoe\*.*"
SectionEnd

Section "Scripts"
    SetOutPath "$INSTDIR\scripts"
    File "start-client.cmd"

    ; shortcut on desktop
    CreateShortCut "$DESKTOP\MeshMoe.lnk" "$INSTDIR\scripts\start-client.cmd" \
        "" "$INSTDIR\runtime\llama-bin\llama-server.exe"
SectionEnd

Section "Edge Node (mesh membership)"
    SetOutPath "$INSTDIR\node"
    File "..\node\edge_node.py"

    ; anonymous self-registration on first boot, token -> ~/.meshmoe/node.json
    ; handled by edge_node.py self_register_token() at startup
SectionEnd
