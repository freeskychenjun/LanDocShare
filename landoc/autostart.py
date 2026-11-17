"""开机自启：写 `HKCU\\...\\CurrentVersion\\Run`（不需要管理员，设计 §6.1）。"""

from __future__ import annotations

import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "LanDocShare"


def command_line() -> str:
    """打包后就是 exe 自己；源码运行时用 `python -m landoc.tray`。

    带 `--autostart`：开机自启时不自动弹浏览器，只在托盘待着（否则每次开机都抢一个标签页）。
    """
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return f'"{exe}" --autostart'
    return f'"{exe}" -m landoc.tray --autostart'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return bool(str(value).strip())


def enable(command: str | None = None) -> None:
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command or command_line())


def disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return
    except OSError:
        return


def set_enabled(flag: bool) -> None:
    enable() if flag else disable()
