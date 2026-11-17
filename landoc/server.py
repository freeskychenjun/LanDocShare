"""只读检索 HTTP 服务（局域网内给同事的 AI 助手用）。

安全边界（设计文档 §6.4/§6.5）：
- 只输出片段与表格，**永不输出全文、下载、source_path**；
- 来源网段用 CIDR 白名单判定（默认 10.91.0.0/16），越权 403；
- 可选"姓名 → IP"名单，姓名缺失/未登记 401；
- 每 IP 滑动窗口限流（默认 60 次/分钟），在认证之前；
- 每次请求写一行 JSONL 审计。
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from landoc import __version__
from landoc.index import MAX_LIMIT, Index

MAX_QUERY_LEN = 200
DEFAULT_RATE = 60


def decode_param(raw: str) -> str:
    """解码单个 query 参数：先 UTF-8，失败按 GBK。

    Git Bash 下 curl 传中文会变成 GBK 字节再被 URL 编码，只按 UTF-8 解会得到
    乱码（典型症状：查询词变成 '???'），故双编码兼容。
    """
    data = urllib.parse.unquote_to_bytes(raw.replace("+", " "))
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse_query(qs: str) -> dict:
    out: dict[str, str] = {}
    for pair in qs.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        out.setdefault(decode_param(key), decode_param(value))
    return out


class RateLimiter:
    """每 IP 滑动窗口限流。"""

    def __init__(self, max_req: int = DEFAULT_RATE, window: float = 60.0):
        self.max = max_req
        self.window = window
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            dq = self._hits.setdefault(ip, deque())
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return False
            dq.append(now)
            if len(self._hits) > 4096:
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window}
            return True


class Audit:
    """JSONL 审计：一行一事件；写失败不阻断服务。"""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, **fields) -> None:
        if not self.path:
            return
        record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **fields}
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _ip_allowed(ip: str, allow_cidrs) -> bool:
    # 空列表 = 暂停共享 = 拒绝所有来源（绝不能反过来变成"全网放行"）
    if not allow_cidrs:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in allow_cidrs:
        try:
            if addr in ipaddress.ip_network(str(cidr), strict=False):
                return True
        except ValueError:
            continue
    return False


class Handler(BaseHTTPRequestHandler):
    index: Index = None  # type: ignore[assignment]
    config = None
    rate: RateLimiter = None  # type: ignore[assignment]
    audit: Audit = None  # type: ignore[assignment]
    server_version = "landoc/0.1"

    def log_message(self, *args):  # 不走 stderr 访问日志，改自写审计
        return

    # ---- 响应 ----
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _record(self, ip: str, name: str, route: str, status: int, **extra) -> None:
        self.audit.log(ip=ip, name=name or "-", api=route, status=status, **extra)

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        parsed = urllib.parse.urlparse(self.path)
        route, params = parsed.path, parse_query(parsed.query)
        ip = self.client_address[0]

        if not self.rate.allow(ip):
            self._record(ip, params.get("name"), route, 429)
            return self._error(429, "rate limit exceeded")

        if not _ip_allowed(ip, getattr(self.config, "allow_cidrs", None)):
            self._record(ip, params.get("name"), route, 403)
            return self._error(403, "cidr not allowed")

        people = getattr(self.config, "allow_people", None) or {}
        if people:
            name = (params.get("name") or "").strip()
            if not name:
                self._record(ip, None, route, 401)
                return self._error(401, "name required")
            if name not in people:
                self._record(ip, name, route, 401)
                return self._error(401, "name not registered")
            expected = (people.get(name) or "").strip()
            if expected and expected != ip:
                self._record(ip, name, route, 401)
                return self._error(401, "ip mismatch for name")

        name = (params.get("name") or "").strip() or None
        try:
            self._dispatch(route, params, ip, name)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可读错误
            self._record(ip, name, route, 500, error=str(exc)[:200])
            self._error(500, f"internal error: {exc.__class__.__name__}")

    def _dispatch(self, route: str, params: dict, ip: str, name: str | None) -> None:
        if route == "/":
            return self._json(self._info_page())
        if route == "/api/health":
            stats = self.index.stats()
            self._record(ip, name, route, 200, count=stats["total"])
            return self._json(
                {
                    "status": "online",
                    "service": "landoc",
                    "version": __version__,
                    "doc_count": stats["total"],
                    "name": getattr(self.config, "name", ""),
                    "last_scan": self.index.get_meta("last_scan", ""),
                }
            )
        if route == "/api/meta":
            self._record(ip, name, route, 200)
            return self._json(
                {
                    "name": getattr(self.config, "name", ""),
                    "service": "landoc",
                    "version": __version__,
                    "doc_count": self.index.stats()["total"],
                    "allow_cidrs": list(getattr(self.config, "allow_cidrs", []) or []),
                    "port": self.server.server_address[1],
                }
            )
        if route == "/api/stats":
            stats = self.index.stats()
            self._record(ip, name, route, 200, count=stats["total"])
            return self._json(stats)
        if route == "/api/search":
            q = (params.get("q") or "").strip()[:MAX_QUERY_LEN]
            if not q:
                self._record(ip, name, route, 400)
                return self._error(400, "q required")
            limit = params.get("limit") or "20"
            try:
                limit_int = max(1, min(int(limit), MAX_LIMIT))
            except ValueError:
                limit_int = 20
            rows, fell_back = self.index.search(q, params.get("dir") or "", limit_int)
            self._record(ip, name, route, 200, count=len(rows), q=q)
            return self._json(
                {"query": q, "count": len(rows), "fallback": fell_back, "results": rows}
            )
        if route == "/api/tree":
            path = params.get("path") or ""
            nodes = self.index.tree(path)
            self._record(ip, name, route, 200, count=len(nodes))
            return self._json({"path": path, "children": nodes})
        if route == "/api/list":
            path = params.get("path") or ""
            rows = self.index.list_dir(path)
            self._record(ip, name, route, 200, count=len(rows))
            return self._json({"path": path, "count": len(rows), "files": rows})
        if route == "/api/tables":
            path = (params.get("path") or "").strip()
            if not path:
                self._record(ip, name, route, 400)
                return self._error(400, "path required")
            tables = self.index.tables(path)
            if tables is None:
                self._record(ip, name, route, 404)
                return self._error(404, "document not in this library")
            self._record(ip, name, route, 200, count=len(tables))
            return self._json({"path": path, "count": len(tables), "tables": tables})
        self._record(ip, name, route, 404)
        return self._error(404, "unknown endpoint")

    def _info_page(self) -> dict:
        return {
            "service": "landoc",
            "status": "online",
            "note": "个人资料库共享检索服务：只提供关键词检索与命中片段/表格，不提供全文或下载。",
            "usage": "同事的 AI 助手通过 lan-lib-search 技能访问；也可手动调用 /api/search?q=关键词",
            "endpoints": {
                "/api/health": "在线状态与文档数",
                "/api/meta": "库名、版本、共享范围",
                "/api/stats": "库统计",
                "/api/search": "q=查询词 & dir=可选目录前缀 & limit=1~30",
                "/api/tree": "path=可选目录，返回子目录聚合",
                "/api/list": "path=目录，返回文件列表（≤500）",
                "/api/tables": "path=文档路径，返回该文档内的表格",
            },
        }


def make_server(cfg, index: Index | None = None, port: int | None = None) -> ThreadingHTTPServer:
    """构造服务实例（不启动）；port 传 0 可让系统分配空闲端口。"""
    # 配置必须显式传下去：Index 的默认值（48 / db 同级的 md）只是碰巧和默认配置一致，
    # 靠默认值会在用户改了 snippet_tokens 后静默不生效。
    index = index or Index(
        Path(cfg.db_path),
        snippet_tokens=getattr(cfg, "snippet_tokens", 48),
        md_root=getattr(cfg, "md_dir", None),
    )
    handler = type("BoundHandler", (Handler,), {})
    handler.index = index
    handler.config = cfg
    handler.rate = RateLimiter()
    handler.audit = Audit(Path(cfg.lib_dir) / "logs" / "access_log.jsonl")
    bind_port = cfg.port if port is None else port
    httpd = ThreadingHTTPServer(("0.0.0.0", bind_port), handler)
    httpd.daemon_threads = True
    return httpd
