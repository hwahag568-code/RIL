import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import RIL_client


class ClientOnedirUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]
        cls.spec = (repo_root / "RIL_client.spec").read_text(
            encoding="utf-8",
        )
        cls.client_source = (
            repo_root / "RIL_client.py"
        ).read_text(encoding="utf-8")
        cls.installer = (
            repo_root / "RIL_Client_Update.nsi"
        ).read_text(encoding="utf-8")
        cls.startup_check = (
            repo_root
            / "dist"
            / "make_setup"
            / "RIL_client_startup_check.ps1"
        ).read_text(encoding="utf-8")
        cls.install_prepare = (
            repo_root
            / "dist"
            / "make_setup"
            / "RIL_install_prepare.ps1"
        ).read_text(encoding="utf-8-sig")

    def test_client_build_is_one_folder(self):
        self.assertIn("exclude_binaries=True", self.spec)
        self.assertIn("COLLECT(", self.spec)

    def test_existing_onefile_install_can_receive_runtime_directory(self):
        optional_old_runtime = self.installer.index(
            'IfFileExists "$0\\${LEGACY_RUNTIME_DIRECTORY}\\*.*" '
            "0 client_remove_empty_legacy_runtime"
        )
        install_new_runtime = self.installer.index(
            'Rename "$0\\${CLIENT_UPDATE_STAGE_DIRECTORY}\\'
            '${CLIENT_RUNTIME_DIRECTORY}" '
            '"$0\\${CLIENT_RUNTIME_DIRECTORY}"'
        )
        self.assertLess(optional_old_runtime, install_new_runtime)

    def test_bootstrap_rename_uses_persisted_old_descriptor(self):
        for marker in (
            "Var OldClientExecutable",
            "Var OldClientRuntimeDirectory",
            "Var OldLegacyRuntimeDirectory",
            "Var OldClientUiFile",
            "Var OldClientRecoveryTaskName",
            "Var TargetClientExecutable",
            "Var TargetClientRuntimeDirectory",
            "Var TargetClientRecoveryTaskName",
            "Var ClientMigrationDescriptor",
            "Function LoadClientMigrationDescriptor",
            "${CLIENT_UPDATE_MIGRATION_DESCRIPTOR}",
            "-InstalledConfigPath",
            "-DescriptorPath",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.installer)

        pending = self.installer.index(
            "${CLIENT_UPDATE_PENDING_MARKER}"
        )
        persist_descriptor = self.installer.index(
            'CopyFiles /SILENT "$ClientMigrationDescriptor"'
        )
        self.assertLess(persist_descriptor, pending)
        self.assertIn(
            'Rename "$0\\$OldClientExecutable"',
            self.installer,
        )
        self.assertIn(
            'Rename "$0\\$OldClientRuntimeDirectory"',
            self.installer,
        )
        self.assertIn(
            'Rename "$0\\${CLIENT_UPDATE_STAGE_DIRECTORY}\\'
            '${CLIENT_EXECUTABLE}" "$0\\${CLIENT_EXECUTABLE}"',
            self.installer,
        )
        self.assertIn(
            'Delete "$0\\${CLIENT_EXECUTABLE}"',
            self.installer,
        )
        self.assertIn(
            '"$0\\$OldClientExecutable"',
            self.installer,
        )
        self.assertIn(
            'ReadINIStr $TargetClientExecutable '
            '"$ClientMigrationDescriptor" "new" '
            '"client_executable"',
            self.installer,
        )
        self.assertIn(
            'Delete "$0\\$TargetClientExecutable"',
            self.installer,
        )
        self.assertIn(
            '/Delete /TN "$TargetClientRecoveryTaskName" /F',
            self.installer,
        )
        self.assertIn("RIL_BOOTSTRAP_REGISTRY_KEY", self.installer)
        self.assertIn("RIL_BOOTSTRAP_REGISTRY_VALUE", self.installer)
        self.assertIn('"InstallLocation"', self.installer)
        self.assertIn("SetRegView 64", self.installer)
        self.assertIn("SetRegView 32", self.installer)

    def test_power_loss_boundaries_distinguish_old_and_new_payload(self):
        commit_marker = self.installer.index(
            "${CLIENT_UPDATE_COMMIT_COMPLETE_MARKER}"
        )
        delete_pending = self.installer.index(
            'Delete "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_UPDATE_PENDING_MARKER}"',
            commit_marker,
        )
        self.assertLess(commit_marker, delete_pending)
        recovery = self.installer.index(
            "Function RecoverInterruptedClientUpdate"
        )
        recovery_body = self.installer[recovery:]
        self.assertIn(
            "recovery_pretransaction_interrupted:",
            recovery_body,
        )
        self.assertIn(
            'StrCpy $RecoveredOldClient "1"',
            recovery_body,
        )
        self.assertIn(
            "${CLIENT_UPDATE_COMMIT_COMPLETE_MARKER}",
            recovery_body,
        )
        committed_check = recovery_body.index(
            'IfFileExists "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_UPDATE_COMMIT_COMPLETE_MARKER}"'
        )
        pending_check = recovery_body.index(
            'IfFileExists "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_UPDATE_PENDING_MARKER}"',
            committed_check,
        )
        self.assertLess(committed_check, pending_check)
        self.assertIn(
            '"$0\\$TargetClientExecutable"',
            recovery_body,
        )
        self.assertIn(
            "Call RestoreOldClientRegistry",
            recovery_body,
        )

    def test_prepare_script_builds_old_and_new_process_descriptor(self):
        for marker in (
            "[string]$InstalledConfigPath",
            "[string]$DescriptorPath",
            "Write-MigrationDescriptor",
            "Read-MigrationDescriptor",
            "$oldConfig",
            "$newConfig",
            "Get-TargetExecutableNames",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.install_prepare)

    def test_prepare_script_persists_installed_and_payload_names(self):
        repo_root = Path(__file__).resolve().parents[1]
        base_config = json.loads(
            (repo_root / "ril_config.json").read_text(
                encoding="utf-8",
            )
        )
        old_config = json.loads(json.dumps(base_config))
        new_config = json.loads(json.dumps(base_config))
        old_installation = old_config["installation"]
        new_installation = new_config["installation"]
        old_installation.update(
            {
                "client_executable": "RIL_client_old.exe",
                "client_runtime_directory": "_client_old",
                "legacy_runtime_directory": "_legacy_old",
                "client_ui_file": "RIL_old.ui",
                "client_update_recovery_task_name": (
                    "RIL_client_old_recovery"
                ),
                "client_install_registry_value": "OldClientDir",
            }
        )
        new_installation.update(
            {
                "client_executable": "RIL_client_new.exe",
                "client_runtime_directory": "_client_new",
                "legacy_runtime_directory": "_legacy_new",
                "client_ui_file": "RIL_new.ui",
                "client_update_recovery_task_name": (
                    "RIL_client_new_recovery"
                ),
                "client_install_registry_value": "NewClientDir",
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            old_path = temporary / "installed.json"
            new_path = temporary / "payload.json"
            descriptor_path = temporary / "migration.ini"
            old_path.write_text(
                json.dumps(old_config),
                encoding="utf-8",
            )
            new_path.write_text(
                json.dumps(new_config),
                encoding="utf-8",
            )
            command = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(
                    repo_root
                    / "dist"
                    / "make_setup"
                    / "RIL_install_prepare.ps1"
                ),
                "-Component",
                "client",
                "-InstallDir",
                str(temporary),
                "-ConfigPath",
                str(new_path),
                "-InstalledConfigPath",
                str(old_path),
                "-DescriptorPath",
                str(descriptor_path),
            ]
            generated = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                generated.returncode,
                0,
                generated.stderr,
            )
            descriptor = descriptor_path.read_text(
                encoding="utf-16",
            )
            self.assertIn(
                "client_executable=RIL_client_old.exe",
                descriptor,
            )
            self.assertIn(
                "client_executable=RIL_client_new.exe",
                descriptor,
            )
            self.assertIn(
                "client_install_registry_value=OldClientDir",
                descriptor,
            )
            self.assertIn(
                "client_install_registry_value=NewClientDir",
                descriptor,
            )

            old_installation["client_executable"] = (
                "must_not_replace_descriptor.exe"
            )
            old_path.write_text(
                json.dumps(old_config),
                encoding="utf-8",
            )
            reused = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                reused.returncode,
                0,
                reused.stderr,
            )
            self.assertNotIn(
                "must_not_replace_descriptor.exe",
                descriptor_path.read_text(encoding="utf-16"),
            )

    def test_new_client_is_started_before_update_is_committed(self):
        install_label = self.installer.index(
            "client_install_staged_runtime:"
        )
        launch = self.installer.index(
            'ExecShell "open" "$0\\${CLIENT_EXECUTABLE}"',
            install_label,
        )
        launch_failure = self.installer.index(
            "IfErrors client_startup_failed",
            launch,
        )
        startup_ready = self.installer.index(
            "client_startup_ready:",
            launch_failure,
        )
        commit = self.installer.index(
            'Delete "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_UPDATE_PENDING_MARKER}"',
            startup_ready,
        )
        self.assertLess(launch, launch_failure)
        self.assertLess(launch_failure, startup_ready)
        self.assertLess(startup_ready, commit)

    def test_installer_waits_for_new_client_startup_health(self):
        install_label = self.installer.index(
            "client_install_staged_runtime:"
        )
        launch = self.installer.index(
            "--ril-startup-ready-file",
            install_label,
        )
        wait = self.installer.index(
            "client_wait_for_startup_ready:",
            launch,
        )
        ready = self.installer.index(
            "client_startup_ready:",
            wait,
        )
        commit = self.installer.index(
            'Delete "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_UPDATE_PENDING_MARKER}"',
            ready,
        )
        failure = self.installer.index(
            "client_startup_failed:",
            commit,
        )

        self.assertLess(launch, wait)
        self.assertLess(wait, ready)
        self.assertLess(ready, commit)
        self.assertIn(
            "CLIENT_STARTUP_HEALTH_TIMEOUT_MILLISECONDS",
            self.installer[wait:ready],
        )
        failure_branch = self.installer[failure:]
        self.assertIn(
            "RIL_install_prepare.ps1",
            failure_branch,
        )
        self.assertIn(
            '-DescriptorPath "$ClientMigrationDescriptor"',
            failure_branch,
        )
        self.assertNotIn("taskkill.exe", failure_branch)
        validation = self.installer.index(
            "client_validate_startup_ready:",
            wait,
        )
        self.assertLess(validation, ready)
        validation_branch = self.installer[validation:ready]
        self.assertIn("-ExpectedVersion", validation_branch)
        self.assertIn("-ExpectedExecutable", validation_branch)
        self.assertIn("Get-CimInstance Win32_Process", self.startup_check)
        self.assertIn(
            "[string]$ready.version -ne $ExpectedVersion",
            self.startup_check,
        )
        self.assertIn(
            "[string]$process.ExecutablePath",
            self.startup_check,
        )

    def test_client_writes_versioned_startup_ready_file(self):
        with tempfile.TemporaryDirectory() as directory:
            ready_path = (
                Path(directory)
                / RIL_client._INSTALLATION[
                    "client_startup_ready_filename"
                ]
            )
            arguments = [
                "RIL_client.exe",
                RIL_client.CLIENT_STARTUP_READY_ARGUMENT,
                str(ready_path),
            ]
            with mock.patch.object(
                RIL_client,
                "APP_DIR",
                directory,
            ):
                qt_arguments, resolved_path = (
                    RIL_client._consume_startup_ready_argument(
                        arguments
                    )
                )
                RIL_client._write_startup_ready_file(
                    resolved_path
                )

            self.assertEqual(qt_arguments, ["RIL_client.exe"])
            payload = json.loads(
                ready_path.read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "ready")
            self.assertEqual(
                payload["version"],
                RIL_client.CURRENT_VERSION,
            )
            self.assertGreater(payload["pid"], 0)
        show = self.client_source.index("myWindow.show()")
        ready = self.client_source.index(
            "_write_startup_ready_file(startup_ready_path)",
            show,
        )
        process_events = self.client_source.index(
            "app.processEvents()",
            ready,
        )
        self.assertLess(show, ready)
        self.assertLess(ready, process_events)

    def test_windows_powershell_validates_ready_process_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            ready_path = Path(directory) / "ready.json"
            ready_path.write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "version": RIL_client.CURRENT_VERSION,
                        "pid": os.getpid(),
                    }
                ),
                encoding="utf-8",
            )
            command = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(
                    Path(__file__).resolve().parents[1]
                    / "dist"
                    / "make_setup"
                    / "RIL_client_startup_check.ps1"
                ),
                "-ReadyPath",
                str(ready_path),
                "-ExpectedVersion",
                RIL_client.CURRENT_VERSION,
                "-ExpectedExecutable",
                sys.executable,
            ]
            valid = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            invalid_version = subprocess.run(
                [
                    *command[:-3],
                    "wrong-version",
                    *command[-2:],
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(invalid_version.returncode, 2)

    def test_installer_console_helpers_run_without_visible_windows(self):
        self.assertNotIn(
            'ExecWait \'"${POWER_SHELL_EXECUTABLE}"',
            self.installer,
        )
        self.assertNotIn(
            "ExecWait '$SYSDIR\\schtasks.exe",
            self.installer,
        )
        self.assertRegex(
            self.installer,
            r"nsExec::ExecToLog "
            r"'\"\$\{POWER_SHELL_EXECUTABLE\}\"[^\r\n]*"
            r"RIL_install_prepare\.ps1[^\r\n]*'\r?\n"
            r"\s*Pop \$1",
        )
        self.assertRegex(
            self.installer,
            r"nsExec::ExecToLog "
            r"'\"\$\{POWER_SHELL_EXECUTABLE\}\"[^\r\n]*"
            r"RIL_client_startup_check\.ps1[^\r\n]*'\r?\n"
            r"\s*Pop \$4",
        )
        for action in ("Create", "Delete"):
            self.assertRegex(
                self.installer,
                r"nsExec::ExecToLog "
                r"'\"\$SYSDIR\\schtasks\.exe\" /"
                + action
                + r"[^\r\n]*'\r?\n\s*Pop \$4",
            )
        self.assertNotIn("taskkill.exe", self.installer)

    def test_recovery_task_is_created_before_old_names_are_deleted(self):
        function = self.installer.index(
            "Function PrepareClientRecovery"
        )
        function_end = self.installer.index(
            "FunctionEnd",
            function,
        )
        body = self.installer[function:function_end]
        create = body.index(
            '/Create /SC ONLOGON /TN '
            '"${CLIENT_RECOVERY_TASK_NAME}"'
        )
        delete_old = body.index(
            '/Delete /TN "$OldClientRecoveryTaskName"'
        )
        delete_target = body.index(
            '/Delete /TN "$TargetClientRecoveryTaskName"'
        )
        self.assertLess(create, delete_old)
        self.assertLess(create, delete_target)

    def test_failed_update_restarts_restored_client(self):
        for label in (
            "client_stage_failed:",
            "client_transaction_rolled_back:",
        ):
            start = self.installer.index(label)
            next_error_level = self.installer.index(
                "SetErrorLevel 12",
                start,
            )
            branch = self.installer[start:next_error_level]
            self.assertIn(
                'IfFileExists "$0\\${CLIENT_EXECUTABLE}"',
                branch,
            )
            self.assertIn(
                'ExecShell "open" "$0\\${CLIENT_EXECUTABLE}"',
                branch,
            )


if __name__ == "__main__":
    unittest.main()
