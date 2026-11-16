"""转换调度器：扫描 → 体检分流 → 转换 → 落 md → 写索引 → 打检查点。

设计要点（设计文档 §6.3/§6.7）：
- 分流按文件头，不按扩展名猜；
- 单文档失败只记账，不中断整批；
- 增量靠源文件 mtime+size（`state.json`），没变化就跳过；
- 检查点在 `finally` 写，断电/中断后能接着干，不会重头再来。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from landoc import office_bridge, pdf_conv, pptx_conv, probe, render_docx

STATE_NAME = "state.json"

DOCX_EXTS = {".doc", ".docx", ".wps"}
PPT_EXTS = {".ppt", ".pptx", ".dps"}
PDF_EXTS = {".pdf"}
SKIP_EXTS = {".xls", ".xlsx", ".et"}


@dataclass
class RunReport:
    converted: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failed: list[tuple[str, str]] = field(default_factory=list)
    md_paths: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _state_path(cfg) -> Path:
    return Path(cfg.lib_dir) / STATE_NAME


def _read_state(cfg) -> dict:
    path = _state_path(cfg)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(cfg, state: dict) -> None:
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _iter_docs(folders) -> list[tuple[Path, str]]:
    """返回 (绝对路径, 库内相对路径)；相对路径带顶层文件夹名，避免同名目录互相覆盖。"""
    out: list[tuple[Path, str]] = []
    for folder in folders:
        # 必须是绝对路径：COM 的 Documents.Open 会把相对路径解析到 Office 自己的
        # 工作目录，WPS 直接报"文档打开失败（3010）"。
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [
                d for d in dirnames if d not in probe.SKIP_DIR_NAMES and not d.startswith(".")
            ]
            for name in filenames:
                if name.startswith("~$"):
                    continue
                ext = Path(name).suffix.lower()
                if ext not in DOCX_EXTS | PPT_EXTS | PDF_EXTS | SKIP_EXTS:
                    continue
                absolute = Path(dirpath) / name
                rel = f"{folder.name}/{absolute.relative_to(folder).as_posix()}"
                out.append((absolute, rel))
    return sorted(out, key=lambda item: item[1])


def _md_text(rel: str) -> str:
    return Path(rel).with_suffix(".md").as_posix()


def convert_one(path: Path, ext: str, encrypted: bool, cfg) -> tuple[str, str]:
    """返回 (markdown 文本, 使用的通道)。失败抛异常，由调用方记账。"""
    if ext in DOCX_EXTS:
        if encrypted:
            xml = office_bridge.extract_wordopenxml(path)
            return render_docx.docx_bytes_to_md(office_bridge.flat_opc_to_docx_bytes(xml)), "com"
        return render_docx.docx_bytes_to_md(path.read_bytes()), "direct"
    if ext in PPT_EXTS:
        return pptx_conv.pptx_to_md(path, encrypted=encrypted), "com" if encrypted else "direct"
    if ext in PDF_EXTS:
        result = pdf_conv.pdf_to_text(path, cfg=cfg)
        if result.via == "skipped":
            raise RuntimeError(result.reason or "pdf_skipped")
        return result.text, result.via
    raise RuntimeError("unsupported")


def _write_md(cfg, rel: str, md: str, source_path: str) -> str:
    target = Path(cfg.md_dir) / _md_text(rel)
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
    """转换一批文档。`index` 需提供 upsert/commit（Task 7 的真 Index 或测试替身）。"""
    if index is None:  # 延迟导入：Task 6 先于 Task 7 落地
        from landoc.index import Index

        index = Index(cfg.db_path)

    report = RunReport()
    state = _read_state(cfg)
    seen = state.setdefault("files", {})
    processed = 0

    try:
        for path, rel in _iter_docs(cfg.folders):
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

            key = seen.get(rel)
            if key and key.get("mtime") == stat.st_mtime and key.get("size") == stat.st_size:
                report.skip("unchanged")
                continue

            kind = probe.classify(path)
            if kind == "blocked":
                report.failed.append((rel, "unreadable"))
                continue

            try:
                md, via = convert_one(path, ext, kind == "encrypted", cfg)
            except office_bridge.OfficeUnavailable:
                report.failed.append((rel, "no_office"))
                continue
            except office_bridge.OfficeTimeout:
                report.failed.append((rel, "timeout"))
                continue
            except office_bridge.PasswordProtected:
                report.failed.append((rel, "password_protected"))
                continue
            except pptx_conv.PptxError:
                report.failed.append((rel, "parse_failed"))
                continue
            except Exception as exc:  # noqa: BLE001 - 单篇失败不拖垮整批
                reason = str(exc) or exc.__class__.__name__
                report.failed.append((rel, reason))
                continue

            if not md.strip():
                report.failed.append((rel, "empty_text"))
                continue

            md_path = _write_md(cfg, rel, md, str(path))
            index.upsert(
                _md_text(rel),
                Path(rel).stem,
                ext.lstrip("."),
                str(path),
                stat.st_size,
                stat.st_mtime,
                md,
            )
            report.converted += 1
            report.md_paths.append(rel)
            seen[rel] = {"mtime": stat.st_mtime, "size": stat.st_size, "via": via}
            processed += 1
    finally:
        index.commit()
        state["last_scan_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_state(cfg, state)
    return report
