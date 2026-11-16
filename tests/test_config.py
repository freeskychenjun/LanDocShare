from __future__ import annotations

from pathlib import Path

from landoc.config import Config


def test_config_roundtrip(tmp_path):
    cfg = Config(name="测试库", lib_dir=tmp_path, folders=[tmp_path / "docs"], allow_cidrs=["10.91.0.0/16"])
    cfg.save()
    loaded = Config.load(tmp_path)
    assert loaded.folders == [tmp_path / "docs"]
    assert loaded.name == "测试库"
    assert loaded.allow_cidrs == ["10.91.0.0/16"]


def test_config_defaults(tmp_path):
    cfg = Config(name="x", lib_dir=tmp_path)
    assert cfg.port == 8766
    assert cfg.snippet_tokens == 48
    assert cfg.allow_cidrs == ["10.91.0.0/16"]
    assert cfg.acrobat_tool_dir is None
    assert cfg.md_dir == tmp_path / "md"
    assert cfg.db_path == tmp_path / "library.db"


def test_config_save_is_atomic_no_tmp_left(tmp_path):
    cfg = Config(name="x", lib_dir=tmp_path)
    cfg.save()
    assert (Path(tmp_path) / "config.json").exists()
    assert [p.name for p in Path(tmp_path).iterdir() if p.name.startswith(".config")] == []


def test_config_load_missing_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope")
