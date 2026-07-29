import unittest
from unittest import mock

import client
import RIL_server


class ClientRequestCorrelationTests(unittest.TestCase):
    def setUp(self):
        client._capability_cache.clear()
        client._capability_locks.clear()

    def test_new_server_returns_result_for_the_same_request_id(self):
        capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
        }
        direct_response = (
            "10.2.151.219",
            "int_success",
            "request-a",
        )

        with (
            mock.patch.object(
                client,
                "get_server_capabilities",
                return_value=capabilities,
            ),
            mock.patch.object(
                client.uuid,
                "uuid4",
                return_value=mock.Mock(hex="request-a"),
            ),
            mock.patch.object(
                client.mynetlib,
                "request_response",
                return_value=direct_response,
            ) as request_response,
        ):
            result = client.run_login(
                "10.2.151.219",
                "user1",
                "password1",
                "AU3",
            )

        self.assertEqual(
            result,
            ("10.2.151.219", "int_success"),
        )
        request_response.assert_called_once_with(
            "10.2.151.219",
            client.PORT,
            ["user1", "password1", "AU3", "request-a"],
            response_timeout=client.LOGIN_RESPONSE_TIMEOUT,
        )

    def test_new_server_rejects_a_result_for_another_request(self):
        capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
        }

        with (
            mock.patch.object(
                client,
                "get_server_capabilities",
                return_value=capabilities,
            ),
            mock.patch.object(
                client.uuid,
                "uuid4",
                return_value=mock.Mock(hex="request-a"),
            ),
            mock.patch.object(
                client.mynetlib,
                "request_response",
                return_value=(
                    "10.2.151.219",
                    "int_success",
                    "request-b",
                ),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "요청 ID가 일치하지 않습니다",
            ):
                client.run_login(
                    "10.2.151.219",
                    "user1",
                    "password1",
                    "AU3",
                )

    def test_old_server_uses_the_legacy_result_request(self):
        legacy_result = (
            "10.2.151.219",
            "int_success",
        )

        with (
            mock.patch.object(
                client,
                "get_server_capabilities",
                return_value={},
            ),
            mock.patch.object(client, "run_client2") as run_client2,
            mock.patch.object(
                client,
                "listen_server",
                return_value=legacy_result,
            ) as listen_server,
        ):
            result = client.run_login(
                "10.2.151.219",
                "user1",
                "password1",
                "AU3",
            )

        self.assertEqual(result, legacy_result)
        run_client2.assert_called_once_with(
            "10.2.151.219",
            "user1",
            "password1",
            "AU3",
        )
        listen_server.assert_called_once_with(
            "10.2.151.219",
            "user1",
            "password1",
            "R",
        )

    def test_direct_response_failure_does_not_restart_via_legacy(self):
        capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
        }

        with (
            mock.patch.object(
                client,
                "get_server_capabilities",
                return_value=capabilities,
            ),
            mock.patch.object(
                client.uuid,
                "uuid4",
                return_value=mock.Mock(hex="request-a"),
            ),
            mock.patch.object(
                client.mynetlib,
                "request_response",
                return_value=None,
            ),
            mock.patch.object(client, "run_client2") as run_client2,
        ):
            with self.assertRaisesRegex(
                client.LoginResultUncertainError,
                "최종 로그인 결과를 확인하지 못했습니다",
            ) as raised:
                client.run_login(
                    "10.2.151.219",
                    "user1",
                    "password1",
                    "AU3",
                )

        self.assertEqual(raised.exception.request_id, "request-a")
        run_client2.assert_not_called()

    def test_capability_timeout_does_not_fall_back_to_legacy(self):
        with (
            mock.patch.object(
                client.mynetlib,
                "request_response",
                return_value=None,
            ),
            mock.patch.object(client, "run_client2") as run_client2,
        ):
            with self.assertRaisesRegex(
                client.CapabilityProbeError,
                "서버 기능 확인 응답이 없습니다",
            ):
                client.run_login(
                    "10.2.151.219",
                    "user1",
                    "password1",
                    "AU3",
                )

        run_client2.assert_not_called()

    def test_explicit_old_server_response_uses_au_legacy_aliases(self):
        for program, legacy_program in (
            ("AU1", "AU22"),
            ("AU2", "AU32"),
        ):
            with self.subTest(program=program):
                with (
                    mock.patch.object(
                        client.mynetlib,
                        "request_response",
                        return_value=(
                            "10.2.151.219",
                            "int_failed",
                        ),
                    ),
                    mock.patch.object(
                        client,
                        "run_client2",
                    ) as run_client2,
                    mock.patch.object(
                        client,
                        "listen_server",
                        return_value=(
                            "10.2.151.219",
                            "int_success",
                        ),
                    ),
                ):
                    client.run_login(
                        "10.2.151.219",
                        "user1",
                        "password1",
                        program,
                    )

                run_client2.assert_called_once_with(
                    "10.2.151.219",
                    "user1",
                    "password1",
                    legacy_program,
                )

    def test_successful_capabilities_are_cached_per_server(self):
        capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
        }

        with mock.patch.object(
            client.mynetlib,
            "request_response",
            return_value=capabilities,
        ) as request_response:
            first = client.get_server_capabilities("10.2.151.219")
            second = client.get_server_capabilities("10.2.151.219")

        self.assertEqual(first, capabilities)
        self.assertEqual(second, capabilities)
        request_response.assert_called_once()

    def test_explicit_old_server_capability_is_cached_with_ttl(self):
        old_server_response = (
            "10.2.151.219",
            "int_failed",
        )

        with mock.patch.object(
            client.mynetlib,
            "request_response",
            return_value=old_server_response,
        ) as request_response:
            first = client.get_server_capabilities("10.2.151.219")
            second = client.get_server_capabilities("10.2.151.219")

        self.assertEqual(first, {})
        self.assertEqual(second, {})
        request_response.assert_called_once()

    def test_capability_cache_uses_ttl_and_reprobes_after_expiry(self):
        first_capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
            "probe": "first",
        }
        refreshed_capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
            "probe": "refreshed",
        }

        with (
            mock.patch.object(
                client.time,
                "monotonic",
                side_effect=[
                    100.0,
                    100.0 + client.CAPABILITY_CACHE_TTL - 0.1,
                    100.0 + client.CAPABILITY_CACHE_TTL,
                ],
            ),
            mock.patch.object(
                client.mynetlib,
                "request_response",
                side_effect=[
                    first_capabilities,
                    refreshed_capabilities,
                ],
            ) as request_response,
        ):
            first = client.get_server_capabilities("10.2.151.219")
            cached = client.get_server_capabilities("10.2.151.219")
            refreshed = client.get_server_capabilities("10.2.151.219")

        self.assertEqual(first, first_capabilities)
        self.assertEqual(cached, first_capabilities)
        self.assertEqual(refreshed, refreshed_capabilities)
        self.assertEqual(request_response.call_count, 2)

    def test_direct_connection_failure_invalidates_capability_cache(self):
        ip = "10.2.151.219"
        capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
        }

        with (
            mock.patch.object(
                client.mynetlib,
                "request_response",
                side_effect=[
                    capabilities,
                    ConnectionError("server replaced"),
                    ConnectionError("server replaced"),
                ],
            ) as request_response,
            mock.patch.object(client, "run_client2") as run_client2,
        ):
            with self.assertRaisesRegex(
                client.LoginResultUncertainError,
                "server replaced",
            ):
                client.run_login(
                    ip,
                    "user1",
                    "password1",
                    "AU3",
                )

        self.assertNotIn(ip, client._capability_cache)
        self.assertEqual(request_response.call_count, 3)
        run_client2.assert_not_called()

    def test_direct_connection_failure_retries_same_request_id_once(self):
        ip = "10.2.151.219"
        capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
        }
        direct_response = (ip, "int_success", "request-a")

        with (
            mock.patch.object(
                client.uuid,
                "uuid4",
                return_value=mock.Mock(hex="request-a"),
            ),
            mock.patch.object(
                client.mynetlib,
                "request_response",
                side_effect=[
                    capabilities,
                    TimeoutError("response timed out"),
                    direct_response,
                ],
            ) as request_response,
            mock.patch.object(client, "run_client2") as run_client2,
        ):
            result = client.run_login(
                ip,
                "user1",
                "password1",
                "AU3",
            )

        self.assertEqual(result, (ip, "int_success"))
        self.assertNotIn(ip, client._capability_cache)
        self.assertEqual(request_response.call_count, 3)
        first_direct_call = request_response.call_args_list[1]
        retry_direct_call = request_response.call_args_list[2]
        self.assertEqual(
            first_direct_call.args,
            retry_direct_call.args,
        )
        self.assertEqual(
            first_direct_call.args[2][-1],
            "request-a",
        )
        first_timeout = first_direct_call.kwargs["response_timeout"]
        retry_timeout = retry_direct_call.kwargs["response_timeout"]
        self.assertGreater(retry_timeout, 0)
        self.assertLessEqual(retry_timeout, first_timeout)
        run_client2.assert_not_called()

    def test_invalid_direct_response_invalidates_capability_cache(self):
        ip = "10.2.151.219"
        capabilities = {
            "type": "capabilities",
            "protocol_version": client.PROTOCOL_VERSION,
            "features": ["request_id", "direct_result"],
        }

        with (
            mock.patch.object(
                client.mynetlib,
                "request_response",
                side_effect=[capabilities, None, None],
            ),
            mock.patch.object(client, "run_client2") as run_client2,
        ):
            with self.assertRaisesRegex(
                client.LoginResultUncertainError,
                "최종 로그인 결과를 확인하지 못했습니다",
            ):
                client.run_login(
                    ip,
                    "user1",
                    "password1",
                    "AU3",
                )

        self.assertNotIn(ip, client._capability_cache)
        run_client2.assert_not_called()


class ServerRequestCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.client_socket = object()
        RIL_server.legacy_results.clear()
        RIL_server.direct_results.clear()

    def test_capability_request_advertises_direct_result_support(self):
        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=["", "", "CAPS"],
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
        ):
            RIL_server.do_work_server(
                self.client_socket,
                ("10.2.151.10", 50000),
            )

        response = my_send.call_args.args[0]
        self.assertEqual(response["type"], "capabilities")
        self.assertEqual(
            response["protocol_version"],
            RIL_server.PROTOCOL_VERSION,
        )
        self.assertIn("request_id", response["features"])
        self.assertIn("direct_result", response["features"])

    def test_server_sends_status_with_the_same_request_id(self):
        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=[
                    "user1",
                    "password1",
                    "AU3",
                    "request-a",
                ],
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
                return_value="int_success",
            ) as execute_ial,
        ):
            RIL_server.do_work_server(
                self.client_socket,
                ("10.2.151.10", 50000),
            )

        execute_ial.assert_called_once_with(
            "user1",
            "password1",
            "AU3",
        )
        my_send.assert_called_once_with(
            (
                "10.2.151.219",
                "int_success",
                "request-a",
            ),
            self.client_socket,
        )

    def test_server_accepts_old_au_command_aliases(self):
        for command in ("AU22", "AU32"):
            with self.subTest(command=command):
                RIL_server.legacy_results.clear()
                with (
                    mock.patch.object(
                        RIL_server.mynetlib,
                        "my_recv",
                        return_value=[
                            "user1",
                            "password1",
                            command,
                        ],
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
                        return_value="int_success",
                    ) as execute_ial,
                ):
                    RIL_server.do_work_server(
                        self.client_socket,
                        ("10.2.151.10", 50000),
                    )

                execute_ial.assert_called_once_with(
                    "user1",
                    "password1",
                    command,
                )
                my_send.assert_not_called()

    def test_dispatch_maps_old_au_aliases_to_current_numbers(self):
        for command, au_number in (
            ("AU22", 1),
            ("AU32", 2),
        ):
            with (
                self.subTest(command=command),
                mock.patch.object(
                    RIL_server.IAL,
                    "StartTaskAU",
                    return_value="int_success",
                ) as start_task,
            ):
                result = RIL_server._dispatch_ial_command(
                    "user1",
                    "password1",
                    command,
                )

            self.assertEqual(result, "int_success")
            start_task.assert_called_once_with(
                "user1",
                "password1",
                au_number,
            )

    def test_legacy_command_does_not_receive_a_direct_result(self):
        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=[
                    "user1",
                    "password1",
                    "AU3",
                ],
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
                return_value="int_success",
            ),
        ):
            RIL_server.do_work_server(
                self.client_socket,
                ("10.2.151.10", 50000),
            )

        my_send.assert_not_called()

    def test_direct_request_does_not_overwrite_pending_legacy_result(self):
        address = ("10.2.151.10", 50000)

        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=[
                    "user1",
                    "password1",
                    "AU3",
                ],
            ),
            mock.patch.object(
                RIL_server.mynetlib,
                "my_send",
            ),
            mock.patch.object(
                RIL_server.socket,
                "gethostbyname",
                return_value="10.2.151.219",
            ),
            mock.patch.object(
                RIL_server,
                "execute_ial_with_hard_timeout",
                return_value="int_failed_1",
            ),
        ):
            RIL_server.do_work_server(
                self.client_socket,
                address,
            )

        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=[
                    "user1",
                    "password1",
                    "AU3",
                    "request-b",
                ],
            ),
            mock.patch.object(
                RIL_server.mynetlib,
                "my_send",
            ) as direct_send,
            mock.patch.object(
                RIL_server.socket,
                "gethostbyname",
                return_value="10.2.151.219",
            ),
            mock.patch.object(
                RIL_server,
                "execute_ial_with_hard_timeout",
                return_value="int_success",
            ),
        ):
            RIL_server.do_work_server(
                self.client_socket,
                address,
            )

        direct_send.assert_called_once_with(
            (
                "10.2.151.219",
                "int_success",
                "request-b",
            ),
            self.client_socket,
        )

        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=[
                    "user1",
                    "password1",
                    "R",
                ],
            ),
            mock.patch.object(
                RIL_server.mynetlib,
                "my_send",
            ) as legacy_send,
            mock.patch.object(
                RIL_server.socket,
                "gethostbyname",
                return_value="10.2.151.219",
            ),
        ):
            RIL_server.do_work_server(
                self.client_socket,
                address,
            )

        legacy_send.assert_called_once_with(
            (
                "10.2.151.219",
                "int_failed_1",
            ),
            self.client_socket,
        )

    def test_retry_with_same_request_id_replays_without_restarting(self):
        address = ("10.2.151.10", 50000)
        command = [
            "user1",
            "password1",
            "AU3",
            "request-retry",
        ]

        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=command,
            ),
            mock.patch.object(
                RIL_server.mynetlib,
                "my_send",
                side_effect=[BrokenPipeError("lost response"), None],
            ) as my_send,
            mock.patch.object(
                RIL_server.socket,
                "gethostbyname",
                return_value="10.2.151.219",
            ),
            mock.patch.object(
                RIL_server,
                "execute_ial_with_hard_timeout",
                return_value="int_success",
            ) as execute_ial,
        ):
            with self.assertRaises(BrokenPipeError):
                RIL_server.do_work_server(self.client_socket, address)
            RIL_server.do_work_server(self.client_socket, address)

        execute_ial.assert_called_once_with(
            "user1",
            "password1",
            "AU3",
        )
        self.assertEqual(my_send.call_count, 2)
        self.assertEqual(
            my_send.call_args.args[0],
            (
                "10.2.151.219",
                "int_success",
                "request-retry",
            ),
        )

    def test_failed_legacy_send_keeps_result_for_retry(self):
        address = ("10.2.151.10", 50000)
        RIL_server._store_legacy_result(
            address,
            "user1",
            "password1",
            "int_success",
        )

        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=["user1", "password1", "R"],
            ),
            mock.patch.object(
                RIL_server.mynetlib,
                "my_send",
                side_effect=BrokenPipeError("lost response"),
            ),
            mock.patch.object(
                RIL_server.socket,
                "gethostbyname",
                return_value="10.2.151.219",
            ),
            mock.patch.object(RIL_server.time, "sleep"),
        ):
            RIL_server.do_work_server(self.client_socket, address)

        self.assertEqual(
            RIL_server._peek_legacy_result(
                address,
                "user1",
                "password1",
            ),
            "int_success",
        )

    def test_missing_legacy_result_returns_without_serial_stall(self):
        address = ("10.2.151.10", 50000)

        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=["user1", "password1", "R"],
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
                RIL_server.time,
                "sleep",
            ) as sleep,
        ):
            RIL_server.do_work_server(self.client_socket, address)

        my_send.assert_not_called()
        sleep.assert_not_called()

    def test_new_legacy_login_discards_unclaimed_old_result(self):
        address = ("10.2.151.10", 50000)
        RIL_server._store_legacy_result(
            address,
            "user1",
            "password1",
            "int_failed",
        )

        with (
            mock.patch.object(
                RIL_server.mynetlib,
                "my_recv",
                return_value=["user1", "password1", "AU3"],
            ),
            mock.patch.object(RIL_server.mynetlib, "my_send"),
            mock.patch.object(
                RIL_server.socket,
                "gethostbyname",
                return_value="10.2.151.219",
            ),
            mock.patch.object(
                RIL_server,
                "execute_ial_with_hard_timeout",
                return_value="int_success",
            ),
        ):
            RIL_server.do_work_server(self.client_socket, address)

        self.assertEqual(
            RIL_server._peek_legacy_result(
                address,
                "user1",
                "password1",
            ),
            "int_success",
        )


if __name__ == "__main__":
    unittest.main()
