"""修正 pystray 托盘图标漏填的 ``uID``，并在图标槽被抢占时自动重加。

pystray 0.19.5 的 ``Icon._message`` 是这么发的::

    win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(..., hID=id(self), ...))

而 ``NOTIFYICONDATAW`` 的字段名是 ``uID``。ctypes 不会因为多传了一个关键字就报错，
它只是把这个值挂在 Python 对象上，**结构体里的 uID 始终是 0**。Windows 收到 uID=0
的 NIM_ADD 时会建一个空槽：托盘里占着位置、没有图标、右键点不出菜单（因为压根没有
可以点的图标）。本机实测：把 uID 补成非零后，Shell 立刻存下了 hIcon 并正常绘制。

补丁做两件事：

1. 调用 ``Shell_NotifyIcon`` 之前，把 uID 为 0 的报文补上 ``DEFAULT_UID``；
   将来 pystray 自己修好了（uID 非零），这一步自动什么都不做。
2. ``NIM_ADD`` 失败时（槽被气泡通知抢占了，或者外壳还在收拾上一个进程的残影），
   先 ``NIM_DELETE`` 再重加；仍失败就交给后台线程隔几秒补几次。否则留下的会是
   一个没有图标、也点不动的空槽。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

# "LD"：同一个进程里每个图标有自己的 hWnd，uID 只要非零就不会撞车。
DEFAULT_UID = 0x4C44

# Shell_NotifyIcon 的报文号，写在这里省得依赖 pystray 的常量名。
_NIM_ADD = 0
_NIM_MODIFY = 1
_NIM_DELETE = 2

# NIF_MESSAGE | NIF_ICON | NIF_TIP
_FULL_FLAGS = 0x00000001 | 0x00000002 | 0x00000004


def _entry_of(data: Any) -> Any:
    """``Shell_NotifyIcon`` 可能收到结构体本身，也可能收到它的指针。"""
    return getattr(data, "contents", data)


def _debug_path() -> Path:
    override = os.environ.get("LANDOC_TRAY_DEBUG_LOG")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "LanDocShare" / "logs" / "tray.log"


def _debug(message: str) -> None:  # pragma: no cover - 只在排障时写盘
    try:
        path = _debug_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except Exception:
        pass


def _resend_icon(original: Any, entry: Any, data: Any) -> Any:
    """把已经存在、但缺图标的记录补全（NIM_MODIFY 带上消息/图标/提示）。"""
    try:
        entry.uFlags = _FULL_FLAGS
    except Exception:  # pragma: no cover
        return 0
    return original(_NIM_MODIFY, data)


def _retry_later(original: Any, data: Any, debug: bool, *,
                 attempts: int = 5, interval: float = 1.5) -> None:
    """在后台隔一会儿再试几次 ``NIM_ADD``（调用方已经删过一次）。"""

    def run() -> None:  # pragma: no cover - 走真实外壳的路径
        for index in range(attempts):
            time.sleep(interval)
            try:
                entry = _entry_of(data)
                original(_NIM_DELETE, data)
                if original(_NIM_ADD, data):
                    if debug:
                        _debug(f"  deferred add ok on attempt {index + 1}")
                    return
                if _resend_icon(original, entry, data):
                    if debug:
                        _debug(f"  deferred modify ok on attempt {index + 1}")
                    return
                if debug:
                    _debug(f"  deferred add attempt {index + 1} failed")
            except Exception:
                return
        if debug:
            _debug("  deferred add gave up")

    threading.Thread(target=run, name="landoc-tray-retry", daemon=True).start()


def patch_pystray_uid(uid: int = DEFAULT_UID) -> bool:
    """给 pystray 的 ``Shell_NotifyIcon`` 包一层，返回是否装上了。

    没装 pystray（比如命令行/无界面环境）时返回 False，不抛异常。
    """
    try:
        from pystray._util import win32
    except Exception:  # pragma: no cover - 没装 pystray 的环境
        return False

    original = win32.Shell_NotifyIcon
    if getattr(original, "_landoc_uid_patch", False):
        return True

    debug = os.environ.get("LANDOC_TRAY_DEBUG", "") not in ("", "0")

    def shell_notify_icon(code: int, data: Any) -> Any:
        try:
            entry = _entry_of(data)
        except Exception:  # pragma: no cover - 报文形状不认识就原样放行
            return original(code, data)
        try:
            if not entry.uID:
                entry.uID = uid
        except Exception:  # pragma: no cover
            pass
        if debug:
            try:
                _debug(f"code={code} uid={entry.uID} cb={entry.uCallbackMessage} "
                       f"flags={entry.uFlags} hIcon={entry.hIcon} hwnd={entry.hWnd}")
            except Exception:
                pass
        result = original(code, data)
        if code == _NIM_ADD and not result:
            if debug:
                _debug("  add failed -> delete & retry")
            original(_NIM_DELETE, data)
            result = original(code, data)
            if not result:
                # 外壳常常已经把这条记录建出来了（只有窗口和 uID，没有图标），
                # 再补一条带 NIF_ICON 的 NIM_MODIFY 就能把它填满。
                if debug:
                    _debug("  add failed again -> modify with icon")
                result = _resend_icon(original, entry, data)
            if not result:
                _retry_later(original, data, debug)
        if debug:
            _debug(f"  -> result={result}")
        return result

    shell_notify_icon._landoc_uid_patch = True  # type: ignore[attr-defined]
    win32.Shell_NotifyIcon = shell_notify_icon
    return True
