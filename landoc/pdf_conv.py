"""PDF → 文本。三条路，按库主人已批准的优先级（设计文档 D3）：

1. 明文 PDF：`pymupdf` 直读（绝大多数 PDF 走这条）。
2. 有 Adobe Acrobat：优先 Acrobat —— 实测 21 页 0.8 秒、文本干净、**不落任何明文副本**。
3. 没有 Acrobat 但有兜底工具（伪装名 `acrobat.exe`）：解一份**临时副本**、抽完立刻删除。

密文 PDF 在本机其它阅读器（Foxit、WPS PDF、Word 2007）上打不开，已实测，
所以这里的判断不能凭"试试看"。
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from landoc import ocr, office_bridge
from landoc.probe import classify, has_acrobat  # noqa: F401 - has_acrobat 供测试替换

MIN_TEXT_CHARS = 50
TOOL_EXE_NAME = "acrobat.exe"
# OCR 文本里留一句提示：识别结果会有错字，看的人得知道这不是原文
OCR_NOTICE = "（扫描件：以下文字由 OCR 识别，可能有错字）"

_ASCII_START = re.compile(r"[A-Za-z0-9]")
_ASCII_END = re.compile(r"[A-Za-z0-9]$")

# COM 断连类 HRESULT：Acrobat 被库主人手动关掉 / 进程被杀时抛这些，丢掉旧对象重建即可。
_COM_GONE = frozenset({-2147023174, -2147023170, -2147417848, -2147467259})


class PDFDecryptError(RuntimeError):
    """兜底工具或 Acrobat 没能把文件变成可读文本。"""


class PDFSessionGone(RuntimeError):
    """Acrobat 进程没了或 COM 断连（不是文档坏），重建会话后值得重试一次。"""


def _default_dispatch(progid: str):
    """按 ProgID 起/取 COM 对象（AcroExch.App / AcroExch.PDDoc / AcroExch.Rect）。"""
    import win32com.client as wc

    return wc.Dispatch(progid)


def _is_com_gone(exc: BaseException) -> bool:
    codes: set[int] = set()
    hr = getattr(exc, "hresult", None)
    if isinstance(hr, int):
        codes.add(hr)
    for arg in getattr(exc, "args", ()) or ():
        if isinstance(arg, int):
            codes.add(arg)
    return bool(codes & _COM_GONE)


def _exit_quietly(app) -> None:
    try:
        app.Exit()
    except Exception:  # noqa: BLE001 - 退出失败不影响结果，后面还有 taskkill 兜底
        pass


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


def _page_text(pd, index: int, dispatch) -> str:
    """取一页文本：整页建一个文本选区，逐段 GetText 后拼回正文。

    `dispatch` 是"按 ProgID 造 COM 对象"的可调用对象（默认 `_default_dispatch`），
    测试可以塞替身，不必真起 Acrobat。
    """
    page = pd.AcquirePage(index)
    try:
        size = page.GetSize()
        width, height = size.x, size.y
    finally:
        try:
            page.Release()
        except Exception:  # noqa: BLE001 - 已释放/无此成员都不影响结果
            pass
    rect = dispatch("AcroExch.Rect")
    # PDF 坐标原点在左下角：Top 是上边界、Bottom 是 0
    rect.Left, rect.Right, rect.Bottom, rect.Top = 0, width, 0, height
    selection = pd.CreateTextSelect(index, rect)
    if selection is None:
        return ""
    return _join_tokens(selection.GetText(i) for i in range(selection.GetNumText()))


def _read_pdf_text(dispatch, path: Path) -> str:
    """用一个新的 PDDoc 读一篇 PDF 的全部文本（不带任何超时/进程管理）。"""
    pd = None
    try:
        pd = dispatch("AcroExch.PDDoc")
        if not pd.Open(str(path)):
            raise PDFDecryptError(f"Acrobat 打不开：{path}")
        pages = [_page_text(pd, i, dispatch) for i in range(pd.GetNumPages())]
        return "\n".join(page for page in pages if page)
    except PDFDecryptError:
        raise
    except Exception as exc:  # noqa: BLE001 - 只有"进程没了"才值得重建重试，其余原样抛
        if _is_com_gone(exc):
            raise PDFSessionGone(f"Acrobat 断连：{exc}") from exc
        raise
    finally:
        if pd is not None:
            try:
                pd.Close()
            except Exception:  # noqa: BLE001
                pass


class PDFSession:
    """复用同一个 Acrobat 实例连续抽一批 PDF 的文本。

    为什么需要它：本机实测（Acrobat DC，21 页加密 PDF）每篇独立起停 6.20 秒，
    换成"一个进程连着转"后 0.73 秒/篇 —— 固定成本只剩首次 4.5 秒的启动，
    88% 的时间原本都花在反复启动/结束 Acrobat 上。

    代价是 Acrobat 进程内存随篇数线性上涨（≈0.44 MB/篇，实测 100 篇 39.5→83.7 MB），
    所以调用方应当每几百篇换一个新会话（pipeline 的 `PDF_SESSION_MAX_DOCS`）。

    COM 有线程亲和性，整批固定跑在一个专用工作线程里；超时则杀掉"本会话新起的"
    进程并让会话失效，绝不碰库主人自己开着的阅读器。
    """

    def __init__(self, dispatch=None, timeout: float = 120.0):
        self._dispatch = dispatch or _default_dispatch
        self._timeout = timeout
        self._queue: "queue.Queue" = queue.Queue()
        self._app = None
        self._before = office_bridge._acrobat_pids()
        self._dead = False
        self.used = 0  # 已处理的篇数：调用方据此决定何时回收这个会话
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import pythoncom

        pythoncom.CoInitialize()
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    break
                func, box, done = job
                try:
                    box["value"] = func(self)
                except BaseException as exc:  # noqa: BLE001 - 原样交给调用方线程
                    box["error"] = exc
                finally:
                    done.set()
        finally:
            pythoncom.CoUninitialize()

    def app(self):
        if self._app is None:
            self._app = self._dispatch("AcroExch.App")
            try:
                self._app.Hide()
            except Exception:  # noqa: BLE001 - 不同版本可能没有 Hide
                pass
        return self._app

    def call(self, func, timeout: float | None = None):
        if self._dead:
            raise PDFSessionGone("Acrobat 会话已失效")
        wait = self._timeout if timeout is None else timeout
        box: dict = {}
        done = threading.Event()
        self.used += 1
        self._queue.put((func, box, done))
        if not done.wait(wait):
            office_bridge._kill_office_processes(
                office_bridge._acrobat_pids() - self._before
            )
            self._dead = True
            raise office_bridge.OfficeTimeout(f"超过 {wait:.0f} 秒未完成")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _job(self, path: Path):
        def job(session: "PDFSession") -> str:
            session.app()
            return _read_pdf_text(self._dispatch, path)

        return job

    def extract(self, path: Path, timeout: float | None = None) -> str:
        try:
            return self.call(self._job(path), timeout)
        except PDFSessionGone:
            # 用户手动关掉了 Acrobat，或进程自己崩了：清掉旧对象重来一次
            self._reset()
            return self.call(self._job(path), timeout)

    def _reset(self) -> None:
        self._app = None
        office_bridge._kill_office_processes(office_bridge._acrobat_pids() - self._before)
        self._dead = False

    def close(self) -> None:
        # 先请 Acrobat 自己退出（和 Office 一样：硬杀会留残留进程）
        if not self._dead and self._app is not None:
            try:
                self.call(lambda session: _exit_quietly(session.app()), timeout=10)
            except Exception:  # noqa: BLE001 - 已断连/已退出都无所谓
                pass
        try:
            self._queue.put(None)
            self._thread.join(timeout=5)
        except RuntimeError:
            pass
        for _ in range(4):
            if not (office_bridge._acrobat_pids() - self._before):
                break
            threading.Event().wait(0.5)
        office_bridge._kill_office_processes(office_bridge._acrobat_pids() - self._before)
        self._app = None

    def __enter__(self) -> "PDFSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _acrobat_text(path: Path, timeout: float = 120.0) -> str:
    """一次性路线：临时起一个 Acrobat 会话，读完就关。

    批量转换请传 `PDFSession` 给 `pdf_to_text`（一个实例连转一批，省下每篇 5.5 秒）。
    实测（Acrobat DC / 21 页加密 PDF）：0.8 秒取完、文本干净、不落任何明文副本；
    PDDoc 在 Acrobat 侧运行，属白名单进程，所以能读到明文。
    """
    with PDFSession(timeout=timeout) as session:
        return session.extract(path, timeout)


def _ocr_text(path: Path, cfg=None) -> str | None:
    """把 PDF 前几页渲染成 PNG 交给 OCR；不可用/没识别出字就返回 None。

    只有"没有文字层"的扫描件才走到这里，代价约 1~2 秒/页（300dpi），所以默认最多 60 页；
    页数更多的扫描件只识别前 60 页，md 里会写明"只识别了前 N 页"。
    """
    if cfg is not None and not getattr(cfg, "ocr_scanned_pdfs", True):
        return None
    if ocr.backend() == "":
        return None
    try:
        import fitz
    except ImportError:  # 没装 pymupdf 时明文 PDF 本来也读不了
        return None
    dpi = int(getattr(cfg, "ocr_dpi", 300) or 300)
    max_pages = int(getattr(cfg, "ocr_max_pages", 60) or 60)
    try:
        doc = fitz.open(path)
    except Exception:  # noqa: BLE001 - 打不开就交给上层记失败
        return None
    images: list[tuple[str, Path]] = []
    total_pages = 0
    tmpdir = Path(tempfile.mkdtemp(prefix="landoc_ocr_"))
    try:
        total_pages = doc.page_count
        # 只有"页面是图片"的 PDF 才值得 OCR：文字少但没有图的（一页短通知/表单）没有
        # 可识别的东西，白渲染一遍只会让建库变慢。
        if not any(doc[index].get_images(full=True) for index in range(min(total_pages, 3))):
            return None
        for index in range(min(total_pages, max_pages)):
            target = tmpdir / f"p{index + 1:04d}.png"
            doc[index].get_pixmap(dpi=dpi).save(str(target))
            images.append((f"p{index + 1:04d}", target))
    except Exception:  # noqa: BLE001 - 渲染失败就放弃 OCR，别把建库拖死
        return None
    finally:
        doc.close()
    try:
        if not images:
            return None
        raw = ocr.ocr_images(images)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not raw:
        return None
    pages = [page.strip() for page in raw.split("\f")]
    pages = [page for page in pages if page]
    if not pages:
        return None
    body = "\n\n".join(f"<!-- page {index + 1} -->\n{page}" for index, page in enumerate(pages))
    header = OCR_NOTICE
    if total_pages > len(pages):
        header += f"（只识别了前 {len(pages)} 页，共 {total_pages} 页）"
    # 只对识别出来的正文做收尾：提示行里的空格是给人看的，不能被"去汉字间空格"吃掉
    return f"{header}\n\n{ocr.tidy(body)}"


def pdf_to_text(path: Path, cfg=None, timeout: float = 120.0, session=None) -> PDFResult:
    """PDF → 文本。`session` 是一个 `PDFSession`（批量转换用，省掉每篇重启 Acrobat）。"""
    path = Path(path)
    if not path.is_file():
        return PDFResult("", "skipped", "unreadable")

    kind = classify(path)
    if kind == "plain":
        text = _extract_with_fitz(path)
        if len(text.strip()) >= MIN_TEXT_CHARS:
            return PDFResult(text, "pymupdf")
        ocr_text = _ocr_text(path, cfg)
        if ocr_text:
            return PDFResult(ocr_text, "ocr")
        reason = "no_ocr" if ocr.backend() == "" else "no_text_layer"
        return PDFResult(text, "pymupdf", reason)

    if kind == "blocked":
        return PDFResult("", "skipped", "unreadable")

    # 密文：Acrobat 优先（有会话就复用同一个 Acrobat 进程）
    if has_acrobat():
        try:
            text = (
                session.extract(path, timeout)
                if session is not None
                else _acrobat_text(path, timeout)
            )
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
            if len(text.strip()) < MIN_TEXT_CHARS:
                ocr_text = _ocr_text(copy, cfg)
                if ocr_text:
                    return PDFResult(ocr_text, "ocr")
            reason = "" if len(text.strip()) >= MIN_TEXT_CHARS else ("no_ocr" if ocr.backend() == "" else "no_text_layer")
            return PDFResult(text, "acrobat_disguise", reason)
        finally:
            if copy is not None:
                shutil.rmtree(Path(copy).parent, ignore_errors=True)  # 明文副本用完即删

    return PDFResult("", "skipped", "no_reader")
