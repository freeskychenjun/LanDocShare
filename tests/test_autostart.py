"""开机自启：写/删 HKCU Run，值就是启动命令。"""

from __future__ import annotations

import sys
import uuid

import pytest

from landoc import autostart


@pytest.fixture()
def run_key(monkeypatch):
    import winreg

    key = rf"Software\LanDocShareTest\{uuid.uuid4().hex}\Run"
    monkeypatch.setattr(autostart, "RUN_KEY", key)
    yield key
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    except OSError:
        pass


def test_enable_disable_roundtrip(run_key):
    assert autostart.is_enabled() is False
    autostart.enable('"C:\\Apps\\LanDocShare\\LanDocShare.exe"')
    assert autostart.is_enabled() is True
    autostart.disable()
    assert autostart.is_enabled() is False
    autostart.disable()  # 再删一次不该炸


def test_set_enabled_uses_flag(run_key):
    autostart.set_enabled(True)
    assert autostart.is_enabled() is True
    autostart.set_enabled(False)
    assert autostart.is_enabled() is False


def test_command_line_for_frozen_exe(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\LanDocShare\LanDocShare.exe")
    assert autostart.command_line() == r'"C:\Apps\LanDocShare\LanDocShare.exe" --autostart'


def test_command_line_for_source_run(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Python314\python.exe")
    assert autostart.command_line() == r'"C:\Python314\python.exe" -m landoc.tray --autostart'


def test_registry_command_line_for_frozen_exe(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\LanDocShare\LanDocShare.exe")
    assert autostart.registry_command_line(8767) == r'"C:\Apps\LanDocShare\LanDocShare.exe" registry --port 8767'


def test_registry_command_line_appends_runtime_args(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\LanDocShare\LanDocShare.exe")
    line = autostart.registry_command_line(
        8767, ["--watchdog", "--package-zip", r"F:\发布 包\LanDocShare.zip"]
    )
    assert line.startswith(r'"C:\Apps\LanDocShare\LanDocShare.exe" registry --port 8767 ')
    assert "--watchdog --package-zip" in line
    assert r'"F:\发布 包\LanDocShare.zip"' in line  # 空格路径要带引号，否则重启后被拆成两段


def test_enable_registry_puts_watchdog_first(monkeypatch):
    seen = {}

    def fake_enable(command, name=None):
        seen["command"] = command
        seen["name"] = name

    monkeypatch.setattr(autostart, "enable", fake_enable)
    command = autostart.enable_registry(8790, extra=["--package-zip", "a.zip"])
    assert "--port 8790" in command
    assert command.index("--watchdog") < command.index("--package-zip")
    assert seen["name"] == autostart.REGISTRY_VALUE_NAME


def test_enable_registry_can_skip_watchdog(monkeypatch):
    monkeypatch.setattr(autostart, "enable", lambda command, name=None: None)
    assert "--watchdog" not in autostart.enable_registry(8767, watchdog=False)


def test_registry_entry_is_separate_from_library_entry(run_key):
    autostart.enable('"C:\\Apps\\LanDocShare\\LanDocShare.exe" --autostart', name=autostart.VALUE_NAME)
    autostart.enable('"C:\\Apps\\LanDocShare\\LanDocShare.exe" registry --port 8767', name=autostart.REGISTRY_VALUE_NAME)
    assert autostart.is_enabled() is True
    assert autostart.registry_enabled() is True
    autostart.disable(name=autostart.REGISTRY_VALUE_NAME)
    assert autostart.registry_enabled() is False
    assert autostart.is_enabled() is True  # 删中心那条不影响库自己那条
