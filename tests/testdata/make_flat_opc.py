"""一次性生成 flat OPC 夹具（tests/testdata/flat_opc_sample.xml）。

为什么需要它：`WordOpenXML` 返回的 Flat OPC 字符串是 COM 桥的输入，单元测试
不能每次都启动 Office，所以把一份**小文档**的 Flat OPC 存成夹具。

用法（只需在夹具需要更新时跑）：
    python tests/testdata/make_flat_opc.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "flat_opc_sample.xml"
TMP_DOCX = HERE / "flat_opc_source.docx"


def build_source_docx(path: Path) -> None:
    """用 python-docx 造一份小文档：12 段正文 + 2 个标题 + 1 张表。"""
    import docx

    d = docx.Document()
    d.add_heading("1  工程概况", level=1)
    d.add_paragraph("乐平水利枢纽工程地处江西省乐平市鸬鹚乡对家村。")
    d.add_heading("2.1  设计洪水", level=2)
    for i in range(10):
        d.add_paragraph(f"第 {i + 1} 段正文，用于验证段落顺序与提取数量。")
    table = d.add_table(rows=3, cols=3)
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row.cells):
            cell.text = f"R{r}C{c}"
    d.save(path)


def main() -> int:
    build_source_docx(TMP_DOCX)
    sys.path.insert(0, str(HERE.parents[1]))
    from landoc import office_bridge

    xml = office_bridge.extract_wordopenxml(TMP_DOCX)
    OUT.write_text(xml, encoding="utf-8")
    print(f"[OK] {OUT} {OUT.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
