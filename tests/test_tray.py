"""托盘程序的纯逻辑部分（不弹图标、不碰真注册表）。"""

from __future__ import annotations

import types
import uuid
from pathlib import Path

from landoc import tray
from landoc.config import Config


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


def _isolate_main(monkeypatch, tmp_path):
    """让 main() 走"没有可用配置"的分支，但不真起服务/托盘。"""
    started = {}
    monkeypatch.setattr(tray, "_remember_lib", lambda lib_dir: None)
    monkeypatch.setattr(tray, "acquire_single_instance", lambda *a, **k: True)
    monkeypatch.setattr(
        tray,
        "TrayApp",
        lambda cfg, **kw: types.SimpleNamespace(run=lambda: started.setdefault("cfg", cfg)),
    )
    return started


def test_first_run_opens_wizard_and_starts_with_its_config(tmp_path, monkeypatch):
    from landoc import wizard

    lib_dir = tmp_path / "lib"
    made = Config(name="新库", lib_dir=lib_dir, folders=[tmp_path])
    calls = []

    def fake_wizard(path, parent=None, **kwargs):
        calls.append((path, kwargs))
        return made

    monkeypatch.setattr(wizard, "run_wizard", fake_wizard)
    started = _isolate_main(monkeypatch, tmp_path)

    assert tray.main(["--lib-dir", str(lib_dir)]) == 0
    assert calls, "没有配置时应该弹向导"
    assert started["cfg"] is made


def test_wizard_cancel_exits_nonzero(tmp_path, monkeypatch):
    from landoc import wizard

    monkeypatch.setattr(wizard, "run_wizard", lambda *a, **k: None)
    _isolate_main(monkeypatch, tmp_path)

    assert tray.main(["--lib-dir", str(tmp_path / "lib")]) == 1


def test_wizard_flag_prefills_existing_config(tmp_path, monkeypatch):
    from landoc import wizard

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    Config(name="老库", lib_dir=lib_dir, folders=[tmp_path]).save()
    seen = {}

    def fake_wizard(path, parent=None, **kwargs):
        seen.update(kwargs)
        return Config(name="改过", lib_dir=lib_dir, allow_cidrs=["127.0.0.1"])

    monkeypatch.setattr(wizard, "run_wizard", fake_wizard)
    started = _isolate_main(monkeypatch, tmp_path)

    assert tray.main(["--lib-dir", str(lib_dir), "--wizard"]) == 0
    assert seen["current"] is not None and seen["current"].name == "老库"
    assert started["cfg"].name == "改过"


def test_existing_config_skips_wizard(tmp_path, monkeypatch):
    from landoc import wizard

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    cfg = Config(name="老库", lib_dir=lib_dir, folders=[tmp_path])
    cfg.save()
    monkeypatch.setattr(
        wizard, "run_wizard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该弹向导"))
    )
    started = _isolate_main(monkeypatch, tmp_path)

    assert tray.main(["--lib-dir", str(lib_dir)]) == 0
    assert started["cfg"].name == "老库"
