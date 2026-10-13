"""Check the installer's shared Windows mutex before loading the client UI."""

import ctypes
from ctypes import wintypes
import os
import subprocess
import base64


def _gate_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenMutexW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    return api


def pending_update(name):
    api = _gate_api()
    handle = api.OpenMutexW(0x100000, False, name + "_ClientPending")
    if handle:
        api.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error == 2:
        return False
    raise ctypes.WinError(error)


class PendingUpdate:
    """Existence-based gate: inherited by a helper before the client exits."""

    def __init__(self, name):
        self.api = _gate_api()
        self.handle = self.api.CreateMutexW(None, False, name + "_ClientPending")
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        if error == 183:
            self.close()
            raise RuntimeError("다른 클라이언트가 이미 업데이트 중입니다.")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def handoff(self, command, **kwargs):
        # CreateProcess inherits the live handle atomically; no file/PID timeout gap.
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [self.handle]}
        os.set_handle_inheritable(self.handle, True)
        try:
            process = subprocess.Popen(command, startupinfo=startup, close_fds=True, **kwargs)
        finally:
            os.set_handle_inheritable(self.handle, False)
        self.close()
        return process

    def launch_installer(self, installer, arguments, client_executable, env):
        def literal(value):
            return "'" + str(value).replace("'", "''") + "'"

        windows = os.environ.get("SystemRoot", r"C:\Windows")
        powershell = os.path.join(windows, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        argument_line = subprocess.list2cmdline([*arguments, "/CLIENT_HANDOFF"])
        script = (
            "$ErrorActionPreference='Stop'; "
            f"$gate=[Microsoft.Win32.SafeHandles.SafeWaitHandle]::new([IntPtr]{self.handle},$true); "
            "$code=1; $p=$null; try { "
            f"$p=Start-Process -FilePath {literal(installer)} -ArgumentList {literal(argument_line)} "
            "-PassThru -WindowStyle Hidden; $p.WaitForExit(); $p.Refresh(); $code=$p.ExitCode "
            "} catch { if ($null -eq $p) { $code=12 } else { throw } } "
            "finally { $gate.Dispose() }; "
            # Managed rollback must restart only after both install and pending locks end.
            f"if ($code -eq 12) {{ Start-Process -FilePath {literal(client_executable)} }}; "
            "exit $code"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return self.handoff(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=windows, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def show_update_notice(message):
    # Do not hold RIL_client.exe open while the user reads the notice.
    windows = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = os.path.join(windows, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    script = (
        "Add-Type -AssemblyName PresentationFramework; "
        "[System.Windows.MessageBox]::Show('"
        + message.replace("'", "''")
        + "','RIL 업데이트') | Out-Null"
    )
    subprocess.Popen(
        [powershell, "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden", "-Command", script],
        cwd=windows,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def update_in_progress(name, kernel32=None):
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenMutexW(0x100001, False, name)
    if not handle:
        error = ctypes.get_last_error()
        if error == 2:
            return False
        raise ctypes.WinError(error)
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result in (0, 128):
            kernel32.ReleaseMutex(handle)
            return False
        if result == 258:
            return True
        raise OSError("업데이트 잠금 상태를 확인하지 못했습니다.")
    finally:
        kernel32.CloseHandle(handle)
