from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

import ril_build_version
import ril_config
import ril_devices
import ril_version


REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.base = ril_config.load_base_config(
            REPO_ROOT / "ril_config.json"
        )

    def test_one_release_version_drives_legacy_marker(self):
        self.assertEqual(
            ril_version.VERSION,
            self.base["release"]["version"],
        )
        self.assertEqual(
            ril_build_version.VERSION,
            self.base["release"]["version"],
        )
        self.assertEqual(
            ril_build_version.PROTOCOL_VERSION,
            self.base["release"]["protocol_version"],
        )
        self.assertNotIn(
            "legacy_update_version",
            self.base["release"],
        )
        self.assertEqual(
            ril_config.legacy_version_from_version("260728"),
            "26072801",
        )
        self.assertEqual(
            ril_config.legacy_version_from_version("260728.2"),
            "26072803",
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "YYMMDD",
        ):
            ril_config.legacy_version_from_version("260728.0")

    def test_release_repository_branch_and_update_urls_must_match(self):
        mismatched_repository = deepcopy(self.base)
        mismatched_repository["release"]["repository"] = "owner/other"
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "release.repository/branch",
        ):
            ril_config.validate_config(mismatched_repository)

        mismatched_url = deepcopy(self.base)
        mismatched_url["update"]["manifest_url"] = (
            "https://example.invalid/update.json"
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "release.repository/branch",
        ):
            ril_config.validate_config(mismatched_url)

    def test_frozen_version_ignores_external_config_release_values(self):
        with tempfile.TemporaryDirectory() as directory:
            external = deepcopy(self.base)
            external["release"]["version"] = "999999.99"
            external["release"]["protocol_version"] = 999
            config_path = Path(directory) / "ril_config.json"
            config_path.write_text(
                json.dumps(external, ensure_ascii=False),
                encoding="utf-8",
            )

            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            ril_config.CONFIG_ENVIRONMENT_VARIABLE:
                                str(config_path)
                        },
                    ),
                    mock.patch.object(
                        sys,
                        "frozen",
                        True,
                        create=True,
                    ),
                ):
                    importlib.reload(ril_version)
                    self.assertEqual(
                        ril_version.VERSION,
                        ril_build_version.VERSION,
                    )
                    self.assertEqual(
                        ril_version.PROTOCOL_VERSION,
                        ril_build_version.PROTOCOL_VERSION,
                    )
                    self.assertNotEqual(
                        ril_version.VERSION,
                        external["release"]["version"],
                    )
            finally:
                importlib.reload(ril_version)

    def test_device_names_addresses_and_groups_come_from_json(self):
        definitions = self.base["devices"]["definitions"]
        self.assertEqual(
            ril_devices.DEVICE_DISPLAY_NAMES["AU3"],
            definitions["AU3"]["display_name"],
        )
        self.assertEqual(
            ril_devices.DEVICE_IPS["Al1"],
            definitions["Al1"]["ip"],
        )
        self.assertEqual(
            ril_devices.devices_in_group("AU"),
            ("AU1", "AU2", "AU3"),
        )

    def test_server_busy_result_code_comes_from_json(self):
        self.assertEqual(
            self.base["protocol"]["busy_result_code"],
            "server_busy",
        )

    def test_local_config_deeply_overrides_known_operational_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_path = root / "ril_config.json"
            local_path = root / "ril_config.local.json"
            base_path.write_text(
                json.dumps(self.base, ensure_ascii=False),
                encoding="utf-8",
            )
            local_path.write_text(
                json.dumps(
                    {
                        "interfaces": {
                            "au": {
                                "3": {
                                    "order_directory": "D:\\AU_3"
                                }
                            }
                        },
                        "devices": {
                            "definitions": {
                                "AU3": {
                                    "ip": "10.2.151.220",
                                    "display_name": "AU 3 예비",
                                }
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            merged = ril_config.load_config(base_path, local_path)

        self.assertEqual(
            merged["interfaces"]["au"]["3"]["order_directory"],
            "D:\\AU_3",
        )
        self.assertEqual(
            merged["interfaces"]["au"]["3"]["result_directory"],
            self.base["interfaces"]["au"]["3"]["result_directory"],
        )
        self.assertEqual(
            merged["devices"]["definitions"]["AU3"]["ip"],
            "10.2.151.220",
        )

    def test_local_config_rejects_version_protocol_and_bootstrap_changes(self):
        for protected_section in (
            "build",
            "release",
            "protocol",
            "installation",
        ):
            with self.subTest(section=protected_section):
                with tempfile.TemporaryDirectory() as directory:
                    local_path = Path(directory) / "local.json"
                    local_path.write_text(
                        json.dumps(
                            {
                                protected_section: self.base[
                                    protected_section
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ril_config.ConfigError):
                        ril_config.load_config(
                            REPO_ROOT / "ril_config.json",
                            local_path,
                        )

    def test_unknown_local_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "local.json"
            local_path.write_text(
                '{"network": {"unknown_timeout": 1}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ril_config.ConfigError,
                "unknown_timeout",
            ):
                ril_config.load_config(
                    REPO_ROOT / "ril_config.json",
                    local_path,
                )

    def test_invalid_runtime_path_shape_is_rejected_before_import(self):
        invalid = deepcopy(self.base)
        invalid["interfaces"]["general"][
            "configured_executable_paths"
        ] = []

        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "configured_executable_paths",
        ):
            ril_config.validate_config(invalid)

    def test_integer_only_operational_values_reject_floats(self):
        paths = (
            ("network", "connect_attempts"),
            ("network", "max_message_size_bytes"),
            ("network", "server_backlog"),
        )
        for section, key in paths:
            with self.subTest(path=f"{section}.{key}"):
                invalid = deepcopy(self.base)
                invalid[section][key] = 1.5
                with self.assertRaises(ril_config.ConfigError):
                    ril_config.validate_config(invalid)

    def test_runtime_directories_are_distinct_safe_folder_names(self):
        runtime_keys = (
            "client_runtime_directory",
            "server_runtime_directory",
            "legacy_runtime_directory",
        )
        values = [
            self.base["installation"][key]
            for key in runtime_keys
        ]
        self.assertEqual(len(values), len(set(values)))

        invalid = deepcopy(self.base)
        invalid["installation"]["client_runtime_directory"] = ".."
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "단일 폴더명",
        ):
            ril_config.validate_config(invalid)

        invalid = deepcopy(self.base)
        invalid["installation"]["client_runtime_directory"] = (
            invalid["installation"]["server_runtime_directory"]
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "서로 달라야",
        ):
            ril_config.validate_config(invalid)

        invalid = deepcopy(self.base)
        invalid["installation"]["client_runtime_directory"] = (
            invalid["installation"]["server_runtime_directory"].upper()
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "대소문자를 구분하지 않고",
        ):
            ril_config.validate_config(invalid)

    def test_installation_bootstrap_names_are_safe_for_migration(self):
        leaf_keys = (
            "client_executable",
            "server_executable",
            "client_ui_file",
            "server_start_script",
            "server_start_power_shell_script",
            "server_restarter_script",
            "server_restarter_power_shell_script",
            "server_update_helper_script",
            "client_startup_ready_filename",
            "shortcut_directory",
            "server_start_menu_shortcut",
            "server_desktop_shortcut",
        )
        for key in leaf_keys:
            with self.subTest(key=key):
                invalid = deepcopy(self.base)
                invalid["installation"][key] = r"..\outside.exe"
                with self.assertRaisesRegex(
                    ril_config.ConfigError,
                    "단일 파일",
                ):
                    ril_config.validate_config(invalid)

        for key in (
            "server_task_name",
            "server_restarter_task_name",
            "client_update_recovery_task_name",
            "client_install_registry_value",
            "server_install_registry_value",
            "server_version_registry_value",
        ):
            with self.subTest(key=key):
                invalid = deepcopy(self.base)
                invalid["installation"][key] = "bad\r\nname"
                with self.assertRaisesRegex(
                    ril_config.ConfigError,
                    "제어 문자",
                ):
                    ril_config.validate_config(invalid)

        invalid = deepcopy(self.base)
        invalid["installation"]["server_task_name"] = 'bad"name'
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "큰따옴표",
        ):
            ril_config.validate_config(invalid)

        invalid = deepcopy(self.base)
        invalid["installation"]["server_restarter_task_name"] = (
            invalid["installation"]["server_task_name"].upper()
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "예약 작업 이름",
        ):
            ril_config.validate_config(invalid)

        for key in ("alignment_attempts", "responsive_check_timeout_ms"):
            with self.subTest(path=f"interfaces.automation.{key}"):
                invalid = deepcopy(self.base)
                invalid["interfaces"]["automation"][key] = 1.5
                with self.assertRaises(ril_config.ConfigError):
                    ril_config.validate_config(invalid)

    def test_device_ips_must_be_valid_ipv4_addresses(self):
        invalid = deepcopy(self.base)
        invalid["devices"]["definitions"]["AU3"]["ip"] = (
            "10.2.151.999"
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "IPv4",
        ):
            ril_config.validate_config(invalid)

    def test_installation_state_paths_must_be_relative_and_safe(self):
        for value in (
            ".",
            "\\",
            r"C:\RIL\state",
            r"..\outside",
            r"state\..\outside",
            r"state\bad:name",
            "state\\trailing. ",
        ):
            with self.subTest(value=value):
                invalid = deepcopy(self.base)
                invalid["installation"][
                    "server_manual_transaction_relative_directory"
                ] = value
                with self.assertRaisesRegex(
                    ril_config.ConfigError,
                    "안전한 상대 경로",
                ):
                    ril_config.validate_config(invalid)

    def test_au_commands_and_profiles_must_be_unambiguous(self):
        duplicate_number = deepcopy(self.base)
        duplicate_number["protocol"]["current_au_commands"]["AU2"] = 1
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "AU 장비 번호가 중복",
        ):
            ril_config.validate_config(duplicate_number)

        overlapping_command = deepcopy(self.base)
        overlapping_command["protocol"]["legacy_au_commands"]["AU1"] = 1
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "현재/구형 AU 명령",
        ):
            ril_config.validate_config(overlapping_command)

        mismatched_alias = deepcopy(self.base)
        mismatched_alias["protocol"]["legacy_client_commands"]["AU1"] = (
            "AU32"
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "같은 장비 번호",
        ):
            ril_config.validate_config(mismatched_alias)

        extra_profile = deepcopy(self.base)
        extra_profile["interfaces"]["au"]["99"] = deepcopy(
            extra_profile["interfaces"]["au"]["3"]
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "AU 실행 경로 설정과 명령 번호",
        ):
            ril_config.validate_config(extra_profile)

    def test_login_and_update_timeouts_preserve_completion_order(self):
        too_short_client = deepcopy(self.base)
        too_short_client["network"]["login_response_timeout_seconds"] = (
            too_short_client["server"][
                "login_execution_timeout_seconds"
            ]
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "login_response_timeout_seconds",
        ):
            ril_config.validate_config(too_short_client)

        too_short_legacy = deepcopy(self.base)
        too_short_legacy["network"]["legacy_total_timeout_seconds"] = (
            too_short_legacy["server"][
                "login_execution_timeout_seconds"
            ]
        )
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "legacy_total_timeout_seconds",
        ):
            ril_config.validate_config(too_short_legacy)

        too_short_drain = deepcopy(self.base)
        too_short_drain["update"]["server"][
            "drain_timeout_seconds"
        ] = too_short_drain["server"][
            "login_execution_timeout_seconds"
        ]
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "drain_timeout_seconds",
        ):
            ril_config.validate_config(too_short_drain)

    def test_shared_update_mutex_is_required(self):
        self.assertTrue(
            self.base["installation"]["update_mutex_name"].strip()
        )
        invalid = deepcopy(self.base)
        del invalid["installation"]["update_mutex_name"]
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "update_mutex_name",
        ):
            ril_config.validate_config(invalid)

        invalid = deepcopy(self.base)
        invalid["update"]["mutex_wait_seconds"] = 0
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "mutex_wait_seconds",
        ):
            ril_config.validate_config(invalid)

        invalid = deepcopy(self.base)
        invalid["update"]["mutex_wait_seconds"] = invalid["update"][
            "server"
        ]["helper_start_timeout_seconds"]
        with self.assertRaisesRegex(
            ril_config.ConfigError,
            "helper_start_timeout_seconds",
        ):
            ril_config.validate_config(invalid)

    def test_operational_modules_do_not_embed_site_paths_or_ips(self):
        modules = (
            "IAL.py",
            "RIL_client.py",
            "RIL_server.py",
            "client.py",
            "mynetlib.py",
            "ril_devices.py",
            "ril_version.py",
        )
        forbidden = (
            "C:\\Program Files",
            "10.2.151.",
            "raw.githubusercontent.com/hwahag568-code/RIL",
            "github.com/hwahag568-code/RIL",
        )
        for module in modules:
            text = (REPO_ROOT / module).read_text(
                encoding="utf-8-sig"
            )
            for value in forbidden:
                with self.subTest(module=module, value=value):
                    self.assertNotIn(value, text)

    def test_installers_ship_base_config_but_preserve_local_config(self):
        expected_base_config = {
            "RIL_Client_Update.nsi": 'File "${CONFIG_FILE}"',
            "RIL_Server_Setup.nsi": (
                'File /oname=ril_config.json "${CONFIG_FILE}"'
            ),
        }
        for installer_name, config_marker in expected_base_config.items():
            text = (REPO_ROOT / installer_name).read_text(
                encoding="utf-8-sig"
            )
            self.assertIn(config_marker, text)
            self.assertNotIn("ril_config.local.json", text)

        server_transaction = (
            REPO_ROOT
            / "dist"
            / "make_setup"
            / "RIL_server_manual_install.ps1"
        ).read_text(encoding="utf-8-sig")
        self.assertNotIn("ril_config.local.json", server_transaction)

    def test_windows_powershell_reads_json_as_utf8(self):
        scripts = (
            "scripts/build_release.ps1",
            "scripts/publish_release.ps1",
            "dist/make_setup/RIL_install_prepare.ps1",
            "dist/make_setup/RIL_server_start.ps1",
            "dist/make_setup/RIL_server_restarter.ps1",
            "dist/make_setup/RIL_server_update_helper.ps1",
            "dist/make_setup/RIL_server_manual_install.ps1",
        )
        for relative_path in scripts:
            with self.subTest(script=relative_path):
                text = (REPO_ROOT / relative_path).read_text(
                    encoding="utf-8-sig"
                )
                json_reads = re.findall(
                    r"Get-Content(?:(?!Get-Content).)*?ConvertFrom-Json",
                    text,
                    flags=re.DOTALL,
                )
                self.assertTrue(json_reads)
                for command in json_reads:
                    self.assertIn("-Encoding UTF8", command)

    def test_github_workflow_resolves_artifact_names_from_json(self):
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "server_installer_filename_template",
            workflow,
        )
        self.assertIn(
            "client_installer_filename",
            workflow,
        )
        self.assertNotIn("release/Update_RIL.exe", workflow)
        self.assertNotIn("release/RIL_Server_Setup_*.exe", workflow)


if __name__ == "__main__":
    unittest.main()
