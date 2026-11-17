"""向导的纯逻辑：拼配置、拦掉不该共享的盘（设计 D8）。"""

from __future__ import annotations

from pathlib import Path

from landoc import wizard


def test_build_config_falls_back_to_default_name(tmp_path):
    cfg = wizard.build_config("   ", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    assert cfg.name == "我的资料库"
    assert cfg.folders == [tmp_path]
    assert cfg.lib_dir == tmp_path / "lib"
    assert cfg.allow_cidrs == ["10.91.0.0/16"]


def test_build_config_local_scope_allows_only_loopback(tmp_path):
    cfg = wizard.build_config("小库", [tmp_path], wizard.SCOPE_LOCAL, tmp_path / "lib")
    assert cfg.allow_cidrs == ["127.0.0.1"]


def test_validate_folder_rejects_unc():
    assert "UNC" in wizard.validate_folder(Path(r"\\server\share\docs"))


def test_validate_folder_rejects_missing(tmp_path):
    assert wizard.validate_folder(tmp_path / "nope") == "目录不存在"


def test_validate_folder_rejects_network_drive(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "_drive_type", lambda path: wizard.DRIVE_REMOTE)
    assert "网络盘" in wizard.validate_folder(tmp_path)


def test_validate_folder_rejects_removable(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "_drive_type", lambda path: wizard.DRIVE_REMOVABLE)
    assert "可移动盘" in wizard.validate_folder(tmp_path)


def test_validate_folder_accepts_local_fixed_drive(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "_drive_type", lambda path: wizard.DRIVE_FIXED)
    assert wizard.validate_folder(tmp_path) is None
