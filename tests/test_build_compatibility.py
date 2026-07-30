import json
from pathlib import Path
import unittest

from scripts import release_validation


class BuildCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.config = json.loads(
            (cls.repo_root / "ril_config.json").read_text(
                encoding="utf-8-sig",
            )
        )
        cls.build_script = (
            cls.repo_root / "scripts" / "build_release.ps1"
        ).read_text(encoding="utf-8")

    def test_supported_build_runtime_is_explicit(self):
        self.assertEqual(
            self.config["build"],
            {
                "platform": "win32",
                "machine": "AMD64",
                "python_major_minor": "3.13",
                "architecture_bits": 64,
            },
        )

    def test_build_script_rejects_a_different_runtime(self):
        self.assertIn("$config.build", self.build_script)
        self.assertIn("sys.platform", self.build_script)
        self.assertIn("platform.machine()", self.build_script)
        self.assertIn("sys.version_info.major", self.build_script)
        self.assertIn("struct.calcsize('P') * 8", self.build_script)
        self.assertIn("Unsupported build runtime", self.build_script)

    def test_build_provenance_records_runtime_and_pyinstaller(self):
        environment = release_validation.current_build_environment()
        self.assertEqual(environment["platform"], "win32")
        self.assertEqual(environment["architecture_bits"], 64)
        self.assertTrue(
            environment["python_version"].startswith("3.13."),
        )
        self.assertEqual(environment["pyinstaller_version"], "6.20.0")
        self.assertIs(
            release_validation.validate_build_environment(
                self.config,
                environment,
            ),
            environment,
        )

    def test_build_provenance_rejects_wrong_architecture(self):
        environment = release_validation.current_build_environment()
        environment["architecture_bits"] = 32
        with self.assertRaisesRegex(RuntimeError, "지원 환경"):
            release_validation.validate_build_environment(
                self.config,
                environment,
            )


if __name__ == "__main__":
    unittest.main()
