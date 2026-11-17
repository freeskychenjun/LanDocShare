"""端到端验收：三份真实加密文档跑通 建库 → 转换 → 索引 → 检索。

slow 用例（需要真机 Office 与真实加密样本）：`set LANSHARE_IT=1` 后运行。
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from landoc import cli
from landoc.config import Config
from landoc.index import Index
from landoc.server import make_server


@pytest.mark.slow
def test_real_encrypted_docs_end_to_end(tmp_path: Path, real_encrypted_docs_dir: Path, acrobat_tool_dir: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    assert (
        cli.main(
            ["init", "--lib-dir", str(lib), "--name", "验收库", "--folder", str(real_encrypted_docs_dir)]
        )
        == 0
    )
    cfg = Config.load(lib)
    cfg.acrobat_tool_dir = acrobat_tool_dir
    cfg.save()

    assert cli.main(["convert", "--lib-dir", str(lib)]) == 0

    index = Index(cfg.db_path, md_root=cfg.md_dir)
    total = index.stats()["total"]
    assert total >= 3, f"只转换出 {total} 篇"

    # 加密 docx（COM → Flat OPC → 重建 docx）能被检索到
    rows, _ = index.search("乐平")
    assert any("乐平" in r["title"] or "乐平" in r["snippet"] for r in rows)

    # 加密 pptx（COM 形状文本）
    rows, _ = index.search("溪霞")
    assert rows

    # 加密 pdf（兜底工具解临时副本）
    rows, _ = index.search("水安全")
    assert rows


@pytest.mark.slow
def test_search_served_over_http(tmp_path: Path, real_encrypted_docs_dir: Path, acrobat_tool_dir: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    cli.main(["init", "--lib-dir", str(lib), "--name", "验收库", "--folder", str(real_encrypted_docs_dir)])
    cfg = Config.load(lib)
    cfg.acrobat_tool_dir = acrobat_tool_dir
    cfg.allow_cidrs = ["127.0.0.1"]
    cfg.save()
    cli.main(["convert", "--lib-dir", str(lib)])

    cfg = Config.load(lib)
    cfg.allow_cidrs = ["127.0.0.1"]
    httpd = make_server(cfg, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _, port = httpd.server_address[:2]
    try:
        url = f"http://127.0.0.1:{port}/api/search?q=" + urllib.parse.quote("溪霞") + "&limit=5"
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["count"] >= 1
        first = payload["results"][0]
        assert set(first) == {"path", "title", "ext", "score", "snippet"}
        assert "/secret" not in json.dumps(payload)
    finally:
        httpd.shutdown()
        httpd.server_close()


def _make_legacy_doc_and_ppt(folder: Path) -> None:
    """造两份结构完整的老格式文件（.doc / .ppt）。

    先用 python-docx / python-pptx 生成 OOXML，再让 WPS 另存成 97-2003 老格式 ——
    白名单进程写盘即密文，所以产出的 .doc/.ppt 是真实加密件，正好覆盖老格式路径。
    """
    import docx
    from pptx import Presentation
    from pptx.util import Inches

    d = docx.Document()
    d.add_heading("1  工程概况", level=1)
    d.add_paragraph("乐平水利枢纽工程地处江西省乐平市鸬鹚乡对家村。")
    d.add_heading("2.1  设计洪水", level=2)
    d.add_paragraph("马斯京根法用于河道洪水演算。")
    table = d.add_table(rows=3, cols=3)
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row.cells):
            cell.text = f"R{r}C{c}"
    src_docx = folder / "rich.docx"
    d.save(src_docx)

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "溪霞水库除险加固"
    slide.placeholders[1].text = "汇报人：王昱东"
    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    slide2.shapes.title.text = "特征水位表"
    tb = slide2.shapes.add_table(2, 2, Inches(1), Inches(1.5), Inches(4), Inches(1)).table
    tb.cell(0, 0).text = "项目"
    tb.cell(0, 1).text = "数值"
    tb.cell(1, 0).text = "正常蓄水位"
    tb.cell(1, 1).text = "58.5"
    src_pptx = folder / "rich.pptx"
    prs.save(src_pptx)

    import time as _time

    import win32com.client as wc

    def dispatch(progid, attempts=4):
        """上一个用例刚杀过 Office 进程时，DispatchEx 可能撞上正在退出的服务，重试几次。"""
        last = None
        for _ in range(attempts):
            try:
                return wc.DispatchEx(progid)
            except Exception as exc:  # noqa: BLE001
                last = exc
                _time.sleep(1.5)
        pytest.skip(f"{progid} 起不来（库主人的办公软件可能正被占用/有对话框）：{last}")

    word = dispatch("KWPS.Application")
    try:
        word.DisplayAlerts = 0
    except Exception:  # noqa: BLE001
        pass
    doc = word.Documents.Open(str(src_docx))
    doc.SaveAs2(str(folder / "rich.doc"), 0)  # 0 = 97-2003 .doc
    doc.Close(False)

    ppt = dispatch("KWPP.Application")
    try:
        ppt.DisplayAlerts = 0
    except Exception:  # noqa: BLE001
        pass
    pres = ppt.Presentations.Open(str(src_pptx), ReadOnly=False, WithWindow=False)
    pres.SaveAs(str(folder / "rich.ppt"), 1)
    pres.Close()

    src_docx.unlink()
    src_pptx.unlink()


@pytest.mark.slow
def test_legacy_doc_and_ppt_keep_structure(tmp_path: Path):
    """老格式（加密 .doc / .ppt）必须走 COM 且保住标题层级与表格。"""
    folder = tmp_path / "老格式"
    folder.mkdir()
    _make_legacy_doc_and_ppt(folder)

    for name in ("rich.doc", "rich.ppt"):
        assert (folder / name).read_bytes()[:4] == bytes.fromhex("17da5fa0"), f"{name} 应为密文"

    lib = tmp_path / "lib"
    lib.mkdir()
    cli.main(["init", "--lib-dir", str(lib), "--name", "老格式", "--folder", str(folder)])
    cli.main(["convert", "--lib-dir", str(lib)])

    cfg = Config.load(lib)
    index = Index(cfg.db_path, md_root=cfg.md_dir)
    assert index.stats()["total"] == 2

    doc_md = (cfg.md_dir / "老格式" / "rich.doc.md").read_text(encoding="utf-8")
    assert "# 1  工程概况" in doc_md
    assert "## 2.1  设计洪水" in doc_md
    assert "| R0C0 | R0C1 | R0C2 |" in doc_md  # 表格结构没丢

    ppt_md = (cfg.md_dir / "老格式" / "rich.ppt.md").read_text(encoding="utf-8")
    assert "## 第 1 页" in ppt_md and "## 第 2 页" in ppt_md
    assert "溪霞水库除险加固" in ppt_md
    assert "| 正常蓄水位 | 58.5 |" in ppt_md
