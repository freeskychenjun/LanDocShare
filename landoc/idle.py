"""空闲判定：最近 N 分钟无键鼠输入，或当前会话已锁定（设计 §6.7）。

建库任务只在用户离开时推进，避免 Office 实例抢 CPU/弹窗打扰。
判定要"宁可不跑"：读不到输入时间时按"不空闲"处理。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
# 注意：这个函数没有 A/W 变体，导出名就叫 OpenInputDesktop
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_ulonglong

DESKTOP_READOBJECTS = 0x0001
ERROR_ACCESS_DENIED = 5


def _tick_ms() -> int:
    return int(kernel32.GetTickCount64())


def _last_input_ms() -> int | None:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return int(info.dwTime)


def seconds_since_input() -> float | None:
    """距最后一次键鼠输入的秒数；读不到返回 None。"""
    last = _last_input_ms()
    if last is None:
        return None
    # dwTime 是 32 位、49.7 天回绕：先按无符号取差，再取模回正
    elapsed = ((_tick_ms() & 0xFFFFFFFF) - last) & 0xFFFFFFFF
    return elapsed / 1000.0


def is_session_locked() -> bool:
    """工作站是否锁定。打开输入桌面被拒（ERROR_ACCESS_DENIED）即视为锁定。"""
    desktop = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if desktop:
        user32.CloseDesktop(desktop)
        return False
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED


def is_idle(minutes: float = 5.0) -> bool:
    """锁屏，或连续 minutes 分钟没有键鼠输入。"""
    if is_session_locked():
        return True
    seconds = seconds_since_input()
    if seconds is None:
        return False
    return seconds >= minutes * 60
