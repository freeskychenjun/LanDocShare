"""转换调度器：扫描 → 体检分流 → 转换 → 落 md → 写索引 → 打检查点。

设计要点（设计文档 §6.3/§6.7）：
- 选路按"容器格式 + 是否加密"决定，不只看加密：`.doc/.wps/.ppt/.dps` 是 OLE 容器，
  python 的 docx/pptx 解析器不认，一律走 Office COM；
- 一批文档共用同一个 Office 会话（`OfficeSession`），不然每篇白付 6~9 秒启动；
- 单篇失败只记账，不中断整批；失败的也记账，避免每轮重付超时；
- 增量靠源文件 mtime+size（`state.json`），源文件被移出共享目录 = 撤回共享 → 连 md 与索引一起清；
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

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


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


def convert_one(path: Path, ext: str, encrypted: bool, cfg, writer_session=None, ppt_session=None):
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
        result = pdf_conv.pdf_to_text(path, cfg=cfg)
        if result.via == "skipped":
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


def run(cfg, index=None, limit: int | None = None) -> RunReport:
    """转换一批文档。`index` 需提供 upsert/delete/commit（真 Index 或测试替身）。"""
    if index is None:  # 延迟导入：Task 6 先于 Task 7 落地
        from landoc.index import Index

        index = Index(cfg.db_path, md_root=cfg.md_dir)

    report = RunReport()
    state = _read_state(cfg)
    files: dict = state.setdefault("files", {})
    failures: dict = state.setdefault("failures", {})
    processed = 0
    writer_session = None
    ppt_session = None
    render_md = getattr(cfg, "render_md", True)
    store_content = getattr(cfg, "store_content", True)
    cap = snippet_cap(cfg)
    if not render_md:
        # 不落 md：上一轮留下的副本是明文全文，必须清掉，否则这个开关等于没开
        shutil.rmtree(Path(cfg.md_dir), ignore_errors=True)
    docs = _iter_docs(cfg.folders)

    def _drop_session(kind: str) -> None:
        nonlocal writer_session, ppt_session
        if kind == "writer":
            if writer_session is not None:
                writer_session.close()
            writer_session = None
        else:
            if ppt_session is not None:
                ppt_session.close()
            ppt_session = None

    try:
        for path, rel in docs:
            if limit is not None and processed >= limit:
                break
            ext = path.suffix.lower()
            if ext in SKIP_EXTS:
                report.skip("xls")
                continue

            try:
                stat = path.stat()
            except OSError:
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
                report.skip("unchanged")
                continue

            failed_before = failures.get(rel)
            if (
                failed_before
                and failed_before.get("mtime") == stat.st_mtime
                and failed_before.get("size") == stat.st_size
                and failed_before.get("attempts", 0) < MAX_ATTEMPTS
            ):
                report.skip("known_failure")
                continue

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
            if needs_com:
                if is_ppt and ppt_session is None:
                    ppt_session = office_bridge.OfficeSession(dispatch=pptx_conv._dispatch_ppt)
                if not is_ppt and writer_session is None:
                    writer_session = office_bridge.OfficeSession()

            try:
                md, via = convert_one(
                    path,
                    ext,
                    kind == "encrypted",
                    cfg,
                    writer_session=writer_session,
                    ppt_session=ppt_session,
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
                _drop_session("ppt" if is_ppt else "writer")
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
                _drop_session("ppt" if is_ppt else "writer")
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
                _drop_session("ppt" if is_ppt else "writer")
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
            processed += 1

        # 源文件被移出共享目录 = 撤回共享：md 与索引一起清
        current = {rel for _, rel in docs}
        for rel in [r for r in files if r not in current]:
            md_path = Path(cfg.md_dir) / _md_rel(rel)
            try:
                md_path.unlink()
            except OSError:
                pass
            try:
                index.delete(_md_rel(rel))
            except Exception:  # noqa: BLE001 - 替身 index 可能没有 delete
                pass
            files.pop(rel, None)
            failures.pop(rel, None)
            report.skip("withdrawn")
    finally:
        _drop_session("writer")
        _drop_session("ppt")
        index.commit()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        state["last_scan_ts"] = stamp
        _write_state(cfg, state)
        _write_json_atomic(
            Path(cfg.lib_dir) / FAILURES_NAME,
            {"generated_at": stamp, "failures": failures},
        )
        try:
            index.set_meta("last_scan", stamp)
        except Exception:  # noqa: BLE001 - 替身 index 不一定支持 meta
            pass
    return report
