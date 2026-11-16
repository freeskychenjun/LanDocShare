"""Office COM 桥：从白名单进程内存里取出 Flat OPC，再重建为合法 .docx 字节。

为什么是这条链路（实测结论，见设计文档 §3.1/§3.2）：
- 白名单进程（winword/wps）读文件时被透明解密，非白名单进程（python）读到密文；
- 白名单进程**写**文件一律落盘为密文，所以"另存为中间文件"的路走不通；
- 唯一可行：Office 打开 → `doc.Content.WordOpenXML` 取回内存字符串 → python 落盘明文。
"""

from __future__ import annotations

import base64
import queue
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


_PASSWORD_HINTS = ("密码", "口令", "password", "Password", "PASSWORD")


def _has_password_dialog(pids: set[int]) -> bool:
    """超时时判断是不是弹了口令/密码对话框（真口令文档会模态阻塞 COM）。"""
    if not pids:
        return False
    try:
        import win32gui
        import win32process
    except ImportError:  # pragma: no cover
        return False
    found = {"hit": False}

    def visit(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001
            return True
        if pid not in pids:
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        cls = win32gui.GetClassName(hwnd) or ""
        if cls == "#32770" or any(hint in title for hint in _PASSWORD_HINTS):
            found["hit"] = True
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:  # noqa: BLE001
        return False
    return found["hit"]


class OfficeSession:
    """复用同一个 Office 实例处理一批文档。

    每篇重启 Office 会白付 6~9 秒启动成本（设计文档 §3.2 的基准是"首篇 6.4 秒，
    之后约 1.4 秒/份"）。COM 有线程亲和性，所以整批固定跑在一个专用工作线程里，
    调用方只做超时等待；超时则杀掉本会话新起的进程并让会话失效。
    """

    def __init__(self, dispatch=None, timeout: float = 120.0):
        self._dispatch = dispatch or _dispatch_office
        self._timeout = timeout
        self._queue: "queue.Queue" = queue.Queue()
        self._app = None
        self._before = _office_pids()
        self._dead = False
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
            self._app = self._dispatch()
        return self._app

    def call(self, func, timeout: float | None = None):
        if self._dead:
            raise OfficeUnavailable("Office 会话已失效")
        wait = self._timeout if timeout is None else timeout
        box: dict = {}
        done = threading.Event()
        self._queue.put((func, box, done))
        if not done.wait(wait):
            pids = _office_pids() - self._before
            password = _has_password_dialog(pids)
            _kill_office_processes(pids)
            self._dead = True
            if password:
                raise PasswordProtected("文档需要打开口令（检测到密码对话框）")
            raise OfficeTimeout(f"超过 {wait:.0f} 秒未完成")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def extract(self, path: Path, timeout: float | None = None) -> str:
        def job(session: "OfficeSession") -> str:
            doc = _open_document(session.app(), path)
            if doc is None:
                raise PasswordProtected(f"文档无法打开（可能带打开口令）：{path}")
            try:
                return doc.Content.WordOpenXML
            finally:
                try:
                    doc.Close(False)
                except Exception:  # noqa: BLE001
                    pass

        return self.call(job, timeout)

    def close(self) -> None:
        try:
            self._queue.put(None)
            self._thread.join(timeout=5)
        except RuntimeError:
            pass
        # 只杀本会话新起的 Office 进程，不动库主人自己开着的窗口
        _kill_office_processes(_office_pids() - self._before)

    def __enter__(self) -> "OfficeSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def extract_wordopenxml(
    path: Path, timeout: float = 120.0, session: OfficeSession | None = None
) -> str:
    """打开文档并返回 Flat OPC 字符串（纯内存，不落盘）。

    传 session 可让一批文档复用同一个 Office 实例；不传则临时起一个会话。
    """
    if session is not None:
        xml = session.extract(path, timeout)
    else:
        with OfficeSession(timeout=timeout) as own_session:
            xml = own_session.extract(path, timeout)
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
