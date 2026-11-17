"""命令行入口：init / probe / convert / serve / status。

同事侧不直接用它（他们用托盘程序），但它是开发、排错与真机验收的主力：
`init` 建库、`probe` 体检、`convert` 跑一批、`serve` 起服务、`status` 看统计。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from landoc import __version__, pipeline, probe
from landoc.config import Config
from landoc.index import Index
from landoc.netinfo import lan_ip


# 二期 registry 中心（设计 §7）固定在 8767，库端顺延端口时必须跳过它，
# 否则"本机 8766 被旧服务占住"的机器会把 8767 抢走，registry 反而起不来。
RESERVED_PORTS = frozenset({8767})


def _port_free(host: str, port: int) -> bool:
    """能否绑定该端口；不加 SO_REUSEADDR（Windows 上它会让 bind 抢占已在监听的端口）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(start: int, host: str = "0.0.0.0", tries: int = 20) -> int:
    """从 start 起找一个能绑定的端口（8766 被旧的共享服务占着时自动顺延，跳过 8767）。"""
    for offset in range(tries):
        port = start + offset
        if port in RESERVED_PORTS:
            continue
        if _port_free(host, port):
            return port
    raise RuntimeError(f"{start}..{start + tries - 1} 端口都被占用（已跳过保留端口 {sorted(RESERVED_PORTS)}）")


ACROBAT_TOOL_CANDIDATES = (
    Path(r"D:\应用软件\右键解密"),
    Path(r"D:\Claude Code\解密平台\工具\单个解密"),
)


def _find_acrobat_tool_dir() -> Path | None:
    """找现成的兜底工具目录（有 acrobat.exe 才算）。"""
    for candidate in ACROBAT_TOOL_CANDIDATES:
        if (candidate / "acrobat.exe").is_file():
            return candidate
    return None


def _storage_policy(cfg) -> str:
    """一句话说清当前存了什么（库主人最关心的隐私开关）。"""
    content = (
        "全文进索引"
        if getattr(cfg, "store_content", True)
        else f"只存前 {pipeline.snippet_cap(cfg)} 字"
    )
    markdown = "落 md 副本" if getattr(cfg, "render_md", True) else "不落 md"
    return f"{content} / {markdown}"


def _snippet_chars(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("要一个整数") from None
    if value < pipeline.MIN_SNIPPET_ONLY_CHARS:
        raise argparse.ArgumentTypeError(
            f"至少 {pipeline.MIN_SNIPPET_ONLY_CHARS} 字（再小就只能搜标题，没有意义）"
        )
    return value


def _cmd_init(args) -> int:
    lib_dir = Path(args.lib_dir)
    lib_dir.mkdir(parents=True, exist_ok=True)
    # 已存在的库：读旧配置再合并，避免把共享范围/工具目录打回默认
    try:
        cfg = Config.load(lib_dir)
    except (OSError, ValueError):
        cfg = Config(name=args.name, lib_dir=lib_dir)
    cfg.name = args.name
    if args.folder:
        merged = list(cfg.folders)
        for raw in args.folder:
            candidate = Path(raw)
            if candidate not in merged:
                merged.append(candidate)
        cfg.folders = merged
    tool_dir = Path(args.acrobat_tool_dir) if args.acrobat_tool_dir else None
    if tool_dir is None and cfg.acrobat_tool_dir is None:
        tool_dir = _find_acrobat_tool_dir()
    if tool_dir is not None:
        cfg.acrobat_tool_dir = tool_dir
    if args.store_content is not None:
        cfg.store_content = args.store_content
    if args.snippet_only_chars is not None:
        cfg.snippet_only_chars = args.snippet_only_chars
    if args.render_md is not None:
        cfg.render_md = args.render_md
    if args.allow_cidrs is not None:
        from landoc.wizard import parse_cidrs

        cidrs, error = parse_cidrs(args.allow_cidrs)
        if error:
            print(f"[错误] {error}", file=sys.stderr)
            return 2
        cfg.allow_cidrs = cidrs
    cfg.save()
    Index(cfg.db_path, md_root=cfg.md_dir).commit()
    print(f"[OK] 已建库：{cfg.name} → {lib_dir}")
    print(f"     共享目录 {len(cfg.folders)} 个：{', '.join(str(f) for f in cfg.folders) or '（未指定）'}")
    print(f"     允许网段：{', '.join(cfg.allow_cidrs)}")
    print(f"     存储策略：{_storage_policy(cfg)}")
    if cfg.acrobat_tool_dir:
        print(f"     加密 PDF 兜底工具：{cfg.acrobat_tool_dir}")
    return 0


def _cmd_probe(args) -> int:
    folders = [Path(f) for f in (args.folder or [])]
    if not folders and args.lib_dir:
        folders = Config.load(Path(args.lib_dir)).folders
    report = probe.sample_report(folders, n=args.n)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["sampled"] == 0:
        print("[警告] 没有采样到任何文档，检查目录是否存在、是否只有 .xls 等不支持的格式")
    elif report["encrypted"] and not report["has_office"]:
        print("[警告] 本机有加密文档但没有 Office，这些文档无法转换")
    return 0


def _cmd_convert(args) -> int:
    cfg = Config.load(Path(args.lib_dir))
    report = pipeline.run(cfg, limit=args.limit)
    print(f"[OK] 新转换 {report.converted} 篇")
    for reason, count in sorted(report.skipped.items()):
        print(f"     跳过 {reason}: {count}")
    if report.failed:
        print(f"     失败 {len(report.failed)} 篇：")
        for rel, reason in report.failed[:20]:
            print(f"       - {rel} → {reason}")
    return 0


def _cmd_serve(args) -> int:
    from landoc.server import make_server

    cfg = Config.load(Path(args.lib_dir))
    port = args.port or pick_port(cfg.port)
    httpd = make_server(cfg, port=port)
    host, actual = httpd.server_address[:2]
    try:  # 实际端口写进 meta：二期 registry 靠它登记，客户端才不会连错
        Index(cfg.db_path, md_root=cfg.md_dir).set_meta("port", str(actual))
    except Exception:  # noqa: BLE001
        pass
    print(f"[OK] 服务已启动：http://{_lan_ip()}:{actual}/  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 已退出")
    finally:
        httpd.server_close()
    return 0


def _cmd_status(args) -> int:
    cfg = Config.load(Path(args.lib_dir))
    index = Index(cfg.db_path, md_root=cfg.md_dir)
    stats = index.stats()
    state = pipeline._read_state(cfg)
    print(f"库：{cfg.name}")
    print(f"目录：{cfg.lib_dir}")
    print(f"文档数：{stats['total']}")
    print(f"共享目录：{', '.join(str(f) for f in cfg.folders) or '（未指定）'}")
    print(f"允许网段：{', '.join(cfg.allow_cidrs)}")
    print(f"允许名单：{_people_summary(cfg)}")
    print(f"存储策略：{_storage_policy(cfg)}")
    print(f"上次扫描：{state.get('last_scan_ts', '（从未）')}")
    return 0


def _people_summary(cfg) -> str:
    people = getattr(cfg, "allow_people", None) or {}
    if not people:
        return "（未启用：允许网段内谁都能查）"
    with_ip = sum(1 for ip in people.values() if str(ip or "").strip())
    if with_ip == len(people):
        return f"{len(people)} 人，按 IP 放行（姓名仅备注）"
    return f"{len(people)} 人，按 IP 放行；其中 {len(people) - with_ip} 人没登记 IP、需报姓名"


def _cmd_people(args) -> int:
    from landoc.people import dump_people_csv, load_people_csv, merge_people

    cfg = Config.load(Path(args.lib_dir))
    current = dict(cfg.allow_people)
    changed = False

    if args.csv:
        try:
            incoming, warnings = load_people_csv(Path(args.csv))
        except (OSError, ValueError) as exc:
            print(f"[错误] 读不了名单：{exc}", file=sys.stderr)
            return 2
        for warning in warnings:
            print(f"[提示] {warning}")
        current, notes = merge_people(current, incoming, replace=args.replace)
        for note in notes:
            print(f"[名单] {note}")
        changed = True

    if args.remove:
        if args.remove in current:
            current.pop(args.remove)
            print(f"[名单] 移除 {args.remove}")
            changed = True
        else:
            print(f"[提示] 名单里没有 {args.remove}")

    if args.set:
        name, _, ip = args.set.partition("=")
        name = name.strip()
        if not name:
            print("[错误] --set 要写成 姓名=IP", file=sys.stderr)
            return 2
        try:
            current[name] = _clean_person_ip(ip)
        except ValueError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2
        print(f"[名单] {name} → {current[name] or '（不登记 IP，需报姓名）'}")
        changed = True

    if changed:
        cfg.allow_people = current
        cfg.save()

    if args.export:
        Path(args.export).write_text(dump_people_csv(current), encoding="utf-8-sig")
        print(f"[导出] {args.export}（{len(current)} 人）")

    if not current:
        print("名单为空：允许网段内谁都能查。")
        return 0
    print(f"共 {len(current)} 人（按 IP 授权，姓名只作备注）：")
    for name, ip in current.items():
        print(f"  {(ip or '—').ljust(16)} {name}")
    print("改动即时生效：下一请求就按新名单校验（服务不用重启）。")
    return 0


def _clean_person_ip(raw: str) -> str:
    import ipaddress

    text = (raw or "").strip()
    if not text:
        return ""
    return str(ipaddress.ip_address(text))


def _lan_ip() -> str:
    return lan_ip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="landoc", description="个人资料库共享（检索侧）")
    parser.add_argument("--version", action="version", version=f"landoc {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="建库并写配置")
    init.add_argument("--lib-dir", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--folder", action="append", help="共享目录，可重复指定多个")
    init.add_argument(
        "--acrobat-tool-dir",
        help="加密 PDF 兜底工具目录（内含 acrobat.exe）；不给则自动探测已知路径",
    )
    init.add_argument(
        "--allow-cidrs",
        default=None,
        metavar="CIDR[,CIDR…]",
        help="允许访问的网段，逗号分隔（默认 10.91.0.0/16；想只给本院就填 10.91.1.0/24）；不给则沿用现有配置",
    )
    init.add_argument(
        "--store-content",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否把全文写进索引（--no-store-content 只存每篇前 N 字，N 见 --snippet-only-chars）；不给则沿用现有配置",
    )
    init.add_argument(
        "--snippet-only-chars",
        type=_snippet_chars,
        default=None,
        metavar="N",
        help="--no-store-content 时保留的正文前缀字数（默认 20000；越大越保检索召回）；不给则沿用现有配置",
    )
    init.add_argument(
        "--render-md",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否在 md/ 留 Markdown 副本（--no-render-md 不落盘）；不给则沿用现有配置",
    )
    init.set_defaults(func=_cmd_init)

    probe_cmd = sub.add_parser("probe", help="抽样体检：明/密/受阻比例")
    probe_cmd.add_argument("--lib-dir")
    probe_cmd.add_argument("--folder", action="append")
    probe_cmd.add_argument("--n", type=int, default=200)
    probe_cmd.set_defaults(func=_cmd_probe)

    convert = sub.add_parser("convert", help="转换一批文档")
    convert.add_argument("--lib-dir", required=True)
    convert.add_argument("--limit", type=int, default=None)
    convert.set_defaults(func=_cmd_convert)

    serve = sub.add_parser("serve", help="启动只读检索服务")
    serve.add_argument("--lib-dir", required=True)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=_cmd_serve)

    status = sub.add_parser("status", help="查看库统计")
    status.add_argument("--lib-dir", required=True)
    status.set_defaults(func=_cmd_status)

    people_cmd = sub.add_parser("people", help="查看/维护允许名单（姓名 → IP）")
    people_cmd.add_argument("--lib-dir", required=True)
    people_cmd.add_argument("--csv", help="从 CSV 导入名单（列：姓名,IP，可有表头）")
    people_cmd.add_argument("--replace", action="store_true", help="以 CSV 为准整体替换，而不是合并")
    people_cmd.add_argument("--set", metavar="姓名=IP", help="新增/修改一条；IP 留空表示必须报姓名")
    people_cmd.add_argument("--remove", metavar="姓名", help="删除一条")
    people_cmd.add_argument("--export", metavar="PATH", help="导出成 CSV（以后给客户端分发）")
    people_cmd.set_defaults(func=_cmd_people)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
