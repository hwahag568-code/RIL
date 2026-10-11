import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import ril_update


REPO_ROOT = Path(__file__).resolve().parents[1]
MAKE_SETUP = REPO_ROOT / "dist" / "make_setup"
SERVER_SCRIPTS = {
    "manual_install": MAKE_SETUP / "RIL_server_manual_install.ps1",
    "update_helper": MAKE_SETUP / "RIL_server_update_helper.ps1",
    "restarter": MAKE_SETUP / "RIL_server_restarter.ps1",
}
EXTRACTED_FUNCTIONS = (
    "ConvertTo-ArgumentText",
    "Invoke-SchTasks",
    "ConvertTo-ProcessRecord",
    "Get-ProcessSnapshot",
    "Get-ProcessById",
    "New-ServerTasks",
    "Set-StagedRecoveryTasks",
)
INSTALL_DIRECTORY = "C:\\Program Files\\RIL"
PROBE_CODE = "import sys, json; print(json.dumps(sys.argv[1:]))"


def powershell_hosts():
    return (
        ("powershell.exe", shutil.which("powershell.exe")),
        ("pwsh", shutil.which("pwsh")),
    )


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def load_config():
    return json.loads(
        (REPO_ROOT / "ril_config.json").read_text(encoding="utf-8")
    )


def build_harness(script_name, body, strict=False):
    names = ", ".join(quote(name) for name in EXTRACTED_FUNCTIONS)
    strict_line = "Set-StrictMode -Version 2.0" if strict else ""
    return f"""$ErrorActionPreference = "Stop"
{strict_line}
$processQueryTimeoutSeconds = 15
$script:processQueryFallbackActive = $false
$schTasksExecutable = {quote(sys.executable)}
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {quote(SERVER_SCRIPTS[script_name])}, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {{
    throw "parse failed"
}}
$wanted = @({names})
$definitions = $ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $wanted -contains $node.Name
}}, $true)
foreach ($definition in $definitions) {{
    Invoke-Expression $definition.Extent.Text
}}
Set-Item -Path Function:Invoke-SchTasksReal `
    -Value (Get-Item Function:Invoke-SchTasks).ScriptBlock
$script:recorded = New-Object System.Collections.Generic.List[string]
function Invoke-SchTasks {{
    param([Parameter(Mandatory = $true)][string]$ArgumentLine)
    $probe = {quote(PROBE_CODE)}
    $result = Invoke-SchTasksReal -ArgumentLine (
        "-c " + (ConvertTo-ArgumentText $probe) + " " + $ArgumentLine
    )
    $script:recorded.Add([string]$result.output)
    return [PSCustomObject]@{{ exit_code = 0; output = $result.output }}
}}
$config = Get-Content -LiteralPath {quote(REPO_ROOT / "ril_config.json")} `
    -Raw -Encoding UTF8 | ConvertFrom-Json
{body}
"""


def run_harness(executable, script_name, body, strict=False):
    with tempfile.TemporaryDirectory() as temporary:
        harness_path = Path(temporary) / "harness.ps1"
        harness_path.write_text(
            build_harness(script_name, body, strict),
            encoding="utf-8-sig",
        )
        completed = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(harness_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    if completed.returncode != 0:
        raise AssertionError(
            f"{executable} failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    for line in completed.stdout.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError(f"no RESULT line:\n{completed.stdout}")


RECORDED_RESULT = (
    'Write-Output ("RESULT:" + '
    "(ConvertTo-Json -InputObject @($script:recorded) -Compress))"
)


def recorded_argv(result):
    return [json.loads(item) for item in result]


class PowerShellHostCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        installation = cls.config["installation"]
        cls.server_task = installation["server_task_name"]
        cls.restarter_task = installation["server_restarter_task_name"]
        cls.hours = str(installation["server_restarter_interval_hours"])
        cls.start_script = (
            INSTALL_DIRECTORY
            + "\\"
            + installation["server_start_script"]
        )
        cls.restarter_script = (
            INSTALL_DIRECTORY
            + "\\"
            + installation["server_restarter_script"]
        )

    def available_hosts(self):
        if shutil.which("powershell.exe") is None:
            self.skipTest("powershell.exe is not available")
        # pwsh가 없는 PC에서는 Windows PowerShell 5.1만 검사한다.
        return [
            executable
            for _, executable in powershell_hosts()
            if executable is not None
        ]

    def expected_task_argv(self, start_value, restarter_value):
        return [
            [
                "/Create", "/SC", "ONLOGON", "/TN", self.server_task,
                "/RL", "HIGHEST", "/TR", start_value, "/F",
            ],
            [
                "/Create", "/SC", "HOURLY", "/MO", self.hours,
                "/TN", self.restarter_task, "/RL", "HIGHEST",
                "/TR", restarter_value, "/F",
            ],
        ]

    def test_task_registration_argv_is_host_independent(self):
        expected = self.expected_task_argv(
            f'"{self.start_script}"',
            f'"{self.restarter_script}"',
        )
        manual_body = (
            f"New-ServerTasks -Config $config "
            f"-InstalledPath {quote(INSTALL_DIRECTORY)}\n"
            + RECORDED_RESULT
        )
        # 설치 경로 끝의 백슬래시가 있어도 같은 결과가 나와야 한다.
        restarter_body = (
            f"$installPath = {quote(INSTALL_DIRECTORY + chr(92))}\n"
            "New-ServerTasks -Config $config\n"
            + RECORDED_RESULT
        )
        helper_default_body = (
            f"$installPath = {quote(INSTALL_DIRECTORY)}\n"
            "$previousConfig = $config\n"
            "New-ServerTasks\n"
            + RECORDED_RESULT
        )
        for executable in self.available_hosts():
            for script_name, body, strict in (
                ("manual_install", manual_body, True),
                ("restarter", restarter_body, False),
                ("update_helper", helper_default_body, False),
            ):
                with self.subTest(script=script_name, host=executable):
                    result = run_harness(
                        executable, script_name, body, strict
                    )
                    self.assertEqual(recorded_argv(result), expected)

    def test_staged_recovery_task_keeps_command_as_one_argument(self):
        installation = self.config["installation"]
        stage = ril_update.server_update_destination(
            self.config, "260730.99"
        ).parent
        restarter = stage / installation[
            "server_restarter_power_shell_script"
        ]
        config_path = stage / installation["server_effective_config_filename"]
        powershell = self.config["update"]["server"]["power_shell_executable"]
        command = (
            f'"{powershell}" -NoProfile -NonInteractive '
            f'-ExecutionPolicy Bypass -File "{restarter}" '
            f'-InstallDir "{INSTALL_DIRECTORY}" '
            f'-ConfigPath "{config_path}"'
        )
        # 실제 staging 경로는 261자 제한에 거의 맞닿아 있어 임시 폴더를
        # 쓸 수 없으므로, 파일 존재 확인만 가로채 실제 경로로 검사한다.
        body = (
            "function Test-Path { $true }\n"
            f"$installPath = {quote(INSTALL_DIRECTORY)}\n"
            f"$stageDirectory = {quote(stage)}\n"
            f"$ConfigPath = {quote(config_path)}\n"
            "Set-StagedRecoveryTasks\n"
            + RECORDED_RESULT
        )
        expected = self.expected_task_argv(command, command)
        for executable in self.available_hosts():
            with self.subTest(host=executable):
                result = run_harness(executable, "update_helper", body)
                self.assertEqual(recorded_argv(result), expected)

    def test_argument_text_escapes_backslashes_and_quotes(self):
        values = ("C:\\Program Files\\RIL\\", 'a "b" c\\')
        body = (
            "$line = "
            + " + ' ' + ".join(
                f"(ConvertTo-ArgumentText {quote(value)})"
                for value in values
            )
            + "\n"
            "$result = Invoke-SchTasksReal -ArgumentLine (\n"
            '    "-c " + (ConvertTo-ArgumentText '
            f"{quote(PROBE_CODE)}) + \" \" + $line\n"
            ")\n"
            'Write-Output ("RESULT:" + (ConvertTo-Json -InputObject '
            "@([string]$result.output) -Compress))"
        )
        for executable in self.available_hosts():
            for script_name in SERVER_SCRIPTS:
                with self.subTest(script=script_name, host=executable):
                    result = run_harness(executable, script_name, body)
                    self.assertEqual(
                        recorded_argv(result), [list(values)]
                    )

    def test_staged_recovery_command_fits_schtasks_limit(self):
        installation = self.config["installation"]
        destination = ril_update.server_update_destination(
            self.config, "260730.99"
        )
        stage = destination.parent
        command = (
            f'"{self.config["update"]["server"]["power_shell_executable"]}" '
            "-NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f'-File "{stage}\\'
            f'{installation["server_restarter_power_shell_script"]}" '
            f'-InstallDir "{INSTALL_DIRECTORY}" '
            f'-ConfigPath "{stage}\\'
            f'{installation["server_effective_config_filename"]}"'
        )
        self.assertLessEqual(len(command), 261)
        helper = SERVER_SCRIPTS["update_helper"].read_text(encoding="utf-8")
        self.assertIn("$recoveryCommand.Length -gt 261", helper)
        self.assertIn(
            "정전 복구 작업 명령이 schtasks /TR 제한(261자)을 넘습니다.",
            helper,
        )

    def test_process_query_uses_cim_with_timeout_and_falls_back(self):
        query = (
            "$self = @(Get-ProcessSnapshot | "
            "Where-Object { [int]$_.ProcessId -eq $PID })\n"
            "$timer = [Diagnostics.Stopwatch]::StartNew()\n"
            "$byId = Get-ProcessById -ProcessId $PID\n"
            "$zero = Get-ProcessById -ProcessId 0\n"
            "$byIdAgain = Get-ProcessById -ProcessId $PID\n"
            "$timer.Stop()\n"
            "$summary = [ordered]@{\n"
            "    snapshot_count = $self.Count\n"
            "    snapshot_path = if ($self.Count) "
            "{ [string]$self[0].ExecutablePath } else { '' }\n"
            "    snapshot_parent_null = if ($self.Count) "
            "{ $null -eq $self[0].ParentProcessId } else { $null }\n"
            "    by_id_path = [string]$byId.ExecutablePath\n"
            "    by_id_parent_null = ($null -eq $byId.ParentProcessId)\n"
            "    by_id_again_path = [string]$byIdAgain.ExecutablePath\n"
            "    zero_is_null = ($null -eq $zero)\n"
            "    fallback_active = [bool]$script:processQueryFallbackActive\n"
            "    elapsed_seconds = $timer.Elapsed.TotalSeconds\n"
            "}\n"
            'Write-Output ("RESULT:" + '
            "(ConvertTo-Json -InputObject $summary -Compress))"
        )
        fallback_body = (
            'function Get-CimInstance { throw "WMI unavailable" }\n'
            + query
        )
        for executable in self.available_hosts():
            for script_name in SERVER_SCRIPTS:
                with self.subTest(
                    script=script_name, host=executable, mode="cim"
                ):
                    summary = run_harness(executable, script_name, query)
                    self.assertEqual(summary["snapshot_count"], 1)
                    self.assertFalse(summary["fallback_active"])
                    self.assertFalse(summary["snapshot_parent_null"])
                    self.assertFalse(summary["by_id_parent_null"])
                    self.assertTrue(summary["zero_is_null"])
                    for key in (
                        "snapshot_path",
                        "by_id_path",
                        "by_id_again_path",
                    ):
                        self.assertRegex(
                            summary[key].lower(),
                            r"(powershell|pwsh)\.exe$",
                        )
                with self.subTest(
                    script=script_name, host=executable, mode="fallback"
                ):
                    summary = run_harness(
                        executable, script_name, fallback_body
                    )
                    self.assertEqual(summary["snapshot_count"], 1)
                    self.assertTrue(summary["fallback_active"])
                    self.assertTrue(summary["snapshot_parent_null"])
                    self.assertTrue(summary["by_id_parent_null"])
                    self.assertTrue(summary["zero_is_null"])
                    for key in (
                        "snapshot_path",
                        "by_id_path",
                        "by_id_again_path",
                    ):
                        self.assertRegex(
                            summary[key].lower(),
                            r"(powershell|pwsh)\.exe$",
                        )
                    self.assertLess(summary["elapsed_seconds"], 5)

    def test_server_scripts_use_shared_native_and_process_helpers(self):
        for name, path in SERVER_SCRIPTS.items():
            text = path.read_text(encoding="utf-8-sig")
            with self.subTest(script=name):
                self.assertEqual(
                    text.count("Get-CimInstance"),
                    text.count(
                        "-OperationTimeoutSec $processQueryTimeoutSeconds"
                    ),
                )
                lines = text.splitlines()
                for index, line in enumerate(lines):
                    if "Get-CimInstance" in line:
                        window = "\n".join(lines[index:index + 3])
                        self.assertNotIn("SilentlyContinue", window)
                self.assertNotIn('/TR "`"', text)
                self.assertNotIn("/TR $recoveryCommand", text)
                self.assertIn("Invoke-SchTasks -ArgumentLine", text)
                self.assertNotIn(
                    '& "$env:SystemRoot\\System32\\schtasks.exe" /Create',
                    text,
                )
        for name, marker in (
            ("manual_install", "$null -ne $worker.ParentProcessId -and"),
            ("update_helper", "$null -ne $worker.ParentProcessId -and"),
            ("restarter", "$null -eq $worker.ParentProcessId -or"),
        ):
            with self.subTest(script=name, marker=marker):
                self.assertIn(
                    marker,
                    SERVER_SCRIPTS[name].read_text(encoding="utf-8-sig"),
                )

    def test_client_recovery_task_quotes_command_path(self):
        installer = (REPO_ROOT / "RIL_Client_Update.nsi").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn(
            '/TR "\\"$0\\${CLIENT_UPDATE_RECOVERY_DIRECTORY}'
            '\\${CLIENT_UPDATE_RECOVERY_COMMAND}\\"" /F',
            installer,
        )


if __name__ == "__main__":
    unittest.main()
