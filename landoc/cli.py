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


def pick_port(start: int, host: str = "0.0.0.0", tries: int = 20) -> int:
    """从 start 起找一个能绑定的端口（8766 被旧的共享服务占着时自动顺延）。"""
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # 不加 SO_REUSEADDR：Windows 上它会让 bind 成功"抢占"已在监听的端口，
            # 探测就永远返回被占用的那个端口。
            try:
                sock.bind((host, port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"{start}..{start + tries - 1} 端口都被占用")


def _cmd_init(args) -> int:
    lib_dir = Path(args.lib_dir)
    lib_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        name=args.name,
        lib_dir=lib_dir,
        folders=[Path(f) for f in (args.folder or [])],
    )
    cfg.save()
    Index(cfg.db_path, md_root=cfg.md_dir).commit()
    print(f"[OK] 已建库：{cfg.name} → {lib_dir}")
    print(f"     共享目录 {len(cfg.folders)} 个：{', '.join(str(f) for f in cfg.folders) or '（未指定）'}")
    print(f"     允许网段：{', '.join(cfg.allow_cidrs)}")
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
    print(f"上次扫描：{state.get('last_scan_ts', '（从未）')}")
    return 0


def _lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("10.255.255.255", 1))  # 不实际发包，只让系统选内网路由
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="landoc", description="个人资料库共享（检索侧）")
    parser.add_argument("--version", action="version", version=f"landoc {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="建库并写配置")
    init.add_argument("--lib-dir", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--folder", action="append", help="共享目录，可重复指定多个")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
