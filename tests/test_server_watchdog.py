import multiprocessing
import threading
import unittest
from unittest import mock

import RIL_server


def _run_nested_controlled_ial(worker_control, result_queue):
    result_queue.put(
        RIL_server.execute_ial_with_hard_timeout(
            "user1",
            "password1",
            "unknown-test-command",
            timeout=5,
            worker_control=worker_control,
        )
    )


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

    def test_requested_stop_terminates_only_the_registered_ial_worker(self):
        tray = FakeProcess(True)
        server = FakeProcess(True)
        server.pid = 2468
        wakeup = FakeProcess(True)
        restart = mock.Mock()
        requested_stop = mock.Mock()
        requested_stop.is_set.return_value = True
        worker_control = RIL_server._IalWorkerControl()
        worker_control.idle_event.clear()
        RIL_server._publish_ial_worker_identity(
            worker_control,
            pid=4321,
            create_time=123.5,
            owner_pid=server.pid,
        )
        ial_worker = mock.Mock()
        ial_worker.create_time.return_value = 123.5
        ial_worker.ppid.return_value = server.pid
        stop_order = []

        def wait_for_worker(timeout):
            if ial_worker.wait.call_count == 1:
                raise RIL_server.psutil.TimeoutExpired(
                    timeout,
                    pid=4321,
                )
            stop_order.append("worker_dead")
            return 0

        ial_worker.wait.side_effect = wait_for_worker
        original_server_terminate = server.terminate

        def terminate_server():
            stop_order.append("listener_terminate")
            original_server_terminate()

        server.terminate = terminate_server

        with (
            mock.patch.object(RIL_server.time, "sleep"),
            mock.patch.object(RIL_server, "ErrorLog"),
            mock.patch.object(
                worker_control.idle_event,
                "wait",
                return_value=False,
            ),
            mock.patch.object(
                RIL_server.psutil,
                "Process",
                return_value=ial_worker,
            ) as process_from_pid,
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                restart,
                join_timeout=5,
                requested_stop=requested_stop,
                ial_worker_control=worker_control,
            )

        self.assertEqual(action, "stop")
        self.assertTrue(worker_control.cancel_event.is_set())
        self.assertEqual(worker_control.pid.value, 0)
        process_from_pid.assert_called_once_with(4321)
        ial_worker.terminate.assert_called_once_with()
        ial_worker.kill.assert_called_once_with()
        self.assertEqual(
            ial_worker.wait.call_args_list,
            [mock.call(timeout=5), mock.call(timeout=5)],
        )
        ial_worker.children.assert_not_called()
        self.assertEqual(
            stop_order,
            ["worker_dead", "listener_terminate"],
        )
        restart.assert_not_called()

    def test_reused_worker_pid_is_not_terminated(self):
        tray = FakeProcess(True)
        server = FakeProcess(True)
        server.pid = 2468
        wakeup = FakeProcess(True)
        requested_stop = mock.Mock()
        requested_stop.is_set.return_value = True
        worker_control = RIL_server._IalWorkerControl()
        worker_control.idle_event.clear()
        RIL_server._publish_ial_worker_identity(
            worker_control,
            pid=4321,
            create_time=123.5,
            owner_pid=server.pid,
        )
        reused_process = mock.Mock()
        reused_process.create_time.return_value = 123.5005

        with (
            mock.patch.object(RIL_server.time, "sleep"),
            mock.patch.object(RIL_server, "ErrorLog"),
            mock.patch.object(
                worker_control.idle_event,
                "wait",
                return_value=False,
            ),
            mock.patch.object(
                RIL_server.psutil,
                "Process",
                return_value=reused_process,
            ),
        ):
            action = RIL_server.run_supervised_processes(
                tray,
                server,
                wakeup,
                mock.Mock(),
                requested_stop=requested_stop,
                ial_worker_control=worker_control,
            )

        self.assertEqual(action, "stop")
        reused_process.terminate.assert_not_called()
        reused_process.kill.assert_not_called()
        reused_process.children.assert_not_called()
        self.assertEqual(worker_control.pid.value, 0)

    def test_dead_listener_waits_for_worker_self_identity_before_exact_stop(
        self,
    ):
        server = FakeProcess(False)
        server.pid = 2468
        worker_control = RIL_server._IalWorkerControl()
        worker_control.idle_event.clear()
        worker = mock.Mock()
        worker.create_time.return_value = 123.5

        def publish_during_handshake(timeout):
            self.assertEqual(
                timeout,
                RIL_server.IAL_IDENTITY_READY_TIMEOUT,
            )
            RIL_server._publish_ial_worker_identity(
                worker_control,
                pid=4321,
                create_time=123.5,
                owner_pid=server.pid,
            )
            worker_control.identity_ready_event.set()
            return True

        with (
            mock.patch.object(
                worker_control.identity_ready_event,
                "wait",
                side_effect=publish_during_handshake,
            ) as identity_wait,
            mock.patch.object(
                RIL_server.psutil,
                "Process",
                return_value=worker,
            ),
        ):
            RIL_server._stop_controlled_ial_worker(
                worker_control,
                server,
                join_timeout=5,
            )

        identity_wait.assert_called_once_with(
            RIL_server.IAL_IDENTITY_READY_TIMEOUT
        )
        worker.terminate.assert_called_once_with()
        worker.wait.assert_called_once_with(timeout=5)
        worker.kill.assert_not_called()
        self.assertTrue(worker_control.idle_event.is_set())
        self.assertEqual(worker_control.pid.value, 0)

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
    def test_stop_ial_process_reports_each_lifecycle_exception(self):
        cases = {
            "initial_is_alive": {
                "is_alive": OSError("is_alive failed"),
            },
            "terminate": {
                "is_alive": [True],
                "terminate": OSError("terminate failed"),
            },
            "join": {
                "is_alive": [True],
                "join": OSError("join failed"),
            },
            "kill": {
                "is_alive": [True, True],
                "kill": OSError("kill failed"),
            },
        }

        for name, behavior in cases.items():
            with self.subTest(name=name):
                process = mock.Mock()
                is_alive = behavior["is_alive"]
                if isinstance(is_alive, list):
                    process.is_alive.side_effect = is_alive
                else:
                    process.is_alive.side_effect = is_alive
                if "terminate" in behavior:
                    process.terminate.side_effect = behavior["terminate"]
                if "join" in behavior:
                    process.join.side_effect = behavior["join"]
                if "kill" in behavior:
                    process.kill.side_effect = behavior["kill"]

                with mock.patch.object(RIL_server, "ErrorLog"):
                    stopped = RIL_server._stop_ial_process(
                        process,
                        terminate_first=True,
                    )

                self.assertFalse(stopped)

    def test_unconfirmed_stop_keeps_identity_idle_and_slot_fail_closed(self):
        worker_control = RIL_server._IalWorkerControl()
        reader = mock.Mock()
        reader.poll.return_value = True
        reader.recv.return_value = "int_success"
        writer = mock.Mock()
        process = mock.Mock()
        process.pid = 4321

        def publish_worker_identity():
            RIL_server._publish_ial_worker_identity(
                worker_control,
                pid=4321,
                create_time=123.5,
                owner_pid=2468,
            )
            worker_control.identity_ready_event.set()

        process.start.side_effect = publish_worker_identity

        try:
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
                mock.patch.object(
                    RIL_server,
                    "_stop_ial_process",
                    return_value=False,
                ),
                mock.patch.object(
                    RIL_server.os,
                    "getpid",
                    return_value=2468,
                ),
                mock.patch.object(RIL_server.os, "_exit") as hard_exit,
                mock.patch.object(RIL_server, "ErrorLog"),
                self.assertRaises(
                    RIL_server._UnconfirmedIalWorkerTermination
                ),
            ):
                RIL_server.execute_ial_with_hard_timeout(
                    "user1",
                    "password1",
                    "AU3",
                    timeout=7,
                    worker_control=worker_control,
                )

            hard_exit.assert_called_once_with(70)
            self.assertEqual(
                RIL_server._read_ial_worker_identity(worker_control),
                (4321, 123.5, 2468),
            )
            self.assertFalse(worker_control.idle_event.is_set())
            self.assertFalse(worker_control.start_lock.acquire(False))
        finally:
            RIL_server._clear_ial_worker_identity(worker_control)
            worker_control.idle_event.set()
            worker_control.start_lock.release()

    def test_failed_identity_handshake_keeps_slot_when_stop_is_unconfirmed(
        self,
    ):
        worker_control = RIL_server._IalWorkerControl()
        process = mock.Mock()
        process.pid = 4321

        def publish_wrong_identity():
            RIL_server._publish_ial_worker_identity(
                worker_control,
                pid=9999,
                create_time=123.5,
                owner_pid=2468,
            )
            worker_control.identity_ready_event.set()

        process.start.side_effect = publish_wrong_identity

        try:
            with (
                mock.patch.object(
                    RIL_server,
                    "_stop_ial_process",
                    return_value=False,
                ),
                mock.patch.object(
                    RIL_server.os,
                    "getpid",
                    return_value=2468,
                ),
                mock.patch.object(RIL_server.os, "_exit") as hard_exit,
                mock.patch.object(RIL_server, "ErrorLog"),
                self.assertRaises(
                    RIL_server._UnconfirmedIalWorkerTermination
                ),
            ):
                RIL_server._start_ial_process(
                    process,
                    worker_control,
                )

            hard_exit.assert_called_once_with(70)
            self.assertEqual(
                RIL_server._read_ial_worker_identity(worker_control),
                (9999, 123.5, 2468),
            )
            self.assertFalse(worker_control.idle_event.is_set())
            self.assertFalse(worker_control.start_lock.acquire(False))
        finally:
            RIL_server._clear_ial_worker_identity(worker_control)
            worker_control.idle_event.set()
            worker_control.start_lock.release()

    def test_cancel_before_start_closes_both_pipe_handles(self):
        worker_control = RIL_server._IalWorkerControl()
        worker_control.cancel_event.set()
        reader = mock.Mock()
        writer = mock.Mock()
        process = mock.Mock()

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
                worker_control=worker_control,
            )

        self.assertEqual(result, "int_failed")
        process.start.assert_not_called()
        reader.close.assert_called_once_with()
        writer.close.assert_called_once_with()

    def test_control_is_shareable_through_nested_spawn(self):
        spawn_context = multiprocessing.get_context("spawn")
        worker_control = RIL_server._IalWorkerControl()
        result_queue = spawn_context.Queue()
        listener = spawn_context.Process(
            target=_run_nested_controlled_ial,
            args=(worker_control, result_queue),
        )

        listener.start()
        listener.join(timeout=15)
        if listener.is_alive():
            listener.kill()
            listener.join(timeout=5)
            self.fail("nested controlled IAL process did not finish")

        self.assertEqual(listener.exitcode, 0)
        self.assertEqual(result_queue.get(timeout=1), "int_failed")
        self.assertTrue(worker_control.idle_event.is_set())
        self.assertEqual(worker_control.pid.value, 0)

    def test_parent_accepts_identity_published_by_the_worker(self):
        worker_control = RIL_server._IalWorkerControl()
        process = mock.Mock()
        process.pid = 4321

        def publish_worker_identity():
            RIL_server._publish_ial_worker_identity(
                worker_control,
                pid=4321,
                create_time=123.5,
                owner_pid=2468,
            )
            worker_control.identity_ready_event.set()

        process.start.side_effect = publish_worker_identity

        try:
            with mock.patch.object(
                RIL_server.os,
                "getpid",
                return_value=2468,
            ):
                started = RIL_server._start_ial_process(
                    process,
                    worker_control,
                )

            self.assertTrue(started)
            self.assertFalse(worker_control.idle_event.is_set())
            self.assertTrue(
                worker_control.identity_ready_event.is_set()
            )
            self.assertEqual(
                RIL_server._read_ial_worker_identity(worker_control),
                (4321, 123.5, 2468),
            )
            process.start.assert_called_once_with()
        finally:
            RIL_server._clear_ial_worker_identity(worker_control)
            worker_control.idle_event.set()
            RIL_server._release_ial_worker_slot(worker_control)

    def test_control_serializes_ial_workers_until_the_first_is_reaped(self):
        worker_control = RIL_server._IalWorkerControl()
        first = mock.Mock()
        first.pid = 4321
        second = mock.Mock()
        second.pid = 4322
        second_attempting = threading.Event()
        second_result = []

        def publish_identity(process):
            def publish():
                RIL_server._publish_ial_worker_identity(
                    worker_control,
                    pid=process.pid,
                    create_time=float(process.pid),
                    owner_pid=2468,
                )
                worker_control.identity_ready_event.set()

            return publish

        first.start.side_effect = publish_identity(first)
        second.start.side_effect = publish_identity(second)

        def start_second():
            second_attempting.set()
            second_result.append(
                RIL_server._start_ial_process(
                    second,
                    worker_control,
                )
            )

        with mock.patch.object(
            RIL_server.os,
            "getpid",
            return_value=2468,
        ):
            self.assertTrue(
                RIL_server._start_ial_process(
                    first,
                    worker_control,
                )
            )
            second_thread = threading.Thread(target=start_second)
            second_thread.start()
            self.assertTrue(second_attempting.wait(timeout=1))
            second.start.assert_not_called()

            RIL_server._clear_ial_worker_identity(worker_control)
            worker_control.idle_event.set()
            RIL_server._release_ial_worker_slot(worker_control)
            second_thread.join(timeout=1)

        self.assertFalse(second_thread.is_alive())
        self.assertEqual(second_result, [True])
        second.start.assert_called_once_with()
        RIL_server._clear_ial_worker_identity(worker_control)
        worker_control.idle_event.set()
        RIL_server._release_ial_worker_slot(worker_control)

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

    def test_cancel_event_is_reaped_by_the_owning_listener(self):
        worker_control = RIL_server._IalWorkerControl()
        reader = mock.Mock()

        def wait_for_result(_timeout):
            worker_control.cancel_event.set()
            return False

        reader.poll.side_effect = wait_for_result
        writer = mock.Mock()
        process = mock.Mock()
        process.pid = 4321
        process.is_alive.side_effect = [
            True,
            False,
            False,
            False,
            False,
        ]
        worker_process = mock.Mock()
        worker_process.create_time.return_value = 123.5

        def publish_worker_identity():
            RIL_server._publish_ial_worker_identity(
                worker_control,
                pid=4321,
                create_time=123.5,
                owner_pid=2468,
            )
            worker_control.identity_ready_event.set()

        process.start.side_effect = publish_worker_identity

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
            mock.patch.object(
                RIL_server.psutil,
                "Process",
                return_value=worker_process,
            ),
            mock.patch.object(
                RIL_server.os,
                "getpid",
                return_value=2468,
            ),
            mock.patch.object(RIL_server, "ErrorLog"),
        ):
            result = RIL_server.execute_ial_with_hard_timeout(
                "user1",
                "password1",
                "AU3",
                timeout=7,
                worker_control=worker_control,
            )

        self.assertEqual(result, "int_failed")
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()
        self.assertTrue(worker_control.idle_event.is_set())
        self.assertEqual(worker_control.pid.value, 0)

    def test_ial_worker_self_publishes_and_validates_exact_identity(self):
        worker_control = RIL_server._IalWorkerControl()
        result_connection = mock.Mock()
        current_process = mock.Mock()
        current_process.create_time.return_value = 123.5
        with (
            mock.patch.object(
                RIL_server.os,
                "getpid",
                return_value=4321,
            ),
            mock.patch.object(
                RIL_server.os,
                "getppid",
                return_value=2468,
            ),
            mock.patch.object(
                RIL_server.psutil,
                "Process",
                return_value=current_process,
            ),
            mock.patch.object(
                RIL_server,
                "_dispatch_ial_command",
                return_value="int_success",
            ),
        ):
            RIL_server._ial_request_worker(
                result_connection,
                "user1",
                "password1",
                "AU3",
                worker_control,
            )

        identity = RIL_server._read_ial_worker_identity(worker_control)
        self.assertEqual(identity, (4321, 123.5, 2468))
        result_connection.send.assert_called_once_with("int_success")
        result_connection.close.assert_called_once_with()

    def test_ial_worker_does_not_run_without_published_identity(self):
        worker_control = RIL_server._IalWorkerControl()
        worker_control.cancel_event.set()
        result_connection = mock.Mock()

        with mock.patch.object(
            RIL_server,
            "_dispatch_ial_command",
        ) as dispatch:
            RIL_server._ial_request_worker(
                result_connection,
                "user1",
                "password1",
                "AU3",
                worker_control,
            )

        dispatch.assert_not_called()
        result_connection.send.assert_called_once_with("int_failed")


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
