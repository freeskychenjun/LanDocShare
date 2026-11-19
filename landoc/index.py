r"""FTS5 索引：建库、增量写、检索、目录聚合、表格抽取。

SQL 与 `F:\md_share\share_server.py` 保持一致（同一套已验证的实现）：
- FTS5 用 external content 表 + `trigram` 分词（中文按子串匹配，不依赖分词边界），
  检索走 `documents_fts MATCH`；正文只存 `documents` 一份，倒排靠触发器同步；
- `tree` / `list_dir` 只从索引聚合，不读磁盘 —— 被排除的目录对同事完全不可见；
- `list_dir` 必须用 `rowid IN (子查询)` 两步查：直接 `LIKE + ORDER BY` 会全表扫描
  含正文的行（实测可超时 30 s 以上），不要"优化"回单层查询。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from landoc import __version__

FTS_RESERVED = {"and", "or", "not", "near"}
MIN_TRIGRAM_CHARS = 3  # FTS5 的 trigram 分词器要求查询词至少 3 个字符
MAX_LIMIT = 30
MAX_LIST = 500
SHORT_QUERY_CACHE = 64  # 两字查询走全表子串扫描，按查询串缓存最近 64 个结果

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
    indexed_at TEXT,
    withdrawn  INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, content,
    content='documents', content_rowid='rowid',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, content)
        VALUES ('delete', old.rowid, old.title, old.content);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, content)
        VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO documents_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# external content 表的建表语句里带这一句；旧库没有 → 需要重建（见 Index._migrate_fts）
FTS_EXTERNAL_MARK = "content='documents'"


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
        # 两字查询的短缓存：服务端每敲一个字都发一次请求，"洪水""乐平"这类词
        # 每次都要全表 instr 扫描。写路径（upsert/delete/mark_withdrawn）整体失效。
        self._cache_lock = threading.Lock()
        self._short_cache: OrderedDict[tuple, tuple] = OrderedDict()
        with self.conn:
            rebuilt = self._migrate_fts()
            self.conn.executescript(SCHEMA)
            self._migrate_withdrawn()
            if rebuilt:
                self._rebuild_fts()
        if rebuilt:
            # 迁移把旧表 DROP 了，腾出来的页还留在文件里；VACUUM 才真正还磁盘。
            # 库被别的进程占着时会拿到 database is locked —— 跳过即可，不影响读写。
            self.vacuum()

    def _migrate_fts(self) -> bool:
        """把 0.2.x 的「自带正文的 FTS 表」换成 external content 表；返回是否重建过。

        旧版 documents_fts 自己存一份 title/content，1000 篇 3 万字大约多占 130 MB。
        新版让它按 rowid 从 documents 读（正文只存一份），表结构不兼容，只能
        DROP + 重建倒排（rebuild 是 external content 表的专用命令）。
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents_fts'"
        ).fetchone()
        if row is None or FTS_EXTERNAL_MARK in (row[0] or "").lower():
            return False  # 新库，或已经是 external content（触发器交给 SCHEMA 补齐）
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone():
            return False  # 半成品库，让 SCHEMA 照常建
        try:
            self.conn.execute("DROP TABLE documents_fts")
        except sqlite3.OperationalError:
            # 另一个进程正占着这张表（服务端与命令行同时建库）：这次先不迁，
            # 旧表配上新触发器照样能写，下次打开再重试。
            return False
        for name in ("documents_ai", "documents_ad", "documents_au"):
            self.conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        return True

    def _migrate_withdrawn(self) -> None:
        """给旧库补 `withdrawn` 列：撤回共享时只标位，不再物理删 md/索引。"""
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "withdrawn" not in columns:
            self.conn.execute(
                "ALTER TABLE documents ADD COLUMN withdrawn INTEGER NOT NULL DEFAULT 0"
            )

    def _rebuild_fts(self) -> None:
        self.conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
        self.conn.commit()

    def vacuum(self) -> bool:
        """压缩库文件（迁移后回收旧表占的页）。忙时返回 False，不影响正确性。"""
        try:
            self.conn.execute("VACUUM")
        except sqlite3.OperationalError:
            return False
        return True

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
        """写一条文档记录；倒排由 documents 上的触发器同步。

        documents_fts 是 external content 表，正文只存 documents 一份，因此这里
        只写 documents —— `ON CONFLICT DO UPDATE` 会触发 UPDATE 触发器做完删旧/插新。
        旧实现还要手工 `SELECT rowid` + 删插 FTS，一篇三次往返。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO documents (rel_path, title, ext, source_path, src_size, src_mtime, content, indexed_at, withdrawn)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(rel_path) DO UPDATE SET
                 title=excluded.title, ext=excluded.ext, source_path=excluded.source_path,
                 src_size=excluded.src_size, src_mtime=excluded.src_mtime,
                 content=excluded.content, indexed_at=excluded.indexed_at,
                 withdrawn=0""",
            (rel_path, title, ext, source_path, int(src_size), float(src_mtime), content, now),
        )
        self._invalidate_search_cache()

    def mark_withdrawn(self, rel_path: str, withdrawn: bool = True) -> None:
        """撤回/恢复共享：只改可见性标记，md 与索引正文都保留。"""
        self.conn.execute(
            "UPDATE documents SET withdrawn = ? WHERE rel_path = ?",
            (1 if withdrawn else 0, rel_path),
        )
        self._invalidate_search_cache()

    def delete(self, rel_path: str) -> None:
        """删除一条文档（瘦身/显式清理时用）：触发器一并发删 FTS 条目，不碰磁盘文件。"""
        self.conn.execute("DELETE FROM documents WHERE rel_path = ?", (rel_path,))
        self._invalidate_search_cache()

    # ---- 两字查询的短缓存 ----
    def _invalidate_search_cache(self) -> None:
        with self._cache_lock:
            self._short_cache.clear()

    def _cached_search(self, key: tuple):
        with self._cache_lock:
            hit = self._short_cache.get(key)
            if hit is None:
                return None
            self._short_cache.move_to_end(key)  # LRU：刚用过的留到最后淘汰
            return [dict(row) for row in hit[0]], hit[1]

    def _remember_search(self, key: tuple, rows: list, fell_back: bool) -> None:
        with self._cache_lock:
            self._short_cache[key] = (tuple(dict(row) for row in rows), fell_back)
            while len(self._short_cache) > SHORT_QUERY_CACHE:
                self._short_cache.popitem(last=False)

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
    def stats(self, *, include_withdrawn: bool = True) -> dict:
        where = "" if include_withdrawn else " WHERE withdrawn = 0"
        total = self.conn.execute(f"SELECT COUNT(*) FROM documents{where}").fetchone()[0]
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

    def search(self, q: str, dir_filter: str = "", limit: int = 20, *, include_withdrawn: bool = True):
        """AND 检索，无结果自动降级 OR；返回 (rows, fell_back)。

        2 个字符的查询（"乐平""洪水"这类水文常用词）低于 trigram 的最小长度，
        走 LIKE 子串扫描兜底：个人库只有几千篇，扫一遍代价可接受；结果按
        (查询串, 目录, 条数, 可见性) 缓存最近 64 个，写路径一到就整体失效。

        `include_withdrawn=False` 时只返回仍在共享范围内的文档；服务端对非本机
        请求传 False，主人本机保留完整库。
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        terms = [t for t in (q or "").split() if t]
        if not terms:
            return [], False
        if min(len(t) for t in terms) < MIN_TRIGRAM_CHARS:
            key = (q.strip(), dir_filter, limit, include_withdrawn)
            cached = self._cached_search(key)
            if cached is not None:
                return cached
            rows = self._like_search(q.strip(), dir_filter, limit, op="AND", include_withdrawn=include_withdrawn)
            if rows:
                result = (rows, False)
            else:
                result = (
                    self._like_search(q.strip(), dir_filter, limit, op="OR", include_withdrawn=include_withdrawn),
                    True,
                )
            self._remember_search(key, *result)
            return result
        match = build_match_query(q, "AND")
        if not match:
            return [], False
        rows = self._run_query(match, dir_filter, limit, include_withdrawn=include_withdrawn)
        if rows:
            return rows, False
        match_or = build_match_query(q, "OR")
        if match_or == match:
            return [], False
        return self._run_query(match_or, dir_filter, limit, include_withdrawn=include_withdrawn), True

    def _run_query(self, match: str, dir_filter: str, limit: int, *, include_withdrawn: bool = True):
        where = "documents_fts MATCH ?"
        params: list = [match]
        if dir_filter:
            where += " AND instr(documents.rel_path, ?) = 1"
            params.append(dir_filter.strip("/\\") + "/")
        if not include_withdrawn:
            where += " AND documents.withdrawn = 0"
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

    def _like_search(self, q: str, dir_filter: str, limit: int, op: str = "AND", *, include_withdrawn: bool = True):
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
        if not include_withdrawn:
            where += " AND documents.withdrawn = 0"
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

    def tree(self, rel: str = "", *, include_withdrawn: bool = True) -> list[dict]:
        prefix = (rel or "").replace("\\", "/").strip("/")
        needle = prefix + "/" if prefix else ""
        condition = "instr(rel_path, ?) = 1"
        if not include_withdrawn:
            condition += " AND withdrawn = 0"
        rows = self.conn.execute(
            f"SELECT rel_path FROM documents WHERE {condition}", (needle,)
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

    def list_dir(self, rel: str = "", limit: int = MAX_LIST, *, include_withdrawn: bool = True) -> list[dict]:
        limit = max(1, min(int(limit), MAX_LIST))
        prefix = (rel or "").replace("\\", "/").strip("/")
        needle = prefix + "/" if prefix else ""
        condition = "instr(rel_path, ?) = 1 AND instr(substr(rel_path, ?), '/') = 0"
        if not include_withdrawn:
            condition += " AND withdrawn = 0"
        rows = self.conn.execute(
            f"""SELECT rel_path, title, src_size FROM documents
               WHERE rowid IN (SELECT rowid FROM documents
                               WHERE {condition})
               ORDER BY title LIMIT ?""",
            (needle, len(prefix) + 2 if prefix else 1, limit),
        ).fetchall()
        return [{"path": r[0], "title": r[1], "size": r[2]} for r in rows]

    def recent(self, limit: int = 50, *, include_withdrawn: bool = True) -> list[dict]:
        """最近收录的文档（按入库时间倒序）：检索页一打开就能看见库里有东西。

        indexed_at 是文本时间戳（"YYYY-MM-DD HH:MM:SS"），同一秒入库的多篇用 rowid
        兜底排序，保证顺序稳定、不会每次刷新都跳。
        """
        limit = max(1, min(int(limit), MAX_LIST))
        condition = "1=1" if include_withdrawn else "withdrawn = 0"
        rows = self.conn.execute(
            f"""SELECT rel_path, title, src_size FROM documents
               WHERE {condition}
               ORDER BY COALESCE(indexed_at, '') DESC, rowid DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [{"path": r[0], "title": r[1], "size": r[2]} for r in rows]

    def tables(self, rel_path: str, max_bytes: int = 300_000, *, include_withdrawn: bool = True) -> list[dict] | None:
        """返回该文档内的全部表格（GFM + HTML）。路径必须存在于索引且不越界，否则 None。"""
        rel = (rel_path or "").replace("\\", "/").strip().lstrip("/")
        if not rel:
            return None
        condition = "rel_path = ?" if include_withdrawn else "rel_path = ? AND withdrawn = 0"
        if not self.conn.execute(
            f"SELECT 1 FROM documents WHERE {condition}", (rel,)
        ).fetchone():
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
