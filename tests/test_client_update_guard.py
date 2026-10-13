import ctypes
from pathlib import Path
import unittest
import subprocess
import sys
import uuid
from unittest import mock

from ril_client_update_guard import show_update_notice, update_in_progress


class ClientUpdateGuardTests(unittest.TestCase):
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
        self.assertIn('StrCmp $RecoveryMode "1" +2\n    SetSilent normal', installer)
        self.assertIn('ril-client-update.log', installer)
        self.assertIn('StrCpy $UpdatePhase "rolled_back"', installer)


if __name__ == "__main__":
    unittest.main()
