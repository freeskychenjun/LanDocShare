"""登记中心（registry）：让同事们互相发现对方的资料库。

设计 §7：
- 默认端口 8767（库端服务从 8766 起顺延时会跳过它）；
- 内存字典 + 落盘 `registry.json`（原子写），条目
  `{name, ip, port, lib_name, doc_count, version, last_seen, status}`；
- `POST /register` 心跳（库软件 60 s 一次）→ 返回中心时间与是否需要升级；
- `GET /libraries` 在线清单（5 分钟内有心跳 = 在线；30 分钟无心跳 = 移除）；
- `GET /health` 中心自身状态。

安全：只接受允许网段来源；**IP 一律取连接对端，不信客户端自报**；
不做鉴权（内容只有元数据，泄露无害）。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from landoc import __version__
from landoc.server import decode_param, parse_query

DEFAULT_PORT = 8767
DEFAULT_CIDRS = ["10.91.0.0/16"]
ONLINE_TTL = 300.0  # 5 分钟内有心跳 = 在线
FORGET_TTL = 1800.0  # 30 分钟没心跳 = 从清单里移除
MAX_BODY = 64 * 1024
LATEST_VERSION = __version__


def _now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))


class RegistryStore:
    """在线库清单。所有时间用 epoch 秒存，输出时才转字符串，便于算 TTL。"""

    def __init__(self, state_path: Path | None = None, *, online_ttl: float = ONLINE_TTL, forget_ttl: float = FORGET_TTL):
        self.state_path = Path(state_path) if state_path else None
        self.online_ttl = online_ttl
        self.forget_ttl = forget_ttl
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        if self.state_path is not None:
            self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = time.time()
        for entry in raw.get("libraries", []):
            key = entry.get("key") or f"{entry.get('ip')}:{entry.get('port')}"
            last_seen = float(entry.get("last_seen", 0) or 0)
            if now - last_seen > self.forget_ttl:
                continue
            entry["last_seen"] = last_seen
            self._entries[key] = entry

    def _save(self) -> None:
        if self.state_path is None:
            return
        payload = {"saved_at": time.time(), "libraries": list(self._entries.values())}
        tmp = self.state_path.with_suffix(".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---- 读写 ----
    def register(self, ip: str, payload: dict) -> dict:
        port = int(payload.get("port") or 0)
        if not 0 < port < 65536:
            raise ValueError("port 不合法")
        name = str(payload.get("name") or "").strip()[:40]
        lib_name = str(payload.get("lib_name") or "").strip()[:60]
        key = f"{ip}:{port}"
        entry = {
            "key": key,
            "name": name,
            "ip": ip,
            "port": port,
            "lib_name": lib_name or f"{name or ip}的资料库",
            "doc_count": int(payload.get("doc_count") or 0),
            "version": str(payload.get("version") or ""),
            "last_seen": time.time(),
        }
        with self._lock:
            self._entries[key] = entry
            self._prune_locked()
            self._save()
        return entry

    def _prune_locked(self) -> None:
        now = time.time()
        for key in [k for k, e in self._entries.items() if now - e["last_seen"] > self.forget_ttl]:
            self._entries.pop(key, None)

    def libraries(self, *, prune: bool = True) -> list[dict]:
        now = time.time()
        with self._lock:
            if prune:
                self._prune_locked()
                self._save()
            entries = list(self._entries.values())
        out = []
        for entry in entries:
            age = max(0.0, now - float(entry["last_seen"]))
            out.append(
                {
                    "key": entry["key"],
                    "name": entry.get("name", ""),
                    "ip": entry["ip"],
                    "port": entry["port"],
                    "lib_name": entry.get("lib_name", ""),
                    "doc_count": entry.get("doc_count", 0),
                    "version": entry.get("version", ""),
                    "last_seen": _now_iso(float(entry["last_seen"])),
                    "age_seconds": round(age, 1),
                    "online": age <= self.online_ttl,
                    "url": f"http://{entry['ip']}:{entry['port']}/",
                }
            )
        out.sort(key=lambda item: (not item["online"], item["name"] or item["ip"]))
        return out

    def count(self) -> int:
        with self._lock:
            return len(self._entries)


def _cidr_allowed(ip: str, cidrs) -> bool:
    if not cidrs:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(str(cidr), strict=False):
                return True
        except ValueError:
            continue
    return False


class RegistryHandler(BaseHTTPRequestHandler):
    store: RegistryStore = None  # type: ignore[assignment]
    allow_cidrs: list[str] = DEFAULT_CIDRS
    server_version = "landoc-registry/0.1"

    def log_message(self, *args):  # 不刷 stderr，审计靠库端日志
        pass

    # ---- 响应 ----
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _client_ip(self) -> str:
        return self.client_address[0]

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        parsed = urllib.parse.urlparse(self.path)
        route, _ = parsed.path, parse_query(parsed.query)
        ip = self._client_ip()
        if not _cidr_allowed(ip, self.allow_cidrs):
            return self._error(403, "cidr not allowed")
        if route == "/":
            return self._json(
                {
                    "service": "landoc-registry",
                    "version": __version__,
                    "libraries": self.store.count(),
                    "usage": ["POST /register", "GET /libraries", "GET /health"],
                }
            )
        if route == "/health":
            return self._json(
                {
                    "status": "online",
                    "service": "landoc-registry",
                    "version": __version__,
                    "libraries": self.store.count(),
                    "server_time": _now_iso(),
                }
            )
        if route == "/libraries":
            libs = self.store.libraries()
            return self._json(
                {
                    "count": len(libs),
                    "online": sum(1 for lib in libs if lib["online"]),
                    "online_ttl_seconds": int(self.store.online_ttl),
                    "latest_version": LATEST_VERSION,
                    "server_time": _now_iso(),
                    "libraries": libs,
                }
            )
        return self._error(404, "not found")

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        ip = self._client_ip()
        if not _cidr_allowed(ip, self.allow_cidrs):
            return self._error(403, "cidr not allowed")
        if parsed.path != "/register":
            return self._error(404, "not found")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._error(400, "bad body")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "bad json")
        try:
            entry = self.store.register(ip, payload)
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._json(
            {
                "ok": True,
                "server_time": _now_iso(),
                "key": entry["key"],
                "latest_version": LATEST_VERSION,
                "upgrade": str(payload.get("version") or "") not in ("", LATEST_VERSION),
            }
        )


def make_registry_server(store: RegistryStore, *, port: int = 0, allow_cidrs=None) -> ThreadingHTTPServer:
    handler = type(
        "BoundRegistryHandler",
        (RegistryHandler,),
        {"store": store, "allow_cidrs": list(allow_cidrs or DEFAULT_CIDRS)},
    )
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="landoc-registry", description="LanDocShare 登记中心")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", default=str(Path.home() / ".landoc-registry"))
    parser.add_argument("--allow-cidrs", default=",".join(DEFAULT_CIDRS), help="允许来源网段，逗号分隔")
    parser.add_argument("--install-autostart", action="store_true", help="写开机自启（HKCU Run），然后退出")
    parser.add_argument("--remove-autostart", action="store_true", help="删掉开机自启，然后退出")
    args = parser.parse_args(argv)

    from landoc import autostart

    if args.install_autostart:
        command = autostart.enable_registry(args.port)
        print(f"[OK] 已设开机自启：{command}")
        return 0
    if args.remove_autostart:
        autostart.disable_registry()
        print("[OK] 已关掉登记中心的开机自启")
        return 0

    cidrs = [part.strip() for part in args.allow_cidrs.split(",") if part.strip()]
    store = RegistryStore(Path(args.state_dir) / "registry.json")
    httpd = make_registry_server(store, port=args.port, allow_cidrs=cidrs)
    actual = httpd.server_address[1]
    print(f"[OK] 登记中心已启动：http://0.0.0.0:{actual}/libraries （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
