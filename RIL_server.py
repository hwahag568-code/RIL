import os
import sys
from ril_config import select_component_config

select_component_config("server")

import mynetlib
import IAL
import ctypes, time
from copy import deepcopy
from collections import deque
from PIL import Image
import pystray, psutil
from pystray import MenuItem as item
import multiprocessing
from multiprocessing import freeze_support
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import threading
import json
from datetime import datetime, timezone
import requests
from ril_config import (
    expand_path,
    load_config,
    resource_path,
)
from ril_devices import (
    AU_COMMAND_NUMBERS,
    GENERAL_COMMAND,
    NOVA_COMMAND,
    OSMO_COMMAND,
)
from ril_update import (
    atomic_write_json,
    download_verified_file,
    fetch_manifest,
    file_sha256,
    get_component_update,
    server_update_destination,
)
from ril_version import PROTOCOL_VERSION, VERSION

_CONFIG = load_config()
_INSTALLATION = _CONFIG["installation"]
_LOGGING = _CONFIG["logging"]
_UPDATE = _CONFIG["update"]
_SERVER_UPDATE = _UPDATE["server"]
_NETWORK = _CONFIG["network"]
_PROTOCOL = _CONFIG["protocol"]
_SERVER = _CONFIG["server"]

APP_DIR = (
    os.path.dirname(sys.executable)
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
LOG_DIR = expand_path(_LOGGING["directory"])
os.makedirs(LOG_DIR, exist_ok=True)

mp = multiprocessing.Process

PI = _SERVER["process_to_close_after_listen"]
SERVER_VERSION = VERSION
CAPABILITIES_COMMAND = _PROTOCOL["capabilities_command"]
DIRECT_RESULT_FEATURES = list(_PROTOCOL["direct_result_features"])
RESULT_CODES = set(_PROTOCOL["result_codes"])
LEGACY_RESULT_TTL = _SERVER["legacy_result_ttl_seconds"]
DIRECT_RESULT_TTL = _SERVER["direct_result_ttl_seconds"]
LOGIN_EXECUTION_TIMEOUT = _SERVER["login_execution_timeout_seconds"]
IAL_CHILD_JOIN_TIMEOUT = _SERVER["ial_child_join_timeout_seconds"]
DIRECT_INFLIGHT_WAIT_TIMEOUT = (
    LOGIN_EXECUTION_TIMEOUT + (2 * IAL_CHILD_JOIN_TIMEOUT)
)
IAL_CONTROL_WAIT_TIMEOUT = (
    (2 * IAL_CHILD_JOIN_TIMEOUT)
    + mynetlib.DEFAULT_CANCEL_POLL_INTERVAL
)
IAL_IDENTITY_READY_TIMEOUT = (
    IAL_CHILD_JOIN_TIMEOUT
    + mynetlib.DEFAULT_CANCEL_POLL_INTERVAL
)
INSTANCE_MUTEX_NAME = _SERVER["instance_mutex_name"]
SERVER_PORT = _NETWORK["port"]
MESSAGE_CHUNK_SIZE = _NETWORK["message_chunk_size_bytes"]
LEGACY_RESULT_COMMAND = _PROTOCOL["legacy_result_command"]
REQUEST_ID_MAX_LENGTH = _PROTOCOL["request_id_max_length"]
SUPERVISOR_POLL_INTERVAL = _SERVER[
    "supervisor_poll_interval_seconds"
]
SUPERVISOR_JOIN_TIMEOUT = _SERVER[
    "supervisor_join_timeout_seconds"
]
ROLLBACK_START_STATE = "rollback_starting_previous"
ERROR_ALREADY_EXISTS = 183
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
legacy_results = {}
direct_results = {}
direct_inflight = {}
BUSY_RESULT_CODE = _PROTOCOL["busy_result_code"]
_result_cache_lock = threading.RLock()
_ial_execution_lock = threading.Lock()
_instance_mutex_handle = None
_instance_mutex_kernel32 = None


class _IalWorkerControl:
    """Cross-process control and exact identity for one IAL worker."""

    def __init__(self):
        self.cancel_event = multiprocessing.Event()
        self.idle_event = multiprocessing.Event()
        self.idle_event.set()
        self.identity_ready_event = multiprocessing.Event()
        self.start_lock = multiprocessing.Lock()
        self.pid = multiprocessing.Value("q", 0, lock=False)
        self.create_time = multiprocessing.Value("d", 0.0, lock=False)
        self.owner_pid = multiprocessing.Value("q", 0, lock=False)
        self.generation = multiprocessing.Value("Q", 0, lock=False)


def _publish_ial_worker_identity(
    control,
    *,
    pid,
    create_time,
    owner_pid,
):
    generation = int(control.generation.value)
    if generation % 2:
        generation += 1
    control.generation.value = generation + 1
    control.create_time.value = float(create_time)
    control.owner_pid.value = int(owner_pid)
    control.pid.value = int(pid)
    control.generation.value = generation + 2


def _read_ial_worker_identity(control):
    for _ in range(10):
        generation_before = int(control.generation.value)
        if generation_before % 2:
            continue
        pid = int(control.pid.value)
        create_time = float(control.create_time.value)
        owner_pid = int(control.owner_pid.value)
        generation_after = int(control.generation.value)
        if (
            generation_before == generation_after
            and generation_after % 2 == 0
        ):
            return pid, create_time, owner_pid
    return None


def _clear_ial_worker_identity(control):
    _publish_ial_worker_identity(
        control,
        pid=0,
        create_time=0.0,
        owner_pid=0,
    )
    control.identity_ready_event.clear()


class _LegacyResultState:
    def __init__(self):
        self.queue = deque()
        self.active_claims = 0


class _DirectInFlight:
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.completed = threading.Event()


def _cleanup_legacy_results():
    with _result_cache_lock:
        cutoff = time.monotonic() - LEGACY_RESULT_TTL
        for key in list(legacy_results):
            state = legacy_results[key]
            while state.queue and state.queue[0][0] < cutoff:
                state.queue.popleft()
            if not state.queue and state.active_claims == 0:
                del legacy_results[key]


def _legacy_result_key(addr, user_id, password):
    return addr[0], user_id, password


def _store_legacy_result(addr, user_id, password, status):
    with _result_cache_lock:
        _cleanup_legacy_results()
        key = _legacy_result_key(addr, user_id, password)
        state = legacy_results.setdefault(key, _LegacyResultState())
        state.queue.append((time.monotonic(), status))


def _peek_legacy_result(addr, user_id, password):
    with _result_cache_lock:
        _cleanup_legacy_results()
        key = _legacy_result_key(addr, user_id, password)
        state = legacy_results.get(key)
        if state is None or not state.queue:
            return None
        return state.queue[0][1]


def _claim_legacy_result(addr, user_id, password):
    with _result_cache_lock:
        _cleanup_legacy_results()
        key = _legacy_result_key(addr, user_id, password)
        state = legacy_results.get(key)
        if state is None or not state.queue:
            return None
        entry = state.queue.popleft()
        state.active_claims += 1
        return key, state, entry


def _finish_legacy_result_claim(claim, delivered):
    if claim is None:
        return
    key, state, entry = claim
    with _result_cache_lock:
        state.active_claims -= 1
        current_state = legacy_results.get(key)
        if (
            not delivered
            and current_state is state
            and entry[0] >= time.monotonic() - LEGACY_RESULT_TTL
        ):
            state.queue.appendleft(entry)
        if (
            current_state is state
            and not state.queue
            and state.active_claims == 0
        ):
            del legacy_results[key]


def _clear_legacy_results(addr, user_id, password):
    with _result_cache_lock:
        legacy_results.pop(
            _legacy_result_key(addr, user_id, password),
            None,
        )


def _cleanup_direct_results():
    with _result_cache_lock:
        cutoff = time.monotonic() - DIRECT_RESULT_TTL
        for key, value in list(direct_results.items()):
            if value[0] < cutoff:
                del direct_results[key]


def _get_direct_result(addr, request_id):
    with _result_cache_lock:
        _cleanup_direct_results()
        return direct_results.get((addr[0], request_id))


def _store_direct_result(
    addr,
    request_id,
    fingerprint,
    server_ip,
    status,
):
    with _result_cache_lock:
        _cleanup_direct_results()
        direct_results[(addr[0], request_id)] = (
            time.monotonic(),
            fingerprint,
            server_ip,
            status,
        )


def _begin_direct_request(addr, request_id, fingerprint):
    key = (addr[0], request_id)
    with _result_cache_lock:
        _cleanup_direct_results()
        completed = direct_results.get(key)
        if completed is not None:
            return "completed", completed

        inflight = direct_inflight.get(key)
        if inflight is not None:
            if inflight.fingerprint != fingerprint:
                return "conflict", None
            return "follower", inflight

        inflight = _DirectInFlight(fingerprint)
        direct_inflight[key] = inflight
        return "owner", inflight


def _complete_direct_request(
    addr,
    request_id,
    inflight,
    fingerprint,
    server_ip,
    status,
):
    key = (addr[0], request_id)
    with _result_cache_lock:
        current = direct_inflight.get(key)
        if current is inflight:
            direct_results[key] = (
                time.monotonic(),
                fingerprint,
                server_ip,
                status,
            )
            del direct_inflight[key]
        inflight.completed.set()

if sys.stderr is None:

    # 에러 내용을 파일로 남기고 싶다면:
    sys.stderr = open(
        os.path.join(LOG_DIR, _SERVER["stderr_log_filename"]),
        "a",
    )

    # 또는 그냥 무시하고 싶다면(비추천):
    # sys.stderr = open(os.devnull, 'w')

if sys.stdout is None:
    sys.stdout = open(
        os.path.join(LOG_DIR, _SERVER["stdout_log_filename"]),
        "a",
    )

def ErrorLog(error: str):
    current_time = time.strftime("%Y.%m.%d/%H:%M:%S", time.localtime(time.time()))
    try:
        with open(
            os.path.join(
                LOG_DIR,
                _SERVER["log_filename_prefix"]
                + str(time.strftime("%y.%m.%d"))
                + ".txt",
            ),
            "a",
            encoding="utf-8",
        ) as file:
            file.write(f"[{current_time}] - {error}\n")
    except Exception as log_error:
        print(
            f"[ErrorLog 실패] {log_error!r} / 원본: {error}",
            flush=True,
        )


def acquire_single_instance_mutex(kernel32=None):
    """Windows 사용자 세션에서 서버 부모 프로세스를 하나만 허용한다."""
    global _instance_mutex_handle, _instance_mutex_kernel32

    if os.name != "nt" and kernel32 is None:
        return True
    if _instance_mutex_handle is not None:
        return True

    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

    if hasattr(kernel32, "SetLastError"):
        kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(
        None,
        False,
        INSTANCE_MUTEX_NAME,
    )
    if not handle:
        raise ctypes.WinError()

    last_error = kernel32.GetLastError()
    if last_error == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    _instance_mutex_handle = handle
    _instance_mutex_kernel32 = kernel32
    return True


def release_single_instance_mutex():
    global _instance_mutex_handle, _instance_mutex_kernel32

    handle = _instance_mutex_handle
    kernel32 = _instance_mutex_kernel32
    _instance_mutex_handle = None
    _instance_mutex_kernel32 = None
    if handle is not None and kernel32 is not None:
        kernel32.CloseHandle(handle)


def TaskKill(prg):
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").casefold() == prg.casefold():
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _dispatch_ial_command(user_id, password, command):
    if command == GENERAL_COMMAND:
        return IAL.StartTask(user_id, password)
    if command == OSMO_COMMAND:
        return IAL.StartTaskOS(user_id, password)
    if command in AU_COMMAND_NUMBERS:
        return IAL.StartTaskAU(
            user_id,
            password,
            AU_COMMAND_NUMBERS[command],
        )
    if (
        isinstance(command, str)
        and command.casefold() == NOVA_COMMAND.casefold()
    ):
        return IAL.StartTaskNovaPrime(user_id, password)

    ErrorLog(f"알 수 없는 명령 코드 c={command!r}")
    return "int_failed"


def _ial_request_worker(
    result_connection,
    user_id,
    password,
    command,
    worker_control=None,
):
    try:
        if worker_control is not None:
            try:
                current_pid = os.getpid()
                current_owner_pid = os.getppid()
                current_create_time = psutil.Process(
                    current_pid
                ).create_time()
                _publish_ial_worker_identity(
                    worker_control,
                    pid=current_pid,
                    create_time=current_create_time,
                    owner_pid=current_owner_pid,
                )
                worker_control.identity_ready_event.set()
                identity = _read_ial_worker_identity(worker_control)
                identity_matches = (
                    identity is not None
                    and identity[0] == current_pid
                    and identity[2] == current_owner_pid
                    and identity[1] == current_create_time
                )
            except (psutil.Error, OSError):
                identity_matches = False

            if (
                worker_control.cancel_event.is_set()
                or not identity_matches
            ):
                status = "int_failed"
            else:
                status = None
        else:
            status = None

        if status is None:
            if (
                worker_control is not None
                and worker_control.cancel_event.is_set()
            ):
                status = "int_failed"
            else:
                try:
                    status = _dispatch_ial_command(
                        user_id,
                        password,
                        command,
                    )
                except Exception as error:
                    ErrorLog(
                        f"StartTask 호출 중 예외 발생: {error!r}"
                    )
                    status = "int_failed"
        result_connection.send(status)
    finally:
        result_connection.close()


def _start_ial_process(process, worker_control):
    if worker_control is None:
        process.start()
        return True

    worker_control.start_lock.acquire()
    if worker_control.cancel_event.is_set():
        worker_control.start_lock.release()
        return False

    worker_control.idle_event.clear()
    _clear_ial_worker_identity(worker_control)
    if worker_control.cancel_event.is_set():
        worker_control.idle_event.set()
        worker_control.start_lock.release()
        return False

    try:
        process.start()
    except BaseException:
        started_pid = getattr(process, "pid", None)
        if isinstance(started_pid, int) and started_pid > 0:
            if not _stop_ial_process(process, terminate_first=True):
                _fail_closed_for_unconfirmed_ial_worker(
                    "IAL 작업 프로세스 시작 실패 후 종료를 "
                    "확인하지 못했습니다."
                )
        _finish_ial_worker_shutdown(worker_control)
        worker_control.start_lock.release()
        raise

    try:
        if not worker_control.identity_ready_event.wait(
            IAL_IDENTITY_READY_TIMEOUT
        ):
            raise RuntimeError(
                "IAL 작업 프로세스가 제한시간 내에 "
                "신원을 등록하지 못했습니다."
            )

        identity = _read_ial_worker_identity(worker_control)
        if (
            identity is None
            or identity[0] != process.pid
            or identity[2] != os.getpid()
            or identity[1] <= 0
        ):
            raise RuntimeError(
                "IAL 작업 프로세스가 잘못된 신원을 등록했습니다."
            )
    except _UnconfirmedIalWorkerTermination:
        raise
    except BaseException:
        if _stop_ial_process(process, terminate_first=True):
            _finish_ial_worker_shutdown(worker_control)
            worker_control.start_lock.release()
            raise
        _fail_closed_for_unconfirmed_ial_worker(
            "신원 등록 실패 후 IAL 작업 프로세스의 "
            "종료를 확인하지 못했습니다."
        )
    return True


def _release_ial_worker_slot(worker_control):
    if worker_control is not None:
        worker_control.start_lock.release()


def _receive_ial_result(
    result_reader,
    command,
    timeout,
    worker_control,
):
    if worker_control is None:
        if result_reader.poll(timeout):
            try:
                return result_reader.recv(), False, False
            except EOFError:
                ErrorLog(
                    "IAL 작업 프로세스가 결과 없이 종료되었습니다: "
                    f"command={command!r}"
                )
                return "int_failed", False, False
        return "int_failed", True, False

    deadline = time.monotonic() + timeout
    while True:
        if worker_control.cancel_event.is_set():
            ErrorLog(
                "서버 종료 요청으로 IAL 작업 프로세스를 "
                f"중단합니다: command={command!r}"
            )
            return "int_failed", False, True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "int_failed", True, False
        if result_reader.poll(
            min(
                mynetlib.DEFAULT_CANCEL_POLL_INTERVAL,
                remaining,
            )
        ):
            try:
                return result_reader.recv(), False, False
            except EOFError:
                ErrorLog(
                    "IAL 작업 프로세스가 결과 없이 종료되었습니다: "
                    f"command={command!r}"
                )
                return "int_failed", False, False


def _stop_ial_process(
    process,
    join_timeout=IAL_CHILD_JOIN_TIMEOUT,
    terminate_first=False,
):
    try:
        if terminate_first and process.is_alive():
            process.terminate()
        process.join(timeout=join_timeout)
        if process.is_alive():
            process.kill()
            process.join(timeout=join_timeout)
        return process.is_alive() is False
    except (Exception, KeyboardInterrupt) as error:
        ErrorLog(
            "IAL 작업 프로세스 종료 확인 중 예외: "
            f"{error!r}"
        )
        return False


class _UnconfirmedIalWorkerTermination(BaseException):
    pass


def _fail_closed_for_unconfirmed_ial_worker(message):
    ErrorLog(message)
    os._exit(70)
    raise _UnconfirmedIalWorkerTermination(message)


def execute_ial_with_hard_timeout(
    user_id,
    password,
    command,
    timeout=LOGIN_EXECUTION_TIMEOUT,
    worker_control=None,
):
    """IAL 자동화를 별도 프로세스에서 실행해 실제 종료 가능한 제한을 둔다."""
    result_reader, result_writer = multiprocessing.Pipe(duplex=False)
    process = mp(
        target=_ial_request_worker,
        args=(
            result_writer,
            user_id,
            password,
            command,
            worker_control,
        ),
        name="RIL_IAL_Request",
    )
    started = False
    timed_out = False
    cancelled = False
    worker_stop_confirmed = False
    worker_shutdown_uncertain = False
    try:
        if not _start_ial_process(process, worker_control):
            return "int_failed"
        started = True
        result_writer.close()

        status, timed_out, cancelled = _receive_ial_result(
            result_reader,
            command,
            timeout,
            worker_control,
        )
        if timed_out:
            ErrorLog(
                "로그인 명령 실행 제한 시간 초과 - "
                "IAL 작업 프로세스 종료: "
                f"command={command!r}"
            )

        stopped = _stop_ial_process(
            process,
            terminate_first=timed_out or cancelled,
        )
        if not stopped:
            worker_shutdown_uncertain = True
            _fail_closed_for_unconfirmed_ial_worker(
                "IAL 작업 프로세스를 강제 종료하지 못해 "
                "서버 프로세스를 종료합니다."
            )
        worker_stop_confirmed = True
        return status
    except _UnconfirmedIalWorkerTermination:
        worker_shutdown_uncertain = True
        raise
    finally:
        if (
            started
            and not worker_stop_confirmed
            and not worker_shutdown_uncertain
        ):
            worker_stop_confirmed = _stop_ial_process(
                process,
                terminate_first=True,
            )
            if not worker_stop_confirmed:
                worker_shutdown_uncertain = True
                _fail_closed_for_unconfirmed_ial_worker(
                    "예외 처리 중 IAL 작업 프로세스의 "
                    "종료를 확인하지 못했습니다."
                )
        if (
            worker_control is not None
            and not worker_shutdown_uncertain
            and (not started or worker_stop_confirmed)
        ):
            _clear_ial_worker_identity(worker_control)
            worker_control.idle_event.set()
        if (
            worker_control is not None
            and started
            and worker_stop_confirmed
        ):
            _release_ial_worker_slot(worker_control)
        result_reader.close()
        if not started:
            result_writer.close()


def _restart_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, *sys.argv]


def restartscript():
    time.sleep(_SERVER["restart_delay_seconds"])
    ErrorLog('restartprg')
    command = _restart_command()
    release_single_instance_mutex()
    try:
        os.execv(command[0], command)
    except Exception as exec_error:
        ErrorLog(f"execv 재시작 실패, 새 프로세스로 재시도: {exec_error!r}")
        try:
            subprocess.Popen(
                command,
                cwd=APP_DIR,
                close_fds=True,
            )
        except Exception as spawn_error:
            ErrorLog(f"새 프로세스 재시작도 실패: {spawn_error!r}")
            raise RuntimeError(
                "RIL 서버 자동 재시작에 실패했습니다."
            ) from spawn_error


def empty_tray_menu(_icon, _menu_item):
    pass


def quit_tray(icon, _menu_item, requested_stop=None):
    if requested_stop is not None:
        requested_stop.set()
    icon.stop()


def make_trayicon(requested_stop=None):
    image = Image.open(
        resource_path(_INSTALLATION["icon_file"])
    )
    def request_quit(icon, menu_item):
        quit_tray(icon, menu_item, requested_stop)

    display_title = _SERVER["window_title_template"].format(
        version=SERVER_VERSION
    )
    menu_title = f"<{display_title} (실행중)>"

    menu = (item(menu_title, empty_tray_menu),  # 여기에 버전이 뜸
            item('프로그램 종료', request_quit))
    # [수정 2] 마우스 올렸을 때 뜨는 툴팁(Tooltip)에도 버전 표시
    tooltip_text = display_title

    icon = pystray.Icon("인터페이스 원격로그인 프로그램 서버", image, tooltip_text, menu)
    icon.run()

def wakeup():
    kernel32 = ctypes.windll.kernel32
    required_state = (
        ES_CONTINUOUS
        | ES_SYSTEM_REQUIRED
        | ES_DISPLAY_REQUIRED
    )
    try:
        while True:
            if kernel32.SetThreadExecutionState(required_state) == 0:
                raise ctypes.WinError()
            time.sleep(_SERVER["wakeup_refresh_interval_seconds"])
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)

def do_work_server(client, addr, ial_worker_control=None):
    print('client :', addr)
    try:
        serverip = client.getsockname()[0]
    except (AttributeError, OSError):
        serverip = socket.gethostbyname(socket.gethostname())
    print(serverip)

    try:
        cmd_r = mynetlib.my_recv(
            MESSAGE_CHUNK_SIZE,
            client,
            total_timeout=mynetlib.DEFAULT_SERVER_CLIENT_TIMEOUT,
        )
    except socket.timeout:
        ErrorLog(
            f"요청 수신 시간 초과: client={addr[0]}"
        )
        return
    except (
        mynetlib.IncompleteMessageError,
        mynetlib.MessageTooLargeError,
    ) as e:
        ErrorLog(
            f"잘못된 요청 데이터: client={addr[0]}, error={e}"
        )
        return
    print("서버 수신 cmd_r:", cmd_r, type(cmd_r))

    # 1) cmd_r 유효성 체크
    if not isinstance(cmd_r, (list, tuple)) or len(cmd_r) not in (3, 4):
        ErrorLog(f"잘못된 요청 또는 수신 실패: {cmd_r}")
        FAIL = (serverip, "int_failed")
        try:
            mynetlib.my_send(FAIL, client)
        except Exception as e:
            ErrorLog(f"응답 전송 실패: {e}")
        return

    a, b, c = cmd_r[:3]
    request_id = cmd_r[3] if len(cmd_r) == 4 else None
    print("파싱된 값:", a, b, c, request_id)

    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > REQUEST_ID_MAX_LENGTH
    ):
        ErrorLog(f"잘못된 request_id: {request_id!r}")
        mynetlib.my_send(
            (serverip, "int_failed", request_id),
            client,
        )
        return

    # 신형 클라이언트가 직접 응답 지원 여부를 먼저 확인한다.
    if c == CAPABILITIES_COMMAND:
        mynetlib.my_send(
            {
                "type": "capabilities",
                "protocol_version": PROTOCOL_VERSION,
                "server_version": SERVER_VERSION,
                "features": DIRECT_RESULT_FEATURES,
            },
            client,
        )
        return

    # ★ 2) 구형 클라이언트의 결과 요청(R) 처리
    if c == LEGACY_RESULT_COMMAND:
        if request_id is not None:
            mynetlib.my_send(
                (serverip, "int_failed", request_id),
                client,
            )
            return
        try:
            # 결과가 아직 없으면 오래 기다리지 않고 연결을 끝낸다.
            # 구형 클라이언트가 자체 재시도하면서 완료 결과를 받는다.
            claim = _claim_legacy_result(addr, a, b)
            res = claim[2][1] if claim is not None else None
            if isinstance(res, str) and res:
                delivered = False
                try:
                    mynetlib.my_send((serverip, res), client)
                    delivered = True
                finally:
                    _finish_legacy_result_claim(claim, delivered)
            else:
                ErrorLog(
                    "아직 준비되지 않은 구형 결과 요청: "
                    f"client={addr[0]}, user={a!r}"
                )
            return

        except Exception as e:
            ErrorLog(f"결과 요청 처리 중 예외 발생: {e!r}")
            try:
                mynetlib.my_send((serverip, "int_failed"), client)
            except Exception as e2:
                ErrorLog(f"예외 이후 클라이언트 응답 실패: {e2!r}")
            return

    # ★ 3) 여기까지 왔으면 R이 아닌 로그인/명령 요청
    request_fingerprint = (a, b, c)
    direct_inflight_entry = None
    if request_id is not None:
        request_role, request_state = _begin_direct_request(
            addr,
            request_id,
            request_fingerprint,
        )
        if request_role == "completed":
            (
                _,
                previous_fingerprint,
                previous_server_ip,
                previous_status,
            ) = request_state
            if previous_fingerprint != request_fingerprint:
                ErrorLog(
                    "동일 request_id의 요청 내용이 일치하지 않습니다: "
                    f"client={addr[0]}, request_id={request_id!r}"
                )
                previous_status = "int_failed"
                previous_server_ip = serverip
            mynetlib.my_send(
                (previous_server_ip, previous_status, request_id),
                client,
            )
            return
        if request_role == "conflict":
            ErrorLog(
                "실행 중인 동일 request_id의 요청 내용이 "
                "일치하지 않습니다: "
                f"client={addr[0]}, request_id={request_id!r}"
            )
            mynetlib.my_send(
                (serverip, "int_failed", request_id),
                client,
            )
            return
        if request_role == "follower":
            if not request_state.completed.wait(
                DIRECT_INFLIGHT_WAIT_TIMEOUT
            ):
                ErrorLog(
                    "동일 request_id의 실행 결과 대기 시간이 "
                    "초과되었습니다: "
                    f"client={addr[0]}, request_id={request_id!r}"
                )
                mynetlib.my_send(
                    (serverip, "int_failed", request_id),
                    client,
                )
                return
            replay = _get_direct_result(addr, request_id)
            if replay is None:
                ErrorLog(
                    "동일 request_id의 완료 결과를 찾지 못했습니다: "
                    f"client={addr[0]}, request_id={request_id!r}"
                )
                mynetlib.my_send(
                    (serverip, "int_failed", request_id),
                    client,
                )
                return
            (
                _,
                previous_fingerprint,
                previous_server_ip,
                previous_status,
            ) = replay
            if previous_fingerprint != request_fingerprint:
                ErrorLog(
                    "완료된 동일 request_id의 요청 내용이 "
                    "일치하지 않습니다: "
                    f"client={addr[0]}, request_id={request_id!r}"
                )
                previous_server_ip = serverip
                previous_status = "int_failed"
            mynetlib.my_send(
                (previous_server_ip, previous_status, request_id),
                client,
            )
            return
        direct_inflight_entry = request_state
    else:
        # 이전 로그인 결과가 회수되지 않았더라도 새 요청보다 먼저
        # 반환되지 않도록 같은 사용자의 오래된 결과를 버린다.
        _clear_legacy_results(addr, a, b)

    if not _ial_execution_lock.acquire(blocking=False):
        ErrorLog(
            "다른 로그인 요청 처리 중 새 요청을 거절했습니다: "
            f"client={addr[0]}, command={c!r}"
        )
        if request_id is not None:
            _complete_direct_request(
                addr,
                request_id,
                direct_inflight_entry,
                request_fingerprint,
                serverip,
                BUSY_RESULT_CODE,
            )
            mynetlib.my_send(
                (serverip, BUSY_RESULT_CODE, request_id),
                client,
            )
        else:
            # 구형 클라이언트는 별도 결과 요청에서 기존 실패 코드만
            # 이해하므로 호환 가능한 실패 결과를 저장한다.
            _store_legacy_result(addr, a, b, "int_failed")
        return

    try:
        try:
            execute_options = {}
            if ial_worker_control is not None:
                execute_options["worker_control"] = ial_worker_control
            status = execute_ial_with_hard_timeout(
                a,
                b,
                c,
                **execute_options,
            )
        except Exception as e:
            ErrorLog(f"IAL 작업 프로세스 실행 중 예외 발생: {e!r}")
            status = "int_failed"

        if status not in RESULT_CODES:
            ErrorLog(f"알 수 없는 실행 결과: {status!r}")
            status = "int_failed"

        # 실행 잠금을 해제하기 전에 결과를 저장해야 동일 요청의
        # 재접속이 새 작업으로 실행되지 않는다.
        if request_id is not None:
            _complete_direct_request(
                addr,
                request_id,
                direct_inflight_entry,
                request_fingerprint,
                serverip,
                status,
            )
        IAL.int_result = ""
        if request_id is None:
            _store_legacy_result(addr, a, b, status)
    finally:
        _ial_execution_lock.release()

    # 신형 요청은 같은 연결에서 request_id와 함께 직접 응답한다.
    # 구형 요청은 뒤이어 들어오는 R 요청이 별도 대기열의 결과를 읽는다.
    if request_id is not None:
        mynetlib.my_send(
            (serverip, status, request_id),
            client,
        )


def do_work_server_safely(
    client,
    addr,
    ial_worker_control=None,
):
    try:
        do_work_server(
            client,
            addr,
            ial_worker_control=ial_worker_control,
        )
    except Exception as error:
        ErrorLog(
            "클라이언트 요청 처리 중 예외: "
            f"client={addr[0]}, error={error!r}"
        )


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


def _program_data_root():
    return Path(
        expand_path(_INSTALLATION["runtime_program_data_dir"])
    )


def server_update_state_path():
    return _program_data_root() / _INSTALLATION[
        "server_update_state_relative_path"
    ]


def server_health_path():
    return _program_data_root() / _INSTALLATION[
        "server_health_filename"
    ]


def write_server_update_state(state, **details):
    value = {
        "schema_version": 1,
        "state": state,
        "current_version": SERVER_VERSION,
        "updated_at": _utc_timestamp(),
        **details,
    }
    atomic_write_json(server_update_state_path(), value)
    return value


def _server_listening():
    TaskKill(PI)
    atomic_write_json(
        server_health_path(),
        {
            "schema_version": 1,
            "status": "ready",
            "version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "port": SERVER_PORT,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "ready_at": _utc_timestamp(),
        },
    )


def _verified_update_exists(path, update):
    path = Path(path)
    if not path.is_file():
        return False
    expected_size = update.get("size")
    if expected_size is not None and path.stat().st_size != expected_size:
        return False
    return file_sha256(path) == update["sha256"]


def _failed_update_is_in_cooldown(target_version):
    try:
        with server_update_state_path().open(
            "r",
            encoding="utf-8-sig",
        ) as file:
            state = json.load(file)
        if state.get("state") not in {
            "failed",
            "failed_rolled_back",
            ROLLBACK_START_STATE,
        }:
            return False
        if str(state.get("target_version")) != str(target_version):
            return False
        updated_at = datetime.fromisoformat(
            str(state["updated_at"]).replace("Z", "+00:00")
        )
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_seconds = (
            datetime.now(timezone.utc) - updated_at
        ).total_seconds()
        return (
            0 <= age_seconds
            < _SERVER_UPDATE["failure_retry_delay_seconds"]
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        return False


def update_state_blocks_this_server_start():
    try:
        with server_update_state_path().open(
            "r",
            encoding="utf-8-sig",
        ) as file:
            state = json.load(file)
        if state.get("state") not in {
            "draining",
            "draining_complete",
            "ready_to_install",
            "installing",
            "health_check",
            "rollback",
            ROLLBACK_START_STATE,
        }:
            return False
        updated_at = datetime.fromisoformat(
            str(state["updated_at"]).replace("Z", "+00:00")
        )
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_seconds = (
            datetime.now(timezone.utc) - updated_at
        ).total_seconds()
        state_is_fresh = (
            0 <= age_seconds
            <= _SERVER_UPDATE["state_stale_timeout_seconds"]
        )
        allowed_version = (
            state.get("allowed_server_version")
            if state.get("state") == ROLLBACK_START_STATE
            else state.get("target_version")
        )
        return state_is_fresh and str(allowed_version) != SERVER_VERSION
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        return False


def check_server_update_once():
    manifest = fetch_manifest(
        _UPDATE["manifest_url"],
        _UPDATE["request_timeout_seconds"],
        requests_module=requests,
    )
    update = get_component_update(
        manifest,
        "server",
        SERVER_VERSION,
    )
    if update is None:
        return None
    if _failed_update_is_in_cooldown(update["version"]):
        ErrorLog(
            "동일 서버 버전의 직전 설치 실패로 재시도를 "
            f"보류합니다: {update['version']}"
        )
        return None

    destination = server_update_destination(
        _CONFIG,
        update["version"],
    )
    if not _verified_update_exists(destination, update):
        download_verified_file(
            update["url"],
            destination,
            update["sha256"],
            expected_size=update.get("size"),
            request_timeout=(
                _UPDATE["download_connect_timeout_seconds"],
                _UPDATE["download_read_timeout_seconds"],
            ),
            total_timeout=_UPDATE[
                "download_total_timeout_seconds"
            ],
            requests_module=requests,
        )

    update = {
        **update,
        "installer_path": str(destination),
    }
    write_server_update_state(
        "downloaded_verified",
        target_version=update["version"],
        installer_path=update["installer_path"],
        sha256=update["sha256"],
    )
    return update


def server_update_loop(stop_event, ready_queue):
    if not _SERVER_UPDATE["automatic"]:
        return
    if stop_event.wait(_SERVER_UPDATE["initial_delay_seconds"]):
        return

    while not stop_event.is_set():
        try:
            update = check_server_update_once()
            if update is not None:
                ready_queue.put_nowait(update)
                return
        except Exception as error:
            ErrorLog(f"서버 자동업데이트 확인 실패: {error!r}")

        if stop_event.wait(
            _SERVER_UPDATE["check_interval_seconds"]
        ):
            return


def start_server_update_monitor(ready_queue):
    stop_event = threading.Event()
    thread = threading.Thread(
        target=server_update_loop,
        args=(stop_event, ready_queue),
        name="RIL_Server_Update_Monitor",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def wait_for_update_helper_ready(
    process,
    ready_path,
    target_version,
):
    deadline = (
        time.monotonic()
        + _SERVER_UPDATE["helper_start_timeout_seconds"]
    )
    try:
        while time.monotonic() < deadline:
            if ready_path.is_file():
                try:
                    with ready_path.open(
                        "r",
                        encoding="utf-8-sig",
                    ) as file:
                        ready = json.load(file)
                    if (
                        int(ready.get("helper_pid", -1))
                        == int(process.pid)
                        and str(ready.get("target_version"))
                        == str(target_version)
                    ):
                        exit_code = process.poll()
                        if exit_code is not None:
                            raise RuntimeError(
                                "서버 업데이트 helper가 준비 직후 "
                                "종료됐습니다. "
                                f"(종료 코드: {exit_code})"
                            )
                        return
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    json.JSONDecodeError,
                ):
                    pass

            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(
                    "서버 업데이트 helper가 준비 전에 종료됐습니다. "
                    f"(종료 코드: {exit_code})"
                )
            time.sleep(
                _SERVER_UPDATE[
                    "helper_start_poll_interval_seconds"
                ]
            )

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(
                    timeout=_SERVER_UPDATE[
                        "thread_join_timeout_seconds"
                    ]
                )
            except subprocess.TimeoutExpired:
                process.kill()
        raise TimeoutError(
            "서버 업데이트 helper 준비 확인 시간이 초과됐습니다."
        )
    finally:
        ready_path.unlink(missing_ok=True)


def launch_server_update(update):
    installer_path = Path(update["installer_path"]).resolve()
    helper_source = resource_path(
        _INSTALLATION["server_update_helper_script"]
    )
    restarter_source = resource_path(
        _INSTALLATION["server_restarter_power_shell_script"]
    )
    if not installer_path.is_file():
        raise FileNotFoundError(installer_path)
    if not helper_source.is_file():
        raise FileNotFoundError(helper_source)
    if not restarter_source.is_file():
        raise FileNotFoundError(restarter_source)

    helper_copy = installer_path.parent / helper_source.name
    restarter_copy = installer_path.parent / restarter_source.name
    shutil.copy2(helper_source, helper_copy)
    shutil.copy2(restarter_source, restarter_copy)
    effective_config_path = (
        installer_path.parent
        / _INSTALLATION["server_effective_config_filename"]
    )
    effective_config = deepcopy(_CONFIG)
    effective_config["release"]["version"] = SERVER_VERSION
    effective_config["release"]["protocol_version"] = PROTOCOL_VERSION
    atomic_write_json(effective_config_path, effective_config)
    helper_ready_path = (
        installer_path.parent
        / _INSTALLATION["server_update_helper_ready_filename"]
    )
    helper_ready_path.unlink(missing_ok=True)
    state_path = server_update_state_path()
    write_server_update_state(
        "ready_to_install",
        target_version=update["version"],
        installer_path=str(installer_path),
        helper_path=str(helper_copy),
        restarter_path=str(restarter_copy),
        effective_config_path=str(effective_config_path),
        helper_ready_path=str(helper_ready_path),
        parent_pid=os.getpid(),
    )

    command = [
        _SERVER_UPDATE["power_shell_executable"],
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper_copy),
        "-InstallerPath",
        str(installer_path),
        "-TargetVersion",
        update["version"],
        "-InstallDir",
        APP_DIR,
        "-ParentPid",
        str(os.getpid()),
        "-StatePath",
        str(state_path),
        "-HealthPath",
        str(server_health_path()),
        "-ConfigPath",
        str(effective_config_path),
        "-ReadyPath",
        str(helper_ready_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(installer_path.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    wait_for_update_helper_ready(
        process,
        helper_ready_path,
        update["version"],
    )


def run_server2(
    activity_event=None,
    drain_requested_event=None,
    drain_complete_event=None,
    ial_worker_control=None,
):
    # listener는 서버 프로세스 수명 동안 유지한다. bind/listen 실패는
    # 상위 watchdog이 감지할 수 있도록 이 프로세스를 종료시킨다.
    activity_lock = threading.Lock()
    active_request_count = 0

    def handle_request(client_socket, address):
        nonlocal active_request_count
        with activity_lock:
            active_request_count += 1
            if activity_event is not None:
                activity_event.set()
        try:
            do_work_server_safely(
                client_socket,
                address,
                ial_worker_control=ial_worker_control,
            )
        finally:
            with activity_lock:
                active_request_count -= 1
                if (
                    activity_event is not None
                    and active_request_count == 0
                ):
                    activity_event.clear()

    try:
        mynetlib.run_server(
            SERVER_PORT,
            handle_request,
            s_count=None,
            on_listening=_server_listening,
            stop_event=drain_requested_event,
            concurrent_handlers=True,
        )
    except BaseException:
        raise
    else:
        if drain_complete_event is not None:
            drain_complete_event.set()


def _finish_ial_worker_shutdown(control):
    _clear_ial_worker_identity(control)
    control.idle_event.set()


def _ial_worker_control_is_confirmed_idle(control):
    if not control.idle_event.is_set():
        return False
    identity = _read_ial_worker_identity(control)
    return (
        identity == (0, 0.0, 0)
        and not control.identity_ready_event.is_set()
    )


def _stop_exact_tracked_ial_worker(
    control,
    server_process,
    join_timeout,
):
    identity = _read_ial_worker_identity(control)
    if identity is None:
        raise RuntimeError(
            "IAL 작업 프로세스 신원 상태가 불안정합니다."
        )
    pid, expected_create_time, owner_pid = identity
    if pid <= 0:
        raise RuntimeError(
            "IAL 작업 프로세스 종료 여부를 확인할 수 없습니다."
        )

    listener_pid = getattr(server_process, "pid", None)
    if listener_pid is not None and owner_pid != int(listener_pid):
        raise RuntimeError(
            "IAL 작업 프로세스 소유자가 listener와 일치하지 않습니다."
        )

    try:
        worker = psutil.Process(pid)
    except psutil.NoSuchProcess:
        _finish_ial_worker_shutdown(control)
        return

    try:
        actual_create_time = worker.create_time()
    except psutil.NoSuchProcess:
        _finish_ial_worker_shutdown(control)
        return
    except psutil.Error as error:
        raise RuntimeError(
            "IAL 작업 프로세스 신원을 확인하지 못했습니다."
        ) from error

    if actual_create_time != expected_create_time:
        ErrorLog(
            "IAL worker PID가 다른 프로세스에 재사용되어 "
            f"종료하지 않습니다: pid={pid}"
        )
        _finish_ial_worker_shutdown(control)
        return

    try:
        listener_alive = server_process.is_alive()
    except (AttributeError, OSError):
        listener_alive = False
    if listener_alive:
        try:
            if worker.ppid() != owner_pid:
                raise RuntimeError(
                    "IAL 작업 프로세스의 실제 부모가 listener와 "
                    "일치하지 않습니다."
                )
        except psutil.NoSuchProcess:
            _finish_ial_worker_shutdown(control)
            return
        except psutil.Error as error:
            raise RuntimeError(
                "IAL 작업 프로세스 부모를 확인하지 못했습니다."
            ) from error

    try:
        worker.terminate()
        try:
            worker.wait(timeout=join_timeout)
        except psutil.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=join_timeout)
    except psutil.NoSuchProcess:
        pass
    except (
        psutil.Error,
        OSError,
    ) as error:
        raise RuntimeError(
            "IAL 작업 프로세스를 종료하지 못했습니다."
        ) from error

    _finish_ial_worker_shutdown(control)


def _stop_controlled_ial_worker(
    control,
    server_process,
    join_timeout,
):
    if control is None:
        return

    control.cancel_event.set()
    try:
        listener_alive = server_process.is_alive()
    except (AttributeError, OSError):
        listener_alive = False
    if listener_alive:
        lock_acquired = control.start_lock.acquire(
            timeout=IAL_CHILD_JOIN_TIMEOUT
        )
        if lock_acquired:
            control.start_lock.release()
    wait_timeout = IAL_CONTROL_WAIT_TIMEOUT if listener_alive else 0
    control.idle_event.wait(wait_timeout)
    if _ial_worker_control_is_confirmed_idle(control):
        return

    if not control.identity_ready_event.is_set():
        control.identity_ready_event.wait(
            IAL_IDENTITY_READY_TIMEOUT
        )
    if _ial_worker_control_is_confirmed_idle(control):
        return

    _stop_exact_tracked_ial_worker(
        control,
        server_process,
        join_timeout,
    )


def _stop_processes(
    processes,
    join_timeout,
    ial_worker_control=None,
    server_process=None,
):
    if ial_worker_control is not None:
        if server_process is None and len(processes) > 1:
            server_process = processes[1]
        if server_process is None:
            raise RuntimeError(
                "IAL 작업 프로세스 소유 listener가 없습니다."
            )
        _stop_controlled_ial_worker(
            ial_worker_control,
            server_process,
            join_timeout,
        )

    for process in processes:
        if process.is_alive():
            process.terminate()

    for process in processes:
        process.join(timeout=join_timeout)

    survivors = [
        process
        for process in processes
        if process.is_alive()
    ]
    for process in survivors:
        process.kill()

    for process in survivors:
        process.join(timeout=join_timeout)

    survivors = [
        process
        for process in survivors
        if process.is_alive()
    ]
    if survivors:
        names = ", ".join(
            getattr(process, "name", type(process).__name__)
            for process in survivors
        )
        message = (
            "자식 프로세스를 종료하지 못했습니다: "
            f"{names}"
        )
        ErrorLog(message)
        raise RuntimeError(message)


def _cancel_durable_server_update(pending_update):
    try:
        write_server_update_state(
            "cancelled",
            target_version=pending_update["version"],
            installer_path=pending_update["installer_path"],
            cancel_reason="requested_stop",
        )
    except Exception as state_error:
        ErrorLog(
            "서버 업데이트 취소 상태 기록 실패 - "
            "draining 상태 파일을 제거합니다: "
            f"{state_error!r}"
        )
        try:
            server_update_state_path().unlink(missing_ok=True)
        except OSError as remove_error:
            ErrorLog(
                "서버 업데이트 draining 상태 파일 제거 실패: "
                f"{remove_error!r}"
            )


def run_supervised_processes(
    tray_process,
    server_process,
    wakeup_process,
    restart,
    poll_interval=SUPERVISOR_POLL_INTERVAL,
    join_timeout=SUPERVISOR_JOIN_TIMEOUT,
    requested_stop=None,
    tray_factory=None,
    wakeup_factory=None,
    update_queue=None,
    update_launcher=None,
    activity_event=None,
    drain_requested_event=None,
    drain_complete_event=None,
    ial_worker_control=None,
):
    processes = [
        tray_process,
        server_process,
        wakeup_process,
    ]
    started_processes = []
    try:
        for process in processes:
            process.start()
            started_processes.append(process)
    except Exception:
        _stop_processes(
            started_processes,
            join_timeout,
            ial_worker_control=ial_worker_control,
            server_process=server_process,
        )
        raise

    pending_update = None
    drain_started_at = None
    while True:
        if requested_stop is not None and requested_stop.is_set():
            ErrorLog("tray 사용자 종료 - 프로그램 종료")
            print("tray 사용자 종료 - 프로그램 종료")
            _stop_processes(
                processes,
                join_timeout,
                ial_worker_control=ial_worker_control,
                server_process=server_process,
            )
            if pending_update is not None and drain_started_at is not None:
                _cancel_durable_server_update(pending_update)
            return "stop"

        if pending_update is None and update_queue is not None:
            try:
                pending_update = update_queue.get_nowait()
            except queue.Empty:
                pass

        if (
            pending_update is not None
            and drain_started_at is None
            and drain_requested_event is not None
        ):
            write_server_update_state(
                "draining",
                target_version=pending_update["version"],
                installer_path=pending_update["installer_path"],
            )
            drain_started_at = time.monotonic()
            drain_requested_event.set()

        if drain_complete_event is not None:
            drain_complete = drain_complete_event.is_set()
            drain_timed_out = (
                drain_started_at is not None
                and time.monotonic() - drain_started_at
                >= _SERVER_UPDATE["drain_timeout_seconds"]
            )
            listener_dead_during_drain = (
                pending_update is not None
                and drain_started_at is not None
                and not drain_complete
                and not server_process.is_alive()
            )
            update_ready = (
                pending_update is not None
                and (
                    drain_complete
                    or drain_timed_out
                    or listener_dead_during_drain
                )
            )
            if drain_complete:
                drain_reason = "listener_ack"
            elif listener_dead_during_drain:
                drain_reason = "listener_dead"
            elif drain_timed_out:
                drain_reason = "timeout"
            else:
                drain_reason = None
        else:
            server_busy = (
                activity_event is not None
                and activity_event.is_set()
                and server_process.is_alive()
            )
            update_ready = (
                pending_update is not None
                and not server_busy
            )
            drain_reason = "activity_idle" if update_ready else None

        if update_ready:
            if drain_reason == "timeout":
                ErrorLog(
                    "서버 업데이트 drain 제한시간 초과 - "
                    "자식 프로세스를 종료합니다."
                )
            elif drain_reason == "listener_dead":
                ErrorLog(
                    "서버 업데이트 drain 중 listener가 종료되어 "
                    "검증된 업데이트를 우선 진행합니다: "
                    "reason=listener_dead"
                )
            ErrorLog(
                "검증된 서버 업데이트 설치를 위해 "
                "자식 프로세스를 종료합니다."
            )
            _stop_processes(
                processes,
                join_timeout,
                ial_worker_control=ial_worker_control,
                server_process=server_process,
            )
            try:
                write_server_update_state(
                    "draining_complete",
                    target_version=pending_update["version"],
                    installer_path=pending_update["installer_path"],
                    drain_reason=drain_reason,
                )
                update_launcher(pending_update)
            except Exception as error:
                ErrorLog(f"서버 업데이트 실행 실패: {error!r}")
                try:
                    write_server_update_state(
                        "failed",
                        target_version=pending_update["version"],
                        installer_path=pending_update["installer_path"],
                        error=repr(error),
                    )
                except Exception as state_error:
                    ErrorLog(
                        "서버 업데이트 실패 상태 기록 실패: "
                        f"{state_error!r}"
                    )
                    try:
                        server_update_state_path().unlink(
                            missing_ok=True,
                        )
                    except OSError as remove_error:
                        ErrorLog(
                            "서버 업데이트 상태 파일 제거 실패: "
                            f"{remove_error!r}"
                        )
                restart()
                return "restart"
            return "update"

        durable_drain_active = (
            pending_update is not None
            and drain_started_at is not None
        )

        if not tray_process.is_alive():
            if durable_drain_active:
                ErrorLog(
                    "서버 업데이트 drain 중 tray가 종료되어 "
                    "전체 재시작을 생략합니다."
                )
            elif tray_factory is None:
                ErrorLog("tray 비정상 종료 - 프로그램 재시작")
                print("tray 비정상 종료 - 프로그램 재시작")
                _stop_processes(
                    processes,
                    join_timeout,
                    ial_worker_control=ial_worker_control,
                    server_process=server_process,
                )
                restart()
                return "restart"
            else:
                ErrorLog("tray 비정상 종료 - tray만 재시작")
                print("tray 비정상 종료 - tray만 재시작")
                tray_process.join(timeout=join_timeout)
                try:
                    replacement_tray = tray_factory()
                    replacement_tray.start()
                except Exception as error:
                    ErrorLog(f"tray 재시작 실패: {error!r}")
                else:
                    tray_process = replacement_tray
                    processes[0] = replacement_tray

        if not server_process.is_alive():
            if durable_drain_active:
                ErrorLog(
                    "서버 업데이트 drain 중 listener가 종료되어 "
                    "다음 감독 주기에서 업데이트를 진행합니다."
                )
            else:
                ErrorLog("server종료 - 프로그램 재시작")
                print("server종료 - 프로그램 재시작")
                _stop_processes(
                    processes,
                    join_timeout,
                    ial_worker_control=ial_worker_control,
                    server_process=server_process,
                )
                restart()
                return "restart"

        if not wakeup_process.is_alive():
            if durable_drain_active:
                ErrorLog(
                    "서버 업데이트 drain 중 wakeup이 종료되어 "
                    "전체 재시작을 생략합니다."
                )
            elif wakeup_factory is None:
                ErrorLog("wakeup종료 - 프로그램 재시작")
                print("wakeup종료 - 프로그램 재시작")
                _stop_processes(
                    processes,
                    join_timeout,
                    ial_worker_control=ial_worker_control,
                    server_process=server_process,
                )
                restart()
                return "restart"
            else:
                ErrorLog("wakeup종료 - wakeup만 재시작")
                print("wakeup종료 - wakeup만 재시작")
                wakeup_process.join(timeout=join_timeout)
                try:
                    wakeup_process = wakeup_factory()
                    wakeup_process.start()
                except Exception as error:
                    ErrorLog(f"wakeup 재시작 실패: {error!r}")
                    _stop_processes(
                        processes,
                        join_timeout,
                        ial_worker_control=ial_worker_control,
                        server_process=server_process,
                    )
                    restart()
                    return "restart"
                processes[2] = wakeup_process

        time.sleep(poll_interval)


if __name__ == "__main__":
    # Windows/PyInstaller multiprocessing worker가 아래 초기화에
    # 진입하지 않도록 가장 먼저 호출한다.
    freeze_support()
    if update_state_blocks_this_server_start():
        sys.exit()
    if not acquire_single_instance_mutex():
        sys.exit()

    try:
        ErrorLog(f"### RIL_server v{SERVER_VERSION} STARTED ###")
        start_time = time.perf_counter()
        requested_stop = multiprocessing.Event()
        server_activity = multiprocessing.Event()
        update_drain_requested = multiprocessing.Event()
        update_drain_complete = multiprocessing.Event()
        ial_worker_control = _IalWorkerControl()
        update_ready_queue = queue.Queue(maxsize=1)
        update_stop_event, update_thread = (
            start_server_update_monitor(update_ready_queue)
        )

        def new_tray_process():
            return mp(
                target=make_trayicon,
                args=(requested_stop,),
            )

        p0 = new_tray_process()
        p1 = mp(
            target=run_server2,
            args=(
                server_activity,
                update_drain_requested,
                update_drain_complete,
                ial_worker_control,
            ),
        )
        p2 = mp(target=wakeup)
        try:
            action = run_supervised_processes(
                p0,
                p1,
                p2,
                restartscript,
                requested_stop=requested_stop,
                tray_factory=new_tray_process,
                wakeup_factory=lambda: mp(target=wakeup),
                update_queue=update_ready_queue,
                update_launcher=launch_server_update,
                activity_event=server_activity,
                drain_requested_event=update_drain_requested,
                drain_complete_event=update_drain_complete,
                ial_worker_control=ial_worker_control,
            )
        finally:
            update_stop_event.set()
            update_thread.join(
                timeout=_SERVER_UPDATE[
                    "thread_join_timeout_seconds"
                ]
            )
        if action == "stop":
            finish_time = time.perf_counter()
            ErrorLog(
                "Program finished in "
                f"{(finish_time - start_time):.3f} seconds"
            )
    finally:
        release_single_instance_mutex()
#pyinstaller -w -F --uac-admin --clean --icon=chunsik1.ico --exclude pandas, --exclude numpy, --exclude pillow RIL_server.py
