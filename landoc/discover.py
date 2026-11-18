"""客户端"我能看哪些库"：拉登记中心名单、缓存、逐库探活。

为什么要有它：托盘里的状态窗口只讲"我自己这个库"，同事想知道"全院现在有
哪些库、我能看哪些"以前只能开网页或跑 skill 的 `query.py libs`。这里把同一
份口径搬进客户端：中心已经按"来源 IP + 可选姓名"过滤过，客户端只负责把
结果拿回来、离线时退回缓存、把明显关机的库标灰。

几条刻意保持的规则（和 `skill/lan-lib-search/scripts/query.py` 一致）：

- 姓名只用于"我是谁"，不做任何本地授权判断——名单在中心那一侧算；
- 缓存里记着"用哪个姓名、向哪个中心"查的：换了姓名或换了中心就作废，免得
  看到上一个姓名才有的库；
- 中心连不上时，缓存里标称在线的库要逐个 `/api/health` 探活，免得窗口里
  一排"在线"其实早关机了；
- 纯标准库（urllib + json），单测不需要真的起中心也能跑。
"""

from __future__ import annotations

import html
import json
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from landoc import __version__

CACHE_TTL = 24 * 3600
HTTP_TIMEOUT = 4.0
HEALTH_TIMEOUT = 1.2
MAX_BODY = 512 * 1024
CACHE_VERSION = 1

_USER_AGENT = f"LanDocShare/{__version__} (tray)"


class _FetchError(Exception):
    """网络/协议层失败，消息直接给用户看。"""


# ---------------------------------------------------------------- 路径与身份


def app_data_dir() -> Path:
    """`%LOCALAPPDATA%\\LanDocShare`；测试里改 LOCALAPPDATA 就能换根。"""
    base = os.environ.get("LOCALAPPDATA", "")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "LanDocShare"


def cache_path() -> Path:
    return app_data_dir() / "accessible_libs.json"


def viewer_name_path() -> Path:
    return app_data_dir() / "viewer_name.txt"


def _skill_identity_path() -> Path:
    """skill 那边记的姓名（`lan-lib-search/identity.txt`），没填过就借用。"""
    home = os.environ.get("LANLIB_HOME", "")
    root = Path(home) if home else app_data_dir().parent / "lan-lib-search"
    return root / "identity.txt"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def load_viewer_name() -> str:
    """本窗口记住的姓名；没有就借用 skill 里填过的那个。"""
    return _read_text(viewer_name_path()) or _read_text(_skill_identity_path())


def save_viewer_name(name: str) -> None:
    path = viewer_name_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (name or "").strip() + "\n")


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class VisibleLibrary:
    key: str
    name: str  # 库主人
    ip: str
    port: int
    lib_name: str  # 库名（对方建库时起的）
    doc_count: int
    version: str
    last_seen: str
    age_seconds: float | None
    online: bool
    url: str

    @property
    def owner(self) -> str:
        return self.name or self.ip

    @property
    def title(self) -> str:
        return self.lib_name or self.name or self.ip

    def heartbeat_text(self) -> str:
        if not self.online:
            return "已离线"
        if self.age_seconds is None:
            return "在线"
        return f"心跳 {_human_age(self.age_seconds)}"


@dataclass(frozen=True)
class LibraryList:
    """一次查询的完整结果；窗口只读它，不再碰网络。"""

    libraries: tuple[VisibleLibrary, ...] = ()
    source: str = "none"  # registry | cache | none
    viewer: str = ""
    latest_version: str = ""
    error: str = ""
    fetched_at: float = 0.0
    cached_at: float = 0.0

    @property
    def online_count(self) -> int:
        return sum(1 for lib in self.libraries if lib.online)

    def __len__(self) -> int:
        return len(self.libraries)

    def __iter__(self):
        return iter(self.libraries)


@dataclass(frozen=True)
class VisibleRow:
    """表格里的一行；`note` 只用于标记"就是本机这个库"。"""

    key: str
    owner: str
    lib_name: str
    doc_count: int
    status: str
    heartbeat: str
    url: str
    note: str = ""


# ---------------------------------------------------------------- 格式化


def _human_age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时前"
    return f"{seconds / 86400:.1f} 天前"


def _clock(timestamp: float) -> str:
    if not timestamp:
        return "刚才"
    return time.strftime("%H:%M", time.localtime(timestamp))


def _stamp(timestamp: float) -> str:
    if not timestamp:
        return "上次"
    return time.strftime("%m-%d %H:%M", time.localtime(timestamp))


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(value or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


# ---------------------------------------------------------------- HTTP


def _normalize_url(value: str) -> str:
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


def _fetch(registry: str, viewer: str, *, timeout: float) -> dict:
    url = registry + "/libraries"
    if viewer:
        url += "?name=" + quote(viewer, safe="")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - 地址来自用户配置
            body = response.read(MAX_BODY + 1)
    except HTTPError as exc:
        raise _FetchError(_describe_http(exc)) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise _FetchError(_describe_network(exc)) from exc
    if len(body) > MAX_BODY:
        raise _FetchError("登记中心的回包过大，地址可能填错了")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _FetchError("登记中心的回包不是 JSON，地址可能填错了") from exc
    if not isinstance(payload, dict):
        raise _FetchError("登记中心的回包格式不对")
    if not isinstance(payload.get("libraries"), list):
        raise _FetchError("登记中心的回包缺少 libraries 字段")
    return payload


def _describe_http(exc: HTTPError) -> str:
    detail = ""
    try:
        raw = exc.read(2048)
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                detail = str(parsed.get("error") or "")
            else:
                detail = ""
        except (UnicodeDecodeError, ValueError):
            detail = raw.decode("utf-8", "replace").strip()[:120]
    except Exception:  # noqa: BLE001 - 读错误体只是锦上添花
        detail = ""
    if exc.code == 403:
        hint = "（这台机器不在中心的允许网段里；本机自测请用局域网地址，别用 127.0.0.1）"
    elif exc.code == 404:
        hint = "（这个地址不是登记中心）"
    else:
        hint = ""
    return f"登记中心返回 HTTP {exc.code}{hint}" + (f"：{detail}" if detail else "")


def _describe_network(exc: object) -> str:
    reason = getattr(exc, "reason", None) or exc
    text = str(reason) or type(reason).__name__
    return f"连不上登记中心（{text}）"


def _health_ok(url: str, *, timeout: float = HEALTH_TIMEOUT) -> bool:
    base = url if url.endswith("/") else url + "/"
    target = urljoin(base, "api/health")
    try:
        request = Request(target, headers={"User-Agent": _USER_AGENT})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return 200 <= int(getattr(response, "status", 200)) < 300
    except (HTTPError, URLError, OSError, TimeoutError, ValueError):
        return False


# ---------------------------------------------------------------- 缓存


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _visible(item: dict) -> VisibleLibrary | None:
    ip = str(item.get("ip") or "").strip()
    port = _as_int(item.get("port"))
    url = str(item.get("url") or "").strip()
    if not url and ip and 0 < port < 65536:
        url = f"http://{ip}:{port}/"
    if not url:
        return None
    if not url.endswith("/"):
        url += "/"
    return VisibleLibrary(
        key=str(item.get("key") or f"{ip}:{port}"),
        name=str(item.get("name") or ""),
        ip=ip,
        port=port,
        lib_name=str(item.get("lib_name") or ""),
        doc_count=_as_int(item.get("doc_count")),
        version=str(item.get("version") or ""),
        last_seen=str(item.get("last_seen") or ""),
        age_seconds=_as_float(item.get("age_seconds")),
        online=bool(item.get("online")),
        url=url,
    )


@dataclass(frozen=True)
class _Cached:
    libraries: tuple[VisibleLibrary, ...]
    saved_at: float
    latest_version: str


def _read_cache(
    path: Path, *, viewer: str, registry: str, now: float
) -> tuple[_Cached | None, str]:
    """读缓存；返回 `(缓存, 不能用的原因)`。文件不存在时原因为空。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, ""
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None, "本地缓存文件坏了"
    if not isinstance(data, dict):
        return None, "本地缓存文件坏了"
    if _as_int(data.get("version")) != CACHE_VERSION:
        return None, ""
    if str(data.get("viewer") or "") != viewer:
        return None, "本地缓存是另一个姓名查的"
    if _normalize_url(str(data.get("registry") or "")) != registry:
        return None, "本地缓存来自另一个登记中心"
    saved_at = _as_float(data.get("saved_at"))
    if saved_at is None:
        return None, "本地缓存文件坏了"
    if now - saved_at > CACHE_TTL:
        return None, "本地缓存已超过 24 小时"
    libraries: list[VisibleLibrary] = []
    for item in data.get("libraries") or []:
        if isinstance(item, dict):
            lib = _visible(item)
            if lib is not None:
                libraries.append(lib)
    return _Cached(tuple(libraries), saved_at, str(data.get("latest_version") or "")), ""


def _write_cache(
    path: Path,
    *,
    viewer: str,
    registry: str,
    latest: str,
    libraries: tuple[VisibleLibrary, ...],
    now: float,
) -> None:
    payload = {
        "version": CACHE_VERSION,
        "viewer": viewer,
        "registry": registry,
        "saved_at": now,
        "latest_version": latest,
        "libraries": [
            {
                "key": lib.key,
                "name": lib.name,
                "ip": lib.ip,
                "port": lib.port,
                "lib_name": lib.lib_name,
                "doc_count": lib.doc_count,
                "version": lib.version,
                "last_seen": lib.last_seen,
                "age_seconds": lib.age_seconds,
                "online": lib.online,
                "url": lib.url,
            }
            for lib in libraries
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1))
    except OSError:
        pass  # 缓存写不了不影响这次查询


def _backup_offline(result: LibraryList, target: Path, *, generated_at: float) -> None:
    """查到一次新名单就顺手留一份离线网页：中心哪天关了，这就是同事手里的清单。"""
    try:
        export_offline(result, target, generated_at=generated_at)
    except Exception:  # noqa: BLE001 - 备份是顺手的事，绝不能把查询搞挂
        pass


# ---------------------------------------------------------------- 主入口


def fetch_libraries(
    registry_url: str,
    viewer: str = "",
    *,
    cache_file: Path | None = None,
    timeout: float = HTTP_TIMEOUT,
    now: float | None = None,
) -> LibraryList:
    """查一次"对我开放的库"；失败时退缓存，缓存里在线的库再探一次活。"""
    clock = time.time() if now is None else float(now)
    viewer = (viewer or "").strip()
    registry = _normalize_url(registry_url)
    path = Path(cache_file) if cache_file is not None else cache_path()
    if not registry:
        return LibraryList(
            source="none",
            viewer=viewer,
            error="还没有配置登记中心地址（config.json 里的 registry_url），不知道去哪里查库。",
            fetched_at=clock,
        )

    try:
        payload = _fetch(registry, viewer, timeout=timeout)
    except _FetchError as exc:
        cached, reason = _read_cache(path, viewer=viewer, registry=registry, now=clock)
        if cached is None:
            error = str(exc)
            if reason:
                error += f"；{reason}"
            return LibraryList(
                source="none", viewer=viewer, error=error, fetched_at=clock
            )
        checked: list[VisibleLibrary] = []
        for lib in cached.libraries:
            online = lib.online and _health_ok(lib.url)
            age = lib.age_seconds
            if age is not None:
                age = max(0.0, age + max(0.0, clock - cached.saved_at))
            checked.append(replace(lib, online=online, age_seconds=age))
        return LibraryList(
            libraries=tuple(checked),
            source="cache",
            viewer=viewer,
            latest_version=cached.latest_version,
            error=str(exc),
            fetched_at=clock,
            cached_at=cached.saved_at,
        )

    libraries = tuple(
        lib
        for item in payload["libraries"]
        if isinstance(item, dict) and (lib := _visible(item)) is not None
    )
    latest = str(payload.get("latest_version") or "")
    _write_cache(
        path, viewer=viewer, registry=registry, latest=latest, libraries=libraries, now=clock
    )
    result = LibraryList(
        libraries=libraries,
        source="registry",
        viewer=viewer,
        latest_version=latest,
        fetched_at=clock,
        cached_at=clock,
    )
    # 离线清单跟着缓存走：默认落到 app_data_dir()/库地址清单.html
    _backup_offline(result, path.parent / OFFLINE_NAME, generated_at=clock)
    return result


# ---------------------------------------------------------------- 给界面用的纯函数


def rows(result: LibraryList, *, self_name: str = "") -> tuple[VisibleRow, ...]:
    """排序后的一行行；在线的排前面，自己的库标"（本机）"。"""
    ordered = sorted(
        result.libraries, key=lambda lib: (not lib.online, (lib.name or lib.ip or "").lower())
    )
    out: list[VisibleRow] = []
    for lib in ordered:
        mine = bool(self_name) and self_name in {lib.lib_name, lib.name}
        out.append(
            VisibleRow(
                key=lib.key,
                owner=lib.owner,
                lib_name=lib.title,
                doc_count=lib.doc_count,
                status="在线" if lib.online else "已离线",
                heartbeat=lib.heartbeat_text(),
                url=lib.url,
                note="（本机）" if mine else "",
            )
        )
    return tuple(out)


def summary_text(result: LibraryList) -> str:
    total = len(result)
    online = result.online_count
    if result.source == "registry":
        who = "你" if result.viewer else "这台机器"
        return f"对{who}开放 {total} 个库，{online} 个在线 · {_clock(result.fetched_at)} 刚从登记中心取到"
    if result.source == "cache":
        return (
            f"显示 {_stamp(result.cached_at)} 的缓存："
            f"{total} 个库，{online} 个在线（已逐个探活）"
        )
    return result.error or "没有取到库名单"


def empty_hint(result: LibraryList, *, viewer: str = "") -> str:
    """一个库都没有时，别只留一张空表，告诉用户可能是哪种情况。"""
    if result.libraries:
        return ""
    if result.source == "none":
        return result.error or "没有取到库名单"
    text = (
        "中心没有返回对你开放的库：可能你不在对方的授权名单里、对方暂停了共享，"
        "或对方的版本较旧、还没上报授权范围。"
    )
    if not viewer:
        text += " 姓名留空时中心只按本机 IP 匹配，可以填上姓名再查一次。"
    return text


def upgrade_note(result: LibraryList) -> str:
    latest = result.latest_version
    if not latest:
        return ""
    stale = [
        lib.title
        for lib in result.libraries
        if lib.version and _version_tuple(lib.version) < _version_tuple(latest)
    ]
    if not stale:
        return ""
    shown = "、".join(stale[:4])
    more = " 等" if len(stale) > 4 else ""
    return f"这些库还是旧版本，对方升级后才看得到完整内容：{shown}{more}"


# ---------------------------------------------------------------- 离线备份


OFFLINE_NAME = "库地址清单.html"

_OFFLINE_CSS = """
body{font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;margin:0;background:#f6f7f9;color:#222}
main{max-width:880px;margin:0 auto;padding:28px 20px 48px}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:15px;margin:26px 0 10px;color:#333}
.meta{color:#666;font-size:13px;line-height:1.7;margin-bottom:16px}
.warn{background:#fff7e6;border:1px solid #f0d9a8;border-radius:8px;padding:10px 14px;
      font-size:13px;color:#7a4d00;line-height:1.7;margin:0 0 18px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e3e6ea}
th,td{padding:8px 10px;border-bottom:1px solid #eef0f2;text-align:left;font-size:14px;
      vertical-align:top}
th{background:#fafbfc;color:#555;font-weight:600;font-size:13px}
tr.offline td{color:#999}
td.num{text-align:right}
a{color:#1a5fb4}
ul.plain{background:#fff;border:1px solid #e3e6ea;border-radius:8px;
         padding:12px 16px 12px 34px;font-size:13px;color:#444;line-height:1.9}
footer{margin-top:24px;color:#999;font-size:12px;line-height:1.8}
"""


def offline_path() -> Path:
    """默认导出位置：跟缓存、姓名放在一起，同事在自己的机器上导自己的那一份。"""
    return app_data_dir() / OFFLINE_NAME


def _source_label(result: LibraryList) -> str:
    if result.source == "registry":
        return f"刚从登记中心取到（{_stamp(result.fetched_at)}）"
    if result.source == "cache":
        return f"本机缓存（{_stamp(result.cached_at)}，已逐个探活）"
    return "没有取到名单，下面是上一次留下的结果"


def _safe_href(url: str) -> str:
    """只放行 http/https：名单来自中心，别让一条 javascript: 地址溜进本地网页。"""
    text = (url or "").strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    return ""


def _offline_row(row: VisibleRow) -> str:
    owner = _esc(row.owner)
    title = _esc(row.lib_name)
    status = _esc(row.status)
    heartbeat = _esc(row.heartbeat)
    href = _safe_href(row.url)
    if href:
        address = f'<a href="{_esc(href)}">{_esc(row.url)}</a>'
    else:
        address = _esc(row.url)
    return (
        '<tr class="offline">' if row.status != "在线" else "<tr>"
    ) + (
        f"<td>{owner}</td><td>{title}</td>"
        f'<td class="num">{int(row.doc_count)}</td>'
        f"<td>{status}<br><small>{heartbeat}</small></td>"
        f"<td>{address}</td></tr>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def render_offline_html(result: LibraryList, *, generated_at: float | None = None) -> str:
    """把一次查询结果渲染成能双击打开的本地网页（无外部依赖、链接可点）。"""
    stamp = time.time() if generated_at is None else float(generated_at)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
    viewer = result.viewer.strip()
    who = f"姓名「{_esc(viewer)}」" if viewer else "未填姓名（中心只按本机 IP 匹配）"
    rows_html = "".join(_offline_row(row) for row in rows(result))
    total, online = len(result), result.online_count
    if rows_html:
        table = (
            "<table><thead><tr>"
            "<th>库主人</th><th>库名</th><th>篇数</th><th>状态</th><th>地址</th>"
            f"</tr></thead><tbody>{rows_html}</tbody></table>"
        )
    else:
        table = (
            '<div class="warn">这次没有取到任何库的地址，可能是登记中心连不上、'
            "你不在对方的授权名单里，或对方暂停了共享。等中心恢复后再查一次。</div>"
        )
    plain = "".join(
        f"<li>{_esc(row.url)} — {_esc(row.lib_name)}（{_esc(row.owner)}，{int(row.doc_count)} 篇）</li>"
        for row in rows(result)
        if _safe_href(row.url)
    )
    plain_block = (
        f'<h2>纯文本地址（方便复制、转发）</h2><ul class="plain">{plain}</ul>'
        if plain
        else ""
    )
    note = upgrade_note(result)
    note_html = f"<p>{_esc(note)}</p>" if note else ""
    error_html = ""
    if result.error and result.source != "registry":
        error_html = f'<p class="meta">这次查询：{_esc(result.error)}</p>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>资料库地址清单 · {_esc(when)}</title>
<style>{_OFFLINE_CSS}</style>
</head>
<body><main>
<h1>资料库地址清单</h1>
<p class="meta">导出时间：{_esc(when)} · 来源：{_esc(_source_label(result))}<br>
查询身份：{who} · 共 {total} 个库，{online} 个在线</p>
<div class="warn">这是<strong>离线备份</strong>，不是实时名单：登记中心恢复后请重新导出；
对方关机、换了端口或暂停共享后，下面的地址就打不开了。地址里的端口是对方建库时实际用的，
不是固定值。</div>
{error_html}{table}
{note_html}{plain_block}
<footer>用法：双击本文件用浏览器打开，点地址即可查对方资料库；也可以把文件发给同事。<br>
地址形如 http://对方IP:端口/；打不开时先问对方是不是换了端口或关机了。</footer>
</main></body>
</html>
"""


def export_offline(
    result: LibraryList, path: Path | None = None, *, generated_at: float | None = None
) -> Path:
    """把清单写成 HTML（原子写），返回实际写入的路径。"""
    target = Path(path) if path is not None else offline_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, render_offline_html(result, generated_at=generated_at))
    return target


__all__ = [
    "CACHE_TTL",
    "LibraryList",
    "OFFLINE_NAME",
    "VisibleLibrary",
    "VisibleRow",
    "app_data_dir",
    "cache_path",
    "empty_hint",
    "export_offline",
    "fetch_libraries",
    "load_viewer_name",
    "offline_path",
    "render_offline_html",
    "rows",
    "save_viewer_name",
    "summary_text",
    "upgrade_note",
    "viewer_name_path",
]
