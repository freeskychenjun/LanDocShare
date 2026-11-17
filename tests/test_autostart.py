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
