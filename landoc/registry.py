"""登记中心（registry）：让同事们互相发现对方的资料库。

设计 §7：
- 默认端口 8767（库端服务从 8766 起顺延时会跳过它）；
- 内存字典 + 落盘 `registry.json`（原子写），条目
  `{name, ip, port, lib_name, doc_count, version, last_seen, status}`；
- `POST /register` 心跳（库软件 60 s 一次）→ 返回中心时间与是否需要升级；
- `GET /libraries` 在线清单（5 分钟内有心跳 = 在线；30 分钟无心跳 = 移除）；
- `GET /health` 中心自身状态。

设计 §9（分发）：
- `GET /` 给同事一页说明：在线库清单 + 两个下载入口；
- `GET /download` 流式发库软件 zip（`--package-zip` 指定，默认在 exe 目录/仓库 distribution/ 找）；
- `GET /skill.zip` 现场生成客户端 skill 包，里面带写好本中心地址的 `scripts/config.json`，
  同事解压到自己的 skills 目录就能用，不用手填地址；
- `/api/health` 与 `/libraries` 带 version / latest_version，客户端据此提示升级。

名单分发（设计 §9 补充）：
- 库主人维护的授权名单可以发布到中心：`POST /roster`（存成 `roster.csv`），
  其他同事的库 `GET /roster.csv` 拉取后合并/替换——这样"全部门名单只有一份，
  库主人改完，大家拉一次就同步了"；
- **发布只允许中心所在机器**（客户端 IP 是本机地址才放行），否则任何人都能改名单；
  拉取对所有允许网段开放（名单就是姓名 + 内网 IP，本身就是给同事用的）。

安全：只接受允许网段来源；**IP 一律取连接对端，不信客户端自报**；
不做鉴权（内容只有元数据，泄露无害）。
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from landoc import __version__
from landoc.roster import (
    MAX_ROSTER_BYTES,
    ROSTER_NAME,
    decode_roster,
    format_roster,
    parse_roster,
)
from landoc.server import decode_param, parse_query

DEFAULT_PORT = 8767
DEFAULT_CIDRS = ["10.91.0.0/16"]
ONLINE_TTL = 300.0  # 5 分钟内有心跳 = 在线
FORGET_TTL = 1800.0  # 30 分钟没心跳 = 从清单里移除
MAX_BODY = 64 * 1024
LATEST_VERSION = __version__
SKILL_DIR_NAME = "lan-lib-search"
PACKAGE_NAME = "LanDocShare.zip"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def skill_root() -> Path:
    """skill 模板目录：源码在仓库里，打包后在 _MEIPASS/skill。"""
    frozen = getattr(sys, "frozen", False)
    base = Path(getattr(sys, "_MEIPASS", "")) if frozen else _repo_root()
    return base / "skill" / SKILL_DIR_NAME


def default_package_zip() -> Path | None:
    """库软件 zip 的候选位置：exe 同目录 → exe 上一级（onedir 的旁边）→ 仓库 distribution/。"""
    env = os.environ.get("LANDOC_PACKAGE_ZIP")
    if env:
        return Path(env)
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        exe_dir / PACKAGE_NAME,
        exe_dir.parent / PACKAGE_NAME,
        _repo_root() / "distribution" / PACKAGE_NAME,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def build_skill_zip(registry_url: str) -> bytes:
    """把 skill 模板打成一个 zip，附上写好中心地址的 config.json。"""
    root = skill_root()
    if not root.is_dir():
        raise FileNotFoundError(f"skill 模板不存在：{root}")
    import io

    buffer = io.BytesIO()
    config = json.dumps({"registry": registry_url.rstrip("/")}, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            arcname = f"{SKILL_DIR_NAME}/{path.relative_to(root).as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
        info = zipfile.ZipInfo(f"{SKILL_DIR_NAME}/scripts/config.json", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, config)
    return buffer.getvalue()


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


def _local_ips() -> frozenset[str]:
    """本机所有地址：名单发布只认这些来源（含回环）。"""
    found = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            address = info[4][0]
            if address:
                found.add(address)
    except OSError:
        pass
    return frozenset(found)


class RegistryHandler(BaseHTTPRequestHandler):
    store: RegistryStore = None  # type: ignore[assignment]
    allow_cidrs: list[str] = DEFAULT_CIDRS
    package_zip: Path | None = None
    advertise: str = ""
    roster_path: Path | None = None
    roster_admin_ips: frozenset[str] = frozenset({"127.0.0.1", "::1"})
    server_version = "landoc-registry/0.1"

    def log_message(self, *args):  # 不刷 stderr，审计靠库端日志
        pass

    # ---- 响应 ----
    def _send(self, code: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _html(self, markup: str, code: int = 200) -> None:
        self._send(code, markup.encode("utf-8"), "text/html; charset=utf-8")

    def _send_file(self, path: Path) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, 256 * 1024)

    def _client_ip(self) -> str:
        return self.client_address[0]

    def _base_url(self) -> str:
        """同事该访问的中心地址：优先 --advertise，否则用请求的 Host。"""
        if self.advertise:
            return self.advertise.rstrip("/")
        host = (self.headers.get("Host") or "").strip()
        if host:
            return f"http://{host}"
        return f"http://{self._client_ip()}:{self.server.server_address[1]}"

    # ---- 名单 ----
    def _roster_state(self) -> dict | None:
        """已发布名单的 (人数, 更新时间)；没发布或读不出来返回 None。"""
        path = self.roster_path
        if path is None or not path.is_file():
            return None
        try:
            people, _warnings = parse_roster(path.read_text(encoding="utf-8-sig"))
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        except (OSError, ValueError):
            return None
        return {"count": len(people), "updated": stamp}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        parsed = urllib.parse.urlparse(self.path)
        route, _ = parsed.path, parse_query(parsed.query)
        ip = self._client_ip()
        if not _cidr_allowed(ip, self.allow_cidrs):
            return self._error(403, "cidr not allowed")
        if route == "/":
            return self._html(self._landing_html())
        if route == "/api/info":
            return self._json(
                {
                    "service": "landoc-registry",
                    "version": __version__,
                    "latest_version": LATEST_VERSION,
                    "libraries": self.store.count(),
                    "usage": [
                        "GET /  说明页与下载入口",
                        "GET /libraries",
                        "GET /health",
                        "GET /skill.zip  客户端 skill 包（带本中心地址）",
                        "GET /download  库软件 zip",
                        "GET /roster.csv  部门授权名单（发布后才有）",
                        "POST /register",
                        "POST /roster  发布名单（仅中心所在机器）",
                    ],
                    "skill_available": skill_root().is_dir(),
                    "package_available": bool(self.package_zip and self.package_zip.is_file()),
                    "roster_available": self._roster_state() is not None,
                    "roster_count": (self._roster_state() or {}).get("count", 0),
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
        if route == "/skill.zip":
            try:
                blob = build_skill_zip(self._base_url())
            except OSError as exc:
                return self._error(500, f"skill 打包失败：{exc}")
            return self._send(
                200,
                blob,
                "application/zip",
                {"Content-Disposition": 'attachment; filename="lan-lib-search.zip"'},
            )
        if route == "/download":
            path = self.package_zip
            if path is None or not path.is_file():
                return self._json(
                    {
                        "error": "package not available",
                        "hint": f"库主人还没放好库软件包（{PACKAGE_NAME}）；可用 --package-zip 指定路径。",
                    },
                    404,
                )
            return self._send_file(path)
        if route in ("/roster.csv", "/roster"):
            state = self._roster_state()
            if state is None:
                return self._json(
                    {
                        "error": "roster not published",
                        "hint": "库主人还没发布授权名单（库软件里「修改建库设置」→「授权名单」→「发布到中心」）。",
                    },
                    404,
                )
            path = self.roster_path
            assert path is not None
            return self._send(
                200,
                path.read_bytes(),
                "text/csv; charset=utf-8",
                {"Content-Disposition": f'inline; filename="{ROSTER_NAME}"'},
            )
        return self._error(404, "not found")

    # ---- 说明页 ----
    def _landing_html(self) -> str:
        base = self._base_url()
        libs = self.store.libraries()
        rows = []
        for lib in libs:
            state = "在线" if lib["online"] else "离线"
            doc = f"{lib['doc_count']} 篇" if lib["doc_count"] else "—"
            rows.append(
                "<tr>"
                f"<td>{html.escape(lib.get('name') or '未命名')}</td>"
                f"<td>{html.escape(lib.get('url', ''))}</td>"
                f"<td>{doc}</td>"
                f"<td class='{'on' if lib['online'] else 'off'}'>{state}</td>"
                "</tr>"
            )
        table = (
            "<table><thead><tr><th>库主人</th><th>地址</th><th>篇数</th><th>状态</th></tr></thead><tbody>"
            + ("".join(rows) or "<tr><td colspan='4' class='off'>还没有库登记上来</td></tr>")
            + "</tbody></table>"
        )
        package_ok = bool(self.package_zip and self.package_zip.is_file())
        package_block = (
            "<a class='btn' href='/download'>下载库软件（zip）</a>"
            if package_ok
            else "<span class='btn off'>库软件包暂未放好</span>"
        )
        roster_state = self._roster_state()
        roster_block = (
            f"部门授权名单已发布：{roster_state['count']} 人（更新于 {html.escape(roster_state['updated'])}）"
            f" · <a href='/roster.csv'>下载名单</a>"
            if roster_state
            else "部门授权名单还没发布（库主人发布后，各库可一键拉取）"
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LanDocShare · 院内资料库登记中心</title>
<style>
:root{{--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--bg:#f6f8fa;--accent:#1f6feb;}}
@media (prefers-color-scheme:dark){{:root{{--fg:#e6edf3;--muted:#9198a1;--line:#30363d;--bg:#0d1117;--accent:#4493f8;}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:40px 20px;font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:760px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}}
p.lead{{color:var(--muted);margin:0 0 24px}}
.card{{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:16px}}
h2{{font-size:15px;margin:0 0 10px;color:var(--muted);font-weight:600;letter-spacing:.02em}}
.btn{{display:inline-block;padding:8px 14px;border-radius:7px;background:var(--accent);color:#fff;text-decoration:none;font-size:14px}}
.btn.off{{background:var(--line);color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--muted);font-weight:600}}
td.on{{color:#1a7f37}} td.off{{color:var(--muted)}}
code{{background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:1px 5px;font-size:13px}}
ol{{margin:6px 0 0;padding-left:20px}} li{{margin:4px 0}}
footer{{color:var(--muted);font-size:13px;margin-top:22px}}
</style></head><body><main>
<h1>LanDocShare · 院内资料库</h1>
<p class="lead">每人一个资料库，只共享检索片段和表格，不提供原文下载。当前 {len(libs)} 个库，{sum(1 for l in libs if l['online'])} 个在线。</p>

<div class="card"><h2>一、让同事能查到你的资料（库主人）</h2>
<ol>
<li>下载库软件并解压：{package_block}</li>
<li>双击 <code>LanDocShare.exe</code>，跟着向导选一个文件夹、确认共享范围。</li>
<li>登记中心地址已写死在软件里：<code>{html.escape(base)}</code>。启动后 60 秒内会出现在下表。</li>
</ol></div>

<div class="card"><h2>二、查全院资料（普通同事）</h2>
<p>下载 skill 包 <a class="btn" href="/skill.zip">lan-lib-search.zip</a>，解压到自己的 skills 目录；或在 AI 助手里让它装这个 skill。之后直接问“谁那儿有玉田水库的资料”即可。</p>
<p style="color:var(--muted);font-size:13px">包里已写好本中心地址 <code>{html.escape(base)}</code>，不用手工配置；没有 Python 的机器可以按 SKILL.md 里的 curl 办法查。</p></div>

<div class="card"><h2>三、当前在线的库</h2>{table}</div>

<div class="card"><h2>四、部门授权名单</h2>
<p style="margin:0">{roster_block}</p>
<p style="color:var(--muted);font-size:13px;margin:6px 0 0">名单由库主人在库软件里维护并发布；各库可以在「授权名单」窗口一键拉取，改完即时生效。</p></div>

<footer>登记中心 v{html.escape(__version__)} · 5 分钟没心跳算离线 · 只收内网来源</footer>
</main></body></html>
"""

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        ip = self._client_ip()
        if not _cidr_allowed(ip, self.allow_cidrs):
            return self._error(403, "cidr not allowed")
        if parsed.path in ("/roster", "/roster.csv"):
            return self._save_roster(ip)
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

    def _save_roster(self, ip: str) -> None:
        """接收名单：只认中心所在机器，避免任何同事都能改全院名单。"""
        if ip not in self.roster_admin_ips:
            return self._error(403, "只允许登记中心所在的机器发布名单")
        if self.roster_path is None:
            return self._error(500, "没有配置名单存放路径")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._error(400, "bad body")
        if length > MAX_ROSTER_BYTES:
            return self._error(413, "名单太大（超过 1 MB）")
        raw = self.rfile.read(length)
        try:
            people, warnings = parse_roster(decode_roster(raw))
        except ValueError as exc:
            return self._error(400, str(exc))
        if not people:
            return self._error(400, "名单是空的")
        self.roster_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.roster_path.with_suffix(".tmp")
        tmp.write_text(format_roster(people), encoding="utf-8-sig")
        os.replace(tmp, self.roster_path)
        return self._json(
            {
                "ok": True,
                "count": len(people),
                "warnings": warnings,
                "server_time": _now_iso(),
                "latest_version": LATEST_VERSION,
            }
        )


def make_registry_server(
    store: RegistryStore,
    *,
    port: int = 0,
    allow_cidrs=None,
    package_zip: Path | None = None,
    advertise: str = "",
    roster_path: Path | None = None,
    roster_admin_ips=None,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundRegistryHandler",
        (RegistryHandler,),
        {
            "store": store,
            "allow_cidrs": list(allow_cidrs or DEFAULT_CIDRS),
            "package_zip": Path(package_zip) if package_zip else None,
            "advertise": advertise,
            "roster_path": Path(roster_path) if roster_path else None,
            "roster_admin_ips": frozenset(roster_admin_ips) if roster_admin_ips else _local_ips(),
        },
    )
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="landoc-registry", description="LanDocShare 登记中心")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", default=str(Path.home() / ".landoc-registry"))
    parser.add_argument("--allow-cidrs", default=",".join(DEFAULT_CIDRS), help="允许来源网段，逗号分隔")
    parser.add_argument("--package-zip", default=None, help="库软件 zip 的路径（默认在 exe 同目录或 distribution/ 找）")
    parser.add_argument("--advertise", default="", help="对外公布的中心地址，如 http://10.91.1.197:8767（默认用请求 Host）")
    parser.add_argument(
        "--roster",
        default=None,
        help="部门授权名单的存放位置（默认 <state-dir>/roster.csv；库软件发布名单时写这里）",
    )
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
    state_dir = Path(args.state_dir)
    store = RegistryStore(state_dir / "registry.json")
    package = Path(args.package_zip) if args.package_zip else default_package_zip()
    roster_path = Path(args.roster) if args.roster else state_dir / ROSTER_NAME
    httpd = make_registry_server(
        store,
        port=args.port,
        allow_cidrs=cidrs,
        package_zip=package,
        advertise=args.advertise,
        roster_path=roster_path,
    )
    actual = httpd.server_address[1]
    print(f"[OK] 登记中心已启动：http://0.0.0.0:{actual}/ （说明页与下载入口）")
    print(f"     库软件：{package if package and package.is_file() else '未找到（/download 会提示）'}")
    print(f"     skill 包：{'可用' if skill_root().is_dir() else f'找不到模板 {skill_root()}'}")
    print(f"     授权名单：{roster_path}{'' if roster_path.is_file() else '（还没发布）'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
