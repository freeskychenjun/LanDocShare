"""转换调度器：扫描 → 体检分流 → 转换 → 落 md → 写索引 → 打检查点。

设计要点（设计文档 §6.3/§6.7）：
- 选路按"容器格式 + 是否加密"决定，不只看加密：`.doc/.wps/.ppt/.dps` 是 OLE 容器，
  python 的 docx/pptx 解析器不认，一律走 Office COM；
- 一批文档共用同一个 Office 会话（`OfficeSession`），不然每篇白付 6~9 秒启动；
- 单篇失败只记账，不中断整批；失败的也记账，避免每轮重付超时；
- 增量靠源文件 mtime+size（`state.json`）；**库只增不减**：单篇源文档消失（删了/改名/移走、
  或共享盘暂时没插）只记一笔 `source_missing`，md 与索引都留着；只有文件夹被移出
  `config.folders`（明确撤回）才删，想瘦身用 `prune_gone()`（`landoc prune --gone`）；
- 检查点在 `finally` 写，断电/中断后能接着干；
- `store_content` / `render_md`（设计 §6.6 的收紧开关）在落盘这一步生效：不落 md 就删掉旧副本，
  只存片段就把写进索引的正文截到 `SNIPPET_ONLY_CHARS`（开关翻转后靠"未收紧/缺副本"触发重扫）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from landoc import office_bridge, pdf_conv, pptx_conv, probe, render_docx

STATE_NAME = "state.json"
FAILURES_NAME = "failures.json"

WRITER_EXTS = {".doc", ".docx", ".wps"}
PPT_EXTS = {".ppt", ".pptx", ".dps"}
PDF_EXTS = {".pdf"}
SKIP_EXTS = {".xls", ".xlsx", ".et"}

MAX_ATTEMPTS = 3  # 同一份文件连续失败超过这个次数就不再自动重试
# 一个 Acrobat 会话连转多少篇就换新的：复用进程把加密 PDF 从 6.2 秒/篇降到 0.73 秒/篇，
# 代价是 Acrobat 内存随篇数线性上涨（≈0.44 MB/篇，实测 100 篇 +44 MB），
# 换一个会话只花一次 4.5 秒的启动，所以每 500 篇回收一次（千篇成本 9 秒）。
PDF_SESSION_MAX_DOCS = 500
# 装/卸 Office、Acrobat 之后，旧的"没有可用的转换器"失败记录必须作废，否则同事机器
# 补装完也永远不重试。只清这两类：它们说的是"机器缺东西"，不是"这份文件坏"。
ENV_REASONS = frozenset({"no_office", "no_reader"})

# 一轮里"转了多少篇就落一次盘"：索引提交便宜（20 篇一次实测比整轮单事务只多 15%），
# 但 state.json 是整份重写（5 万篇时 ≈6 MB），所以它按时间落（60 秒），索引按篇数落。
# 中断时 finally 会补一次全量落盘；中断最多白转"上一次落盘之后"那几篇。
COMMIT_EVERY = 20
STATE_SECONDS = 60.0
RATE_EWMA = 0.5  # 秒/篇速率的滑动平均权重：让 ETA 不因一篇超时而跳变
# 规划阶段的 stat 是纯 I/O：7,468 篇串行 471 ms、4 条线程 220 ms（G:\ 实测）。
# 文件太少就不开线程池——起线程与排队的成本会把它吃回去。
STAT_WORKERS = 4
STAT_PARALLEL_MIN = 200

# store_content=False 时保留的正文前缀（设计 §6.6 "仅存片段"）。取值越大越保检索召回，
# 越小越防"整库明文被拷走"；本库的取舍是召回优先，默认给足 20000 字（多数简报/汇报/规程
# 能整篇留下），单个库也能用 `init --snippet-only-chars` 再调。
# 注意：截断之外的正文搜不到 —— 这是该开关的固有代价，不要"顺手补全"。
DEFAULT_SNIPPET_ONLY_CHARS = 20000
MIN_SNIPPET_ONLY_CHARS = 200  # 再小就等于"只能搜标题"，没有意思


@dataclass
class RunReport:
    converted: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failed: list[tuple[str, str]] = field(default_factory=list)
    md_paths: list[str] = field(default_factory=list)
    # 进度口径（状态窗口与 ETA 用）：total_docs = 库里可转换的篇数，
    # pending = 本轮开始时还差多少篇，attempted = 本轮真去转了几篇（成功+失败都算）。
    total_docs: int = 0
    pending: int = 0
    attempted: int = 0
    elapsed: float = 0.0
    rate: float = 0.0  # 秒/篇（本轮实测，写进 state 给下一轮继续用）
    stop_reason: str = ""  # "" / limit / budget / stop / done

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def remaining(self) -> int:
        """本轮结束时还剩多少篇没转（不含因失败被跳过、已到上限的）。"""
        return max(0, self.pending - self.attempted)

    @property
    def eta_seconds(self) -> float:
        return self.remaining * self.rate if self.rate else 0.0


def _top_name(folder: Path) -> str:
    """库内相对路径的顶层名：盘符根目录（folder.name == ""）时退化为盘符。"""
    name = (folder.name or "").strip()
    if name:
        return name
    drive = (folder.drive or "").rstrip(":")
    return drive or "root"


def _md_rel(rel: str) -> str:
    """md 相对路径：保留原扩展名，避免 `报告.docx` 与 `报告.pdf` 互相覆盖。"""
    return rel + ".md"


def _configured_tops(folders) -> set[str]:
    """共享列表里每个文件夹的顶层名，**不管它当下能不能访问**。

    撤不撤回只看这个集合：文件夹被移出共享列表才算"明确撤回"；文件夹只是暂时
    不可达（网盘没连、移动硬盘没插）不算 —— 早先按"这一轮没扫到"就删，一次扫不到
    就会把整个库的 md 和索引清空。
    """
    tops: set[str] = set()
    for folder in folders or []:
        try:
            tops.add(_top_name(Path(folder).expanduser().resolve()))
        except (OSError, RuntimeError):
            continue
    return tops


def snippet_cap(cfg) -> int:
    """当前库"仅存片段"的前缀长度；配置缺失/太小/非法时退回默认值。"""
    try:
        cap = int(getattr(cfg, "snippet_only_chars", DEFAULT_SNIPPET_ONLY_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_SNIPPET_ONLY_CHARS
    return cap if cap >= MIN_SNIPPET_ONLY_CHARS else DEFAULT_SNIPPET_ONLY_CHARS


def _prefix(md: str, cap: int) -> str:
    """截到 cap 字，尽量停在行边界，免得把表格行/段落切成两半。"""
    if len(md) <= cap:
        return md
    cut = md.rfind("\n", 0, cap)
    return md[:cut] if cut > cap // 2 else md[:cap]


def _within_snippet_cap(index, rel: str, cap: int) -> bool:
    """`store_content=False` 时，索引里的旧行是否已经收紧到 cap 字以内。

    替身 index 没有 `content_length` → 视为已收紧（隐私开关只在真库上需要生效）。
    """
    length_of = getattr(index, "content_length", None)
    if length_of is None:
        return True
    length = length_of(_md_rel(rel))
    return length is not None and length <= cap


def _is_ooxml(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def _state_path(cfg) -> Path:
    return Path(cfg.lib_dir) / STATE_NAME


def _read_state(cfg) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_state(cfg, state: dict) -> None:
    _write_json_atomic(_state_path(cfg), state)


def _write_failures(cfg, failures: dict) -> None:
    """把失败清单镜像到库目录（给人看的，真相在 state.json 里）。

    镜像是 state 的副本，凡改动 `state["failures"]` 的地方（扫描落盘、prune）
    都得一起刷，否则镜像里会留着已经清掉的死条目。
    """
    _write_json_atomic(
        Path(cfg.lib_dir) / FAILURES_NAME,
        {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "failures": failures},
    )


def env_signature() -> dict:
    """本机"有没有可用的转换器"的指纹（注册表查询，毫秒级，不启动任何进程）。

    存进 state.json 之后，下一轮只要指纹变了就知道同事刚装了/卸了 Office 或 Acrobat，
    可以放心把 no_office / no_reader 的失败记录作废重试。
    """
    def _probe(name: str) -> bool:
        fn = getattr(probe, name, None)
        if fn is None:  # 探针被裁掉（或非 Windows）时按"有"处理，别凭空清空失败记录
            return True
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 - 探测失败不该拦住建库
            return True

    return {"office": _probe("has_office"), "acrobat": _probe("has_acrobat")}


def _drop_stale_env_failures(state: dict) -> int:
    """环境指纹变了就丢掉 no_office / no_reader 的失败记录，返回丢掉的条数。"""
    current = env_signature()
    previous = state.get("env")
    state["env"] = current
    if not isinstance(previous, dict) or not previous:
        return 0  # 第一次记账（老版本升级上来也一样）：只记指纹，不动历史失败
    if previous == current:
        return 0
    failures = state.get("failures") or {}
    stale = [rel for rel, item in failures.items() if str(item.get("reason", "")) in ENV_REASONS]
    for rel in stale:
        failures.pop(rel, None)
    return len(stale)


def _stat_one(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _stat_many(paths: list[Path]) -> list[os.stat_result | None]:
    """批量 stat，结果与 `paths` 一一对应；读不到的给 None。

    这一批 stat 在规划阶段占大头（7,468 篇 471 ms），而 stat 是纯 I/O、彼此独立，
    分给几条线程就能压到 220 ms。文件少时直接串行：线程池的启动与排队成本会吃回去。
    """
    if len(paths) < STAT_PARALLEL_MIN:
        return [_stat_one(path) for path in paths]
    out: list[os.stat_result | None] = [None] * len(paths)

    def work(item: tuple[int, Path]) -> None:
        position, path = item
        out[position] = _stat_one(path)

    with ThreadPoolExecutor(max_workers=STAT_WORKERS) as pool:
        list(pool.map(work, enumerate(paths), chunksize=64))
    return out


def _iter_docs(folders) -> list[tuple[Path, str]]:
    """返回 (绝对路径, 库内相对路径)；相对路径带顶层文件夹名，避免同名目录互相覆盖。"""
    out: list[tuple[Path, str]] = []
    for folder in folders:
        # 必须是绝对路径：COM 的 Documents.Open 会把相对路径解析到 Office 自己的工作目录，
        # WPS 直接报"文档打开失败（3010）"。
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            continue
        top = _top_name(folder)
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [
                d for d in dirnames if d not in probe.SKIP_DIR_NAMES and not d.startswith(".")
            ]
            for name in filenames:
                if name.startswith("~$"):
                    continue
                ext = Path(name).suffix.lower()
                if ext not in WRITER_EXTS | PPT_EXTS | PDF_EXTS | SKIP_EXTS:
                    continue
                absolute = Path(dirpath) / name
                rel = f"{top}/{absolute.relative_to(folder).as_posix()}".lstrip("/")
                out.append((absolute, rel))
    return sorted(out, key=lambda item: item[1])


def convert_one(
    path: Path,
    ext: str,
    encrypted: bool,
    cfg,
    writer_session=None,
    ppt_session=None,
    pdf_session=None,
):
    """返回 (markdown 文本, 使用的通道)。失败抛异常，由调用方记账。"""
    if ext in WRITER_EXTS:
        if encrypted or not _is_ooxml(path):
            xml = office_bridge.extract_wordopenxml(path, session=writer_session)
            return render_docx.docx_bytes_to_md(office_bridge.flat_opc_to_docx_bytes(xml)), "com"
        return render_docx.docx_bytes_to_md(path.read_bytes()), "direct"

    if ext in PPT_EXTS:
        need_com = encrypted or not _is_ooxml(path)
        md = pptx_conv.pptx_to_md(
            path, encrypted=need_com, session=ppt_session if need_com else None
        )
        return md, "com" if need_com else "direct"

    if ext in PDF_EXTS:
        result = pdf_conv.pdf_to_text(path, cfg=cfg, session=pdf_session)
        # 扫描件取不到文本层时 pdf_to_text 不标 skipped，但正文本就是空白；
        # 记 no_text_layer 比记 empty_text 更能说明"这是图片型 PDF，一期不 OCR"。
        if result.via == "skipped" or result.reason:
            raise RuntimeError(result.reason or "pdf_skipped")
        return result.text, result.via

    raise RuntimeError("unsupported")


def _write_md(cfg, rel: str, md: str, source_path: str) -> str:
    target = Path(cfg.md_dir) / _md_rel(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    front = (
        "---\n"
        f"source_path: {source_path}\n"
        f"converted_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        "---\n\n"
    )
    fd, tmp = tempfile.mkstemp(prefix=".md", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(front + md)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target.as_posix()


def run(
    cfg,
    index=None,
    limit: int | None = None,
    budget: float | None = None,
    should_stop=None,
    on_progress=None,
):
    """转换一批文档。`index` 需提供 upsert/delete/commit（真 Index 或测试替身）。

    一轮的边界由三样东西决定（都可选，叠加生效）：
    - `limit`：**尝试**转多少篇就停（成功、失败、超时都算一篇）。早先是"成功几篇"，
      那样一批全是带口令的坏文件时单次 run 会跑十几个小时；
    - `budget`：最多用多少秒（每篇开始前看一眼表，单篇自身仍受 120 秒超时约束）；
    - `should_stop()`：返回真就停（库端用它实现"用户一回来就收工"）。
    停在哪由 `report.stop_reason` 说明，进度每隔 `COMMIT_EVERY` 篇/`STATE_SECONDS` 秒
    落一次盘，并通过 `on_progress(report)` 通知调用方（状态窗口据此显示剩余与 ETA）。
    """
    if index is None:  # 延迟导入：Task 6 先于 Task 7 落地
        from landoc.index import Index

        index = Index(cfg.db_path, md_root=cfg.md_dir)

    report = RunReport()
    state = _read_state(cfg)
    files: dict = state.setdefault("files", {})
    failures: dict = state.setdefault("failures", {})
    # 同事补装了 Office/Acrobat：旧的"没有可用转换器"失败记录要作废，否则永远不重试
    _drop_stale_env_failures(state)
    writer_session = None
    ppt_session = None
    pdf_session = None
    render_md = getattr(cfg, "render_md", True)
    store_content = getattr(cfg, "store_content", True)
    cap = snippet_cap(cfg)
    if not render_md:
        # 不落 md：上一轮留下的副本是明文全文，必须清掉，否则这个开关等于没开
        shutil.rmtree(Path(cfg.md_dir), ignore_errors=True)
    started = time.monotonic()
    last_state = started
    docs = _iter_docs(cfg.folders)

    def _drop_session(kind: str) -> None:
        nonlocal writer_session, ppt_session, pdf_session
        if kind == "writer":
            if writer_session is not None:
                writer_session.close()
            writer_session = None
        elif kind == "pdf":
            if pdf_session is not None:
                pdf_session.close()
            pdf_session = None
        else:
            if ppt_session is not None:
                ppt_session.close()
            ppt_session = None

    def _flush(force: bool = False) -> None:
        """落盘：索引每 20 篇提交一次；state/failures 按时间（整份重写，别太勤）。"""
        nonlocal last_state
        index.commit()
        now = time.monotonic()
        if force or now - last_state >= STATE_SECONDS:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            state["last_scan_ts"] = stamp
            _write_state(cfg, state)
            _write_failures(cfg, failures)
            last_state = now
        if on_progress is not None:
            try:
                on_progress(report)
            except Exception:  # noqa: BLE001 - 进度回调失败不该拖垮建库
                pass

    # 先规划一轮要干什么：跳过的先记掉，剩下的才是"本轮要做的事"。
    # 这样 pending 一开始就是确定的（limit/budget 有了明确单位），状态窗口也能显示
    # "还剩多少篇"，而不是只有一个滚动的"已转换 N 篇"。
    pending: list[tuple[Path, str, str, os.stat_result]] = []
    wanted: list[tuple[Path, str, str]] = []
    for path, rel in docs:
        ext = path.suffix.lower()
        if ext in SKIP_EXTS:
            report.skip("xls")
            continue
        report.total_docs += 1
        wanted.append((path, rel, ext))

    stats = _stat_many([item[0] for item in wanted])
    for (path, rel, ext), stat in zip(wanted, stats):
        if stat is None:
            report.failed.append((rel, "unreadable"))
            continue

        known = files.get(rel)
        # 重新打开"落 md"、或副本被手工删掉时，要补回一份（只是重转这一篇）
        md_missing = render_md and not (Path(cfg.md_dir) / _md_rel(rel)).exists()
        unchanged = bool(
            known
            and known.get("mtime") == stat.st_mtime
            and known.get("size") == stat.st_size
            and not md_missing
            and (store_content or _within_snippet_cap(index, rel, cap))
        )
        if unchanged:
            # 源文档回来了（盘插上了、文件改回原名）：把"源已不在"的记号撤掉
            if isinstance(known, dict):
                known.pop("source_missing", None)
                known.pop("missing_since", None)
                # 文件夹重新加回共享：撤回标记一并撤掉，同事又能看到
                if known.pop("withdrawn", None):
                    known.pop("withdrawn_since", None)
                    mark = getattr(index, "mark_withdrawn", None)
                    if mark is not None:
                        mark(_md_rel(rel), False)
            report.skip("unchanged")
            continue

        failed_before = failures.get(rel)
        if failed_before:
            # 两种跳过：这份文件还是上次失败的那个版本（I3：未变的文件不重复付超时）；
            # 或者它已经失败到上限了。
            # （曾经写成 `attempts < MAX_ATTEMPTS` 参与与运算，等于第 3 次之后
            #  条件恒假 —— 坏了/带口令的文件每个空闲窗口都重付一次超时。）
            attempts = int(failed_before.get("attempts", 0) or 0)
            same_version = (
                failed_before.get("mtime") == stat.st_mtime
                and failed_before.get("size") == stat.st_size
            )
            if same_version or attempts >= MAX_ATTEMPTS:
                report.skip("known_failure")
                continue
        pending.append((path, rel, ext, stat))

    report.pending = len(pending)
    since_flush = 0
    # 速率只算"真在转"的时间：规划阶段扫目录/stat 与转换速度无关，混进去会让 ETA 偏大。
    # 每轮第一篇还要替整轮付一次 Office/Acrobat 启动（4.5~9 秒），那一篇也不计入速率——
    # 否则一轮只转 10 篇时，ETA 会被这 9 秒永久地抬高约一倍。
    doc_started: float | None = None
    doc_seconds = 0.0
    first_doc_done = False

    def _settle_doc() -> None:
        """结算上一篇的耗时（continue/异常都算），第一篇只标记不累计。"""
        nonlocal doc_started, doc_seconds, first_doc_done
        if doc_started is None:
            return
        spent = time.monotonic() - doc_started
        doc_started = None
        if first_doc_done:
            doc_seconds += spent
        else:
            first_doc_done = True

    try:
        for path, rel, ext, stat in pending:
            _settle_doc()
            if since_flush >= COMMIT_EVERY or time.monotonic() - last_state >= STATE_SECONDS:
                _flush()
                since_flush = 0
            if should_stop is not None and should_stop():
                report.stop_reason = "stop"
                break
            if limit is not None and report.attempted >= limit:
                report.stop_reason = "limit"
                break
            if budget is not None and time.monotonic() - started >= budget:
                report.stop_reason = "budget"
                break
            report.attempted += 1
            doc_started = time.monotonic()
            failed_before = failures.get(rel)
            kind = probe.classify(path)
            if kind == "blocked":
                report.failed.append((rel, "unreadable"))
                failures[rel] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "reason": "unreadable",
                    "attempts": failed_before.get("attempts", 0) + 1 if failed_before else 1,
                    "last_try": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                continue

            needs_com = kind == "encrypted" or (
                ext in WRITER_EXTS | PPT_EXTS and not _is_ooxml(path)
            )
            is_ppt = ext in PPT_EXTS
            is_writer = ext in WRITER_EXTS
            is_pdf = ext in PDF_EXTS
            if needs_com:
                # 只给真正会用到的通道起会话：加密 PDF 不需要 Word/PPT，别白付一次启动
                if is_ppt and ppt_session is None:
                    ppt_session = office_bridge.OfficeSession(dispatch=pptx_conv._dispatch_ppt)
                if is_writer and writer_session is None:
                    writer_session = office_bridge.OfficeSession()
            if is_pdf and kind == "encrypted":
                # 加密 PDF 走 Acrobat：一个会话连着转（6.2 秒/篇 → 0.73 秒/篇），
                # 内存随时间涨，所以每 PDF_SESSION_MAX_DOCS 篇换一个新的
                if pdf_session is not None and pdf_session.used >= PDF_SESSION_MAX_DOCS:
                    _drop_session("pdf")
                if pdf_session is None and pdf_conv.has_acrobat():
                    pdf_session = pdf_conv.PDFSession()

            # 超时/不可用时该丢哪个会话：PDF 走 Acrobat，其余按 Office 的两种分
            _com_kind = "pdf" if is_pdf else ("ppt" if is_ppt else "writer")

            try:
                md, via = convert_one(
                    path,
                    ext,
                    kind == "encrypted",
                    cfg,
                    writer_session=writer_session,
                    ppt_session=ppt_session,
                    pdf_session=pdf_session,
                )
            except office_bridge.OfficeUnavailable:
                report.failed.append((rel, "no_office"))
                failures[rel] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "reason": "no_office",
                    "attempts": failed_before.get("attempts", 0) + 1 if failed_before else 1,
                    "last_try": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                _drop_session(_com_kind)
                continue
            except office_bridge.OfficeTimeout:
                report.failed.append((rel, "timeout"))
                failures[rel] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "reason": "timeout",
                    "attempts": failed_before.get("attempts", 0) + 1 if failed_before else 1,
                    "last_try": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                _drop_session(_com_kind)
                continue
            except office_bridge.PasswordProtected:
                report.failed.append((rel, "password_protected"))
                failures[rel] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "reason": "password_protected",
                    "attempts": failed_before.get("attempts", 0) + 1 if failed_before else 1,
                    "last_try": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                _drop_session(_com_kind)
                continue
            except pptx_conv.PptxError:
                report.failed.append((rel, "parse_failed"))
                failures[rel] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "reason": "parse_failed",
                    "attempts": failed_before.get("attempts", 0) + 1 if failed_before else 1,
                    "last_try": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                continue
            except Exception as exc:  # noqa: BLE001 - 单篇失败不拖垮整批
                reason = str(exc) or exc.__class__.__name__
                report.failed.append((rel, reason))
                failures[rel] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "reason": reason,
                    "attempts": failed_before.get("attempts", 0) + 1 if failed_before else 1,
                    "last_try": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                continue

            if not md.strip():
                report.failed.append((rel, "empty_text"))
                failures[rel] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "reason": "empty_text",
                    "attempts": failed_before.get("attempts", 0) + 1 if failed_before else 1,
                    "last_try": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                continue

            try:
                if render_md:
                    _write_md(cfg, rel, md, str(path))
                index.upsert(
                    _md_rel(rel),
                    Path(rel).stem,
                    ext.lstrip("."),
                    str(path),
                    stat.st_size,
                    stat.st_mtime,
                    md if store_content else _prefix(md, cap),
                )
            except OSError:
                report.failed.append((rel, "write_failed"))
                continue
            except Exception as exc:  # noqa: BLE001 - sqlite 写入失败同样只记账
                report.failed.append((rel, exc.__class__.__name__.lower()))
                continue

            report.converted += 1
            report.md_paths.append(rel)
            files[rel] = {"mtime": stat.st_mtime, "size": stat.st_size, "via": via}
            failures.pop(rel, None)
            since_flush += 1

        # 库里那份 md 副本才是这个库的全部家当，源文档只是"当初从哪来的"一条线索。
        # 所以扫描器只做两件事：
        #   1. 文件夹被移出共享列表（config.folders 里没了）= 撤回共享，但库不删：
        #      md 与索引都留着，只把 withdrawn 标成 1；服务端只让本机（主人）看见。
        #      重新加回共享文件夹时，下面的 unchanged 分支会把标记清掉。
        #   2. 单篇源文件消失（删了/改名/移走/文件夹暂时没插）= 保留，记一笔 source_missing。
        # 早先只要"这一轮没扫到"就删，导致拔了移动硬盘启动托盘就把整个库清空。
        current = {rel for _, rel in docs}
        configured = _configured_tops(cfg.folders)
        for rel in list(files):
            if rel in current:
                continue
            top = rel.split("/", 1)[0]
            if top in configured:
                if isinstance(files[rel], dict) and not files[rel].get("source_missing"):
                    files[rel]["source_missing"] = True
                    files[rel]["missing_since"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    report.skip("source_missing")
                continue
            item = files.get(rel)
            if not isinstance(item, dict):
                item = {}
                files[rel] = item
            item["withdrawn"] = True
            item.setdefault("withdrawn_since", time.strftime("%Y-%m-%d %H:%M:%S"))
            mark = getattr(index, "mark_withdrawn", None)
            if mark is not None:
                mark(_md_rel(rel), True)
            report.skip("withdrawn")
        if not report.stop_reason:
            report.stop_reason = "done"
    finally:
        _settle_doc()
        report.elapsed = time.monotonic() - started
        prev = float(state.get("rate_s_per_doc") or 0.0)
        steady_docs = report.attempted - 1
        sample = doc_seconds / steady_docs if steady_docs > 0 else 0.0
        if sample <= 0 and prev <= 0 and report.attempted:
            # 第一轮就只转了一篇：只拿得到含启动开销的整轮时间，先这么估
            sample = report.elapsed / report.attempted
        if sample > 0:
            state["rate_s_per_doc"] = round(
                prev * (1 - RATE_EWMA) + sample * RATE_EWMA if prev else sample, 3
            )
        report.rate = float(state.get("rate_s_per_doc") or 0.0)
        _drop_session("writer")
        _drop_session("ppt")
        _drop_session("pdf")
        _flush(force=True)
        try:
            index.set_meta("last_scan", state["last_scan_ts"])
        except Exception:  # noqa: BLE001 - 替身 index 不一定支持 meta
            pass
    return report


def missing_count(cfg) -> int:
    """库里有多少篇"源文件已不在、内容仍在库中"的文档（状态窗口那一行用它）。"""
    files = _read_state(cfg).get("files") or {}
    return sum(
        1 for item in files.values() if isinstance(item, dict) and item.get("source_missing")
    )


def prune_gone(cfg, index=None) -> int:
    """删掉"源文件已不在"的文档：md 副本 + 索引行 + state 记录，返回删了几篇。

    这是删库的唯一出口。扫描器自己不再删东西 —— 库要能拷到另一台机器上、不拷源文档
    照样用，所以"瘦身"得是有人明确点头的动作（`landoc prune --gone`）。
    """
    if index is None:
        from landoc.index import Index

        index = Index(cfg.db_path, md_root=cfg.md_dir)
    state = _read_state(cfg)
    files = state.get("files") or {}
    failures = state.get("failures")
    gone = [
        rel
        for rel, item in list(files.items())
        if isinstance(item, dict) and item.get("source_missing")
    ]
    for rel in gone:
        try:
            (Path(cfg.md_dir) / _md_rel(rel)).unlink()
        except OSError:
            pass
        try:
            index.delete(_md_rel(rel))
        except Exception:  # noqa: BLE001 - 替身 index 可能没有 delete
            pass
        files.pop(rel, None)
        # 转换失败记录跟着走：库都删了，这条失败没有重试的对象，
        # 留着只会让"失败 N 篇"永远清不掉。
        if isinstance(failures, dict):
            failures.pop(rel, None)
    if gone:
        try:
            index.commit()
        except Exception:  # noqa: BLE001
            pass
        _write_state(cfg, state)
        if isinstance(failures, dict):
            _write_failures(cfg, failures)
    return len(gone)
