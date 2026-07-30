import socket
import threading
import time
import unittest
from unittest import mock

import client
import mynetlib
import RIL_server


class FakeClientSocket:
    def __init__(self, recv_values=None):
        self.recv_values = iter(recv_values or [])
        self.timeouts = []
        self.connected_to = None
        self.closed = False
        self.recv_calls = 0

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, address):
        self.connected_to = address

    def recv(self, _size):
        self.recv_calls += 1
        value = next(self.recv_values)
        if isinstance(value, BaseException):
            raise value
        return value

    def sendall(self, _data):
        return None

    def close(self):
        self.closed = True


class FakeListeningSocket:
    def __init__(self, accepted_client):
        self.accepted_client = accepted_client
        self.closed = False
        self.socket_options = []

    def setsockopt(self, *args):
        self.socket_options.append(args)

    def bind(self, _address):
        return None

    def listen(self, _backlog):
        return None

    def accept(self):
        return self.accepted_client, ("10.2.151.10", 50000)

    def close(self):
        self.closed = True


class ReceiveTimeoutTests(unittest.TestCase):
    def test_my_recv_propagates_socket_timeout(self):
        receiving_socket = FakeClientSocket(
            [socket.timeout("timed out")]
        )

        with self.assertRaises(socket.timeout):
            mynetlib.my_recv(1024, receiving_socket)

    def test_my_recv_rejects_an_oversized_message(self):
        receiving_socket = FakeClientSocket([b"12345"])

        with self.assertRaises(mynetlib.MessageTooLargeError):
            mynetlib.my_recv(
                1024,
                receiving_socket,
                max_bytes=4,
            )

    def test_my_recv_rejects_an_incomplete_closed_message(self):
        receiving_socket = FakeClientSocket(
            [b"not-a-pickle", b""]
        )

        with self.assertRaises(mynetlib.IncompleteMessageError):
            mynetlib.my_recv(1024, receiving_socket)

    def test_my_recv_enforces_an_absolute_total_timeout(self):
        receiving_socket = FakeClientSocket([b"x"])

        with (
            mock.patch.object(
                mynetlib.time,
                "monotonic",
                side_effect=[0, 0, 11],
            ),
            self.assertRaises(socket.timeout),
        ):
            mynetlib.my_recv(
                1024,
                receiving_socket,
                total_timeout=10,
            )

        self.assertEqual(receiving_socket.timeouts, [10])

    def test_my_send_rejects_an_oversized_message(self):
        sending_socket = FakeClientSocket()

        with self.assertRaises(mynetlib.MessageTooLargeError):
            mynetlib.my_send(
                b"x" * mynetlib.MAX_MESSAGE_SIZE,
                sending_socket,
            )

    def test_request_response_has_a_finite_read_timeout(self):
        request_socket = FakeClientSocket(
            [socket.timeout("timed out")]
        )

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                return_value=request_socket,
            ),
            mock.patch.object(
                mynetlib.time,
                "monotonic",
                side_effect=[0, 0, 0, 0, 0, 0],
            ),
            self.assertRaises(socket.timeout),
        ):
            mynetlib.request_response(
                "10.2.151.219",
                2023,
                ["", "", "CAPS"],
                connect_timeout=3,
                response_timeout=17,
            )

        self.assertEqual(request_socket.timeouts, [3, 5, 17, 17])
        self.assertTrue(request_socket.closed)

    def test_request_response_partial_data_cannot_extend_deadline(self):
        request_socket = FakeClientSocket([b"x"])

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                return_value=request_socket,
            ),
            mock.patch.object(
                mynetlib.time,
                "monotonic",
                side_effect=[0, 0, 0, 0, 0, 0, 18],
            ),
            self.assertRaises(socket.timeout),
        ):
            mynetlib.request_response(
                "10.2.151.219",
                2023,
                ["user1", "password1", "AU3", "request-a"],
                connect_timeout=3,
                send_timeout=5,
                response_timeout=17,
            )

        self.assertEqual(request_socket.recv_calls, 1)
        self.assertTrue(request_socket.closed)

    def test_legacy_result_receive_uses_finite_timeouts(self):
        result_socket = FakeClientSocket(
            [socket.timeout("timed out")]
        )

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                return_value=result_socket,
            ),
            mock.patch.object(
                mynetlib.time,
                "monotonic",
                side_effect=[0, 0, 0, 0, 0, 0, 0],
            ),
        ):
            result = mynetlib.recv_result(
                "10.2.151.219",
                2023,
                client.do_recv_result,
                "user1",
                "password1",
                "R",
                connect_timeout=3,
                response_timeout=12,
            )

        self.assertIsNone(result)
        self.assertEqual(
            result_socket.timeouts,
            [3, 12, 5, 12, 12],
        )
        self.assertTrue(result_socket.closed)

    def test_legacy_partial_data_cannot_extend_deadline(self):
        result_socket = FakeClientSocket([b"x"])

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                return_value=result_socket,
            ),
            mock.patch.object(
                mynetlib.time,
                "monotonic",
                side_effect=[0, 0, 0, 0, 0, 0, 0, 13],
            ),
        ):
            result = mynetlib.recv_result(
                "10.2.151.219",
                2023,
                client.do_recv_result,
                "user1",
                "password1",
                "R",
                connect_timeout=3,
                response_timeout=12,
            )

        self.assertIsNone(result)
        self.assertEqual(result_socket.recv_calls, 1)
        self.assertTrue(result_socket.closed)

    def test_legacy_result_does_not_start_a_read_after_deadline(self):
        result_socket = FakeClientSocket()

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                return_value=result_socket,
            ),
            mock.patch.object(
                mynetlib.time,
                "monotonic",
                return_value=21,
            ),
        ):
            result = mynetlib.recv_result(
                "10.2.151.219",
                2023,
                lambda *_args: None,
                "user1",
                "password1",
                "R",
                connect_timeout=3,
                response_timeout=12,
                deadline=20,
            )

        self.assertIsNone(result)
        self.assertEqual(result_socket.timeouts, [])
        self.assertIsNone(result_socket.connected_to)
        self.assertTrue(result_socket.closed)

    def test_command_send_uses_a_finite_timeout(self):
        command_socket = FakeClientSocket()

        with mock.patch.object(
            mynetlib.socket,
            "socket",
            return_value=command_socket,
        ):
            mynetlib.run_client(
                "10.2.151.219",
                2023,
                lambda *_args: None,
                "user1",
                "password1",
                "AU3",
                connect_timeout=3,
                send_timeout=5,
            )

        self.assertEqual(command_socket.timeouts, [3, 5])
        self.assertTrue(command_socket.closed)

    def test_command_retries_only_connect_phase_failures(self):
        first_socket = FakeClientSocket()
        first_socket.connect = mock.Mock(
            side_effect=ConnectionRefusedError("listener rebinding"),
        )
        second_socket = FakeClientSocket()
        send_command = mock.Mock()

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                side_effect=[first_socket, second_socket],
            ),
            mock.patch.object(mynetlib.time, "sleep") as sleep,
        ):
            mynetlib.run_client(
                "10.2.151.219",
                2023,
                send_command,
                "user1",
                "password1",
                "AU3",
                connect_attempts=2,
                connect_retry_delay=0.25,
            )

        self.assertTrue(first_socket.closed)
        self.assertTrue(second_socket.closed)
        sleep.assert_called_once_with(0.25)
        send_command.assert_called_once_with(
            second_socket,
            "user1",
            "password1",
            "AU3",
        )

    def test_command_does_not_retry_after_connection_and_send_failure(self):
        command_socket = FakeClientSocket()
        send_command = mock.Mock(
            side_effect=socket.timeout("send timed out"),
        )

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                return_value=command_socket,
            ) as socket_factory,
            self.assertRaises(socket.timeout),
        ):
            mynetlib.run_client(
                "10.2.151.219",
                2023,
                send_command,
                "user1",
                "password1",
                "AU3",
                connect_attempts=3,
            )

        socket_factory.assert_called_once_with(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )
        send_command.assert_called_once()


class ServerSocketLifetimeTests(unittest.TestCase):
    def test_run_server_closes_both_sockets_when_handler_fails(self):
        accepted_client = FakeClientSocket()
        listening_socket = FakeListeningSocket(accepted_client)

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                return_value=listening_socket,
            ),
            self.assertRaisesRegex(RuntimeError, "handler failed"),
        ):
            mynetlib.run_server(
                2023,
                lambda _client, _addr: (_ for _ in ()).throw(
                    RuntimeError("handler failed")
                ),
                1,
                client_timeout=10,
            )

        self.assertEqual(accepted_client.timeouts, [10])
        self.assertTrue(accepted_client.closed)
        self.assertTrue(listening_socket.closed)

    def test_windows_listener_uses_exclusive_port_ownership(self):
        accepted_client = FakeClientSocket()
        listening_socket = FakeListeningSocket(accepted_client)

        with (
            mock.patch.object(
                mynetlib.socket,
                "socket",
                return_value=listening_socket,
            ),
            mock.patch.object(
                mynetlib.socket,
                "SO_EXCLUSIVEADDRUSE",
                0x4000,
                create=True,
            ),
        ):
            mynetlib.run_server(
                2023,
                lambda *_args: None,
                s_count=1,
            )

        self.assertIn(
            (socket.SOL_SOCKET, 0x4000, 1),
            listening_socket.socket_options,
        )
        self.assertNotIn(
            (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
            listening_socket.socket_options,
        )

    def test_persistent_server_handles_many_sequential_requests(self):
        request_count = 100
        handled = []
        server_stopped = threading.Event()
        server_listening = threading.Event()

        class StopServer(Exception):
            pass

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        def handler(client_socket, _addr):
            command = mynetlib.my_recv(1024, client_socket)
            handled.append(command)
            mynetlib.my_send(("ok", command), client_socket)
            if len(handled) == request_count:
                raise StopServer()

        def serve():
            try:
                mynetlib.run_server(
                    port,
                    handler,
                    s_count=None,
                    client_timeout=2,
                    on_listening=server_listening.set,
                )
            except StopServer:
                server_stopped.set()

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()

        self.assertTrue(
            server_listening.wait(timeout=2),
            "서버 리스너가 제한 시간 안에 시작되지 않았습니다.",
        )
        responses = [
            mynetlib.request_response(
                "127.0.0.1",
                port,
                0,
                connect_timeout=0.1,
                send_timeout=1,
                response_timeout=1,
            )
        ]

        for command in range(1, request_count):
            responses.append(
                mynetlib.request_response(
                    "127.0.0.1",
                    port,
                    command,
                    connect_timeout=0.1,
                    send_timeout=1,
                    response_timeout=1,
                )
            )

        server_thread.join(timeout=2)

        self.assertTrue(server_stopped.is_set())
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(handled, list(range(request_count)))
        self.assertEqual(
            responses,
            [("ok", command) for command in range(request_count)],
        )

    def test_server_logs_and_releases_an_incomplete_request_timeout(self):
        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                side_effect=socket.timeout("timed out"),
            ),
            mock.patch.object(RIL_server, "ErrorLog") as error_log,
            mock.patch.object(
                RIL_server.socket,
                "gethostbyname",
                return_value="10.2.151.219",
            ),
        ):
            RIL_server.do_work_server(
                object(),
                ("10.2.151.10", 50000),
            )

        self.assertIn(
            "요청 수신 시간 초과",
            error_log.call_args.args[0],
        )


class LegacyResultDeadlineTests(unittest.TestCase):
    def test_legacy_result_polling_stops_at_the_total_deadline(self):
        with (
            mock.patch.object(
                client.mynetlib,
                "recv_result",
                return_value=None,
            ) as recv_result,
            mock.patch.object(
                client.time,
                "monotonic",
                side_effect=[0, 0, 241],
            ),
            mock.patch.object(client.time, "sleep") as sleep,
            self.assertRaises(client.LoginResultTimeoutError),
        ):
            client.listen_server(
                "10.2.151.219",
                "user1",
                "password1",
                "R",
            )

        recv_result.assert_called_once()
        call = recv_result.call_args
        self.assertEqual(
            call.args[:2],
            ("10.2.151.219", client.PORT),
        )
        self.assertTrue(callable(call.args[2]))
        self.assertEqual(
            call.args[3:],
            ("user1", "password1", "R"),
        )
        self.assertEqual(
            call.kwargs,
            {
                "connect_timeout": client.LEGACY_CONNECT_TIMEOUT,
                "response_timeout": client.LEGACY_READ_TIMEOUT,
                "deadline": client.LEGACY_TOTAL_TIMEOUT,
            },
        )
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
