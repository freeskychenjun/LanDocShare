"""命令行入口：init / probe / convert / serve / status。

同事侧不直接用它（他们用托盘程序），但它是开发、排错与真机验收的主力：
`init` 建库、`probe` 体检、`convert` 跑一批、`serve` 起服务、`status` 看统计。
"""

from __future__ import annotations

import argparse
import json
import os
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


ACROBAT_TOOL_DIR_ENV = "LANDOC_ACROBAT_TOOL_DIR"
# 开发机上的历史位置：只在源码运行（没打包）时才去探，免得包里带着别人家的目录名
_DEV_ACROBAT_TOOL_CANDIDATES = (
    Path(r"D:\应用软件\右键解密"),
    Path(r"D:\Claude Code\解密平台\工具\单个解密"),
)


def acrobat_tool_candidates() -> tuple[Path, ...]:
    """兜底工具目录的候选位置，按优先级排。

    环境变量 LANDOC_ACROBAT_TOOL_DIR 优先（换机器不用改代码），其次是程序旁边的
    `工具/`（打包分发时把工具放这儿最省事），最后才是开发机上的历史路径。
    """
    found: list[Path] = []
    env = os.environ.get(ACROBAT_TOOL_DIR_ENV, "").strip()
    if env:
        found.append(Path(env))
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    found.append(base / "工具")
    if not getattr(sys, "frozen", False):
        found.extend(_DEV_ACROBAT_TOOL_CANDIDATES)
    return tuple(found)


def _find_acrobat_tool_dir() -> Path | None:
    """找现成的兜底工具目录（有 acrobat.exe 才算）。"""
    for candidate in acrobat_tool_candidates():
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


def _parse_optional_cidrs(text: str | None) -> tuple[list[str] | None, str | None]:
    """解析可选网段参数：None=不改，空串/“无”=清空，其余交给向导的 parse_cidrs。"""
    if text is None:
        return None, None
    if not str(text).strip() or str(text).strip().lower() in {"none", "无", "清空"}:
        return [], None
    from landoc.wizard import parse_cidrs

    return parse_cidrs(str(text))


def _cmd_init(args) -> int:
    lib_dir = Path(args.lib_dir)
    lib_dir.mkdir(parents=True, exist_ok=True)
    # 已存在的库：读旧配置再合并，避免把共享范围/工具目录打回默认
    try:
        cfg = Config.load(lib_dir)
        fresh = False
    except (OSError, ValueError):
        cfg = Config(name=args.name, lib_dir=lib_dir)
        fresh = True
    cfg.name = args.name
    if args.folder:
        merged = list(cfg.folders)
        for raw in args.folder:
            candidate = Path(raw)
            if candidate not in merged:
                merged.append(candidate)
        # 同名顶层文件夹会让不同文档在库内撞成同一个键、每轮互相覆盖，建库前拒掉
        dup = pipeline.duplicate_tops(merged)
        if dup:
            names = "、".join(dup)
            print(
                f"[错误] 两个共享文件夹同名：{names}；库内会互相覆盖，请改名或改共享上级目录",
                file=sys.stderr,
            )
            return 2
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
    elif fresh:
        # 新建库：默认放行"本机所在网段"，而不是源码里那个占位网段
        from landoc.wizard import default_lan_cidrs

        cfg.allow_cidrs = default_lan_cidrs()
    deny, deny_error = _parse_optional_cidrs(getattr(args, "deny_cidrs", None))
    if deny_error:
        print(f"[错误] {deny_error}", file=sys.stderr)
        return 2
    if deny is not None:
        cfg.deny_cidrs = deny
    if args.registry_url is not None:
        cfg.registry_url = args.registry_url.strip()
    cfg.save()
    Index(cfg.db_path, md_root=cfg.md_dir).commit()
    print(f"[OK] 已建库：{cfg.name} → {lib_dir}")
    print(f"     共享目录 {len(cfg.folders)} 个：{', '.join(str(f) for f in cfg.folders) or '（未指定）'}")
    print(f"     允许网段：{', '.join(cfg.allow_cidrs)}")
    if cfg.deny_cidrs:
        print(f"     拒绝网段：{', '.join(cfg.deny_cidrs)}")
    print(f"     存储策略：{_storage_policy(cfg)}")
    if cfg.registry_url:
        print(f"     登记中心：{cfg.registry_url}")
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
    print(f"拒绝网段：{', '.join(cfg.deny_cidrs) if cfg.deny_cidrs else '（不额外拒绝）'}")
    print(f"允许名单：{_people_summary(cfg)}")
    print(f"存储策略：{_storage_policy(cfg)}")
    print(f"登记中心：{cfg.registry_url or '（未登记，同事只能靠手动地址发现）'}")
    print(f"上次扫描：{state.get('last_scan_ts', '（从未）')}")
    gone = pipeline.missing_count(cfg)
    if gone:
        print(f"源已不在：{gone} 篇（内容仍在库中，可正常检索；要清掉用 `landoc prune --gone`）")
    return 0


def _cmd_prune(args) -> int:
    """库只增不减：扫描器不再因为"源文档没了"删东西，瘦身得在这动手。"""
    cfg = Config.load(Path(args.lib_dir))
    gone = pipeline.missing_count(cfg)
    if not args.gone:
        if not gone:
            print("[提示] 没有源文件缺失的文档，库是完整的")
            return 0
        print(f"[提示] 有 {gone} 篇源文件已不在（内容仍在库中，照常可检索）")
        print("       要真删掉它们的 md 副本与索引行，加 --gone")
        return 0
    removed = pipeline.prune_gone(cfg)
    print(f"[OK] 已清理 {removed} 篇源文件已不在的文档")
    return 0


def _people_summary(cfg) -> str:
    people = getattr(cfg, "allow_people", None) or {}
    if not people:
        return "（未启用：允许网段内谁都能查）"
    with_ip = sum(1 for ip in people.values() if str(ip or "").strip())
    departments = getattr(cfg, "people_departments", None) or {}
    depts = len({str(v).strip() for v in departments.values() if str(v or "").strip()})
    extra = f"，{depts} 个部门" if depts else ""
    if with_ip == len(people):
        return f"{len(people)} 人，按 IP 放行（姓名仅备注{extra}）"
    return f"{len(people)} 人，按 IP 放行{extra}；其中 {len(people) - with_ip} 人没登记 IP、不授权"


def _cmd_people(args) -> int:
    from landoc import roster
    from landoc.people import load_people_table

    cfg = Config.load(Path(args.lib_dir))
    book = roster.Roster.from_config(cfg)
    changed = False

    if args.csv:
        try:
            people, departments, warnings = load_people_table(Path(args.csv))
        except (OSError, ValueError) as exc:
            print(f"[错误] 读不了名单：{exc}", file=sys.stderr)
            return 2
        for warning in warnings:
            print(f"[提示] {warning}")
        book, note = roster.apply_pull(
            book, roster.Roster(people, departments), replace=args.replace, origin="CSV"
        )
        print(f"[名单] {note}")
        changed = True

    if args.remove:
        if args.remove in book.people:
            book.people.pop(args.remove)
            book.departments.pop(args.remove, None)
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
            _name, clean_ip, dept = roster.validate_entry(
                name, ip, book.departments.get(name, "")
            )
        except ValueError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2
        book.people[_name] = clean_ip
        if dept:
            book.departments[_name] = dept
        print(f"[名单] {_name} → {clean_ip}")
        changed = True

    if args.dept:
        name, _, dept = args.dept.partition("=")
        name = name.strip()
        if not name:
            print("[错误] --dept 要写成 姓名=部门；部门留空表示清掉", file=sys.stderr)
            return 2
        if name not in book.people:
            print(f"[提示] 名单里没有 {name}；先用 --set 或 --csv 把人加进来")
        else:
            try:
                _name, _ip, clean_dept = roster.validate_entry(
                    name, book.people.get(name, ""), dept
                )
            except ValueError as exc:
                print(f"[错误] {exc}", file=sys.stderr)
                return 2
            if clean_dept:
                book.departments[_name] = clean_dept
                print(f"[名单] {_name} 的部门 → {clean_dept}")
            else:
                book.departments.pop(_name, None)
                print(f"[名单] 清掉 {_name} 的部门")
            changed = True

    if args.pull is not None:
        try:
            _text, incoming = roster.fetch_roster(cfg.registry_url or args.pull)
            book, note = roster.apply_pull(book, incoming, replace=args.replace)
            print(f"[拉取] {note}")
            changed = True
        except roster.RosterError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2

    if changed:
        book.to_config(cfg)
        cfg.save()

    if args.publish is not None:
        target = cfg.registry_url or args.publish
        try:
            payload = roster.publish_roster(target, book.copy())
        except roster.RosterError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            if "403" in str(exc):
                print(
                    "       名单只由管理员（登记中心那台机器）维护；"
                    "同事用 people --pull 同步中心名单即可。",
                    file=sys.stderr,
                )
            return 2
        print(f"[发布] 已发布 {payload.get('count', '?')} 人到 {target}")

    if args.export:
        from landoc.people import dump_people_csv

        Path(args.export).write_text(
            dump_people_csv(book.people, book.departments), encoding="utf-8-sig"
        )
        print(f"[导出] {args.export}（{len(book)} 人）")

    if not book:
        print("名单为空：允许网段内谁都能查。")
        return 0
    counts = book.department_counts()
    dept_line = "、".join(f"{name} {count}" for name, count in counts) if counts else ""
    print(f"共 {len(book)} 人（按 IP 授权，姓名只作备注）：")
    for name, ip, dept in roster.sorted_entries(book):
        print(f"  {(ip or '—').ljust(16)} {name.ljust(8)} {dept or ''}")
    if dept_line:
        print(f"部门分布：{dept_line}")
    print("改动即时生效：下一请求就按新名单校验（服务不用重启）。")
    return 0


def _lan_ip() -> str:
    return lan_ip()


def _cmd_update(args) -> int:
    """检查/下载新版本；`--install` 只在打包后的 exe 里可用。"""
    from landoc import updater

    registry_url = str(args.registry or "").strip()
    if not registry_url and args.lib_dir:
        registry_url = Config.load(Path(args.lib_dir)).registry_url
    if not registry_url:
        print("[错误] 没配登记中心：给 --registry 或用 init --registry-url", file=sys.stderr)
        return 2
    print(f"本机版本：v{__version__}")
    try:
        info = updater.fetch_update_info(registry_url, __version__)
    except Exception as exc:  # noqa: BLE001 - 排错命令，出错就直说
        print(f"[错误] 检查更新失败：{exc}", file=sys.stderr)
        return 2
    if info is None:
        print("已是最新版本。")
        return 0
    print(f"有新版本：v{info.version}（{info.size / 1024 / 1024:.0f} MB，{info.released_at or '发布时间未知'}）")
    for note in info.notes:
        print(f"     · {note}")
    if info.too_old:
        print(f"[提示] 版本跨度太大（最低 {info.min_from}），请手动下载完整包重装")
        return 1
    if args.check:
        return 0
    try:
        staged = updater.prepare_update(info, progress=lambda text: print(f"     {text}", flush=True))
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 下载失败：{exc}", file=sys.stderr)
        return 2
    print(f"[OK] 已下载到：{staged.app_dir}")
    if args.install:
        if not updater.install_supported():
            print("[提示] 源码运行不能替换程序目录；请在打包后的 exe 里点「升级」", file=sys.stderr)
            return 1
        from landoc.tray import acquire_single_instance

        if not acquire_single_instance():
            print("[错误] LanDocShare 正在运行：先从托盘退出，再执行 --install", file=sys.stderr)
            return 2
        target = updater.current_install_dir()
        if target is None:
            print("[错误] 找不到程序目录", file=sys.stderr)
            return 2
        updater.spawn_update(staged, target=target)
        print("[OK] 更新器已启动，本进程退出")
        return 0
    print("提示：右键托盘图标 →「升级到 v…」即可完成安装（自动重启，失败会回滚）。")
    return 0


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
        help="允许访问的网段，逗号分隔（默认 172.16.0.0/16；想只给本院就填 172.16.1.0/24）；不给则沿用现有配置",
    )
    init.add_argument(
        "--deny-cidrs",
        default=None,
        metavar="CIDR[,CIDR…]",
        help="显式拒绝的网段，逗号分隔（优先于允许网段；留空或填“无”表示清空）；不给则沿用现有配置",
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
    init.add_argument(
        "--registry-url",
        default=None,
        metavar="URL",
        help="登记中心地址（如 http://172.16.1.197:8767）；给了才会每 60 s 心跳登记，不给则保持原值",
    )
    init.set_defaults(func=_cmd_init)

    probe_cmd = sub.add_parser("probe", help="抽样体检：明/密/受阻比例")
    probe_cmd.add_argument("--lib-dir")
    probe_cmd.add_argument("--folder", action="append")
    probe_cmd.add_argument("--n", type=int, default=200)
    probe_cmd.set_defaults(func=_cmd_probe)

    convert = sub.add_parser("convert", help="转换一批文档")
    convert.add_argument("--lib-dir", required=True)
    convert.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本轮最多尝试转换的篇数（失败/超时也算一篇）",
    )
    convert.set_defaults(func=_cmd_convert)

    serve = sub.add_parser("serve", help="启动只读检索服务")
    serve.add_argument("--lib-dir", required=True)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=_cmd_serve)

    status = sub.add_parser("status", help="查看库统计")
    status.add_argument("--lib-dir", required=True)
    status.set_defaults(func=_cmd_status)

    prune = sub.add_parser(
        "prune", help="清理「源文件已不在」的文档（库只增不减，瘦身得人点头）"
    )
    prune.add_argument("--lib-dir", required=True)
    prune.add_argument("--gone", action="store_true", help="真的删掉（md 副本 + 索引行）")
    prune.set_defaults(func=_cmd_prune)

    people_cmd = sub.add_parser("people", help="查看/维护允许名单（姓名 → IP → 部门）")
    people_cmd.add_argument("--lib-dir", required=True)
    people_cmd.add_argument("--csv", help="从 CSV 导入名单（列：姓名,IP,部门，部门可省，可有表头）")
    people_cmd.add_argument("--replace", action="store_true", help="以 CSV/中心为准整体替换，而不是合并")
    people_cmd.add_argument("--set", metavar="姓名=IP", help="新增/修改一条；IP 必填（姓名不能当访问凭证）")
    people_cmd.add_argument(
        "--dept", metavar="姓名=部门", help="给某人标部门/备注（部门留空表示清掉）"
    )
    people_cmd.add_argument("--remove", metavar="姓名", help="删除一条")
    people_cmd.add_argument("--export", metavar="PATH", help="导出成 CSV（以后给客户端分发）")
    people_cmd.add_argument(
        "--pull",
        nargs="?",
        const="",
        metavar="URL",
        help="从登记中心拉取名单（默认用库配置里的 registry_url）",
    )
    people_cmd.add_argument(
        "--publish",
        nargs="?",
        const="",
        metavar="URL",
        help="把名单发布到登记中心（默认用库配置里的 registry_url）",
    )
    people_cmd.set_defaults(func=_cmd_people)

    update_cmd = sub.add_parser("update", help="检查/下载新版本（--install 仅打包后可用）")
    update_cmd.add_argument("--lib-dir", help="库目录（用里面的 registry_url）")
    update_cmd.add_argument("--registry", default=None, help="登记中心地址，默认取库配置里的 registry_url")
    update_cmd.add_argument("--check", action="store_true", help="只检查版本，不下载")
    update_cmd.add_argument("--install", action="store_true", help="下载后立即安装并重启（需先退出托盘程序）")
    update_cmd.set_defaults(func=_cmd_update)

    # 登记中心的参数**原样透传**：`landoc registry …` 之后的一切都交给 registry.main 自己解析
    # （以前是这边逐条搬 13 个参数，中心一加参数两边就得同步改，`--roster` 就这么漏过一次）。
    # 真正的转发在 `main()` 开头做（argparse 的 REMAINDER 遇到 `--xxx` 会失灵），这里只占个位置，
    # 让 `landoc --help` 里仍能看到这个子命令。
    registry_cmd = sub.add_parser(
        "registry",
        help="启动登记中心（让同事们互相发现彼此的库）",
        description="参数原样转发给登记中心，例如 `landoc registry --port 8767 --watchdog`；"
        "`landoc registry --help` 看完整列表。",
        add_help=False,
    )
    registry_cmd.set_defaults(func=_cmd_registry)
    return parser


def _cmd_registry(args) -> int:
    from landoc import registry

    return registry.main([])


def main(argv: list[str] | None = None) -> int:
    # 控制台是 GBK 时，版本说明里的怪字符不该让整条命令崩掉（打包版尤其明显）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 老环境/被重定向就算了
            pass
    words = list(sys.argv[1:] if argv is None else argv)
    # `registry` 之后的一切原样交给登记中心自己的 argparse：这边不再维护一份参数表，
    # 中心加了参数也不用两边同步（argparse 的 REMAINDER 在这里吃不住 --xxx，只能先切）
    if words and words[0] == "registry":
        from landoc import registry

        return registry.main(words[1:])
    args = build_parser().parse_args(words)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
