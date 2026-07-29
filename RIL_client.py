#아직 통신불량이라고 나옴

import sys, socket, time
import json
from pathlib import Path
from PyQt5.QtWidgets import *
from PyQt5 import uic
from PyQt5.QtCore import Qt, QThread, pyqtSignal
import client
import traceback
import requests
import psutil
import subprocess
import os
import tempfile
from ril_config import expand_path, load_config, resource_path
from ril_devices import (
    DEVICE_COMMANDS,
    DEVICE_DISPLAY_NAMES,
    DEVICE_IDS,
    DEVICE_IPS,
    devices_in_group,
)
from ril_update import (
    DownloadCancelled,
    download_verified_file,
    fetch_manifest,
    get_component_update,
    version_key,
)
from ril_version import LEGACY_UPDATE_VERSION, VERSION

_CONFIG = load_config()
_INSTALLATION = _CONFIG["installation"]
_LOGGING = _CONFIG["logging"]
_UPDATE = _CONFIG["update"]
_CLIENT = _CONFIG["client"]
_CLIENT_UPDATE = _UPDATE["client"]
_PROTOCOL = _CONFIG["protocol"]
SERVER_BUSY_RESULT_CODE = _PROTOCOL["busy_result_code"]

APP_DIR = (
    os.path.dirname(sys.executable)
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
LOG_DIR = expand_path(_LOGGING["directory"])

# UI 파일 연결
form_class = uic.loadUiType(
    str(resource_path(_INSTALLATION["client_ui_file"]))
)[0]

# ==============================================================================
# [설정] 자동 업데이트 정보
# ==============================================================================
CURRENT_VERSION = VERSION

UPDATE_MANIFEST_URL = _UPDATE["manifest_url"]
LEGACY_VERSION_URL = _UPDATE["legacy_version_url"]
LEGACY_INSTALLER_URL = _UPDATE["legacy_client_installer_url"]
UPDATE_REQUEST_TIMEOUT = _UPDATE["request_timeout_seconds"]
DOWNLOAD_TIMEOUT = (
    _UPDATE["download_connect_timeout_seconds"],
    _UPDATE["download_read_timeout_seconds"],
)
DOWNLOAD_TOTAL_TIMEOUT = _UPDATE["download_total_timeout_seconds"]
CLIENT_STARTUP_READY_ARGUMENT = "--ril-startup-ready-file"


def _consume_startup_ready_argument(arguments):
    qt_arguments = list(arguments)
    indexes = [
        index
        for index, value in enumerate(qt_arguments)
        if value == CLIENT_STARTUP_READY_ARGUMENT
    ]
    if not indexes:
        return qt_arguments, None
    if len(indexes) != 1:
        raise RuntimeError("클라이언트 시작 확인 인수가 중복되었습니다.")

    index = indexes[0]
    if index + 1 >= len(qt_arguments):
        raise RuntimeError("클라이언트 시작 확인 파일 경로가 없습니다.")
    ready_path = Path(qt_arguments[index + 1]).resolve()
    expected_directory = Path(APP_DIR).resolve()
    expected_filename = _INSTALLATION[
        "client_startup_ready_filename"
    ]
    if (
        ready_path.parent != expected_directory
        or ready_path.name != expected_filename
    ):
        raise RuntimeError(
            "클라이언트 시작 확인 파일 경로가 설치 폴더와 "
            "일치하지 않습니다."
        )

    del qt_arguments[index:index + 2]
    return qt_arguments, ready_path


def _write_startup_ready_file(path):
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = {
        "schema_version": 1,
        "status": "ready",
        "version": CURRENT_VERSION,
        "pid": os.getpid(),
        "ready_at": time.time(),
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def get_local_ipv4_addresses():
    addresses = {
        address.address
        for interface_addresses in psutil.net_if_addrs().values()
        for address in interface_addresses
        if address.family == socket.AF_INET and address.address
    }
    if not addresses:
        raise RuntimeError("로컬 IPv4 주소를 찾지 못했습니다.")
    return addresses


def download_file(
    url,
    dest_path,
    progress_callback=None,
    is_cancelled=None,
    total_timeout=DOWNLOAD_TOTAL_TIMEOUT,
    expected_sha256=None,
):
    """완료된 파일만 목적 경로에 남도록 업데이트 설치파일을 받는다."""
    return download_verified_file(
        url,
        dest_path,
        expected_sha256,
        request_timeout=DOWNLOAD_TIMEOUT,
        total_timeout=total_timeout,
        progress_callback=progress_callback,
        is_cancelled=is_cancelled,
        requests_module=requests,
        monotonic=time.monotonic,
    )


# -------------------------------
# Update Worker Thread (버전 체크 전담)
# -------------------------------
class UpdateWorker(QThread):
    update_available = pyqtSignal(str, str, object)

    def run(self):
        print(f"[UpdateWorker] 업데이트 확인 시작... (URL: {UPDATE_MANIFEST_URL})")
        try:
            if self.isInterruptionRequested():
                return
            manifest = fetch_manifest(
                UPDATE_MANIFEST_URL,
                UPDATE_REQUEST_TIMEOUT,
                requests_module=requests,
            )
            update = get_component_update(
                manifest,
                "client",
                CURRENT_VERSION,
            )

            if self.isInterruptionRequested():
                return
            if update is not None:
                remote_version = update["version"]
                print(
                    f"[UpdateWorker] 새 버전 발견: "
                    f"{remote_version} > {CURRENT_VERSION}"
                )
                self.update_available.emit(
                    remote_version,
                    update["url"],
                    update["sha256"],
                )
            else:
                print("[UpdateWorker] 업데이트 없음")
            return

        except Exception as e:
            print(f"[UpdateWorker] manifest 확인 실패, 기존 방식으로 재시도: {e}")

        try:
            if self.isInterruptionRequested():
                return
            response = requests.get(
                LEGACY_VERSION_URL,
                timeout=UPDATE_REQUEST_TIMEOUT,
                verify=False,
            )
            response.raise_for_status()
            remote_legacy_version = response.text.strip()

            if self.isInterruptionRequested():
                return
            if int(remote_legacy_version) > int(LEGACY_UPDATE_VERSION):
                self.update_available.emit(
                    remote_legacy_version,
                    LEGACY_INSTALLER_URL,
                    None,
                )
            else:
                print("[UpdateWorker] 기존 방식 업데이트 없음")
        except Exception as e:
            print(f"[UpdateWorker] 기존 방식 업데이트 확인 실패: {e}")

# -------------------------------
# Download Worker (파일 다운로드 전담 + 진행률 전송)
# -------------------------------
class DownloadWorker(QThread):
    progress = pyqtSignal(int)      # 진행률(%) 신호
    downloaded = pyqtSignal(str)    # 완료 신호 (파일경로)
    error = pyqtSignal(str)         # 에러 신호
    canceled = pyqtSignal()         # 사용자 취소 신호

    def __init__(self, url, dest_path, expected_sha256=None):
        super().__init__()
        self.url = url
        self.dest_path = dest_path
        self.expected_sha256 = expected_sha256
        self._cancel_requested = False

    def _is_cancelled(self):
        return (
            self._cancel_requested
            or self.isInterruptionRequested()
        )

    def run(self):
        try:
            download_file(
                self.url,
                self.dest_path,
                progress_callback=self.progress.emit,
                is_cancelled=self._is_cancelled,
                expected_sha256=self.expected_sha256,
            )
            if self._is_cancelled():
                self.canceled.emit()
                return
            self.downloaded.emit(self.dest_path)
        except DownloadCancelled:
            self.canceled.emit()
        except Exception as e:
            if self._is_cancelled():
                self.canceled.emit()
            else:
                self.error.emit(str(e))

    def cancel(self):
        self._cancel_requested = True
        self.requestInterruption()

# -------------------------------
# Worker Thread (서버 통신 전담)
# -------------------------------
class ClientWorker(QThread):
    result_signal = pyqtSignal(str, str)  # (장비명, 결과)

    def __init__(self, ip, int_id, int_pw, prg, label_name):
        super().__init__()
        self.ip = ip
        self.int_id = int_id
        self.int_pw = int_pw
        self.prg = prg
        self.label_name = label_name

    def run(self):
        try:
            result = client.run_login(
                self.ip,
                self.int_id,
                self.int_pw,
                self.prg,
                is_cancelled=self.isInterruptionRequested,
            )

            if result is None:
                self.result_signal.emit(self.label_name, "통신불량1:응답없음")
                return

            if not isinstance(result, tuple) or len(result) < 2:
                self.result_signal.emit(self.label_name, f"통신불량2:형식오류-{result}")
                return
            server_ip, status = result
            if status == "int_success":
                self.result_signal.emit(self.label_name, "성공")
            elif status == "int_failed":
                self.result_signal.emit(self.label_name, "실패")
            elif status == SERVER_BUSY_RESULT_CODE:
                self.result_signal.emit(self.label_name, "서버 사용 중")
            # ==========================================================
            # [추가할 부분] 1번, 2번 개별 실패 코드를 알아듣도록 추가
            elif status == "int_failed_1":
                self.result_signal.emit(self.label_name, "1번창 실패")
            elif status == "int_failed_2":
                self.result_signal.emit(self.label_name, "2번창 실패")
            elif status == "int_failed_1_2":
                self.result_signal.emit(self.label_name, "1,2번창 실패")
            # ==========================================================
            else:
                self.result_signal.emit(self.label_name, f"통신불량3:알수없는코드-{status}")

        except client.LoginResultUncertainError as e:
            print(
                "!!! 서버 결과 미확인 !!!\n"
                f"{traceback.format_exc()}"
            )
            self.result_signal.emit(
                self.label_name,
                f"결과미확인:{e}",
            )
        except Exception as e:
            # ---------------------------------------------------------
            # [수정 핵심] 에러를 잡아서 로그에 남길 수 있도록 상세 내용을 전달
            # ---------------------------------------------------------
            error_msg = f"통신불량4 <{type(e).__name__}> {str(e)}"

            # 개발자 확인용 (콘솔에는 상세 위치까지 출력)
            print(f"!!! 에러 발생 !!!\n{traceback.format_exc()}")

            # UI로 에러 메시지를 보냄 -> WindowClass의 update_ui에서 이걸 받아 ErrorLog에 기록함
            self.result_signal.emit(self.label_name, error_msg)

# -------------------------------
# 메인 윈도우
# -------------------------------
class WindowClass(QMainWindow, form_class):
    _SELECTION_PRESET_NAMES = (
        "radioButton_onlyIA",
        "radioButton_onlyCH",
        "radioButton_onlyAU",
        "radioButton_onlyDxI",
        "radioButton_onlyAlinity",
        "radioButton_ALL",
        "radioButton_None",
        "radioButton_dangjik",
        "radioButton_jochul",
    )

    def __init__(self):
        super().__init__()
        self.setupUi(self)

# ---------------------------------------------------------
        # [추가됨] UI에 버전 표시하기
        # ---------------------------------------------------------
        # 1. 윈도우 창 제목 (상단 바) 변경
        self.setWindowTitle(
            _CLIENT["window_title_template"].format(
                version=CURRENT_VERSION
            )
        )

        # # 2. 프로그램 내부 큰 제목 (Label) 변경
        # # RIL.ui에 있는 라벨 이름이 'label' 입니다.
        # self.label.setText(f"인터페이스 원격로그인 프로그램 v{CURRENT_VERSION}")
        # ---------------------------------------------------------
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.move(*_CLIENT["initial_window_position"])
        # PW 입력창을 ***로 표시
        self.lineEdit_PW.setEchoMode(QLineEdit.Password)
        self.radioButton_onlyIA.clicked.connect(self.select_IA) #
        self.radioButton_onlyCH.clicked.connect(self.select_CH) #
        self.radioButton_onlyAU.clicked.connect(self.select_AU) #
        self.radioButton_onlyDxI.clicked.connect(self.select_DxI) #
        self.radioButton_onlyAlinity.clicked.connect(self.select_Alinity) #
        self.radioButton_ALL.clicked.connect(self.select_ALL) #
        self.radioButton_None.clicked.connect(self.deselect_ALL) #
        self.radioButton_dangjik.clicked.connect(self.select_dangjik) #
        self.radioButton_jochul.clicked.connect(self.select_jochul) #
        self.pushButton_run.clicked.connect(self.run_selectedPC) #
        self.lineEdit_ID.setText("")#변경할 ID입력
        self.lineEdit_ID.returnPressed.connect(lambda: self.focusNextChild()) #엔터치면 pw로 이동
        self.lineEdit_PW.setText("")#변경할 pw입력
        self.lineEdit_PW.setEchoMode(QLineEdit.Password)
        self.lineEdit_PW.returnPressed.connect(self.run_selectedPC)

        self.interface_list = DEVICE_IDS
        self.interface_list_IA = devices_in_group("IA")
        self.interface_list_AU = devices_in_group("AU")
        self.interface_list_DxI = devices_in_group("DxI")
        self.interface_list_Alinity = devices_in_group("Alinity")
        self.interface_list_CH = devices_in_group("CH")
        self.interface_list_dangjik = devices_in_group("dangjik")
        self.interface_list_jochul = devices_in_group("jochul")
        self.interface_list_nojochul = devices_in_group("nojochul")
        self.int_iplist = dict(DEVICE_IPS)
        self._validate_device_widgets()
        for device_id, display_name in DEVICE_DISPLAY_NAMES.items():
            checkbox = getattr(self, f"checkBox_{device_id}")
            checkbox.setText(display_name)
            checkbox.clicked.connect(self._clear_selection_preset)
        # 실행 중 QThread 보관 및 장비별 중복 실행 방지
        self.threads = []
        self._active_workers = {}
        self._worker_generations = {}
        self._closing = False

        # 처음 화면에서는 라벨을 빈칸으로 초기화
        for i in self.interface_list:
            label_widget = getattr(self, f"label_{i}", None)
            if label_widget:
                label_widget.setText("")
        self.start_update_check()

    def _validate_device_widgets(self):
        missing = []
        for device_id in self.interface_list:
            for prefix in ("checkBox_", "label_"):
                if getattr(self, f"{prefix}{device_id}", None) is None:
                    missing.append(f"{prefix}{device_id}")
        if missing:
            raise RuntimeError(
                "장비 카탈로그와 UI가 일치하지 않습니다: "
                + ", ".join(missing)
            )

    # ---------------------------
    # [추가] 자동 업데이트 관련 메서드
    # ---------------------------
    def start_update_check(self):
        if not _CLIENT_UPDATE["automatic"]:
            self.update_worker = None
            return
        self.update_worker = UpdateWorker()
        worker = self.update_worker
        self.update_worker.update_available.connect(self.ask_update) # 신호 연결
        self.update_worker.finished.connect(
            lambda: self._background_worker_finished(
                "update_worker",
                worker,
            )
        )
        self.update_worker.start()

    def ask_update(
        self,
        server_version,
        installer_url,
        expected_sha256=None,
    ):
        """업데이트가 있을 때만 호출됨 (메인 스레드 안전)"""
        if self._closing:
            return
        reply = QMessageBox.question(
            self,
            '업데이트 확인',
            f"새로운 버전({server_version})이 있습니다.\n지금 업데이트 하시겠습니까?\n(프로그램이 종료되고 재설치됩니다.)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )

        if reply == QMessageBox.Yes:
            self.download_and_install(
                server_version,
                installer_url,
                expected_sha256,
            )

    # ---------------------------
    # [수정됨] 다운로드 및 설치 로직 (UI 프리징 해결)
    # ---------------------------
    def download_and_install(
        self,
        server_version,
        installer_url,
        expected_sha256=None,
    ):
        try:
            # 1. [Cache Busting] URL 뒤에 시간 붙이기
            import time
            bust_url = f"{installer_url}?t={int(time.time())}"
            print(f"[다운로드 요청 URL] {bust_url}")

            # (주의: 여기서 requests.get을 직접 호출하지 마세요! Worker가 할 일입니다.)

            # 2. 저장할 경로 설정
            temp_dir = tempfile.gettempdir()
            installer_name = _UPDATE["client"][
                "temporary_filename_template"
            ].format(version=server_version)
            installer_path = os.path.join(temp_dir, installer_name)

            # 3. 프로그레스바 다이얼로그 생성
            self.progress_dialog = QProgressDialog("업데이트 파일을 다운로드 중입니다...", "취소", 0, 100, self)
            self.progress_dialog.setWindowTitle("업데이트 진행 중")
            self.progress_dialog.setWindowModality(Qt.WindowModal)
            self.progress_dialog.setAutoClose(False)
            self.progress_dialog.setValue(0)
            self.progress_dialog.show()

            # 4. 다운로드 워커 생성 및 연결
            self.download_worker = DownloadWorker(
                bust_url,
                installer_path,
                expected_sha256=expected_sha256,
            )
            worker = self.download_worker

            self.download_worker.progress.connect(self.update_progress)
            self.download_worker.downloaded.connect(self.install_update)
            self.download_worker.error.connect(self.download_error)
            self.download_worker.canceled.connect(self.download_canceled)
            self.download_worker.finished.connect(
                lambda: self._background_worker_finished(
                    "download_worker",
                    worker,
                )
            )

            # 5. 취소 버튼 연결
            self.progress_dialog.canceled.connect(self.cancel_download)

            # 6. 다운로드 시작
            self.download_worker.start()

        except Exception as e:
            QMessageBox.critical(self, "오류", f"업데이트 준비 중 오류: {e}")

    # 진행률 업데이트 슬롯
    def update_progress(self, percent):
        if not self._closing:
            self.progress_dialog.setValue(percent)

    # 다운로드 완료 후 설치 시작 슬롯
    def install_update(self, file_path):
        if (
            self._closing
            or self.download_worker is None
            or self.download_worker._is_cancelled()
        ):
            return
        self.progress_dialog.setLabelText("다운로드 완료! 설치 프로그램을 실행합니다...")
        self.progress_dialog.setValue(100)
        self._close_progress_dialog()

        # 다운로드 QThread와 진행 중 로그인 QThread가 모두 종료된 뒤
        # 설치 프로그램을 시작한다. 실행 중인 스레드를 남긴 채 Qt 이벤트
        # 루프가 먼저 끝나는 경합을 막기 위한 통합 종료 경로다.
        self._pending_installer_path = file_path
        self.close()

    # 에러 발생 시 처리 슬롯
    def download_error(self, error_msg):
        self._close_progress_dialog()
        if not self._closing:
            QMessageBox.critical(self, "다운로드 오류", f"파일 다운로드 중 에러 발생:\n{error_msg}")

    def download_canceled(self):
        self._close_progress_dialog()

    def cancel_download(self):
        worker = getattr(self, "download_worker", None)
        if worker is None:
            return
        self.progress_dialog.setLabelText(
            "다운로드 취소 중입니다..."
        )
        worker.cancel()

    def _close_progress_dialog(self):
        dialog = getattr(self, "progress_dialog", None)
        if dialog is None:
            return
        try:
            dialog.canceled.disconnect(self.cancel_download)
        except (TypeError, RuntimeError):
            pass
        dialog.close()

    # 공통 유틸: 장비 그룹 체크 상태 설정
    def _set_group_checked(self, names, checked: bool):
        """
        names: ("D1", "D2", ...) 형식의 튜플 / 리스트
        checked: True → 체크, False → 체크 해제
        """
        for name in names:
            checkbox = getattr(self, f"checkBox_{name}", None)
            if checkbox is None:
                # UI에 없는 장비 이름이 들어와도 프로그램이 죽지 않도록 방어
                # 필요하면 여기서 print나 로그 남길 수 있음
                # print(f"[WARN] checkBox_{name} not found in UI")
                continue
            checkbox.setChecked(checked)

    # 공통 유틸: 장비 하나 체크/해제 (필요하면 사용)
    def _set_one_checked(self, name: str, checked: bool):
        checkbox = getattr(self, f"checkBox_{name}", None)
        if checkbox is None:
            # print(f"[WARN] checkBox_{name} not found in UI")
            return
        checkbox.setChecked(checked)

    def _clear_selection_preset(self, *_args):
        """수동 체크박스 변경 시 현재 선택 프리셋 표시를 해제한다."""
        buttons = [
            getattr(self, name)
            for name in self._SELECTION_PRESET_NAMES
        ]
        auto_exclusive = [
            button.autoExclusive()
            for button in buttons
        ]
        try:
            for button in buttons:
                button.setAutoExclusive(False)
            for button in buttons:
                button.setChecked(False)
        finally:
            for button, enabled in zip(buttons, auto_exclusive):
                button.setAutoExclusive(enabled)

    # -----------------------------
    # 라디오 버튼 핸들러들
    # -----------------------------
    def select_IA(self):
        """
        IA 장비만 선택:
        - interface_list_IA  : 체크
        - interface_list_CH  : 체크 해제
        """
        self._set_group_checked(self.interface_list_IA, True)
        self._set_group_checked(self.interface_list_CH, False)

    def select_CH(self):
        """
        CH 장비만 선택:
        - interface_list_CH  : 체크
        - interface_list_IA  : 체크 해제
        """
        self._set_group_checked(self.interface_list_CH, True)
        self._set_group_checked(self.interface_list_IA, False)

    def select_AU(self):
        """AU 1, 2, 3만 선택."""
        self._set_group_checked(self.interface_list, False)
        self._set_group_checked(self.interface_list_AU, True)

    def select_DxI(self):
        """DxI 1, 2만 선택."""
        self._set_group_checked(self.interface_list, False)
        self._set_group_checked(self.interface_list_DxI, True)

    def select_Alinity(self):
        """Alinity 1, 3만 선택."""
        self._set_group_checked(self.interface_list, False)
        self._set_group_checked(self.interface_list_Alinity, True)

    def select_ALL(self):
        """
        모든 장비 선택:
        - interface_list 전체 체크
        """
        self._set_group_checked(self.interface_list, True)

    def deselect_ALL(self):
        """
        모든 장비 선택 해제:
        - interface_list 전체 체크 해제
        """
        self._set_group_checked(self.interface_list, False)

    def select_dangjik(self):
        """
        당직 모드:
        - 전체 선택 해제 후 interface_list_dangjik만 체크
        """
        self._set_group_checked(self.interface_list, False)
        self._set_group_checked(self.interface_list_dangjik, True)

    def select_jochul(self):
        """
        조출 모드:
        - interface_list_jochul    : 체크
        - interface_list_nojochul  : 체크 해제
        """
        self._set_group_checked(self.interface_list_jochul, True)
        self._set_group_checked(self.interface_list_nojochul, False)
    # ---------------------------
    # UI 업데이트 함수
    # ---------------------------
    def update_ui(self, label_name, result):
        label_widget = getattr(self, f"label_{label_name}")
        label_widget.setToolTip("")
        if result == "성공":
            label_widget.setStyleSheet("Color : #00AA00")
            label_widget.setText("로그인 성공")
        elif result == "실패":
            label_widget.setStyleSheet("Color : #FF0000")
            label_widget.setText("로그인 실패")
            self.ErrorLog(f"{label_name} 로그인 실패")
        elif result in ("1번창 실패", "2번창 실패", "1,2번창 실패"):  # ← 이 블록 추가
            label_widget.setStyleSheet("Color : #FF6600")
            label_widget.setText(result)
            self.ErrorLog(f"{label_name} {result}")
        elif result.startswith("결과미확인"):
            label_widget.setStyleSheet("Color : #FF6600")
            label_widget.setText("결과 미확인")
            label_widget.setToolTip(
                "서버의 최종 결과를 확인하지 못했습니다. "
                "인터페이스 상태를 먼저 확인하고 다시 실행하세요."
            )
            self.ErrorLog(
                f"{label_name} 결과 미확인 - 즉시 재실행 주의: "
                f"{result}"
            )
        elif result == "서버 사용 중":
            label_widget.setStyleSheet("Color : #FF6600")
            label_widget.setText("서버 사용 중")
            label_widget.setToolTip(
                "다른 원격 로그인 요청이 진행 중입니다. "
                "잠시 후 다시 시도하세요."
            )
            self.ErrorLog(f"{label_name} 서버 사용 중")
        elif result.startswith("통신불량"):
            label_widget.setStyleSheet("Color : #FF0000")

            # 화면에는 짧게 표시 (예: "통신불량4")
            short_msg = result.split('<')[0] if '<' in result else result
            label_widget.setText(short_msg)

            # 로그 파일에는 전체 에러 내용(상세 정보 포함) 기록
            self.ErrorLog(f"{label_name} 에러발생: {result}")

    # ---------------------------
    # 에러 로그 기록
    # ---------------------------
    def ErrorLog(self, error: str):
        current_time = time.strftime("%Y.%m.%d/%H:%M:%S", time.localtime())
        log_path = os.path.join(
            LOG_DIR,
            _CLIENT["log_filename_prefix"]
            + str(time.strftime("%y.%m.%d"))
            + ".txt",
        )
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{current_time}] - {error}\n")


    # -------------------------------
    # 공통 유틸: 에러 메시지
    # -------------------------------
    def _show_error(self, title: str, text: str):
        """
        Tkinter messagebox 대신 PyQt5 QMessageBox 사용
        """
        QMessageBox.critical(self, title, text, QMessageBox.Ok)

    # -------------------------------
    # 공통 유틸: 체크박스 상태 조회 (UI에 없어도 안전)
    # -------------------------------
    def _is_checked(self, name: str) -> bool:
        """
        name: "D1", "D2" 등 장비 이름
        checkBox_name 위젯이 없어도 False만 반환하고 에러는 안 냄
        """
        checkbox = getattr(self, f"checkBox_{name}", None)
        if checkbox is None:
            return False
        return checkbox.isChecked()

    def _ensure_worker_tracking(self):
        """이전 테스트 객체를 포함해 worker 추적 상태를 지연 초기화한다."""
        if not hasattr(self, "threads"):
            self.threads = []
        if not hasattr(self, "_active_workers"):
            self._active_workers = {}
        if not hasattr(self, "_worker_generations"):
            self._worker_generations = {}
        if not hasattr(self, "_closing"):
            self._closing = False

    def _handle_worker_result(
        self,
        device_id,
        generation,
        result,
    ):
        """현재 실행 세대의 결과만 화면에 반영한다."""
        WindowClass._ensure_worker_tracking(self)
        if self._closing:
            return
        if self._worker_generations.get(device_id) != generation:
            return
        WindowClass.update_ui(self, device_id, result)

    def _worker_finished(
        self,
        device_id,
        generation,
        worker,
    ):
        """완료된 worker와 worker가 보유한 로그인 정보를 정리한다."""
        WindowClass._ensure_worker_tracking(self)

        if (
            self._active_workers.get(device_id) is worker
            and self._worker_generations.get(device_id) == generation
        ):
            self._active_workers.pop(device_id, None)

        try:
            self.threads.remove(worker)
        except ValueError:
            pass

        worker.int_id = None
        worker.int_pw = None
        worker.deleteLater()

        WindowClass._maybe_quit_after_close(self)

    def _background_worker_finished(self, attribute_name, worker):
        if getattr(self, attribute_name, None) is worker:
            setattr(self, attribute_name, None)
        worker.deleteLater()
        WindowClass._maybe_quit_after_close(self)

    def _running_background_workers(self):
        workers = []
        for attribute_name in ("update_worker", "download_worker"):
            worker = getattr(self, attribute_name, None)
            if worker is not None and worker.isRunning():
                workers.append(worker)
        return workers

    def _maybe_quit_after_close(self):
        WindowClass._ensure_worker_tracking(self)
        if (
            self._closing
            and not self._active_workers
            and not WindowClass._running_background_workers(self)
        ):
            app = QApplication.instance()
            installer_path = getattr(
                self,
                "_pending_installer_path",
                None,
            )
            if installer_path is not None:
                self._pending_installer_path = None
                try:
                    env = os.environ.copy()
                    env.pop("_MEIPASS", None)
                    subprocess.Popen(
                        [
                            installer_path,
                            *[
                                str(argument)
                                for argument in _CLIENT_UPDATE[
                                    "installer_arguments"
                                ]
                            ],
                        ],
                        shell=False,
                        env=env,
                    )
                except Exception as e:
                    self._closing = False
                    if app is not None:
                        app.setQuitOnLastWindowClosed(True)
                    self.show()
                    QMessageBox.critical(
                        self,
                        "설치 오류",
                        f"설치 파일 실행 실패:\n{e}",
                    )
                    return
            if app is not None:
                app.quit()

    def closeEvent(self, event):
        """진행 중인 로그인 worker가 정리될 때까지 앱 수명을 유지한다."""
        WindowClass._ensure_worker_tracking(self)
        self._closing = True

        active_workers = list(self._active_workers.values())
        for worker in active_workers:
            worker.requestInterruption()

        background_workers = WindowClass._running_background_workers(self)
        for worker in background_workers:
            if isinstance(worker, DownloadWorker):
                worker.cancel()
            else:
                worker.requestInterruption()

        if active_workers or background_workers:
            app = QApplication.instance()
            if app is not None:
                app.setQuitOnLastWindowClosed(False)

        event.accept()

    # ---------------------------
    # 선택된 장비 실행
    # ---------------------------
    def run_selectedPC(self):
        WindowClass._ensure_worker_tracking(self)

        int_id = self.lineEdit_ID.text()
        int_pw = self.lineEdit_PW.text()

        # 입력값 검증
        if int_id == "":
            self._show_error("ID 미입력", "ID를 입력해주세요.")
            return
        id_length = _CLIENT["credential_id_length"]
        if len(int_id) != id_length:
            self._show_error(
                "ID 오류",
                f"ID는 {id_length}글자여야 합니다.",
            )
            return
        if int_pw == "":
            self._show_error("PW 미입력", "PW를 입력해주세요.")
            return
        if not any(self._is_checked(i) for i in self.interface_list):
            self._show_error("선택 오류", "장비를 선택해주세요.")
            return

        selected_devices = [
            device_id
            for device_id in self.interface_list
            if self._is_checked(device_id)
        ]
        runnable_devices = [
            device_id
            for device_id in selected_devices
            if device_id not in self._active_workers
        ]
        if not runnable_devices:
            self._show_error(
                "로그인 진행 중",
                "선택한 장비는 이미 로그인을 진행 중입니다.",
            )
            return

        # 인터페이스 PC에서 실행 방지
        try:
            local_ipv4_addresses = get_local_ipv4_addresses()
        except Exception as e:
            self._show_error(
                "실행 제한 확인 오류",
                "이 PC의 네트워크 주소를 확인할 수 없어 "
                "실행을 중단합니다.",
            )
            self.ErrorLog(
                "로컬 IPv4 조회 실패: "
                f"{type(e).__name__}: {e}"
            )
            return

        interface_ipv4_addresses = set(self.int_iplist.values())
        matched_addresses = sorted(
            local_ipv4_addresses & interface_ipv4_addresses
        )
        if matched_addresses:
            self._show_error(
                "실행 제한",
                "인터페이스 장비 PC"
                f"({', '.join(matched_addresses)})에서는 "
                "실행할 수 없습니다.",
            )
            return

        # 사용자 확인
        msgbox = f"ID : {int_id}\nPW : {int_pw}\n입력하신 정보가 맞습니까?"
        reply = QMessageBox.question(self, 'IDPW확인', msgbox,
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return

        # 입력 로그 기록
        self.ErrorLog(int_id + ' / ' + int_pw)

        # 선택된 장비 실행 (스레드로)
        for i in runnable_devices:
            # 체크박스가 실제로 존재하고 체크된 경우에만 진행
            checkbox = getattr(self, f"checkBox_{i}", None)
            if checkbox is None or not checkbox.isChecked():
                continue

            # IP 매핑에 없는 장비는 스킵 (혹시 나중에 ST2 빼고 int_iplist 안 고쳤을 때 대비)
            if i not in self.int_iplist:
                continue

            runint = self.int_iplist[i]
            prg = DEVICE_COMMANDS[i]

            # 로그인 중 표시
            label_widget = getattr(self, f"label_{i}", None)
            if label_widget is not None:
                label_widget.setStyleSheet("Color : #0000FF")
                label_widget.setText("로그인중...")

            generation = self._worker_generations.get(i, 0) + 1
            self._worker_generations[i] = generation
            worker = ClientWorker(runint, int_id, int_pw, prg, i)
            worker.result_signal.connect(
                lambda _label_name, result, device_id=i,
                       worker_generation=generation:
                WindowClass._handle_worker_result(
                    self,
                    device_id,
                    worker_generation,
                    result,
                )
            )
            worker.finished.connect(
                lambda device_id=i, worker_generation=generation,
                       completed_worker=worker:
                WindowClass._worker_finished(
                    self,
                    device_id,
                    worker_generation,
                    completed_worker,
                )
            )

            self._active_workers[i] = worker
            self.threads.append(worker)
            try:
                worker.start()
            except Exception:
                WindowClass._worker_finished(
                    self,
                    i,
                    generation,
                    worker,
                )
                raise

# -------------------------------
# 실행부
# -------------------------------
if __name__ == "__main__":
    qt_arguments, startup_ready_path = (
        _consume_startup_ready_argument(sys.argv)
    )
    app = QApplication(qt_arguments)
    myWindow = WindowClass()
    myWindow.show()
    if startup_ready_path is not None:
        _write_startup_ready_file(startup_ready_path)
    app.processEvents()
    sys.exit(app.exec_())

#한 파일
# pyinstaller -w -F --uac-admin --clean --icon=chunsik1.ico --exclude pandas, --exclude numpy, --exclude pillow RIL_client.py


#한 폴더
# pyinstaller -w --uac-admin --clean --icon=chunsik1.ico --exclude pandas, --exclude numpy, --exclude pillow RIL_client.py

#spec 파일 이용(폴더명에 버전들어가고 실행파일은 버전없게)
# pyinstaller RIL_client.spec
