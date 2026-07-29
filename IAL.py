#soc1_server.py
#<컴퓨터 간 접속상태 확인을 위해 1회 접속처리>
from pywinauto.application import Application
from pywinauto import timings
import psutil
import ctypes, ctypes.wintypes, time
import pygetwindow as gw
import win32api, os
import threading
import win32gui
import win32process
import subprocess
import re

from ril_config import expand_path, load_config


_CONFIG = load_config()
_INTERFACES = _CONFIG["interfaces"]
_AUTOMATION = _INTERFACES["automation"]
_EXECUTABLES = _INTERFACES["executable_names"]
_GENERAL = _INTERFACES["general"]
_OSMO = _INTERFACES["osmo"]
_NOVA = _INTERFACES["nova"]

TITLE_OSMO1 = _OSMO["1"]["title"]
TITLE_OSMO2 = _OSMO["2"]["title"]

#파일명
INT = _EXECUTABLES["general"]
INTOS1 = _EXECUTABLES["osmo_1"]
INTOS2 = _EXECUTABLES["osmo_2"]
INTCA1 = _EXECUTABLES["nova_1"]
INTCA2 = _EXECUTABLES["nova_2"]
INTAURSLT = _EXECUTABLES["au_result"]

#폴더명
_GENERAL_EXECUTABLE_PATHS = tuple(
    expand_path(path)
    for path in _GENERAL["configured_executable_paths"]
)
_GENERAL_DISCOVERY_ROOTS = tuple(
    expand_path(path)
    for path in _GENERAL["discovery_roots"]
)
INTP = _GENERAL_EXECUTABLE_PATHS[0]
INTF = os.path.dirname(INTP)
INTFOS1 = expand_path(_OSMO["1"]["directory"])
INTFOS2 = expand_path(_OSMO["2"]["directory"])
INTFCA1 = expand_path(_NOVA["1"]["directory"])
INTFCA2 = expand_path(_NOVA["2"]["directory"])
AU_CONFIG = {
    int(number): {
        "order_dir": expand_path(values["order_directory"]),
        "result_dir": expand_path(values["result_directory"]),
        "order_title": values["order_title"],
        "result_title": values["result_title"],
    }
    for number, values in _INTERFACES["au"].items()
}

#폴더+파일명
INTPOS1 = os.path.join(INTFOS1, INTOS1)
INTPOS2 = os.path.join(INTFOS2, INTOS2)
INTPCA1 = os.path.join(INTFCA1, INTCA1)
INTPCA2 = os.path.join(INTFCA2, INTCA2)

INTT = _AUTOMATION["login_window_title"]
Novaprime1 = _NOVA["1"]["title"]
Novaprime2 = _NOVA["2"]["title"]
int_result = ""

WINDOW_WAIT_TIMEOUT = _AUTOMATION["login_timeout_seconds"]
WINDOW_WAIT_INTERVAL = _AUTOMATION["window_wait_poll_interval_seconds"]
PROCESS_STOP_TIMEOUT = _AUTOMATION["process_stop_timeout_seconds"]
PROCESS_FORCE_WAIT = _AUTOMATION["process_force_wait_seconds"]
COMPONENT_WINDOW_TIMEOUT = _AUTOMATION[
    "component_window_timeout_seconds"
]
GENERAL_WINDOW_TIMEOUT = _AUTOMATION["general_window_timeout_seconds"]
ALIGNMENT_DELAY = _AUTOMATION["default_alignment_delay_seconds"]
ALIGNMENT_ATTEMPTS = _AUTOMATION["alignment_attempts"]
ALIGNMENT_INTERVAL = _AUTOMATION[
    "alignment_retry_interval_seconds"
]


def _title_contains(actual_title, expected_title):
    return expected_title.casefold() in (actual_title or "").casefold()

def snapshot_window_handles():
    """숨은 창을 포함한 기존 top-level 창을 저장한다."""
    handles = set()

    def collect_window(handle, _extra):
        if handle and win32gui.IsWindow(handle):
            handles.add(handle)
        return True

    try:
        win32gui.EnumWindows(collect_window, None)
    except Exception as e:
        print("[snapshot_window_handles] error:", repr(e))
    return handles


def _window_process_ids_with_title(title_substring):
    """현재 창 제목에 대응하는 프로세스 ID만 일시적으로 찾는다."""
    process_ids = set()

    def collect_window(handle, _extra):
        try:
            title = win32gui.GetWindowText(handle) or ""
            if _title_contains(title, title_substring):
                _thread_id, process_id = (
                    win32process.GetWindowThreadProcessId(handle)
                )
                if process_id:
                    process_ids.add(process_id)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect_window, None)
    return process_ids


def _window_owned_by_executable(handle, expected_executable):
    """창 소유 프로세스의 실제 실행파일이 기대 경로와 같은지 확인한다."""
    if not expected_executable:
        return False
    try:
        _thread_id, process_id = (
            win32process.GetWindowThreadProcessId(handle)
        )
        if not process_id:
            return False
        actual_executable = psutil.Process(process_id).exe()
        if not actual_executable:
            return False
        return _same_directory(actual_executable, expected_executable)
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        OSError,
    ):
        return False
    except Exception:
        return False


# 메인 창이 실제로 떴는지 기다리는 함수
def wait_for_window_title_contains(
    substr,
    expected_executable,
    timeout=WINDOW_WAIT_TIMEOUT,
    interval=WINDOW_WAIT_INTERVAL,
    exclude_handles=None,
    title_predicate=None,
):
    """
    윈도우 타이틀에 substr 가 포함된 창이 나타날 때까지 대기.
    나타나면 True, timeout 지나면 False 리턴.
    """
    excluded = set(exclude_handles or ())
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            for w in gw.getAllWindows():
                title = w.title or ""
                handle = w._hWnd
                if (
                    (
                        title_predicate(title)
                        if title_predicate is not None
                        else _title_contains(title, substr)
                    )
                    and handle not in excluded
                    and win32gui.IsWindowVisible(handle)
                    and _window_owned_by_executable(
                        handle,
                        expected_executable,
                    )
                ):
                    print(f"[wait_for_window_title_contains] found: {title!r}")
                    return True
        except Exception as e:
            print("[wait_for_window_title_contains] error:", repr(e))
        time.sleep(interval)
    print(f"[wait_for_window_title_contains] timeout waiting for substring: {substr!r}")
    return False


def _is_general_interface_title(title):
    """일반 장비의 정상 메인 창 제목만 허용한다."""
    normalized = " ".join((title or "").split())
    folded = normalized.casefold()
    error_markers = tuple(_GENERAL["rejected_title_markers"])
    if any(marker in folded for marker in error_markers):
        return False
    if re.search(r"\b(?:jit|exception|error)\b", folded):
        return False

    return _title_contains(
        normalized,
        _GENERAL["success_title_contains"],
    )
    
def _same_directory(left, right):
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return (
            os.path.normcase(os.path.realpath(left))
            == os.path.normcase(os.path.realpath(right))
        )


def _append_unique_path(paths, executable):
    if not any(_same_directory(executable, value) for value in paths):
        paths.append(executable)


def _find_task_targets(
    prg,
    target_dir=None,
    require_unique_path=False,
    allowed_process_ids=None,
):
    targets = []
    executable_paths = []
    missing_path_count = 0
    allowed_ids = (
        set(allowed_process_ids)
        if allowed_process_ids is not None
        else None
    )
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if (proc.info.get("name") or "").casefold() != prg.casefold():
                continue
            process_id = proc.info.get("pid")
            if process_id is None:
                process_id = getattr(proc, "pid", None)
            if allowed_ids is not None and process_id not in allowed_ids:
                continue
            executable = proc.info.get("exe")
            if not executable:
                if target_dir is None:
                    missing_path_count += 1
                continue
            if target_dir is not None and (
                not _same_directory(
                    os.path.dirname(executable),
                    target_dir,
                )
            ):
                continue
            targets.append(proc)
            _append_unique_path(executable_paths, executable)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if missing_path_count:
        raise RuntimeError(
            f"{prg} 프로세스 {missing_path_count}개의 실행 경로를 "
            "확인할 수 없어 안전하게 종료하지 않았습니다."
        )

    if require_unique_path and len(executable_paths) > 1:
        raise RuntimeError(
            "같은 이름의 인터페이스가 여러 폴더에서 실행 중입니다: "
            + " | ".join(executable_paths)
        )

    return targets, executable_paths


def _find_profile_task_targets(
    process_name,
    configured_path,
    window_title,
    label,
):
    """
    같은 파일명을 쓰는 장비 중 현재 profile만 선택한다.

    PID는 종료 전 창과 프로세스를 연결할 때만 사용하며 저장하거나
    재시작 뒤의 창 검색에 사용하지 않는다.
    """
    title_process_ids = _window_process_ids_with_title(window_title)
    if title_process_ids:
        targets, paths = _find_task_targets(
            process_name,
            require_unique_path=True,
            allowed_process_ids=title_process_ids,
        )
        if targets:
            actual_directory = os.path.dirname(paths[0])
            return _find_task_targets(
                process_name,
                target_dir=actual_directory,
                require_unique_path=True,
            )

    # 이전 원격 로그인에서 인증에 실패해 메인 title 대신 공통
    # 로그인 창만 남은 경우에도, 해당 파일명의 후보가 하나일 때는
    # 종료 전 실제 경로를 다시 사용할 수 있게 한다.
    login_process_ids = _window_process_ids_with_title(INTT)
    if login_process_ids:
        login_targets, login_paths = _find_task_targets(
            process_name,
            require_unique_path=True,
            allowed_process_ids=login_process_ids,
        )
        if login_targets:
            all_targets, all_paths = _find_task_targets(
                process_name,
                require_unique_path=True,
            )
            if (
                len(login_paths) == 1
                and len(all_paths) == 1
                and _same_directory(login_paths[0], all_paths[0])
            ):
                # 동일 실제 파일의 title 없는 추가 인스턴스도 함께
                # 종료해 재실행을 방해하지 않게 한다.
                return all_targets, all_paths
            raise RuntimeError(
                f"{label}: 로그인 창의 실행 경로를 하나로 "
                "확정할 수 없어 안전하게 종료하지 않았습니다."
            )

    configured_directory = os.path.dirname(configured_path)
    targets, paths = _find_task_targets(
        process_name,
        target_dir=configured_directory,
        require_unique_path=True,
    )
    if targets:
        return targets, paths

    other_targets, other_paths = _find_task_targets(
        process_name,
        require_unique_path=True,
    )
    if other_targets:
        raise RuntimeError(
            f"{label}: 실행 중인 프로세스의 창 제목과 설정 경로가 "
            "일치하지 않아 안전하게 종료하지 않았습니다: "
            + " | ".join(other_paths)
        )
    return [], []


def _stop_task_targets(
    prg,
    targets,
    timeout=PROCESS_STOP_TIMEOUT,
):
    for proc in targets:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            raise RuntimeError(
                f"{prg} 프로세스를 종료할 권한이 없습니다."
            ) from error

    if targets:
        _, alive = psutil.wait_procs(targets, timeout=timeout)
        for proc in alive:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied as error:
                raise RuntimeError(
                    f"{prg} 프로세스를 강제 종료할 권한이 없습니다."
                ) from error
        if alive:
            _, alive = psutil.wait_procs(
                alive,
                timeout=PROCESS_FORCE_WAIT,
            )
            if alive:
                raise RuntimeError(
                    f"{prg} 프로세스 {len(alive)}개가 종료되지 않았습니다."
                )


def TaskKill(
    prg,
    timeout=PROCESS_STOP_TIMEOUT,
    target_dir=None,
    require_unique_path=False,
):
    targets, executable_paths = _find_task_targets(
        prg,
        target_dir=target_dir,
        require_unique_path=require_unique_path,
    )
    _stop_task_targets(prg, targets, timeout=timeout)
    return executable_paths

def RunTask(run_f, run_prg1): 
    executable = os.path.join(run_f, run_prg1)
    result = win32api.ShellExecute(
        0,
        "open",
        executable,
        "",
        run_f,
        1,
    )
    if int(result) <= 32:
        raise OSError(
            f"인터페이스를 실행하지 못했습니다({int(result)}): "
            f"{executable}"
        )

def RunTaskInDirectory(run_f, run_prg1):
    executable = os.path.join(run_f, run_prg1)
    psutil.Popen([executable], cwd=run_f)

def RunTaskAsInteractiveUser(run_f, run_prg1):
    """Explorer와 같은 사용자 컨텍스트에서 프로그램을 실행한다."""
    executable = os.path.join(run_f, run_prg1)
    if not os.path.isfile(executable):
        raise FileNotFoundError(executable)

    size_type = ctypes.c_size_t
    byte_pointer = ctypes.POINTER(ctypes.wintypes.BYTE)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("lpReserved", ctypes.wintypes.LPWSTR),
            ("lpDesktop", ctypes.wintypes.LPWSTR),
            ("lpTitle", ctypes.wintypes.LPWSTR),
            ("dwX", ctypes.wintypes.DWORD),
            ("dwY", ctypes.wintypes.DWORD),
            ("dwXSize", ctypes.wintypes.DWORD),
            ("dwYSize", ctypes.wintypes.DWORD),
            ("dwXCountChars", ctypes.wintypes.DWORD),
            ("dwYCountChars", ctypes.wintypes.DWORD),
            ("dwFillAttribute", ctypes.wintypes.DWORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("wShowWindow", ctypes.wintypes.WORD),
            ("cbReserved2", ctypes.wintypes.WORD),
            ("lpReserved2", byte_pointer),
            ("hStdInput", ctypes.wintypes.HANDLE),
            ("hStdOutput", ctypes.wintypes.HANDLE),
            ("hStdError", ctypes.wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.wintypes.HANDLE),
            ("hThread", ctypes.wintypes.HANDLE),
            ("dwProcessId", ctypes.wintypes.DWORD),
            ("dwThreadId", ctypes.wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)

    user32.GetShellWindow.restype = ctypes.wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(size_type),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = ctypes.wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        size_type,
        ctypes.c_void_p,
        size_type,
        ctypes.c_void_p,
        ctypes.POINTER(size_type),
    ]
    kernel32.UpdateProcThreadAttribute.restype = ctypes.wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = ctypes.wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
    userenv.CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.BOOL,
    ]
    userenv.CreateEnvironmentBlock.restype = ctypes.wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    userenv.DestroyEnvironmentBlock.restype = ctypes.wintypes.BOOL

    def raise_last_error(action):
        error = ctypes.get_last_error()
        raise OSError(error, f"{action}: {ctypes.FormatError(error)}")

    parent_process = None
    shell_token = ctypes.wintypes.HANDLE()
    environment = ctypes.c_void_p()
    attribute_list = None
    attribute_list_initialized = False
    process_info = PROCESS_INFORMATION()

    try:
        shell_window = user32.GetShellWindow()
        if not shell_window:
            raise RuntimeError("현재 로그인 사용자의 Explorer 창을 찾지 못했습니다.")

        shell_process_id = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(
            shell_window,
            ctypes.byref(shell_process_id),
        )
        if not shell_process_id.value:
            raise_last_error("Explorer 프로세스 확인 실패")

        parent_process = kernel32.OpenProcess(
            0x0080 | 0x1000,
            False,
            shell_process_id.value,
        )
        if not parent_process:
            raise_last_error("Explorer 프로세스 열기 실패")

        attribute_size = size_type()
        kernel32.InitializeProcThreadAttributeList(
            None,
            1,
            0,
            ctypes.byref(attribute_size),
        )
        if not attribute_size.value:
            raise_last_error("프로세스 속성 크기 확인 실패")

        attribute_buffer = ctypes.create_string_buffer(
            attribute_size.value,
        )
        attribute_list = ctypes.cast(
            attribute_buffer,
            ctypes.c_void_p,
        )
        if not kernel32.InitializeProcThreadAttributeList(
            attribute_list,
            1,
            0,
            ctypes.byref(attribute_size),
        ):
            raise_last_error("프로세스 속성 초기화 실패")
        attribute_list_initialized = True

        parent_handle = ctypes.wintypes.HANDLE(parent_process)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            0x00020000,
            ctypes.byref(parent_handle),
            ctypes.sizeof(parent_handle),
            None,
            None,
        ):
            raise_last_error("Explorer 부모 프로세스 지정 실패")

        if not advapi32.OpenProcessToken(
            parent_process,
            0x0002 | 0x0008,
            ctypes.byref(shell_token),
        ):
            raise_last_error("Explorer 사용자 토큰 열기 실패")

        if not userenv.CreateEnvironmentBlock(
            ctypes.byref(environment),
            shell_token,
            False,
        ):
            raise_last_error("Explorer 사용자 환경 생성 실패")

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.lpDesktop = "winsta0\\default"
        startup.StartupInfo.dwFlags = 0x00000001
        startup.StartupInfo.wShowWindow = 1
        startup.lpAttributeList = attribute_list

        command_line = ctypes.create_unicode_buffer(
            subprocess.list2cmdline([executable])
        )
        creation_flags = 0x00080000 | 0x00000400
        if not kernel32.CreateProcessW(
            executable,
            command_line,
            None,
            None,
            False,
            creation_flags,
            environment,
            run_f,
            ctypes.byref(startup.StartupInfo),
            ctypes.byref(process_info),
        ):
            raise_last_error("현재 로그인 사용자로 인터페이스 실행 실패")

        print(
            f"[RunTaskAsInteractiveUser] 실행 경로: {executable}",
            flush=True,
        )
    finally:
        if process_info.hThread:
            kernel32.CloseHandle(process_info.hThread)
        if process_info.hProcess:
            kernel32.CloseHandle(process_info.hProcess)
        if environment:
            userenv.DestroyEnvironmentBlock(environment)
        if shell_token:
            kernel32.CloseHandle(shell_token)
        if attribute_list_initialized:
            kernel32.DeleteProcThreadAttributeList(attribute_list)
        if parent_process:
            kernel32.CloseHandle(parent_process)

def _discover_interface_executable(stopped_paths):
    remembered = []
    for path in stopped_paths:
        if path:
            _append_unique_path(remembered, path)

    if remembered:
        if len(remembered) > 1:
            raise RuntimeError(
                "종료 전 인터페이스 실행 경로가 여러 개입니다: "
                + " | ".join(remembered)
            )
        executable = remembered[0]
        if not os.path.isfile(executable):
            raise FileNotFoundError(
                "종료 전에 실행 중이던 인터페이스 파일을 "
                f"찾지 못했습니다: {executable}"
            )
        return executable

    configured_paths = [
        path
        for path in _GENERAL_EXECUTABLE_PATHS
        if os.path.isfile(path)
    ]

    if len(configured_paths) == 1:
        return configured_paths[0]
    if len(configured_paths) > 1:
        raise RuntimeError(
            "설정된 인터페이스 실행파일이 여러 개 존재합니다: "
            + " | ".join(configured_paths)
        )

    discovered = []
    roots = _GENERAL_DISCOVERY_ROOTS
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.scandir(root):
                if not entry.is_dir():
                    continue
                candidate = os.path.join(entry.path, INT)
                if os.path.isfile(candidate):
                    discovered.append(candidate)
        except OSError:
            continue

    if len(discovered) == 1:
        return discovered[0]
    if len(discovered) > 1:
        raise RuntimeError(
            "여러 인터페이스 실행파일이 발견되어 자동 선택할 수 없습니다: "
            + " | ".join(discovered)
        )
    raise FileNotFoundError(
        "Ui.Kumc.GR.Interface.exe 실행 경로를 찾지 못했습니다."
    )


def _resolve_restart_executable(
    stopped_paths,
    configured_path,
    label,
):
    remembered = []
    for path in stopped_paths:
        if path:
            _append_unique_path(remembered, path)

    if remembered:
        if len(remembered) > 1:
            raise RuntimeError(
                f"{label}의 종료 전 실행 경로가 여러 개입니다: "
                + " | ".join(remembered)
            )
        executable = remembered[0]
        if not os.path.isfile(executable):
            raise FileNotFoundError(
                f"{label}: 종료 전에 실행 중이던 파일을 찾지 못했습니다: "
                f"{executable}"
            )
        return executable

    if os.path.isfile(configured_path):
        return configured_path
    raise FileNotFoundError(
        f"{label} 실행파일을 찾지 못했습니다: {configured_path}"
    )


def _run_resolved_executable(executable, runner):
    run_dir = os.path.dirname(executable)
    run_name = os.path.basename(executable)
    attempts = _AUTOMATION["launch_attempts"]
    retry_delay = _AUTOMATION["launch_retry_delay_seconds"]
    for attempt in range(1, attempts + 1):
        try:
            print(
                f"[restart] 실행 경로: {executable} "
                f"({attempt}/{attempts})",
                flush=True,
            )
            runner(run_dir, run_name)
            return
        except Exception:
            if attempt >= attempts:
                raise
            time.sleep(retry_delay)


def _prepare_restart_executables(component_specs):
    """
    각 인터페이스의 실제 실행 경로를 종료 전에 보존한다.

    component_specs 항목:
    (표시명, 프로세스 파일명, 설정상 대체 경로, 실행 함수)
    """
    plans = []
    for component_spec in component_specs:
        label, process_name, configured_path, runner = component_spec[:4]
        window_title = (
            component_spec[4]
            if len(component_spec) > 4
            else None
        )
        if window_title is None:
            targets, running_paths = _find_task_targets(
                process_name,
                require_unique_path=True,
            )
        else:
            targets, running_paths = _find_profile_task_targets(
                process_name,
                configured_path,
                window_title,
                label,
            )
        executable = _resolve_restart_executable(
            running_paths,
            configured_path,
            label,
        )
        plans.append(
            (process_name, targets, executable, runner)
        )

    stopped = []
    try:
        for process_name, targets, executable, runner in plans:
            _stop_task_targets(process_name, targets)
            stopped.append((executable, runner))
    except Exception:
        # 일부 구성요소 종료에 실패해도 이미 완전히 종료된 앞
        # 구성요소는 로그인 창 상태로라도 즉시 복구한다.
        for executable, runner in stopped:
            try:
                _run_resolved_executable(executable, runner)
            except Exception as restore_error:
                print(
                    "[restart] 준비 실패 후 인터페이스 복구 실패: "
                    f"{executable}: {restore_error!r}",
                    flush=True,
                )
        raise

    return [
        executable
        for _process_name, _targets, executable, _runner in plans
    ]


def align_after_login(
    delay=ALIGNMENT_DELAY,
    au_number=None,
    attempts=ALIGNMENT_ATTEMPTS,
    interval=ALIGNMENT_INTERVAL,
    synchronous=False,
):
    """로그인 성공 후 창 정렬. AU는 제한된 횟수만 재시도한다."""
    def worker():
        time.sleep(delay)
        try:
            if au_number is None:
                print(f"[align_after_login] {delay}초 후 movemove() 호출")
                movemove()
                return

            max_attempts = max(1, int(attempts))
            retry_interval = max(0, float(interval))
            for attempt in range(1, max_attempts + 1):
                print(
                    f"[align_after_login] AU {au_number} 정렬 시도 "
                    f"{attempt}/{max_attempts}"
                )
                if findau(au_number):
                    print(f"[align_after_login] AU {au_number} 정렬 완료")
                    return
                if attempt < max_attempts:
                    time.sleep(retry_interval)

            print(
                f"[align_after_login] AU {au_number} 정렬 실패: "
                f"{max_attempts}회 탐색 후 종료"
            )
        except Exception as e:
            print("[align_after_login] movemove 오류:", e)

    if synchronous:
        worker()
        return
    threading.Thread(target=worker, daemon=True).start()

def _get_nova_windows():
    """현재 떠 있는 Nova 인터페이스 창들을 리스트로 반환."""
    wins = []
    try:
        for w in gw.getAllWindows():
            title = w.title or ""
            if not win32gui.IsWindowVisible(w._hWnd):
                continue
            # Nova 인터페이스 창만 필터
            if (
                any(
                    _title_contains(title, values["title"])
                    for values in _NOVA.values()
                )
                and _title_contains(
                    title,
                    _GENERAL["success_title_contains"],
                )
            ):
                wins.append(w)
    except Exception as e:
        print("[_get_nova_windows] error:", repr(e))
        return []

    # 제목 기준으로 정렬 (1, 2 순서 보장용)
    wins.sort(key=lambda w: (w.title or "").casefold())
    return wins

def novaprime1_int():
    """첫 번째 Nova 창을 왼쪽 반 화면으로 이동 (두 창 다 있을 때만 의미 있음)."""
    wins = _get_nova_windows()
    if len(wins) < 2:
        # 창이 2개 미만이면 정렬하지 않음
        raise RuntimeError("novaprime1_int: Nova 창이 2개가 아니라서 정렬하지 않음")

    w1 = wins[0]
    print("[novaprime1_int] 왼쪽으로 이동:", repr(w1.title))
    try:
        left, top, right, bottom = _get_window_work_area(w1)
        work_width = right - left
        _place_window(
            w1,
            left,
            top,
            work_width // 2,
            bottom - top,
        )
    except Exception as e:
        print("[novaprime1_int] move/resize error:", repr(e))

def novaprime2_int():
    """두 번째 Nova 창을 오른쪽 반 화면으로 이동 (두 창 다 있을 때만 의미 있음)."""
    wins = _get_nova_windows()
    if len(wins) < 2:
        # 창이 2개 미만이면 정렬하지 않음
        raise RuntimeError("novaprime2_int: Nova 창이 2개가 아니라서 정렬하지 않음")

    w1, w2 = wins[:2]
    print("[novaprime2_int] 오른쪽으로 이동:", repr(w2.title))
    try:
        left, top, right, bottom = _get_window_work_area(w1)
        work_width = right - left
        left_width = work_width // 2
        _place_window(
            w2,
            left + left_width,
            top,
            work_width - left_width,
            bottom - top,
        )
    except Exception as e:
        print("[novaprime2_int] move/resize error:", repr(e))
        
def findnova():
    """NOVA 창 정렬:
       - 창이 2개 이상일 때만 좌/우 반반 정렬
       - 1개 이하이면 아무 것도 하지 않고 리턴
    """
    try:
        wins = _get_nova_windows()

        print("[findnova] Nova 창 검색 결과:")
        for w in wins:
            print("   -", repr(w.title))

        if not wins:
            print("[findnova] Nova 인터페이스 창이 없습니다.")
            return

        # 창이 1개뿐이면 정렬하지 않고 리턴
        if len(wins) < 2:
            print("[findnova] Nova 창이 1개만 떠 있어서 정렬하지 않고 종료합니다.")
            return

        # 여기까지 왔으면 2개 이상 → 앞의 2개만 좌/우 반반
        print("[findnova] Nova 창 2개 이상 -> 좌/우 반반 정렬")
        novaprime1_int()
        novaprime2_int()

    except Exception as e:
        print("findnova error:", repr(e))
        # 정렬 실패해도 로그인은 유지해야 하므로 그냥 넘어감
        pass

def _get_osmo_windows():
    """창 제목으로 현재 떠 있는 OSMO 1/2 창을 반환."""

    os1_win = None
    os2_win = None

    try:
        for w in gw.getAllWindows():
            title = w.title or ""
            if not win32gui.IsWindowVisible(w._hWnd):
                continue

            if _title_contains(title, TITLE_OSMO1) and os1_win is None:
                os1_win = w
                print("[_get_osmo_windows] OS1 창:", repr(title))
            elif _title_contains(title, TITLE_OSMO2) and os2_win is None:
                os2_win = w
                print("[_get_osmo_windows] OS2 창:", repr(title))
    except Exception as e:
        print("[_get_osmo_windows] window scan error:", repr(e))
        return os1_win, os2_win

    return os1_win, os2_win

def findosmo():
    """OSMO 창 정렬:
       - 두 창(OSMO2430-1, OSMO2430-2)이 모두 있을 때만 좌/우 반반 정렬
       - 하나만 있거나 아예 없으면 아무 것도 하지 않고 리턴
    """
    try:
        os1, os2 = _get_osmo_windows()

        print("[findosmo] OSMO 창 검색 결과:")
        if os1:
            print("   - OS1:", repr(os1.title))
        if os2:
            print("   - OS2:", repr(os2.title))

        # 둘 다 없으면 그냥 종료
        if not os1 and not os2:
            print("[findosmo] OSMO 인터페이스 창이 없습니다.")
            return

        # 둘 중 하나만 있으면 아무 것도 하지 않고 종료
        if (os1 and not os2) or (os2 and not os1):
            print("[findosmo] OSMO 창이 1개만 떠 있어서 정렬하지 않고 종료합니다.")
            return

        # 여기까지 왔으면 os1, os2 둘 다 있는 경우 → 좌/우 반반 정렬
        print("[findosmo] OS1/OS2 두 창 -> 좌/우 반반 정렬")
        try:
            left, top, right, bottom = _get_window_work_area(os1)
            work_width = right - left
            left_width = work_width // 2
            _place_window(
                os1,
                left,
                top,
                left_width,
                bottom - top,
            )
        except Exception as e:
            print("[findosmo] OS1 move/resize error:", repr(e))

        try:
            _place_window(
                os2,
                left + left_width,
                top,
                work_width - left_width,
                bottom - top,
            )
        except Exception as e:
            print("[findosmo] OS2 move/resize error:", repr(e))

    except Exception as e:
        print("findosmo error:", repr(e))
        pass
    
def _get_au_windows(au_number=None):
    """현재 떠 있는 AU 오더/결과 창들을 (order, result) 튜플로 반환."""
    order_win = None
    result_win = None
    order_title = None
    result_title = None
    if au_number is not None:
        config = AU_CONFIG[au_number]
        order_title = config["order_title"]
        result_title = config["result_title"]

    try:
        for w in gw.getAllWindows():
            title = w.title or ""
            if not win32gui.IsWindowVisible(w._hWnd):
                continue

            is_order_title = (
                _title_contains(title, order_title)
                if order_title is not None
                else any(
                    _title_contains(
                        title,
                        config["order_title"],
                    )
                    for config in AU_CONFIG.values()
                )
            )
            is_result_title = (
                _title_contains(title, result_title)
                if result_title is not None
                else any(
                    _title_contains(
                        title,
                        config["result_title"],
                    )
                    for config in AU_CONFIG.values()
                )
            )

            if is_order_title and order_win is None:
                order_win = w
            if is_result_title and result_win is None:
                result_win = w
    except Exception as e:
        print("[_get_au_windows] error:", repr(e))
        return None, None

    return order_win, result_win

def _get_primary_work_area():
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    if ctypes.windll.user32.SystemParametersInfoW(
        0x0030,
        0,
        ctypes.byref(rect),
        0,
    ):
        return rect.left, rect.top, rect.right, rect.bottom

    width = ctypes.windll.user32.GetSystemMetrics(0)
    height = ctypes.windll.user32.GetSystemMetrics(1)
    return 0, 0, width, height

def _get_window_work_area(window):
    try:
        monitor = win32api.MonitorFromWindow(window._hWnd, 2)
        return tuple(win32api.GetMonitorInfo(monitor)["Work"])
    except Exception:
        return _get_primary_work_area()

def _place_window(window, left, top, width, height):
    window.restore()
    time.sleep(_AUTOMATION["window_move_settle_delay_seconds"])
    window.moveTo(left, top)
    window.resizeTo(width, height)

def findau(au_number=None):
    """AU 오더/리절트 정렬:
       - 오더(차세대) / 결과(Result) 두 창이 모두 있을 때만 좌/우 반반 정렬
       - 하나만 있거나 아예 없으면 아무 것도 하지 않고 리턴
    """
    try:
        order_win, result_win = _get_au_windows(au_number)

        print("[findau] AU 창 검색 결과:")
        if order_win:
            print("   - ORDER:", repr(order_win.title))
        if result_win:
            print("   - RESULT:", repr(result_win.title))

        # 둘 다 없으면 종료
        if not order_win and not result_win:
            print("[findau] AU 인터페이스 창이 없습니다.")
            return False

        # 둘 중 하나만 있으면 아무 것도 하지 않고 종료
        if (order_win and not result_win) or (result_win and not order_win):
            print("[findau] AU 창이 1개만 떠 있어서 정렬하지 않고 종료합니다.")
            return False

        # 여기까지 왔으면 두 창 모두 있는 경우 → 좌/우 반반
        print("[findau] ORDER/RESULT 두 창 -> 좌/우 반반 정렬")
        left, top, right, bottom = _get_window_work_area(order_win)
        work_width = right - left
        work_height = bottom - top
        order_width = work_width // 2
        result_width = work_width - order_width

        _place_window(order_win, left, top, order_width, work_height)
        _place_window(
            result_win,
            left + order_width,
            top,
            result_width,
            work_height,
        )
        return True

    except Exception as e:
        print("findau error:", repr(e))
        return False

def movemove():
    print("[movemove] 호출됨")    
    try :
        findosmo()
    except Exception as e:
        print(e)
    try :
        findau()
    except Exception as e:
        print(e)
    try :
        findnova()
    except Exception as e:
        print(e)        


def _select_login_window(
    windows,
    expected_executable,
    exclude_handles=None,
):
    """새 로그인 창 중 기대 실행파일이 소유한 창만 고른다."""
    excluded = set(exclude_handles or ())
    for window in windows:
        try:
            handle = window._hWnd
            if (
                handle in excluded
                or (window.title or "").strip() != INTT
                or not win32gui.IsWindow(handle)
                or not win32gui.IsWindowVisible(handle)
                or window.width <= 10
                or not _window_owned_by_executable(
                    handle,
                    expected_executable,
                )
            ):
                continue
        except Exception:
            continue
        return handle
    return None


def _escape_sendkeys_text(value):
    replacements = {
        "+": "{+}",
        "^": "{^}",
        "%": "{%}",
        "~": "{~}",
        "(": "{(}",
        ")": "{)}",
        "[": "{[}",
        "]": "{]}",
        "{": "{{}",
        "}": "{}}",
    }
    return "".join(replacements.get(char, char) for char in value)


def login_common(
    app,
    int_id,
    int_pw,
    expected_executable,
    timeout=_AUTOMATION["login_timeout_seconds"],
    exclude_handles=None,
):
    import time
    import pygetwindow as gw
    import win32gui
    import win32com.client
    import ctypes 
    import socket 

    def log(msg):
        t_date = time.strftime("%y.%m.%d")
        t_time = time.strftime("%H:%M:%S")
        log_str = f"[{t_time}] [login_common] {msg}"
        print(log_str)
        try:
            log_directory = expand_path(
                _CONFIG["logging"]["directory"]
            )
            os.makedirs(log_directory, exist_ok=True)
            with open(
                os.path.join(
                    log_directory,
                    f"{_CONFIG['server']['log_filename_prefix']}"
                    f"{t_date}.txt",
                ),
                "a",
                encoding="utf-8",
            ) as f:
                f.write(log_str + "\n")
        except:
            pass 

    # 현재 실행 중인 PC의 이름을 가져와서 대문자로 변환
    current_pc_name = socket.gethostname().upper()
    
    special_focus_name = next(
        (
            name.upper()
            for name in _AUTOMATION[
                "special_focus_computer_names"
            ]
            if name.upper() in current_pc_name
        ),
        None,
    )

    log(f"함수 시작: 윈도우 창 탐색 대기중... (현재 PC 이름: {current_pc_name})")

    # 1. 윈도우 창 껍데기 찾기
    target_hwnd = None
    end_time = time.time() + timeout
    while time.time() < end_time:
        wins = gw.getWindowsWithTitle(INTT)
        target_hwnd = _select_login_window(
            wins,
            expected_executable,
            exclude_handles=exclude_handles,
        )
        if target_hwnd:
            log(f"화면에 보이는 진짜 창 발견! (HWND: {target_hwnd})")
            break 
        time.sleep(_AUTOMATION["login_poll_interval_seconds"])

    if not target_hwnd:
        log("에러: 타임아웃까지 창을 찾지 못했습니다.")
        raise RuntimeError("login_common: 로그인 창을 찾지 못했습니다.")

    # 2. 창 응답 상태 확인
    log("창 응답 상태(렉 걸림 여부) 확인 중...")
    is_responding = False
    responsive_attempts = _AUTOMATION["responsive_check_attempts"]
    responsive_timeout_ms = _AUTOMATION["responsive_check_timeout_ms"]
    responsive_delay = _AUTOMATION["login_poll_interval_seconds"]
    for i in range(responsive_attempts):
        res = win32gui.SendMessageTimeout(
            target_hwnd,
            0x0000,
            0,
            0,
            0x0002,
            responsive_timeout_ms,
        )
        if res[0] != 0:
            log(
                "창 응답 정상 확인! "
                f"(대기 {i * responsive_delay}초 소요)"
            )
            is_responding = True
            break
        log(
            "창 응답 없음(렉 걸림). "
            f"{responsive_delay}초 대기 후 재시도... "
            f"({i + 1}/{responsive_attempts})"
        )
        time.sleep(responsive_delay)

    if not is_responding:
        raise RuntimeError("login_common: 로그인 창이 응답하지 않습니다.")

    # ==========================================================
    # [스마트 분기] PC 이름에 따라 포커스 강탈 방식을 다르게 적용!
    # ==========================================================
    try:
        # 공통: 가짜 ALT 키보드 이벤트로 윈도우 방어막 1차 해제
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)       
        ctypes.windll.user32.keybd_event(0x12, 0, 0x0002, 0)  

        # Presepsin PC일 경우: 확실하고 무식한 최소화->복원 기법 사용 (깜빡임 O)
        if special_focus_name:
            log(
                f">>> [{special_focus_name} 전용] "
                "'최소화 후 복원' 강제 포커스 기법 작동"
            )
            ctypes.windll.user32.ShowWindow(target_hwnd, 6) # SW_MINIMIZE
            time.sleep(_AUTOMATION["activation_click_delay_seconds"])
            ctypes.windll.user32.ShowWindow(target_hwnd, 9) # SW_RESTORE
            time.sleep(_AUTOMATION["activation_click_delay_seconds"])
            ctypes.windll.user32.SwitchToThisWindow(target_hwnd, True)

        # 일반 다른 정상 PC들일 경우: 우아하게 바로 끌어올리기 (깜빡임 X)
        else:
            log(">>> [일반 PC] 기본 포커스 강탈 기법 작동")
            ctypes.windll.user32.ShowWindow(target_hwnd, 9) # SW_RESTORE

        # 공통: 맨 위로 끌어올리기 마무리
        ctypes.windll.user32.SetWindowPos(target_hwnd, -1, 0, 0, 0, 0, 3) 
        ctypes.windll.user32.BringWindowToTop(target_hwnd)
        ctypes.windll.user32.SetForegroundWindow(target_hwnd)
        
        time.sleep(_AUTOMATION["focus_settle_delay_seconds"])
    except Exception as e:
        log(f"포커스 강탈 에러: {e}")

    # ==========================================================
    # 마우스 클릭 및 좌표 검증
    # ==========================================================
    log("마우스 물리적 클릭(제목 표시줄) 및 활성화 검증 시작")
    activation_success = False
    old_x = None
    old_y = None
    try:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        old_x, old_y = pt.x, pt.y

        rect = win32gui.GetWindowRect(target_hwnd)
        click_offset_x, click_offset_y = _AUTOMATION[
            "activation_click_offset"
        ]
        safe_click_x = rect[0] + click_offset_x
        safe_click_y = rect[1] + click_offset_y
        
        activation_attempts = _AUTOMATION["activation_attempts"]
        for attempt in range(activation_attempts):
            ctypes.windll.user32.SetCursorPos(safe_click_x, safe_click_y)
            time.sleep(
                _AUTOMATION["activation_click_delay_seconds"]
            )
            ctypes.windll.user32.mouse_event(2, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)
            time.sleep(
                _AUTOMATION["activation_retry_delay_seconds"]
            )
            
            fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
            if fg_hwnd == target_hwnd:
                log("창 완벽 활성화(검은색 타이틀) 확인 완료!")
                activation_success = True
                break
            else:
                log(
                    "창 비활성화 상태. 재클릭 시도... "
                    f"({attempt + 1}/{activation_attempts}) / "
                    f"현재 포커스: {fg_hwnd}"
                )
                
    except Exception as e:
        log(f"마우스 클릭 중 에러: {e}")

    if not activation_success:
        log("에러: 로그인 창을 활성화하지 못해 키보드 입력을 중단합니다.")
        try:
            ctypes.windll.user32.SetWindowPos(target_hwnd, -2, 0, 0, 0, 0, 3)
            if old_x is not None and old_y is not None:
                ctypes.windll.user32.SetCursorPos(old_x, old_y)
        except Exception:
            pass
        raise RuntimeError("login_common: 로그인 창을 활성화하지 못했습니다.")

    def assert_target_ready(stage):
        if not win32gui.IsWindow(target_hwnd) or not win32gui.IsWindowVisible(target_hwnd):
            raise RuntimeError(f"login_common: {stage} 전에 로그인 창이 닫혔습니다.")
        if not _window_owned_by_executable(
            target_hwnd,
            expected_executable,
        ):
            raise RuntimeError(
                f"login_common: {stage} 전에 로그인 창의 "
                "실행 경로를 확인할 수 없거나 변경되었습니다."
            )
        if win32gui.GetForegroundWindow() != target_hwnd:
            raise RuntimeError(f"login_common: {stage} 전에 로그인 창의 포커스를 잃었습니다.")

    # 4. 다이렉트 키보드 매크로 발사
    log(f"키보드 매크로 발사 준비 (ID: {int_id})")
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        assert_target_ready("ID 입력")
        log("ID 입력 중...")
        shell.SendKeys(_escape_sendkeys_text(int_id))
        time.sleep(_AUTOMATION["id_input_delay_seconds"])
        assert_target_ready("ID 확인")
        shell.SendKeys("{ENTER}")
        time.sleep(_AUTOMATION["focus_settle_delay_seconds"])
        assert_target_ready("PW 입력")
        log("PW 입력 중...")
        shell.SendKeys(_escape_sendkeys_text(int_pw))
        time.sleep(_AUTOMATION["password_input_delay_seconds"])
        assert_target_ready("PW 확인")
        shell.SendKeys("{ENTER}")
        log("키보드 입력 완료!")
    except Exception as e:
        log(f"키보드 타이핑 에러: {e}")
        raise
    finally:
        # 5. 마무리
        log("마무리 작업(창 고정 해제 및 마우스 원위치)")
        try:
            ctypes.windll.user32.SetWindowPos(target_hwnd, -2, 0, 0, 0, 0, 3)
            if old_x is not None and old_y is not None:
                ctypes.windll.user32.SetCursorPos(old_x, old_y)
        except:
            pass
    log("함수 정상 종료됨.")

def StartTask(int_id, int_pw):
    global int_result
    int_result = ""
    try:
        targets, running_paths = _find_task_targets(
            INT,
            require_unique_path=True,
        )
        executable = _discover_interface_executable(running_paths)
        _stop_task_targets(INT, targets)
        print(f"[StartTask] 실행 경로: {executable}", flush=True)

        existing_windows = snapshot_window_handles()
        _run_resolved_executable(
            executable,
            RunTaskAsInteractiveUser,
        )
        app = Application(backend="win32")
        login_common(
            app,
            int_id,
            int_pw,
            expected_executable=executable,
            exclude_handles=existing_windows,
        )
        ok = wait_for_window_title_contains(
            _GENERAL["success_title_contains"],
            expected_executable=executable,
            timeout=GENERAL_WINDOW_TIMEOUT,
            exclude_handles=existing_windows,
            title_predicate=_is_general_interface_title,
        )
        if ok:
            int_result = "int_success"
        else:
            int_result = "int_failed"
    except Exception as e:
        print(f"[StartTask] 실패: {e!r}", flush=True)
        int_result = "int_failed"
    return int_result

def StartTaskOS(int_id, int_pw):
    global int_result
    int_result = ""
    osmo_executables = _prepare_restart_executables(
        (
            ("OSMO 1", INTOS1, INTPOS1, RunTask),
            ("OSMO 2", INTOS2, INTPOS2, RunTask),
        )
    )

    failed = []

    ok1 = StartTaskOS1(int_id, int_pw, osmo_executables[0])
    if not ok1:
        failed.append(1)
    time.sleep(_AUTOMATION["between_component_delay_seconds"])
    ok2 = StartTaskOS2(int_id, int_pw, osmo_executables[1])
    if not ok2:
        failed.append(2)

    if not failed:
        int_result = "int_success"
        align_after_login(
            delay=_AUTOMATION["osmo_alignment_delay_seconds"],
            synchronous=True,
        )
    elif len(failed) == 1:
        int_result = f"int_failed_{failed[0]}"
    else:
        int_result = "int_failed_1_2"
    return int_result

def StartTaskOS1(int_id, int_pw, executable):
    try:
        existing_windows = snapshot_window_handles()
        _run_resolved_executable(executable, RunTask)
        app = Application(backend="win32")
        login_common(
            app,
            int_id,
            int_pw,
            expected_executable=executable,
            exclude_handles=existing_windows,
        )
        ok = wait_for_window_title_contains(
            TITLE_OSMO1,
            expected_executable=executable,
            timeout=COMPONENT_WINDOW_TIMEOUT,
            exclude_handles=existing_windows,
        )
        if not ok:
            return False
        return True
    except Exception as e:
        print(f"[StartTaskOS1] 실패: {e!r}", flush=True)
        return False

def StartTaskOS2(int_id, int_pw, executable):
    try:
        existing_windows = snapshot_window_handles()
        _run_resolved_executable(executable, RunTask)
        app = Application(backend="win32")
        login_common(
            app,
            int_id,
            int_pw,
            expected_executable=executable,
            exclude_handles=existing_windows,
        )
        ok = wait_for_window_title_contains(
            TITLE_OSMO2,
            expected_executable=executable,
            timeout=COMPONENT_WINDOW_TIMEOUT,
            exclude_handles=existing_windows,
        )
        if not ok:
            return False
        return True
    except Exception as e:
        print(f"[StartTaskOS2] 실패: {e!r}", flush=True)
        return False
    
def StartTaskAU(int_id, int_pw, au_number):
    global int_result
    int_result = ""
    config = AU_CONFIG[au_number]
    au_executables = _prepare_restart_executables(
        (
            (
                f"AU {au_number} 오더",
                INT,
                os.path.join(config["order_dir"], INT),
                RunTaskInDirectory,
                config["order_title"],
            ),
            (
                f"AU {au_number} 결과",
                INTAURSLT,
                os.path.join(config["result_dir"], INTAURSLT),
                RunTaskInDirectory,
                config["result_title"],
            ),
        )
    )

    failed = []
    order_ok = StartTaskAUOrder(
        int_id,
        int_pw,
        config,
        au_number,
        au_executables[0],
    )
    if not order_ok:
        failed.append(1)
    result_ok = StartTaskAUResult(
        int_id,
        int_pw,
        config,
        au_number,
        au_executables[1],
    )
    if not result_ok:
        failed.append(2)

    if not failed:
        int_result = "int_success"
        align_after_login(
            au_number=au_number,
            synchronous=True,
        )
    elif len(failed) == 1:
        int_result = f"int_failed_{failed[0]}"
    else:
        int_result = "int_failed_1_2"
    return int_result

def StartTaskAUOrder(
    int_id,
    int_pw,
    config,
    au_number,
    executable,
):
    try:
        existing_windows = snapshot_window_handles()
        _run_resolved_executable(executable, RunTaskInDirectory)
        app = Application(backend="win32")
        login_common(
            app,
            int_id,
            int_pw,
            expected_executable=executable,
            exclude_handles=existing_windows,
        )
        title = config["order_title"]
        return wait_for_window_title_contains(
            title,
            expected_executable=executable,
            timeout=COMPONENT_WINDOW_TIMEOUT,
            exclude_handles=existing_windows,
        )
    except Exception as e:
        print(f"[StartTaskAUOrder] 실패: {e!r}", flush=True)
        return False

def StartTaskAUResult(
    int_id,
    int_pw,
    config,
    au_number,
    executable,
):
    try:
        existing_windows = snapshot_window_handles()
        _run_resolved_executable(executable, RunTaskInDirectory)
        app = Application(backend="win32")
        login_common(
            app,
            int_id,
            int_pw,
            expected_executable=executable,
            exclude_handles=existing_windows,
        )
        title = config["result_title"]
        return wait_for_window_title_contains(
            title,
            expected_executable=executable,
            timeout=COMPONENT_WINDOW_TIMEOUT,
            exclude_handles=existing_windows,
        )
    except Exception as e:
        print(f"[StartTaskAUResult] 실패: {e!r}", flush=True)
        return False
        
def StartTaskNovaPrime(int_id, int_pw):
    global int_result
    int_result = ""
    nova_executables = _prepare_restart_executables(
        (
            ("Nova Prime 1", INTCA1, INTPCA1, RunTask),
            ("Nova Prime 2", INTCA2, INTPCA2, RunTask),
        )
    )

    failed = []

    ok1 = StartTaskNova1(int_id, int_pw, nova_executables[0])
    if not ok1:
        failed.append(1)

    ok2 = StartTaskNova2(int_id, int_pw, nova_executables[1])
    if not ok2:
        failed.append(2)

    if not failed:
        int_result = "int_success"
        align_after_login(synchronous=True)
    elif len(failed) == 1:
        int_result = f"int_failed_{failed[0]}"
    else:
        int_result = "int_failed_1_2"
    return int_result

def StartTaskNova1(int_id, int_pw, executable):
    try:
        existing_windows = snapshot_window_handles()
        _run_resolved_executable(executable, RunTask)
        app = Application(backend="win32")
        login_common(
            app,
            int_id,
            int_pw,
            expected_executable=executable,
            exclude_handles=existing_windows,
        )
        ok = wait_for_window_title_contains(
            Novaprime1,
            expected_executable=executable,
            timeout=COMPONENT_WINDOW_TIMEOUT,
            exclude_handles=existing_windows,
        )
        if not ok:
            return False
        return True
    except Exception as e:
        print(f"[StartTaskNova1] 실패: {e!r}", flush=True)
        return False

def StartTaskNova2(int_id, int_pw, executable):
    try:
        existing_windows = snapshot_window_handles()
        _run_resolved_executable(executable, RunTask)
        app = Application(backend="win32")
        login_common(
            app,
            int_id,
            int_pw,
            expected_executable=executable,
            exclude_handles=existing_windows,
        )
        ok = wait_for_window_title_contains(
            Novaprime2,
            expected_executable=executable,
            timeout=COMPONENT_WINDOW_TIMEOUT,
            exclude_handles=existing_windows,
        )
        if not ok:
            return False
        return True
    except Exception as e:
        print(f"[StartTaskNova2] 실패: {e!r}", flush=True)
        return False
