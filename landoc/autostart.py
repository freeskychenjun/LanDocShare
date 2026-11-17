"""开机自启：写 `HKCU\\...\\CurrentVersion\\Run`（不需要管理员，设计 §6.1）。

库软件与登记中心各用一个值名：`LanDocShare` / `LanDocShare-registry`，
勾掉其中一个不会影响另一个。
"""

from __future__ import annotations

import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "LanDocShare"
REGISTRY_VALUE_NAME = "LanDocShare-registry"


def _python() -> str:
    return sys.executable


def command_line(extra: str = "--autostart") -> str:
    """打包后就是 exe 自己；源码运行时用 `python -m landoc.tray`。

    带 `--autostart`：开机自启时不自动弹窗口，只在托盘待着（否则每次开机都抢一个标签页）。
    """
    exe = _python()
    if getattr(sys, "frozen", False):
        return f'"{exe}" {extra}'.strip()
    return f'"{exe}" -m landoc.tray {extra}'.strip()


def registry_command_line(port: int = 8767) -> str:
    """登记中心的自启命令：打包后 `LanDocShare.exe registry --port 8767`。"""
    exe = _python()
    if getattr(sys, "frozen", False):
        return f'"{exe}" registry --port {port}'
    return f'"{exe}" -m landoc.registry --port {port}'


def is_enabled(name: str = VALUE_NAME) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return False
    return bool(str(value).strip())


def enable(command: str | None = None, name: str = VALUE_NAME) -> None:
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command or command_line())


def disable(name: str = VALUE_NAME) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        return
    except OSError:
        return


def set_enabled(flag: bool, name: str = VALUE_NAME) -> None:
    enable(name=name) if flag else disable(name=name)


def enable_registry(port: int = 8767) -> str:
    command = registry_command_line(port)
    enable(command, name=REGISTRY_VALUE_NAME)
    return command


def disable_registry() -> None:
    disable(name=REGISTRY_VALUE_NAME)


def registry_enabled() -> bool:
    return is_enabled(name=REGISTRY_VALUE_NAME)
