"""托盘程序的纯逻辑部分（不弹图标、不碰真注册表）。"""

from __future__ import annotations

import uuid
from pathlib import Path

from landoc import tray


def test_remember_and_reuse_last_library(tmp_path, monkeypatch):
    marker = tmp_path / "last_lib.txt"
    monkeypatch.setattr(tray, "LAST_LIB_MARKER", marker)
    lib_dir = tmp_path / "MyLib"
    lib_dir.mkdir()
    (lib_dir / "config.json").write_text("{}", encoding="utf-8")

    tray._remember_lib(lib_dir)

    assert marker.read_text(encoding="utf-8") == str(lib_dir)
    assert tray._default_lib_dir() == lib_dir


def test_default_library_falls_back_when_marker_points_nowhere(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "LAST_LIB_MARKER", tmp_path / "missing.txt")

    assert tray._default_lib_dir() == Path("D:/MyLib")


def test_second_instance_loses_the_mutex():
    name = f"LanDocShare-test-{uuid.uuid4()}"
    assert tray.acquire_single_instance(name) is True
    # 同一个进程再申请一次也要拒绝，否则"双击第二次"会起第二个服务
    assert tray.acquire_single_instance(name) is False
