import unittest
from types import SimpleNamespace
from unittest import mock

import RIL_client


class FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class FakeWorker:
    def __init__(self, ip, int_id, int_pw, prg, label_name):
        self.ip = ip
        self.int_id = int_id
        self.int_pw = int_pw
        self.prg = prg
        self.label_name = label_name
        self.result_signal = FakeSignal()
        self.finished = FakeSignal()
        self.started = False
        self.interruption_requested = False
        self.deleted = False

    def start(self):
        self.started = True

    def requestInterruption(self):
        self.interruption_requested = True

    def deleteLater(self):
        self.deleted = True

    def isRunning(self):
        return self.started and not self.deleted


class FakeBackgroundWorker:
    def __init__(self):
        self.running = True
        self.interruption_requested = False
        self.deleted = False

    def isRunning(self):
        return self.running

    def requestInterruption(self):
        self.interruption_requested = True

    def deleteLater(self):
        self.deleted = True


class FakeTextField:
    def __init__(self, value):
        self._value = value

    def text(self):
        return self._value


class FakeCheckBox:
    def __init__(self, checked):
        self._checked = checked

    def isChecked(self):
        return self._checked


class FakeLabel:
    def __init__(self):
        self.text = ""
        self.style = ""
        self.tooltip = ""

    def setText(self, text):
        self.text = text

    def setStyleSheet(self, style):
        self.style = style

    def setToolTip(self, text):
        self.tooltip = text


def make_window(selected=("Al1",)):
    window = SimpleNamespace()
    window.lineEdit_ID = FakeTextField("user1")
    window.lineEdit_PW = FakeTextField("password1")
    window.interface_list = ("Al1", "Al3")
    window.int_iplist = {
        "Al1": "10.2.151.52",
        "Al3": "10.2.151.53",
    }
    window.checkBox_Al1 = FakeCheckBox("Al1" in selected)
    window.checkBox_Al3 = FakeCheckBox("Al3" in selected)
    window.label_Al1 = FakeLabel()
    window.label_Al3 = FakeLabel()
    window._is_checked = lambda name: getattr(
        window,
        f"checkBox_{name}",
    ).isChecked()
    window._show_error = mock.Mock()
    window.ErrorLog = mock.Mock()
    window.threads = []
    return window


def run_selected(window, created_workers):
    def create_worker(*args):
        worker = FakeWorker(*args)
        created_workers.append(worker)
        return worker

    question = mock.Mock(return_value=RIL_client.QMessageBox.Yes)
    with (
        mock.patch.object(
            RIL_client,
            "get_local_ipv4_addresses",
            return_value={"192.168.10.20"},
        ),
        mock.patch.object(
            RIL_client.QMessageBox,
            "question",
            question,
        ),
        mock.patch.object(
            RIL_client,
            "ClientWorker",
            side_effect=create_worker,
        ),
    ):
        RIL_client.WindowClass.run_selectedPC(window)
    return question


class GuiWorkerLifecycleTests(unittest.TestCase):
    def test_client_worker_reports_an_uncertain_result_separately(self):
        worker = RIL_client.ClientWorker(
            "10.2.151.53",
            "user1",
            "password1",
            "INT",
            "Al3",
        )
        results = []
        worker.result_signal.connect(
            lambda device_id, result: results.append(
                (device_id, result)
            )
        )

        with mock.patch.object(
            RIL_client.client,
            "run_login",
            side_effect=RIL_client.client.LoginResultUncertainError(
                "응답을 확인하지 못했습니다.",
                "request-a",
            ),
        ):
            worker.run()

        self.assertEqual(results[0][0], "Al3")
        self.assertTrue(results[0][1].startswith("결과미확인:"))

    def test_client_worker_reports_server_busy_separately(self):
        worker = RIL_client.ClientWorker(
            "10.2.151.53",
            "user1",
            "password1",
            "INT",
            "Al3",
        )
        results = []
        worker.result_signal.connect(
            lambda device_id, result: results.append(
                (device_id, result)
            )
        )

        with mock.patch.object(
            RIL_client.client,
            "run_login",
            return_value=(
                "10.2.151.53",
                RIL_client.SERVER_BUSY_RESULT_CODE,
            ),
        ):
            worker.run()

        self.assertEqual(results, [("Al3", "서버 사용 중")])

    def test_same_device_is_not_started_twice_while_active(self):
        window = make_window()
        workers = []

        first_question = run_selected(window, workers)
        second_question = run_selected(window, workers)

        self.assertEqual(len(workers), 1)
        self.assertTrue(workers[0].started)
        self.assertIs(window._active_workers["Al1"], workers[0])
        self.assertEqual(window.threads, [workers[0]])
        first_question.assert_called_once()
        second_question.assert_not_called()
        window.ErrorLog.assert_called_once()
        window._show_error.assert_called_once_with(
            "로그인 진행 중",
            "선택한 장비는 이미 로그인을 진행 중입니다.",
        )

    def test_different_devices_can_run_in_parallel(self):
        window = make_window(("Al1", "Al3"))
        workers = []

        run_selected(window, workers)

        self.assertEqual(len(workers), 2)
        self.assertTrue(all(worker.started for worker in workers))
        self.assertEqual(
            set(window._active_workers),
            {"Al1", "Al3"},
        )

    def test_finished_worker_is_cleaned_and_stale_result_is_ignored(self):
        window = make_window()
        workers = []

        run_selected(window, workers)
        first_worker = workers[0]
        first_worker.finished.emit()

        self.assertNotIn("Al1", window._active_workers)
        self.assertNotIn(first_worker, window.threads)
        self.assertIsNone(first_worker.int_id)
        self.assertIsNone(first_worker.int_pw)
        self.assertTrue(first_worker.deleted)

        run_selected(window, workers)
        second_worker = workers[1]
        self.assertEqual(window.label_Al1.text, "로그인중...")

        first_worker.result_signal.emit("Al1", "실패")
        self.assertEqual(window.label_Al1.text, "로그인중...")

        second_worker.result_signal.emit("Al1", "성공")
        self.assertEqual(window.label_Al1.text, "로그인 성공")

    def test_close_ignores_results_and_quits_after_workers_finish(self):
        window = make_window()
        workers = []
        run_selected(window, workers)
        worker = workers[0]
        event = mock.Mock()
        app = mock.Mock()

        with mock.patch.object(
            RIL_client.QApplication,
            "instance",
            return_value=app,
        ):
            RIL_client.WindowClass.closeEvent(window, event)
            worker.result_signal.emit("Al1", "성공")

            self.assertTrue(worker.interruption_requested)
            self.assertEqual(window.label_Al1.text, "로그인중...")
            app.setQuitOnLastWindowClosed.assert_called_once_with(False)
            event.accept.assert_called_once()

            worker.finished.emit()

        app.quit.assert_called_once()

    def test_close_waits_for_update_worker_too(self):
        window = make_window(selected=())
        update_worker = FakeBackgroundWorker()
        window.update_worker = update_worker
        event = mock.Mock()
        app = mock.Mock()

        with mock.patch.object(
            RIL_client.QApplication,
            "instance",
            return_value=app,
        ):
            RIL_client.WindowClass.closeEvent(window, event)

            self.assertTrue(update_worker.interruption_requested)
            app.setQuitOnLastWindowClosed.assert_called_once_with(False)
            app.quit.assert_not_called()

            update_worker.running = False
            RIL_client.WindowClass._background_worker_finished(
                window,
                "update_worker",
                update_worker,
            )

        self.assertTrue(update_worker.deleted)
        app.quit.assert_called_once()

    def test_pending_installer_starts_only_after_workers_are_gone(self):
        window = make_window(selected=())
        window._closing = True
        window._pending_installer_path = r"C:\Temp\RIL_Update.exe"
        app = mock.Mock()

        with (
            mock.patch.object(
                RIL_client.QApplication,
                "instance",
                return_value=app,
            ),
            mock.patch.object(RIL_client.subprocess, "Popen") as popen,
        ):
            RIL_client.WindowClass._maybe_quit_after_close(window)

        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(
            args[0],
            [r"C:\Temp\RIL_Update.exe", "/S"],
        )
        self.assertFalse(kwargs["shell"])
        self.assertIsNone(window._pending_installer_path)
        app.quit.assert_called_once()

    def test_uncertain_result_is_visibly_distinct(self):
        window = make_window()

        RIL_client.WindowClass.update_ui(
            window,
            "Al1",
            "결과미확인:서버 응답 유실",
        )

        self.assertEqual(window.label_Al1.text, "결과 미확인")
        self.assertIn("#FF6600", window.label_Al1.style)
        self.assertIn("상태를 먼저 확인", window.label_Al1.tooltip)
        window.ErrorLog.assert_called_once()

    def test_success_clears_previous_uncertain_tooltip(self):
        window = make_window()

        RIL_client.WindowClass.update_ui(
            window,
            "Al1",
            "결과미확인:서버 응답 유실",
        )
        RIL_client.WindowClass.update_ui(
            window,
            "Al1",
            "성공",
        )

        self.assertEqual(window.label_Al1.text, "로그인 성공")
        self.assertEqual(window.label_Al1.tooltip, "")

    def test_server_busy_is_visibly_distinct(self):
        window = make_window()

        RIL_client.WindowClass.update_ui(
            window,
            "Al1",
            "서버 사용 중",
        )

        self.assertEqual(window.label_Al1.text, "서버 사용 중")
        self.assertIn("#FF6600", window.label_Al1.style)
        self.assertIn("잠시 후", window.label_Al1.tooltip)
        window.ErrorLog.assert_called_once_with("Al1 서버 사용 중")


if __name__ == "__main__":
    unittest.main()
