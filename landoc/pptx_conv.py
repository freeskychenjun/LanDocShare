"""pptx/ppt → Markdown。

明文 `.pptx` 走 `python-pptx`（快、无副作用）；`.ppt`（OLE 容器）与任何密文都只能走
白名单进程内的 COM（`KWPP.Application` / `PowerPoint.Application`），因为非白名单
进程读到的是密文、且 python-pptx 不认 OLE。
"""

from __future__ import annotations

from pathlib import Path

from landoc import office_bridge

PPT_PROGIDS = ("KWPP.Application", "PowerPoint.Application")


class PptxError(RuntimeError):
    """演示文稿无法解析。"""


def _table_to_gfm(table) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            text = " ".join(p.text.strip() for p in cell.text_frame.paragraphs).strip()
            cells.append(text.replace("\n", " ").replace("|", "\\|"))
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _plain_to_md(path: Path) -> str:
    try:
        from pptx import Presentation

        prs = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001 - python-pptx 抛的异常类型很杂
        raise PptxError(f"无法解析演示文稿：{path}（{exc}）") from exc

    blocks: list[str] = []
    for index, slide in enumerate(prs.slides, start=1):
        blocks.append(f"## 第 {index} 页")
        for shape in slide.shapes:
            if getattr(shape, "has_table", False) and shape.has_table:
                rendered = _table_to_gfm(shape.table)
                if rendered:
                    blocks.append(rendered)
                continue
            if not getattr(shape, "has_text_frame", False):
                continue
            text = (shape.text_frame.text or "").strip()
            if text:
                blocks.append(text)
    return "\n\n".join(blocks)


def _dispatch_ppt():
    import win32com.client as wc

    last: Exception | None = None
    for progid in PPT_PROGIDS:
        try:
            app = wc.DispatchEx(progid)
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
        try:
            app.DisplayAlerts = 0
        except Exception:  # noqa: BLE001
            pass
        return app
    raise office_bridge.OfficeUnavailable(f"未找到可用的 PowerPoint/WPP：{last}")


def _slide_text(session) -> str:
    """在会话线程里逐页取形状文本与表格。"""
    raise NotImplementedError  # pragma: no cover - 占位，实际逻辑在 _com_job


def _com_job(path: Path):
    def job(session) -> str:
        app = session.app()
        try:
            pres = app.Presentations.Open(str(path), ReadOnly=True, WithWindow=False)
        except Exception:  # noqa: BLE001 - WPS 的 Open 签名可能不接受 WithWindow
            pres = app.Presentations.Open(str(path), ReadOnly=True)
        blocks: list[str] = []
        for index in range(1, pres.Slides.Count + 1):
            blocks.append(f"## 第 {index} 页")
            slide = pres.Slides.Item(index)
            for j in range(1, slide.Shapes.Count + 1):
                shape = slide.Shapes.Item(j)
                try:
                    if getattr(shape, "HasTable", 0) and shape.HasTable:
                        table = shape.Table
                        rows: list[list[str]] = []
                        for r in range(1, table.Rows.Count + 1):
                            cells: list[str] = []
                            for c in range(1, table.Columns.Count + 1):
                                try:
                                    value = table.Cell(r, c).Shape.TextFrame.TextRange.Text
                                except Exception:  # noqa: BLE001
                                    value = ""
                                cells.append(value.replace("\r", " ").strip().replace("|", "\\|"))
                            rows.append(cells)
                        if rows:
                            width = max(len(r) for r in rows)
                            rows = [r + [""] * (width - len(r)) for r in rows]
                            lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
                            lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                            blocks.append("\n".join(lines))
                        continue
                    if getattr(shape, "HasTextFrame", 0) and shape.TextFrame.HasText:
                        text = shape.TextFrame.TextRange.Text.strip()
                        if text:
                            blocks.append(text)
                except Exception:  # noqa: BLE001 - 单个形状失败不影响整页
                    continue
        return "\n\n".join(blocks)

    return job


def pptx_to_md(
    path: Path, encrypted: bool = False, timeout: float = 120.0, session=None
) -> str:
    """把演示文稿转成 Markdown；密文或 OLE 格式（.ppt/.dps）自动走 COM。"""
    path = Path(path)
    if not encrypted:
        return _plain_to_md(path)
    job = _com_job(path)
    if session is not None:
        return session.call(job, timeout)
    with office_bridge.OfficeSession(dispatch=_dispatch_ppt, timeout=timeout) as own:
        return own.call(job, timeout)
