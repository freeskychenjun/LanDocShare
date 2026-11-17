from __future__ import annotations

import socket
from pathlib import Path

import docx
import pytest

from landoc import cli
from landoc.config import Config
from landoc.index import Index


def test_init_creates_config_and_db(tmp_path: Path):
    lib = tmp_path / "lib"
    docs = tmp_path / "成果"
    docs.mkdir()
    assert cli.main(["init", "--lib-dir", str(lib), "--name", "测试库", "--folder", str(docs)]) == 0
    assert (lib / "config.json").exists()
    assert (lib / "library.db").exists()
    cfg = Config.load(lib)
    assert cfg.name == "测试库"
    assert cfg.folders == [docs]


def test_init_accepts_multiple_folders(tmp_path: Path):
    lib = tmp_path / "lib"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--folder", str(a), "--folder", str(b)])
    assert len(Config.load(lib).folders) == 2


def test_status_prints_counts(tmp_path: Path, capsys):
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库"])
    assert cli.main(["status", "--lib-dir", str(lib)]) == 0
    out = capsys.readouterr().out
    assert "文档数：0" in out
    assert "上次扫描：（从未）" in out


def test_convert_runs_pipeline_on_plain_docx(tmp_path: Path, capsys):
    lib = tmp_path / "lib"
    docs = tmp_path / "成果"
    docs.mkdir()
    d = docx.Document()
    d.add_heading("1  工程概况", level=1)
    d.add_paragraph("乐平水利枢纽工程概况。")
    d.save(docs / "a.docx")
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--folder", str(docs)])
    assert cli.main(["convert", "--lib-dir", str(lib)]) == 0
    assert "新转换 1 篇" in capsys.readouterr().out
    assert Index(Config.load(lib).db_path).stats()["total"] == 1


def test_probe_prints_report(tmp_path: Path, capsys):
    docs = tmp_path / "成果"
    docs.mkdir()
    (docs / "a.docx").write_bytes(b"PK\x03\x04" + b"x" * 32)
    (docs / "b.docx").write_bytes(bytes.fromhex("17da5fa01633cd9a") + b"x" * 32)
    assert cli.main(["probe", "--folder", str(docs), "--n", "10"]) == 0
    out = capsys.readouterr().out
    assert '"sampled": 2' in out
    assert '"encrypted": 1' in out


def test_pick_port_skips_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("0.0.0.0", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        assert cli.pick_port(taken) == taken + 1


def test_pick_port_skips_registry_port(monkeypatch):
    """8767 留给二期 registry：库端顺延时不能把它抢走（本机 8766 就被旧服务占着）。"""
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)
    assert cli.RESERVED_PORTS == {8767}
    assert cli.pick_port(8766) == 8766
    assert cli.pick_port(8767) == 8768


def test_parser_rejects_unknown_command():
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])


def test_init_accepts_allow_cidrs(tmp_path: Path):
    lib = tmp_path / "lib"
    assert cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--allow-cidrs", "10.91.1.0/24"]) == 0
    assert Config.load(lib).allow_cidrs == ["10.91.1.0/24"]


def test_init_rejects_bad_allow_cidrs(tmp_path: Path, capsys):
    lib = tmp_path / "lib"
    assert cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--allow-cidrs", "10.91.1.0/99"]) == 2
    assert not (lib / "config.json").exists()


def test_init_accepts_acrobat_tool_dir(tmp_path: Path):
    """I8: 兜底工具目录必须有入口，否则 README 承诺的能力配不出来。"""
    lib = tmp_path / "lib"
    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "acrobat.exe").write_bytes(b"MZ")
    cli.main(
        ["init", "--lib-dir", str(lib), "--name", "库", "--acrobat-tool-dir", str(tool)]
    )
    assert Config.load(lib).acrobat_tool_dir == tool


def test_init_preserves_existing_settings(tmp_path: Path):
    """M7: 重跑 init 追加目录，不能把已有的共享范围/工具目录打回默认。"""
    lib = tmp_path / "lib"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--folder", str(a)])
    cfg = Config.load(lib)
    cfg.allow_cidrs = ["127.0.0.1"]
    cfg.acrobat_tool_dir = tmp_path / "tool"
    cfg.save()
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--folder", str(b)])
    reloaded = Config.load(lib)
    assert reloaded.allow_cidrs == ["127.0.0.1"]
    assert reloaded.acrobat_tool_dir == tmp_path / "tool"
    assert set(reloaded.folders) == {a, b}


def test_init_tightens_privacy_switches(tmp_path: Path):
    """设计 §6.6 的收紧开关：init 能设，也能翻回去，且重跑不带开关时不打回默认。"""
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--no-store-content", "--no-render-md"])
    cfg = Config.load(lib)
    assert cfg.store_content is False and cfg.render_md is False
    cli.main(["init", "--lib-dir", str(lib), "--name", "库"])
    assert Config.load(lib).store_content is False
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--store-content", "--render-md"])
    cfg = Config.load(lib)
    assert cfg.store_content is True and cfg.render_md is True


def test_status_reports_storage_policy(tmp_path: Path, capsys):
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--no-store-content"])
    cli.main(["status", "--lib-dir", str(lib)])
    out = capsys.readouterr().out
    assert "存储策略：只存前 20000 字 / 落 md 副本" in out


def test_init_sets_and_keeps_snippet_only_chars(tmp_path: Path, capsys):
    """片段长度可调（召回优先的旋钮）；重跑不带参数时不能被打回默认。"""
    lib = tmp_path / "lib"
    cli.main(
        ["init", "--lib-dir", str(lib), "--name", "库", "--no-store-content", "--snippet-only-chars", "8000"]
    )
    assert Config.load(lib).snippet_only_chars == 8000
    cli.main(["init", "--lib-dir", str(lib), "--name", "库"])
    assert Config.load(lib).snippet_only_chars == 8000
    cli.main(["status", "--lib-dir", str(lib)])
    assert "存储策略：只存前 8000 字 / 落 md 副本" in capsys.readouterr().out


def test_init_rejects_uselessly_small_snippet_chars(tmp_path: Path):
    import pytest

    with pytest.raises(SystemExit) as exc:
        cli.main(["init", "--lib-dir", str(tmp_path / "lib"), "--name", "库", "--snippet-only-chars", "10"])
    assert exc.value.code == 2


def test_people_imports_csv_and_exports_back(tmp_path: Path, capsys):
    """按 IP 授权的主路径：CSV 灌名单 → 导出可再分发。"""
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库"])
    csv_path = tmp_path / "名单.csv"
    csv_path.write_text("姓名,IP\n陈鋆,10.91.1.197\n杨欢,10.91.5.238\n", encoding="utf-8")

    assert cli.main(["people", "--lib-dir", str(lib), "--csv", str(csv_path)]) == 0
    assert Config.load(lib).allow_people == {"陈鋆": "10.91.1.197", "杨欢": "10.91.5.238"}

    export_path = tmp_path / "导出.csv"
    assert cli.main(["people", "--lib-dir", str(lib), "--export", str(export_path)]) == 0
    assert "陈鋆" in export_path.read_text(encoding="utf-8-sig")
    out = capsys.readouterr().out
    assert "共 2 人" in out


def test_people_merge_keeps_existing_and_can_remove(tmp_path: Path):
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库"])
    csv_path = tmp_path / "名单.csv"
    csv_path.write_text("姓名,IP\n陈鋆,10.91.1.197\n", encoding="utf-8")
    cli.main(["people", "--lib-dir", str(lib), "--csv", str(csv_path)])
    cli.main(["people", "--lib-dir", str(lib), "--set", "张三="])
    assert Config.load(lib).allow_people == {"陈鋆": "10.91.1.197", "张三": ""}
    cli.main(["people", "--lib-dir", str(lib), "--remove", "陈鋆"])
    assert Config.load(lib).allow_people == {"张三": ""}


def test_people_rejects_bad_csv(tmp_path: Path, capsys):
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库"])
    csv_path = tmp_path / "坏.csv"
    csv_path.write_text("姓名,IP\n陈鋆,10.91.1.999\n", encoding="utf-8")
    assert cli.main(["people", "--lib-dir", str(lib), "--csv", str(csv_path)]) == 2
    assert Config.load(lib).allow_people == {}


def test_status_reports_people_summary(tmp_path: Path, capsys):
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库"])
    cli.main(["status", "--lib-dir", str(lib)])
    assert "允许名单：（未启用：允许网段内谁都能查）" in capsys.readouterr().out
    cli.main(["people", "--lib-dir", str(lib), "--set", "陈鋆=10.91.1.197"])
    cli.main(["status", "--lib-dir", str(lib)])
    assert "允许名单：1 人，按 IP 放行（姓名仅备注）" in capsys.readouterr().out


def test_registry_command_forwards_flags(monkeypatch):
    from landoc import registry

    seen: dict = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(registry, "main", fake_main)
    assert cli.main(["registry", "--port", "8790", "--install-autostart"]) == 0
    assert seen["argv"] == ["--port", "8790", "--install-autostart"]


def test_init_records_registry_url(tmp_path: Path):
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--registry-url", "http://10.91.1.197:8767"])
    assert Config.load(lib).registry_url == "http://10.91.1.197:8767"
