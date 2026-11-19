from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from landoc import pdf_conv
from landoc.config import Config


def test_plain_pdf_uses_pymupdf(plain_pdf_path: Path):
    result = pdf_conv.pdf_to_text(plain_pdf_path)
    assert result.via == "pymupdf"
    assert "附件1" in result.text


def test_encrypted_pdf_without_acrobat_uses_disguise(
    encrypted_pdf_path, cfg_with_tool_dir, monkeypatch, plaintext_pdf_bytes
):
    """没有 Acrobat 时走兜底：解临时副本 → 抽文本 → 删副本。"""
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: False)
    monkeypatch.setattr(
        pdf_conv, "_run_tool", lambda tool, target, cwd, timeout: target.write_bytes(plaintext_pdf_bytes)
    )
    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_with_tool_dir)
    assert result.via == "acrobat_disguise"
    assert "附件1" in result.text


def test_disguise_leaves_no_plaintext_copy(
    encrypted_pdf_path, cfg_with_tool_dir, monkeypatch, plaintext_pdf_bytes, tmp_path
):
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: False)
    monkeypatch.setattr(
        pdf_conv, "_run_tool", lambda tool, target, cwd, timeout: target.write_bytes(plaintext_pdf_bytes)
    )
    pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_with_tool_dir)
    leftovers = list(Path(tmp_path).rglob("*.pdf"))
    assert all(p.read_bytes()[:4] != b"%PDF" or p.name == "plain.pdf" for p in leftovers)


def test_encrypted_pdf_without_tool_is_skipped(encrypted_pdf_path, cfg_without_tool, monkeypatch):
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: False)
    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_without_tool)
    assert result.via == "skipped"
    assert result.reason == "no_reader"


def test_unreadable_path_is_skipped(tmp_path: Path):
    result = pdf_conv.pdf_to_text(tmp_path / "nope.pdf")
    assert result.via == "skipped"
    assert result.reason == "unreadable"


def _add_image_page(doc):
    """给 PDF 加一页"整页是图"的页面：OCR 只对这种页面动手（见 pdf_conv._ocr_text）。"""
    import fitz

    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), 0)
    pix.clear_with(200)
    page.insert_image(fitz.Rect(0, 0, 8, 8), pixmap=pix)
    return page


def test_pdf_without_text_layer_reports_reason(tmp_path: Path, monkeypatch):
    import fitz

    doc = fitz.open()
    _add_image_page(doc)  # 扫描页：有图、没有文本层
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    # OCR 这条路单独测；这里只关心"有 OCR 但没识别出字"时记 no_text_layer
    monkeypatch.setattr(pdf_conv.ocr, "backend", lambda lang="zh-Hans-CN": "windows")
    monkeypatch.setattr(pdf_conv, "_ocr_text", lambda path, cfg=None: None)
    result = pdf_conv.pdf_to_text(path)
    assert result.via == "pymupdf"
    assert result.reason == "no_text_layer"


def test_scanned_pdf_reports_no_ocr_when_engine_is_missing(tmp_path: Path, monkeypatch):
    import fitz

    doc = fitz.open()
    _add_image_page(doc)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    monkeypatch.setattr(pdf_conv.ocr, "backend", lambda lang="zh-Hans-CN": "")
    result = pdf_conv.pdf_to_text(path)
    assert result.via == "pymupdf"
    assert result.reason == "no_ocr"


def test_scanned_pdf_is_ocrd_when_engine_is_available(tmp_path: Path, monkeypatch):
    """没有文字层的 PDF 走 OCR：结果带上提示行与页码锚点，via 记 ocr。"""
    import fitz

    doc = fitz.open()
    _add_image_page(doc)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    monkeypatch.setattr(pdf_conv.ocr, "backend", lambda lang="zh-Hans-CN": "windows")
    monkeypatch.setattr(
        pdf_conv.ocr,
        "ocr_images",
        lambda images, lang="zh-Hans-CN", timeout=None: "江 西 省 水 利 厅 文 件" * 5,
    )
    result = pdf_conv.pdf_to_text(path)
    assert result.via == "ocr"
    assert result.reason == ""
    assert pdf_conv.OCR_NOTICE in result.text
    assert "<!-- page 1 -->" in result.text
    assert "江 西 省" not in result.text  # 汉字之间的空格要清掉


def test_short_text_pdf_without_images_is_not_ocrd(tmp_path: Path, monkeypatch):
    """文字很少但没有图的 PDF（一页短通知）不该白渲染一遍：OCR 只认"整页是图"的扫描件。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "附件1", fontsize=12)
    path = tmp_path / "short.pdf"
    doc.save(path)
    doc.close()
    monkeypatch.setattr(pdf_conv.ocr, "backend", lambda lang="zh-Hans-CN": "windows")
    monkeypatch.setattr(
        pdf_conv.ocr,
        "ocr_images",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("没有图的 PDF 不该跑 OCR")),
    )
    result = pdf_conv.pdf_to_text(path)
    assert result.via == "pymupdf"
    assert result.reason == "no_text_layer"


def test_ocr_can_be_switched_off_per_library(tmp_path: Path, monkeypatch):
    import fitz

    from landoc.config import Config

    doc = fitz.open()
    _add_image_page(doc)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    cfg = Config(name="t", lib_dir=tmp_path, ocr_scanned_pdfs=False)
    monkeypatch.setattr(pdf_conv.ocr, "backend", lambda lang="zh-Hans-CN": "windows")
    monkeypatch.setattr(
        pdf_conv.ocr,
        "ocr_images",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("关掉 OCR 后不该渲染任何页面")),
    )
    result = pdf_conv.pdf_to_text(path, cfg=cfg)
    assert result.via == "pymupdf"
    assert result.reason == "no_text_layer"


def test_decrypt_via_disguise_rejects_tool_that_changed_nothing(
    encrypted_pdf_path: Path, cfg_with_tool_dir, monkeypatch
):
    monkeypatch.setattr(pdf_conv, "_run_tool", lambda tool, target, cwd, timeout: None)
    with pytest.raises(pdf_conv.PDFDecryptError):
        pdf_conv.decrypt_via_disguise(encrypted_pdf_path, cfg_with_tool_dir.acrobat_tool_dir)


@pytest.mark.slow
def test_real_encrypted_pdf_decrypts_with_real_tool(encrypted_pdf_path: Path):
    """真机：拿库主人现成的 acrobat.exe 解一份副本，验证文本层可读。"""
    tool_dir = Path(r"D:\应用软件\右键解密")
    if not (tool_dir / "acrobat.exe").is_file():
        pytest.skip(f"兜底工具不存在：{tool_dir}")
    cfg = Config(name="t", lib_dir=encrypted_pdf_path.parent, acrobat_tool_dir=tool_dir)
    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg)
    assert result.via in {"acrobat_disguise", "acrobat"}
    assert len(result.text) > 3000
    assert "十五五" in result.text


def test_acrobat_is_used_when_available(encrypted_pdf_path, cfg_without_tool, monkeypatch):
    """本机有 Acrobat 时走 Acrobat（白名单内、不需要临时明文副本）。"""
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: True)
    monkeypatch.setattr(
        pdf_conv, "_acrobat_text", lambda path, timeout=120.0: "十五五水安全保障规划" * 20
    )
    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_without_tool)
    assert result.via == "acrobat"
    assert "水安全保障" in result.text


# ============ PDFSession：一个 Acrobat 连转一批（每篇省 5.5 秒起停）============


class _FakeSize:
    x = 595.0
    y = 842.0


class _FakePage:
    def GetSize(self):
        return _FakeSize()

    def Release(self):
        pass


class _FakeRect:
    pass


class _FakeSelection:
    def __init__(self, text: str):
        self._tokens = list(text)

    def GetNumText(self):
        return len(self._tokens)

    def GetText(self, i):
        return self._tokens[i]


class _FakeApp:
    def __init__(self):
        self.hidden = False
        self.exited = False

    def Hide(self):
        self.hidden = True

    def Exit(self):
        self.exited = True


class _FakePDDoc:
    def __init__(self, pages):
        self._pages = list(pages)
        self.closed = False

    def Open(self, path):
        return True

    def GetNumPages(self):
        return len(self._pages)

    def AcquirePage(self, index):
        return _FakePage()

    def CreateTextSelect(self, index, rect):
        return _FakeSelection(self._pages[index])

    def Close(self):
        self.closed = True


class _FakeCom:
    """按 ProgID 造替身对象，并数一共起了几次 App / 几次 PDDoc。"""

    def __init__(self, pages=("首页正文", "第二页正文")):
        self.pages = list(pages)
        self.app_dispatches = 0
        self.pddoc_dispatches = 0
        self.rect_dispatches = 0
        self.apps: list[_FakeApp] = []
        self.pddocs: list[_FakePDDoc] = []
        self.on_pddoc = None

    def __call__(self, progid):
        if progid == "AcroExch.App":
            self.app_dispatches += 1
            app = _FakeApp()
            self.apps.append(app)
            return app
        if progid == "AcroExch.Rect":
            self.rect_dispatches += 1
            return _FakeRect()
        self.pddoc_dispatches += 1
        hook, self.on_pddoc = self.on_pddoc, None
        if hook is not None:
            hook(self.pddoc_dispatches)
        pd = _FakePDDoc(self.pages)
        self.pddocs.append(pd)
        return pd


def _silence_process_refs(monkeypatch, pids=None):
    alive = {"pids": set(pids or ())}
    killed: list[set[int]] = []
    monkeypatch.setattr(pdf_conv.office_bridge, "_acrobat_pids", lambda: set(alive["pids"]))
    monkeypatch.setattr(
        pdf_conv.office_bridge,
        "_kill_office_processes",
        lambda pids=None: killed.append(set(pids or ())),
    )
    return alive, killed


def test_pdf_session_reuses_one_acrobat_instance(monkeypatch):
    """核心收益：三篇共用一个 Acrobat（原来每篇都要 Dispatch 一次、杀一次）。"""
    com = _FakeCom()
    _silence_process_refs(monkeypatch)
    with pdf_conv.PDFSession(dispatch=com) as session:
        for _ in range(3):
            text = session.extract(Path("x.pdf"))
            assert "首页正文" in text and "第二页正文" in text
        assert session.used == 3
    assert com.app_dispatches == 1  # 一次启动，不是三次
    assert com.pddoc_dispatches == 3  # 每篇一个新 PDDoc（复用 PDDoc 实测没有额外收益）
    assert all(pd.closed for pd in com.pddocs)
    assert com.apps[0].exited  # 收尾走 Exit，不留残留进程


def test_pdf_session_rebuilds_after_acrobat_was_closed(monkeypatch):
    """库主人手动把 Acrobat 关掉：断连要能被识别，重建后重试一次而不是记失败。"""

    class Gone(Exception):
        hresult = -2147023174  # RPC 服务器不可用

    com = _FakeCom()
    com.on_pddoc = lambda n: (_ for _ in ()).throw(Gone("RPC 服务器不可用"))
    _silence_process_refs(monkeypatch)
    with pdf_conv.PDFSession(dispatch=com) as session:
        assert "首页正文" in session.extract(Path("x.pdf"))
    assert com.app_dispatches == 2  # 第一次不灵，第二次重建
    assert com.pddoc_dispatches == 2


def test_pdf_session_timeout_kills_only_its_own_processes(monkeypatch):
    import time as _time

    alive, killed = _silence_process_refs(monkeypatch, pids={101, 102})
    com = _FakeCom()

    def slow(n):
        alive["pids"].add(999)  # 我们自己拉起来的那个
        _time.sleep(0.6)

    com.on_pddoc = slow
    with pdf_conv.PDFSession(dispatch=com, timeout=0.2) as session:
        with pytest.raises(pdf_conv.office_bridge.OfficeTimeout):
            session.extract(Path("x.pdf"))
    flat = set().union(*killed) if killed else set()
    assert 999 in flat, "超时必须杀掉本会话新起的 Acrobat"
    assert 101 not in flat and 102 not in flat, "库主人自己开着的阅读器不能动"


def test_pdf_to_text_uses_shared_session(encrypted_pdf_path, cfg_without_tool, monkeypatch):
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: True)
    seen: list[Path] = []

    class FakeSession:
        def extract(self, path, timeout=None):
            seen.append(path)
            return "十五五水安全保障规划" * 20

    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_without_tool, session=FakeSession())
    assert result.via == "acrobat"
    assert seen == [encrypted_pdf_path]


def test_acrobat_failure_falls_back_to_disguise(
    encrypted_pdf_path, cfg_with_tool_dir, plaintext_pdf_bytes, monkeypatch
):
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: True)

    def boom(path, timeout=120.0):
        raise pymupdf_error()

    def pymupdf_error():
        return RuntimeError("Acrobat 崩了")

    monkeypatch.setattr(pdf_conv, "_acrobat_text", boom)
    monkeypatch.setattr(
        pdf_conv, "_run_tool", lambda tool, target, cwd, timeout: target.write_bytes(plaintext_pdf_bytes)
    )
    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_with_tool_dir)
    assert result.via == "acrobat_disguise"


@pytest.mark.slow
def test_real_acrobat_extracts_encrypted_pdf(encrypted_pdf_path: Path):
    """真机：Acrobat 直接打开加密 PDF 取文本，不落任何明文副本。"""
    if not pdf_conv.has_acrobat():
        pytest.skip("本机没有 Adobe Acrobat")
    before = set(Path(tempfile.gettempdir()).glob("landoc_pdf_*"))
    result = pdf_conv.pdf_to_text(encrypted_pdf_path)
    assert result.via == "acrobat"
    assert len(result.text) > 3000
    assert "十五五" in result.text and "水安全保障" in result.text
    after = set(Path(tempfile.gettempdir()).glob("landoc_pdf_*"))
    assert after == before, "Acrobat 路线不应该产生临时明文目录"
