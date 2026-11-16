"""PDF → 文本。三条路，按库主人已批准的优先级（设计文档 D3）：

1. 明文 PDF：`pymupdf` 直读（绝大多数 PDF 走这条）。
2. 有 Adobe Acrobat：优先 Acrobat（白名单内、合规）。
3. 没有 Acrobat 但有兜底工具（伪装名 `acrobat.exe`）：解一份**临时副本**、抽完立刻删除。

密文 PDF 在本机其它阅读器（Foxit、WPS PDF、Word 2007）上打不开，已实测，
所以这里的判断不能凭"试试看"。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from landoc.probe import classify, has_acrobat  # noqa: F401 - has_acrobat 供测试替换

MIN_TEXT_CHARS = 50
TOOL_EXE_NAME = "acrobat.exe"


class PDFDecryptError(RuntimeError):
    """兜底工具没能把文件变成明文 PDF。"""


@dataclass
class PDFResult:
    text: str
    via: str  # "pymupdf" | "acrobat" | "acrobat_disguise" | "skipped"
    reason: str = ""


def _run_tool(tool: Path, target: Path, cwd: Path, timeout: float) -> None:
    """调用伪装名工具原地解密（工具自己把明文写回目标文件）。"""
    subprocess.run(
        [str(tool), str(target)], cwd=str(cwd), timeout=timeout, capture_output=True, check=False
    )


def decrypt_via_disguise(
    path: Path, tool_dir: Path, timeout: float = 120.0, runner=None
) -> Path:
    """在临时目录里解一份副本并返回它；**调用方负责删除**。"""
    tool = Path(tool_dir) / TOOL_EXE_NAME
    if not tool.is_file():
        raise FileNotFoundError(f"兜底工具不存在：{tool}")
    tmpdir = Path(tempfile.mkdtemp(prefix="landoc_pdf_"))
    copy = tmpdir / (Path(path).stem + ".pdf")
    shutil.copy2(path, copy)
    (runner or _run_tool)(tool, copy, Path(tool_dir), timeout)
    if classify(copy) != "plain":
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise PDFDecryptError(f"工具执行后文件仍是密文：{copy}")
    return copy


def _extract_with_fitz(path: Path) -> str:
    import fitz

    doc = fitz.open(str(path))
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _acrobat_text(path: Path, timeout: float) -> str:
    """Acrobat 分支：一期只做可用性探测，取文本实现留给 spike S2（不得假装成功）。"""
    raise NotImplementedError("Acrobat 取文本路径待 S2 验证")


def pdf_to_text(path: Path, cfg=None, timeout: float = 120.0) -> PDFResult:
    path = Path(path)
    if not path.is_file():
        return PDFResult("", "skipped", "unreadable")

    kind = classify(path)
    if kind == "plain":
        text = _extract_with_fitz(path)
        reason = "" if len(text.strip()) >= MIN_TEXT_CHARS else "no_text_layer"
        return PDFResult(text, "pymupdf", reason)

    if kind == "blocked":
        return PDFResult("", "skipped", "unreadable")

    # 密文：Acrobat 优先
    if has_acrobat():
        try:
            text = _acrobat_text(path, timeout)
            if len(text.strip()) >= MIN_TEXT_CHARS:
                return PDFResult(text, "acrobat")
        except (NotImplementedError, OSError, RuntimeError):
            pass  # 落到兜底

    tool_dir = getattr(cfg, "acrobat_tool_dir", None) if cfg is not None else None
    if tool_dir and (Path(tool_dir) / TOOL_EXE_NAME).is_file():
        copy = None
        try:
            copy = decrypt_via_disguise(path, Path(tool_dir), timeout=timeout)
            text = _extract_with_fitz(copy)
            reason = "" if len(text.strip()) >= MIN_TEXT_CHARS else "no_text_layer"
            return PDFResult(text, "acrobat_disguise", reason)
        finally:
            if copy is not None:
                shutil.rmtree(Path(copy).parent, ignore_errors=True)  # 明文副本用完即删

    return PDFResult("", "skipped", "no_reader")
