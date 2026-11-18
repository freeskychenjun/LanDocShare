from __future__ import annotations

import json
import time
from pathlib import Path

import docx
import pytest

from landoc import office_bridge, pipeline
from landoc.config import Config

ENCRYPTED_HEADER = bytes.fromhex("17da5fa01633cd9a")

LONG_MD = "乐平水利枢纽工程概况。" * 200  # 远超片段上限，用来区分"全文"与"片段"


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

    def delete(self, rel_path):
        self.rows = [r for r in self.rows if r["rel_path"] != rel_path]

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
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
    )
    report = pipeline.run(cfg, index=StubIndex())
    assert report.converted == 2
    assert report.failed == []


def test_xlsx_is_skipped_with_reason(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
    )
    report = pipeline.run(cfg, index=StubIndex())
    assert report.skipped["xls"] == 1


def test_temp_lock_file_is_not_counted(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
    )
    report = pipeline.run(cfg, index=StubIndex())
    rels = [r for r in report.md_paths]
    assert all("~$" not in r for r in rels)


def test_md_written_with_source_path_frontmatter(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
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
    def boom(path, timeout=120.0, **kwargs):
        raise office_bridge.OfficeUnavailable("no office")

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", boom)
    report = pipeline.run(cfg, index=StubIndex())
    reasons = {reason for _, reason in report.failed}
    assert "no_office" in reasons
    secret_md = Path(cfg.md_dir) / "成果" / "secret.docx.md"
    assert not secret_md.exists()


def test_incremental_skips_unchanged(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
    )
    pipeline.run(cfg, index=StubIndex())
    second = pipeline.run(cfg, index=StubIndex())
    assert second.converted == 0
    assert second.skipped["unchanged"] == 2


def test_touched_file_is_reconverted(cfg, flat_opc, monkeypatch, docs_dir):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
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
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
    )
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(cfg, index=StubIndex())
    state = pipeline._read_state(cfg)
    assert state.get("last_scan_ts")


def test_limit_stops_after_n_docs(cfg, flat_opc, monkeypatch):
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
    )
    report = pipeline.run(cfg, index=StubIndex(), limit=1)
    assert report.converted == 1


def test_relative_folder_is_resolved_to_absolute(tmp_path, monkeypatch, docs_dir):
    """相对目录必须解析成绝对路径：COM 不接受相对路径（WPS 报 3010 打开失败）。"""
    monkeypatch.chdir(tmp_path)
    entries = pipeline._iter_docs([Path("成果")])
    assert entries and all(path.is_absolute() for path, _ in entries)


# ============ 终审修复批（Critical C1 + Important I1/I2/I3/I6/I7/I10）============

def test_plain_doc_goes_through_com(cfg, docs_dir, flat_opc, monkeypatch):
    """C1: 明文 .doc 是 OLE 容器，python-docx 解析不了，必须走 COM。"""
    (docs_dir / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"x" * 64)
    (docs_dir / "secret.docx").unlink()
    (docs_dir / "stats.xlsx").unlink()
    (docs_dir / "plain.docx").unlink()
    calls = {"com": 0}

    def fake_extract(path, *args, **kwargs):
        calls["com"] += 1
        return flat_opc

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", fake_extract)
    report = pipeline.run(cfg, index=StubIndex())
    assert calls["com"] == 1
    assert report.converted == 1 and report.failed == []


def test_plain_ppt_goes_through_com(cfg, docs_dir, monkeypatch):
    (docs_dir / "old.ppt").write_bytes(b"\xd0\xcf\x11\xe0" + b"x" * 64)
    for name in ("plain.docx", "secret.docx", "stats.xlsx"):
        (docs_dir / name).unlink()
    seen = {}

    def fake_ppt_to_md(path, encrypted=False, **kwargs):
        seen["encrypted"] = encrypted
        return "## 第 1 页\n\n汇报人：王昱东"

    monkeypatch.setattr(pipeline.pptx_conv, "pptx_to_md", fake_ppt_to_md)
    report = pipeline.run(cfg, index=StubIndex())
    assert seen["encrypted"] is True  # OLE 容器 → 走 COM
    assert report.converted == 1


def test_write_failure_does_not_abort_batch(cfg, flat_opc, monkeypatch):
    """I1: 单篇落盘失败只记账，不能中断整批。"""
    monkeypatch.setattr(office_bridge, "extract_wordopenxml", lambda *a, **k: flat_opc)
    real_write = pipeline._write_md

    def flaky_write(cfg_, rel, md, source_path):
        if rel.endswith("plain.docx"):
            raise OSError("文件名或扩展名太长")
        return real_write(cfg_, rel, md, source_path)

    monkeypatch.setattr(pipeline, "_write_md", flaky_write)
    report = pipeline.run(cfg, index=StubIndex())
    assert report.converted == 1
    assert any(reason == "write_failed" for _, reason in report.failed)


def test_scanned_pdf_is_recorded_as_no_text_layer(cfg, docs_dir, flat_opc, monkeypatch):
    """扫描件没有文本层：失败原因要说清楚，别糊成 empty_text。"""
    # 同目录里的假密文 docx 会走真 COM，这里给个替身，测试只关心那条 PDF
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda path, timeout=120.0, **kw: flat_opc
    )
    (docs_dir / "扫描件.pdf").write_bytes(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n")
    monkeypatch.setattr(
        pipeline.pdf_conv,
        "pdf_to_text",
        lambda path, cfg=None, **k: pipeline.pdf_conv.PDFResult(
            "\n\n\n", "pymupdf", "no_text_layer"
        ),
    )
    report = pipeline.run(cfg, index=StubIndex())
    assert ("成果/扫描件.pdf", "no_text_layer") in report.failed
    assert all(reason != "empty_text" for _, reason in report.failed)


def test_same_stem_different_ext_kept_apart(cfg, docs_dir, flat_opc, monkeypatch):
    """I2: 同时存在 报告.docx 与 报告.pdf 时，两篇都要留。"""
    monkeypatch.setattr(office_bridge, "extract_wordopenxml", lambda *a, **k: flat_opc)
    real = docx.Document()
    real.add_paragraph("报告正文")
    real.save(docs_dir / "报告.docx")
    (docs_dir / "报告.pdf").write_bytes(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n")
    monkeypatch.setattr(
        pipeline.pdf_conv,
        "pdf_to_text",
        lambda path, cfg=None, **k: pipeline.pdf_conv.PDFResult("报告正文 内容", "pymupdf"),
    )
    index = StubIndex()
    pipeline.run(cfg, index=index)
    paths = [row["rel_path"] for row in index.rows]
    assert "成果/报告.docx.md" in paths
    assert "成果/报告.pdf.md" in paths


def test_known_failure_is_not_retried_every_run(cfg, docs_dir, monkeypatch):
    """I3: 转换失败也要记账，未变化的文件下一轮不再重付超时。"""

    def always_timeout(path, *args, **kwargs):
        raise office_bridge.OfficeTimeout("boom")

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", always_timeout)
    first = pipeline.run(cfg, index=StubIndex())
    assert any(reason == "timeout" for _, reason in first.failed)
    second = pipeline.run(cfg, index=StubIndex())
    assert second.skipped.get("known_failure") == 1
    assert second.failed == []


def test_failures_file_written_for_owner_review(cfg, docs_dir, monkeypatch):
    def always_timeout(path, *args, **kwargs):
        raise office_bridge.OfficeTimeout("boom")

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", always_timeout)
    pipeline.run(cfg, index=StubIndex())
    failures = Path(cfg.lib_dir) / "failures.json"
    assert failures.is_file()
    payload = failures.read_text(encoding="utf-8")
    assert "timeout" in payload


def test_removed_source_is_pruned_from_index_and_md(cfg, docs_dir, flat_opc, monkeypatch):
    """I7: 文件被移出共享目录 = 撤回共享，md 与索引都必须清掉。"""
    monkeypatch.setattr(office_bridge, "extract_wordopenxml", lambda *a, **k: flat_opc)
    index = StubIndex()
    pipeline.run(cfg, index=index)
    assert index.rows
    (docs_dir / "plain.docx").unlink()
    pipeline.run(cfg, index=index)
    assert not (Path(cfg.md_dir) / "成果" / "plain.docx.md").exists()
    assert all("plain.docx" not in row["rel_path"] for row in index.rows)


def test_drive_root_folder_keeps_md_inside_lib(cfg):
    """I10: 共享目录是盘符根（folder.name == ''）时，md 不能写到库目录外面。"""
    from pathlib import Path as _P

    assert pipeline._top_name(_P("D:/")) == "D"
    assert pipeline._top_name(_P("D:/成果")) == "成果"
    assert not pipeline._md_rel("成果/a.docx").startswith("/")


def test_office_session_reuses_one_instance(monkeypatch):
    """I6: 一批文档共用一个 Office 实例，不再每篇起停。"""
    dispatches = {"n": 0}

    class FakeDoc:
        class Content:
            WordOpenXML = "<pkg:package/>"

        def Close(self, flag):
            return None

    class FakeApp:
        class Documents:
            @staticmethod
            def Open(path, **kwargs):
                return FakeDoc()

        def Quit(self):
            return None

    def fake_dispatch():
        dispatches["n"] += 1
        return FakeApp()

    monkeypatch.setattr(office_bridge, "_dispatch_office", fake_dispatch)
    monkeypatch.setattr(office_bridge, "_office_pids", lambda: set())
    monkeypatch.setattr(office_bridge, "_kill_office_processes", lambda pids=None: None)
    with office_bridge.OfficeSession() as session:
        for _ in range(3):
            office_bridge.extract_wordopenxml(Path("x.docx"), session=session)
    assert dispatches["n"] == 1


# ============ 收紧共享开关（store_content / render_md，设计 §6.6）============

def test_store_content_off_keeps_only_snippet_in_index(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "convert_one", lambda *a, **k: (LONG_MD, "direct"))
    cfg.store_content = False
    cfg.snippet_only_chars = 1000
    index = StubIndex()
    pipeline.run(cfg, index=index)
    assert index.rows
    assert all(len(row["content"]) == 1000 for row in index.rows)
    assert all(LONG_MD.startswith(row["content"]) for row in index.rows)
    # md 副本仍是全文：那份是留给库主人自己看的，不进索引、不经服务端
    md = Path(cfg.md_dir) / "成果" / "plain.docx.md"
    assert md.read_text(encoding="utf-8").endswith(LONG_MD)


def test_render_md_off_writes_no_copy_but_keeps_index(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "convert_one", lambda *a, **k: (LONG_MD, "direct"))
    cfg.render_md = False
    index = StubIndex()
    pipeline.run(cfg, index=index)
    assert index.rows and index.rows[0]["content"] == LONG_MD
    assert list(Path(cfg.md_dir).rglob("*.md")) == []


def test_turning_render_md_off_clears_old_copies(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "convert_one", lambda *a, **k: (LONG_MD, "direct"))
    pipeline.run(cfg, index=StubIndex())
    assert list(Path(cfg.md_dir).rglob("*.md"))
    cfg.render_md = False
    pipeline.run(cfg, index=StubIndex())
    assert list(Path(cfg.md_dir).rglob("*.md")) == []


def test_turning_render_md_back_on_restores_copies(cfg, monkeypatch):
    """开关翻回来时，未改动的文档也要补回 md，否则"不落 md"会永久留空。"""
    monkeypatch.setattr(pipeline, "convert_one", lambda *a, **k: (LONG_MD, "direct"))
    cfg.render_md = False
    pipeline.run(cfg, index=StubIndex())
    assert list(Path(cfg.md_dir).rglob("*.md")) == []
    cfg.render_md = True
    report = pipeline.run(cfg, index=StubIndex())
    assert report.converted == 2
    assert list(Path(cfg.md_dir).rglob("*.md"))


def test_store_content_off_tightens_existing_rows_on_rescan(cfg, monkeypatch):
    """真 Index：关闭后才重扫，旧的全文字段必须收紧 —— 隐私开关不能只对新文件生效。"""
    from landoc.index import Index

    monkeypatch.setattr(pipeline, "convert_one", lambda *a, **k: (LONG_MD, "direct"))
    cfg.snippet_only_chars = 1000
    index = Index(cfg.db_path, md_root=cfg.md_dir)
    pipeline.run(cfg, index=index)
    assert index.content_length("成果/plain.docx.md") > 1000
    cfg.store_content = False
    report = pipeline.run(cfg, index=index)
    assert report.converted == 2
    assert index.content_length("成果/plain.docx.md") == 1000


def test_snippet_cap_defaults_to_generous_and_clamps_tiny(cfg):
    """召回优先：默认给足 20000 字；配置里塞个没意义的极小值就退回默认。"""
    assert pipeline.DEFAULT_SNIPPET_ONLY_CHARS == 20000
    assert pipeline.snippet_cap(cfg) == 20000
    cfg.snippet_only_chars = 5000
    assert pipeline.snippet_cap(cfg) == 5000
    cfg.snippet_only_chars = 10
    assert pipeline.snippet_cap(cfg) == 20000
    cfg.snippet_only_chars = "不是数字"
    assert pipeline.snippet_cap(cfg) == 20000


def test_prefix_cuts_at_line_boundary(cfg, monkeypatch):
    """截断停在整行末尾，别把表格行切成两半（碎片表格会污染 /api/tables）。"""
    body = "\n".join(f"| 行{i} | 值{i} |" for i in range(80))
    monkeypatch.setattr(pipeline, "convert_one", lambda *a, **k: (body, "direct"))
    cfg.store_content = False
    cfg.snippet_only_chars = 300
    index = StubIndex()
    pipeline.run(cfg, index=index)
    content = index.rows[0]["content"]
    assert 0 < len(content) <= 300
    assert content.endswith("|")
    assert body.startswith(content)


# ============ 失败重试上限 / 环境自愈 ============

@pytest.fixture
def one_bad_doc(tmp_path: Path) -> tuple[Config, Path]:
    """只放一份"每次都会失败"的密文 docx，排除其它文件的干扰。"""
    docs = tmp_path / "成果"
    docs.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    target = docs / "坏文件.docx"
    target.write_bytes(ENCRYPTED_HEADER + b"cipher" * 8)
    return Config(name="测试库", lib_dir=lib, folders=[docs]), target


def test_gives_up_after_max_attempts(one_bad_doc, monkeypatch):
    """1.1: 累计失败到上限就永久放弃 —— 不能每改一次又重付 120 秒超时。

    回归点：旧条件里 `attempts < MAX_ATTEMPTS` 与其它条件与在一起，第 3 次之后
    恒为假，于是每个空闲周期都重试，attempts 无限增长。
    """
    cfg, target = one_bad_doc

    def always_timeout(*args, **kwargs):
        raise office_bridge.OfficeTimeout("boom")

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", always_timeout)
    for attempt in range(pipeline.MAX_ATTEMPTS):
        # 每次换一版内容（大小也不同），模拟"主人又改了一版"
        target.write_bytes(ENCRYPTED_HEADER + bytes([attempt]) * (8 + attempt))
        report = pipeline.run(cfg, index=StubIndex())
        assert [reason for _, reason in report.failed] == ["timeout"]
        assert report.skipped.get("known_failure") is None

    target.write_bytes(ENCRYPTED_HEADER + b"another version" * 4)
    report = pipeline.run(cfg, index=StubIndex())
    assert report.failed == []
    assert report.skipped.get("known_failure") == 1


def test_same_failing_version_is_skipped_before_hitting_the_cap(one_bad_doc, monkeypatch):
    """1.1: 文件没变就别再试（哪怕还没到上限）。"""

    def always_timeout(*args, **kwargs):
        raise office_bridge.OfficeTimeout("boom")

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", always_timeout)
    cfg, _target = one_bad_doc
    pipeline.run(cfg, index=StubIndex())
    report = pipeline.run(cfg, index=StubIndex())
    assert report.skipped.get("known_failure") == 1
    assert report.failed == []


def test_installing_office_clears_no_office_failures(one_bad_doc, monkeypatch, flat_opc):
    """1.2: 同事补装 Office 后，旧的 no_office 失败记录必须作废重试，不能永不自愈。"""
    cfg, _target = one_bad_doc

    def no_office(*args, **kwargs):
        raise office_bridge.OfficeUnavailable("no office")

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", no_office)
    monkeypatch.setattr(pipeline, "env_signature", lambda: {"office": False, "acrobat": False})
    first = pipeline.run(cfg, index=StubIndex())
    assert [reason for _, reason in first.failed] == ["no_office"]
    # 同一台机器再跑一轮：不重复付时空
    again = pipeline.run(cfg, index=StubIndex())
    assert again.skipped.get("known_failure") == 1
    # 那台机器后来装了 Office
    monkeypatch.setattr(
        office_bridge, "extract_wordopenxml", lambda *a, **k: flat_opc
    )
    monkeypatch.setattr(pipeline, "env_signature", lambda: {"office": True, "acrobat": False})
    healed = pipeline.run(cfg, index=StubIndex())
    assert healed.converted == 1
    assert healed.failed == []


def test_env_change_keeps_file_specific_failures(one_bad_doc, monkeypatch):
    """1.2: 只清"机器缺东西"那两类；超时/损坏是文件自己的问题，装了 Office 也不该重试。"""
    cfg, _target = one_bad_doc

    def always_timeout(*args, **kwargs):
        raise office_bridge.OfficeTimeout("boom")

    monkeypatch.setattr(office_bridge, "extract_wordopenxml", always_timeout)
    monkeypatch.setattr(pipeline, "env_signature", lambda: {"office": True, "acrobat": False})
    pipeline.run(cfg, index=StubIndex())
    monkeypatch.setattr(pipeline, "env_signature", lambda: {"office": True, "acrobat": True})
    report = pipeline.run(cfg, index=StubIndex())
    assert report.skipped.get("known_failure") == 1
    assert report.failed == []


def test_env_signature_is_recorded_in_state(one_bad_doc, monkeypatch):
    """1.2: 指纹落在 state.json 里，下一轮才比得出来。"""
    cfg, _target = one_bad_doc
    monkeypatch.setattr(pipeline, "env_signature", lambda: {"office": True, "acrobat": False})
    monkeypatch.setattr(pipeline, "convert_one", lambda *a, **k: ("正文", "direct"))
    pipeline.run(cfg, index=StubIndex())
    state = json.loads((Path(cfg.lib_dir) / pipeline.STATE_NAME).read_text(encoding="utf-8"))
    assert state["env"] == {"office": True, "acrobat": False}
