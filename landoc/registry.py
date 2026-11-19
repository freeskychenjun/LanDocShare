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
  拉取默认也只允许中心本机；库端要「一键拉取」需中心显式加 `--roster-public`
  （名单就是姓名 + 内网 IP，默认不整段公开）；
- 名单编辑页 `GET /roster/edit`：中心本机浏览器里逐行改姓名/IP/部门，或粘贴 CSV 导入，
  `POST /roster/edit` 保存后 303 回页面（避免刷新重复提交）。同样**只对本机开放**——
  判断依据与 `POST /roster` 一致：来源 IP 必须在 `roster_admin_ips`（默认本机全部地址）里。

安全：只接受允许网段来源；**IP 一律取连接对端，不信客户端自报**；
中心仍不做内容鉴权，但元数据不再对全网段裸奔：库端心跳上报
`allow_cidrs` / `allow_people`，`GET /libraries`、首页和计数只返回
"对当前来源 IP / 姓名开放"的库；没上报授权范围的旧版库一律不显示。
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from landoc import __version__, netinfo
from landoc.ratelimit import RateLimiter
from landoc.roster import (
    MAX_ROSTER_BYTES,
    ROSTER_NAME,
    Roster,
    apply_pull,
    decode_roster,
    format_roster,
    parse_roster,
    sorted_entries,
    validate_entry,
)
from landoc.server import authorize_person, decode_param, parse_query
from landoc.updater import is_newer, parse_version, sha256_file

DEFAULT_PORT = 8767
DEFAULT_CIDRS = ["10.91.0.0/16"]
WATCHDOG_DEFAULT_INTERVAL = 30.0
WATCHDOG_DEFAULT_FAILURES = 3
ONLINE_TTL = 300.0  # 5 分钟内有心跳 = 在线
FORGET_TTL = 1800.0  # 30 分钟没心跳 = 从清单里移除
MAX_BODY = 64 * 1024
MAX_DRAIN_BYTES = 8 * 1024 * 1024  # 拒绝请求时最多先读掉这么多 body，免得客户端写一半被 RST
MAX_AUDIENCE_CIDRS = 64
MAX_AUDIENCE_PEOPLE = 512
MAX_AUDIENCE_NAME = 80
LATEST_VERSION = __version__
REGISTRY_RATE = 120  # 每 IP 每分钟请求上限：一台机器可挂多个库，留足心跳余量
SKILL_DIR_NAME = "lan-lib-search"
PACKAGE_NAME = "LanDocShare.zip"
MANIFEST_NAME = "latest.json"
RELEASE_PLATFORM = "windows-x64"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def skill_root() -> Path:
    """skill 模板目录：源码在仓库里，打包后在 _MEIPASS/skill。"""
    frozen = getattr(sys, "frozen", False)
    base = Path(getattr(sys, "_MEIPASS", "")) if frozen else _repo_root()
    return base / "skill" / SKILL_DIR_NAME


def _publish_dirs() -> list[Path]:
    """发布物的候选目录（按优先级）：exe 同目录 → 上一级 → 上两级的 distribution/。

    打包版放在 `仓库/dist/LanDocShare/`，发布脚本却往 `仓库/distribution/` 写；只看
    exe 同目录/上一级会漏掉真正的发布目录（曾经因此在 dist/ 里放了份过期清单，
    中心就一直报旧版本）。
    """
    exe_dir = Path(sys.executable).resolve().parent
    return [
        exe_dir,
        exe_dir.parent,
        exe_dir.parent.parent / "distribution",
        _repo_root() / "distribution",
    ]


def default_package_zip() -> Path | None:
    """库软件 zip 的候选位置：exe 同目录 → 上一级（onedir 的旁边）→ distribution/。"""
    env = os.environ.get("LANDOC_PACKAGE_ZIP")
    if env:
        return Path(env)
    for folder in _publish_dirs():
        path = folder / PACKAGE_NAME
        if path.is_file():
            return path
    return None


def default_manifest_path(package_zip: Path | None = None) -> Path | None:
    """版本清单（latest.json）在哪：环境变量 → zip 同目录 → exe 同目录/上一级 → distribution/。

    清单是**版本号的唯一真源**：中心进程自己的 `__version__` 只能代表"中心这份程序"，
    打包机升完级、中心没重启时它会落后；`latest.json` 由发布脚本写，重读只按 mtime 判缓存，
    发布后不用重启中心。
    """
    env = os.environ.get("LANDOC_MANIFEST")
    if env:
        return Path(env)
    if package_zip is not None:
        return Path(package_zip).parent / MANIFEST_NAME
    for folder in _publish_dirs():
        candidate = folder / MANIFEST_NAME
        if candidate.is_file():
            return candidate
    return None


_MANIFEST_CACHE: dict[str, tuple[int, dict | None]] = {}
_MANIFEST_LOCK = threading.Lock()


def _normalize_manifest(raw, base_dir: Path) -> dict:
    """校验并补全清单字段；文件/校验和都以磁盘上的包为准，写错了不猜。"""
    if not isinstance(raw, dict):
        raise ValueError("清单必须是 JSON 对象")
    version = str(raw.get("version") or "").strip()
    if parse_version(version) is None:
        raise ValueError(f"version 不是 x.y.z：{version!r}")
    name = str(raw.get("file") or "").strip()
    if not name or name in (".", "..") or any(sep in name for sep in ("/", "\\")):
        raise ValueError("file 只能是分发目录里的文件名")
    path = Path(base_dir) / name
    if not path.is_file():
        raise ValueError(f"清单指向的包不存在：{path}")
    stat = path.stat()
    sha256 = str(raw.get("sha256") or "").strip().lower()
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("sha256 必须是 64 位十六进制")
    if not sha256:  # 手写清单可以省掉，中心自己算（95 MB 约 0.3 s，有缓存）
        sha256 = sha256_file(path)
    notes = raw.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    return {
        "version": version,
        "file": name,
        "path": str(path),
        "sha256": sha256,
        "size": stat.st_size,
        "released_at": str(raw.get("released_at") or ""),
        "notes": [str(item) for item in notes],
        "mandatory": bool(raw.get("mandatory")),
        "min_from": str(raw.get("min_from") or ""),
        "platform": str(raw.get("platform") or RELEASE_PLATFORM),
    }


def load_manifest(path: Path | None) -> dict | None:
    """读 latest.json（按 mtime 缓存）；不存在/写坏了返回 None，不抛异常。"""
    if path is None:
        return None
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = str(Path(path).resolve())
    with _MANIFEST_LOCK:
        cached = _MANIFEST_CACHE.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns:
            return cached[1]
    try:
        # utf-8-sig：有人用记事本/VS Code 存过就会带 BOM，别因此读不出清单
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        manifest: dict | None = _normalize_manifest(raw, Path(path).parent)
    except (OSError, ValueError) as exc:
        print(f"[警告] 版本清单读不出来：{path} -> {exc}", file=sys.stderr)
        manifest = None
    with _MANIFEST_LOCK:
        _MANIFEST_CACHE[key] = (stat.st_mtime_ns, manifest)
    return manifest


_SKILL_ZIP_CACHE: dict[str, tuple[tuple, bytes]] = {}
_SKILL_ZIP_LOCK = threading.Lock()


def _skill_template_signature(root: Path) -> tuple:
    """模板里每个文件的名字+mtime+大小：任何一个改了都会换签名。"""
    items = []
    for path in root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            stat = path.stat()
        except OSError:  # 刚好被删掉/占用：这次签名少一项，下次自然重建
            continue
        items.append((path.relative_to(root).as_posix(), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(items))


def build_skill_zip(registry_url: str) -> bytes:
    """把 skill 模板打成一个 zip，附上写好中心地址的 config.json。

    打包结果是按"模板签名 + 中心地址"缓存的：每份 zip 都一样，没必要每个同事点一次
    就重新读一遍文件、重新压一遍（模板十几个文件，压一次几十毫秒，还会占满请求线程）。
    """
    root = skill_root()
    if not root.is_dir():
        raise FileNotFoundError(f"skill 模板不存在：{root}")
    url = registry_url.rstrip("/")
    signature = _skill_template_signature(root)
    with _SKILL_ZIP_LOCK:
        cached = _SKILL_ZIP_CACHE.get(url)
        if cached is not None and cached[0] == signature:
            return cached[1]
    import io

    buffer = io.BytesIO()
    config = json.dumps({"registry": url}, ensure_ascii=False).encode("utf-8")
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
    blob = buffer.getvalue()
    with _SKILL_ZIP_LOCK:
        _SKILL_ZIP_CACHE[url] = (signature, blob)
    return blob


def _now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def _as_int(value) -> int:
    """query 里的计数参数：解不出来就当 0。"""
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def raw_rows_from_form(fields: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    """把表单里重复出现的 name/ip/dept 三列拼回行（不校验、空行跳过）。

    `urllib.parse.parse_qs` 会把同名字段收成列表，浏览器按行顺序提交，所以
    第 i 个 name 配第 i 个 ip / 第 i 个 dept 就是用户看到的那一行。
    """
    names = list(fields.get("name") or [])
    ips = list(fields.get("ip") or [])
    depts = list(fields.get("dept") or [])
    rows: list[tuple[str, str, str]] = []
    for idx in range(max(len(names), len(ips), len(depts))):
        row = (
            names[idx] if idx < len(names) else "",
            ips[idx] if idx < len(ips) else "",
            depts[idx] if idx < len(depts) else "",
        )
        if not any(cell.strip() for cell in row):
            continue  # 用户点「+ 添加一行」留下的空行，别当错误
        rows.append(row)
    return rows


def roster_from_form(rows: list[tuple[str, str, str]]) -> tuple[Roster, list[str]]:
    """逐行校验并拼成 `Roster`；一次返回全部错误，省得用户改一行提交一次。"""
    people: dict[str, str] = {}
    departments: dict[str, str] = {}
    errors: list[str] = []
    for idx, (name, ip, department) in enumerate(rows, start=1):
        try:
            clean_name, clean_ip, clean_dept = validate_entry(name, ip, department)
        except ValueError as exc:
            errors.append(f"第 {idx} 行：{exc}")
            continue
        if clean_name in people:
            errors.append(f"第 {idx} 行：「{clean_name}」重复了，一个人只写一次")
            continue
        people[clean_name] = clean_ip
        if clean_dept:
            departments[clean_name] = clean_dept
    return Roster(people, departments), errors


_MISSING = object()


def _clean_audience(
    payload: dict,
) -> tuple[list[str] | None, list[str] | None, dict[str, str] | None]:
    """把心跳里的授权范围收紧成可存的形式。

    缺字段 = 旧版库没上报（None，一律不显示）；空列表 = 暂停共享（[]，也不显示）；
    格式不对直接拒绝整次心跳（fail closed），绝不把坏数据当成"不限制"。
    """
    raw_cidrs = payload.get("allow_cidrs", _MISSING)
    cidrs: list[str] | None = None
    if raw_cidrs is not _MISSING:
        cidrs = _clean_cidr_list(raw_cidrs, "allow_cidrs")

    raw_deny = payload.get("deny_cidrs", _MISSING)
    deny: list[str] | None = None
    if raw_deny is not _MISSING:
        deny = _clean_cidr_list(raw_deny, "deny_cidrs")

    raw_people = payload.get("allow_people", _MISSING)
    people: dict[str, str] | None = None
    if raw_people is not _MISSING:
        if not isinstance(raw_people, dict):
            raise ValueError("allow_people 必须是对象")
        if len(raw_people) > MAX_AUDIENCE_PEOPLE:
            raise ValueError(f"allow_people 最多 {MAX_AUDIENCE_PEOPLE} 人")
        cleaned_people: dict[str, str] = {}
        for raw_name, raw_ip in raw_people.items():
            person = str(raw_name).strip()
            if not person or len(person) > MAX_AUDIENCE_NAME:
                raise ValueError("allow_people 含空姓名或过长姓名")
            expected = str(raw_ip or "").strip()
            if expected:
                try:
                    ipaddress.ip_address(expected)
                except ValueError as exc:
                    raise ValueError(f"allow_people 里 {person} 的 IP 不合法") from exc
            cleaned_people[person] = expected
        people = cleaned_people
    return cidrs, deny, people


def _clean_cidr_list(raw, field: str) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{field} 必须是列表")
    if len(raw) > MAX_AUDIENCE_CIDRS:
        raise ValueError(f"{field} 最多 {MAX_AUDIENCE_CIDRS} 项")
    cleaned: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text or len(text) > 64:
            raise ValueError(f"{field} 含空项或过长项")
        try:
            ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"{field} 不合法：{text[:40]}") from exc
        cleaned.append(text)
    return cleaned


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
        allow_cidrs, deny_cidrs, allow_people = _clean_audience(payload)
        key = f"{ip}:{port}"
        entry = {
            "key": key,
            "name": name,
            "ip": ip,
            "port": port,
            "lib_name": lib_name or f"{name or ip}的资料库",
            "doc_count": int(payload.get("doc_count") or 0),
            "version": str(payload.get("version") or ""),
            "allow_cidrs": allow_cidrs,
            "deny_cidrs": deny_cidrs,
            "allow_people": allow_people,
            "last_seen": time.time(),
        }
        with self._lock:
            self._entries[key] = entry
            self._prune_locked()
            self._save()
        return entry

    def _prune_locked(self) -> int:
        """丢掉超过 forget_ttl 没心跳的库，返回真的删掉了几条。"""
        now = time.time()
        stale = [k for k, e in self._entries.items() if now - e["last_seen"] > self.forget_ttl]
        for key in stale:
            self._entries.pop(key, None)
        return len(stale)

    def libraries(
        self, *, prune: bool = True, viewer_ip: str | None = None, viewer_name: str = ""
    ) -> list[dict]:
        """在线清单；给了 viewer_ip 就只返回对该来源开放的库。

        viewer_ip=None 表示"不过滤"（内部/测试用）；对外一律传来源 IP。
        """
        now = time.time()
        with self._lock:
            if prune and self._prune_locked():
                # 只有真删掉了才落盘：说明页每次刷新都 prune 一次，无条件写盘会把
                # 磁盘 IO 变成"每个请求一次"（手机反复刷新首页就能把中心拖卡）。
                self._save()
            entries = list(self._entries.values())
        out = []
        for entry in entries:
            if viewer_ip is not None and not _audience_allows(entry, viewer_ip, viewer_name):
                continue
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
    """网段判定与库端服务共用 `netinfo.cidr_allowed`（"空清单=拒绝"的口径必须一致）。"""
    return netinfo.cidr_allowed(ip, cidrs)


def _audience_allows(entry: dict, viewer_ip: str, viewer_name: str) -> bool:
    """复刻库端的两道门：先看黑名单，再看允许网段，最后看姓名/IP 名单。

    旧版库没上报 allow_cidrs（None）→ 不显示；暂停共享（[]）→ 不显示。
    `authorize_person` 与库端共用，保证"中心看得到"和"库端进得去"口径一致。
    """
    cidrs = entry.get("allow_cidrs")
    if cidrs is None:
        return False
    if _cidr_allowed(viewer_ip, entry.get("deny_cidrs") or []):
        return False
    if not _cidr_allowed(viewer_ip, cidrs):
        return False
    _audit, reason = authorize_person(entry.get("allow_people") or {}, viewer_ip, viewer_name)
    return reason is None


_ROSTER_CACHE: dict[str, tuple[tuple[int, int], dict | None]] = {}
_ROSTER_CACHE_LOCK = threading.Lock()

def _local_ips() -> frozenset[str]:
    """本机所有地址：名单发布只认这些来源（含回环）。

    口径与 `netinfo.local_ips()` 是同一套（"这台机器是不是中心"全项目只该有一个答案），
    这里只保留一个名字，实现交给 netinfo，免得两处各自漂移。
    """
    return netinfo.local_ips()


def _current_local_ips() -> frozenset[str]:
    """`_local_ips()` 的缓存版：每个请求都 getaddrinfo 会拖慢说明页。

    缓存已下沉到 `netinfo.cached_local_ips()`，服务端与中心共用同一份 TTL。
    """
    return netinfo.cached_local_ips()


_ROSTER_PAGE_STYLE = """
:root{--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--bg:#f6f8fa;--card:#fff;--accent:#1f6feb;--ok:#1a7f37;--bad:#cf222e}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--muted:#9198a1;--line:#30363d;--bg:#0d1117;--card:#161b22;--accent:#4493f8;--ok:#3fb950;--bad:#f85149}}
*{box-sizing:border-box}
body{margin:0;padding:36px 20px;font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:860px;margin:0 auto}
h1{font-size:23px;margin:0 0 4px}
p.lead{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600;letter-spacing:.02em}
table{width:100%;border-collapse:collapse;font-size:14px}
th{color:var(--muted);font-weight:600;text-align:left;padding:0 6px 6px;font-size:13px}
td{padding:3px 6px}
td input{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit;font-size:14px}
td input:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:var(--accent)}
td.act{width:38px}
button{font:inherit;cursor:pointer}
.btn{padding:8px 16px;border:0;border-radius:7px;background:var(--accent);color:#fff;font-size:14px}
.btn:hover{filter:brightness(1.08)}
.del{padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--muted);line-height:1}
.del:hover{color:var(--bad);border-color:var(--bad)}
.ghost{padding:8px 14px;border:1px dashed var(--line);border-radius:7px;background:transparent;color:var(--fg);font-size:14px}
.ghost:hover{border-color:var(--accent);color:var(--accent)}
.tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:14px 0 0}
.hint{color:var(--muted);font-size:13px}
.ok,.bad{margin:0 0 14px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);font-size:14px}
.ok{border-color:var(--ok);color:var(--ok)}
.bad{border-color:var(--bad);color:var(--bad)}
details.card summary{cursor:pointer;color:var(--muted);font-weight:600;font-size:14px}
details[open] summary{margin-bottom:12px}
textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,Consolas,monospace;resize:vertical}
textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}
footer{color:var(--muted);font-size:13px;margin-top:22px}
footer a{color:var(--accent);text-decoration:none}
@media (max-width:640px){
#roster,#roster tbody{display:block}
#roster thead{display:none}
#roster tr{display:block;border:1px solid var(--line);border-radius:9px;padding:10px;margin-bottom:10px;background:var(--bg)}
#roster td{display:block;padding:3px 0}
#roster td.act{width:auto;text-align:right}
}
"""

_ROSTER_PAGE_SCRIPT = """
(function () {
  var tbody = document.querySelector('#roster tbody');
  var add = document.getElementById('addrow');
  if (!tbody || !add) return;
  function blank() {
    var tr = document.createElement('tr');
    tr.innerHTML = "<td><input name='name' maxlength='40' placeholder='姓名' autocomplete='off'></td>" +
      "<td><input name='ip' placeholder='内网 IP（必填）' autocomplete='off'></td>" +
      "<td><input name='dept' maxlength='40' placeholder='部门，可留空' autocomplete='off'></td>" +
      "<td class='act'><button type='button' class='del' title='删掉这一行'>✕</button></td>";
    return tr;
  }
  add.addEventListener('click', function () {
    var tr = blank();
    tbody.appendChild(tr);
    tr.querySelector('input').focus();
  });
  tbody.addEventListener('click', function (ev) {
    var btn = ev.target.closest('button.del');
    if (!btn) return;
    var tr = btn.closest('tr');
    if (!tr || !tr.parentNode) return;
    if (tbody.querySelectorAll('tr').length <= 1) {
      tr.querySelectorAll('input').forEach(function (input) { input.value = ''; });
      return;
    }
    tr.parentNode.removeChild(tr);
  });
})();
"""


def _roster_denied_html(port: int = DEFAULT_PORT) -> str:
    """非中心机器打开编辑页时的提示（不含任何名单内容）。"""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LanDocShare · 名单编辑</title>
<style>{_ROSTER_PAGE_STYLE}</style></head><body><main>
<h1>只有登记中心那台机器能编辑名单</h1>
<p class="lead">名单决定谁能查到全院资料，所以只允许在中心本机的浏览器里改，避免被同事误改。</p>
<div class="card">
<p style="margin:0">走到中心那台电脑上，打开 <code>http://127.0.0.1:{int(port)}/roster/edit</code> 就能改。<br>
在这台机器上你可以 <a href="/roster.csv">下载当前名单</a>，或者在库软件的「授权名单」窗口里一键拉取。</p>
</div>
<footer><a href="/">← 回到说明页</a></footer>
</main></body></html>
"""


class RegistryHandler(BaseHTTPRequestHandler):
    store: RegistryStore = None  # type: ignore[assignment]
    allow_cidrs: list[str] = DEFAULT_CIDRS
    package_zip: Path | None = None
    manifest_path: Path | None = None
    advertise: str = ""
    roster_path: Path | None = None
    roster_admin_ips: frozenset[str] = frozenset({"127.0.0.1", "::1"})
    # True = 没显式给名单，认"本机当前地址"；此时每次判权限都现查（60 秒缓存）
    _roster_admin_dynamic: bool = False
    # 每 IP 限流（可选）：只在真正对外开服务时挂上，测试里默认不限
    rate: "RateLimiter | None" = None
    server_version = f"landoc-registry/{__version__}"
    # 响应都带 Content-Length：允许 keep-alive，库端每分钟一次心跳不必重连。
    protocol_version = "HTTP/1.1"
    timeout = 60

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

    def _drain_request_body(self) -> None:
        """先把请求体读掉再回错误。

        这里是 HTTP/1.0：回完响应就关连接。请求体没读完就关，客户端正在写的那一段
        会收到 RST（Windows 上抛 ConnectionAbortedError，浏览器显示「网络错误」），
        明明只是想给它看一张 403 说明页，结果页面都出不来。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        try:
            self.rfile.read(min(length, MAX_DRAIN_BYTES))
        except OSError:  # 对方已经断开也不必再管
            pass

    def _send_file(self, path: Path, *, sha256: str = "", download: bool = True) -> None:
        """流式发 zip，支持 `Range: bytes=N-` 断点续传（同事下到一半断网不用重来）。

        sha256 同时放在 `X-Landoc-Sha256` 和 ETag 里，客户端下完先校验再解压。
        """
        path = Path(path)
        size = path.stat().st_size
        start, end = 0, size - 1
        partial = False
        raw_range = (self.headers.get("Range") or "").strip()
        if raw_range.startswith("bytes="):
            spec = raw_range[len("bytes=") :].split(",")[0].strip()
            try:
                left, _, right = spec.partition("-")
                if left:
                    start = int(left)
                    end = int(right) if right else size - 1
                else:  # bytes=-N 表示最后 N 字节
                    length = int(right)
                    if length <= 0:
                        raise ValueError
                    start, end = max(0, size - length), size - 1
                if start < 0 or start >= size or end < start:
                    raise ValueError
                end = min(end, size - 1)
                partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        disposition = "attachment" if download else "inline"
        self.send_header("Content-Disposition", f'{disposition}; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if sha256:
            self.send_header("X-Landoc-Sha256", sha256)
            self.send_header("ETag", f'"{sha256[:32]}"')
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # ---- 版本清单 ----
    def _manifest(self) -> dict | None:
        return load_manifest(self.manifest_path)

    def _latest(self) -> str:
        manifest = self._manifest()
        return str(manifest["version"]) if manifest else LATEST_VERSION

    def _release_payload(self, current: str = "") -> dict | None:
        manifest = self._manifest()
        if manifest is None:
            return None
        latest = str(manifest["version"])
        update = (not current) or is_newer(latest, current)
        outdated = bool(manifest["min_from"]) and is_newer(str(manifest["min_from"]), current or "0.0.0")
        return {
            "latest": latest,
            "version": latest,
            "update": update and not outdated,
            "manual_only": update and outdated,
            "download": f"{self._base_url()}/download/{latest}",
            "sha256": manifest["sha256"],
            "size": manifest["size"],
            "notes": manifest["notes"],
            "released_at": manifest["released_at"],
            "mandatory": manifest["mandatory"],
            "min_from": manifest["min_from"],
            "platform": manifest["platform"],
            "hint": (
                f"版本跨度太大（最低 {manifest['min_from']}），请重新下载完整包"
                if update and outdated
                else ""
            ),
        }

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
        """已发布名单的 (人数, 更新时间, 部门分布)；没发布或读不出来返回 None。

        按 mtime+size 缓存：说明页/`/api/info` 一次请求会问好几遍，每次重新读盘再把
        上千行 CSV 解析一遍是白花的 IO（名单发布时 mtime 变，缓存自然失效）。
        """
        path = self.roster_path
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None  # 没发布
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        key = str(path)
        with _ROSTER_CACHE_LOCK:
            cached = _ROSTER_CACHE.get(key)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
        state: dict | None
        try:
            roster, _warnings = parse_roster(path.read_text(encoding="utf-8-sig"))
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
            state = {
                "count": len(roster),
                "updated": stamp,
                "departments": roster.department_counts(),
            }
        except (OSError, ValueError):
            state = None
        with _ROSTER_CACHE_LOCK:
            _ROSTER_CACHE[key] = (fingerprint, state)
        return state

    def _roster_rows(self) -> list[tuple[str, str, str]]:
        """已发布名单，按「部门首次出现顺序 → 部门内 IP」排好；没发布就是空列表。"""
        path = self.roster_path
        if path is None or not path.is_file():
            return []
        try:
            book, _warnings = parse_roster(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        return sorted_entries(book)

    def _admin_ips(self) -> frozenset[str]:
        """名单管理权限的地址清单。

        没显式指定时（`roster_admin_ips` 缺省）认"本机当前地址"，而不是启动那一刻的
        快照：笔记本换了网/接了手机热点之后，中心进程往往几天不重启，用快照会让人在
        自己机器上被 403 挡住，而且完全看不出是为什么。
        """
        if self._roster_admin_dynamic:
            return _current_local_ips()
        return self.roster_admin_ips

    def _is_local_request(self) -> bool:
        """这个请求是不是中心这台机器自己发的：来源 IP 必须在本机地址清单里。

        清单默认由 `_local_ips()` 取（回环 + 各网卡地址），也可以用
        `roster_admin_ips` 显式收窄——所以这里只认清单，不做任何"看起来像本机"的猜测。
        """
        return self._client_ip() in self._admin_ips()

    def _roster_download_allowed(self) -> bool:
        """名单能否给这个请求下载：本机始终可以；其他机器要中心显式 --roster-public。"""
        return bool(getattr(self, "roster_public", False)) or self._is_local_request()

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        parsed = urllib.parse.urlparse(self.path)
        route, query = parsed.path, parse_query(parsed.query)
        ip = self._client_ip()
        viewer_name = str(query.get("name") or "").strip()[:MAX_AUDIENCE_NAME]
        if not _cidr_allowed(ip, self.allow_cidrs) or _cidr_allowed(
            ip, getattr(self, "deny_cidrs", [])
        ):
            return self._error(403, "cidr not allowed")
        if self.rate is not None and not self.rate.allow(ip):
            return self._error(429, "rate limit exceeded")
        if route == "/":
            return self._html(self._landing_html(ip))
        if route == "/api/update":
            current = str(query.get("version") or "").strip()
            payload = self._release_payload(current)
            if payload is None:
                return self._json(
                    {
                        "error": "no release manifest",
                        "hint": f"中心还没发布版本清单（分发目录里的 {MANIFEST_NAME}）；用 packaging/release.py 发布后自动生效。",
                        "latest": LATEST_VERSION,
                        "update": False,
                    },
                    404,
                )
            return self._json(payload)
        if route == "/api/info":
            manifest = self._manifest()
            visible = self.store.libraries(viewer_ip=ip, viewer_name=viewer_name)
            return self._json(
                {
                    "service": "landoc-registry",
                    "version": __version__,
                    "latest_version": self._latest(),
                    "libraries": len(visible),
                    "release": (
                        {
                            "version": manifest["version"],
                            "size": manifest["size"],
                            "sha256": manifest["sha256"],
                            "released_at": manifest["released_at"],
                            "notes": manifest["notes"],
                        }
                        if manifest
                        else None
                    ),
                    "usage": [
                        "GET /  说明页与下载入口（可带 ?name=你的姓名）",
                        "GET /libraries?name=你的姓名  只列对你有权限的库",
                        "GET /health",
                        "GET /api/update?version=0.1.0  自动更新检查",
                        "GET /skill.zip  客户端 skill 包（带本中心地址）",
                        "GET /download  库软件 zip（最新版）",
                        "GET /download/<version>  指定版本（支持 Range 断点续传）",
                        "GET /roster.csv  部门授权名单（发布后才有）",
                        "GET /roster/edit  名单编辑页（仅中心所在机器）",
                        "POST /register",
                        "POST /roster  发布名单（仅中心所在机器）",
                        "POST /roster/edit  保存/导入名单（仅中心所在机器）",
                    ],
                    "skill_available": skill_root().is_dir(),
                    "package_available": bool(self.package_zip and self.package_zip.is_file()),
                    "manifest_available": manifest is not None,
                    "roster_available": self._roster_state() is not None,
                    "roster_count": (self._roster_state() or {}).get("count", 0),
                }
            )
        if route == "/health":
            visible = self.store.libraries(viewer_ip=ip, viewer_name=viewer_name)
            return self._json(
                {
                    "status": "online",
                    "service": "landoc-registry",
                    "version": __version__,
                    "latest_version": self._latest(),
                    "libraries": len(visible),
                    "server_time": _now_iso(),
                }
            )
        if route == "/libraries":
            libs = self.store.libraries(viewer_ip=ip, viewer_name=viewer_name)
            return self._json(
                {
                    "count": len(libs),
                    "online": sum(1 for lib in libs if lib["online"]),
                    "online_ttl_seconds": int(self.store.online_ttl),
                    "latest_version": self._latest(),
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
        if route == "/download" or route.startswith("/download/"):
            manifest = self._manifest()
            wanted = urllib.parse.unquote(route[len("/download/") :]).strip() if route != "/download" else ""
            if manifest is not None:
                if wanted and parse_version(wanted) != parse_version(manifest["version"]):
                    return self._json(
                        {
                            "error": "version not available",
                            "hint": f"只保留最新版 v{manifest['version']}；老版本已下架。",
                            "latest": manifest["version"],
                        },
                        404,
                    )
                return self._send_file(
                    Path(manifest["path"]), sha256=str(manifest["sha256"])
                )
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
        if route == "/roster/edit":
            return self._roster_edit(query)
        if route in ("/roster.csv", "/roster"):
            # 名单含全院姓名/IP：默认只让中心本机下载，避免在整个允许网段裸奔。
            # 库端「一键拉取」需要中心显式加 --roster-public 才放开。
            if not self._roster_download_allowed():
                return self._json(
                    {
                        "error": "roster not public",
                        "hint": "名单默认只允许中心本机下载；要让库端拉取，中心启动时加 --roster-public。",
                    },
                    403,
                )
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
    def _landing_html(self, viewer_ip: str) -> str:
        base = self._base_url()
        libs = self.store.libraries(viewer_ip=viewer_ip)
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
            + (
                "".join(rows)
                or "<tr><td colspan='4' class='off'>没有对你开放的库（也可能库主人还没升级到会报告授权范围的版本）</td></tr>"
            )
            + "</tbody></table>"
        )
        package_ok = bool(self.package_zip and self.package_zip.is_file())
        package_block = (
            "<a class='btn' href='/download'>下载库软件（zip）</a>"
            if package_ok
            else "<span class='btn off'>库软件包暂未放好</span>"
        )
        manifest = self._manifest()
        if manifest is None:
            release_block = (
                "<div class='card'><h2>最新版本</h2>"
                "<p style='margin:0'>还没发布版本清单，客户端暂时不会自动升级。</p></div>"
            )
        else:
            notes = "；".join(str(item) for item in manifest["notes"][:4])
            released = manifest["released_at"] or "刚发布"
            release_block = (
                "<div class='card'><h2>最新版本</h2>"
                f"<p style='margin:0'>v{html.escape(manifest['version'])}"
                f"（{manifest['size'] / 1024 / 1024:.0f} MB，{html.escape(released)}）"
                + (
                    f"<br><span style='color:var(--muted);font-size:13px'>{html.escape(notes)}</span>"
                    if notes
                    else ""
                )
                + "</p><p style='color:var(--muted);font-size:13px;margin:6px 0 0'>"
                "已经装了 0.2.0 及以后的版本：右键托盘图标 →「升级到 v…」一键完成（自动下载、校验、重启，失败会自动回滚）。"
                "0.1.0 或更早的机器需要最后手动装一次 v0.2.0。</p></div>"
            )
        roster_state = self._roster_state()
        if roster_state:
            dept_text = "、".join(
                f"{html.escape(name)} {count}" for name, count in roster_state["departments"][:6]
            )
            extra = f"：{dept_text}" if dept_text else ""
            can_download = self._roster_download_allowed()
            link = (
                " · <a href='/roster.csv'>下载名单</a>"
                if can_download
                else " · 名单默认仅中心本机可下载"
            )
            roster_block = (
                f"部门授权名单已发布：{roster_state['count']} 人{extra}"
                f"（更新于 {html.escape(roster_state['updated'])}）"
                f"{link}"
            )
        else:
            roster_block = "部门授权名单还没发布（中心本机发布后本机可下载；各库一键拉取需 --roster-public）"
        if self._is_local_request():
            roster_block += " · <a href='/roster/edit'>编辑名单</a>"
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
<p class="lead">每人一个资料库，只共享检索片段和表格，不提供原文下载。当前对你开放 {len(libs)} 个库，{sum(1 for l in libs if l['online'])} 个在线。只显示你的来源 IP 有权限访问的库；姓名只用于审计，不影响授权。</p>

<div class="card"><h2>一、让同事能查到你的资料（库主人）</h2>
<ol>
<li>下载库软件并解压：{package_block}</li>
<li>双击 <code>LanDocShare.exe</code>，跟着向导选一个文件夹、确认共享范围。</li>
<li>登记中心地址已写死在软件里：<code>{html.escape(base)}</code>。启动后 60 秒内会出现在下表。</li>
</ol></div>

{release_block}

<div class="card"><h2>二、查全院资料（普通同事）</h2>
<p>下载 skill 包 <a class="btn" href="/skill.zip">lan-lib-search.zip</a>，解压到自己的 skills 目录；或在 AI 助手里让它装这个 skill。之后直接问“谁那儿有玉田水库的资料”即可。</p>
<p style="color:var(--muted);font-size:13px">包里已写好本中心地址 <code>{html.escape(base)}</code>，不用手工配置；没有 Python 的机器可以按 SKILL.md 里的 curl 办法查。</p></div>

<div class="card"><h2>三、对你开放的库</h2>{table}</div>

<div class="card"><h2>四、部门授权名单</h2>
<p style="margin:0">{roster_block}</p>
<p style="color:var(--muted);font-size:13px;margin:6px 0 0">名单由库主人在库软件里维护并发布；各库可以在「授权名单」窗口一键拉取，改完即时生效。</p></div>

<footer>登记中心 v{html.escape(__version__)} · 最新版 v{html.escape(self._latest())} · 5 分钟没心跳算离线 · 只收内网来源</footer>
</main></body></html>
"""

    # ---- 名单编辑页（只有中心这台机器能打开）----
    def _redirect(self, location: str) -> None:
        """303 回页面：刷新不会重复提交（POST-Redirect-GET）。"""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _read_form(self) -> tuple[bytes, dict[str, list[str]]]:
        """读 urlencoded 表单；同名字段（每一行的 name/ip/dept）都保留。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ValueError("表单是空的")
        if length > MAX_ROSTER_BYTES:
            raise ValueError("提交的内容太大（超过 1 MB）")
        raw = self.rfile.read(length)
        fields = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        return raw, fields

    def _write_roster(self, book: Roster) -> None:
        """原子写名单（utf-8-sig，Excel 双击打开不乱码）。"""
        assert self.roster_path is not None
        self.roster_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.roster_path.with_suffix(".tmp")
        tmp.write_text(format_roster(book), encoding="utf-8-sig")
        os.replace(tmp, self.roster_path)

    def _roster_edit(self, query: dict) -> None:
        """GET /roster/edit：本机浏览器里的名单编辑器。"""
        if not self._is_local_request():
            return self._html(_roster_denied_html(self.server.server_address[1]), 403)
        saved = _as_int(query.get("saved"))
        warn = _as_int(query.get("warn"))
        notice = ""
        if saved:
            notice = f"已保存：{saved} 人"
            if warn:
                notice += f"（另有 {warn} 条提示：重名以第一次出现的为准）"
        return self._html(self._roster_edit_html(notice=notice))

    @staticmethod
    def _roster_row_html(name: str = "", ip: str = "", department: str = "") -> str:
        return (
            "<tr>"
            f"<td><input name='name' value='{html.escape(name)}' maxlength='40' placeholder='姓名' autocomplete='off'></td>"
            f"<td><input name='ip' value='{html.escape(ip)}' placeholder='内网 IP（必填）' autocomplete='off'></td>"
            f"<td><input name='dept' value='{html.escape(department)}' maxlength='40' placeholder='部门，可留空' autocomplete='off'></td>"
            "<td class='act'><button type='button' class='del' title='删掉这一行'>✕</button></td>"
            "</tr>"
        )

    def _roster_edit_html(
        self,
        rows: list[tuple[str, str, str]] | None = None,
        *,
        error: str = "",
        notice: str = "",
    ) -> str:
        """编辑器页面：rows 为 None 时读当前已发布名单，传了就用传的（校验失败时回填用户输入）。"""
        book = self._roster_rows() if rows is None else list(rows)
        path = self.roster_path
        updated = ""
        if path is not None and path.is_file():
            try:
                updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
            except OSError:
                updated = ""
        departments: list[str] = []
        for _name, _ip, department in book:
            if department and department not in departments:
                departments.append(department)
        stamp = f" · 更新于 {html.escape(updated)}" if updated else " · 还没发布过"
        if error:
            banner = f"<p class='bad'>{html.escape(error)}</p>"
        elif notice:
            banner = f"<p class='ok'>{html.escape(notice)}</p>"
        else:
            banner = ""
        body_rows = "".join(self._roster_row_html(n, i, d) for n, i, d in book)
        if not book:
            body_rows = self._roster_row_html()
        open_attr = "open" if not book else ""
        base = self._base_url()
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LanDocShare · 部门授权名单 · 编辑</title>
<style>{_ROSTER_PAGE_STYLE}</style></head><body><main>
<h1>部门授权名单 · 编辑</h1>
<p class="lead">名单发布在登记中心 <code>{html.escape(base)}</code>，同事的库拉取后按里面的 IP 放行；改完点「保存并发布」，各库下次拉取就同步。这一页只对中心这台机器开放。</p>
{banner}
<div class="card">
<h2>名单：{len(book)} 人 · {len(departments)} 个部门{stamp}</h2>
<form method="post" action="/roster/edit">
<input type="hidden" name="action" value="save">
<table id="roster">
<thead><tr><th>姓名</th><th>IP</th><th>部门</th><th></th></tr></thead>
<tbody>
{body_rows}
</tbody>
</table>
<p class="tools">
<button type="button" id="addrow" class="ghost">+ 添加一行</button>
<button type="submit" class="btn">保存并发布</button>
<span class="hint">姓名、IP 必填；空 IP 不授权（姓名不能当口令）。部门可留空。</span>
</p>
</form>
</div>
<details class="card" {open_attr}>
<summary>从 CSV 粘贴导入</summary>
<form method="post" action="/roster/edit">
<input type="hidden" name="action" value="import">
<textarea name="csv" rows="8" spellcheck="false" placeholder="姓名,IP,部门&#10;张三,10.91.1.23,水文所&#10;李四,10.91.1.24,规划所"></textarea>
<p class="tools">
<label><input type="checkbox" name="replace" value="1" checked> 整体替换现有名单（不勾选 = 按姓名合并）</label>
</p>
<p class="tools">
<button type="submit" class="btn">导入</button>
<span class="hint">每行一个人；表头、空行、# 注释都能容忍，逗号/tab/分号/全角逗号都能当分隔符。</span>
</p>
</form>
</details>
<footer><a href="/">← 回到说明页</a> · <a href="/roster.csv">下载当前名单</a> · 中心 v{html.escape(__version__)}</footer>
</main>
<script>{_ROSTER_PAGE_SCRIPT}</script>
</body></html>
"""

    def _edit_roster(self) -> None:
        """POST /roster/edit：表格保存（action=save）或粘贴导入（action=import）。"""
        if not self._is_local_request():
            self._drain_request_body()
            return self._html(_roster_denied_html(self.server.server_address[1]), 403)
        if self.roster_path is None:
            return self._html(self._roster_edit_html([], error="中心启动时没配置名单存放路径（--roster），改不了名单。"), 500)
        try:
            _raw, fields = self._read_form()
        except ValueError as exc:
            return self._html(self._roster_edit_html([], error=str(exc)), 400)
        action = ((fields.get("action") or ["save"])[0] or "save").strip().lower()
        warnings: list[str] = []
        if action == "import":
            text = (fields.get("csv") or [""])[0]
            replace = ((fields.get("replace") or [""])[0] or "").strip() not in ("", "0")
            try:
                incoming, warnings = parse_roster(text)
            except ValueError as exc:
                return self._html(self._roster_edit_html(error=f"粘贴的名单有问题：{exc}"), 400)
            if not incoming:
                return self._html(
                    self._roster_edit_html(error="粘贴的内容里没解析出人名；格式是每行「姓名,IP,部门」。"),
                    400,
                )
            if replace:
                book = incoming
            else:
                current: Roster = Roster()
                if self.roster_path.is_file():
                    try:
                        current, _warn = parse_roster(self.roster_path.read_text(encoding="utf-8-sig"))
                    except (OSError, ValueError):
                        current = Roster()
                book, _note = apply_pull(current, incoming)
        else:
            rows = raw_rows_from_form(fields)
            if not rows:
                return self._html(self._roster_edit_html([], error="名单是空的：至少留一个人。"), 400)
            book, errors = roster_from_form(rows)
            if errors:
                return self._html(self._roster_edit_html(rows, error="；".join(errors)), 400)
        self._write_roster(book)
        return self._redirect(f"/roster/edit?saved={len(book)}&warn={len(warnings)}")

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        ip = self._client_ip()
        if not _cidr_allowed(ip, self.allow_cidrs) or _cidr_allowed(
            ip, getattr(self, "deny_cidrs", [])
        ):
            self._drain_request_body()
            return self._error(403, "cidr not allowed")
        if self.rate is not None and not self.rate.allow(ip):
            self._drain_request_body()
            return self._error(429, "rate limit exceeded")
        if parsed.path in ("/roster", "/roster.csv"):
            return self._save_roster()
        if parsed.path == "/roster/edit":
            return self._edit_roster()
        if parsed.path != "/register":
            self._drain_request_body()
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
        latest = self._latest()
        current = str(payload.get("version") or "")
        return self._json(
            {
                "ok": True,
                "server_time": _now_iso(),
                "key": entry["key"],
                "latest_version": latest,
                "upgrade": bool(current) and is_newer(latest, current),
            }
        )

    def _save_roster(self) -> None:
        """接收名单：只认中心所在机器，避免任何同事都能改全院名单。"""
        if not self._is_local_request():
            self._drain_request_body()
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
            roster, warnings = parse_roster(decode_roster(raw))
        except ValueError as exc:
            return self._error(400, str(exc))
        if not roster:
            return self._error(400, "名单是空的")
        self._write_roster(roster)
        return self._json(
            {
                "ok": True,
                "count": len(roster),
                "departments": roster.department_counts(),
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
    deny_cidrs=None,
    package_zip: Path | None = None,
    manifest: Path | None = None,
    advertise: str = "",
    roster_path: Path | None = None,
    roster_admin_ips=None,
    roster_public: bool = False,
    rate: "RateLimiter | None" = None,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundRegistryHandler",
        (RegistryHandler,),
        {
            "store": store,
            "allow_cidrs": list(allow_cidrs or DEFAULT_CIDRS),
            "deny_cidrs": list(deny_cidrs or []),
            "package_zip": Path(package_zip) if package_zip else None,
            "manifest_path": Path(manifest) if manifest else None,
            "advertise": advertise,
            "roster_path": Path(roster_path) if roster_path else None,
            "roster_admin_ips": frozenset(roster_admin_ips) if roster_admin_ips else _local_ips(),
            "_roster_admin_dynamic": not roster_admin_ips,
            "roster_public": bool(roster_public),
            "rate": rate,
        },
    )
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    return httpd


STOP_NAME = "stop.request"
STOP_POLL_SECONDS = 2.0


def stop_path(state_dir: Path) -> Path:
    return Path(state_dir) / STOP_NAME


def request_stop(state_dir: Path) -> Path:
    """留个记号，让正在跑的中心自己优雅退出（不杀进程、不再重启）。"""
    path = stop_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")
    return path


def _start_stop_watcher(httpd, state_dir: Path) -> threading.Thread:
    """每 2 秒看一眼 `stop.request`；有就关掉 serve_forever，等 main 收尾。"""
    path = stop_path(state_dir)

    def loop() -> None:
        while True:
            time.sleep(STOP_POLL_SECONDS)
            if not path.exists():
                continue
            try:
                path.unlink()
            except OSError:
                pass
            print("[停止] 收到 --stop 请求，正在关掉对外服务…", flush=True)
            httpd.shutdown()
            return

    thread = threading.Thread(target=loop, name="landoc-registry-stop", daemon=True)
    thread.start()
    return thread


def _autostart_args(args: argparse.Namespace) -> list[str]:
    """把这次启动实际用到的运行参数原样写进自启命令（路径转绝对）。

    开机自启时工作目录不是管理员敲命令的那个目录，相对路径必须在这里定死，
    否则重启后 `--package-zip distribution/...` 会找不到包，下载入口直接 404。
    """
    extra: list[str] = []
    default_state = Path.home() / ".landoc-registry"
    if Path(args.state_dir) != default_state:
        extra += ["--state-dir", str(Path(args.state_dir).resolve())]
    if args.allow_cidrs != ",".join(DEFAULT_CIDRS):
        extra += ["--allow-cidrs", str(args.allow_cidrs)]
    deny = str(getattr(args, "deny_cidrs", "") or "").strip()
    if deny:
        extra += ["--deny-cidrs", deny]
    if getattr(args, "roster_public", False):
        extra += ["--roster-public"]
    for flag, value in (
        ("--package-zip", args.package_zip),
        ("--manifest", args.manifest),
        ("--advertise", args.advertise),
        ("--roster", args.roster),
    ):
        if not value:
            continue
        text = str(value)
        if flag != "--advertise":
            text = str(Path(text).resolve())
        extra += [flag, text]
    # 看门狗自己的参数也写进去：换过日志位置、调过探测间隔的，重启后还得是那一套
    watchdog_log = getattr(args, "watchdog_log", None)
    if watchdog_log:
        extra += ["--watchdog-log", str(Path(watchdog_log).resolve())]
    interval = getattr(args, "watchdog_interval", None)
    if interval is not None and float(interval) != WATCHDOG_DEFAULT_INTERVAL:
        extra += ["--watchdog-interval", f"{float(interval):g}"]
    failures = getattr(args, "watchdog_failures", None)
    if failures is not None and int(failures) != WATCHDOG_DEFAULT_FAILURES:
        extra += ["--watchdog-failures", str(int(failures))]
    return extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="landoc-registry", description="LanDocShare 登记中心")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", default=str(Path.home() / ".landoc-registry"))
    parser.add_argument("--allow-cidrs", default=",".join(DEFAULT_CIDRS), help="允许来源网段，逗号分隔")
    parser.add_argument(
        "--deny-cidrs",
        default="",
        help="显式拒绝来源网段，逗号分隔（优先于 --allow-cidrs；空=不额外拒绝）",
    )
    parser.add_argument("--package-zip", default=None, help="库软件 zip 的路径（默认在 exe 同目录或 distribution/ 找）")
    parser.add_argument(
        "--manifest",
        default=None,
        help=f"版本清单 {MANIFEST_NAME} 的路径（默认与 zip 同目录；发布后按 mtime 重读，不用重启中心）",
    )
    parser.add_argument("--advertise", default="", help="对外公布的中心地址，如 http://10.91.1.197:8767（默认用请求 Host）")
    parser.add_argument(
        "--roster",
        default=None,
        help="部门授权名单的存放位置（默认 <state-dir>/roster.csv；库软件发布名单时写这里）",
    )
    parser.add_argument(
        "--roster-public",
        action="store_true",
        help="允许中心允许网段内的机器 GET /roster.csv（库端一键拉取需要；默认只允许中心本机）",
    )
    parser.add_argument("--install-autostart", action="store_true", help="写开机自启（HKCU Run，带看门狗），然后退出")
    parser.add_argument("--remove-autostart", action="store_true", help="删掉开机自启，然后退出")
    parser.add_argument(
        "--watchdog",
        action="store_true",
        help="常驻看门狗：中心退出/卡死时自动拉起（配合 --install-autostart 开机即守护）",
    )
    parser.add_argument(
        "--watchdog-log",
        default=None,
        help="看门狗与中心子进程的输出写哪（默认 %%LOCALAPPDATA%%\\LanDocShare\\logs\\registry-watchdog.log）",
    )
    parser.add_argument(
        "--watchdog-interval",
        type=float,
        default=WATCHDOG_DEFAULT_INTERVAL,
        help="每隔多少秒探一次 /health；0 = 不探，只管子进程退出",
    )
    parser.add_argument(
        "--watchdog-failures",
        type=int,
        default=WATCHDOG_DEFAULT_FAILURES,
        help="连续多少次 /health 没应答算卡死并重启（默认 3）",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="让正在运行的登记中心优雅退出（等手头请求答完），然后退出；配合看门狗重打包用",
    )
    args = parser.parse_args(argv)

    if args.stop:
        path = request_stop(Path(args.state_dir))
        print(f"[OK] 已请求登记中心停止（{path}）")
        print("     它会在几秒内答完手头的请求再退出；看门狗也会一起退，重打包完再启动一次。")
        return 0

    from landoc import autostart

    if args.install_autostart:
        extra = _autostart_args(args)
        command = autostart.enable_registry(args.port, extra=extra)
        print(f"[OK] 已设开机自启（看门狗守护）：{command}")
        return 0
    if args.remove_autostart:
        autostart.disable_registry()
        print("[OK] 已关掉登记中心的开机自启")
        return 0

    if args.watchdog:
        from landoc import watchdog

        return watchdog.supervise(
            list(sys.argv[1:] if argv is None else argv),
            log_path=Path(args.watchdog_log) if args.watchdog_log else None,
            port=args.port,
            health_interval=args.watchdog_interval,
            health_failures=args.watchdog_failures,
        )

    cidrs = [part.strip() for part in args.allow_cidrs.split(",") if part.strip()]
    deny_cidrs = [part.strip() for part in str(args.deny_cidrs or "").split(",") if part.strip()]
    # 黑名单不能“写错就静默失效”：坏项直接拒绝启动，避免管理员以为挡住了其实没挡。
    try:
        for item in deny_cidrs:
            ipaddress.ip_network(item, strict=False)
    except ValueError as exc:
        print(f"[错误] --deny-cidrs 不合法：{exc}", file=sys.stderr)
        return 2
    state_dir = Path(args.state_dir)
    store = RegistryStore(state_dir / "registry.json")
    package = Path(args.package_zip) if args.package_zip else default_package_zip()
    manifest_path = Path(args.manifest) if args.manifest else default_manifest_path(package)
    manifest = load_manifest(manifest_path)
    roster_path = Path(args.roster) if args.roster else state_dir / ROSTER_NAME
    try:
        httpd = make_registry_server(
            store,
            port=args.port,
            allow_cidrs=cidrs,
            deny_cidrs=deny_cidrs,
            package_zip=package,
            manifest=manifest_path,
            advertise=args.advertise,
            roster_path=roster_path,
            roster_public=args.roster_public,
            rate=RateLimiter(REGISTRY_RATE),
        )
    except OSError as exc:
        from landoc import watchdog

        print(f"[错误] 端口 {args.port} 起不来：{exc}")
        print("       可能已经有一个登记中心，或端口被别的程序占着；换 --port，或先停掉占用者。")
        return watchdog.PORT_BUSY_CODE
    actual = httpd.server_address[1]
    # 上次 --stop 留下的记号不能拿来杀这次刚起的中心
    stop_path(state_dir).unlink(missing_ok=True)
    _start_stop_watcher(httpd, state_dir)
    print(f"[OK] 登记中心已启动：http://0.0.0.0:{actual}/ （说明页与下载入口）")
    print(f"     库软件：{package if package and package.is_file() else '未找到（/download 会提示）'}")
    if manifest is None:
        print(f"     版本清单：未发布（{manifest_path or MANIFEST_NAME} 不存在；自动升级只会提示手动下载）")
    else:
        print(f"     版本清单：v{manifest['version']} · {manifest['size'] / 1024 / 1024:.0f} MB · {manifest['path']}")
    print(f"     skill 包：{'可用' if skill_root().is_dir() else f'找不到模板 {skill_root()}'}")
    print(f"     授权名单：{roster_path}{'' if roster_path.is_file() else '（还没发布）'}")
    print(f"     改名单就在这台机器上开：http://127.0.0.1:{actual}/roster/edit （别人打不开）")
    print(f"     要停下来重打包：registry --stop（{stop_path(state_dir)}），看门狗会一起退")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
