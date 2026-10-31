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

Section "Stoa"
  ; 先关掉正在运行的旧版本，再覆盖
  nsExec::Exec 'taskkill /F /IM Stoa.exe'
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
  nsExec::Exec 'taskkill /F /IM Stoa.exe'
  Delete "$DESKTOP\Stoa.lnk"
  RMDir /r "$SMPROGRAMS\Stoa"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  ; 数据（%LOCALAPPDATA%\Stoa：记忆、对话、设置）保留
SectionEnd
