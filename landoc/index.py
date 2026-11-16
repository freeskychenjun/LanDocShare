"""FTS5 索引：建库、增量写、检索、目录聚合、表格抽取。

SQL 与 `F:\md_share\share_server.py` 保持一致（同一套已验证的实现）：
- FTS5 用 `content='documents'` 的外部内容表，检索走 `documents_fts MATCH`；
- `tree` / `list_dir` 只从索引聚合，不读磁盘 —— 被排除的目录对同事完全不可见；
- `list_dir` 必须用 `rowid IN (子查询)` 两步查：直接 `LIKE + ORDER BY` 会全表扫描
  含正文的行（实测可超时 30 s 以上），不要"优化"回单层查询。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

import jieba

jieba.setLogLevel(40)

FTS_RESERVED = {"and", "or", "not", "near"}
MAX_LIMIT = 30
MAX_LIST = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
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
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, content,
    tokenize='unicode61'
);
"""


def build_match_query(q: str, op: str = "AND") -> str:
    """查询串 jieba 分词并转义，构造 FTS5 MATCH 表达式。"""
    if any(kw in q.upper() for kw in (" OR ", " NEAR ")):
        return q
    tokens = [t for t in jieba.cut(q.strip()) if t.strip()]
    quoted = []
    for token in tokens:
        token = token.replace('"', '""')
        if token.lower() in FTS_RESERVED:
            token = f'"{token}"'
        quoted.append(f'"{token}"')
    return f" {op} ".join(quoted) if quoted else ""


def _segment(text: str) -> str:
    """jieba 分词后用空格连接，作为 FTS5 的入库文本。"""
    return " ".join(token for token in jieba.cut(text or "") if token.strip())


_SNIPPET_TOKEN_RE = re.compile(r'"([^"]+)"')


def _query_terms(match: str) -> list[str]:
    return [t for t in _SNIPPET_TOKEN_RE.findall(match) if t.strip()]


def _make_snippet(raw: str, match: str, window: int = 48) -> str:
    """从原始正文里截取命中窗口，并用 <<词>> 标出命中位置。"""
    text = " ".join(raw.split())
    terms = [t for t in _query_terms(match) if t and not t.isupper()]
    lowered = text.lower()
    position = -1
    hit = ""
    for term in terms:
        found = lowered.find(term.lower())
        if found >= 0 and (position < 0 or found < position):
            position, hit = found, term
    if position < 0:
        head = text[: window * 2]
        return head + ("…" if len(text) > len(head) else "")
    start = max(0, position - window)
    end = min(len(text), position + len(hit) + window)
    body = text[start:end]
    for term in sorted({hit, *terms}, key=len, reverse=True):
        if not term:
            continue
        body = re.sub(re.escape(term), lambda m: f"<<{m.group(0)}>>", body, flags=re.IGNORECASE)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{body}{suffix}"


def like_escape(s: str) -> str:
    """转义 LIKE 通配符（配合 ESCAPE '\\\\' 使用）。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Index:
    """一个资料库的索引。线程内各持有自己的连接（服务端多线程访问）。"""

    _SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
    _HTML_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)

    def __init__(self, db_path: Path, snippet_tokens: int = 48, md_root: Path | None = None):
        self.db_path = Path(db_path)
        self.snippet_tokens = snippet_tokens
        self.md_root = Path(md_root) if md_root else self.db_path.parent / "md"
        self._local = threading.local()
        with self.conn:
            self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # ---- 写入 ----
    def upsert(self, rel_path, title, ext, source_path, src_size, src_mtime, content) -> None:
        """写一条文档记录。

        FTS 里存的是 **jieba 分段后的文本**：unicode61 会把整段中文当一个 token，
        不分词就永远匹配不到"马斯京根"这种词；原始正文留在 documents.content 里，
        片段（snippet）由 `_make_snippet` 从原文生成，展示才自然。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO documents (rel_path, title, ext, source_path, src_size, src_mtime, content, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(rel_path) DO UPDATE SET
                 title=excluded.title, ext=excluded.ext, source_path=excluded.source_path,
                 src_size=excluded.src_size, src_mtime=excluded.src_mtime,
                 content=excluded.content, indexed_at=excluded.indexed_at""",
            (rel_path, title, ext, source_path, int(src_size), float(src_mtime), content, now),
        )
        rowid = self.conn.execute(
            "SELECT rowid FROM documents WHERE rel_path = ?", (rel_path,)
        ).fetchone()[0]
        self.conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (rowid,))
        self.conn.execute(
            "INSERT INTO documents_fts (rowid, title, content) VALUES (?, ?, ?)",
            (rowid, _segment(title), _segment(content)),
        )

    def commit(self) -> None:
        self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    # ---- 读取 ----
    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return {"total": total, "service": "landoc", "version": "0.1.0"}

    def get_content(self, rel_path: str) -> str | None:
        row = self.conn.execute(
            "SELECT content FROM documents WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        return row[0] if row else None

    def search(self, q: str, dir_filter: str = "", limit: int = 20):
        """AND 检索，无结果自动降级 OR；返回 (rows, fell_back)。"""
        limit = max(1, min(int(limit), MAX_LIMIT))
        match = build_match_query(q, "AND")
        if not match:
            return [], False
        rows = self._run_query(match, dir_filter, limit)
        if rows:
            return rows, False
        match_or = build_match_query(q, "OR")
        if match_or == match:
            return [], False
        return self._run_query(match_or, dir_filter, limit), True

    def _run_query(self, match: str, dir_filter: str, limit: int):
        where = "documents_fts MATCH ?"
        params: list = [match]
        if dir_filter:
            where += " AND documents.rel_path LIKE ? ESCAPE '\\'"
            params.append(like_escape(dir_filter.strip("/\\")) + "/%")
        sql = f"""
            SELECT documents.rel_path, documents.title, documents.ext,
                   bm25(documents_fts, 10.0, 1.0) AS score,
                   documents.content AS raw
            FROM documents_fts
            JOIN documents ON documents.rowid = documents_fts.rowid
            WHERE {where}
            ORDER BY score
            LIMIT ?
        """
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "path": r[0],
                "title": r[1],
                "ext": r[2],
                "score": round(r[3], 2),
                "snippet": (r[4] or "").replace("\n", " "),
            }
            for r in rows
        ]

    def tree(self, rel: str = "") -> list[dict]:
        prefix = (rel or "").replace("\\", "/").strip("/")
        like = like_escape(prefix + "/") if prefix else ""
        rows = self.conn.execute(
            "SELECT rel_path FROM documents WHERE rel_path LIKE ? ESCAPE '\\'", (like + "%",)
        ).fetchall()
        counts: dict[str, int] = {}
        offset = len(prefix) + 1 if prefix else 0
        for (rp,) in rows:
            parts = rp[offset:].split("/")
            if len(parts) >= 2 and parts[0]:
                counts[parts[0]] = counts.get(parts[0], 0) + 1
        return [
            {"name": name, "count": counts[name], "path": f"{prefix}/{name}" if prefix else name}
            for name in sorted(counts)
        ]

    def list_dir(self, rel: str = "", limit: int = MAX_LIST) -> list[dict]:
        limit = max(1, min(int(limit), MAX_LIST))
        prefix = (rel or "").replace("\\", "/").strip("/")
        like = like_escape(prefix) + "/%" if prefix else "%"
        rows = self.conn.execute(
            """SELECT rel_path, title, src_size FROM documents
               WHERE rowid IN (SELECT rowid FROM documents
                               WHERE rel_path LIKE ? ESCAPE '\\'
                                 AND instr(substr(rel_path, ?), '/') = 0)
               ORDER BY title LIMIT ?""",
            (like, len(prefix) + 2 if prefix else 1, limit),
        ).fetchall()
        return [{"path": r[0], "title": r[1], "size": r[2]} for r in rows]

    def tables(self, rel_path: str, max_bytes: int = 300_000) -> list[dict] | None:
        """返回该文档内的全部表格（GFM + HTML）。路径必须存在于索引，否则 None。"""
        rel = (rel_path or "").replace("\\", "/").strip().lstrip("/")
        if not rel:
            return None
        if not self.conn.execute("SELECT 1 FROM documents WHERE rel_path = ?", (rel,)).fetchone():
            return None
        path = Path(self.md_root).joinpath(*rel.split("/"))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = self.get_content(rel)
            if content is None:
                return None
            text = content
        if text.startswith("---"):
            lines = text.split("\n")
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    text = "\n".join(lines[i + 1 :])
                    break
        return self._extract_tables(text, max_bytes)

    def _extract_tables(self, text: str, max_bytes: int) -> list[dict]:
        found: list[dict] = []
        for m in self._HTML_TABLE_RE.finditer(text):
            found.append({"kind": "html", "context": "", "markdown": m.group(0)})

        lines = text.split("\n")
        i, context = 0, ""
        while i < len(lines):
            line = lines[i]
            if line.strip().startswith("#"):
                context = line.strip()
            if "|" in line and i + 1 < len(lines) and self._SEP_RE.match(lines[i + 1]):
                block = [line, lines[i + 1]]
                j = i + 2
                while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                    block.append(lines[j])
                    j += 1
                found.append({"kind": "gfm", "context": context, "markdown": "\n".join(block)})
                i = j
                continue
            i += 1

        out: list[dict] = []
        total = 0
        for table in found:
            if total + len(table["markdown"]) > max_bytes:
                table["markdown"] = table["markdown"][: max(0, max_bytes - total)]
                table["truncated"] = True
                out.append(table)
                break
            out.append(table)
            total += len(table["markdown"])
        for index, table in enumerate(out, 1):
            table["index"] = index
        return out
