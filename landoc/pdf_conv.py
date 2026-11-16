"""PDF → 文本。三条路，按库主人已批准的优先级（设计文档 D3）：

1. 明文 PDF：`pymupdf` 直读（绝大多数 PDF 走这条）。
2. 有 Adobe Acrobat：优先 Acrobat —— 实测 21 页 0.8 秒、文本干净、**不落任何明文副本**。
3. 没有 Acrobat 但有兜底工具（伪装名 `acrobat.exe`）：解一份**临时副本**、抽完立刻删除。

密文 PDF 在本机其它阅读器（Foxit、WPS PDF、Word 2007）上打不开，已实测，
所以这里的判断不能凭"试试看"。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from landoc import office_bridge
from landoc.probe import classify, has_acrobat  # noqa: F401 - has_acrobat 供测试替换

MIN_TEXT_CHARS = 50
TOOL_EXE_NAME = "acrobat.exe"

_ASCII_START = re.compile(r"[A-Za-z0-9]")
_ASCII_END = re.compile(r"[A-Za-z0-9]$")


class PDFDecryptError(RuntimeError):
    """兜底工具或 Acrobat 没能把文件变成可读文本。"""


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


def _join_tokens(tokens) -> str:
    """把 Acrobat 的取词结果拼回正文。

    这台 Acrobat 返回的是**单个字符**（每个字一个 token，换行也是独立 token），
    直接 join 会得到"附\\n件\\n1"这种碎片，trigram 检索命中率归零（实测）。
    规则：中文之间不加分隔符，英文/数字之间补空格，空白 token 只保留一个换行。
    """
    newline = chr(10)
    carriage = chr(13)
    out: list[str] = []
    prev_ascii = False
    for raw in tokens:
        if not raw:
            continue
        piece = raw.replace(carriage + newline, newline).replace(carriage, newline)
        if not piece.strip():
            if newline in piece and (not out or not out[-1].endswith(newline)):
                out.append(newline)
            continue
        piece = piece.replace(newline, " ")
        starts_ascii = bool(_ASCII_START.match(piece))
        if out and prev_ascii and starts_ascii:
            out.append(" ")
        out.append(piece)
        prev_ascii = bool(_ASCII_END.search(piece))
    return "".join(out)


def _page_text(pd, index: int, wc) -> str:
    """取一页文本：整页建一个文本选区，逐段 GetText 后拼回正文。"""
    page = pd.AcquirePage(index)
    try:
        size = page.GetSize()
        width, height = size.x, size.y
    finally:
        try:
            page.Release()
        except Exception:  # noqa: BLE001 - 已释放/无此成员都不影响结果
            pass
    rect = wc.Dispatch("AcroExch.Rect")
    # PDF 坐标原点在左下角：Top 是上边界、Bottom 是 0
    rect.Left, rect.Right, rect.Bottom, rect.Top = 0, width, 0, height
    selection = pd.CreateTextSelect(index, rect)
    if selection is None:
        return ""
    return _join_tokens(selection.GetText(i) for i in range(selection.GetNumText()))


def _acrobat_text(path: Path, timeout: float = 120.0) -> str:
    """Acrobat 路线：PDDoc 打开 → 逐页全页取文本（不经临时文件）。

    实测（Acrobat DC / 21 页加密 PDF）：0.8 秒、文本干净、不落任何明文副本。
    PDDoc 在 Acrobat 侧运行，属白名单进程，所以能读到明文。
    """

    def work() -> str:
        import pythoncom  # 仅 Windows
        import win32com.client as wc

        pythoncom.CoInitialize()
        pd = None
        try:
            app = wc.Dispatch("AcroExch.App")
            try:
                app.Hide()
            except Exception:  # noqa: BLE001 - 不同版本可能没有 Hide
                pass
            pd = wc.Dispatch("AcroExch.PDDoc")
            if not pd.Open(str(path)):
                raise PDFDecryptError(f"Acrobat 打不开：{path}")
            pages = [_page_text(pd, i, wc) for i in range(pd.GetNumPages())]
            return "\n".join(page for page in pages if page)
        finally:
            if pd is not None:
                try:
                    pd.Close()
                except Exception:  # noqa: BLE001
                    pass
            pythoncom.CoUninitialize()

    before = office_bridge._acrobat_pids()
    try:
        return office_bridge._run_with_timeout(
            work,
            timeout,
            on_timeout=lambda: office_bridge._kill_office_processes(
                office_bridge._acrobat_pids() - before
            ),
        )
    finally:
        # 只清我们自己拉起来的 Acrobat 实例，库主人自己开着的阅读器不动
        office_bridge._kill_office_processes(office_bridge._acrobat_pids() - before)


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
        except (PDFDecryptError, office_bridge.OfficeTimeout, OSError, RuntimeError):
            pass  # 落到兜底工具

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
