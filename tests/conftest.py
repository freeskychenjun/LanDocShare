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


@pytest.fixture(autouse=True)
def _fresh_local_ips_cache():
    """`netinfo.cached_local_ips()` 是进程级缓存：每个用例前后都清掉。

    否则某个用例把 `local_ips` 换成假地址（验证 owner 判定），缓存会把假地址
    带进后面的用例里。
    """
    from landoc import netinfo

    netinfo.clear_local_ips_cache()
    yield
    netinfo.clear_local_ips_cache()


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


DESKTOP_PDF = Path(
    r"C:\Users\Administrator\Desktop\测试文件\附件1 江西省“十五五”水安全保障规划报告（送审稿）.pdf"
)
ACROBAT_TOOL_DIR = Path(r"D:\应用软件\右键解密")


@pytest.fixture
def plain_pdf_path(tmp_path: Path) -> Path:
    """pymupdf 生成的明文 PDF（含中文，需 CJK 内置字体）。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "附件1 江西省十五五水安全保障规划报告", fontname="china-s", fontsize=14)
    page.insert_text((72, 140), "马斯京根法河道洪水演算示例", fontname="china-s", fontsize=12)
    path = tmp_path / "plain.pdf"
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def encrypted_pdf_path() -> Path:
    """桌面上那份真实加密 PDF（密文头 17da5fa0），仅 slow 用例使用。"""
    if not DESKTOP_PDF.is_file():
        pytest.skip(f"真实加密文件不存在：{DESKTOP_PDF}")
    return DESKTOP_PDF


@pytest.fixture
def cfg_with_tool_dir(tmp_path: Path):
    """配置里带兜底工具目录（acrobat.exe 存在即可，实际解密由 monkeypatch 模拟）。"""
    from landoc.config import Config

    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "acrobat.exe").write_bytes(b"MZ")
    return Config(name="t", lib_dir=tmp_path / "lib", acrobat_tool_dir=tool_dir)


@pytest.fixture
def cfg_without_tool(tmp_path: Path):
    from landoc.config import Config

    return Config(name="t", lib_dir=tmp_path / "lib")


@pytest.fixture
def plaintext_pdf_bytes() -> bytes:
    """一段最小明文 PDF 字节，供"模拟解密工具"写回副本用。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "附件1 江西省十五五水安全保障规划报告", fontname="china-s", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def real_encrypted_docs_dir(tmp_path: Path) -> Path:
    """把三份真实样本（加密 docx / 加密 pptx / 加密 pdf）复制到一个临时目录。

    python 读到的是密文、写出的也还是密文，所以复制不改变加密状态。
    """
    import shutil

    sources = [
        DESKTOP_DOCX,
        DESKTOP_PPTX,
        DESKTOP_PDF,
    ]
    missing = [str(p) for p in sources if not p.is_file()]
    if missing:
        pytest.skip(f"真实样本缺失：{missing}")
    target = tmp_path / "真实样本"
    target.mkdir()
    for src in sources:
        shutil.copy2(src, target / src.name)
    return target


@pytest.fixture
def acrobat_tool_dir() -> Path:
    tool_dir = Path(r"D:\应用软件\右键解密")
    if not (tool_dir / "acrobat.exe").is_file():
        pytest.skip(f"兜底工具缺失：{tool_dir}")
    return tool_dir
