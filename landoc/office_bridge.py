"""Office COM 桥：从白名单进程内存里取出 Flat OPC，再重建为合法 .docx 字节。

为什么是这条链路（实测结论，见设计文档 §3.1/§3.2）：
- 白名单进程（winword/wps）读文件时被透明解密，非白名单进程（python）读到密文；
- 白名单进程**写**文件一律落盘为密文，所以"另存为中间文件"的路走不通；
- 唯一可行：Office 打开 → `doc.Content.WordOpenXML` 取回内存字符串 → python 落盘明文。
"""

from __future__ import annotations

import base64
import subprocess
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

PKG = "{http://schemas.microsoft.com/office/2006/xmlPackage}"
OFFICE_PROGIDS = ("KWPS.Application", "Word.Application")
OFFICE_PROCESS_NAMES = ("wps.exe", "WINWORD.EXE", "wpp.exe", "POWERPNT.EXE")


class OfficeUnavailable(RuntimeError):
    """本机既没有 WPS 也没有 MS Office。"""


class OfficeTimeout(RuntimeError):
    """单文档超过时限（通常是弹了密码框或大文档转换）。"""


class PasswordProtected(RuntimeError):
    """文档带打开口令，COM 打开返回空文档。"""


def _dispatch_office():
    """依次尝试 WPS Writer 与 MS Word，返回可用的 Application 对象。"""
    import win32com.client as wc  # 延迟导入：非 Windows 环境也能 import 本模块

    last: Exception | None = None
    for progid in OFFICE_PROGIDS:
        try:
            app = wc.DispatchEx(progid)
        except Exception as exc:  # noqa: BLE001 - COM 异常类型五花八门
            last = exc
            continue
        try:
            app.Visible = False
        except Exception:  # noqa: BLE001
            pass
        try:
            app.DisplayAlerts = 0
        except Exception:  # noqa: BLE001
            pass
        return app
    raise OfficeUnavailable(f"未找到可用的 Office：{last}")


def _open_document(app, path: Path):
    """COM 打开文档的唯一入口（测试会替换它以模拟卡死）。"""
    return app.Documents.Open(str(path), ReadOnly=True, AddToRecentFiles=False)


def _decode_console(raw: bytes | None) -> str:
    """解码控制台工具输出：中文 Windows 上 tasklist 默认输出 GBK。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _office_pids() -> set[int]:
    """当前存活的 Office 进程 PID（用于只杀“我们自己启的那个”，不误伤用户正开着的窗口）。"""
    pids: set[int] = set()
    for name in OFFICE_PROCESS_NAMES:
        try:
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {name}"],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in _decode_console(proc.stdout).splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) >= 2 and cols[1].isdigit():
                pids.add(int(cols[1]))
    return pids


def _kill_office_processes(pids: set[int] | None = None) -> None:
    """结束我们启动的 Office 进程（pids 为空时不做事）。"""
    for pid in pids or set():
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=20
            )
        except (OSError, subprocess.SubprocessError):
            pass


def _run_with_timeout(func, timeout: float, on_timeout=None):
    """在工作线程里跑 COM 调用；超时则回调清理并抛 OfficeTimeout。"""
    box: dict = {}

    def target():
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - 原样交给主线程
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        if on_timeout is not None:
            on_timeout()
        raise OfficeTimeout(f"超过 {timeout:.0f} 秒未完成")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def extract_wordopenxml(path: Path, timeout: float = 120.0) -> str:
    """打开文档并返回 Flat OPC 字符串（纯内存，不落盘）。

    COM 必须在创建它的同一个线程里使用（否则动态派发拿不到 `Documents`），
    所以整个“启动 Office → 打开 → 取 XML → 关闭”都放在工作线程内完成，
    外层只负责超时与杀进程。
    """

    def work() -> str:
        import pythoncom  # 仅 Windows 可用，延迟导入

        pythoncom.CoInitialize()
        doc = None
        try:
            app = _dispatch_office()
            doc = _open_document(app, path)
            if doc is None:
                raise PasswordProtected(f"文档无法打开（可能带打开口令）：{path}")
            return doc.Content.WordOpenXML
        finally:
            if doc is not None:
                try:
                    doc.Close(False)
                except Exception:  # noqa: BLE001
                    pass
            # 不断言 app.Quit()：WPS 关掉最后一个文档后再 Quit 会抛 RPC 失败（0x800706be），
            # 进程清理由 _kill_office_processes(新增 PID) 负责。
            pythoncom.CoUninitialize()

    before = _office_pids()
    try:
        xml = _run_with_timeout(
            work, timeout, on_timeout=lambda: _kill_office_processes(_office_pids() - before)
        )
    finally:
        _kill_office_processes(_office_pids() - before)
    if not xml or "<pkg:package" not in xml:
        raise OfficeUnavailable(f"WordOpenXML 返回异常：{path}")
    return xml


def _content_types_xml(overrides: list[tuple[str, str]], defaults: set[tuple[str, str]]) -> bytes:
    """合成 Flat OPC 里缺失的 `[Content_Types].xml`（Word 不返回这一部分）。"""
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
    ]
    for ext, ctype in sorted(defaults):
        if ext == "rels":
            continue
        lines.append(f'<Default Extension="{ext}" ContentType="{ctype}"/>')
    lines.append(
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    )
    for name, ctype in overrides:
        lines.append(f'<Override PartName="/{name}" ContentType="{ctype}"/>')
    lines.append("</Types>")
    return "\n".join(lines).encode("utf-8")


def _default_content_type(ext: str, declared: str) -> str:
    if ext in {"png", "jpg", "jpeg", "gif", "emf", "wmf", "bmp", "tiff", "bin"}:
        return declared
    if ext == "xml":
        return "application/xml"
    return declared or "application/octet-stream"


def flat_opc_to_docx_bytes(xml: str) -> bytes:
    """把 Flat OPC 字符串重建成合法 .docx 字节（python 写 = 明文）。"""
    import io
    import zipfile

    root = ET.fromstring(xml)
    parts: list[tuple[str, bytes]] = []
    overrides: list[tuple[str, str]] = []
    defaults: set[tuple[str, str]] = set()

    for part in root.findall(PKG + "part"):
        name = (part.get(PKG + "name") or "").lstrip("/")
        ctype = part.get(PKG + "contentType") or ""
        xml_data = part.find(PKG + "xmlData")
        bin_data = part.find(PKG + "binaryData")
        if xml_data is not None and len(xml_data):
            payload = ET.tostring(list(xml_data)[0], encoding="utf-8", xml_declaration=True)
        elif bin_data is not None and (bin_data.text or "").strip():
            payload = base64.b64decode("".join((bin_data.text or "").split()))
        else:
            continue
        parts.append((name, payload))
        overrides.append((name, ctype))
        ext = name.rsplit(".", 1)[-1].lower()
        defaults.add((ext, _default_content_type(ext, ctype)))

    if not parts:
        raise ValueError("Flat OPC 中没有任何 pkg:part")

    parts.insert(0, ("[Content_Types].xml", _content_types_xml(overrides, defaults)))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in parts:
            zf.writestr(name, payload)
    return buf.getvalue()
