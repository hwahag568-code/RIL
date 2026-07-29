import unittest
from unittest import mock

import RIL_server


class FakeProcess:
    def __init__(self, *alive_values):
        self._alive_values = list(alive_values)
        self._alive = alive_values[-1] if alive_values else True
        self.start_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_timeouts = []

    def start(self):
        self.start_calls += 1

    def is_alive(self):
        if self._alive_values:
            self._alive = self._alive_values.pop(0)
        return self._alive

    def terminate(self):
        self.terminate_calls += 1
        self._alive = False

    def kill(self):
        self.kill_calls += 1
        self._alive = False

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)


class StubbornProcess(FakeProcess):
    def terminate(self):
        self.terminate_calls += 1


class UnstoppableProcess(StubbornProcess):
    def kill(self):
        self.kill_calls += 1


class FailingStartProcess(FakeProcess):
    def start(self):
        self.start_calls += 1
        raise RuntimeError("start failed")


class TrayMenuCallbackTests(unittest.TestCase):
    def test_quit_menu_action_stops_the_icon(self):
        icon = mock.Mock()
        requested_stop = mock.Mock()

        RIL_server.quit_tray(icon, None, requested_stop)

        requested_stop.set.assert_called_once_with()
        icon.stop.assert_called_once_with()


class ServerWatchdogTests(unittest.TestCase):
    def test_restarts_when_server_dies_after_a_poll(self):
        tray = FakeProcess(True, True)
        server = FakeProcess(True, False)
        wakeup = FakeProcess(True)
        restart = mock.Mock()

        with (
            mock.patch.object(RIL_server.time, "sleep") as sleep,
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                poll_interval=1,
                join_timeout=5,
            )

        self.assertEqual(action, "restart")
        self.assertEqual(
            [tray.start_calls, server.start_calls, wakeup.start_calls],
            [1, 1, 1],
        )
        sleep.assert_called_once_with(1)
        restart.assert_called_once_with()
        self.assertEqual(tray.terminate_calls, 1)
        self.assertEqual(server.terminate_calls, 0)
        self.assertEqual(wakeup.terminate_calls, 1)
        self.assertEqual(tray.join_timeouts, [5])
        self.assertEqual(server.join_timeouts, [5])
        self.assertEqual(wakeup.join_timeouts, [5])

    def test_restarts_only_wakeup_when_wakeup_process_dies(self):
        tray = FakeProcess(True, True)
        server = FakeProcess(True, False)
        wakeup = FakeProcess(False)
        replacement = FakeProcess(True)
        restart = mock.Mock()
        wakeup_factory = mock.Mock(return_value=replacement)

        with (
            mock.patch.object(RIL_server.time, "sleep") as sleep,
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
                wakeup_factory=wakeup_factory,
            )

        self.assertEqual(action, "restart")
        sleep.assert_called_once_with(1)
        restart.assert_called_once_with()
        self.assertEqual(tray.terminate_calls, 1)
        self.assertEqual(server.terminate_calls, 0)
        self.assertEqual(wakeup.terminate_calls, 0)
        self.assertEqual(wakeup.join_timeouts, [5])
        self.assertEqual(replacement.start_calls, 1)
        wakeup_factory.assert_called_once_with()

    def test_requested_tray_exit_stops_children_without_restart(self):
        tray = FakeProcess(False)
        server = FakeProcess(True)
        wakeup = FakeProcess(True)
        restart = mock.Mock()
        requested_stop = mock.Mock()
        requested_stop.is_set.return_value = True

        with (
            mock.patch.object(RIL_server.time, "sleep") as sleep,
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
                requested_stop=requested_stop,
            )

        self.assertEqual(action, "stop")
        sleep.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(tray.terminate_calls, 0)
        self.assertEqual(server.terminate_calls, 1)
        self.assertEqual(wakeup.terminate_calls, 1)

    def test_requested_stop_does_not_wait_for_tray_process_to_exit(self):
        tray = FakeProcess(True)
        server = FakeProcess(True)
        wakeup = FakeProcess(True)
        restart = mock.Mock()
        requested_stop = mock.Mock()
        requested_stop.is_set.return_value = True

        with (
            mock.patch.object(RIL_server.time, "sleep") as sleep,
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
                requested_stop=requested_stop,
            )

        self.assertEqual(action, "stop")
        sleep.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(tray.terminate_calls, 1)
        self.assertEqual(server.terminate_calls, 1)
        self.assertEqual(wakeup.terminate_calls, 1)

    def test_tray_crash_restarts_instead_of_looking_like_user_exit(self):
        tray = FakeProcess(False)
        server = FakeProcess(True)
        wakeup = FakeProcess(True)
        restart = mock.Mock()
        requested_stop = mock.Mock()
        requested_stop.is_set.return_value = False

        with (
            mock.patch.object(RIL_server.time, "sleep"),
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
                requested_stop=requested_stop,
            )

        self.assertEqual(action, "restart")
        restart.assert_called_once_with()
        self.assertEqual(server.terminate_calls, 1)
        self.assertEqual(wakeup.terminate_calls, 1)

    def test_tray_crash_restarts_only_tray_when_factory_is_available(self):
        tray = FakeProcess(False)
        replacement = FakeProcess(True, False)
        server = FakeProcess(True)
        wakeup = FakeProcess(True)
        restart = mock.Mock()
        requested_stop = mock.Mock()
        requested_stop.is_set.side_effect = [False, True]
        tray_factory = mock.Mock(return_value=replacement)

        with (
            mock.patch.object(RIL_server.time, "sleep"),
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
                requested_stop=requested_stop,
                tray_factory=tray_factory,
            )

        self.assertEqual(action, "stop")
        restart.assert_not_called()
        tray_factory.assert_called_once_with()
        self.assertEqual(replacement.start_calls, 1)
        self.assertEqual(tray.join_timeouts, [5])

    def test_requested_tray_exit_has_priority_if_server_is_also_dead(self):
        tray = FakeProcess(False)
        server = FakeProcess(False)
        wakeup = FakeProcess(True)
        restart = mock.Mock()
        requested_stop = mock.Mock()
        requested_stop.is_set.return_value = True

        with (
            mock.patch.object(RIL_server.time, "sleep"),
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
                requested_stop=requested_stop,
            )

        self.assertEqual(action, "stop")
        restart.assert_not_called()

    def test_start_failure_stops_children_that_already_started(self):
        tray = FakeProcess(True)
        server = FailingStartProcess()
        wakeup = FakeProcess(True)

        with (
            mock.patch.object(RIL_server, "ErrorLog"),
            self.assertRaisesRegex(RuntimeError, "start failed"),
        ):
            RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                mock.Mock(),
                join_timeout=5,
            )

        self.assertEqual(tray.start_calls, 1)
        self.assertEqual(tray.terminate_calls, 1)
        self.assertEqual(tray.join_timeouts, [5])
        self.assertEqual(wakeup.start_calls, 0)

    def test_kills_a_child_that_ignores_terminate_before_restart(self):
        tray = StubbornProcess(True)
        server = FakeProcess(False)
        wakeup = FakeProcess(True)
        restart = mock.Mock()

        with (
            mock.patch.object(RIL_server.time, "sleep"),
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
            )

        self.assertEqual(action, "restart")
        self.assertEqual(tray.terminate_calls, 1)
        self.assertEqual(tray.kill_calls, 1)
        self.assertEqual(tray.join_timeouts, [5, 5])
        restart.assert_called_once_with()

    def test_does_not_restart_while_an_old_child_is_still_alive(self):
        tray = UnstoppableProcess(True)
        server = FakeProcess(False)
        wakeup = FakeProcess(True)
        restart = mock.Mock()

        with (
            mock.patch.object(RIL_server.time, "sleep"),
            mock.patch.object(RIL_server, "ErrorLog"),
            self.assertRaisesRegex(
                RuntimeError,
                "자식 프로세스를 종료하지 못했습니다",
            ),
        ):
            RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
            )

        restart.assert_not_called()


class IalHardTimeoutTests(unittest.TestCase):
    def test_returns_child_result_and_reaps_child(self):
        reader = mock.Mock()
        reader.poll.return_value = True
        reader.recv.return_value = "int_success"
        writer = mock.Mock()
        process = mock.Mock()
        process.is_alive.return_value = False

        with (
            mock.patch.object(
                RIL_server.multiprocessing,
                "Pipe",
                return_value=(reader, writer),
            ),
            mock.patch.object(
                RIL_server,
                "mp",
                return_value=process,
            ),
        ):
            result = RIL_server.execute_ial_with_hard_timeout(
                "user1",
                "password1",
                "AU3",
                timeout=7,
            )

        self.assertEqual(result, "int_success")
        reader.poll.assert_called_once_with(7)
        process.start.assert_called_once_with()
        process.join.assert_called_once_with(
            timeout=RIL_server.IAL_CHILD_JOIN_TIMEOUT,
        )
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        writer.close.assert_called_once_with()
        reader.close.assert_called_once_with()

    def test_timeout_terminates_child_and_returns_failure(self):
        reader = mock.Mock()
        reader.poll.return_value = False
        writer = mock.Mock()
        process = mock.Mock()
        process.is_alive.side_effect = [
            True,
            False,
            False,
            False,
        ]

        with (
            mock.patch.object(
                RIL_server.multiprocessing,
                "Pipe",
                return_value=(reader, writer),
            ),
            mock.patch.object(
                RIL_server,
                "mp",
                return_value=process,
            ),
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            result = RIL_server.execute_ial_with_hard_timeout(
                "user1",
                "password1",
                "AU3",
                timeout=7,
            )

        self.assertEqual(result, "int_failed")
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()


class ServerRuntimeRecoveryTests(unittest.TestCase):
    def tearDown(self):
        RIL_server.release_single_instance_mutex()

    def test_existing_named_mutex_rejects_duplicate_parent(self):
        kernel32 = mock.Mock()
        kernel32.CreateMutexW.return_value = 123
        kernel32.GetLastError.return_value = (
            RIL_server.ERROR_ALREADY_EXISTS
        )

        acquired = RIL_server.acquire_single_instance_mutex(
            kernel32=kernel32,
        )

        self.assertFalse(acquired)
        kernel32.CloseHandle.assert_called_once_with(123)

    def test_named_mutex_handle_is_held_until_release(self):
        kernel32 = mock.Mock()
        kernel32.CreateMutexW.return_value = 456
        kernel32.GetLastError.return_value = 0

        acquired = RIL_server.acquire_single_instance_mutex(
            kernel32=kernel32,
        )
        RIL_server.release_single_instance_mutex()

        self.assertTrue(acquired)
        kernel32.CloseHandle.assert_called_once_with(456)

    def test_frozen_restart_command_does_not_duplicate_executable_arg(self):
        with (
            mock.patch.object(
                RIL_server.sys,
                "frozen",
                True,
                create=True,
            ),
            mock.patch.object(
                RIL_server.sys,
                "executable",
                r"C:\Program Files\RIL\RIL_server.exe",
            ),
            mock.patch.object(
                RIL_server.sys,
                "argv",
                [
                    r"C:\Program Files\RIL\RIL_server.exe",
                    "--test",
                ],
            ),
        ):
            command = RIL_server._restart_command()

        self.assertEqual(
            command,
            [
                r"C:\Program Files\RIL\RIL_server.exe",
                "--test",
            ],
        )

    def test_restart_falls_back_to_new_process_when_exec_fails(self):
        with (
            mock.patch.object(RIL_server.time, "sleep"),
            mock.patch.object(RIL_server, "ErrorLog"),
            mock.patch.object(
                RIL_server,
                "release_single_instance_mutex",
            ),
            mock.patch.object(
                RIL_server,
                "_restart_command",
                return_value=["RIL_server.exe"],
            ),
            mock.patch.object(
                RIL_server.os,
                "execv",
                side_effect=OSError("exec failed"),
            ),
            mock.patch.object(
                RIL_server.subprocess,
                "Popen",
            ) as popen,
        ):
            RIL_server.restartscript()

        popen.assert_called_once_with(
            ["RIL_server.exe"],
            cwd=RIL_server.APP_DIR,
            close_fds=True,
        )

    def test_wakeup_holds_required_flags_until_exit(self):
        kernel32 = mock.Mock()
        kernel32.SetThreadExecutionState.return_value = 1

        with (
            mock.patch.object(
                RIL_server.ctypes,
                "windll",
                mock.Mock(kernel32=kernel32),
            ),
            mock.patch.object(
                RIL_server.time,
                "sleep",
                side_effect=KeyboardInterrupt,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            RIL_server.wakeup()

        self.assertEqual(
            kernel32.SetThreadExecutionState.call_args_list,
            [
                mock.call(
                    RIL_server.ES_CONTINUOUS
                    | RIL_server.ES_SYSTEM_REQUIRED
                    | RIL_server.ES_DISPLAY_REQUIRED
                ),
                mock.call(RIL_server.ES_CONTINUOUS),
            ],
        )


if __name__ == "__main__":
    unittest.main()
