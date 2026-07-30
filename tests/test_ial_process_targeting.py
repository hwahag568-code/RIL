import unittest
from unittest import mock

import IAL


class FakeProcess:
    def __init__(self, name, executable, process_id=None):
        self.info = {
            "pid": process_id,
            "name": name,
            "exe": executable,
        }
        self.pid = process_id
        self.kill_calls = 0

    def kill(self):
        self.kill_calls += 1


class IalProcessTargetingTests(unittest.TestCase):
    def test_shell_execute_failure_is_reported_immediately(self):
        with mock.patch.object(
            IAL.win32api,
            "ShellExecute",
            return_value=31,
        ):
            with self.assertRaisesRegex(
                OSError,
                "실행하지 못했습니다",
            ):
                IAL.RunTask(r"C:\Interface", "interface.exe")

    def test_shell_execute_success_is_accepted(self):
        with mock.patch.object(
            IAL.win32api,
            "ShellExecute",
            return_value=33,
        ):
            IAL.RunTask(r"C:\Interface", "interface.exe")

    def test_launch_retries_once_from_the_same_executable_path(self):
        runner = mock.Mock(
            side_effect=[OSError("temporary failure"), None]
        )
        executable = r"D:\Moved Interface\interface.exe"

        with mock.patch.object(IAL.time, "sleep") as sleep:
            IAL._run_resolved_executable(executable, runner)

        self.assertEqual(
            runner.call_args_list,
            [
                mock.call(r"D:\Moved Interface", "interface.exe"),
                mock.call(r"D:\Moved Interface", "interface.exe"),
            ],
        )
        sleep.assert_called_once_with(
            IAL._AUTOMATION["launch_retry_delay_seconds"]
        )

    def test_launch_retry_does_not_switch_to_configured_copy(self):
        runner = mock.Mock(side_effect=OSError("cannot start"))
        executable = r"D:\Moved Interface\interface.exe"

        with (
            mock.patch.object(IAL.time, "sleep"),
            self.assertRaisesRegex(OSError, "cannot start"),
        ):
            IAL._run_resolved_executable(executable, runner)

        self.assertEqual(
            runner.call_count,
            IAL._AUTOMATION["launch_attempts"],
        )
        self.assertTrue(
            all(
                call.args
                == (r"D:\Moved Interface", "interface.exe")
                for call in runner.call_args_list
            )
        )

    def test_target_directory_does_not_kill_same_exe_in_other_folder(self):
        order = FakeProcess(
            IAL.INT,
            r"C:\Program Files (x86)\LIS_Interface\AU_3"
            rf"\{IAL.INT}",
        )
        other = FakeProcess(
            IAL.INT,
            r"C:\Program Files\LIS_Interface\Other"
            rf"\{IAL.INT}",
        )

        with (
            mock.patch.object(
                IAL.psutil,
                "process_iter",
                return_value=[order, other],
            ),
            mock.patch.object(
                IAL,
                "_same_directory",
                side_effect=lambda left, right: "AU_3" in left,
            ),
            mock.patch.object(
                IAL.psutil,
                "wait_procs",
                return_value=([], []),
            ),
        ):
            paths = IAL.TaskKill(
                IAL.INT,
                target_dir=IAL.AU_CONFIG[3]["order_dir"],
            )

        self.assertEqual(order.kill_calls, 1)
        self.assertEqual(other.kill_calls, 0)
        self.assertEqual(paths, [order.info["exe"]])

    def test_general_start_refuses_multiple_running_install_paths(self):
        first = FakeProcess(
            IAL.INT,
            rf"C:\First\{IAL.INT}",
        )
        second = FakeProcess(
            IAL.INT,
            rf"C:\Second\{IAL.INT}",
        )

        with mock.patch.object(
            IAL.psutil,
            "process_iter",
            return_value=[first, second],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "여러 폴더에서 실행 중",
            ):
                IAL.TaskKill(
                    IAL.INT,
                    require_unique_path=True,
                )

        self.assertEqual(first.kill_calls, 0)
        self.assertEqual(second.kill_calls, 0)

    def test_process_without_restart_path_is_not_stopped(self):
        process = FakeProcess(IAL.INT, None)

        with mock.patch.object(
            IAL.psutil,
            "process_iter",
            return_value=[process],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "실행 경로를 확인할 수 없어",
            ):
                IAL.TaskKill(
                    IAL.INT,
                    require_unique_path=True,
                )

        self.assertEqual(process.kill_calls, 0)

    def test_stopped_path_has_priority_over_configured_fallback(self):
        stopped = rf"D:\Moved Interface\{IAL.INTOS1}"
        configured = rf"C:\Configured Interface\{IAL.INTOS1}"

        with mock.patch.object(
            IAL.os.path,
            "isfile",
            side_effect=lambda value: value == stopped,
        ):
            executable = IAL._resolve_restart_executable(
                [stopped],
                configured,
                "OSMO 1",
            )

        self.assertEqual(executable, stopped)

    def test_missing_stopped_path_does_not_silently_use_configured_copy(self):
        stopped = rf"D:\Removed Interface\{IAL.INTOS1}"
        configured = rf"C:\Configured Interface\{IAL.INTOS1}"

        with mock.patch.object(
            IAL.os.path,
            "isfile",
            side_effect=lambda value: value == configured,
        ):
            with self.assertRaisesRegex(
                FileNotFoundError,
                "종료 전에 실행 중이던",
            ):
                IAL._resolve_restart_executable(
                    [stopped],
                    configured,
                    "OSMO 1",
                )

    def test_every_component_path_is_validated_before_any_process_stops(self):
        components = (
            (
                "first",
                IAL.INTOS1,
                rf"C:\First\{IAL.INTOS1}",
                IAL.RunTask,
            ),
            (
                "second",
                IAL.INTOS2,
                rf"C:\Second\{IAL.INTOS2}",
                IAL.RunTask,
            ),
        )

        with (
            mock.patch.object(
                IAL,
                "_find_task_targets",
                side_effect=[
                    (["process-1"], [r"D:\First\first.exe"]),
                    (["process-2"], [r"E:\Second\second.exe"]),
                ],
            ),
            mock.patch.object(
                IAL,
                "_resolve_restart_executable",
                side_effect=[
                    r"D:\First\first.exe",
                    FileNotFoundError("second missing"),
                ],
            ),
            mock.patch.object(
                IAL,
                "_stop_task_targets",
            ) as stop_targets,
        ):
            with self.assertRaisesRegex(
                FileNotFoundError,
                "second missing",
            ):
                IAL._prepare_restart_executables(components)

        stop_targets.assert_not_called()

    def test_profile_title_selects_the_moved_running_copy(self):
        configured = rf"C:\Configured AU\{IAL.INT}"
        actual = rf"D:\Moved AU\{IAL.INT}"
        process = FakeProcess(IAL.INT, actual, process_id=321)

        with (
            mock.patch.object(
                IAL,
                "_window_process_ids_with_title",
                return_value={321},
            ),
            mock.patch.object(
                IAL.psutil,
                "process_iter",
                side_effect=[[process], [process]],
            ),
        ):
            targets, paths = IAL._find_profile_task_targets(
                IAL.INT,
                configured,
                "차세대 AU_3 INTERFACE",
                "AU 3 오더",
            )

        self.assertEqual(targets, [process])
        self.assertEqual(paths, [actual])

    def test_profile_title_includes_same_path_instances(self):
        configured = rf"C:\Configured AU\{IAL.INT}"
        actual = rf"D:\Moved AU\{IAL.INT}"
        titled = FakeProcess(IAL.INT, actual, process_id=333)
        untitled = FakeProcess(IAL.INT, actual, process_id=334)

        with (
            mock.patch.object(
                IAL,
                "_window_process_ids_with_title",
                return_value={333},
            ),
            mock.patch.object(
                IAL.psutil,
                "process_iter",
                side_effect=[
                    [titled, untitled],
                    [titled, untitled],
                ],
            ),
        ):
            targets, paths = IAL._find_profile_task_targets(
                IAL.INT,
                configured,
                "차세대 AU_3 INTERFACE",
                "AU 3 오더",
            )

        self.assertEqual(targets, [titled, untitled])
        self.assertEqual(paths, [actual])

    def test_profile_login_window_reuses_the_moved_running_copy(self):
        configured = rf"C:\Configured AU\{IAL.INT}"
        actual = rf"D:\Moved AU\{IAL.INT}"
        process = FakeProcess(IAL.INT, actual, process_id=654)

        with (
            mock.patch.object(
                IAL,
                "_window_process_ids_with_title",
                side_effect=[set(), {654}],
            ) as find_window_processes,
            mock.patch.object(
                IAL.psutil,
                "process_iter",
                return_value=[process],
            ),
        ):
            targets, paths = IAL._find_profile_task_targets(
                IAL.INT,
                configured,
                "차세대 AU_3 INTERFACE",
                "AU 3 오더",
            )

        self.assertEqual(
            find_window_processes.call_args_list,
            [
                mock.call("차세대 AU_3 INTERFACE"),
                mock.call(IAL.INTT),
            ],
        )
        self.assertEqual(targets, [process])
        self.assertEqual(paths, [actual])

    def test_profile_login_window_rejects_another_same_name_path(self):
        configured = rf"C:\Configured AU\{IAL.INT}"
        login_path = rf"D:\Stale AU\{IAL.INT}"
        configured_path = rf"C:\Configured AU\{IAL.INT}"
        login_process = FakeProcess(
            IAL.INT,
            login_path,
            process_id=777,
        )
        configured_process = FakeProcess(
            IAL.INT,
            configured_path,
            process_id=888,
        )

        with (
            mock.patch.object(
                IAL,
                "_window_process_ids_with_title",
                side_effect=[set(), {777}],
            ),
            mock.patch.object(
                IAL.psutil,
                "process_iter",
                side_effect=[
                    [login_process, configured_process],
                    [login_process, configured_process],
                ],
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "여러 폴더에서 실행 중",
            ),
        ):
            IAL._find_profile_task_targets(
                IAL.INT,
                configured,
                "차세대 AU_3 INTERFACE",
                "AU 3 오더",
            )

        self.assertEqual(login_process.kill_calls, 0)
        self.assertEqual(configured_process.kill_calls, 0)

    def test_profile_login_window_includes_same_path_instances(self):
        configured = rf"C:\Configured AU\{IAL.INT}"
        actual = rf"D:\Moved AU\{IAL.INT}"
        titled = FakeProcess(IAL.INT, actual, process_id=901)
        untitled = FakeProcess(IAL.INT, actual, process_id=902)

        with (
            mock.patch.object(
                IAL,
                "_window_process_ids_with_title",
                side_effect=[set(), {901}],
            ),
            mock.patch.object(
                IAL.psutil,
                "process_iter",
                side_effect=[
                    [titled, untitled],
                    [titled, untitled],
                ],
            ),
        ):
            targets, paths = IAL._find_profile_task_targets(
                IAL.INT,
                configured,
                "차세대 AU_3 INTERFACE",
                "AU 3 오더",
            )

        self.assertEqual(targets, [titled, untitled])
        self.assertEqual(paths, [actual])

    def test_profile_does_not_mistake_another_running_copy(self):
        configured = rf"C:\Configured AU 3\{IAL.INT}"
        other_path = rf"D:\Other AU\{IAL.INT}"
        other = FakeProcess(IAL.INT, other_path, process_id=123)

        with (
            mock.patch.object(
                IAL,
                "_window_process_ids_with_title",
                side_effect=[set(), set()],
            ),
            mock.patch.object(
                IAL,
                "_find_task_targets",
                side_effect=[
                    ([], []),
                    ([other], [other_path]),
                ],
            ) as find_targets,
            self.assertRaisesRegex(
                RuntimeError,
                "안전하게 종료하지 않았습니다",
            ),
        ):
            IAL._find_profile_task_targets(
                IAL.INT,
                configured,
                "차세대 AU_3 INTERFACE",
                "AU 3 오더",
            )

        self.assertEqual(
            find_targets.call_args_list,
            [
                mock.call(
                    IAL.INT,
                    target_dir=r"C:\Configured AU 3",
                    require_unique_path=True,
                ),
                mock.call(
                    IAL.INT,
                    require_unique_path=True,
                ),
            ],
        )
        self.assertEqual(other.kill_calls, 0)

    def test_osmo_restarts_both_actual_paths_when_first_login_fails(self):
        actual_1 = rf"D:\OSMO-A\{IAL.INTOS1}"
        actual_2 = rf"E:\OSMO-B\{IAL.INTOS2}"

        with (
            mock.patch.object(
                IAL,
                "_find_task_targets",
                side_effect=[
                    (["process-1"], [actual_1]),
                    (["process-2"], [actual_2]),
                ],
            ) as find_targets,
            mock.patch.object(IAL, "_stop_task_targets"),
            mock.patch.object(
                IAL,
                "_resolve_restart_executable",
                side_effect=[actual_1, actual_2],
            ),
            mock.patch.object(
                IAL,
                "StartTaskOS1",
                return_value=False,
            ) as start_1,
            mock.patch.object(
                IAL,
                "StartTaskOS2",
                return_value=True,
            ) as start_2,
            mock.patch.object(IAL.time, "sleep"),
        ):
            result = IAL.StartTaskOS("user1", "password1")

        self.assertEqual(result, "int_failed_1")
        self.assertEqual(
            find_targets.call_args_list,
            [
                mock.call(IAL.INTOS1, require_unique_path=True),
                mock.call(IAL.INTOS2, require_unique_path=True),
            ],
        )
        start_1.assert_called_once_with(
            "user1",
            "password1",
            actual_1,
        )
        start_2.assert_called_once_with(
            "user1",
            "password1",
            actual_2,
        )

    def test_au_restarts_order_and_result_from_actual_paths(self):
        actual_order = rf"D:\AU Order\{IAL.INT}"
        actual_result = rf"E:\AU Result\{IAL.INTAURSLT}"

        with (
            mock.patch.object(
                IAL,
                "_find_profile_task_targets",
                side_effect=[
                    (["order-process"], [actual_order]),
                    (["result-process"], [actual_result]),
                ],
            ) as find_targets,
            mock.patch.object(IAL, "_stop_task_targets"),
            mock.patch.object(
                IAL,
                "_resolve_restart_executable",
                side_effect=[actual_order, actual_result],
            ),
            mock.patch.object(
                IAL,
                "StartTaskAUOrder",
                return_value=True,
            ) as start_order,
            mock.patch.object(
                IAL,
                "StartTaskAUResult",
                return_value=True,
            ) as start_result,
            mock.patch.object(IAL, "align_after_login"),
        ):
            result = IAL.StartTaskAU("user1", "password1", 3)

        self.assertEqual(result, "int_success")
        self.assertEqual(
            find_targets.call_args_list,
            [
                mock.call(
                    IAL.INT,
                    rf"{IAL.AU_CONFIG[3]['order_dir']}\{IAL.INT}",
                    IAL.AU_CONFIG[3]["order_title"],
                    "AU 3 오더",
                ),
                mock.call(
                    IAL.INTAURSLT,
                    rf"{IAL.AU_CONFIG[3]['result_dir']}\{IAL.INTAURSLT}",
                    IAL.AU_CONFIG[3]["result_title"],
                    "AU 3 결과",
                ),
            ],
        )
        start_order.assert_called_once_with(
            "user1",
            "password1",
            IAL.AU_CONFIG[3],
            3,
            actual_order,
        )
        start_result.assert_called_once_with(
            "user1",
            "password1",
            IAL.AU_CONFIG[3],
            3,
            actual_result,
        )

    def test_nova_restarts_both_actual_paths(self):
        actual_1 = rf"D:\Nova-A\{IAL.INTCA1}"
        actual_2 = rf"E:\Nova-B\{IAL.INTCA2}"

        with (
            mock.patch.object(
                IAL,
                "_find_task_targets",
                side_effect=[
                    (["process-1"], [actual_1]),
                    (["process-2"], [actual_2]),
                ],
            ) as find_targets,
            mock.patch.object(IAL, "_stop_task_targets"),
            mock.patch.object(
                IAL,
                "_resolve_restart_executable",
                side_effect=[actual_1, actual_2],
            ),
            mock.patch.object(
                IAL,
                "StartTaskNova1",
                return_value=True,
            ) as start_1,
            mock.patch.object(
                IAL,
                "StartTaskNova2",
                return_value=True,
            ) as start_2,
            mock.patch.object(IAL, "align_after_login"),
        ):
            result = IAL.StartTaskNovaPrime("user1", "password1")

        self.assertEqual(result, "int_success")
        self.assertEqual(
            find_targets.call_args_list,
            [
                mock.call(IAL.INTCA1, require_unique_path=True),
                mock.call(IAL.INTCA2, require_unique_path=True),
            ],
        )
        start_1.assert_called_once_with(
            "user1",
            "password1",
            actual_1,
        )
        start_2.assert_called_once_with(
            "user1",
            "password1",
            actual_2,
        )


if __name__ == "__main__":
    unittest.main()
