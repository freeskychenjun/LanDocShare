#!/usr/bin/env python
"""lan-lib-search：向院内所有在线资料库并发发一句话查询，合并后打印。

只依赖标准库（设计 §8 / D6）：同事机器上不一定有第三方包，pip 也不一定通。
用法：
    python query.py libs                       # 列出在线库
    python query.py search 马斯京根             # 对你开放的库一起查
    python query.py search 马斯京根 --lib 陈鋆   # 只查某人的库
    python query.py tables --lib 陈鋆 --path "2026年项目/xxx.docx.md"
    python query.py whoami --set 陈鋆          # 登记自己的名字（可选）
    python query.py search 乐平 --json          # 给程序读

网络约定（设计 §7 / §8）：
- 名单从登记中心 GET /libraries 拿，并缓存 24 h 备用；带上 identity.txt 的姓名后，
  中心只返回"这个姓名/IP 有权限访问"的库。中心不通时用缓存 + 并发 /api/health 探活。
- 每库 GET /api/search?q=…&limit=8&name=…，单库超时按"这个库历来多快"单独定
  （默认 1.2 s，连上不回的库压到 0.6 s，慢库放宽到 2 s），并发 32 路；
  某个库超时不报错，只在脚注里说"在线 X/Y 个库返回结果"。
- 合并：score 归一到各库最大值 × 库级权重（默认 1，可在 config.json 里按人调），
  同一个库内保持原顺序；**同一份文档（标题+文件名+类型相同）只留排名最高的一条**，
  其余位置折成"共 N 处"，取前 20。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_REGISTRY = "http://10.91.1.197:8767"
PER_LIB_LIMIT = 8
TAKE = 20
TIMEOUT = 1.2  # 单库默认超时：睡着的库最多拖 1.2 s（原来是 2 s）
TIMEOUT_MIN = 0.6  # 连续超时过的库（多半在睡/离线）用最短预算，不陪它等
TIMEOUT_MAX = 2.0  # 历史延迟确实高的库才放宽到这里
PROBE_SECONDS = 600.0  # 连超的库每隔这么久给一次完整预算，探它是否醒了
# 并发扇出的库数。库多了要抬高（12 并发时 60 个库要跑 5 批，实测 747 ms；
# 32 并发同样 60 个库 268 ms）。可用环境变量 LANLIB_WORKERS 覆盖，上限 64。
WORKERS = max(1, min(64, int(os.environ.get("LANLIB_WORKERS") or 32)))

# 中心在 /libraries 里告诉我们"最新版本是多少"，用来提示哪些库该升级
_LATEST: dict[str, str] = {"version": ""}


# ---------------------------------------------------------------- 位置与配置
def script_dir() -> Path:
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """缓存/身份/配置放用户的本地应用目录，不往 skill 安装目录里写（可能只读）。"""
    override = os.environ.get("LANLIB_HOME")
    base = Path(override) if override else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "lan-lib-search"
    base.mkdir(parents=True, exist_ok=True)
    return base


def load_config() -> dict:
    for path in (data_dir() / "config.json", script_dir() / "config.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def registry_url(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    value = os.environ.get("LANLIB_REGISTRY") or cfg.get("registry") or DEFAULT_REGISTRY
    return str(value).rstrip("/")


# ---------------------------------------------------------------- HTTP
def _get_json(url: str, timeout: float = TIMEOUT) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 内网地址
        return json.loads(resp.read().decode("utf-8") or "{}")


def _cache_path(name: str) -> Path:
    """按身份分缓存：换了姓名不能拿别人的可见库清单。

    姓名的哈希只用来分文件，不把姓名写进文件名（避免中文/特殊字符）。
    """
    if not name:
        return data_dir() / "cache_libraries.json"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return data_dir() / f"cache_libraries_{digest}.json"


def fetch_libraries(name: str = "") -> tuple[list[dict], str]:
    """取在线库清单。返回 (libraries, 来源)，来源是 registry / cache+health / none。

    带上 name 后，中心只返回"这个姓名/IP 有权限访问"的库；匿名则只按来源 IP。
    """
    cache_path = _cache_path(name)
    params = {"name": name} if name else {}
    url = registry_url() + "/libraries" + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        payload = _get_json(url, timeout=3.0)
        libraries = payload.get("libraries", [])
        _LATEST["version"] = str(payload.get("latest_version") or "")
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return libraries, "registry"
    except (OSError, ValueError, urllib.error.URLError):
        pass  # 中心不可达 → 用缓存
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], "none"
    libraries = cached.get("libraries", [])
    _LATEST["version"] = str(cached.get("latest_version") or "")
    return _probe_online(libraries), "cache+health"


def _probe_online(libraries: list[dict]) -> list[dict]:
    """缓存里的"在线"是会过期的，逐库探活一次——但要并发探。

    串行探时 N 个库最坏要 N × 1.5 秒（20 个库 30 秒），而这只是"中心挂了"的
    兜底路径，没人愿意为它等半分钟。并发探的墙钟上限是 ⌈N/WORKERS⌉ × 1.5 秒。
    原地改 `online` 字段、保持原有顺序（顺序要跟缓存里的登记顺序一致）。
    """
    targets = [lib for lib in libraries if lib.get("online", True)]
    if not targets:
        return libraries
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(targets))) as pool:
        alive = list(pool.map(lambda lib: _health_ok(lib.get("url", "")), targets))
    for lib, ok in zip(targets, alive):
        lib["online"] = ok
    return libraries


def upgrade_note(libraries: list[dict]) -> str:
    """哪些库的版本比中心公布的最新版旧 —— 一行提示，不打断查询。"""
    latest = _LATEST.get("version") or ""
    if not latest:
        return ""
    stale = [lib for lib in libraries if lib.get("version") and lib["version"] != latest]
    if not stale:
        return ""
    names = "、".join(sorted({str(lib.get("name") or lib.get("ip") or "?") for lib in stale}))
    return f"（{len(stale)} 个库版本落后，最新 {latest}：{names}；提醒库主人升级）"


def _health_ok(url: str, timeout: float = 1.5) -> bool:
    if not url:
        return False
    try:
        _get_json(urllib.parse.urljoin(url, "api/health"), timeout=timeout)
        return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


# ---------------------------------------------------------------- 查询与合并
def personal_name() -> str:
    try:
        return (data_dir() / "identity.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class LatencyBook:
    """记住每个库历来多快，据此给每个库单独定超时。

    库多了以后最拖时间的不是"查得慢"，而是"某个同事电脑睡着了"：连上不回，
    只能等超时。第一次白等一次默认超时，之后把它记成连连超时，预算压到
    TIMEOUT_MIN，后面的查询就不再陪它等 2 秒。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "latency.json")
        self.entries: dict[str, dict] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = time.time()
        for url, entry in (raw.get("libs") or {}).items():
            if not isinstance(entry, dict):
                continue
            if now - float(entry.get("seen", 0) or 0) > 30 * 86400:
                continue
            self.entries[str(url)] = entry

    def save(self) -> None:
        if not self.dirty:
            return
        payload = {"saved_at": time.time(), "libs": self.entries}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # 记不下来不影响查询
        self.dirty = False

    def timeout_for(self, url: str) -> float:
        entry = self.entries.get(url) or {}
        strikes = int(entry.get("strikes", 0) or 0)
        if strikes > 0:
            # 连着超时的库：预算减半，最低 TIMEOUT_MIN。但每 PROBE_SECONDS 给一次
            # 完整预算探它是否回来了——否则它永远没机会证明自己已经醒了。
            last_fail = float(entry.get("last_fail", 0) or 0)
            if time.time() - last_fail >= PROBE_SECONDS:
                return TIMEOUT
            return max(TIMEOUT_MIN, TIMEOUT / (2**strikes))
        ewma = float(entry.get("ewma_ms", 0) or 0)
        if ewma > 0:
            return max(TIMEOUT_MIN, min(TIMEOUT_MAX, ewma * 3 / 1000))
        return TIMEOUT

    def record(self, url: str, elapsed_ms: float, ok: bool) -> None:
        entry = self.entries.setdefault(url, {})
        entry["seen"] = time.time()
        if ok:
            previous = float(entry.get("ewma_ms", 0) or 0)
            entry["ewma_ms"] = elapsed_ms if previous <= 0 else previous * 0.7 + elapsed_ms * 0.3
            entry["strikes"] = 0
            entry["last_ok"] = time.time()
        else:
            entry["strikes"] = int(entry.get("strikes", 0) or 0) + 1
            entry["last_fail"] = time.time()
            entry.setdefault("last_ok", 0)
        self.dirty = True


def search_one(lib: dict, query: str, name: str = "", limit: int = PER_LIB_LIMIT, timeout: float = TIMEOUT) -> dict:
    """查一个库；失败也返回结构、不抛异常（不能让一个库拖垮全院）。"""
    params = {"q": query, "limit": str(limit)}
    if name:
        params["name"] = name
    url = urllib.parse.urljoin(lib["url"], "api/search") + "?" + urllib.parse.urlencode(params)
    started = time.perf_counter()
    try:
        payload = _get_json(url, timeout=timeout)
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "lib": lib,
            "ok": True,
            "error": "",
            "results": payload.get("results", []),
            "fallback": bool(payload.get("fallback")),
            "elapsed_ms": round(elapsed, 1),
        }
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001 - 读不到错误体就只报状态码
            pass
        return {
            "lib": lib,
            "ok": False,
            "error": detail or f"HTTP {exc.code}",
            "results": [],
            "elapsed_ms": round(elapsed, 1),
        }
    except (OSError, ValueError, urllib.error.URLError) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "lib": lib,
            "ok": False,
            "error": exc.__class__.__name__,
            "results": [],
            "elapsed_ms": round(elapsed, 1),
        }


def search_all(
    libraries: list[dict],
    query: str,
    name: str = "",
    per_lib: int = PER_LIB_LIMIT,
    book: LatencyBook | None = None,
) -> list[dict]:
    online = [lib for lib in libraries if lib.get("online", True)]
    if not online:
        return []
    book = book if book is not None else LatencyBook()
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(online))) as pool:
        futures = [
            pool.submit(search_one, lib, query, name, per_lib, book.timeout_for(lib.get("url", "")))
            for lib in online
        ]
        for future in as_completed(futures):
            item = future.result()
            out.append(item)
            book.record(item["lib"].get("url", ""), float(item.get("elapsed_ms") or 0), item["ok"])
    book.save()
    return out


def merge(found: list[dict], weights: dict | None = None, take: int = TAKE) -> list[dict]:
    """按库归一化后合并排序。

    bm25 的 score 是负数、越负越相关，且各库量纲不同（篇数、词频），
    所以先除以本库最负的那条（= 本库最佳）得到 0~1，再乘库级权重。
    """
    weights = weights or {}
    merged: list[dict] = []
    for item in found:
        if not item["ok"] or not item["results"]:
            continue
        lib = item["lib"]
        weight = float(weights.get(lib.get("name", ""), 1.0))
        best = min(float(row.get("score", 0.0)) for row in item["results"])
        for order, row in enumerate(item["results"]):
            score = float(row.get("score", 0.0))
            ratio = (score / best) if best else 0.0
            merged.append(
                {
                    "lib_name": lib.get("name") or lib.get("lib_name", ""),
                    "lib_url": lib.get("url", ""),
                    "doc_count": lib.get("doc_count", 0),
                    "title": row.get("title", ""),
                    "path": row.get("path", ""),
                    "ext": row.get("ext", ""),
                    "snippet": row.get("snippet", ""),
                    "score": score,
                    "rank": round(ratio * weight, 4),
                    "order": order,
                }
            )
    merged.sort(key=lambda row: (-row["rank"], row["lib_name"], row["order"]))
    return _dedupe(merged)[:take]


def _norm_text(text: str) -> str:
    """去空格/标点、统一大小写，用来判断"两份是不是同一份文档"。"""
    return re.sub(r"[\W_]+", "", str(text or "").lower(), flags=re.UNICODE)


def _doc_key(row: dict) -> tuple[str, str, str]:
    """同一份文档被多个库收录时，标题、类型、文件名通常一模一样。

    故意只认这三样同时相等：宁可漏合并（多显示一条），也不要把标题相近的
    不同文档并掉——「计算书」「设计报告」这类标题在院里很常见。
    """
    return (
        _norm_text(row.get("title", "")),
        _norm_text(row.get("ext", "")),
        _norm_text(Path(str(row.get("path") or "")).name),
    )


def _dedupe(rows: list[dict]) -> list[dict]:
    """把重复文档折成一条：保留排名最高的，其余位置记进 copies。"""
    kept: list[dict] = []
    seen: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = _doc_key(row)
        if not any(key):
            kept.append(row)
            continue
        first = seen.get(key)
        if first is None:
            row["copies"] = [{"lib_name": row["lib_name"], "path": row["path"]}]
            seen[key] = row
            kept.append(row)
        else:
            first["copies"].append({"lib_name": row["lib_name"], "path": row["path"]})
    return kept


# ---------------------------------------------------------------- 输出
def _clip(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def print_libraries(libraries: list[dict], source: str) -> None:
    online = [lib for lib in libraries if lib.get("online", True)]
    if not libraries:
        if source == "none":
            print("没有拿到任何库的名单：登记中心连不上，本地也没有缓存。")
            print('可以把中心地址写进 config.json（{"registry": "http://…:8767"}），或设环境变量 LANLIB_REGISTRY。')
        else:
            print("中心没有返回任何对你开放的库。")
            print("可能原因：库主人还没升级到会报告授权范围的版本、你不在对方的授权名单里、或对方暂停了共享。")
        return
    print(f"在线 {len(online)} / {len(libraries)} 个库（名单来源：{source}）")
    for lib in libraries:
        mark = "●" if lib.get("online", True) else "○"
        age = lib.get("age_seconds")
        if lib.get("online", True):
            age_text = f"，{int(age)} 秒前心跳" if isinstance(age, (int, float)) else ""
        else:
            age_text = "，已离线"
        print(f"  {mark} {lib.get('name') or '未命名'} · {lib.get('lib_name', '')} · {lib.get('doc_count', 0)} 篇{age_text}")
        print(f"     {lib.get('url', '')}")
    note = upgrade_note(libraries)
    if note:
        print(note)


def print_results(query: str, merged: list[dict], found: list[dict], libraries: list[dict]) -> None:
    online = [lib for lib in libraries if lib.get("online", True)]
    responded = [item for item in found if item["ok"]]
    if not merged:
        print(f"没有找到包含「{query}」的内容。")
        if any(item.get("fallback") for item in responded):
            print("（有库是短查询兜底命中的，可以再试更短的词。）")
        print(f"在线 {len(responded)}/{len(online)} 个库返回结果。")
        return
    print(f"「{query}」在 {len({m['lib_name'] for m in merged})} 个库里命中 {len(merged)} 条：\n")
    current = None
    index = 1
    for row in merged:
        if row["lib_name"] != current:
            current = row["lib_name"]
            print(f"【{current}】{row['doc_count']} 篇 · {row['lib_url']}")
        print(f"  {index}. {row['title']}  ({row['ext']})")
        print(f"     {row['path']}")
        copies = row.get("copies") or []
        if len(copies) > 1:
            names = sorted({c["lib_name"] or "?" for c in copies})
            where = "、".join(names) if len(names) > 1 else "同一库内另一处"
            print(f"     · 同一份文档共 {len(copies)} 处（{where}）")
        print(f"     {_clip(row['snippet'])}")
        index += 1
    print()
    for item in found:
        if not item["ok"]:
            print(f"  （{item['lib'].get('name', '?')} 未响应：{item['error']}）")
    print(f"在线 {len(responded)}/{len(online)} 个库返回结果；同一库内按相关度，跨库做了归一化。")
    print("只给片段和路径，不含原文；要某篇里的表格用：tables --lib <库主人> --path <路径>")


# ---------------------------------------------------------------- 命令
def cmd_libs(args) -> int:
    name = personal_name()
    libraries, source = fetch_libraries(name)
    if args.json:
        print(
            json.dumps(
                {
                    "source": source,
                    "viewer": name,
                    "latest_version": _LATEST.get("version", ""),
                    "libraries": libraries,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print_libraries(libraries, source)
    return 0 if libraries else 3


def cmd_search(args) -> int:
    query = args.query.strip()
    if not query:
        print("请给一个查询词。", file=sys.stderr)
        return 2
    cfg = load_config()
    name = personal_name()
    libraries, source = fetch_libraries(name)
    if args.lib:
        libraries = [
            lib
            for lib in libraries
            if args.lib in (lib.get("name") or "") or args.lib in (lib.get("lib_name") or "")
        ]
        if not libraries:
            print(f"没找到叫「{args.lib}」的库（在线名单里没有）。", file=sys.stderr)
            return 3
    found = search_all(libraries, query, name=name, per_lib=args.per_lib)
    merged = merge(found, weights=cfg.get("weights") or {}, take=args.take)
    if args.json:
        payload = {
            "query": query,
            "source": source,
            "libraries": len(libraries),
            "answered": sum(1 for item in found if item["ok"]),
            "results": merged,
            "failures": [{"lib": item["lib"].get("name"), "error": item["error"]} for item in found if not item["ok"]],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if merged else 4
    print_results(query, merged, found, libraries)
    return 0 if merged else 4


def cmd_tables(args) -> int:
    libraries, _source = fetch_libraries(personal_name())
    match = [
        lib for lib in libraries if args.lib in (lib.get("name") or "") or args.lib in (lib.get("lib_name") or "")
    ]
    if not match:
        print(f"没找到叫「{args.lib}」的库。", file=sys.stderr)
        return 3
    path = args.path

    def _fetch(path_arg: str) -> dict:
        params = {"path": path_arg}
        personal = personal_name()
        if personal:
            params["name"] = personal
        url = urllib.parse.urljoin(match[0]["url"], "api/tables") + "?" + urllib.parse.urlencode(params)
        return _get_json(url, timeout=5.0)

    try:
        try:
            payload = _fetch(path)
        except urllib.error.HTTPError as exc:
            # 最常见的手误：--path 漏了 .md 后缀（索引里的路径都带它）。补一次就够，别再让用户重跑。
            if exc.code != 404 or path.endswith(".md"):
                raise
            path += ".md"
            payload = _fetch(path)
    except urllib.error.HTTPError as exc:
        print(f"取表格失败：HTTP {exc.code}（路径不在这个库的索引里？）", file=sys.stderr)
        return 4
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"取表格失败：{exc.__class__.__name__}", file=sys.stderr)
        return 4
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    tables = payload.get("tables", [])
    print(f"{match[0].get('name')} 的库 · {path} · {len(tables)} 张表")
    for n, table in enumerate(tables, 1):
        context = table.get("context") or ""
        print(f"\n表 {n}：{context}" if context else f"\n表 {n}")
        print(table.get("markdown") or table.get("text") or "")
    return 0


def cmd_whoami(args) -> int:
    path = data_dir() / "identity.txt"
    if args.set is not None:
        path.write_text(args.set.strip(), encoding="utf-8")
        print(f"已记下：{args.set.strip() or '（空，将匿名查询）'} → {path}")
        return 0
    current = personal_name()
    print(f"当前身份：{current or '（未登记，查询时匿名）'}")
    print(f"文件：{path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="query.py", description="查院内的共享资料库（只返回片段与表格）")
    sub = parser.add_subparsers(dest="command", required=True)

    libs = sub.add_parser("libs", help="列出在线的库")
    libs.add_argument("--json", action="store_true")
    libs.set_defaults(func=cmd_libs)

    search = sub.add_parser("search", help="全院一起查")
    search.add_argument("query")
    search.add_argument("--lib", help="只查某个人的库（名字的一部分也行）")
    search.add_argument("--per-lib", type=int, default=PER_LIB_LIMIT, dest="per_lib")
    search.add_argument("--take", type=int, default=TAKE)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    tables = sub.add_parser("tables", help="取某篇文档里的表格")
    tables.add_argument("--lib", required=True)
    tables.add_argument("--path", required=True, help="搜索结果里那条相对路径")
    tables.add_argument("--json", action="store_true")
    tables.set_defaults(func=cmd_tables)

    whoami = sub.add_parser("whoami", help="登记/查看自己的名字（可选）")
    whoami.add_argument("--set", default=None)
    whoami.set_defaults(func=cmd_whoami)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
