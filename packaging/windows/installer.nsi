; Stoa 单文件安装包：装到 %LOCALAPPDATA%\Programs\Stoa（不需要管理员），建桌面和开始菜单快捷方式，装完直接打开
Unicode true
SetCompressor /SOLID lzma
!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef SRC
  !define SRC "..\..\dist\Stoa"
!endif
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Stoa"
Name "Stoa"
Caption "Stoa ${VERSION} 安装"
OutFile "..\..\dist\Stoa-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\Stoa"
RequestExecutionLevel user
Icon "icon.ico"
UninstallIcon "icon.ico"
BrandingText "Stoa ${VERSION}"
ShowInstDetails nevershow
!include "MUI2.nsh"
!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\Stoa.exe"
!define MUI_FINISHPAGE_RUN_TEXT "现在打开 Stoa"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

!macro KillStoa
  ; 连同子进程一起关掉，再清掉从安装目录启动的残留进程（内置 python 等），避免文件被占用删不掉
  nsExec::Exec 'taskkill /F /T /IM Stoa.exe'
  nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $$_.Path -like '$INSTDIR\*' } | Stop-Process -Force -ErrorAction SilentlyContinue"`
  Sleep 1500
!macroend

Section "Stoa"
  ; 先关掉正在运行的旧版本，再覆盖
  !insertmacro KillStoa
  SetOutPath "$INSTDIR"
  File /r "${SRC}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortCut "$DESKTOP\Stoa.lnk" "$INSTDIR\Stoa.exe" "" "$INSTDIR\Stoa.ico"
  CreateDirectory "$SMPROGRAMS\Stoa"
  CreateShortCut "$SMPROGRAMS\Stoa\Stoa.lnk" "$INSTDIR\Stoa.exe" "" "$INSTDIR\Stoa.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "Stoa"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Stoa"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\Stoa.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  !insertmacro KillStoa
  Delete "$DESKTOP\Stoa.lnk"
  RMDir /r "$SMPROGRAMS\Stoa"
  RMDir /r "$INSTDIR"
  ; 一键更新留下的旧版备份（Stoa.previous-*）、暂存目录和更新结果一起删
  nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -Path '$INSTDIR.previous-*','$INSTDIR.next-*','$INSTDIR.update-result.json' -Recurse -Force -ErrorAction SilentlyContinue"`
  IfFileExists "$INSTDIR\*.*" 0 removed
    Sleep 2000
    !insertmacro KillStoa
    RMDir /r /REBOOTOK "$INSTDIR"
  removed:
  DeleteRegKey HKCU "${UNINST_KEY}"
  ; 数据（%LOCALAPPDATA%\Stoa：记忆、对话、设置）保留
SectionEnd
