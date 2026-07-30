import socket
import unittest
from types import SimpleNamespace
from unittest import mock

import RIL_client


class FakeTextField:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


def make_window(interface_ips):
    window = SimpleNamespace()
    window.lineEdit_ID = FakeTextField("user1")
    window.lineEdit_PW = FakeTextField("password1")
    window.interface_list = ("Al1",)
    window.int_iplist = dict(interface_ips)
    window._is_checked = lambda name: name == "Al1"
    window._show_error = mock.Mock()
    window.ErrorLog = mock.Mock()
    window.threads = []
    return window


class LocalIpv4AddressTests(unittest.TestCase):
    def test_collects_ipv4_from_all_network_adapters(self):
        addresses = {
            "Management": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="192.168.10.20",
                ),
                SimpleNamespace(
                    family=socket.AF_INET6,
                    address="fe80::1",
                ),
            ],
            "Laboratory": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="10.2.151.53",
                ),
            ],
        }

        with mock.patch.object(
            RIL_client.psutil,
            "net_if_addrs",
            return_value=addresses,
        ):
            result = RIL_client.get_local_ipv4_addresses()

        self.assertEqual(
            result,
            {"192.168.10.20", "10.2.151.53"},
        )


class InterfacePcGuardTests(unittest.TestCase):
    def test_blocks_when_local_ipv4_matches_interface_ip(self):
        window = make_window({"Al1": "10.2.151.52"})

        with (
            mock.patch.object(
                RIL_client,
                "get_local_ipv4_addresses",
                return_value={"10.2.151.52"},
            ),
            mock.patch.object(
                RIL_client.QMessageBox,
                "question",
            ) as question,
            mock.patch.object(RIL_client, "ClientWorker") as worker,
        ):
            RIL_client.WindowClass.run_selectedPC(window)

        window._show_error.assert_called_once()
        self.assertEqual(
            window._show_error.call_args.args[0],
            "실행 제한",
        )
        question.assert_not_called()
        worker.assert_not_called()

    def test_blocks_when_secondary_ipv4_matches_interface_ip(self):
        window = make_window({"Al1": "10.2.151.52"})

        with (
            mock.patch.object(
                RIL_client,
                "get_local_ipv4_addresses",
                return_value={
                    "192.168.10.20",
                    "10.2.151.52",
                },
            ),
            mock.patch.object(
                RIL_client.QMessageBox,
                "question",
            ) as question,
        ):
            RIL_client.WindowClass.run_selectedPC(window)

        window._show_error.assert_called_once()
        question.assert_not_called()

    def test_non_interface_pc_continues_to_confirmation(self):
        window = make_window({"Al1": "10.2.151.52"})

        with (
            mock.patch.object(
                RIL_client,
                "get_local_ipv4_addresses",
                return_value={"192.168.10.20"},
            ),
            mock.patch.object(
                RIL_client.QMessageBox,
                "question",
                return_value=RIL_client.QMessageBox.No,
            ) as question,
        ):
            RIL_client.WindowClass.run_selectedPC(window)

        window._show_error.assert_not_called()
        question.assert_called_once()

    def test_address_lookup_failure_blocks_execution(self):
        window = make_window({"Al1": "10.2.151.52"})

        with (
            mock.patch.object(
                RIL_client,
                "get_local_ipv4_addresses",
                side_effect=OSError("adapter lookup failed"),
            ),
            mock.patch.object(
                RIL_client.QMessageBox,
                "question",
            ) as question,
            mock.patch.object(RIL_client, "ClientWorker") as worker,
        ):
            RIL_client.WindowClass.run_selectedPC(window)

        window._show_error.assert_called_once()
        self.assertEqual(
            window._show_error.call_args.args[0],
            "실행 제한 확인 오류",
        )
        window.ErrorLog.assert_called_once()
        question.assert_not_called()
        worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
