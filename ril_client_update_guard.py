"""Check the installer's shared Windows mutex before loading the client UI."""

import ctypes
from ctypes import wintypes
import os
import subprocess


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
