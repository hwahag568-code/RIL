!include "MUI2.nsh"
!include "FileFunc.nsh"

; registry_key/value 자체가 바뀌어도 custom 설치 경로를 찾기 위한
; 고정 Windows product identity. 설정 가능한 값으로 만들면 bootstrap
; rename과 함께 사라지므로 의도적으로 고정한다.
!define RIL_BOOTSTRAP_REGISTRY_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\RIL"
!define RIL_BOOTSTRAP_REGISTRY_VALUE "InstallLocation"

!ifndef APP_VERSION
    !error "APP_VERSION is required"
!endif
!ifndef SERVER_INSTALLER_FILENAME
    !error "SERVER_INSTALLER_FILENAME is required"
!endif
!ifndef INSTALL_DIR
    !error "INSTALL_DIR is required"
!endif
!ifndef REGISTRY_KEY
    !error "REGISTRY_KEY is required"
!endif
!ifndef SERVER_INSTALL_REGISTRY_VALUE
    !error "SERVER_INSTALL_REGISTRY_VALUE is required"
!endif
!ifndef LEGACY_INSTALL_REGISTRY_VALUE
    !error "LEGACY_INSTALL_REGISTRY_VALUE is required"
!endif
!ifndef SERVER_VERSION_REGISTRY_VALUE
    !error "SERVER_VERSION_REGISTRY_VALUE is required"
!endif
!ifndef SERVER_EXECUTABLE
    !error "SERVER_EXECUTABLE is required"
!endif
!ifndef SERVER_BUILD_DIRECTORY
    !error "SERVER_BUILD_DIRECTORY is required"
!endif
!ifndef SERVER_RUNTIME_DIRECTORY
    !error "SERVER_RUNTIME_DIRECTORY is required"
!endif
!ifndef SERVER_START_SCRIPT
    !error "SERVER_START_SCRIPT is required"
!endif
!ifndef SERVER_START_PS1
    !error "SERVER_START_PS1 is required"
!endif
!ifndef SERVER_RESTARTER_SCRIPT
    !error "SERVER_RESTARTER_SCRIPT is required"
!endif
!ifndef SERVER_RESTARTER_PS1
    !error "SERVER_RESTARTER_PS1 is required"
!endif
!ifndef SERVER_UPDATE_HELPER
    !error "SERVER_UPDATE_HELPER is required"
!endif
!ifndef CONFIG_FILE
    !error "CONFIG_FILE is required"
!endif
!ifndef ICON_FILE
    !error "ICON_FILE is required"
!endif
!ifndef SERVER_TASK_NAME
    !error "SERVER_TASK_NAME is required"
!endif
!ifndef SERVER_RESTARTER_TASK_NAME
    !error "SERVER_RESTARTER_TASK_NAME is required"
!endif
!ifndef SERVER_RESTARTER_INTERVAL_HOURS
    !error "SERVER_RESTARTER_INTERVAL_HOURS is required"
!endif
!ifndef POWER_SHELL_EXECUTABLE
    !error "POWER_SHELL_EXECUTABLE is required"
!endif
!ifndef SHORTCUT_DIRECTORY
    !error "SHORTCUT_DIRECTORY is required"
!endif
!ifndef SERVER_START_MENU_SHORTCUT
    !error "SERVER_START_MENU_SHORTCUT is required"
!endif
!ifndef SERVER_DESKTOP_SHORTCUT
    !error "SERVER_DESKTOP_SHORTCUT is required"
!endif
!ifndef UPDATE_MUTEX_NAME
    !error "UPDATE_MUTEX_NAME is required"
!endif
!ifndef UPDATE_MUTEX_WAIT_MILLISECONDS
    !error "UPDATE_MUTEX_WAIT_MILLISECONDS is required"
!endif

Name "인터페이스 원격로그인 서버"
OutFile "release\${SERVER_INSTALLER_FILENAME}"
InstallDir "${INSTALL_DIR}"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!define MUI_ICON "${ICON_FILE}"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Korean"

Var IsUpdate

Function .onInit
    ; 서버와 64비트 PowerShell helper가 같은 registry view를 사용한다.
    SetRegView 64
    StrCpy $IsUpdate "0"
    ${GetParameters} $1
    ${GetOptions} $1 "/UPDATE" $2
    IfErrors normal_install
    ; 자동업데이트 helper가 마지막 /D= 인수로 지정한 현재 APP_DIR을
    ; 유지한다. stale registry 값으로 다른 폴더에 설치하지 않는다.
    StrCpy $IsUpdate "1"
    Goto init_done

normal_install:
    System::Call 'kernel32::SetLastError(i 0)'
    System::Call 'kernel32::CreateMutexW(p 0, i 1, w "${UPDATE_MUTEX_NAME}") p .r3 ?e'
    Pop $4
    StrCmp $3 "0" update_mutex_failed
    StrCmp $4 "183" update_mutex_wait update_mutex_acquired

update_mutex_wait:
    System::Call 'kernel32::WaitForSingleObject(p r3, i ${UPDATE_MUTEX_WAIT_MILLISECONDS}) i .r5'
    StrCmp $5 "0" update_mutex_acquired
    StrCmp $5 "128" update_mutex_acquired update_mutex_busy

update_mutex_acquired:
    ReadRegStr $0 HKLM "${REGISTRY_KEY}" "${SERVER_INSTALL_REGISTRY_VALUE}"
    ${If} $0 == ""
        ReadRegStr $0 HKLM "${REGISTRY_KEY}" "${LEGACY_INSTALL_REGISTRY_VALUE}"
    ${EndIf}
    ${If} $0 == ""
        ReadRegStr $0 HKLM "${RIL_BOOTSTRAP_REGISTRY_KEY}" "${RIL_BOOTSTRAP_REGISTRY_VALUE}"
    ${EndIf}
    ${If} $0 == ""
        ; 이전 x86 NSIS 설치본의 경로만 32비트 view에서 호환 조회한다.
        SetRegView 32
        ReadRegStr $0 HKLM "${REGISTRY_KEY}" "${SERVER_INSTALL_REGISTRY_VALUE}"
        ${If} $0 == ""
            ReadRegStr $0 HKLM "${REGISTRY_KEY}" "${LEGACY_INSTALL_REGISTRY_VALUE}"
        ${EndIf}
        ${If} $0 == ""
            ReadRegStr $0 HKLM "${RIL_BOOTSTRAP_REGISTRY_KEY}" "${RIL_BOOTSTRAP_REGISTRY_VALUE}"
        ${EndIf}
        SetRegView 64
    ${EndIf}
    ${If} $0 != ""
        StrCpy $INSTDIR $0
    ${EndIf}

init_done:
    Return

update_mutex_failed:
    IfSilent update_mutex_failed_silent
    MessageBox MB_OK|MB_ICONSTOP "서버 설치 잠금을 만들지 못했습니다. (Windows 오류: $4)"
update_mutex_failed_silent:
    SetErrorLevel 23
    Abort

update_mutex_busy:
    IfSilent update_mutex_busy_silent
    MessageBox MB_OK|MB_ICONSTOP "다른 서버 설치 또는 자동업데이트가 진행 중입니다."
update_mutex_busy_silent:
    SetErrorLevel 24
    Abort
FunctionEnd

Section "Server"
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=RIL_server_manual_install.ps1 "dist\make_setup\RIL_server_manual_install.ps1"
    SetOutPath "$PLUGINSDIR\server_payload"
    SetOverwrite on
    File /r "${SERVER_BUILD_DIRECTORY}\*.*"
    File "dist\make_setup\${SERVER_START_SCRIPT}"
    File "dist\make_setup\${SERVER_START_PS1}"
    File "dist\make_setup\${SERVER_RESTARTER_SCRIPT}"
    File "dist\make_setup\${SERVER_RESTARTER_PS1}"
    File "dist\make_setup\${SERVER_UPDATE_HELPER}"
    File /oname=ril_config.json "${CONFIG_FILE}"
    File "${ICON_FILE}"
    SetOutPath "$PLUGINSDIR"

    StrCmp $IsUpdate "1" server_update_payload server_manual_transaction

server_update_payload:
    StrCpy $1 "서버 자동업데이트 파일 적용"
    nsExec::ExecToLog '"${POWER_SHELL_EXECUTABLE}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\RIL_server_manual_install.ps1" -Mode UpdatePayload -InstallDir "$INSTDIR" -PayloadDir "$PLUGINSDIR\server_payload" -ConfigPath "$PLUGINSDIR\server_payload\ril_config.json" -TargetVersion "${APP_VERSION}"'
    Pop $0
    StrCmp $0 "0" server_install_succeeded server_transaction_failed

server_manual_transaction:
    StrCpy $1 "서버 설치"
    nsExec::ExecToLog '"${POWER_SHELL_EXECUTABLE}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\RIL_server_manual_install.ps1" -Mode ManualTransactional -InstallDir "$INSTDIR" -PayloadDir "$PLUGINSDIR\server_payload" -ConfigPath "$PLUGINSDIR\server_payload\ril_config.json" -TargetVersion "${APP_VERSION}"'
    Pop $0
    StrCmp $0 "0" server_install_succeeded server_transaction_failed

server_transaction_failed:
    IfSilent server_transaction_failed_silent
    MessageBox MB_OK|MB_ICONSTOP "$1에 실패했습니다. 기존 서버는 가능한 경우 자동 복원되었습니다. (종료 코드: $0)"
server_transaction_failed_silent:
    SetErrorLevel 27
    Abort

server_install_succeeded:

    SetRegView 64
    WriteRegStr HKLM "${RIL_BOOTSTRAP_REGISTRY_KEY}" "${RIL_BOOTSTRAP_REGISTRY_VALUE}" "$INSTDIR"
    SetRegView 32
    WriteRegStr HKLM "${RIL_BOOTSTRAP_REGISTRY_KEY}" "${RIL_BOOTSTRAP_REGISTRY_VALUE}" "$INSTDIR"
    SetRegView 64

    CreateDirectory "$SMPROGRAMS\${SHORTCUT_DIRECTORY}"
    CreateShortCut "$SMPROGRAMS\${SHORTCUT_DIRECTORY}\${SERVER_START_MENU_SHORTCUT}" "$INSTDIR\${SERVER_EXECUTABLE}"
    CreateShortCut "$DESKTOP\${SERVER_DESKTOP_SHORTCUT}" "$INSTDIR\${SERVER_EXECUTABLE}"

    SetErrorLevel 0
SectionEnd
