from __future__ import annotations

from landoc import render_docx


def test_headings_become_markdown(docx_with_headings_bytes):
    md = render_docx.docx_bytes_to_md(docx_with_headings_bytes)
    assert "# 1  工程概况" in md
    assert "## 2.1  设计洪水" in md
    assert "正文第一段。" in md


def test_paragraph_order_is_document_order(docx_with_headings_bytes):
    md = render_docx.docx_bytes_to_md(docx_with_headings_bytes)
    assert md.index("工程概况") < md.index("正文第一段") < md.index("设计洪水")


def test_tables_become_gfm(docx_with_table_bytes):
    md = render_docx.docx_bytes_to_md(docx_with_table_bytes)
    assert md.count("|") >= 8
    assert "\n|---|---|---|\n" in md  # GFM 分隔行
    assert "R1C2" in md


def test_comments_are_stripped(docx_bytes_with_comments):
    md = render_docx.docx_bytes_to_md(docx_bytes_with_comments)
    assert "正文内容。" in md
    assert "评审意见" not in md


def test_pipe_inside_cell_is_escaped():
    import io

    import docx

    d = docx.Document()
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "a|b"
    t.cell(0, 1).text = "c"
    t.cell(1, 0).text = "1"
    t.cell(1, 1).text = "2"
    buf = io.BytesIO()
    d.save(buf)
    md = render_docx.docx_bytes_to_md(buf.getvalue())
    assert "a\\|b" in md


def test_empty_document_returns_empty_string():
    import io

    import docx

    buf = io.BytesIO()
    docx.Document().save(buf)
    assert render_docx.docx_bytes_to_md(buf.getvalue()).strip() == ""
