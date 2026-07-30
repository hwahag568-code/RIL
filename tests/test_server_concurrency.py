import socket
import struct
import threading
import time
import unittest
from unittest import mock

import mynetlib
import RIL_server


class ServerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        with RIL_server._result_cache_lock:
            RIL_server.legacy_results.clear()
            RIL_server.direct_results.clear()
            RIL_server.direct_inflight.clear()

    @staticmethod
    def _available_port():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
        finally:
            probe.close()

    def test_busy_request_finishes_without_waiting_for_active_login(self):
        port = self._available_port()
        listening = threading.Event()
        stop_server = threading.Event()
        execute_started = threading.Event()
        release_execute = threading.Event()
        first_result = []
        first_errors = []
        duplicate_result = []
        duplicate_errors = []
        server_errors = []

        def execute_ial(*_args):
            execute_started.set()
            if not release_execute.wait(timeout=5):
                raise TimeoutError("test did not release IAL")
            return "int_success"

        def serve():
            try:
                mynetlib.run_server(
                    port,
                    RIL_server.do_work_server_safely,
                    s_count=None,
                    client_timeout=2,
                    on_listening=listening.set,
                    stop_event=stop_server,
                    accept_poll_interval=0.02,
                    concurrent_handlers=True,
                )
            except Exception as error:
                server_errors.append(error)

        def run_first_request():
            try:
                first_result.append(
                    mynetlib.request_response(
                        "127.0.0.1",
                        port,
                        [
                            "user1",
                            "password1",
                            "AU3",
                            "request-first",
                        ],
                        connect_timeout=1,
                        send_timeout=1,
                        response_timeout=5,
                    )
                )
            except Exception as error:
                first_errors.append(error)

        def run_duplicate_request():
            try:
                duplicate_result.append(
                    mynetlib.request_response(
                        "127.0.0.1",
                        port,
                        [
                            "user1",
                            "password1",
                            "AU3",
                            "request-first",
                        ],
                        connect_timeout=1,
                        send_timeout=1,
                        response_timeout=5,
                    )
                )
            except Exception as error:
                duplicate_errors.append(error)

        with mock.patch.object(
            RIL_server,
            "execute_ial_with_hard_timeout",
            side_effect=execute_ial,
        ) as execute:
            server_thread = threading.Thread(target=serve)
            server_thread.start()
            self.assertTrue(listening.wait(timeout=2))

            first_thread = threading.Thread(target=run_first_request)
            first_thread.start()
            self.assertTrue(execute_started.wait(timeout=2))

            duplicate_thread = threading.Thread(
                target=run_duplicate_request
            )
            duplicate_thread.start()
            time.sleep(0.05)
            self.assertTrue(
                duplicate_thread.is_alive(),
                "동일 request_id 재접속이 원 작업 완료를 기다리지 않았습니다.",
            )

            busy_response = mynetlib.request_response(
                "127.0.0.1",
                port,
                [
                    "user2",
                    "password2",
                    "AU3",
                    "request-busy",
                ],
                connect_timeout=1,
                send_timeout=1,
                response_timeout=1,
            )
            conflict_response = mynetlib.request_response(
                "127.0.0.1",
                port,
                [
                    "different-user",
                    "password1",
                    "AU3",
                    "request-first",
                ],
                connect_timeout=1,
                send_timeout=1,
                response_timeout=1,
            )
            RIL_server._store_direct_result(
                ("127.0.0.1", 0),
                "request-replay",
                ("user3", "password3", "AU3"),
                "127.0.0.1",
                "int_success",
            )
            replay_response = mynetlib.request_response(
                "127.0.0.1",
                port,
                [
                    "user3",
                    "password3",
                    "AU3",
                    "request-replay",
                ],
                connect_timeout=1,
                send_timeout=1,
                response_timeout=1,
            )
            RIL_server._store_legacy_result(
                ("127.0.0.1", 0),
                "legacy-user",
                "legacy-password",
                "int_success",
            )
            legacy_response = mynetlib.request_response(
                "127.0.0.1",
                port,
                [
                    "legacy-user",
                    "legacy-password",
                    RIL_server.LEGACY_RESULT_COMMAND,
                ],
                connect_timeout=1,
                send_timeout=1,
                response_timeout=1,
            )
            capabilities = mynetlib.request_response(
                "127.0.0.1",
                port,
                ["", "", RIL_server.CAPABILITIES_COMMAND],
                connect_timeout=1,
                send_timeout=1,
                response_timeout=1,
            )

            self.assertTrue(first_thread.is_alive())
            self.assertEqual(
                busy_response[1:],
                (RIL_server.BUSY_RESULT_CODE, "request-busy"),
            )
            self.assertEqual(
                conflict_response[1:],
                ("int_failed", "request-first"),
            )
            self.assertEqual(
                replay_response,
                ("127.0.0.1", "int_success", "request-replay"),
            )
            self.assertEqual(
                legacy_response,
                ("127.0.0.1", "int_success"),
            )
            self.assertEqual(capabilities["type"], "capabilities")
            execute.assert_called_once_with(
                "user1",
                "password1",
                "AU3",
            )

            stop_server.set()
            time.sleep(0.08)
            self.assertTrue(
                server_thread.is_alive(),
                "활성 handler 종료 전에 drain이 완료됐습니다.",
            )

            release_execute.set()
            first_thread.join(timeout=2)
            duplicate_thread.join(timeout=2)
            server_thread.join(timeout=2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(duplicate_thread.is_alive())
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(duplicate_errors, [])
        self.assertEqual(server_errors, [])
        self.assertEqual(
            first_result,
            [("127.0.0.1", "int_success", "request-first")],
        )
        self.assertEqual(
            duplicate_result,
            [("127.0.0.1", "int_success", "request-first")],
        )

    def test_busy_legacy_request_stores_compatible_failure(self):
        address = ("10.2.151.10", 50000)
        self.assertTrue(
            RIL_server._ial_execution_lock.acquire(blocking=False)
        )
        try:
            with (
                mock.patch.object(
                    RIL_server.mynetlib,
                    "my_recv",
                    return_value=["user2", "password2", "AU3"],
                ),
                mock.patch.object(
                    RIL_server.mynetlib,
                    "my_send",
                ) as my_send,
                mock.patch.object(
                    RIL_server.socket,
                    "gethostbyname",
                    return_value="10.2.151.219",
                ),
                mock.patch.object(
                    RIL_server,
                    "execute_ial_with_hard_timeout",
                ) as execute,
            ):
                RIL_server.do_work_server(object(), address)
        finally:
            RIL_server._ial_execution_lock.release()

        execute.assert_not_called()
        my_send.assert_not_called()
        self.assertEqual(
            RIL_server._peek_legacy_result(
                address,
                "user2",
                "password2",
            ),
            "int_failed",
        )

    def test_reset_connection_retries_same_inflight_request(self):
        port = self._available_port()
        listening = threading.Event()
        stop_server = threading.Event()
        execute_started = threading.Event()
        release_execute = threading.Event()
        retry_result = []
        retry_errors = []
        server_errors = []

        def execute_ial(*_args):
            execute_started.set()
            if not release_execute.wait(timeout=5):
                raise TimeoutError("test did not release IAL")
            return "int_success"

        def serve():
            try:
                mynetlib.run_server(
                    port,
                    RIL_server.do_work_server_safely,
                    s_count=None,
                    client_timeout=2,
                    on_listening=listening.set,
                    stop_event=stop_server,
                    accept_poll_interval=0.02,
                    concurrent_handlers=True,
                )
            except Exception as error:
                server_errors.append(error)

        def retry_request():
            try:
                retry_result.append(
                    mynetlib.request_response(
                        "127.0.0.1",
                        port,
                        [
                            "user1",
                            "password1",
                            "AU3",
                            "request-reset",
                        ],
                        connect_timeout=1,
                        send_timeout=1,
                        response_timeout=5,
                    )
                )
            except Exception as error:
                retry_errors.append(error)

        raw_client = None
        retry_thread = None
        with mock.patch.object(
            RIL_server,
            "execute_ial_with_hard_timeout",
            side_effect=execute_ial,
        ) as execute:
            server_thread = threading.Thread(target=serve)
            server_thread.start()
            try:
                self.assertTrue(listening.wait(timeout=2))
                raw_client = socket.socket(
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                )
                raw_client.settimeout(1)
                raw_client.connect(("127.0.0.1", port))
                mynetlib.my_send(
                    [
                        "user1",
                        "password1",
                        "AU3",
                        "request-reset",
                    ],
                    raw_client,
                )
                self.assertTrue(execute_started.wait(timeout=2))

                raw_client.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("HH", 1, 0),
                )
                raw_client.close()
                raw_client = None

                retry_thread = threading.Thread(target=retry_request)
                retry_thread.start()
                time.sleep(0.05)
                self.assertTrue(
                    retry_thread.is_alive(),
                    "연결 유실 재시도가 원 작업 완료를 기다리지 않았습니다.",
                )

                release_execute.set()
                retry_thread.join(timeout=2)
                stop_server.set()
                server_thread.join(timeout=2)
            finally:
                release_execute.set()
                stop_server.set()
                if raw_client is not None:
                    raw_client.close()
                if retry_thread is not None:
                    retry_thread.join(timeout=2)
                server_thread.join(timeout=2)

        self.assertFalse(server_thread.is_alive())
        self.assertIsNotNone(retry_thread)
        self.assertFalse(retry_thread.is_alive())
        self.assertEqual(server_errors, [])
        self.assertEqual(retry_errors, [])
        self.assertEqual(
            retry_result,
            [("127.0.0.1", "int_success", "request-reset")],
        )
        execute.assert_called_once_with(
            "user1",
            "password1",
            "AU3",
        )

    def test_concurrent_legacy_polls_claim_different_results(self):
        address = ("10.2.151.10", 50000)
        RIL_server._store_legacy_result(
            address,
            "legacy-user",
            "legacy-password",
            "int_success",
        )
        RIL_server._store_legacy_result(
            address,
            "legacy-user",
            "legacy-password",
            "int_failed_1",
        )
        send_barrier = threading.Barrier(2)
        responses = []
        errors = []

        def send_response(response, _client):
            send_barrier.wait(timeout=2)
            responses.append(response)

        def poll_result():
            try:
                RIL_server.do_work_server(object(), address)
            except Exception as error:
                errors.append(error)

        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=[
                    "legacy-user",
                    "legacy-password",
                    RIL_server.LEGACY_RESULT_COMMAND,
                ],
            ),
            mock.patch.object(
                RIL_server.mynetlib,
                "my_send",
                side_effect=send_response,
            ),
            mock.patch.object(
                RIL_server.socket,
                "gethostbyname",
                return_value="10.2.151.219",
            ),
        ):
            poll_threads = [
                threading.Thread(target=poll_result)
                for _ in range(2)
            ]
            for thread in poll_threads:
                thread.start()
            for thread in poll_threads:
                thread.join(timeout=2)

        self.assertTrue(
            all(not thread.is_alive() for thread in poll_threads)
        )
        self.assertEqual(errors, [])
        self.assertCountEqual(
            responses,
            [
                ("10.2.151.219", "int_success"),
                ("10.2.151.219", "int_failed_1"),
            ],
        )
        self.assertIsNone(
            RIL_server._peek_legacy_result(
                address,
                "legacy-user",
                "legacy-password",
            )
        )

    def test_server_listener_uses_concurrent_handlers(self):
        drain_complete = threading.Event()

        with mock.patch.object(
            RIL_server.mynetlib,
            "run_server",
        ) as run_server:
            RIL_server.run_server2(
                drain_complete_event=drain_complete,
            )

        self.assertTrue(drain_complete.is_set())
        self.assertTrue(
            run_server.call_args.kwargs["concurrent_handlers"]
        )


if __name__ == "__main__":
    unittest.main()
