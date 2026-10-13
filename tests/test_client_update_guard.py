import ctypes
import json
from pathlib import Path
import unittest
import subprocess
import sys
import uuid
from unittest import mock

from ril_client_update_guard import PendingUpdate, pending_update, show_update_notice, update_in_progress


class ClientUpdateGuardTests(unittest.TestCase):
    def test_pending_gate_blocks_duplicate_before_installer_starts(self):
        name = "Local\\RIL_pending_test_" + uuid.uuid4().hex
        self.assertFalse(pending_update(name))
        gate = PendingUpdate(name)
        try:
            self.assertTrue(pending_update(name))
            with self.assertRaisesRegex(RuntimeError, "이미 업데이트"):
                PendingUpdate(name)
            self.assertTrue(pending_update(name))
        finally:
            gate.close()
        self.assertFalse(pending_update(name))

    def test_handoff_keeps_gate_alive_until_helper_exit(self):
        name = "Local\\RIL_pending_test_" + uuid.uuid4().hex
        gate = PendingUpdate(name)
        child = gate.handoff(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.assertIsNone(gate.handle)
            self.assertTrue(pending_update(name))
            with self.assertRaises(RuntimeError):
                PendingUpdate(name)
        finally:
            child.communicate(timeout=10)
        self.assertFalse(pending_update(name))

    def test_failed_handoff_keeps_gate_until_caller_cleans_up(self):
        name = "Local\\RIL_pending_test_" + uuid.uuid4().hex
        gate = PendingUpdate(name)
        try:
            with mock.patch("ril_client_update_guard.subprocess.Popen", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    gate.handoff(["unused"])
            self.assertTrue(pending_update(name))
        finally:
            gate.close()
        self.assertFalse(pending_update(name))

    def test_helper_crash_releases_pending_gate(self):
        name = "Local\\RIL_pending_test_" + uuid.uuid4().hex
        gate = PendingUpdate(name)
        child = gate.handoff(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(pending_update(name))
        finally:
            child.terminate()
            child.wait(timeout=10)
        self.assertFalse(pending_update(name))

    def test_real_powershell_handoff_waits_for_installer_exit(self):
        import os
        name = "Local\\RIL_pending_test_" + uuid.uuid4().hex
        gate = PendingUpdate(name)
        child = gate.launch_installer(
            sys.executable, ["-c", "import time; time.sleep(1)"], sys.executable, os.environ.copy(),
        )
        try:
            self.assertTrue(pending_update(name))
            self.assertEqual(child.wait(timeout=20), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            gate.close()
        self.assertFalse(pending_update(name))

    def test_finished_download_releases_gate_only_without_installer_handoff(self):
        import RIL_client
        from types import SimpleNamespace
        for pending in (None, "installer.exe"):
            with self.subTest(pending=pending):
                gate = mock.Mock()
                worker = mock.Mock()
                window = SimpleNamespace(download_worker=worker, _update_gate=gate, _pending_installer_path=pending)
                with mock.patch.object(RIL_client.WindowClass, "_maybe_quit_after_close"):
                    RIL_client.WindowClass._background_worker_finished(window, "download_worker", worker)
                if pending:
                    gate.close.assert_not_called()
                else:
                    gate.close.assert_called_once_with()

    def test_notice_is_detached_from_client_and_install_directory(self):
        with mock.patch("ril_client_update_guard.subprocess.Popen") as launch:
            show_update_notice("설치 중 '대기'")
        args, kwargs = launch.call_args
        self.assertTrue(args[0][0].endswith("powershell.exe"))
        self.assertIn("''대기''", args[0][-1])
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertNotIn("RIL", kwargs["cwd"])

    def test_real_windows_mutex_held_by_another_process(self):
        name = "Local\\RIL_guard_test_" + uuid.uuid4().hex
        script = (
            "import ctypes, sys; "
            "k=ctypes.WinDLL('kernel32',use_last_error=True); "
            "k.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]; "
            "k.CreateMutexW.restype=ctypes.c_void_p; "
            "h=k.CreateMutexW(None,True,sys.argv[1]); "
            "assert h; print('ready',flush=True); sys.stdin.read()"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertTrue(update_in_progress(name))
        finally:
            child.communicate(timeout=10)
        self.assertFalse(update_in_progress(name))

    def test_busy_installer_blocks_client_and_closes_handle(self):
        api = mock.Mock()
        api.OpenMutexW.return_value = 42
        api.WaitForSingleObject.return_value = 258
        self.assertTrue(update_in_progress("test", api))
        api.ReleaseMutex.assert_not_called()
        api.CloseHandle.assert_called_once_with(42)

    def test_unowned_and_abandoned_mutex_allow_start(self):
        for result in (0, 128):
            with self.subTest(result=result):
                api = mock.Mock()
                api.OpenMutexW.return_value = 42
                api.WaitForSingleObject.return_value = result
                self.assertFalse(update_in_progress("test", api))
                api.ReleaseMutex.assert_called_once_with(42)
                api.CloseHandle.assert_called_once_with(42)

    def test_missing_mutex_allows_start(self):
        api = mock.Mock()
        api.OpenMutexW.return_value = 0
        with mock.patch.object(ctypes, "get_last_error", return_value=2):
            self.assertFalse(update_in_progress("test", api))

    def test_access_error_does_not_allow_start(self):
        api = mock.Mock()
        api.OpenMutexW.return_value = 0
        with mock.patch.object(ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError):
                update_in_progress("test", api)

    def test_wait_failure_closes_handle(self):
        api = mock.Mock()
        api.OpenMutexW.return_value = 42
        api.WaitForSingleObject.return_value = 0xFFFFFFFF
        with self.assertRaises(OSError):
            update_in_progress("test", api)
        api.CloseHandle.assert_called_once_with(42)

    def test_guard_runs_before_ui_import_and_health_probe_is_exempt(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "RIL_client.py").read_text(encoding="utf-8")
        self.assertLess(source.index("busy = update_in_progress"), source.index("from PyQt5"))
        self.assertIn('if "--ril-startup-ready-file" not in sys.argv:', source)
        installer = (root / "RIL_Client_Update.nsi").read_text(encoding="utf-8")
        self.assertIn('ril-client-update.log', installer)
        self.assertIn('StrCpy $UpdatePhase "rolled_back"', installer)

    def test_automatic_install_remains_silent_without_progress_override(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "ril_config.json").read_text(encoding="utf-8"))
        self.assertIn("/S", config["update"]["client"]["installer_arguments"])
        installer = (root / "RIL_Client_Update.nsi").read_text(encoding="utf-8")
        self.assertNotIn("SetSilent normal", installer)
        self.assertNotIn("MUI_INSTFILESPAGE_HEADER_TEXT", installer)
        self.assertNotIn("MUI_INSTFILESPAGE_HEADER_SUBTEXT", installer)

    def test_gate_precedes_download_and_managed_rollback_defers_restart(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "RIL_client.py").read_text(encoding="utf-8")
        start = source.index("def download_and_install(")
        self.assertLess(source.index("self._update_gate = PendingUpdate", start), source.index("self.download_worker = DownloadWorker", start))
        installer = (root / "RIL_Client_Update.nsi").read_text(encoding="utf-8")
        for label, destination in (
            ("client_stage_launch_old", "client_stage_failed_message"),
            ("client_stage_launch_current", "client_stage_failed_message"),
            ("client_rollback_launch_old", "client_transaction_rolled_back_message"),
            ("client_rollback_launch_current", "client_transaction_rolled_back_message"),
        ):
            self.assertIn(f'{label}:\n    StrCmp $ClientHandoff "1" {destination}', installer)


if __name__ == "__main__":
    unittest.main()
