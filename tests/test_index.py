from __future__ import annotations

import sqlite3
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
    # 2 字查询走 instr 兜底，但片段里的命中词同样要标 <<>>，否则网页/助手都看不出命中了哪
    assert "<<乐平>>" in rows[0]["snippet"]


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


def test_recent_orders_by_indexed_at_then_rowid(idx: Index):
    """最近收录：新入库的排前面，同一秒入库的靠 rowid 稳定兜底。"""
    idx.upsert("old.md", "老文档", "docx", "/s/old.docx", 10, 1.0, "老内容")
    idx.upsert("new.md", "新文档", "docx", "/s/new.docx", 10, 1.0, "新内容")
    idx.conn.execute("UPDATE documents SET indexed_at = '2026-01-01 00:00:00' WHERE rel_path = 'old.md'")
    idx.conn.execute("UPDATE documents SET indexed_at = '2026-11-18 09:00:00' WHERE rel_path = 'new.md'")
    idx.commit()
    assert [r["path"] for r in idx.recent()] == ["new.md", "old.md"]


def test_recent_honours_limit_and_empty_library(idx: Index):
    assert idx.recent() == []
    for i in range(5):
        idx.upsert(f"f{i}.md", f"文档{i}", "docx", f"/s/f{i}.docx", 10, 1.0, "乐平")
    assert len(idx.recent(limit=3)) == 3
    assert len(idx.recent(limit=0)) == 1  # 下界夹到 1，绝不返回空列表骗前端


def test_fts_shares_one_copy_of_the_text(idx: Index):
    """正文只存 documents 一份：FTS 是 external content 表，不再自带 title/content。"""
    idx.upsert("a.md", "乐平枢纽", "docx", "/s/a.docx", 10, 1.0, "乐平水利枢纽工程概况")
    idx.commit()
    sql = idx.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents_fts'"
    ).fetchone()[0]
    assert "content='documents'" in sql and "content_rowid='rowid'" in sql
    # external content 表自身不存正文：查询仍能 JOIN 出正文，片段也从 documents 生成
    rows, _ = idx.search("乐平")
    assert rows[0]["snippet"].count("乐平") >= 1
    assert "<<乐平>>" in rows[0]["snippet"]


def test_triggers_keep_fts_in_sync_across_upsert_and_delete(idx: Index):
    """不手工删插 FTS：INSERT/UPDATE/DELETE 触发器要能把倒排跟着改对。"""
    idx.upsert("a.md", "马斯京根演算", "docx", "/s/a.docx", 10, 1.0, "马斯京根法河道洪水演算")
    idx.commit()
    assert idx.search("马斯京根")[0]
    # 同名改正文 + 改标题：旧词必须消失，新词必须能命中，且 FTS 里只有这一行
    idx.upsert("a.md", "鄱阳湖调度", "docx", "/s/a.docx", 20, 2.0, "鄱阳湖星子站水位过程")
    idx.commit()
    assert idx.search("马斯京根")[0] == []
    assert idx.search("鄱阳湖")[0][0]["title"] == "鄱阳湖调度"
    assert idx.conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 1
    idx.delete("a.md")
    idx.commit()
    assert idx.search("鄱阳湖")[0] == []
    assert idx.conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 0


LEGACY_SCHEMA = """
CREATE TABLE documents (
    rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path   TEXT UNIQUE NOT NULL,
    title      TEXT NOT NULL,
    ext        TEXT,
    source_path TEXT,
    src_size   INTEGER NOT NULL,
    src_mtime  REAL NOT NULL,
    content    TEXT NOT NULL,
    indexed_at TEXT
);
CREATE VIRTUAL TABLE documents_fts USING fts5(title, content, tokenize='trigram');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _legacy_library(db: Path) -> None:
    """造一个 0.2.x 的库：documents_fts 自带正文、没有触发器。"""
    conn = sqlite3.connect(str(db))
    conn.executescript(LEGACY_SCHEMA)
    for rel, title, content in (
        ("a.md", "乐平枢纽", "乐平水利枢纽工程概况"),
        ("b.md", "马斯京根", "马斯京根法河道洪水演算"),
    ):
        conn.execute(
            "INSERT INTO documents (rel_path, title, ext, source_path, src_size, src_mtime, content, indexed_at)"
            " VALUES (?, ?, 'docx', '/s/x.docx', 10, 1.0, ?, '2026-11-17 10:00:00')",
            (rel, title, content),
        )
        rowid = conn.execute("SELECT rowid FROM documents WHERE rel_path = ?", (rel,)).fetchone()[0]
        conn.execute(
            "INSERT INTO documents_fts (rowid, title, content) VALUES (?, ?, ?)", (rowid, title, content)
        )
    conn.commit()
    conn.close()


def test_legacy_library_is_migrated_in_place(tmp_path: Path):
    """老库打开时自动换成 external content 表并重建倒排，检索不丢。"""
    db = tmp_path / "library.db"
    _legacy_library(db)
    idx = Index(db)
    sql = idx.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents_fts'"
    ).fetchone()[0]
    assert "content='documents'" in sql
    for name in ("documents_ai", "documents_ad", "documents_au"):
        assert idx.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name = ?", (name,)
        ).fetchone()
    assert idx.search("马斯京根")[0][0]["path"] == "b.md"  # 倒排已重建
    assert idx.stats()["total"] == 2
    # 再开一次是幂等的：不重建成空表，触发器也还在
    again = Index(db)
    assert again.search("乐平")[0][0]["path"] == "a.md"
    assert again.conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 2
