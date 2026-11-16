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


def test_parser_rejects_unknown_command():
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])
