"""docx 字节 → Markdown。

刻意只用 `python-docx` 读 `w:body` 的子节点顺序，不读 `comments.xml`：
Flat OPC 里带的批注是内部评审意见，进了索引就等于把它共享给全院。
"""

from __future__ import annotations

import io
import re

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

_HEADING_RE = re.compile(r"^(?:heading|标题)\s*([1-6])$", re.IGNORECASE)
_TITLE_NAMES = {"title", "标题"}


def _heading_level(style) -> int:
    name = (getattr(style, "name", "") or "").strip()
    m = _HEADING_RE.match(name)
    if m:
        return int(m.group(1))
    if name.lower() in _TITLE_NAMES:
        return 1
    return 0


def _cell_text(cell) -> str:
    text = " ".join(p.text.strip() for p in cell.paragraphs).strip()
    return text.replace("\n", " ").replace("|", "\\|")


def _table_to_gfm(table: Table) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        rows.append([_cell_text(cell) for cell in row.cells])
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def docx_bytes_to_md(data: bytes) -> str:
    """把 docx 字节渲染成 Markdown（标题用 #，表格用 GFM，批注不参与）。"""
    document = Document(io.BytesIO(data))
    blocks: list[str] = []

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            level = _heading_level(para.style)
            blocks.append(f"{'#' * level} {text}" if level else text)
        elif child.tag == qn("w:tbl"):
            rendered = _table_to_gfm(Table(child, document))
            if rendered:
                blocks.append(rendered)

    return "\n\n".join(blocks)
