"""开机自启：写 `HKCU\\...\\CurrentVersion\\Run`（不需要管理员，设计 §6.1）。

库软件与登记中心各用一个值名：`LanDocShare` / `LanDocShare-registry`，
勾掉其中一个不会影响另一个。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence

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


def registry_command_line(port: int = 8767, extra: Sequence[str] = ()) -> str:
    """登记中心的自启命令：打包后 `LanDocShare.exe registry --port 8767`。

    `extra` 是这次启动实际用到的运行参数（--package-zip/--manifest/--state-dir
    等）：自启时如果不带上，重启后分发配置就丢了，同事的下载入口会 404。
    """
    exe = _python()
    if getattr(sys, "frozen", False):
        parts = [f'"{exe}"', "registry", "--port", str(port)]
    else:
        parts = [f'"{exe}"', "-m", "landoc.registry", "--port", str(port)]
    if extra:
        parts.append(subprocess.list2cmdline([str(item) for item in extra]))
    return " ".join(parts)


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


def enable_registry(
    port: int = 8767, *, extra: Sequence[str] = (), watchdog: bool = True
) -> str:
    """写中心的自启项；默认带 `--watchdog`，开机后中心挂了会被自动拉起。"""
    args = [str(item) for item in extra]
    if watchdog and "--watchdog" not in args:
        args.insert(0, "--watchdog")
    command = registry_command_line(port, args)
    enable(command, name=REGISTRY_VALUE_NAME)
    return command


def disable_registry() -> None:
    disable(name=REGISTRY_VALUE_NAME)


def registry_enabled() -> bool:
    return is_enabled(name=REGISTRY_VALUE_NAME)
