from pathlib import Path
import unittest

import IAL
import RIL_client
import RIL_server
from ril_config import load_config, resource_path


class RuntimePathTests(unittest.TestCase):
    def test_client_ui_is_resolved_from_application_directory(self):
        config = load_config()
        ui_path = resource_path(
            config["installation"]["client_ui_file"]
        )
        self.assertTrue(ui_path.is_file())

    def test_server_icon_is_resolved_from_application_directory(self):
        config = load_config()
        icon_path = resource_path(
            config["installation"]["icon_file"]
        )
        self.assertTrue(icon_path.is_file())

    def test_server_start_script_uses_its_own_directory(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = (
            repo_root
            / "dist"
            / "make_setup"
            / "RIL_server_start.bat"
        ).read_text(encoding="utf-8")
        power_shell_script = (
            repo_root
            / "dist"
            / "make_setup"
            / "RIL_server_start.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('"%~dp0RIL_server_start.ps1"', script)
        self.assertIn("ril_config.json", power_shell_script)
        self.assertIn(
            "config.installation.server_restarter_power_shell_script",
            power_shell_script,
        )

    def test_interface_paths_do_not_depend_on_8dot3_aliases(self):
        paths = [
            IAL.INTF,
            IAL.INTFOS1,
            IAL.INTFOS2,
            IAL.INTFCA1,
            IAL.INTFCA2,
            IAL.INTP,
            IAL.INTPOS1,
            IAL.INTPOS2,
            IAL.INTPCA1,
            IAL.INTPCA2,
            *(
                path
                for config in IAL.AU_CONFIG.values()
                for path in config.values()
            ),
        ]

        self.assertFalse(
            any("PROGRA~" in path.upper() for path in paths)
        )

    def test_installers_use_the_long_program_files_variable(self):
        repo_root = Path(__file__).resolve().parents[1]
        config = load_config()
        self.assertEqual(
            config["installation"]["nsis_install_dir"],
            "$PROGRAMFILES64\\RIL",
        )
        build_script = (
            repo_root / "scripts" / "build_release.ps1"
        ).read_text(encoding="utf-8")
        for filename in ("RIL_Client_Update.nsi", "RIL_Server_Setup.nsi"):
            installer = (repo_root / filename).read_text(encoding="utf-8")
            with self.subTest(installer=filename):
                self.assertIn("${INSTALL_DIR}", installer)
                self.assertNotIn("PROGRA~", installer.upper())
        self.assertIn(
            "/DINSTALL_DIR=$($installation.nsis_install_dir)",
            build_script,
        )


if __name__ == "__main__":
    unittest.main()
