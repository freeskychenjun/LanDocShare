r"""FTS5 索引：建库、增量写、检索、目录聚合、表格抽取。

SQL 与 `F:\md_share\share_server.py` 保持一致（同一套已验证的实现）：
- FTS5 用独立表 + `trigram` 分词（中文按子串匹配，不依赖分词边界），检索走 `documents_fts MATCH`；
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

from landoc import __version__

FTS_RESERVED = {"and", "or", "not", "near"}
MIN_TRIGRAM_CHARS = 3  # FTS5 的 trigram 分词器要求查询词至少 3 个字符
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
    tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def build_match_query(q: str, op: str = "AND") -> str:
    """把查询串变成 FTS5 MATCH 表达式：按空白切词，每段加引号后以 op 连接。

    索引用的是 trigram 分词器，所以"马斯京根"能命中"马斯京根法河道演算"这种
    分词边界对不上的情况（这正是纯 jieba 分词方案的死穴）。
    """
    terms = [t for t in q.replace('"', " ").split() if t]
    if not terms:
        return ""
    return f" {op} ".join(f'"{t}"' for t in terms)


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


def _safe_md_path(md_root: Path, rel: str) -> Path | None:
    """把索引里的 rel_path 映射成 md 目录下的绝对路径；越界返回 None。

    索引内容由转换器产出，正常不会越界；但 `/api/tables` 的 path 是外部输入，
    只校验"在索引里"挡不住被污染的索引（`..`、盘符、绝对路径），故这里再规范化一次。
    """
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts):
        return None
    try:
        root = Path(md_root).resolve()
        target = root.joinpath(*parts).resolve()
        target.relative_to(root)
    except (OSError, ValueError):  # 路径非法（超长、非法字符）或跳出 md 目录
        return None
    return target


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

        FTS 里存原文，靠 trigram 分词器做子串索引；片段（snippet）由
        `_make_snippet` 从 documents.content 生成，命中词用 <<>> 标出。
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
            (rowid, title, content),
        )

    def delete(self, rel_path: str) -> None:
        """删除一条文档（撤回共享时用）：documents 与 FTS 一起删。"""
        row = self.conn.execute(
            "SELECT rowid FROM documents WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        if not row:
            return
        self.conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (row[0],))
        self.conn.execute("DELETE FROM documents WHERE rowid = ?", (row[0],))

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
        return {"total": total, "service": "landoc", "version": __version__}

    def get_content(self, rel_path: str) -> str | None:
        row = self.conn.execute(
            "SELECT content FROM documents WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        return row[0] if row else None

    def content_length(self, rel_path: str) -> int | None:
        """索引里已存正文的字数；没有这条记录时返回 None。

        转换器用它判断 `store_content=False` 的旧行是否已经收紧（见 `pipeline.run`）。
        """
        row = self.conn.execute(
            "SELECT length(content) FROM documents WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        return row[0] if row else None

    def search(self, q: str, dir_filter: str = "", limit: int = 20):
        """AND 检索，无结果自动降级 OR；返回 (rows, fell_back)。

        2 个字符的查询（"乐平""洪水"这类水文常用词）低于 trigram 的最小长度，
        走 LIKE 子串扫描兜底：个人库只有几千篇，扫一遍代价可接受。
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        terms = [t for t in (q or "").split() if t]
        if not terms:
            return [], False
        if min(len(t) for t in terms) < MIN_TRIGRAM_CHARS:
            rows = self._like_search(q.strip(), dir_filter, limit, op="AND")
            if rows:
                return rows, False
            return self._like_search(q.strip(), dir_filter, limit, op="OR"), True
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
            where += " AND instr(documents.rel_path, ?) = 1"
            params.append(dir_filter.strip("/\\") + "/")
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
                "snippet": _make_snippet(r[4] or "", match, self.snippet_tokens),
            }
            for r in rows
        ]

    def _like_search(self, q: str, dir_filter: str, limit: int, op: str = "AND"):
        """短查询的兜底：逐词子串扫（用 instr，避开 LIKE 的通配符转义）。"""
        terms = [t for t in q.split() if t]
        clauses, params = [], []
        for term in terms:
            clauses.append("(instr(documents.content, ?) > 0 OR instr(documents.title, ?) > 0)")
            params += [term, term]
        joiner = " AND " if op == "AND" else " OR "
        where = joiner.join(clauses) if clauses else "1=0"
        if dir_filter:
            where += " AND instr(documents.rel_path, ?) = 1"
            params.append(dir_filter.strip("/\\") + "/")
        sql = f"""
            SELECT documents.rel_path, documents.title, documents.ext, 0.0 AS score,
                   documents.content AS raw
            FROM documents
            WHERE {where}
            ORDER BY (instr(documents.title, ?) > 0) DESC, length(documents.content)
            LIMIT ?
        """
        params.append(terms[0] if terms else q)
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "path": r[0],
                "title": r[1],
                "ext": r[2],
                "score": round(r[3], 2),
                "snippet": _make_snippet(r[4] or "", build_match_query(q), self.snippet_tokens),
            }
            for r in rows
        ]

    def tree(self, rel: str = "") -> list[dict]:
        prefix = (rel or "").replace("\\", "/").strip("/")
        needle = prefix + "/" if prefix else ""
        rows = self.conn.execute(
            "SELECT rel_path FROM documents WHERE instr(rel_path, ?) = 1", (needle,)
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
        needle = prefix + "/" if prefix else ""
        rows = self.conn.execute(
            """SELECT rel_path, title, src_size FROM documents
               WHERE rowid IN (SELECT rowid FROM documents
                               WHERE instr(rel_path, ?) = 1
                                 AND instr(substr(rel_path, ?), '/') = 0)
               ORDER BY title LIMIT ?""",
            (needle, len(prefix) + 2 if prefix else 1, limit),
        ).fetchall()
        return [{"path": r[0], "title": r[1], "size": r[2]} for r in rows]

    def tables(self, rel_path: str, max_bytes: int = 300_000) -> list[dict] | None:
        """返回该文档内的全部表格（GFM + HTML）。路径必须存在于索引且不越界，否则 None。"""
        rel = (rel_path or "").replace("\\", "/").strip().lstrip("/")
        if not rel:
            return None
        if not self.conn.execute("SELECT 1 FROM documents WHERE rel_path = ?", (rel,)).fetchone():
            return None
        path = _safe_md_path(Path(self.md_root), rel)
        if path is None:  # 索引被污染（含 .. / 盘符）时宁可当作"不在库里"
            return None
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
