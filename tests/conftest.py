"""pytest 共享夹具：仓库根加入 sys.path；slow 标记默认跳过。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TESTDATA = Path(__file__).resolve().parent / "testdata"
DESKTOP_DOCX = Path(r"C:\Users\Administrator\Desktop\2026.11.10 乐平下闸蓄水设计自检报告-水文.docx")


def pytest_collection_modifyitems(config, items):
    """没设 LANSHARE_IT=1 时跳过 slow（要真机 Office / 真实加密文件）的用例。"""
    if os.environ.get("LANSHARE_IT") == "1":
        return
    skip = pytest.mark.skip(reason="需要真机环境：设置 LANSHARE_IT=1 后运行")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def flat_opc() -> str:
    """一份小文档的 Flat OPC 字符串（由 tests/testdata/make_flat_opc.py 生成）。"""
    return (TESTDATA / "flat_opc_sample.xml").read_text(encoding="utf-8")


@pytest.fixture
def plain_docx_path(tmp_path: Path) -> Path:
    """python-docx 生成的明文 docx：12 段 + 1 表。"""
    import docx

    d = docx.Document()
    d.add_heading("1  工程概况", level=1)
    for i in range(10):
        d.add_paragraph(f"第 {i + 1} 段正文")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "A"
    t.cell(1, 1).text = "B"
    path = tmp_path / "plain.docx"
    d.save(path)
    return path


@pytest.fixture
def encrypted_docx_path() -> Path:
    """桌面上那份真实加密 docx（密文头 17da5fa0），仅 slow 用例使用。"""
    if not DESKTOP_DOCX.is_file():
        pytest.skip(f"真实加密文件不存在：{DESKTOP_DOCX}")
    return DESKTOP_DOCX


@pytest.fixture
def docx_with_headings_bytes() -> bytes:
    """带 Heading 1/2 与中文标题样式的 docx 字节。"""
    import io

    import docx

    d = docx.Document()
    d.add_heading("1  工程概况", level=1)
    d.add_paragraph("正文第一段。")
    d.add_heading("2.1  设计洪水", level=2)
    d.add_paragraph("正文第二段。")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def docx_with_table_bytes() -> bytes:
    import io

    import docx

    d = docx.Document()
    d.add_paragraph("表 1  特征水位表")
    t = d.add_table(rows=3, cols=3)
    for r, row in enumerate(t.rows):
        for c, cell in enumerate(row.cells):
            cell.text = f"R{r}C{c}"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def docx_bytes_with_comments() -> bytes:
    """docx 包内注入 comments.xml：批注文字绝不能出现在 md 里。"""
    import io
    import zipfile

    import docx

    d = docx.Document()
    d.add_paragraph("正文内容。")
    buf = io.BytesIO()
    d.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in src.namelist():
            zf.writestr(item, src.read(item))
        zf.writestr(
            "word/comments.xml",
            '<?xml version="1.0" encoding="UTF-8"?><w:comments '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:comment><w:p><w:r><w:t>评审意见：此处需补充论证</w:t></w:r></w:p></w:comment>"
            "</w:comments>",
        )
    return out.getvalue()


DESKTOP_PPTX = Path(
    r"C:\Users\Administrator\Desktop\测试文件\WorkBuddy水文分析实战运用汇报-溪霞水库.pptx"
)


@pytest.fixture
def plain_pptx_path(tmp_path: Path) -> Path:
    """python-pptx 生成的明文演示文稿：2 页，含文本框与表格。"""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "溪霞水库除险加固"
    slide.placeholders[1].text = "汇报人：王昱东"
    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    slide2.shapes.title.text = "特征水位"
    table = slide2.shapes.add_table(2, 2, Inches(1), Inches(1.5), Inches(4), Inches(1)).table
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "数值"
    table.cell(1, 0).text = "正常蓄水位"
    table.cell(1, 1).text = "58.5"
    path = tmp_path / "plain.pptx"
    prs.save(path)
    return path


@pytest.fixture
def encrypted_pptx_path() -> Path:
    """桌面上那份真实加密 pptx（密文头 17da5fa0），仅 slow 用例使用。"""
    if not DESKTOP_PPTX.is_file():
        pytest.skip(f"真实加密文件不存在：{DESKTOP_PPTX}")
    return DESKTOP_PPTX
