"""Windows hibernation with an absolute UTC wake timer. No changes to power policy."""
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import os
import re
import subprocess


def parse_wake(text, now=None):
    now = now or datetime.now().astimezone()
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", text):
            hour, minute = map(int, text.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
        else:
            target = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
    except ValueError:
        raise ValueError("Время: /sleep 08:30 или /sleep 2026-10-25 08:30. Часовой пояс компьютера.") from None
    if target.timestamp() - now.timestamp() < 120:
        raise ValueError("До пробуждения должно оставаться минимум 2 минуты.")
    return target


def filetime(target):
    # Positive FILETIME is absolute UTC. Relative timers pause in Windows 8+ sleep.
    return int((target.timestamp() + 11644473600) * 10_000_000)


class WindowsPower:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Гибернация реализована только для Windows.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.powr = ctypes.WinDLL("powrprof", use_last_error=True)
        self.kernel.CreateWaitableTimerW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateWaitableTimerW.restype = wintypes.HANDLE
        self.kernel.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong),
                                               wintypes.LONG, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
        self.kernel.SetWaitableTimer.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CancelWaitableTimer.argtypes = [wintypes.HANDLE]
        self.kernel.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        self.kernel.SetThreadExecutionState.restype = wintypes.DWORD
        self.powr.IsPwrHibernateAllowed.restype = ctypes.c_ubyte
        self.powr.SetSuspendState.argtypes = [ctypes.c_ubyte] * 3
        self.powr.SetSuspendState.restype = ctypes.c_ubyte

    def keep_awake(self, enabled):
        # Thread-scoped: call and release on the same long-lived runtime thread.
        # No DISPLAY_REQUIRED: the monitor can remain off. Explicit sleep still works.
        flags = 0x80000000 | (0x00000001 if enabled else 0)
        if not self.kernel.SetThreadExecutionState(flags):
            raise ctypes.WinError(ctypes.get_last_error())

    def preflight(self):
        if not self.powr.IsPwrHibernateAllowed():
            raise RuntimeError("Гибернация недоступна. Включи её в Windows: powercfg /hibernate on (администратор).")
        class Status(ctypes.Structure):
            _fields_ = [("ac", ctypes.c_ubyte), ("battery", ctypes.c_ubyte),
                        ("percent", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte),
                        ("life", wintypes.DWORD), ("full", wintypes.DWORD)]
        status = Status()
        if not self.kernel.GetSystemPowerStatus(ctypes.byref(status)) or status.ac not in (0, 1):
            raise RuntimeError("Не удалось определить источник питания Windows.")
        command = os.path.join(os.environ["SystemRoot"], "System32", "powercfg.exe")
        result = subprocess.run([command, "/query", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"],
                                capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        indices = re.findall(rb"0x([0-9a-fA-F]{8})", result.stdout)
        if result.returncode or len(indices) < 2:
            raise RuntimeError("Не удалось проверить разрешение таймеров пробуждения.")
        index = int(indices[-2 if status.ac == 1 else -1], 16)
        if index != 1:
            raise RuntimeError("В Windows отключены обычные таймеры пробуждения для текущего источника питания. "
                               "Электропитание → дополнительные параметры → Сон → таймеры пробуждения → Включить.")

    def enable_shutdown_privilege(self):
        class Luid(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]
        class Privileges(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("luid", Luid), ("attributes", wintypes.DWORD)]
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(Luid)]
        adv.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(Privileges),
                                             wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        token, privileges = wintypes.HANDLE(), Privileges()
        if not adv.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x20 | 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            privileges.count, privileges.attributes = 1, 2
            if not adv.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(privileges.luid)):
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.set_last_error(0)
            ok = adv.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None)
            if not ok or ctypes.get_last_error():
                raise RuntimeError("Windows не предоставила этому процессу право на гибернацию.")
        finally:
            self.kernel.CloseHandle(token)

    def hibernate_until(self, wake):
        self.preflight()
        if wake.timestamp() - datetime.now().timestamp() < 60:
            raise RuntimeError("Время пробуждения слишком близко или уже прошло. Гибернация отменена.")
        self.enable_shutdown_privilege()
        handle = self.kernel.CreateWaitableTimerW(None, True, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            due = ctypes.c_longlong(filetime(wake))
            ctypes.set_last_error(0)
            ok = self.kernel.SetWaitableTimer(handle, ctypes.byref(due), 0, None, None, True)
            if not ok or ctypes.get_last_error() == 50:
                raise RuntimeError("Windows не смогла установить таймер пробуждения. Компьютер остаётся включённым.")
            if not self.powr.SetSuspendState(True, False, False):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CancelWaitableTimer(handle)
            self.kernel.CloseHandle(handle)
