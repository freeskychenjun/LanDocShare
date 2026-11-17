from __future__ import annotations

from pathlib import Path

import pytest

from landoc.index import Index, build_match_query, like_escape


@pytest.fixture
def idx(tmp_path: Path) -> Index:
    return Index(tmp_path / "library.db")


def test_build_match_query_quotes_and_escapes():
    assert build_match_query("马斯京根 演算") == '"马斯京根" AND "演算"'
    assert build_match_query("and") == '"and"'
    assert build_match_query('say "hi"') == '"say" AND "hi"'


def test_short_query_uses_like_fallback(tmp_path):
    idx = Index(tmp_path / "l.db")
    idx.upsert("a.md", "乐平枢纽", "docx", "/s/a.docx", 10, 1.0, "乐平水利枢纽工程")
    rows, fell_back = idx.search("乐平")
    assert len(rows) == 1 and fell_back is False


def test_like_escape_turns_wildcards_literal():
    assert like_escape("a%b_c\\d") == "a\\%b\\_c\\\\d"


def test_search_and_or_fallback(idx: Index):
    idx.upsert("a.md", "马斯京根演算", "docx", "/s/a.docx", 10, 1.0, "马斯京根法河道洪水演算")
    rows, fell_back = idx.search("马斯京根")
    assert rows and not fell_back


def test_search_falls_back_to_or(idx: Index):
    idx.upsert("a.md", "马斯京根演算", "docx", "/s/a.docx", 10, 1.0, "马斯京根法河道洪水演算")
    rows, fell_back = idx.search("马斯京根 缙云")
    assert fell_back and rows


def test_reserved_words_are_escaped(idx: Index):
    idx.upsert("b.md", "and or near", "docx", "/s/b.docx", 10, 1.0, 'and or near 引号 " 测试')
    rows, _ = idx.search("and or near")
    assert isinstance(rows, list)


def test_upsert_updates_existing_row(idx: Index):
    idx.upsert("a.md", "旧标题", "docx", "/s/a.docx", 10, 1.0, "旧内容")
    idx.upsert("a.md", "新标题", "docx", "/s/a.docx", 20, 2.0, "新内容")
    assert idx.stats()["total"] == 1
    rows, _ = idx.search("新内容")
    assert rows[0]["title"] == "新标题"


def test_tree_and_list_use_index_only(idx: Index):
    idx.upsert("成果/2019年/东堡闸.md", "东堡闸", "docx", "/s/x.docx", 10, 1.0, "东堡")
    idx.upsert("成果/2019年/乐平.md", "乐平", "docx", "/s/y.docx", 10, 1.0, "乐平")
    names = [node["name"] for node in idx.tree("")]
    assert names == ["成果"]
    assert idx.tree("成果")[0] == {"name": "2019年", "count": 2, "path": "成果/2019年"}
    assert [row["title"] for row in idx.list_dir("成果/2019年")] == ["东堡闸", "乐平"]


def test_dir_filter_limits_search(idx: Index):
    idx.upsert("成果/2019年/a.md", "乐平", "docx", "/s/a.docx", 10, 1.0, "乐平水利枢纽")
    idx.upsert("成果/2020年/b.md", "乐平", "docx", "/s/b.docx", 10, 1.0, "乐平水利枢纽")
    rows, _ = idx.search("乐平", dir_filter="成果/2019年")
    assert len(rows) == 1 and rows[0]["path"].startswith("成果/2019年")


def test_tables_from_disk_markdown(idx: Index, tmp_path: Path):
    md = tmp_path / "md" / "c.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "---\nsource_path: /s/c.docx\n---\n\n# 特征水位\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    idx.upsert("c.md", "c", "docx", "/s/c.docx", 10, 1.0, "全文")
    tables = idx.tables("c.md")
    assert tables and tables[0]["markdown"].startswith("| a | b |")
    assert tables[0]["context"] == "# 特征水位"


def test_tables_rejects_path_outside_index(idx: Index):
    assert idx.tables("../etc/passwd") is None


def test_tables_rejects_traversal_even_when_index_has_it(idx: Index):
    """索引被污染（rel_path 含 ..）时，/api/tables 也不能读到库外文件。"""
    idx.upsert("../outside.md", "越界", "docx", "/s/x.docx", 10, 1.0, "| a | b |\n|---|---|\n| 1 | 2 |")
    assert idx.tables("../outside.md") is None


def test_tables_traversal_check_normalizes_backslashes(idx: Index):
    idx.upsert("../../Windows/win.ini", "越界", "docx", "/s/x.docx", 10, 1.0, "| a | b |")
    assert idx.tables("..\\..\\Windows\\win.ini") is None


def test_content_length_reports_stored_chars(idx: Index):
    idx.upsert("a.md", "a", "docx", "/s/a.docx", 10, 1.0, "乐平水利枢纽")
    assert idx.content_length("a.md") == 6
    assert idx.content_length("missing.md") is None


def test_tables_falls_back_to_stored_content(idx: Index):
    idx.upsert("d.md", "d", "docx", "/s/d.docx", 10, 1.0, "| x | y |\n|---|---|\n| 1 | 2 |")
    tables = idx.tables("d.md")
    assert tables and tables[0]["markdown"].startswith("| x | y |")


def test_limit_is_clamped(idx: Index):
    for i in range(40):
        idx.upsert(f"f{i}.md", f"文档{i}", "docx", f"/s/f{i}.docx", 10, 1.0, "乐平水利枢纽工程")
    rows, _ = idx.search("乐平", limit=999)
    assert len(rows) == 30


def test_stats_shape(idx: Index):
    stats = idx.stats()
    assert stats["service"] == "landoc"
    assert stats["total"] == 0


def test_meta_table_roundtrip(idx: Index):
    """I9: meta 表必须存在，否则 set_meta 一调用就炸。"""
    idx.set_meta("port", "8767")
    assert idx.get_meta("port") == "8767"
    assert idx.get_meta("missing", "默认") == "默认"


def test_delete_removes_row_and_fts(idx: Index):
    """I7: 撤回共享时要能把文档从索引里删干净。"""
    idx.upsert("a.md", "乐平枢纽", "docx", "/s/a.docx", 10, 1.0, "乐平水利枢纽工程")
    assert idx.search("乐平")[0]
    idx.delete("a.md")
    idx.commit()
    assert idx.stats()["total"] == 0
    assert idx.search("乐平")[0] == []
