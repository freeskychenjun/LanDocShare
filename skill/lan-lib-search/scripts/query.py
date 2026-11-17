#!/usr/bin/env python
"""lan-lib-search：向院内所有在线资料库并发发一句话查询，合并后打印。

只依赖标准库（设计 §8 / D6）：同事机器上不一定有第三方包，pip 也不一定通。
用法：
    python query.py libs                       # 列出在线库
    python query.py search 马斯京根             # 全院一起查
    python query.py search 马斯京根 --lib 陈鋆   # 只查某人的库
    python query.py tables --lib 陈鋆 --path "2026年项目/xxx.docx.md"
    python query.py whoami --set 陈鋆          # 登记自己的名字（可选）
    python query.py search 乐平 --json          # 给程序读

网络约定（设计 §7 / §8）：
- 名单从登记中心 GET /libraries 拿，并缓存 24 h 备用；中心不通时用缓存 + 逐库 /api/health 探活。
- 每库 GET /api/search?q=…&limit=8&name=…，单库超时 2 s，并发 12 路；
  某个库超时不报错，只在脚注里说"在线 X/Y 个库返回结果"。
- 合并：score 归一到各库最大值 × 库级权重（默认 1，可在 config.json 里按人调），
  同一个库内保持原顺序，取前 20。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_REGISTRY = "http://10.91.1.197:8767"
PER_LIB_LIMIT = 8
TAKE = 20
TIMEOUT = 2.0
WORKERS = 12

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


def fetch_libraries() -> tuple[list[dict], str]:
    """取在线库清单。返回 (libraries, 来源)，来源是 registry / cache+health / none。"""
    cache_path = data_dir() / "cache_libraries.json"
    try:
        payload = _get_json(registry_url() + "/libraries", timeout=3.0)
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
    for lib in libraries:  # 缓存里的"在线"会过期，逐库探活一次
        if lib.get("online", True):
            lib["online"] = _health_ok(lib.get("url", ""))
    return libraries, "cache+health"


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


def search_one(lib: dict, query: str, name: str = "", limit: int = PER_LIB_LIMIT) -> dict:
    """查一个库；失败也返回结构、不抛异常（不能让一个库拖垮全院）。"""
    params = {"q": query, "limit": str(limit)}
    if name:
        params["name"] = name
    url = urllib.parse.urljoin(lib["url"], "api/search") + "?" + urllib.parse.urlencode(params)
    try:
        payload = _get_json(url, timeout=TIMEOUT)
        return {
            "lib": lib,
            "ok": True,
            "error": "",
            "results": payload.get("results", []),
            "fallback": bool(payload.get("fallback")),
        }
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001 - 读不到错误体就只报状态码
            pass
        return {"lib": lib, "ok": False, "error": detail or f"HTTP {exc.code}", "results": []}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"lib": lib, "ok": False, "error": exc.__class__.__name__, "results": []}


def search_all(libraries: list[dict], query: str, name: str = "", per_lib: int = PER_LIB_LIMIT) -> list[dict]:
    online = [lib for lib in libraries if lib.get("online", True)]
    if not online:
        return []
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(online))) as pool:
        futures = [pool.submit(search_one, lib, query, name, per_lib) for lib in online]
        for future in as_completed(futures):
            out.append(future.result())
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
    return merged[:take]


# ---------------------------------------------------------------- 输出
def _clip(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def print_libraries(libraries: list[dict], source: str) -> None:
    online = [lib for lib in libraries if lib.get("online", True)]
    if not libraries:
        print("没有拿到任何库的名单：登记中心连不上，本地也没有缓存。")
        print('可以把中心地址写进 config.json（{"registry": "http://…:8767"}），或设环境变量 LANLIB_REGISTRY。')
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
    libraries, source = fetch_libraries()
    if args.json:
        print(
            json.dumps(
                {"source": source, "latest_version": _LATEST.get("version", ""), "libraries": libraries},
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
    libraries, source = fetch_libraries()
    if args.lib:
        libraries = [
            lib
            for lib in libraries
            if args.lib in (lib.get("name") or "") or args.lib in (lib.get("lib_name") or "")
        ]
        if not libraries:
            print(f"没找到叫「{args.lib}」的库（在线名单里没有）。", file=sys.stderr)
            return 3
    found = search_all(libraries, query, name=personal_name(), per_lib=args.per_lib)
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
    libraries, _source = fetch_libraries()
    match = [
        lib for lib in libraries if args.lib in (lib.get("name") or "") or args.lib in (lib.get("lib_name") or "")
    ]
    if not match:
        print(f"没找到叫「{args.lib}」的库。", file=sys.stderr)
        return 3
    url = urllib.parse.urljoin(match[0]["url"], "api/tables") + "?" + urllib.parse.urlencode({"path": args.path})
    try:
        payload = _get_json(url, timeout=5.0)
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
    print(f"{match[0].get('name')} 的库 · {args.path} · {len(tables)} 张表")
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
