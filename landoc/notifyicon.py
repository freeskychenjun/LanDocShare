"""修正 pystray 托盘图标漏填的 ``uID``。

pystray 0.19.5 的 ``Icon._message`` 是这么发的::

    win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(..., hID=id(self), ...))

而 ``NOTIFYICONDATAW`` 的字段名是 ``uID``。ctypes 不会因为多传了一个关键字就报错，
它只是把这个值挂在 Python 对象上，**结构体里的 uID 始终是 0**。Windows 收到 uID=0
的 NIM_ADD 时会建一个空槽：托盘里占着位置、没有图标、右键点不出菜单（因为压根没有
可以点的图标）。本机实测：把 uID 补成非零后，Shell 立刻存下了 hIcon 并正常绘制。

补丁只做一件事：调用 ``Shell_NotifyIcon`` 之前，把 uID 为 0 的报文补上 ``DEFAULT_UID``。
将来 pystray 自己修好了（uID 非零），这里自动什么都不做。
"""

from __future__ import annotations

from typing import Any

# "LD"：同一个进程里每个图标有自己的 hWnd，uID 只要非零就不会撞车。
DEFAULT_UID = 0x4C44


def _entry_of(data: Any) -> Any:
    """``Shell_NotifyIcon`` 可能收到结构体本身，也可能收到它的指针。"""
    return getattr(data, "contents", data)


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

    def shell_notify_icon(code: int, data: Any) -> Any:
        try:
            entry = _entry_of(data)
            if not entry.uID:
                entry.uID = uid
        except Exception:  # pragma: no cover - 报文形状不认识就原样放行
            pass
        return original(code, data)

    shell_notify_icon._landoc_uid_patch = True  # type: ignore[attr-defined]
    win32.Shell_NotifyIcon = shell_notify_icon
    return True
