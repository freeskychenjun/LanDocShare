from __future__ import annotations

from pathlib import Path

from landoc.config import Config


def test_config_roundtrip(tmp_path):
    cfg = Config(name="测试库", lib_dir=tmp_path, folders=[tmp_path / "docs"], allow_cidrs=["10.91.0.0/16"])
    cfg.snippet_only_chars = 5000
    cfg.save()
    loaded = Config.load(tmp_path)
    assert loaded.folders == [tmp_path / "docs"]
    assert loaded.name == "测试库"
    assert loaded.allow_cidrs == ["10.91.0.0/16"]
    assert loaded.snippet_only_chars == 5000


def test_config_defaults(tmp_path):
    cfg = Config(name="x", lib_dir=tmp_path)
    assert cfg.port == 8766
    assert cfg.snippet_tokens == 48
    assert cfg.store_content is True
    assert cfg.render_md is True
    assert cfg.snippet_only_chars == 20000  # 召回优先：片段长度默认给足
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

def test_config_file_declares_a_version(tmp_path):
    """3.5: 配置里带语义版本号，将来字段含义变了才有地方挂迁移钩子。"""
    import json

    from landoc.config import CONFIG_VERSION

    cfg = Config(name="x", lib_dir=tmp_path)
    cfg.save()
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["config_version"] == CONFIG_VERSION == 1
    # 这个键不是 dataclass 字段：旧/新版本互相读都不该被它噎住
    assert Config.load(tmp_path).name == "x"


def test_config_ignores_unknown_keys(tmp_path):
    """将来加字段时，老程序读新配置也只按自己认识的字段加载，不炸。"""
    import json

    payload = {"name": "库", "lib_dir": str(tmp_path), "folders": [], "config_version": 9, "future_key": 1}
    (tmp_path / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert Config.load(tmp_path).name == "库"
