import codecs
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import RIL_client
from scripts import release_validation


class FakeResponse:
    def __init__(self, chunks, content_length=None):
        self.chunks = chunks
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self.chunks


class DownloadReliabilityTests(unittest.TestCase):
    def test_manifest_hash_is_forwarded_to_the_download_worker(self):
        expected_hash = "a" * 64
        remote_version = f"{RIL_client.CURRENT_VERSION}.99"
        response = mock.Mock()
        response.json.return_value = {
            "schema_version": 2,
            "version": remote_version,
            "client": {
                "url": "https://example.invalid/update.exe",
                "sha256": expected_hash,
            },
        }
        available = []
        worker = RIL_client.UpdateWorker()
        worker.update_available.connect(
            lambda *values: available.append(values)
        )

        with mock.patch.object(
            RIL_client.requests,
            "get",
            return_value=response,
        ):
            worker.run()

        self.assertEqual(
            available,
            [
                (
                    remote_version,
                    "https://example.invalid/update.exe",
                    expected_hash,
                )
            ],
        )

    def test_download_rejects_a_manifest_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.exe"
            destination.write_bytes(b"old")
            response = FakeResponse([b"new"], content_length=3)

            with mock.patch.object(
                RIL_client.requests,
                "get",
                return_value=response,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "SHA-256",
                ):
                    RIL_client.download_file(
                        "https://example.invalid/update.exe",
                        str(destination),
                        expected_sha256="0" * 64,
                    )

            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(Path(f"{destination}.part").exists())

    def test_download_accepts_the_manifest_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.exe"
            payload = b"verified update"
            response = FakeResponse(
                [payload],
                content_length=len(payload),
            )

            with mock.patch.object(
                RIL_client.requests,
                "get",
                return_value=response,
            ):
                RIL_client.download_file(
                    "https://example.invalid/update.exe",
                    str(destination),
                    expected_sha256=hashlib.sha256(
                        payload
                    ).hexdigest(),
                )

            self.assertEqual(destination.read_bytes(), payload)

    def test_worker_cancel_after_download_return_does_not_install(self):
        worker = RIL_client.DownloadWorker(
            "https://example.invalid/update.exe",
            "update.exe",
        )
        downloaded = []
        canceled = []
        worker.downloaded.connect(downloaded.append)
        worker.canceled.connect(lambda: canceled.append(True))

        def finish_while_canceling(*args, **kwargs):
            worker.cancel()

        with mock.patch.object(
            RIL_client,
            "download_file",
            side_effect=finish_while_canceling,
        ):
            worker.run()

        self.assertEqual(downloaded, [])
        self.assertEqual(canceled, [True])

    def test_worker_timeout_after_cancel_is_reported_as_canceled(self):
        worker = RIL_client.DownloadWorker(
            "https://example.invalid/update.exe",
            "update.exe",
        )
        errors = []
        canceled = []
        worker.error.connect(errors.append)
        worker.canceled.connect(lambda: canceled.append(True))

        def timeout_while_canceling(*args, **kwargs):
            worker.cancel()
            raise TimeoutError("read timed out")

        with mock.patch.object(
            RIL_client,
            "download_file",
            side_effect=timeout_while_canceling,
        ):
            worker.run()

        self.assertEqual(errors, [])
        self.assertEqual(canceled, [True])

    def test_download_uses_timeout_and_atomically_replaces_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.exe"
            destination.write_bytes(b"old")
            progress = []
            response = FakeResponse([b"new", b"-file"], content_length=8)

            with mock.patch.object(
                RIL_client.requests,
                "get",
                return_value=response,
            ) as request:
                RIL_client.download_file(
                    "https://example.invalid/update.exe",
                    str(destination),
                    progress_callback=progress.append,
                )

            self.assertEqual(destination.read_bytes(), b"new-file")
            self.assertFalse(Path(f"{destination}.part").exists())
            self.assertEqual(progress[-1], 100)
            request.assert_called_once_with(
                "https://example.invalid/update.exe",
                stream=True,
                timeout=RIL_client.DOWNLOAD_TIMEOUT,
                verify=False,
            )

    def test_cancel_removes_partial_file_and_keeps_old_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.exe"
            destination.write_bytes(b"old")
            response = FakeResponse([b"partial"], content_length=7)

            with mock.patch.object(
                RIL_client.requests,
                "get",
                return_value=response,
            ):
                with self.assertRaises(RIL_client.DownloadCancelled):
                    RIL_client.download_file(
                        "https://example.invalid/update.exe",
                        str(destination),
                        is_cancelled=lambda: True,
                    )

            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(Path(f"{destination}.part").exists())

    def test_incomplete_download_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.exe"
            response = FakeResponse([b"short"], content_length=10)

            with mock.patch.object(
                RIL_client.requests,
                "get",
                return_value=response,
            ):
                with self.assertRaises(OSError):
                    RIL_client.download_file(
                        "https://example.invalid/update.exe",
                        str(destination),
                    )

            self.assertFalse(destination.exists())
            self.assertFalse(Path(f"{destination}.part").exists())

    def test_slow_stream_cannot_extend_total_download_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.exe"
            response = FakeResponse([b"a", b"b"], content_length=2)

            with (
                mock.patch.object(
                    RIL_client.requests,
                    "get",
                    return_value=response,
                ),
                mock.patch.object(
                    RIL_client.time,
                    "monotonic",
                    side_effect=[0.0, 0.0, 0.05, 0.2],
                ),
            ):
                with self.assertRaisesRegex(
                    TimeoutError,
                    "전체 다운로드 시간이 초과",
                ):
                    RIL_client.download_file(
                        "https://example.invalid/update.exe",
                        str(destination),
                        total_timeout=0.1,
                    )

            self.assertFalse(destination.exists())
            self.assertFalse(Path(f"{destination}.part").exists())


class ReleaseValidationTests(unittest.TestCase):
    def test_server_installer_assets_are_part_of_build_provenance(self):
        self.assertIn(
            "dist/make_setup/RIL_server_start.bat",
            release_validation.BUILD_INPUTS,
        )
        self.assertIn(
            "dist/make_setup/RIL_server_restarter.cmd",
            release_validation.BUILD_INPUTS,
        )
        self.assertIn(
            "ril_build_version.py",
            release_validation.BUILD_INPUTS,
        )
        self.assertIn(
            "dist/make_setup/RIL_server_manual_install.ps1",
            release_validation.BUILD_INPUTS,
        )

    def test_build_version_module_is_generated_from_release_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_config = (
                Path(__file__).resolve().parents[1]
                / "ril_config.json"
            )
            (root / "ril_config.json").write_bytes(
                source_config.read_bytes()
            )
            config = release_validation.load_release_config(root)
            version = config["release"]["version"]
            protocol_version = config["release"]["protocol_version"]

            path = release_validation.write_build_version(
                root,
                version,
                protocol_version,
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                release_validation.build_version_source(
                    version,
                    protocol_version,
                ),
            )

            path.write_text(
                'VERSION = "false-latest"\nPROTOCOL_VERSION = 999\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "바이너리 고정 버전 모듈",
            ):
                release_validation.validate_build_version(
                    root,
                    version,
                    protocol_version,
                )

    def test_pyinstaller_output_names_follow_config(self):
        config = {
            "installation": {
                "client_executable": "Custom_Client.exe",
                "server_executable": "Custom_Server.exe",
            }
        }
        self.assertEqual(
            release_validation.build_output_paths(config),
            (
                "dist/Custom_Client",
                "dist/Custom_Server",
            ),
        )

    def test_build_info_rejects_source_changed_after_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.py").write_text("before", encoding="utf-8")
            (root / "output.exe").write_bytes(b"binary")

            release_validation.write_build_info(
                root,
                "260728",
                2,
                input_paths=("input.py",),
                output_paths=("output.exe",),
            )
            (root / "input.py").write_text("after", encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "현재 소스와 기존 바이너리가 일치하지 않습니다",
            ):
                release_validation.validate_build_info(
                    root,
                    "260728",
                    2,
                    input_paths=("input.py",),
                    output_paths=("output.exe",),
                )

    def test_build_info_hashes_the_entire_client_one_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_dir = root / "dist" / "RIL_client"
            runtime_dir = client_dir / "_internal"
            runtime_dir.mkdir(parents=True)
            (client_dir / "RIL_client.exe").write_bytes(b"client")
            runtime_dll = runtime_dir / "python313.dll"
            runtime_dll.write_bytes(b"before")
            (root / "input.py").write_text("source", encoding="utf-8")

            release_validation.write_build_info(
                root,
                "260728",
                2,
                input_paths=("input.py",),
                output_paths=("dist/RIL_client",),
            )
            runtime_dll.write_bytes(b"after")

            with self.assertRaisesRegex(
                RuntimeError,
                "현재 소스와 기존 바이너리가 일치하지 않습니다",
            ):
                release_validation.validate_build_info(
                    root,
                    "260728",
                    2,
                    input_paths=("input.py",),
                    output_paths=("dist/RIL_client",),
                )

    def test_release_rejects_manifest_from_another_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_config = (
                Path(__file__).resolve().parents[1]
                / "ril_config.json"
            )
            config = json.loads(
                source_config.read_text(encoding="utf-8-sig")
            )
            config["release"]["version"] = "260728"
            (root / "ril_config.json").write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )
            release_validation.write_build_version(
                root,
                "260728",
                2,
            )
            release = root / "release"
            release.mkdir()
            client = release / "Update_RIL.exe"
            server = release / "RIL_Server_Setup_260728.exe"
            client.write_bytes(b"client")
            server.write_bytes(b"server")
            (release / "version.txt").write_text(
                "26072801\n",
                encoding="ascii",
            )
            manifest = {
                "version": "2.1.0",
                "protocol_version": 2,
                "client": {
                    "url": (
                        "https://example.invalid/"
                        "v2.1.0/Update_RIL.exe"
                    ),
                    "sha256": release_validation.file_sha256(client),
                },
                "server": {
                    "file": "RIL_Server_Setup_2.1.0.exe",
                    "sha256": release_validation.file_sha256(server),
                },
            }
            (release / "update.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "버전/파일 구성이 일치하지 않습니다",
            ):
                release_validation.validate_release(
                    root,
                    "260728",
                    2,
                    "26072801",
                )

    def test_release_rejects_utf8_bom_in_http_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_config = (
                Path(__file__).resolve().parents[1]
                / "ril_config.json"
            )
            config = json.loads(
                source_config.read_text(encoding="utf-8-sig")
            )
            config["release"]["version"] = "260728"
            (root / "ril_config.json").write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )
            release_validation.write_build_version(root, "260728", 2)

            release = root / "release"
            release.mkdir()
            client_name = config["artifacts"][
                "client_installer_filename"
            ]
            server_name = config["artifacts"][
                "server_installer_filename_template"
            ].format(version="260728")
            client = release / client_name
            server = release / server_name
            client.write_bytes(b"client")
            server.write_bytes(b"server")
            (release / config["artifacts"]["legacy_version_filename"]).write_text(
                "26072801\n",
                encoding="ascii",
            )
            manifest = {
                "schema_version": 2,
                "version": "260728",
                "protocol_version": 2,
                "client": {
                    "url": release_validation.artifact_url(
                        config,
                        "260728",
                        client_name,
                    ),
                    "sha256": release_validation.file_sha256(client),
                    "size": client.stat().st_size,
                    "automatic_update": True,
                },
                "server": {
                    "url": release_validation.artifact_url(
                        config,
                        "260728",
                        server_name,
                    ),
                    "file": server_name,
                    "sha256": release_validation.file_sha256(server),
                    "size": server.stat().st_size,
                    "automatic_update": True,
                },
            }
            manifest_bytes = json.dumps(manifest).encode("utf-8")
            (release / config["artifacts"]["manifest_filename"]).write_bytes(
                codecs.BOM_UTF8 + manifest_bytes
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "manifest UTF-8 BOM",
            ):
                release_validation.validate_release(
                    root,
                    "260728",
                    2,
                    "26072801",
                )

    def test_publish_validates_locally_before_two_phase_github_flow(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = (
            repo_root / "scripts" / "publish_release.ps1"
        ).read_text(encoding="utf-8")

        verify_build = script.index("verify-build-info")
        verify_release = script.index("verify-release")
        local_validation = script.index(
            "\nAssert-LocalRelease -RequireBuildInfo:"
        )
        dry_run = script.index("if ($DryRun)")
        github_preflight = script.index("\nAssert-GitHubWriteAccess\n")
        stage = script.rindex('if ($Phase -eq "Stage")')

        self.assertLess(verify_build, local_validation)
        self.assertLess(verify_release, local_validation)
        self.assertLess(local_validation, dry_run)
        self.assertLess(dry_run, github_preflight)
        self.assertLess(github_preflight, stage)
        self.assertIn('[ValidateSet("Stage", "Activate")]', script)
        self.assertNotIn("-ServersDeployed", script)
        self.assertIn("unified client/server", script)

    def test_server_installer_uses_source_controlled_restarter_script(self):
        repo_root = Path(__file__).resolve().parents[1]
        installer = (
            repo_root / "RIL_Server_Setup.nsi"
        ).read_text(encoding="utf-8")
        restarter = (
            repo_root
            / "dist"
            / "make_setup"
            / "RIL_server_restarter.cmd"
        ).read_text(encoding="utf-8")

        self.assertIn("${SERVER_RESTARTER_SCRIPT}", installer)
        self.assertIn("${SERVER_RESTARTER_PS1}", installer)
        self.assertNotIn(
            'File "dist\\make_setup\\RIL_server_restarter.exe"',
            installer,
        )
        self.assertIn("RIL_server_restarter.ps1", restarter)
        restarter_ps1 = (
            repo_root
            / "dist"
            / "make_setup"
            / "RIL_server_restarter.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("ParentProcessId", restarter_ps1)
        self.assertIn("HashSet[int]", restarter_ps1)
        self.assertIn("--multiprocessing-fork", restarter_ps1)
        self.assertIn("Get-CimInstance Win32_Process", restarter_ps1)
        self.assertIn("server_update_state_relative_path", restarter_ps1)

    def test_client_installer_stages_and_rolls_back_runtime_transactionally(self):
        repo_root = Path(__file__).resolve().parents[1]
        installer = (
            repo_root / "RIL_Client_Update.nsi"
        ).read_text(encoding="utf-8")

        stage_runtime = installer.index(
            'File /r "${CLIENT_BUILD_DIRECTORY}\\*.*"'
        )
        backup_client_runtime = installer.index(
            'Rename "$0\\${CLIENT_RUNTIME_DIRECTORY}" '
            '"$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_RUNTIME_DIRECTORY}"'
        )
        backup_legacy_runtime = installer.index(
            'Rename "$0\\${LEGACY_RUNTIME_DIRECTORY}" '
            '"$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${LEGACY_RUNTIME_DIRECTORY}"'
        )
        install_runtime = installer.index(
            'Rename "$0\\${CLIENT_UPDATE_STAGE_DIRECTORY}\\'
            '${CLIENT_RUNTIME_DIRECTORY}" '
            '"$0\\${CLIENT_RUNTIME_DIRECTORY}"'
        )
        backup_complete = installer.index(
            'FileOpen $2 "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_UPDATE_BACKUP_COMPLETE_MARKER}" w'
        )
        commit = installer.index(
            'Delete "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_UPDATE_PENDING_MARKER}"'
        )

        self.assertLess(stage_runtime, backup_client_runtime)
        self.assertLess(
            backup_client_runtime,
            backup_legacy_runtime,
        )
        self.assertLess(backup_legacy_runtime, backup_complete)
        self.assertLess(backup_complete, install_runtime)
        self.assertLess(install_runtime, commit)
        launch = installer.index(
            'ExecShell "open" "$0\\${CLIENT_EXECUTABLE}"',
            install_runtime,
        )
        self.assertLess(launch, commit)
        self.assertIn(
            "IfErrors client_startup_failed",
            installer[launch:commit],
        )
        self.assertIn(
            "client_wait_for_startup_ready:",
            installer[launch:commit],
        )
        startup_failure = installer.index(
            "client_startup_failed:",
            commit,
        )
        self.assertIn(
            "Goto client_transaction_failed",
            installer[startup_failure:],
        )
        self.assertIn("Call RecoverInterruptedClientUpdate", installer)
        self.assertIn("client_transaction_failed:", installer)
        self.assertIn("client_transaction_rolled_back:", installer)
        self.assertIn(
            'IfFileExists "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_UPDATE_PENDING_MARKER}"',
            installer,
        )
        self.assertIn("Call PrepareClientRecovery", installer)
        self.assertIn("Call CleanupClientRecovery", installer)
        self.assertIn("/SC ONLOGON", installer)
        self.assertIn("${CLIENT_RECOVERY_TASK_NAME}", installer)
        self.assertIn("/S /RECOVERY", installer)
        self.assertIn("UPDATE_MUTEX_NAME", installer)
        self.assertIn(
            'CreateMutexW(p 0, i 1, w "${UPDATE_MUTEX_NAME}")',
            installer,
        )
        self.assertIn(
            "WaitForSingleObject(p r7, "
            "i ${UPDATE_MUTEX_WAIT_MILLISECONDS})",
            installer,
        )
        self.assertIn("FileSeek", installer)
        self.assertIn(
            'StrCmp $RecoveryMode "1" client_recovery_only',
            installer,
        )
        recovery_only = installer.index("client_recovery_only:")
        recovery_launch = installer.index(
            'ExecShell "open" "$0\\${CLIENT_EXECUTABLE}"',
            recovery_only,
        )
        recovery_cleanup = installer.index(
            "Call CleanupClientRecovery",
            recovery_only,
        )
        self.assertLess(recovery_launch, recovery_cleanup)
        self.assertIn(
            "IfErrors client_recovery_launch_failed",
            installer[recovery_launch:recovery_cleanup],
        )
        self.assertIn(
            'Rename "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_RUNTIME_DIRECTORY}" '
            '"$0\\${CLIENT_RUNTIME_DIRECTORY}"',
            installer,
        )
        self.assertIn(
            'Rename "$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${LEGACY_RUNTIME_DIRECTORY}" '
            '"$0\\${LEGACY_RUNTIME_DIRECTORY}"',
            installer,
        )
        self.assertIn(
            'IfFileExists "$0\\${CLIENT_RUNTIME_DIRECTORY}\\*.*" '
            "0 client_remove_empty_client_runtime",
            installer,
        )
        self.assertIn(
            'IfFileExists "$0\\${LEGACY_RUNTIME_DIRECTORY}\\*.*" '
            "0 client_remove_empty_legacy_runtime",
            installer,
        )
        self.assertIn("!ifndef CLIENT_RUNTIME_DIRECTORY", installer)
        self.assertIn("!ifndef LEGACY_RUNTIME_DIRECTORY", installer)
        self.assertNotIn("SERVER_RUNTIME_DIRECTORY", installer)
        self.assertNotIn("ril_config.local.json", installer)

    def test_server_installer_aborts_when_task_creation_fails(self):
        repo_root = Path(__file__).resolve().parents[1]
        transaction = (
            repo_root
            / "dist"
            / "make_setup"
            / "RIL_server_manual_install.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("New-ServerTasks", transaction)
        self.assertGreaterEqual(transaction.count("/Create"), 2)
        self.assertIn(
            'throw "서버 자동실행 작업을 만들지 못했습니다."',
            transaction,
        )
        self.assertIn(
            'throw "서버 restarter 작업을 만들지 못했습니다."',
            transaction,
        )

    def test_build_removes_old_versioned_server_installers(self):
        repo_root = Path(__file__).resolve().parents[1]
        build_script = (
            repo_root / "scripts" / "build_release.ps1"
        ).read_text(encoding="utf-8")

        tests = build_script.index(
            "python -m unittest discover -s tests -v"
        )
        pyinstaller = build_script.index("python -m PyInstaller")
        self.assertLess(tests, pyinstaller)
        self.assertIn("-Filter $serverInstallerFilter", build_script)
        self.assertIn(
            'server_installer_filename_template',
            build_script,
        )
        self.assertNotIn("Get-FileHash", build_script)
        self.assertIn("hashlib.sha256", build_script)
        self.assertIn("write-version-module", build_script)
        self.assertIn(
            "/DCLIENT_BUILD_DIRECTORY=$clientBuildDirectory",
            build_script,
        )
        self.assertIn(
            "/DCLIENT_RECOVERY_TASK_NAME="
            "$($installation.client_update_recovery_task_name)",
            build_script,
        )
        self.assertIn(
            "/DUPDATE_MUTEX_NAME="
            "$($installation.update_mutex_name)",
            build_script,
        )
        self.assertIn(
            "/DUPDATE_MUTEX_WAIT_MILLISECONDS="
            "$updateMutexWaitMilliseconds",
            build_script,
        )
        self.assertIn(
            "[System.Text.UTF8Encoding]::new($false)",
            build_script,
        )
        self.assertIn(
            "/DCLIENT_RUNTIME_DIRECTORY="
            "$($installation.client_runtime_directory)",
            build_script,
        )
        self.assertIn(
            "/DLEGACY_RUNTIME_DIRECTORY="
            "$($installation.legacy_runtime_directory)",
            build_script,
        )
        self.assertIn(
            "/DSERVER_BUILD_DIRECTORY=$serverBuildDirectory",
            build_script,
        )
        self.assertIn(
            "/DSERVER_RUNTIME_DIRECTORY="
            "$($installation.server_runtime_directory)",
            build_script,
        )

    def test_specs_use_configured_binary_names_and_assets(self):
        repo_root = Path(__file__).resolve().parents[1]
        client_spec = (
            repo_root / "RIL_client.spec"
        ).read_text(encoding="utf-8")
        server_spec = (
            repo_root / "RIL_server.spec"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "installation['client_executable']",
            client_spec,
        )
        self.assertIn("installation['icon_file']", client_spec)
        self.assertNotIn("name='RIL_client'", client_spec)
        self.assertNotIn("icon=['chunsik1.ico']", client_spec)
        self.assertIn(
            "installation['client_runtime_directory']",
            client_spec,
        )
        self.assertIn(
            "contents_directory=client_runtime_directory",
            client_spec,
        )

        self.assertIn(
            "installation['server_executable']",
            server_spec,
        )
        self.assertIn(
            "installation['server_update_helper_script']",
            server_spec,
        )
        self.assertIn(
            "'server_restarter_power_shell_script'",
            server_spec,
        )
        self.assertIn("installation['icon_file']", server_spec)
        self.assertNotIn("name='RIL_server'", server_spec)
        self.assertNotIn("icon=['chunsik1.ico']", server_spec)
        self.assertIn("exclude_binaries=True", server_spec)
        self.assertIn(
            "installation['server_runtime_directory']",
            server_spec,
        )
        self.assertIn(
            "contents_directory=server_runtime_directory",
            server_spec,
        )
        self.assertIn("coll = COLLECT(", server_spec)

    def test_server_installer_replaces_the_complete_onedir_runtime(self):
        repo_root = Path(__file__).resolve().parents[1]
        installer = (
            repo_root / "RIL_Server_Setup.nsi"
        ).read_text(encoding="utf-8")
        transaction = (
            repo_root
            / "dist"
            / "make_setup"
            / "RIL_server_manual_install.ps1"
        ).read_text(encoding="utf-8")

        cleanup = transaction.index("Remove-ServerPayload")
        copy_bundle = transaction.index(
            "Copy-Item -LiteralPath $sourceRuntime"
        )
        self.assertLess(cleanup, copy_bundle)
        self.assertIn(
            "Test-Path -LiteralPath $installedRuntime",
            transaction,
        )
        self.assertNotIn("CLIENT_RUNTIME_DIRECTORY", installer)
        self.assertNotIn("LEGACY_RUNTIME_DIRECTORY", installer)
        self.assertIn("-Mode ManualTransactional", installer)
        self.assertIn("-Mode UpdatePayload", installer)

    def test_workflow_runs_tests_and_uploads_only_release_set(self):
        repo_root = Path(__file__).resolve().parents[1]
        workflow = (
            repo_root / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        build_script = (
            repo_root / "scripts" / "build_release.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "python -m unittest discover -s tests -v",
            build_script,
        )
        self.assertIn(r".\scripts\build_release.ps1", workflow)
        self.assertIn(
            "${{ steps.release_paths.outputs.client }}",
            workflow,
        )
        self.assertIn(
            "${{ steps.release_paths.outputs.server }}",
            workflow,
        )
        self.assertIn(
            "${{ steps.release_paths.outputs.manifest }}",
            workflow,
        )
        self.assertIn(
            "${{ steps.release_paths.outputs.legacy }}",
            workflow,
        )
        self.assertNotIn("path: release/\n", workflow)


if __name__ == "__main__":
    unittest.main()
