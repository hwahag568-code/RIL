!include "MUI2.nsh"
!include "FileFunc.nsh"

!ifndef APP_VERSION
    !error "APP_VERSION is required"
!endif
!ifndef CLIENT_INSTALLER_FILENAME
    !error "CLIENT_INSTALLER_FILENAME is required"
!endif
!ifndef INSTALL_DIR
    !error "INSTALL_DIR is required"
!endif
!ifndef REGISTRY_KEY
    !error "REGISTRY_KEY is required"
!endif
!ifndef CLIENT_INSTALL_REGISTRY_VALUE
    !error "CLIENT_INSTALL_REGISTRY_VALUE is required"
!endif
!ifndef LEGACY_INSTALL_REGISTRY_VALUE
    !error "LEGACY_INSTALL_REGISTRY_VALUE is required"
!endif
!ifndef CLIENT_EXECUTABLE
    !error "CLIENT_EXECUTABLE is required"
!endif
!ifndef CLIENT_BUILD_DIRECTORY
    !error "CLIENT_BUILD_DIRECTORY is required"
!endif
!ifndef CLIENT_RUNTIME_DIRECTORY
    !error "CLIENT_RUNTIME_DIRECTORY is required"
!endif
!ifndef LEGACY_RUNTIME_DIRECTORY
    !error "LEGACY_RUNTIME_DIRECTORY is required"
!endif
!ifndef CLIENT_UI_FILE
    !error "CLIENT_UI_FILE is required"
!endif
!ifndef CLIENT_RECOVERY_TASK_NAME
    !error "CLIENT_RECOVERY_TASK_NAME is required"
!endif
!ifndef CLIENT_STARTUP_READY_FILENAME
    !error "CLIENT_STARTUP_READY_FILENAME is required"
!endif
!ifndef CLIENT_STARTUP_CHECK_SCRIPT
    !error "CLIENT_STARTUP_CHECK_SCRIPT is required"
!endif
!ifndef CLIENT_STARTUP_HEALTH_TIMEOUT_MILLISECONDS
    !error "CLIENT_STARTUP_HEALTH_TIMEOUT_MILLISECONDS is required"
!endif
!ifndef UPDATE_MUTEX_NAME
    !error "UPDATE_MUTEX_NAME is required"
!endif
!ifndef UPDATE_MUTEX_WAIT_MILLISECONDS
    !error "UPDATE_MUTEX_WAIT_MILLISECONDS is required"
!endif
!ifndef CONFIG_FILE
    !error "CONFIG_FILE is required"
!endif
!ifndef INSTALL_PREPARE_SCRIPT
    !error "INSTALL_PREPARE_SCRIPT is required"
!endif
!ifndef POWER_SHELL_EXECUTABLE
    !error "POWER_SHELL_EXECUTABLE is required"
!endif
!ifndef ICON_FILE
    !error "ICON_FILE is required"
!endif

!define CLIENT_UPDATE_STAGE_DIRECTORY ".ril_client_update_stage"
!define CLIENT_UPDATE_BACKUP_DIRECTORY ".ril_client_update_backup"
!define CLIENT_UPDATE_PENDING_MARKER "transaction.pending"
!define CLIENT_UPDATE_BACKUP_COMPLETE_MARKER "backup.complete"
!define CLIENT_UPDATE_COMMIT_COMPLETE_MARKER "commit.complete"
!define CLIENT_UPDATE_RECOVERY_DIRECTORY ".ril_client_update_recovery"
!define CLIENT_UPDATE_RECOVERY_INSTALLER "recover_update.exe"
!define CLIENT_UPDATE_RECOVERY_COMMAND "recover_update.cmd"
!define CLIENT_UPDATE_MIGRATION_DESCRIPTOR "migration.ini"
; 이 표준 uninstall anchor는 bootstrap 설정 자체가 바뀌어도 기존
; custom 설치 경로를 찾기 위한 고정 product identity다.
!define RIL_BOOTSTRAP_REGISTRY_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\RIL"
!define RIL_BOOTSTRAP_REGISTRY_VALUE "InstallLocation"

Name "인터페이스 원격로그인 클라이언트 업데이트"
OutFile "release\${CLIENT_INSTALLER_FILENAME}"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!define MUI_ICON "${ICON_FILE}"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Korean"

Var RecoveryMode
Var RecoveredOldClient
Var ClientMigrationDescriptor
Var DescriptorLoadStatus
Var RegistryMigrationStatus
Var OldClientExecutable
Var OldClientRuntimeDirectory
Var OldLegacyRuntimeDirectory
Var OldClientUiFile
Var OldClientRecoveryTaskName
Var OldClientStartupReadyFilename
Var OldRegistryKey
Var OldClientInstallRegistryValue
Var OldLegacyInstallRegistryValue
Var TargetClientExecutable
Var TargetClientRuntimeDirectory
Var TargetLegacyRuntimeDirectory
Var TargetClientUiFile
Var TargetClientRecoveryTaskName
Var TargetClientStartupReadyFilename
Var TargetRegistryKey
Var TargetClientInstallRegistryValue
Var TargetLegacyInstallRegistryValue

Function .onInit
    StrCpy $RecoveryMode "0"
    ${GetParameters} $5
    ${GetOptions} $5 "/RECOVERY" $6
    IfErrors recovery_mode_done
    StrCpy $RecoveryMode "1"

recovery_mode_done:
    System::Call 'kernel32::SetLastError(i 0)'
    ; 서버 installer/helper와 같은 배포 mutex를 실제 소유해
    ; 공용 ril_config.json 교체를 직렬화한다.
    System::Call 'kernel32::CreateMutexW(p 0, i 1, w "${UPDATE_MUTEX_NAME}") p .r7 ?e'
    Pop $8
    StrCmp $7 "0" client_mutex_failed
    StrCmp $8 "183" client_mutex_wait
    Return

client_mutex_wait:
    System::Call 'kernel32::WaitForSingleObject(p r7, i ${UPDATE_MUTEX_WAIT_MILLISECONDS}) i .r8'
    StrCmp $8 "0" client_mutex_acquired
    StrCmp $8 "128" client_mutex_acquired client_mutex_busy

client_mutex_acquired:
    Return

client_mutex_failed:
    IfSilent client_mutex_failed_silent
    MessageBox MB_OK|MB_ICONSTOP "클라이언트 업데이트 잠금을 만들지 못했습니다. (Windows 오류: $8)"
client_mutex_failed_silent:
    SetErrorLevel 23
    Abort

client_mutex_busy:
    IfSilent client_mutex_busy_silent
    MessageBox MB_OK|MB_ICONSTOP "다른 RIL 설치, 업데이트 또는 복구가 제한시간 안에 끝나지 않았습니다."
client_mutex_busy_silent:
    SetErrorLevel 24
    Abort
FunctionEnd

Section "Update"
    StrCmp $RecoveryMode "1" client_resolve_recovery_install_dir

    ReadRegStr $0 HKLM "${REGISTRY_KEY}" "${CLIENT_INSTALL_REGISTRY_VALUE}"
    ${If} $0 == ""
        ReadRegStr $0 HKLM "${REGISTRY_KEY}" "${LEGACY_INSTALL_REGISTRY_VALUE}"
    ${EndIf}
    ${If} $0 == ""
        ReadRegStr $0 HKLM "${RIL_BOOTSTRAP_REGISTRY_KEY}" "${RIL_BOOTSTRAP_REGISTRY_VALUE}"
    ${EndIf}
    ${If} $0 == ""
        SetRegView 64
        ReadRegStr $0 HKLM "${REGISTRY_KEY}" "${CLIENT_INSTALL_REGISTRY_VALUE}"
        ${If} $0 == ""
            ReadRegStr $0 HKLM "${REGISTRY_KEY}" "${LEGACY_INSTALL_REGISTRY_VALUE}"
        ${EndIf}
        ${If} $0 == ""
            ReadRegStr $0 HKLM "${RIL_BOOTSTRAP_REGISTRY_KEY}" "${RIL_BOOTSTRAP_REGISTRY_VALUE}"
        ${EndIf}
        SetRegView 32
    ${EndIf}
    ${If} $0 == ""
        StrCpy $0 "${INSTALL_DIR}"
    ${EndIf}
    Goto client_install_dir_resolved

client_resolve_recovery_install_dir:
    ; 복구 프로그램은 설치 폴더 바로 아래의 전용 폴더에서 실행된다.
    ; 레지스트리 값 이름이 바뀌었더라도 이 경로로 기존 설치를 찾는다.
    ${GetParent} "$EXEDIR" $0

client_install_dir_resolved:

    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=ril_config.json "${CONFIG_FILE}"
    File /oname=RIL_install_prepare.ps1 "${INSTALL_PREPARE_SCRIPT}"
    File /oname=RIL_client_startup_check.ps1 "dist\make_setup\${CLIENT_STARTUP_CHECK_SCRIPT}"

    StrCpy $ClientMigrationDescriptor "$PLUGINSDIR\${CLIENT_UPDATE_MIGRATION_DESCRIPTOR}"
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_MIGRATION_DESCRIPTOR}" 0 client_migration_descriptor_selected
    StrCpy $ClientMigrationDescriptor "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_MIGRATION_DESCRIPTOR}"

client_migration_descriptor_selected:
    nsExec::ExecToLog '"${POWER_SHELL_EXECUTABLE}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\RIL_install_prepare.ps1" -Component client -InstallDir "$0" -ConfigPath "$PLUGINSDIR\ril_config.json" -InstalledConfigPath "$0\${CONFIG_FILE}" -DescriptorPath "$ClientMigrationDescriptor"'
    Pop $1
    StrCmp $1 "0" client_load_migration_descriptor client_prepare_failed

client_load_migration_descriptor:
    Call LoadClientMigrationDescriptor
    StrCmp $DescriptorLoadStatus "0" client_prepare_done client_prepare_failed

client_prepare_failed:
    IfSilent client_prepare_failed_silent
    MessageBox MB_OK|MB_ICONSTOP "기존 클라이언트를 종료하지 못했습니다. 실행 중인 RIL_client를 종료한 뒤 다시 시도하세요. (종료 코드: $1)"
client_prepare_failed_silent:
    SetErrorLevel 10
    Abort

client_prepare_done:
    Call RecoverInterruptedClientUpdate
    StrCmp $2 "0" client_recovery_succeeded client_recovery_failed

client_recovery_succeeded:
    StrCmp $RecoveryMode "1" client_recovery_only client_refresh_migration_descriptor

client_refresh_migration_descriptor:
    ; 중단된 거래를 복구했다면 백업의 설명자는 직전 거래용이다.
    ; 복구된 OLD 설정과 이번 payload의 NEW 설정으로 새 설명자를 만든다.
    StrCpy $ClientMigrationDescriptor "$PLUGINSDIR\${CLIENT_UPDATE_MIGRATION_DESCRIPTOR}"
    Delete "$ClientMigrationDescriptor"
    nsExec::ExecToLog '"${POWER_SHELL_EXECUTABLE}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\RIL_install_prepare.ps1" -Component client -InstallDir "$0" -ConfigPath "$PLUGINSDIR\ril_config.json" -InstalledConfigPath "$0\${CONFIG_FILE}" -DescriptorPath "$ClientMigrationDescriptor"'
    Pop $1
    StrCmp $1 "0" client_reload_migration_descriptor client_prepare_failed

client_reload_migration_descriptor:
    Call LoadClientMigrationDescriptor
    StrCmp $DescriptorLoadStatus "0" client_recovery_done client_prepare_failed

client_recovery_only:
    StrCmp $RecoveredOldClient "1" client_recovery_launch_old
    StrCmp "$TargetClientExecutable" "${CLIENT_EXECUTABLE}" client_recovery_launch_current client_recovery_launch_target

client_recovery_launch_target:
    IfFileExists "$0\$TargetClientExecutable" 0 client_recovery_launch_failed
    ClearErrors
    ExecShell "open" "$0\$TargetClientExecutable"
    IfErrors client_recovery_launch_failed
    Goto client_recovery_launch_succeeded

client_recovery_launch_current:
    IfFileExists "$0\${CLIENT_EXECUTABLE}" 0 client_recovery_launch_failed
    ClearErrors
    ExecShell "open" "$0\${CLIENT_EXECUTABLE}"
    IfErrors client_recovery_launch_failed
    Goto client_recovery_launch_succeeded

client_recovery_launch_old:
    IfFileExists "$0\$OldClientExecutable" 0 client_recovery_launch_failed
    ClearErrors
    ExecShell "open" "$0\$OldClientExecutable"
    IfErrors client_recovery_launch_failed

client_recovery_launch_succeeded:
    Call CleanupClientRecovery

    SetErrorLevel 0
    SetAutoClose true
    Goto client_done

client_recovery_launch_failed:
    IfSilent client_recovery_launch_failed_silent
    MessageBox MB_OK|MB_ICONSTOP "복구된 클라이언트를 실행하지 못했습니다. 다음 로그인 때 복구 작업이 다시 시도됩니다."
client_recovery_launch_failed_silent:
    SetErrorLevel 14
    Abort

client_recovery_failed:
    IfSilent client_recovery_failed_silent
    MessageBox MB_OK|MB_ICONSTOP "중단된 이전 클라이언트 업데이트를 복구하지 못했습니다. $0\${CLIENT_UPDATE_BACKUP_DIRECTORY} 폴더를 보존하고 관리자에게 문의하세요."
client_recovery_failed_silent:
    SetErrorLevel 13
    Abort

client_recovery_done:
    SetOutPath "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}"
    SetOverwrite on
    ClearErrors
    File /r "${CLIENT_BUILD_DIRECTORY}\*.*"
    File "${CLIENT_UI_FILE}"
    File "${CONFIG_FILE}"
    IfErrors client_stage_failed

    IfFileExists "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\${CLIENT_EXECUTABLE}" 0 client_stage_failed
    IfFileExists "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\${CLIENT_RUNTIME_DIRECTORY}\*.*" 0 client_stage_failed
    IfFileExists "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\${CLIENT_UI_FILE}" 0 client_stage_failed
    IfFileExists "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\${CONFIG_FILE}" 0 client_stage_failed

    Call PrepareClientRecovery
    StrCmp $3 "0" client_recovery_task_ready client_stage_failed

client_recovery_task_ready:
    SetOutPath "$PLUGINSDIR"
    ClearErrors
    CreateDirectory "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}"
    IfErrors client_stage_failed
    ClearErrors
    CopyFiles /SILENT "$ClientMigrationDescriptor" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_MIGRATION_DESCRIPTOR}"
    IfErrors client_stage_failed
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_MIGRATION_DESCRIPTOR}" 0 client_stage_failed
    FileOpen $2 "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_PENDING_MARKER}" w
    IfErrors client_stage_failed
    FileWrite $2 "pending"
    IfErrors client_pending_marker_write_failed
    FileClose $2
    IfErrors client_stage_failed
    Goto client_begin_backup

client_pending_marker_write_failed:
    FileClose $2
    Goto client_stage_failed

client_begin_backup:
    StrCmp "$OldClientExecutable" "${CLIENT_EXECUTABLE}" client_backup_current_executable client_backup_old_executable

client_backup_old_executable:
    IfFileExists "$0\$OldClientExecutable" 0 client_backup_client_runtime
    ClearErrors
    Rename "$0\$OldClientExecutable" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientExecutable"
    IfErrors client_transaction_failed
    Goto client_backup_client_runtime

client_backup_current_executable:
    IfFileExists "$0\${CLIENT_EXECUTABLE}" 0 client_backup_client_runtime
    ClearErrors
    Rename "$0\${CLIENT_EXECUTABLE}" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_EXECUTABLE}"
    IfErrors client_transaction_failed

client_backup_client_runtime:
    StrCmp "$OldClientRuntimeDirectory" "${CLIENT_RUNTIME_DIRECTORY}" client_backup_current_runtime client_backup_old_runtime

client_backup_old_runtime:
    IfFileExists "$0\$OldClientRuntimeDirectory\*.*" 0 client_remove_empty_old_runtime
    ClearErrors
    Rename "$0\$OldClientRuntimeDirectory" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientRuntimeDirectory"
    IfErrors client_transaction_failed
    Goto client_backup_legacy_runtime

client_remove_empty_old_runtime:
    RMDir "$0\$OldClientRuntimeDirectory"
    Goto client_backup_legacy_runtime

client_backup_current_runtime:
    IfFileExists "$0\${CLIENT_RUNTIME_DIRECTORY}\*.*" 0 client_remove_empty_client_runtime
    ClearErrors
    Rename "$0\${CLIENT_RUNTIME_DIRECTORY}" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_RUNTIME_DIRECTORY}"
    IfErrors client_transaction_failed
    Goto client_backup_legacy_runtime

client_remove_empty_client_runtime:
    RMDir "$0\${CLIENT_RUNTIME_DIRECTORY}"

client_backup_legacy_runtime:
    StrCmp "$OldLegacyRuntimeDirectory" "${LEGACY_RUNTIME_DIRECTORY}" client_backup_current_legacy_runtime client_backup_old_legacy_runtime

client_backup_old_legacy_runtime:
    IfFileExists "$0\$OldLegacyRuntimeDirectory\*.*" 0 client_remove_empty_old_legacy_runtime
    ClearErrors
    Rename "$0\$OldLegacyRuntimeDirectory" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldLegacyRuntimeDirectory"
    IfErrors client_transaction_failed
    Goto client_backup_ui

client_remove_empty_old_legacy_runtime:
    RMDir "$0\$OldLegacyRuntimeDirectory"
    Goto client_backup_ui

client_backup_current_legacy_runtime:
    IfFileExists "$0\${LEGACY_RUNTIME_DIRECTORY}\*.*" 0 client_remove_empty_legacy_runtime
    ClearErrors
    Rename "$0\${LEGACY_RUNTIME_DIRECTORY}" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${LEGACY_RUNTIME_DIRECTORY}"
    IfErrors client_transaction_failed
    Goto client_backup_ui

client_remove_empty_legacy_runtime:
    RMDir "$0\${LEGACY_RUNTIME_DIRECTORY}"

client_backup_ui:
    StrCmp "$OldClientUiFile" "${CLIENT_UI_FILE}" client_backup_current_ui client_backup_old_ui

client_backup_old_ui:
    IfFileExists "$0\$OldClientUiFile" 0 client_backup_config
    ClearErrors
    Rename "$0\$OldClientUiFile" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientUiFile"
    IfErrors client_transaction_failed
    Goto client_backup_config

client_backup_current_ui:
    IfFileExists "$0\${CLIENT_UI_FILE}" 0 client_backup_config
    ClearErrors
    Rename "$0\${CLIENT_UI_FILE}" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UI_FILE}"
    IfErrors client_transaction_failed

client_backup_config:
    IfFileExists "$0\${CONFIG_FILE}" 0 client_mark_backup_complete
    ClearErrors
    Rename "$0\${CONFIG_FILE}" "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CONFIG_FILE}"
    IfErrors client_transaction_failed

client_mark_backup_complete:
    ClearErrors
    FileOpen $2 "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_BACKUP_COMPLETE_MARKER}" w
    IfErrors client_transaction_failed
    FileWrite $2 "complete"
    IfErrors client_backup_complete_marker_write_failed
    FileClose $2
    IfErrors client_transaction_failed
    Goto client_install_staged_runtime

client_backup_complete_marker_write_failed:
    FileClose $2
    Goto client_transaction_failed

client_install_staged_runtime:
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\${CLIENT_EXECUTABLE}" "$0\${CLIENT_EXECUTABLE}"
    IfErrors client_transaction_failed
    Rename "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\${CLIENT_RUNTIME_DIRECTORY}" "$0\${CLIENT_RUNTIME_DIRECTORY}"
    IfErrors client_transaction_failed
    Rename "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\${CLIENT_UI_FILE}" "$0\${CLIENT_UI_FILE}"
    IfErrors client_transaction_failed
    Rename "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\${CONFIG_FILE}" "$0\${CONFIG_FILE}"
    IfErrors client_transaction_failed

    ClearErrors
    Delete "$0\${CLIENT_STARTUP_READY_FILENAME}"
    IfErrors client_startup_failed
    IfFileExists "$0\${CLIENT_STARTUP_READY_FILENAME}" client_startup_failed
    ExecShell "open" "$0\${CLIENT_EXECUTABLE}" '--ril-startup-ready-file "$0\${CLIENT_STARTUP_READY_FILENAME}"'
    IfErrors client_startup_failed

    StrCpy $1 "0"
client_wait_for_startup_ready:
    IfFileExists "$0\${CLIENT_STARTUP_READY_FILENAME}" client_validate_startup_ready
    Sleep 250
    IntOp $1 $1 + 250
    IntCmp $1 ${CLIENT_STARTUP_HEALTH_TIMEOUT_MILLISECONDS} client_startup_failed client_wait_for_startup_ready client_startup_failed

client_validate_startup_ready:
    nsExec::ExecToLog '"${POWER_SHELL_EXECUTABLE}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\RIL_client_startup_check.ps1" -ReadyPath "$0\${CLIENT_STARTUP_READY_FILENAME}" -ExpectedVersion "${APP_VERSION}" -ExpectedExecutable "$0\${CLIENT_EXECUTABLE}"'
    Pop $4
    StrCmp $4 "0" client_startup_ready client_startup_failed

client_startup_ready:
    ClearErrors
    Delete "$0\${CLIENT_STARTUP_READY_FILENAME}"
    IfErrors client_startup_failed
    IfFileExists "$0\${CLIENT_STARTUP_READY_FILENAME}" client_startup_failed

    Call CommitClientRegistryMigration
    StrCmp $RegistryMigrationStatus "0" client_registry_committed client_startup_failed

client_registry_committed:
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\RIL" "DisplayVersion" "${APP_VERSION}"

    ClearErrors
    FileOpen $2 "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_COMMIT_COMPLETE_MARKER}" w
    IfErrors client_startup_failed
    FileWrite $2 "complete"
    IfErrors client_commit_marker_write_failed
    FileClose $2
    IfErrors client_startup_failed
    Delete "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_PENDING_MARKER}"
    SetOutPath "$PLUGINSDIR"
    RMDir /r "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}"
    RMDir /r "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}"
    Call CleanupClientRecovery

    SetErrorLevel 0
    SetAutoClose true
    Goto client_done

client_commit_marker_write_failed:
    FileClose $2
    Goto client_startup_failed

client_startup_failed:
    nsExec::ExecToLog '"${POWER_SHELL_EXECUTABLE}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\RIL_install_prepare.ps1" -Component client -InstallDir "$0" -ConfigPath "$PLUGINSDIR\ril_config.json" -InstalledConfigPath "$0\${CONFIG_FILE}" -DescriptorPath "$ClientMigrationDescriptor"'
    Pop $4
    Sleep 500
    Delete "$0\${CLIENT_STARTUP_READY_FILENAME}"
    Goto client_transaction_failed

client_stage_failed:
    SetOutPath "$PLUGINSDIR"
    RMDir /r "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}"
    RMDir /r "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}"
    Call CleanupClientRecovery
    StrCmp "$OldClientExecutable" "${CLIENT_EXECUTABLE}" client_stage_launch_current client_stage_launch_old

client_stage_launch_old:
    IfFileExists "$0\$OldClientExecutable" 0 client_stage_failed_message
    ClearErrors
    ExecShell "open" "$0\$OldClientExecutable"
    Goto client_stage_failed_message

client_stage_launch_current:
    IfFileExists "$0\${CLIENT_EXECUTABLE}" 0 client_stage_failed_message
    ClearErrors
    ExecShell "open" "$0\${CLIENT_EXECUTABLE}"

client_stage_failed_message:
    IfSilent client_stage_failed_silent
    MessageBox MB_OK|MB_ICONSTOP "클라이언트 업데이트 파일을 준비하지 못했습니다. 기존 설치는 변경되지 않았습니다."
client_stage_failed_silent:
    SetErrorLevel 12
    Abort

client_transaction_failed:
    Call RecoverInterruptedClientUpdate
    StrCmp $2 "0" client_transaction_rolled_back client_recovery_failed

client_transaction_rolled_back:
    Call CleanupClientRecovery
    StrCmp "$OldClientExecutable" "${CLIENT_EXECUTABLE}" client_rollback_launch_current client_rollback_launch_old

client_rollback_launch_old:
    IfFileExists "$0\$OldClientExecutable" 0 client_transaction_rolled_back_message
    ClearErrors
    ExecShell "open" "$0\$OldClientExecutable"
    Goto client_transaction_rolled_back_message

client_rollback_launch_current:
    IfFileExists "$0\${CLIENT_EXECUTABLE}" 0 client_transaction_rolled_back_message
    ClearErrors
    ExecShell "open" "$0\${CLIENT_EXECUTABLE}"

client_transaction_rolled_back_message:
    IfSilent client_transaction_rolled_back_silent
    MessageBox MB_OK|MB_ICONSTOP "클라이언트 업데이트 파일을 설치하지 못해 기존 버전으로 복구했습니다."
client_transaction_rolled_back_silent:
    SetErrorLevel 12
    Abort

client_done:
SectionEnd

Function LoadClientMigrationDescriptor
    StrCpy $DescriptorLoadStatus "1"
    ClearErrors
    ReadINIStr $OldClientExecutable "$ClientMigrationDescriptor" "old" "client_executable"
    ReadINIStr $OldClientRuntimeDirectory "$ClientMigrationDescriptor" "old" "client_runtime_directory"
    ReadINIStr $OldLegacyRuntimeDirectory "$ClientMigrationDescriptor" "old" "legacy_runtime_directory"
    ReadINIStr $OldClientUiFile "$ClientMigrationDescriptor" "old" "client_ui_file"
    ReadINIStr $OldClientRecoveryTaskName "$ClientMigrationDescriptor" "old" "client_update_recovery_task_name"
    ReadINIStr $OldClientStartupReadyFilename "$ClientMigrationDescriptor" "old" "client_startup_ready_filename"
    ReadINIStr $OldRegistryKey "$ClientMigrationDescriptor" "old" "registry_key"
    ReadINIStr $OldClientInstallRegistryValue "$ClientMigrationDescriptor" "old" "client_install_registry_value"
    ReadINIStr $OldLegacyInstallRegistryValue "$ClientMigrationDescriptor" "old" "legacy_install_registry_value"
    ReadINIStr $TargetClientExecutable "$ClientMigrationDescriptor" "new" "client_executable"
    ReadINIStr $TargetClientRuntimeDirectory "$ClientMigrationDescriptor" "new" "client_runtime_directory"
    ReadINIStr $TargetLegacyRuntimeDirectory "$ClientMigrationDescriptor" "new" "legacy_runtime_directory"
    ReadINIStr $TargetClientUiFile "$ClientMigrationDescriptor" "new" "client_ui_file"
    ReadINIStr $TargetClientRecoveryTaskName "$ClientMigrationDescriptor" "new" "client_update_recovery_task_name"
    ReadINIStr $TargetClientStartupReadyFilename "$ClientMigrationDescriptor" "new" "client_startup_ready_filename"
    ReadINIStr $TargetRegistryKey "$ClientMigrationDescriptor" "new" "registry_key"
    ReadINIStr $TargetClientInstallRegistryValue "$ClientMigrationDescriptor" "new" "client_install_registry_value"
    ReadINIStr $TargetLegacyInstallRegistryValue "$ClientMigrationDescriptor" "new" "legacy_install_registry_value"
    IfErrors migration_descriptor_load_failed
    StrCmp $OldClientExecutable "" migration_descriptor_load_failed
    StrCmp $OldClientRuntimeDirectory "" migration_descriptor_load_failed
    StrCmp $OldLegacyRuntimeDirectory "" migration_descriptor_load_failed
    StrCmp $OldClientUiFile "" migration_descriptor_load_failed
    StrCmp $OldClientRecoveryTaskName "" migration_descriptor_load_failed
    StrCmp $OldClientStartupReadyFilename "" migration_descriptor_load_failed
    StrCmp $OldRegistryKey "" migration_descriptor_load_failed
    StrCmp $OldClientInstallRegistryValue "" migration_descriptor_load_failed
    StrCmp $OldLegacyInstallRegistryValue "" migration_descriptor_load_failed
    StrCmp $TargetClientExecutable "" migration_descriptor_load_failed
    StrCmp $TargetClientRuntimeDirectory "" migration_descriptor_load_failed
    StrCmp $TargetLegacyRuntimeDirectory "" migration_descriptor_load_failed
    StrCmp $TargetClientUiFile "" migration_descriptor_load_failed
    StrCmp $TargetClientRecoveryTaskName "" migration_descriptor_load_failed
    StrCmp $TargetClientStartupReadyFilename "" migration_descriptor_load_failed
    StrCmp $TargetRegistryKey "" migration_descriptor_load_failed
    StrCmp $TargetClientInstallRegistryValue "" migration_descriptor_load_failed
    StrCmp $TargetLegacyInstallRegistryValue "" migration_descriptor_load_failed
    StrCpy $DescriptorLoadStatus "0"

migration_descriptor_load_failed:
FunctionEnd

Function CommitClientRegistryMigration
    StrCpy $RegistryMigrationStatus "1"
    ClearErrors
    WriteRegStr HKLM "${REGISTRY_KEY}" "${CLIENT_INSTALL_REGISTRY_VALUE}" "$0"
    IfErrors commit_registry_failed
    WriteRegStr HKLM "${REGISTRY_KEY}" "${LEGACY_INSTALL_REGISTRY_VALUE}" "$0"
    IfErrors commit_registry_failed

    StrCmp "$OldRegistryKey" "${REGISTRY_KEY}" commit_old_client_same_key commit_delete_old_client
commit_old_client_same_key:
    StrCmp "$OldClientInstallRegistryValue" "${CLIENT_INSTALL_REGISTRY_VALUE}" commit_check_old_legacy
    StrCmp "$OldClientInstallRegistryValue" "${LEGACY_INSTALL_REGISTRY_VALUE}" commit_check_old_legacy
commit_delete_old_client:
    DeleteRegValue HKLM "$OldRegistryKey" "$OldClientInstallRegistryValue"

commit_check_old_legacy:
    StrCmp "$OldRegistryKey" "${REGISTRY_KEY}" commit_old_legacy_same_key commit_delete_old_legacy
commit_old_legacy_same_key:
    StrCmp "$OldLegacyInstallRegistryValue" "${CLIENT_INSTALL_REGISTRY_VALUE}" commit_registry_done
    StrCmp "$OldLegacyInstallRegistryValue" "${LEGACY_INSTALL_REGISTRY_VALUE}" commit_registry_done
commit_delete_old_legacy:
    DeleteRegValue HKLM "$OldRegistryKey" "$OldLegacyInstallRegistryValue"

commit_registry_done:
    SetRegView 64
    WriteRegStr HKLM "${RIL_BOOTSTRAP_REGISTRY_KEY}" "${RIL_BOOTSTRAP_REGISTRY_VALUE}" "$0"
    IfErrors commit_registry_failed
    SetRegView 32
    WriteRegStr HKLM "${RIL_BOOTSTRAP_REGISTRY_KEY}" "${RIL_BOOTSTRAP_REGISTRY_VALUE}" "$0"
    IfErrors commit_registry_failed
    StrCpy $RegistryMigrationStatus "0"
commit_registry_failed:
    SetRegView 32
FunctionEnd

Function RestoreOldClientRegistry
    StrCpy $RegistryMigrationStatus "1"
    ClearErrors
    WriteRegStr HKLM "$OldRegistryKey" "$OldClientInstallRegistryValue" "$0"
    IfErrors restore_registry_failed
    WriteRegStr HKLM "$OldRegistryKey" "$OldLegacyInstallRegistryValue" "$0"
    IfErrors restore_registry_failed

    StrCmp "$OldRegistryKey" "$TargetRegistryKey" restore_target_client_same_key restore_delete_target_client
restore_target_client_same_key:
    StrCmp "$TargetClientInstallRegistryValue" "$OldClientInstallRegistryValue" restore_check_target_legacy
    StrCmp "$TargetClientInstallRegistryValue" "$OldLegacyInstallRegistryValue" restore_check_target_legacy
restore_delete_target_client:
    DeleteRegValue HKLM "$TargetRegistryKey" "$TargetClientInstallRegistryValue"

restore_check_target_legacy:
    StrCmp "$OldRegistryKey" "$TargetRegistryKey" restore_target_legacy_same_key restore_delete_target_legacy
restore_target_legacy_same_key:
    StrCmp "$TargetLegacyInstallRegistryValue" "$OldClientInstallRegistryValue" restore_check_current_client
    StrCmp "$TargetLegacyInstallRegistryValue" "$OldLegacyInstallRegistryValue" restore_check_current_client
restore_delete_target_legacy:
    DeleteRegValue HKLM "$TargetRegistryKey" "$TargetLegacyInstallRegistryValue"

restore_check_current_client:
    StrCmp "$OldRegistryKey" "${REGISTRY_KEY}" restore_new_client_same_key restore_delete_new_client
restore_new_client_same_key:
    StrCmp "${CLIENT_INSTALL_REGISTRY_VALUE}" "$OldClientInstallRegistryValue" restore_check_new_legacy
    StrCmp "${CLIENT_INSTALL_REGISTRY_VALUE}" "$OldLegacyInstallRegistryValue" restore_check_new_legacy
restore_delete_new_client:
    DeleteRegValue HKLM "${REGISTRY_KEY}" "${CLIENT_INSTALL_REGISTRY_VALUE}"

restore_check_new_legacy:
    StrCmp "$OldRegistryKey" "${REGISTRY_KEY}" restore_new_legacy_same_key restore_delete_new_legacy
restore_new_legacy_same_key:
    StrCmp "${LEGACY_INSTALL_REGISTRY_VALUE}" "$OldClientInstallRegistryValue" restore_registry_done
    StrCmp "${LEGACY_INSTALL_REGISTRY_VALUE}" "$OldLegacyInstallRegistryValue" restore_registry_done
restore_delete_new_legacy:
    DeleteRegValue HKLM "${REGISTRY_KEY}" "${LEGACY_INSTALL_REGISTRY_VALUE}"

restore_registry_done:
    StrCpy $RegistryMigrationStatus "0"
restore_registry_failed:
FunctionEnd

Function PrepareClientRecovery
    StrCpy $3 "1"
    ClearErrors
    CreateDirectory "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}"
    IfErrors recovery_prepare_failed
    StrCmp "$EXEPATH" "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_INSTALLER}" recovery_installer_ready
    IfFileExists "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_INSTALLER}" 0 recovery_copy_installer
    ClearErrors
    Delete "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_INSTALLER}"
    IfErrors recovery_prepare_failed

recovery_copy_installer:
    ClearErrors
    CopyFiles /SILENT "$EXEPATH" "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_INSTALLER}"
    IfErrors recovery_prepare_failed

recovery_installer_ready:
    IfFileExists "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_INSTALLER}" 0 recovery_prepare_failed
    ClearErrors
    FileOpen $9 "$EXEPATH" r
    IfErrors recovery_prepare_failed
    FileSeek $9 0 END $5
    IfErrors recovery_source_size_failed
    FileClose $9
    IfErrors recovery_prepare_failed
    FileOpen $9 "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_INSTALLER}" r
    IfErrors recovery_prepare_failed
    FileSeek $9 0 END $6
    IfErrors recovery_copy_size_failed
    FileClose $9
    IfErrors recovery_prepare_failed
    IntCmp $5 $6 recovery_installer_size_ok recovery_prepare_failed recovery_prepare_failed

recovery_source_size_failed:
    FileClose $9
    Goto recovery_prepare_failed

recovery_copy_size_failed:
    FileClose $9
    Goto recovery_prepare_failed

recovery_installer_size_ok:
    ClearErrors
    FileOpen $4 "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_COMMAND}" w
    IfErrors recovery_prepare_failed
    FileWrite $4 "@echo off$\r$\n"
    IfErrors recovery_command_write_failed
    FileWrite $4 '"%~dp0${CLIENT_UPDATE_RECOVERY_INSTALLER}" /S /RECOVERY$\r$\n'
    IfErrors recovery_command_write_failed
    FileClose $4
    IfErrors recovery_prepare_failed

    ClearErrors
    nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Create /SC ONLOGON /TN "${CLIENT_RECOVERY_TASK_NAME}" /RL HIGHEST /TR "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_COMMAND}" /F'
    Pop $4
    IfErrors recovery_prepare_failed
    StrCmp $4 "0" recovery_cleanup_old_task recovery_prepare_failed

recovery_cleanup_old_task:
    StrCmp "$OldClientRecoveryTaskName" "$TargetClientRecoveryTaskName" recovery_cleanup_target_task
    StrCmp "$OldClientRecoveryTaskName" "${CLIENT_RECOVERY_TASK_NAME}" recovery_cleanup_target_task
    ClearErrors
    nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Delete /TN "$OldClientRecoveryTaskName" /F'
    Pop $4

recovery_cleanup_target_task:
    StrCmp "$TargetClientRecoveryTaskName" "${CLIENT_RECOVERY_TASK_NAME}" recovery_prepare_complete
    ClearErrors
    nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Delete /TN "$TargetClientRecoveryTaskName" /F'
    Pop $4

recovery_command_write_failed:
    FileClose $4
    Goto recovery_prepare_failed

recovery_prepare_complete:
    StrCpy $3 "0"

recovery_prepare_failed:
FunctionEnd

Function CleanupClientRecovery
    StrCmp "$OldClientRecoveryTaskName" "$TargetClientRecoveryTaskName" cleanup_target_recovery_task
    StrCmp "$OldClientRecoveryTaskName" "${CLIENT_RECOVERY_TASK_NAME}" cleanup_target_recovery_task
    ClearErrors
    nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Delete /TN "$OldClientRecoveryTaskName" /F'
    Pop $4

cleanup_target_recovery_task:
    StrCmp "$TargetClientRecoveryTaskName" "${CLIENT_RECOVERY_TASK_NAME}" cleanup_current_recovery_task
    ClearErrors
    nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Delete /TN "$TargetClientRecoveryTaskName" /F'
    Pop $4

cleanup_current_recovery_task:
    ClearErrors
    nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Delete /TN "${CLIENT_RECOVERY_TASK_NAME}" /F'
    Pop $4
    SetOutPath "$PLUGINSDIR"
    Delete /REBOOTOK "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_COMMAND}"
    Delete /REBOOTOK "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}\${CLIENT_UPDATE_RECOVERY_INSTALLER}"
    RMDir /r /REBOOTOK "$0\${CLIENT_UPDATE_RECOVERY_DIRECTORY}"
FunctionEnd

Function RecoverInterruptedClientUpdate
    StrCpy $2 "0"
    StrCpy $RecoveredOldClient "0"
    SetOutPath "$PLUGINSDIR"
    Delete "$0\${CLIENT_STARTUP_READY_FILENAME}"
    StrCmp "$TargetClientStartupReadyFilename" "${CLIENT_STARTUP_READY_FILENAME}" recovery_delete_old_ready
    Delete "$0\$TargetClientStartupReadyFilename"

recovery_delete_old_ready:
    StrCmp "$OldClientStartupReadyFilename" "${CLIENT_STARTUP_READY_FILENAME}" recovery_check_pending
    StrCmp "$OldClientStartupReadyFilename" "$TargetClientStartupReadyFilename" recovery_check_pending
    Delete "$0\$OldClientStartupReadyFilename"

recovery_check_pending:
    ; commit marker가 원자적인 커밋 경계다. pending 삭제 직전 정전으로
    ; 두 marker가 함께 남아도 이미 검증된 NEW를 유지한다.
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_COMMIT_COMPLETE_MARKER}" recovery_discard_stale_backup recovery_check_transaction_pending

recovery_check_transaction_pending:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_PENDING_MARKER}" recovery_pending_found recovery_pretransaction_interrupted

recovery_pretransaction_interrupted:
    ; 복구 작업 생성 뒤 pending 기록 전에 정전된 경우 OLD는 손대지 않았다.
    StrCpy $RecoveredOldClient "1"
    Goto recovery_discard_stale_backup

recovery_pending_found:
    StrCpy $RecoveredOldClient "1"
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_BACKUP_COMPLETE_MARKER}" 0 recovery_restore_executable

    StrCmp "$TargetClientRuntimeDirectory" "${CLIENT_RUNTIME_DIRECTORY}" recovery_remove_current_runtime recovery_remove_target_runtime

recovery_remove_target_runtime:
    RMDir /r "$0\$TargetClientRuntimeDirectory"
    Goto recovery_remove_target_executable

recovery_remove_current_runtime:
    RMDir /r "$0\${CLIENT_RUNTIME_DIRECTORY}"

recovery_remove_target_executable:
    StrCmp "$TargetClientExecutable" "${CLIENT_EXECUTABLE}" recovery_remove_current_executable recovery_remove_descriptor_executable

recovery_remove_descriptor_executable:
    IfFileExists "$0\$TargetClientExecutable" 0 recovery_remove_target_ui
    ClearErrors
    Delete "$0\$TargetClientExecutable"
    IfErrors recovery_failed
    Goto recovery_remove_target_ui

recovery_remove_current_executable:
    IfFileExists "$0\${CLIENT_EXECUTABLE}" 0 recovery_remove_ui
    ClearErrors
    Delete "$0\${CLIENT_EXECUTABLE}"
    IfErrors recovery_failed

recovery_remove_ui:
    Goto recovery_remove_target_ui

recovery_remove_target_ui:
    StrCmp "$TargetClientUiFile" "${CLIENT_UI_FILE}" recovery_remove_current_ui recovery_remove_descriptor_ui

recovery_remove_descriptor_ui:
    IfFileExists "$0\$TargetClientUiFile" 0 recovery_remove_config
    ClearErrors
    Delete "$0\$TargetClientUiFile"
    IfErrors recovery_failed
    Goto recovery_remove_config

recovery_remove_current_ui:
    IfFileExists "$0\${CLIENT_UI_FILE}" 0 recovery_remove_config
    ClearErrors
    Delete "$0\${CLIENT_UI_FILE}"
    IfErrors recovery_failed

recovery_remove_config:
    IfFileExists "$0\${CONFIG_FILE}" 0 recovery_restore_executable
    ClearErrors
    Delete "$0\${CONFIG_FILE}"
    IfErrors recovery_failed

recovery_restore_executable:
    StrCmp "$OldClientExecutable" "${CLIENT_EXECUTABLE}" recovery_restore_current_executable recovery_restore_old_executable

recovery_restore_old_executable:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientExecutable" 0 recovery_restore_client_runtime
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientExecutable" "$0\$OldClientExecutable"
    IfErrors recovery_failed
    Goto recovery_restore_client_runtime

recovery_restore_current_executable:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_EXECUTABLE}" 0 recovery_restore_client_runtime
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_EXECUTABLE}" "$0\${CLIENT_EXECUTABLE}"
    IfErrors recovery_failed

recovery_restore_client_runtime:
    StrCmp "$OldClientRuntimeDirectory" "${CLIENT_RUNTIME_DIRECTORY}" recovery_restore_current_runtime recovery_restore_old_runtime

recovery_restore_old_runtime:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientRuntimeDirectory\*.*" 0 recovery_restore_legacy_runtime
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientRuntimeDirectory" "$0\$OldClientRuntimeDirectory"
    IfErrors recovery_failed
    Goto recovery_restore_legacy_runtime

recovery_restore_current_runtime:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_RUNTIME_DIRECTORY}\*.*" 0 recovery_restore_legacy_runtime
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_RUNTIME_DIRECTORY}" "$0\${CLIENT_RUNTIME_DIRECTORY}"
    IfErrors recovery_failed

recovery_restore_legacy_runtime:
    StrCmp "$OldLegacyRuntimeDirectory" "${LEGACY_RUNTIME_DIRECTORY}" recovery_restore_current_legacy_runtime recovery_restore_old_legacy_runtime

recovery_restore_old_legacy_runtime:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldLegacyRuntimeDirectory\*.*" 0 recovery_restore_ui
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldLegacyRuntimeDirectory" "$0\$OldLegacyRuntimeDirectory"
    IfErrors recovery_failed
    Goto recovery_restore_ui

recovery_restore_current_legacy_runtime:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${LEGACY_RUNTIME_DIRECTORY}\*.*" 0 recovery_restore_ui
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${LEGACY_RUNTIME_DIRECTORY}" "$0\${LEGACY_RUNTIME_DIRECTORY}"
    IfErrors recovery_failed

recovery_restore_ui:
    StrCmp "$OldClientUiFile" "${CLIENT_UI_FILE}" recovery_restore_current_ui recovery_restore_old_ui

recovery_restore_old_ui:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientUiFile" 0 recovery_restore_config
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\$OldClientUiFile" "$0\$OldClientUiFile"
    IfErrors recovery_failed
    Goto recovery_restore_config

recovery_restore_current_ui:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UI_FILE}" 0 recovery_restore_config
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UI_FILE}" "$0\${CLIENT_UI_FILE}"
    IfErrors recovery_failed

recovery_restore_config:
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CONFIG_FILE}" 0 recovery_commit_restore
    ClearErrors
    Rename "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CONFIG_FILE}" "$0\${CONFIG_FILE}"
    IfErrors recovery_failed

recovery_commit_restore:
    Call RestoreOldClientRegistry
    StrCmp $RegistryMigrationStatus "0" recovery_registry_restored recovery_failed

recovery_registry_restored:
    Delete "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\${CLIENT_UPDATE_PENDING_MARKER}"

recovery_discard_stale_backup:
    SetOutPath "$PLUGINSDIR"
    RMDir /r "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}"
    RMDir /r "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}"
    IfFileExists "$0\${CLIENT_UPDATE_BACKUP_DIRECTORY}\*.*" recovery_failed
    IfFileExists "$0\${CLIENT_UPDATE_STAGE_DIRECTORY}\*.*" recovery_failed
    Return

recovery_failed:
    StrCpy $2 "1"
FunctionEnd
