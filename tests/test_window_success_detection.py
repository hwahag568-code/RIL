import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

import IAL


def make_window(handle, title, width=800):
    return SimpleNamespace(_hWnd=handle, title=title, width=width)


class WindowSuccessDetectionTests(unittest.TestCase):
    def test_sendkeys_text_escapes_control_characters(self):
        self.assertEqual(
            IAL._escape_sendkeys_text("ab+^%~()[]{}"),
            r"ab{+}{^}{%}{~}{(}{)}{[}{]}{{}{}}",
        )

    def test_login_uses_escaped_id_and_password(self):
        source = inspect.getsource(IAL.login_common)
        self.assertIn(
            "shell.SendKeys(_escape_sendkeys_text(int_id))",
            source,
        )
        self.assertIn(
            "shell.SendKeys(_escape_sendkeys_text(int_pw))",
            source,
        )

    def test_snapshot_includes_hidden_existing_window_handles(self):
        def enumerate_windows(callback, extra):
            callback(10, extra)
            callback(20, extra)

        with (
            mock.patch.object(
                IAL.win32gui,
                "EnumWindows",
                side_effect=enumerate_windows,
            ),
            mock.patch.object(IAL.win32gui, "IsWindow", return_value=True),
        ):
            handles = IAL.snapshot_window_handles()

        self.assertEqual(handles, {10, 20})

    def test_wait_ignores_matching_window_that_existed_before_launch(self):
        old_window = make_window(10, "Unrelated INTERFACE")
        new_window = make_window(
            20,
            "--- NovaStatProfilePrime 1 - INTERFACE ---",
        )
        expected_executable = r"C:\Nova\NovaStatProfilePrime_1.exe"

        with (
            mock.patch.object(
                IAL.gw,
                "getAllWindows",
                return_value=[old_window, new_window],
            ),
            mock.patch.object(
                IAL.win32gui,
                "IsWindowVisible",
                return_value=True,
            ),
            mock.patch.object(
                IAL,
                "_window_owned_by_executable",
                return_value=True,
            ),
        ):
            found = IAL.wait_for_window_title_contains(
                IAL.Novaprime1,
                expected_executable=expected_executable,
                timeout=1,
                interval=0,
                exclude_handles={10},
            )

        self.assertTrue(found)

    def test_wait_does_not_accept_old_matching_window(self):
        old_window = make_window(10, "NovaStatProfilePrime 2 INTERFACE")
        expected_executable = r"C:\Nova\NovaStatProfilePrime_2.exe"

        with (
            mock.patch.object(
                IAL.gw,
                "getAllWindows",
                return_value=[old_window],
            ),
            mock.patch.object(
                IAL.win32gui,
                "IsWindowVisible",
                return_value=True,
            ),
            mock.patch.object(
                IAL,
                "_window_owned_by_executable",
                return_value=True,
            ),
            mock.patch.object(
                IAL.time,
                "monotonic",
                side_effect=[0.0, 0.0, 0.2],
            ),
            mock.patch.object(IAL.time, "sleep"),
        ):
            found = IAL.wait_for_window_title_contains(
                IAL.Novaprime2,
                expected_executable=expected_executable,
                timeout=0.1,
                interval=0,
                exclude_handles={10},
            )

        self.assertFalse(found)

    def test_nova_titles_are_profile_specific(self):
        self.assertEqual(IAL.Novaprime1, "NovaStatProfilePrime 1")
        self.assertEqual(IAL.Novaprime2, "NovaStatProfilePrime 2")

    def test_general_interface_title_rejects_jit_and_error_dialogs(self):
        self.assertFalse(
            IAL._is_general_interface_title(
                "Ui.Kumc.GR.Interface",
            )
        )
        self.assertFalse(
            IAL._is_general_interface_title(
                ".NET Framework - JIT Debugging Error",
            )
        )
        self.assertFalse(
            IAL._is_general_interface_title(
                "인터페이스 오류",
            )
        )

    def test_general_interface_title_accepts_decorated_main_title(self):
        self.assertTrue(
            IAL._is_general_interface_title(
                "--- 차세대 Alinity INTERFACE ---",
            )
        )
        self.assertTrue(
            IAL._is_general_interface_title(
                "[운영] DxI INTERFACE - 연결됨",
            )
        )
        self.assertTrue(
            IAL._is_general_interface_title(
                "FUJITSU INTERFACE",
            )
        )

    def test_login_selector_ignores_a_login_window_from_previous_launch(self):
        old_login = make_window(10, IAL.INTT)
        new_login = make_window(20, IAL.INTT)
        expected_executable = r"C:\Interface\Expected.exe"

        with (
            mock.patch.object(
                IAL.win32gui,
                "IsWindowVisible",
                return_value=True,
            ),
            mock.patch.object(
                IAL.win32gui,
                "IsWindow",
                return_value=True,
            ),
            mock.patch.object(
                IAL,
                "_window_owned_by_executable",
                return_value=True,
            ),
        ):
            selected = IAL._select_login_window(
                [old_login, new_login],
                expected_executable,
                exclude_handles={10},
            )

        self.assertEqual(selected, 20)

    def test_login_selector_skips_a_stale_window(self):
        stale_login = mock.Mock()
        stale_login._hWnd = 10
        type(stale_login).title = mock.PropertyMock(
            side_effect=RuntimeError("window was destroyed"),
        )
        new_login = make_window(20, IAL.INTT)
        expected_executable = r"C:\Interface\Expected.exe"

        with (
            mock.patch.object(
                IAL.win32gui,
                "IsWindowVisible",
                return_value=True,
            ),
            mock.patch.object(
                IAL.win32gui,
                "IsWindow",
                return_value=True,
            ),
            mock.patch.object(
                IAL,
                "_window_owned_by_executable",
                return_value=True,
            ),
        ):
            selected = IAL._select_login_window(
                [stale_login, new_login],
                expected_executable,
            )

        self.assertEqual(selected, 20)

    def test_login_selector_skips_other_executable_and_accepts_expected(self):
        other_login = make_window(10, IAL.INTT)
        expected_login = make_window(20, IAL.INTT)
        expected_executable = r"C:\Interface\Expected.exe"

        def belongs_to_expected(handle, executable):
            self.assertEqual(executable, expected_executable)
            return handle == 20

        with (
            mock.patch.object(
                IAL.win32gui,
                "IsWindowVisible",
                return_value=True,
            ),
            mock.patch.object(
                IAL.win32gui,
                "IsWindow",
                return_value=True,
            ),
            mock.patch.object(
                IAL,
                "_window_owned_by_executable",
                side_effect=belongs_to_expected,
            ),
        ):
            selected = IAL._select_login_window(
                [other_login, expected_login],
                expected_executable,
            )

        self.assertEqual(selected, 20)

    def test_window_owner_requires_matching_full_executable_path(self):
        process = mock.Mock()
        process.exe.return_value = r"C:\Interface\Expected.exe"

        with (
            mock.patch.object(
                IAL.win32process,
                "GetWindowThreadProcessId",
                return_value=(1, 1234),
            ),
            mock.patch.object(
                IAL.psutil,
                "Process",
                return_value=process,
            ),
        ):
            self.assertTrue(
                IAL._window_owned_by_executable(
                    20,
                    r"C:\Interface\Expected.exe",
                )
            )
            self.assertFalse(
                IAL._window_owned_by_executable(
                    20,
                    r"D:\Other\Expected.exe",
                )
            )

    def test_window_owner_fails_closed_when_process_path_is_unavailable(self):
        process = mock.Mock()
        process.exe.side_effect = IAL.psutil.AccessDenied(pid=1234)

        with (
            mock.patch.object(
                IAL.win32process,
                "GetWindowThreadProcessId",
                return_value=(1, 1234),
            ),
            mock.patch.object(
                IAL.psutil,
                "Process",
                return_value=process,
            ),
        ):
            self.assertFalse(
                IAL._window_owned_by_executable(
                    20,
                    r"C:\Interface\Expected.exe",
                )
            )

    def test_success_wait_skips_same_title_owned_by_other_executable(self):
        other_window = make_window(10, IAL.Novaprime1)
        expected_window = make_window(20, IAL.Novaprime1)
        expected_executable = r"C:\Nova\NovaStatProfilePrime_1.exe"

        with (
            mock.patch.object(
                IAL.gw,
                "getAllWindows",
                return_value=[other_window, expected_window],
            ),
            mock.patch.object(
                IAL.win32gui,
                "IsWindowVisible",
                return_value=True,
            ),
            mock.patch.object(
                IAL,
                "_window_owned_by_executable",
                side_effect=lambda handle, _path: handle == 20,
            ),
        ):
            found = IAL.wait_for_window_title_contains(
                IAL.Novaprime1,
                expected_executable=expected_executable,
                timeout=1,
                interval=0,
            )

        self.assertTrue(found)

    def test_every_launcher_excludes_windows_that_predate_its_launch(self):
        launchers = (
            IAL.StartTask,
            IAL.StartTaskOS1,
            IAL.StartTaskOS2,
            IAL.StartTaskAUOrder,
            IAL.StartTaskAUResult,
            IAL.StartTaskNova1,
            IAL.StartTaskNova2,
        )

        for launcher in launchers:
            with self.subTest(launcher=launcher.__name__):
                source = inspect.getsource(launcher)
                self.assertIn(
                    "exclude_handles=existing_windows",
                    source,
                )
                self.assertIn(
                    "expected_executable=executable",
                    source,
                )

    def test_composite_launchers_align_before_the_ial_child_exits(self):
        launchers = (
            IAL.StartTaskOS,
            IAL.StartTaskAU,
            IAL.StartTaskNovaPrime,
        )

        for launcher in launchers:
            with self.subTest(launcher=launcher.__name__):
                source = inspect.getsource(launcher)
                self.assertIn("synchronous=True", source)

    def test_synchronous_au_alignment_does_not_create_a_daemon_thread(self):
        with (
            mock.patch.object(IAL.time, "sleep"),
            mock.patch.object(IAL, "findau", return_value=True) as findau,
            mock.patch.object(IAL.threading, "Thread") as thread,
        ):
            IAL.align_after_login(
                au_number=3,
                synchronous=True,
            )

        findau.assert_called_once_with(3)
        thread.assert_not_called()

    def test_nova_alignment_uses_monitor_work_area_and_casefold_titles(self):
        nova1 = mock.Mock()
        nova1._hWnd = 10
        nova1.title = "novastatprofileprime 1 - interface"
        nova2 = mock.Mock()
        nova2._hWnd = 20
        nova2.title = "NOVASTATPROFILEPRIME 2 - INTERFACE"

        with (
            mock.patch.object(
                IAL.gw,
                "getAllWindows",
                return_value=[nova2, nova1],
            ),
            mock.patch.object(
                IAL.win32gui,
                "IsWindowVisible",
                return_value=True,
            ),
            mock.patch.object(
                IAL,
                "_get_window_work_area",
                return_value=(100, 50, 1700, 950),
            ),
            mock.patch.object(IAL.time, "sleep"),
        ):
            IAL.findnova()

        nova1.moveTo.assert_called_once_with(100, 50)
        nova1.resizeTo.assert_called_once_with(800, 900)
        nova2.moveTo.assert_called_once_with(900, 50)
        nova2.resizeTo.assert_called_once_with(800, 900)

    def test_osmo_alignment_uses_monitor_work_area(self):
        osmo1 = mock.Mock()
        osmo1._hWnd = 10
        osmo1.title = IAL.TITLE_OSMO1
        osmo2 = mock.Mock()
        osmo2._hWnd = 20
        osmo2.title = IAL.TITLE_OSMO2

        with (
            mock.patch.object(
                IAL.gw,
                "getAllWindows",
                return_value=[osmo1, osmo2],
            ),
            mock.patch.object(
                IAL.win32gui,
                "IsWindowVisible",
                return_value=True,
            ),
            mock.patch.object(
                IAL,
                "_get_window_work_area",
                return_value=(100, 50, 1700, 950),
            ),
            mock.patch.object(IAL.time, "sleep"),
        ):
            IAL.findosmo()

        osmo1.moveTo.assert_called_once_with(100, 50)
        osmo1.resizeTo.assert_called_once_with(800, 900)
        osmo2.moveTo.assert_called_once_with(900, 50)
        osmo2.resizeTo.assert_called_once_with(800, 900)


if __name__ == "__main__":
    unittest.main()
