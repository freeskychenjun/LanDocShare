"""文件头体检：分流明文 / 密文 / 不可读，并给出抽样报告。

单位透明加密的判据是文件头前 4 字节 `17DA5FA0`（完整标记 17da5fa01633cd9a）。
白名单进程（winword/wps/acrobat/acad 等）读时被驱动透明解密，python 读到的
永远是密文，所以"能不能直读"必须先体检，不能猜。
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # 注册表探测可选，非 Windows 环境下退化为 False
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

ENCRYPTED_MAGIC = bytes.fromhex("17da5fa01633cd9a")
_ENCRYPTED_PREFIX = ENCRYPTED_MAGIC[:4]
_PLAIN_PREFIXES = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"%PDF")

DOC_EXTS = {".doc", ".docx", ".wps", ".ppt", ".pptx", ".dps", ".pdf"}
SKIP_DIR_NAMES = {"$RECYCLE.BIN", "System Volume Information", ".git"}

OFFICE_PROGIDS = ("KWPS.Application", "Word.Application", "KWPP.Application")
ACROBAT_EXE_CANDIDATES = (
    Path(r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe"),
    Path(r"C:\Program Files (x86)\Adobe\Acrobat DC\Acrobat\Acrobat.exe"),
    Path(r"C:\Program Files (x86)\Adobe\Acrobat 11.0\Acrobat\Acrobat.exe"),
)


def classify(path: Path) -> str:
    """返回 "plain" / "encrypted" / "blocked"。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return "blocked"
    if head == _ENCRYPTED_PREFIX:
        return "encrypted"
    for prefix in _PLAIN_PREFIXES:
        if head.startswith(prefix):
            return "plain"
    return "blocked"


def _progid_registered(progid: str) -> bool:
    if winreg is None:
        return False
    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid).Close()
    except OSError:
        return False
    return True


def has_office() -> bool:
    """本机是否装了 WPS 或 MS Office（只看 ProgID 注册，不启动任何进程）。"""
    return any(_progid_registered(p) for p in OFFICE_PROGIDS)


def has_acrobat() -> bool:
    """本机是否有 Adobe Acrobat（AcroExch.App 或 Acrobat.exe 真实存在）。"""
    if _progid_registered("AcroExch.App"):
        return True
    return any(p.is_file() for p in ACROBAT_EXE_CANDIDATES)


def _iter_docs(folders: list[Path]):
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")]
            for name in filenames:
                if name.startswith("~$"):
                    continue
                if Path(name).suffix.lower() in DOC_EXTS:
                    yield Path(dirpath) / name


def sample_report(folders: list[Path], n: int = 200) -> dict:
    """抽样体检：最多看 n 份文档，统计明/密/受阻比例，并探测 Office 与 Acrobat。"""
    counts = {"plain": 0, "encrypted": 0, "blocked": 0}
    sampled = 0
    for path in _iter_docs(folders):
        if sampled >= n:
            break
        counts[classify(path)] += 1
        sampled += 1
    return {
        "sampled": sampled,
        "plain": counts["plain"],
        "encrypted": counts["encrypted"],
        "blocked": counts["blocked"],
        "has_office": has_office(),
        "has_acrobat": has_acrobat(),
    }
