import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ril_config import (
    CLIENT_INSTALLED_CONFIG_FILENAME,
    CONFIG_ENVIRONMENT_VARIABLE,
    SERVER_INSTALLED_CONFIG_FILENAME,
    load_config,
    select_component_config,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class ComponentConfigIsolationTests(unittest.TestCase):
    @staticmethod
    def source_between(source, start_marker, end_marker):
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        return source[start:end]

    @classmethod
    def setUpClass(cls):
        cls.base_config = json.loads(
            (REPO_ROOT / "ril_config.json").read_text(
                encoding="utf-8-sig",
            )
        )
        cls.client_source = (REPO_ROOT / "RIL_client.py").read_text(
            encoding="utf-8-sig",
        )
        cls.server_source = (REPO_ROOT / "RIL_server.py").read_text(
            encoding="utf-8-sig",
        )
        cls.client_installer = (
            REPO_ROOT / "RIL_Client_Update.nsi"
        ).read_text(encoding="utf-8-sig")
        cls.server_installer = (
            REPO_ROOT / "RIL_Server_Setup.nsi"
        ).read_text(encoding="utf-8-sig")
        cls.server_start = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_start.ps1"
        ).read_text(encoding="utf-8-sig")
        cls.server_restarter = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_restarter.ps1"
        ).read_text(encoding="utf-8-sig")
        cls.server_helper = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_update_helper.ps1"
        ).read_text(encoding="utf-8-sig")
        cls.server_transaction = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_manual_install.ps1"
        ).read_text(encoding="utf-8-sig")

    def test_server_first_root_overwrite_does_not_change_old_client_config(self):
        old_client = json.loads(json.dumps(self.base_config))
        new_root = json.loads(json.dumps(self.base_config))
        old_client["installation"]["client_executable"] = (
            "RIL_client_old.exe"
        )
        old_client["installation"]["client_ui_file"] = "RIL_old.ui"
        new_root["installation"]["client_executable"] = (
            "RIL_client_new.exe"
        )
        new_root["installation"]["client_ui_file"] = "RIL_new.ui"

        with tempfile.TemporaryDirectory() as directory:
            install_dir = Path(directory)
            (install_dir / "ril_config.json").write_text(
                json.dumps(new_root),
                encoding="utf-8",
            )
            (install_dir / CLIENT_INSTALLED_CONFIG_FILENAME).write_text(
                json.dumps(old_client),
                encoding="utf-8",
            )

            previous = os.environ.pop(
                CONFIG_ENVIRONMENT_VARIABLE,
                None,
            )
            try:
                selected = select_component_config(
                    "client",
                    application_dir=install_dir,
                )
                config = load_config()
            finally:
                os.environ.pop(CONFIG_ENVIRONMENT_VARIABLE, None)
                if previous is not None:
                    os.environ[CONFIG_ENVIRONMENT_VARIABLE] = previous

        self.assertEqual(
            selected,
            install_dir / CLIENT_INSTALLED_CONFIG_FILENAME,
        )
        self.assertEqual(
            config["installation"]["client_executable"],
            "RIL_client_old.exe",
        )
        self.assertEqual(
            config["installation"]["client_ui_file"],
            "RIL_old.ui",
        )

    def test_client_selects_component_config_before_transitive_imports(self):
        selector = self.client_source.index(
            'select_component_config("client")'
        )
        client_import = self.client_source.index("import client")
        device_import = self.client_source.index("from ril_devices import")
        self.assertLess(selector, client_import)
        self.assertLess(selector, device_import)

    def test_server_selects_component_config_before_transitive_imports(self):
        selector = self.server_source.index(
            'select_component_config("server")'
        )
        network_import = self.server_source.index("import mynetlib")
        ial_import = self.server_source.index("import IAL")
        device_import = self.server_source.index("from ril_devices import")
        self.assertLess(selector, network_import)
        self.assertLess(selector, ial_import)
        self.assertLess(selector, device_import)

    def test_existing_install_without_component_config_uses_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            install_dir = Path(directory)
            previous = os.environ.pop(
                CONFIG_ENVIRONMENT_VARIABLE,
                None,
            )
            try:
                selected = select_component_config(
                    "server",
                    application_dir=install_dir,
                )
                configured = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)
            finally:
                os.environ.pop(CONFIG_ENVIRONMENT_VARIABLE, None)
                if previous is not None:
                    os.environ[CONFIG_ENVIRONMENT_VARIABLE] = previous

        self.assertIsNone(selected)
        self.assertIsNone(configured)

    @unittest.skipUnless(
        os.name == "nt",
        "Windows PowerShell bootstrap probe",
    )
    def test_client_first_root_overwrite_keeps_old_server_bootstrap(self):
        old_server = json.loads(json.dumps(self.base_config))
        new_root = json.loads(json.dumps(self.base_config))
        old_server["installation"][
            "server_restarter_power_shell_script"
        ] = "probe-restarter.ps1"
        new_root["installation"][
            "server_restarter_power_shell_script"
        ] = "missing-new-restarter.ps1"

        with tempfile.TemporaryDirectory() as directory:
            install_dir = Path(directory)
            shared_path = install_dir / "ril_config.json"
            server_path = install_dir / SERVER_INSTALLED_CONFIG_FILENAME
            output_path = install_dir / "probe-output.json"
            shared_path.write_text(
                json.dumps(new_root),
                encoding="utf-8",
            )
            server_path.write_text(
                json.dumps(old_server),
                encoding="utf-8",
            )
            (install_dir / "probe-restarter.ps1").write_text(
                "param([string]$InstallDir,[string]$ConfigPath)\n"
                "[ordered]@{\n"
                "  config_path = $ConfigPath\n"
                "  environment_path = $env:RIL_CONFIG_PATH\n"
                "} | ConvertTo-Json | Set-Content -LiteralPath "
                f"'{output_path}' -Encoding UTF8\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(
                        REPO_ROOT
                        / "dist"
                        / "make_setup"
                        / "RIL_server_start.ps1"
                    ),
                    "-InstallDir",
                    str(install_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr,
            )
            probe = json.loads(
                output_path.read_text(encoding="utf-8-sig")
            )

        self.assertEqual(
            Path(probe["config_path"]),
            server_path,
        )
        self.assertEqual(
            Path(probe["environment_path"]),
            server_path,
        )

    def test_client_installer_owns_transactional_client_config_only(self):
        self.assertIn(
            f'!define CLIENT_INSTALLED_CONFIG_FILENAME '
            f'"{CLIENT_INSTALLED_CONFIG_FILENAME}"',
            self.client_installer,
        )
        self.assertIn(
            "${CLIENT_INSTALLED_CONFIG_FILENAME}",
            self.client_installer,
        )
        self.assertIn(
            'Rename "$0\\${CLIENT_INSTALLED_CONFIG_FILENAME}" '
            '"$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\'
            '${CLIENT_INSTALLED_CONFIG_FILENAME}"',
            self.client_installer,
        )
        self.assertIn(
            'Rename "$0\\${CLIENT_UPDATE_STAGE_DIRECTORY}\\'
            '${CLIENT_INSTALLED_CONFIG_FILENAME}" '
            '"$0\\${CLIENT_INSTALLED_CONFIG_FILENAME}"',
            self.client_installer,
        )
        self.assertIn(
            '-InstalledConfigPath "$InstalledClientConfigPath"',
            self.client_installer,
        )
        self.assertIn(
            'StrCpy $InstalledClientConfigPath '
            '"$0\\${CLIENT_INSTALLED_CONFIG_FILENAME}"',
            self.client_installer,
        )
        self.assertIn(
            'StrCpy $InstalledClientConfigPath "$0\\${CONFIG_FILE}"',
            self.client_installer,
        )
        self.assertNotIn(
            SERVER_INSTALLED_CONFIG_FILENAME,
            self.client_installer,
        )

    def test_client_first_update_preserves_shared_root_for_legacy_server(self):
        self.assertNotIn(
            'Rename "$0\\${CONFIG_FILE}" '
            '"$0\\${CLIENT_UPDATE_BACKUP_DIRECTORY}\\${CONFIG_FILE}"',
            self.client_installer,
        )
        self.assertNotIn(
            'Rename "$0\\${CLIENT_UPDATE_STAGE_DIRECTORY}\\${CONFIG_FILE}" '
            '"$0\\${CONFIG_FILE}"',
            self.client_installer,
        )
        self.assertNotIn(
            'File "${CONFIG_FILE}"',
            self.source_between(
                self.client_installer,
                "client_recovery_done:",
                "client_recovery_task_ready:",
            ),
        )

    def test_server_first_update_preserves_shared_root_for_legacy_client(self):
        manual_names = self.source_between(
            self.server_transaction,
            "function Get-ServerFileNames",
            "function Get-ServerRuntimeName",
        )
        helper_names = self.source_between(
            self.server_helper,
            "function Get-ManagedServerFileNames",
            "function Get-ManagedRuntimeNames",
        )
        restarter_names = self.source_between(
            self.server_restarter,
            "function Get-ManagedServerFileNames",
            "function Get-ManagedRuntimeNames",
        )
        for managed_names in (
            manual_names,
            helper_names,
            restarter_names,
        ):
            with self.subTest(managed_names=managed_names[:40]):
                self.assertIn(
                    "$serverInstalledConfigName",
                    managed_names,
                )
                self.assertNotIn('"ril_config.json"', managed_names)

    def test_server_installer_owns_and_prefers_server_config_only(self):
        self.assertIn(
            f'File /oname={SERVER_INSTALLED_CONFIG_FILENAME} '
            '"${CONFIG_FILE}"',
            self.server_installer,
        )
        for source in (
            self.server_transaction,
            self.server_helper,
            self.server_restarter,
            self.server_start,
        ):
            with self.subTest(source=source[:30]):
                self.assertIn(
                    SERVER_INSTALLED_CONFIG_FILENAME,
                    source,
                )
                self.assertNotIn(
                    CLIENT_INSTALLED_CONFIG_FILENAME,
                    source,
                )

        selector = self.source_between(
            self.server_transaction,
            "function Get-InstalledServerConfigPath",
            "function Test-SamePath",
        )
        self.assertIn(
            f'$serverInstalledConfigName = '
            f'"{SERVER_INSTALLED_CONFIG_FILENAME}"',
            self.server_transaction,
        )
        preferred = selector.index("$serverInstalledConfigName")
        shared_fallback = selector.index(
            '"ril_config.json"',
            preferred,
        )
        self.assertLess(preferred, shared_fallback)


if __name__ == "__main__":
    unittest.main()
