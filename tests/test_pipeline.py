from __future__ import annotations

import time
from pathlib import Path

import docx
import pytest

from landoc import office_bridge, pipeline
from landoc.config import Config

ENCRYPTED_HEADER = bytes.fromhex("17da5fa01633cd9a")


class StubIndex:
    """与 Task 7 的真 `Index` 同签名的最小替身（见 ledger 的 pre-flight ruling）。"""

    def __init__(self):
        self.rows: list[dict] = []

    def upsert(self, rel_path, title, ext, source_path, src_size, src_mtime, content):
        self.rows.append(
            dict(
                rel_path=rel_path,
                title=title,
                ext=ext,
                source_path=source_path,
                src_size=src_size,
                src_mtime=src_mtime,
                content=content,
            )
        )

    def commit(self):
        pass

    def stats(self):
        return {"total": len(self.rows)}


@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "成果"
    d.mkdir()
    # 明文 docx
    doc = docx.Document()
    doc.add_heading("1  工程概况", level=1)
    doc.add_paragraph("乐平水利枢纽工程概况。")
    doc.save(d / "plain.docx")
    # 假密文 docx（头对就行，转换走 monkeypatch 的 COM）
    (d / "secret.docx").write_bytes(ENCRYPTED_HEADER + b"cipher" * 32)
    # 应被跳过的表格文件
    (d / "stats.xlsx").write_bytes(b"PK\x03\x04" + b"x" * 32)
    # 临时锁文件
    (d / "~$lock.docx").write_bytes(b"PK\x03\x04")
    return d


@pytest.fixture
def cfg(docs_dir: Path, tmp_path: Path) -> Config:
    lib = tmp_path / "lib"
    lib.mkdir()
    return Config(name="测试库", lib_dir=lib, folders=[docs_dir])


def test_mixed_batch_routes_by_classification(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0: flat_opc
    )
    report = pipeline.run(cfg, index=StubIndex())
    assert report.converted == 2
    assert report.failed == []


def test_xlsx_is_skipped_with_reason(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0: flat_opc
    )
    report = pipeline.run(cfg, index=StubIndex())
    assert report.skipped["xls"] == 1


def test_temp_lock_file_is_not_counted(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0: flat_opc
    )
    report = pipeline.run(cfg, index=StubIndex())
    rels = [r for r in report.md_paths]
    assert all("~$" not in r for r in rels)


def test_md_written_with_source_path_frontmatter(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0: flat_opc
    )
    index = StubIndex()
    pipeline.run(cfg, index=index)
    md_files = sorted(Path(cfg.md_dir).rglob("*.md"))
    assert md_files
    text = md_files[0].read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "source_path:" in text
    assert index.rows[0]["content"]


def test_no_office_records_reason_and_writes_no_empty_md(cfg, monkeypatch):
    def boom(path, timeout=120.0):
        raise office_bridge.OfficeUnavailable("no office")

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", boom)
    report = pipeline.run(cfg, index=StubIndex())
    reasons = {reason for _, reason in report.failed}
    assert "no_office" in reasons
    secret_md = Path(cfg.md_dir) / "成果" / "secret.md"
    assert not secret_md.exists()


def test_incremental_skips_unchanged(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0: flat_opc
    )
    pipeline.run(cfg, index=StubIndex())
    second = pipeline.run(cfg, index=StubIndex())
    assert second.converted == 0
    assert second.skipped["unchanged"] == 2


def test_touched_file_is_reconverted(cfg, flat_opc, monkeypatch, docs_dir):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0: flat_opc
    )
    pipeline.run(cfg, index=StubIndex())
    target = docs_dir / "plain.docx"
    target.write_bytes(target.read_bytes() + b"x")  # 变大 → 视为变化
    second = pipeline.run(cfg, index=StubIndex())
    assert second.converted == 1


def test_checkpoint_written_even_on_interrupt(cfg, flat_opc, monkeypatch, docs_dir):
    calls = {"n": 0}
    real = pipeline.convert_one

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "convert_one", flaky)
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0: flat_opc
    )
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(cfg, index=StubIndex())
    state = pipeline._read_state(cfg)
    assert state.get("last_scan_ts")


def test_limit_stops_after_n_docs(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0: flat_opc
    )
    report = pipeline.run(cfg, index=StubIndex(), limit=1)
    assert report.converted == 1
