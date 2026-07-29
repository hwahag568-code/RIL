from datetime import datetime, timezone
import codecs
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock

import RIL_server
import ril_update


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeProcess:
    def __init__(self):
        self.alive = True
        self.start_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_calls = []

    def start(self):
        self.start_calls += 1

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminate_calls += 1
        self.alive = False

    def kill(self):
        self.kill_calls += 1
        self.alive = False

    def join(self, timeout=None):
        self.join_calls.append(timeout)


class ManifestComponentTests(unittest.TestCase):
    def test_fetch_manifest_accepts_utf8_bom_regardless_of_http_charset(self):
        response = mock.Mock()
        response.content = (
            codecs.BOM_UTF8
            + json.dumps(
                {
                    "schema_version": 2,
                    "version": "260728.3",
                }
            ).encode("utf-8")
        )
        requests_module = mock.Mock()
        requests_module.get.return_value = response

        manifest = ril_update.fetch_manifest(
            "https://example/update.json",
            3,
            requests_module=requests_module,
        )

        self.assertEqual(manifest["version"], "260728.3")

    def test_client_and_server_use_the_same_top_level_version(self):
        manifest = {
            "schema_version": 2,
            "version": "260728.3",
            "client": {
                "url": "https://example/client.exe",
                "sha256": "a" * 64,
                "size": 10,
            },
            "server": {
                "url": "https://example/server.exe",
                "sha256": "b" * 64,
                "size": 20,
                "automatic_update": True,
            },
        }

        client_update = ril_update.get_component_update(
            manifest,
            "client",
            "260728.2",
        )
        server_update = ril_update.get_component_update(
            manifest,
            "server",
            "260728.2",
        )

        self.assertEqual(client_update["version"], "260728.3")
        self.assertEqual(server_update["version"], "260728.3")

    def test_server_manifest_can_explicitly_disable_automatic_update(self):
        manifest = {
            "schema_version": 2,
            "version": "260728.3",
            "server": {
                "url": "https://example/server.exe",
                "sha256": "a" * 64,
                "automatic_update": False,
            },
        }
        self.assertIsNone(
            ril_update.get_component_update(
                manifest,
                "server",
                "260728.2",
            )
        )

    def test_client_manifest_can_explicitly_disable_automatic_update(self):
        manifest = {
            "schema_version": 2,
            "version": "260728.3",
            "client": {
                "url": "https://example/client.exe",
                "sha256": "a" * 64,
                "automatic_update": False,
            },
        }
        self.assertIsNone(
            ril_update.get_component_update(
                manifest,
                "client",
                "260728.2",
            )
        )

    def test_newer_manifest_with_unknown_schema_is_rejected(self):
        manifest = {
            "schema_version": 3,
            "version": "260728.3",
            "client": {
                "url": "https://example/client.exe",
                "sha256": "a" * 64,
            },
        }
        with self.assertRaisesRegex(
            ril_update.UpdateManifestError,
            "schema_version",
        ):
            ril_update.get_component_update(
                manifest,
                "client",
                "260728.2",
            )


class ServerUpdateTests(unittest.TestCase):
    def _update(self):
        return {
            "version": "260728.3",
            "url": "https://example/server.exe",
            "sha256": "a" * 64,
            "size": 123,
            "file": "server.exe",
        }

    def test_server_download_is_verified_before_becoming_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "server.exe"
            update = self._update()
            with (
                mock.patch.object(
                    RIL_server,
                    "fetch_manifest",
                    return_value={"version": "260728.3"},
                ),
                mock.patch.object(
                    RIL_server,
                    "get_component_update",
                    return_value=update,
                ),
                mock.patch.object(
                    RIL_server,
                    "server_update_destination",
                    return_value=destination,
                ),
                mock.patch.object(
                    RIL_server,
                    "_verified_update_exists",
                    return_value=False,
                ),
                mock.patch.object(
                    RIL_server,
                    "download_verified_file",
                ) as download,
                mock.patch.object(
                    RIL_server,
                    "write_server_update_state",
                ) as write_state,
            ):
                result = RIL_server.check_server_update_once()

        self.assertEqual(result["installer_path"], str(destination))
        download.assert_called_once()
        self.assertEqual(
            download.call_args.kwargs["expected_size"],
            123,
        )
        write_state.assert_called_once_with(
            "downloaded_verified",
            target_version="260728.3",
            installer_path=str(destination),
            sha256="a" * 64,
        )

    def test_failed_target_version_is_throttled(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "state": "failed_rolled_back",
                        "target_version": "260728.3",
                        "updated_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    RIL_server,
                    "server_update_state_path",
                    return_value=state_path,
                ),
                mock.patch.object(
                    RIL_server,
                    "fetch_manifest",
                    return_value={"version": "260728.3"},
                ),
                mock.patch.object(
                    RIL_server,
                    "get_component_update",
                    return_value=self._update(),
                ),
                mock.patch.object(
                    RIL_server,
                    "server_update_destination",
                ) as destination,
                mock.patch.object(RIL_server, "ErrorLog"),
            ):
                result = RIL_server.check_server_update_once()

        self.assertIsNone(result)
        destination.assert_not_called()

    def test_rollback_start_state_throttles_only_the_same_target(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "state": RIL_server.ROLLBACK_START_STATE,
                        "current_version": RIL_server.SERVER_VERSION,
                        "target_version": "260728.3",
                        "allowed_server_version": (
                            RIL_server.SERVER_VERSION
                        ),
                        "updated_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                RIL_server,
                "server_update_state_path",
                return_value=state_path,
            ):
                self.assertTrue(
                    RIL_server._failed_update_is_in_cooldown(
                        "260728.3"
                    )
                )
                self.assertFalse(
                    RIL_server._failed_update_is_in_cooldown(
                        "260728.4"
                    )
                )

    def test_update_launcher_uses_staged_effective_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "server.exe"
            installer.write_bytes(b"installer")
            helper_source = root / "source" / "helper.ps1"
            helper_source.parent.mkdir()
            helper_source.write_text("Write-Output ok", encoding="utf-8")
            restarter_source = root / "source" / "restarter.ps1"
            restarter_source.write_text(
                "Write-Output recover",
                encoding="utf-8",
            )
            state_path = root / "state.json"
            health_path = root / "health.json"

            def resolve_resource(filename):
                if filename == RIL_server._INSTALLATION[
                    "server_update_helper_script"
                ]:
                    return helper_source
                if filename == RIL_server._INSTALLATION[
                    "server_restarter_power_shell_script"
                ]:
                    return restarter_source
                raise AssertionError(filename)

            mismatched_runtime_config = json.loads(
                json.dumps(RIL_server._CONFIG)
            )
            mismatched_runtime_config["release"]["version"] = "999999.9"
            mismatched_runtime_config["release"][
                "protocol_version"
            ] = 999
            helper_process = mock.Mock()
            helper_process.pid = 4321
            helper_process.poll.return_value = None

            def start_helper(command, **_kwargs):
                ready_index = command.index("-ReadyPath")
                Path(command[ready_index + 1]).write_text(
                    json.dumps(
                        {
                            "helper_pid": helper_process.pid,
                            "target_version": self._update()["version"],
                        }
                    ),
                    encoding="utf-8",
                )
                return helper_process

            with (
                mock.patch.object(
                    RIL_server,
                    "resource_path",
                    side_effect=resolve_resource,
                ),
                mock.patch.object(
                    RIL_server,
                    "_CONFIG",
                    mismatched_runtime_config,
                ),
                mock.patch.object(
                    RIL_server,
                    "server_update_state_path",
                    return_value=state_path,
                ),
                mock.patch.object(
                    RIL_server,
                    "server_health_path",
                    return_value=health_path,
                ),
                mock.patch.object(
                    RIL_server,
                    "write_server_update_state",
                ),
                mock.patch.object(
                    RIL_server.subprocess,
                    "Popen",
                    side_effect=start_helper,
                ) as popen,
            ):
                RIL_server.launch_server_update(
                    {
                        **self._update(),
                        "installer_path": str(installer),
                    }
                )

            effective_path = root / RIL_server._INSTALLATION[
                "server_effective_config_filename"
            ]
            effective = json.loads(
                effective_path.read_text(encoding="utf-8")
            )
            staged_restarter = root / restarter_source.name
            staged_restarter_text = staged_restarter.read_text(
                encoding="utf-8"
            )
            helper_ready = root / RIL_server._INSTALLATION[
                "server_update_helper_ready_filename"
            ]
            helper_ready_was_removed = not helper_ready.exists()
            command = popen.call_args.args[0]

        self.assertEqual(
            effective["release"]["version"],
            RIL_server.SERVER_VERSION,
        )
        self.assertEqual(
            effective["release"]["protocol_version"],
            RIL_server.PROTOCOL_VERSION,
        )
        self.assertEqual(
            staged_restarter_text,
            "Write-Output recover",
        )
        self.assertTrue(helper_ready_was_removed)
        config_index = command.index("-ConfigPath")
        self.assertEqual(command[config_index + 1], str(effective_path))
        ready_index = command.index("-ReadyPath")
        self.assertEqual(command[ready_index + 1], str(helper_ready))

    def test_helper_exit_before_handshake_fails_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            ready_path = Path(directory) / "ready.json"
            process = mock.Mock()
            process.pid = 4321
            process.poll.return_value = 31

            with self.assertRaisesRegex(
                RuntimeError,
                "준비 전에 종료",
            ):
                RIL_server.wait_for_update_helper_ready(
                    process,
                    ready_path,
                    "260728.3",
                )

        self.assertFalse(ready_path.exists())

    def test_supervisor_waits_for_listener_drain_ack(self):
        tray = FakeProcess()
        server = FakeProcess()
        wakeup = FakeProcess()
        ready = queue.Queue()
        ready.put(
            {
                "version": "260728.3",
                "installer_path": "server.exe",
            }
        )
        drain_requested = threading.Event()
        drain_complete = threading.Event()
        launcher = mock.Mock()
        restart = mock.Mock()

        def complete_drain(_interval):
            launcher.assert_not_called()
            self.assertTrue(drain_requested.is_set())
            drain_complete.set()

        with (
            mock.patch.object(
                RIL_server.time,
                "sleep",
                side_effect=complete_drain,
            ),
            mock.patch.object(RIL_server, "ErrorLog"),
            mock.patch.object(
                RIL_server,
                "write_server_update_state",
            ) as write_state,
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                update_queue=ready,
                update_launcher=launcher,
                drain_requested_event=drain_requested,
                drain_complete_event=drain_complete,
            )

        self.assertEqual(action, "update")
        restart.assert_not_called()
        launcher.assert_called_once()
        self.assertEqual(
            [call.args[0] for call in write_state.call_args_list],
            ["draining", "draining_complete"],
        )

    def test_launcher_failure_records_failed_state_before_restart(self):
        tray = FakeProcess()
        server = FakeProcess()
        wakeup = FakeProcess()
        ready = queue.Queue()
        ready.put(
            {
                "version": "260728.3",
                "installer_path": "server.exe",
            }
        )
        drain_requested = threading.Event()
        drain_complete = threading.Event()
        drain_complete.set()
        launcher = mock.Mock(side_effect=OSError("launch failed"))
        restart = mock.Mock()

        with (
            mock.patch.object(RIL_server, "ErrorLog"),
            mock.patch.object(
                RIL_server,
                "write_server_update_state",
            ) as write_state,
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                update_queue=ready,
                update_launcher=launcher,
                drain_requested_event=drain_requested,
                drain_complete_event=drain_complete,
            )

        self.assertEqual(action, "restart")
        restart.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in write_state.call_args_list],
            ["draining", "draining_complete", "failed"],
        )
        failed_details = write_state.call_args_list[-1].kwargs
        self.assertEqual(failed_details["target_version"], "260728.3")
        self.assertIn("launch failed", failed_details["error"])

    def test_rollback_start_state_allows_only_the_previous_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = {
                "state": RIL_server.ROLLBACK_START_STATE,
                "current_version": RIL_server.SERVER_VERSION,
                "target_version": f"{RIL_server.SERVER_VERSION}.99",
                "allowed_server_version": RIL_server.SERVER_VERSION,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            state_path.write_text(
                json.dumps(state),
                encoding="utf-8",
            )

            with mock.patch.object(
                RIL_server,
                "server_update_state_path",
                return_value=state_path,
            ):
                self.assertFalse(
                    RIL_server.update_state_blocks_this_server_start()
                )
                state["allowed_server_version"] = "old-other-version"
                state_path.write_text(
                    json.dumps(state),
                    encoding="utf-8",
                )
                self.assertTrue(
                    RIL_server.update_state_blocks_this_server_start()
                )

    def test_deployment_scripts_keep_recovery_available(self):
        prepare = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_install_prepare.ps1"
        ).read_text(encoding="utf-8-sig")
        restarter = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_restarter.ps1"
        ).read_text(encoding="utf-8-sig")
        helper = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_update_helper.ps1"
        ).read_text(encoding="utf-8-sig")
        start_script = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_start.ps1"
        ).read_text(encoding="utf-8-sig")
        installer = (
            REPO_ROOT / "RIL_Server_Setup.nsi"
        ).read_text(encoding="utf-8-sig")

        self.assertNotIn("/Delete /TN", prepare)
        self.assertIn('"rollback"', restarter)
        self.assertIn("backup_directory", restarter)
        self.assertIn("Complete-RecoveryIfNeeded", restarter)
        self.assertIn("ready_at", restarter)
        self.assertIn("$helperIsRunning", restarter)
        self.assertIn("$serverIsStillRunning", restarter)
        self.assertIn("$backupComplete", helper)
        self.assertIn("ready_at", helper)
        self.assertIn("ExecutablePath", helper)
        self.assertIn(
            "$config.installation.update_mutex_name",
            helper,
        )
        self.assertIn(
            "$config.update.mutex_wait_seconds",
            helper,
        )
        self.assertIn("$updateMutex.WaitOne(", helper)
        self.assertNotIn("$updateMutex.WaitOne(0)", helper)
        self.assertIn("$updateMutex.ReleaseMutex()", helper)
        self.assertIn("Set-StagedRecoveryTasks", helper)
        self.assertIn("-ConfigPath `\"$ConfigPath`\"", helper)
        self.assertIn("[string]$ReadyPath", helper)
        self.assertIn("$readyPublished", helper)
        self.assertIn(
            'Write-UpdateState "ready_to_install"',
            helper,
        )
        self.assertIn("server_runtime_directory", helper)
        self.assertIn(
            "Copy-Item -LiteralPath $runtimeSource",
            helper,
        )
        self.assertIn(
            "Copy-Item -LiteralPath $backupRuntime",
            helper,
        )
        helper_runtime_remove = helper.index(
            "Remove-Item -LiteralPath $installedRuntime"
        )
        helper_runtime_restore = helper.index(
            "Copy-Item -LiteralPath $backupRuntime"
        )
        self.assertLess(helper_runtime_remove, helper_runtime_restore)
        self.assertIn("server_runtime_directory", restarter)
        restarter_runtime_remove = restarter.index(
            "Remove-Item -LiteralPath $installedRuntime"
        )
        restarter_runtime_restore = restarter.index(
            "Copy-Item -LiteralPath $backupRuntime"
        )
        self.assertLess(
            restarter_runtime_remove,
            restarter_runtime_restore,
        )
        self.assertIn("-Recurse -Force", helper)
        self.assertIn("-Recurse -Force", restarter)
        helper_restore = helper.index("function Restore-PreviousServer")
        helper_rollback_state = helper.index(
            'Write-UpdateState "rollback_starting_previous"',
            helper_restore,
        )
        helper_previous_start = helper.index(
            "Start-Process -FilePath $serverPath",
            helper_restore,
        )
        self.assertLess(helper_rollback_state, helper_previous_start)
        self.assertIn("allowed_server_version", helper)
        helper_failure = helper.index(
            "$failure = $_.Exception.Message"
        )
        helper_backup_condition = helper.index(
            "if ($backupComplete)",
            helper_failure,
        )
        helper_blocking_rollback = helper.index(
            'Write-UpdateState "rollback"',
            helper_failure,
        )
        helper_no_backup_rollback = helper.index(
            'Write-UpdateState "rollback_starting_previous"',
            helper_failure,
        )
        helper_failure_restore = helper.index(
            "Restore-PreviousServer -RestoreFiles $backupComplete",
            helper_failure,
        )
        self.assertLess(
            helper_backup_condition,
            helper_blocking_rollback,
        )
        self.assertLess(
            helper_blocking_rollback,
            helper_no_backup_rollback,
        )
        self.assertLess(
            helper_no_backup_rollback,
            helper_failure_restore,
        )
        no_backup_state = helper[
            helper_no_backup_rollback:helper_failure_restore
        ]
        self.assertIn(
            "allowed_server_version = $previousVersion",
            no_backup_state,
        )
        self.assertIn("backup_directory = $null", no_backup_state)
        restarter_rollback_state = restarter.index(
            '-State "rollback_starting_previous"'
        )
        restarter_previous_start = restarter.index(
            "Start-Process -FilePath $serverPath",
            restarter_rollback_state,
        )
        self.assertLess(
            restarter_rollback_state,
            restarter_previous_start,
        )
        self.assertIn("allowed_server_version", restarter)
        self.assertIn(
            "target_version = [string]$PreviousState.target_version",
            restarter,
        )
        self.assertIn(
            'updated_at = [DateTime]::UtcNow.ToString("o")',
            restarter,
        )
        no_backup_recovery = restarter.index(
            "elseif ($isUpdateState)"
        )
        no_backup_state_write = restarter.index(
            '-State "rollback_starting_previous"',
            no_backup_recovery,
        )
        no_backup_previous_start = restarter.index(
            "Start-Process -FilePath $serverPath",
            no_backup_recovery,
        )
        self.assertLess(
            no_backup_state_write,
            no_backup_previous_start,
        )
        no_backup_segment = restarter[
            no_backup_recovery:no_backup_previous_start
        ]
        self.assertIn(
            "$recoveryVersion = [string]$config.release.version",
            no_backup_segment,
        )
        self.assertIn(
            "-CurrentVersion $recoveryVersion",
            no_backup_segment,
        )
        stale_running_recovery = restarter.index(
            "if ($null -ne $recoveryState -and "
            "$parents.Count -gt 0)"
        )
        stale_running_stop = restarter.index(
            "Stop-Process -Id $_.ProcessId -Force",
            stale_running_recovery,
        )
        stale_running_start = restarter.index(
            "Start-Process -FilePath $serverPath",
            stale_running_stop,
        )
        stale_running_health = restarter.index(
            "Complete-RecoveryIfNeeded",
            stale_running_start,
        )
        self.assertLess(stale_running_stop, stale_running_start)
        self.assertLess(stale_running_start, stale_running_health)
        restarter_lock = restarter.index("$updateMutex.WaitOne(0)")
        restarter_recovery = restarter.index(
            "if (Test-Path -LiteralPath $statePath)"
        )
        restarter_unlock = restarter.rindex(
            "$updateMutex.ReleaseMutex()"
        )
        self.assertLess(restarter_lock, restarter_recovery)
        self.assertLess(restarter_recovery, restarter_unlock)
        self.assertIn(
            "$config.installation.update_mutex_name",
            restarter,
        )
        self.assertIn(
            "catch [Threading.AbandonedMutexException]",
            restarter,
        )
        self.assertNotIn("client_runtime_directory", helper)
        self.assertNotIn("legacy_runtime_directory", helper)
        self.assertNotIn("client_runtime_directory", restarter)
        self.assertNotIn("legacy_runtime_directory", restarter)
        self.assertNotIn("client_update_mutex_name", helper)
        self.assertNotIn("server_update_mutex_name", helper)
        self.assertNotIn("client_update_mutex_name", restarter)
        self.assertNotIn("server_update_mutex_name", restarter)
        self.assertEqual(
            helper.count("ConvertFrom-Json"),
            helper.count("-Encoding UTF8 |"),
        )
        self.assertEqual(
            restarter.count("ConvertFrom-Json"),
            restarter.count("-Encoding UTF8 |"),
        )
        self.assertEqual(
            start_script.count("ConvertFrom-Json"),
            start_script.count("-Encoding UTF8 |"),
        )
        self.assertNotIn("exit 30", helper)
        self.assertIn('[string]$ConfigPath = ""', restarter)
        self.assertIn("SetRegView 64", installer)
        self.assertIn("SetRegView 32", installer)
        self.assertIn("UPDATE_MUTEX_NAME", installer)
        self.assertIn(
            'CreateMutexW(p 0, i 1, w "${UPDATE_MUTEX_NAME}")',
            installer,
        )
        self.assertIn(
            "WaitForSingleObject(p r3, "
            "i ${UPDATE_MUTEX_WAIT_MILLISECONDS})",
            installer,
        )
        self.assertIn(
            'StrCmp $5 "128" update_mutex_acquired '
            "update_mutex_busy",
            installer,
        )

    def test_update_scripts_separate_previous_and_target_bootstrap(self):
        helper = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_update_helper.ps1"
        ).read_text(encoding="utf-8-sig")
        restarter = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_restarter.ps1"
        ).read_text(encoding="utf-8-sig")

        for marker in (
            "$previousConfig",
            "$targetConfig",
            "previous_config",
            "target_config",
            "Get-ManagedServerFileNames",
            "Get-ManagedRuntimeNames",
            "Remove-ServerTaskDefinitions",
            "Restore-RegistrySnapshots",
        ):
            with self.subTest(script="helper", marker=marker):
                self.assertIn(marker, helper)
            with self.subTest(script="restarter", marker=marker):
                self.assertIn(marker, restarter)

        self.assertIn(
            "Wait-ServerHealth -Config $targetConfig",
            helper,
        )
        self.assertIn(
            "Restore-PreviousServer -RestoreFiles $backupComplete",
            helper,
        )
        helper_installer_exit = helper.index(
            "if ($process.ExitCode -ne 0)"
        )
        helper_descriptor = helper.rfind(
            "Update-MigrationDescriptor",
            0,
            helper_installer_exit,
        )
        helper_target_health = helper.index(
            "Wait-ServerHealth -Config $targetConfig"
        )
        self.assertGreaterEqual(helper_descriptor, 0)
        self.assertLess(helper_descriptor, helper_target_health)

        restarter_runtime_restore = restarter.index(
            "Copy-Item -LiteralPath $backupRuntime"
        )
        restarter_old_tasks = restarter.index(
            "New-ServerTasks -Config $config",
            restarter_runtime_restore,
        )
        restarter_task_cleanup = restarter.index(
            "Remove-ObsoleteServerTaskDefinitions",
            restarter_old_tasks,
        )
        self.assertLess(
            restarter_runtime_restore,
            restarter_old_tasks,
        )
        self.assertLess(
            restarter_old_tasks,
            restarter_task_cleanup,
        )


if __name__ == "__main__":
    unittest.main()
