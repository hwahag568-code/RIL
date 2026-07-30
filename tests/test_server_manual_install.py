import unittest
from pathlib import Path

from scripts import release_validation


REPO_ROOT = Path(__file__).resolve().parents[1]


class ServerManualInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = (
            REPO_ROOT / "RIL_Server_Setup.nsi"
        ).read_text(encoding="utf-8-sig")
        cls.transaction = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_manual_install.ps1"
        ).read_text(encoding="utf-8-sig")

    def test_nsis_only_stages_payload_and_delegates_install(self):
        self.assertIn("Var IsUpdate", self.installer)
        self.assertIn(
            'SetOutPath "$PLUGINSDIR\\server_payload"',
            self.installer,
        )
        self.assertIn(
            'File /r "${SERVER_BUILD_DIRECTORY}\\*.*"',
            self.installer,
        )
        self.assertIn("-Mode ManualTransactional", self.installer)
        self.assertIn("-Mode UpdatePayload", self.installer)
        self.assertNotIn(
            'RMDir /r "$INSTDIR\\${SERVER_RUNTIME_DIRECTORY}"',
            self.installer,
        )
        self.assertNotIn("/create /sc ONLOGON", self.installer)
        self.assertIn("RIL_BOOTSTRAP_REGISTRY_KEY", self.installer)
        self.assertIn("RIL_BOOTSTRAP_REGISTRY_VALUE", self.installer)
        self.assertIn('"InstallLocation"', self.installer)

    def test_installer_runs_server_transaction_without_console_window(self):
        self.assertNotIn(
            'ExecWait \'"${POWER_SHELL_EXECUTABLE}"',
            self.installer,
        )
        for mode in ("UpdatePayload", "ManualTransactional"):
            self.assertRegex(
                self.installer,
                r"nsExec::ExecToLog "
                r"'\"\$\{POWER_SHELL_EXECUTABLE\}\"[^\r\n]*"
                + rf"-Mode {mode}[^\r\n]*'\r?\n"
                + r"\s*Pop \$0\r?\n"
                + r'\s*StrCmp \$0 "0" '
                + r"server_install_succeeded server_transaction_failed",
            )

    def test_manual_mode_has_backup_health_commit_and_rollback(self):
        for marker in (
            "Backup-PreviousInstallation",
            "Export-ExistingTask",
            "Get-RegistrySnapshot",
            "Stop-InstalledServer",
            "Install-ServerPayload",
            "Wait-ServerHealth",
            "Restore-PreviousInstallation",
            'Write-TransactionState -Phase "committed"',
            'Write-TransactionState -Phase "rollback_failed"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.transaction)

    def test_health_check_binds_status_version_path_and_pids(self):
        for marker in (
            '[string]$health.status -ne "ready"',
            "[string]$health.version -ne $ExpectedVersion",
            "$readyAt -lt $NotBefore",
            "[int]$health.parent_pid -ne $ExpectedParentPid",
            "$worker.ExecutablePath",
            "$parent.ExecutablePath",
            "[int]$worker.ParentProcessId",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.transaction)

    def test_only_server_payload_is_copied_or_removed(self):
        for marker in (
            "server_executable",
            "server_start_script",
            "server_start_power_shell_script",
            "server_restarter_script",
            "server_restarter_power_shell_script",
            "server_update_helper_script",
            "server_runtime_directory",
            '"ril_config.json"',
            "icon_file",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.transaction)

        self.assertNotIn("client_runtime_directory", self.transaction)
        self.assertNotIn("legacy_runtime_directory", self.transaction)
        self.assertNotIn("client_executable", self.transaction)
        self.assertNotIn('"ril_config.local.json"', self.transaction)
        self.assertNotIn(
            'Copy-Item -Path (Join-Path $payloadPath "*")',
            self.transaction,
        )
        self.assertNotIn(
            'Remove-Item -LiteralPath $installPath -Recurse',
            self.transaction,
        )

    def test_update_mode_does_not_nest_manual_transaction(self):
        update_mode = self.transaction.index(
            'if ($Mode -eq "UpdatePayload")'
        )
        update_end = self.transaction.index(
            "$taskConfig =",
            update_mode,
        )
        self.assertLess(update_mode, update_end)
        update_block = self.transaction[update_mode:update_end]
        self.assertIn("Install-ServerPayload", update_block)
        self.assertNotIn("Backup-PreviousInstallation", update_block)
        self.assertNotIn("Wait-ServerHealth", update_block)

    def test_update_mode_publishes_old_and_new_bootstrap_descriptors(self):
        update_mode = self.transaction.index(
            'if ($Mode -eq "UpdatePayload")'
        )
        update_end = self.transaction.index(
            "$taskConfig =",
            update_mode,
        )
        update_block = self.transaction[update_mode:update_end]
        for marker in (
            "Publish-AutomaticMigrationState",
            "-PreviousConfig $previousServerConfig",
            "-TargetConfig $newConfig",
            "Remove-ObsoleteServerTaskDefinitions",
            "Remove-OldServerRegistryValues",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, update_block)
        self.assertLess(
            update_block.index("Publish-AutomaticMigrationState"),
            update_block.index("Stop-InstalledServers"),
        )
        self.assertLess(
            update_block.index("Stop-InstalledServers"),
            update_block.index("Install-ServerPayload"),
        )
        self.assertLess(
            update_block.index("New-ServerTasks"),
            update_block.index("Remove-ObsoleteServerTaskDefinitions"),
        )

    def test_manual_rollback_tracks_old_and_new_tasks_and_registry(self):
        for marker in (
            "Test-ObjectMember",
            "Get-RegistrySnapshots",
            "Restore-RegistrySnapshots",
            "new_server_task_name",
            "new_restarter_task_name",
            "Remove-ServerTaskDefinitions",
            "registry_snapshots",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.transaction)
        self.assertIn(
            "$Value -is [System.Collections.IDictionary]",
            self.transaction,
        )
        self.assertIn("$Value.Contains($Name)", self.transaction)

    def test_manual_upgrade_accepts_a_previous_onefile_server(self):
        self.assertNotIn(
            "기존 서버 런타임이 없어 안전한 백업이 불가능합니다.",
            self.transaction,
        )
        self.assertIn(
            "previous_config = $previousServerConfig",
            self.transaction,
        )
        self.assertIn(
            "elseif ($null -ne $State.previous_config)",
            self.transaction,
        )
        self.assertIn(
            "previous_health_verified = [bool]$previousHealthVerified",
            self.transaction,
        )
        self.assertIn(
            "Wait-LegacyServerProcess",
            self.transaction,
        )
        self.assertIn(
            "$previousHealthVerified -and",
            self.transaction,
        )
        self.assertIn(
            "Get-CombinedRuntimeNames -Configs $Configs",
            self.transaction,
        )

    def test_stale_transaction_is_recovered_before_new_install(self):
        recovery = self.transaction.index(
            "Recover-InterruptedTransaction"
        )
        backup = self.transaction.index(
            "Backup-PreviousInstallation",
            recovery,
        )
        self.assertLess(recovery, backup)
        self.assertIn('"rollback_failed"', self.transaction)
        self.assertIn('"installing"', self.transaction)
        self.assertIn('"health_check"', self.transaction)

    def test_recovery_precedes_parsing_the_installed_config(self):
        runtime_recovery = self.transaction.rindex(
            "Recover-InterruptedTransaction"
        )
        installed_config_parse = self.transaction.index(
            "$existingConfig = Read-JsonFile",
            runtime_recovery,
        )
        self.assertLess(runtime_recovery, installed_config_parse)

    def test_manual_transaction_script_is_build_provenance(self):
        self.assertIn(
            "dist/make_setup/RIL_server_manual_install.ps1",
            release_validation.BUILD_INPUTS,
        )

    def test_manual_transaction_directory_comes_from_json(self):
        self.assertIn(
            "server_manual_transaction_relative_directory",
            self.transaction,
        )
        self.assertNotIn(
            'Join-Path $programDataRoot "manual_server_install"',
            self.transaction,
        )


if __name__ == "__main__":
    unittest.main()
