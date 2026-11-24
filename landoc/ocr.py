"""扫描件 OCR：Windows 系统 OCR 优先，Tesseract 兜底。

为什么两条腿：
- `Windows.Media.Ocr` 随系统中文语言包提供，识别质量最好、零体积，但不是每台机器都有
  （英文系统只有 en-US；精简版/LTSC 可能把语言包裁掉）；
- Tesseract 我们打包一份**完整闭包**（exe + 35 个 DLL + 中英 fast 模型，共约 27 MB），
  保证"系统缺中文识别包"的同事机器上，扫描件也能收进库。闭包必须自洽：随包那份在
  净化 PATH 之后要能跑（`verify_bundled_tesseract()`，出包前 `packaging/release.py`
  会跑一遍）—— 开发机装着 Tesseract 时，缺 DLL 也能被系统里那份顶住，谁都发现不了。

两条路都只在建库机上跑：检索端读的是 md/索引，不需要任何 OCR 组件。

打包版里 `landoc/tools/` 会被 PyInstaller 解到 `sys._MEIPASS/landoc/tools/`，
所以路径一律经 `tools_dir()` 取，不要写死。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path

DEFAULT_LANG = "zh-Hans-CN"
# 少于这个字数就当"没识别出东西"：OCR 遇到空白页/整页印章时常吐一两个字
MIN_OCR_CHARS = 20
# 语言列表要起一次 PowerShell（约 0.4 秒），缓存 5 分钟；装完语言包重启程序即可生效
LANG_TTL = 300.0

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_LANG_LOCK = threading.Lock()
_LANG_AT = 0.0
_LANG_CACHE: tuple[str, ...] = ()
_LANG_PROBED = False

# 汉字与全角标点：Windows OCR 会在它们之间插空格（"江 西 省"），收尾时要清掉
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef"


def tools_dir() -> Path:
    """随包的 OCR 工具目录（源码跑在 landoc/tools，打包后在 _MEIPASS/landoc/tools）。"""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        return Path(frozen) / "landoc" / "tools"
    return Path(__file__).with_name("tools")


def _powershell() -> str | None:
    if os.name != "nt":
        return None
    return shutil.which("powershell.exe") or shutil.which("powershell")


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


# ---------------------------------------------------------------- Windows 系统 OCR


def available_languages(*, refresh: bool = False) -> tuple[str, ...]:
    """系统装了哪些 OCR 识别语言（如 ("zh-Hans-CN",)）；拿不到就是空元组。"""
    global _LANG_AT, _LANG_CACHE, _LANG_PROBED
    now = time.monotonic()
    with _LANG_LOCK:
        if _LANG_PROBED and not refresh and now - _LANG_AT < LANG_TTL:
            return _LANG_CACHE
        tags: tuple[str, ...] = ()
        script = tools_dir() / "ocr_win.ps1"
        shell = _powershell()
        if shell and script.is_file():
            try:
                done = _run(
                    [
                        shell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(script),
                        "-Mode",
                        "langs",
                    ],
                    timeout=60.0,
                )
                if done.returncode == 0:
                    tags = tuple(line.strip() for line in done.stdout.splitlines() if line.strip())
            except (OSError, subprocess.SubprocessError):
                tags = ()
        _LANG_CACHE, _LANG_AT, _LANG_PROBED = tags, now, True
        return tags


def _matches(tag: str, lang: str) -> bool:
    """`zh-Hans` 与 `zh-Hans-CN` 视为同一套中文识别器（不同系统版本报的粒度不同）。"""
    tag_l, lang_l = tag.strip().lower(), lang.strip().lower()
    if not tag_l or not lang_l:
        return False
    return tag_l == lang_l or tag_l.startswith(lang_l + "-") or lang_l.startswith(tag_l + "-")


def windows_available(lang: str = DEFAULT_LANG) -> bool:
    return any(_matches(tag, lang) for tag in available_languages())


# ---------------------------------------------------------------- Tesseract 兜底


def tesseract_exe() -> Path | None:
    """先找随包的，再找 PATH，最后找默认安装位置。"""
    bundled = tools_dir() / "tesseract" / "tesseract.exe"
    if bundled.is_file():
        return bundled
    found = shutil.which("tesseract")
    if found:
        return Path(found)
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(key)
        if not base:
            continue
        candidate = Path(base) / "Tesseract-OCR" / "tesseract.exe"
        if candidate.is_file():
            return candidate
    return None


def tesseract_data_dir() -> Path | None:
    """随包的 fast 模型优先：机器上装的那份可能是坏的（实测本机的 chi_sim 被透明加密过，读不了）。"""
    bundled = tools_dir() / "tessdata"
    if bundled.is_dir() and any(bundled.glob("*.traineddata")):
        return bundled
    return None


def tesseract_languages() -> tuple[str, ...]:
    data = tesseract_data_dir()
    if data is not None:
        return tuple(sorted(path.stem for path in data.glob("*.traineddata")))
    exe = tesseract_exe()
    if exe is None:
        return ()
    try:
        done = _run([str(exe), "--list-langs"], timeout=30.0)
    except (OSError, subprocess.SubprocessError):
        return ()
    names = []
    for line in done.stdout.splitlines():
        line = line.strip()
        if not line or line.endswith(":") or " " in line:
            continue  # 头一行是 "List of available languages (2):"
        names.append(line)
    return tuple(sorted(names))


def verify_bundled_tesseract(timeout: float = 60.0) -> tuple[bool, str]:
    """按发布口径验一遍随包的 Tesseract：净化 PATH 之后还能不能跑起来。

    开发机上装着 Tesseract 时，随包那份缺 DLL 也照样能跑（系统目录与安装目录里的同名
    DLL 会被借走，实测 PATH 里带着 `C:\\Program Files\\Tesseract-OCR` 时 rc=0，
    净掉之后 0xC0000135）。所以这里只给 exe 自己的目录 + 系统目录，出包前跑它，
    缺依赖当场暴露，而不是等同事的机器上"兜底"失效。

    返回 `(能不能跑, 说明)`：说明里放第一行版本号，或失败原因。
    """
    exe = tools_dir() / "tesseract" / "tesseract.exe"
    if not exe.is_file():
        return False, "源码树里没有随包的 tesseract.exe"
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    env = {
        key: os.environ[key]
        for key in ("SystemRoot", "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
        if key in os.environ
    }
    env.setdefault("SystemRoot", system_root)
    # 只留 exe 自己的目录：这才是同事机器上的样子
    env["PATH"] = os.pathsep.join([str(exe.parent), str(Path(system_root) / "System32"), system_root])
    try:
        done = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"跑不起来：{exc}"
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        missing = 0xC0000135  # STATUS_DLL_NOT_FOUND：Windows 上 Python 给的是它的有符号形式
        hint = "；多半是 DLL 闭包不全（净 PATH 下运行）" if done.returncode in (missing, missing - 0x100000000) else ""
        return False, f"退出码 {done.returncode}：{detail[0] if detail else '(无输出)'}{hint}"
    text = f"{done.stdout}\n{done.stderr}"
    if "tesseract" not in text.lower():
        return False, f"输出看不懂：{text.strip()[:160]}"
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return True, first


def _lang_codes(lang: str) -> tuple[str, ...]:
    """系统语言标签 → Tesseract 语言代码（中文简繁分开）。"""
    base = lang.strip().lower()
    if base.startswith("zh"):
        return ("chi_tra",) if "hant" in base else ("chi_sim",)
    return (base.split("-")[0],)


# ---------------------------------------------------------------- 选路


def backend(lang: str = DEFAULT_LANG) -> str:
    """当前机器能用哪条 OCR 路："windows" / "tesseract" / ""（都没有）。"""
    if windows_available(lang):
        return "windows"
    exe = tesseract_exe()
    if exe is None:
        return ""
    wanted = _lang_codes(lang)
    have = tesseract_languages()
    if any(code in have for code in wanted) or (not have and wanted):
        return "tesseract"
    return ""


def describe(lang: str = DEFAULT_LANG) -> str:
    """状态窗里显示的一句话（空字符串表示不显示）。"""
    kind = backend(lang)
    if kind == "windows":
        return "Windows 系统 OCR（识别中文扫描件）"
    if kind == "tesseract":
        return "Tesseract 兜底（系统缺中文 OCR 包）"
    return "不可用：扫描件会记为“没有文字层”"


# ---------------------------------------------------------------- 识别


def tidy(text: str) -> str:
    """收尾：清掉 Windows OCR 在汉字之间塞的空格、压掉行尾空白与多余空行。"""
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(rf"(?<=[{_CJK}])[ \t]+", "", text)
    text = re.sub(rf"[ \t]+(?=[{_CJK}])", "", text)
    text = re.sub(r"(?<=\.)[ \t]+(?=\d)", "", text)  # "表 1. 1. 1" → "表1.1.1"
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def ocr_images(
    images: Sequence[tuple[str, Path]], *, lang: str = DEFAULT_LANG, timeout: float | None = None
) -> str | None:
    """识别一批图片（顺序即页序），返回用 `\\f` 分页的文本；一条路都不通返回 None。

    Windows OCR 没识别出东西时再用 Tesseract 试一遍：两条路错字的位置不一样，
    谁的字多留谁（实测同一页 Windows 571 字 / Tesseract 300 字上下）。
    """
    items = [(str(name), Path(path)) for name, path in images]
    if not items:
        return None
    budget = timeout if timeout is not None else max(120.0, 3.0 * len(items) + 30.0)
    text: str | None = None
    if backend(lang) == "windows":
        text = _windows_ocr(items, lang=lang, timeout=budget)
    if (text is None or len(text.strip()) < MIN_OCR_CHARS) and tesseract_exe() is not None:
        fallback = _tesseract_ocr(items, lang=lang, timeout=budget)
        if fallback is not None and len(fallback.strip()) > len((text or "").strip()):
            text = fallback
    return text if text and text.strip() else None


def _windows_ocr(items: list[tuple[str, Path]], *, lang: str, timeout: float) -> str | None:
    script = tools_dir() / "ocr_win.ps1"
    shell = _powershell()
    if not script.is_file() or not shell:
        return None
    with tempfile.TemporaryDirectory(prefix="landoc_ocr_job_") as tmp:
        job = Path(tmp) / "job.json"
        out = Path(tmp) / "out"
        job.write_text(
            json.dumps(
                {"images": [{"id": name, "path": str(path)} for name, path in items]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            done = _run(
                [
                    shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-Mode",
                    "batch",
                    "-JobFile",
                    str(job),
                    "-OutDir",
                    str(out),
                    "-Lang",
                    lang,
                ],
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        pages: list[str] = []
        for name, _ in items:
            target = out / f"{name}.txt"
            try:
                pages.append(target.read_text(encoding="utf-8").strip())
            except OSError:
                pages.append("")
        return "\f".join(pages)


def _tesseract_ocr(items: list[tuple[str, Path]], *, lang: str, timeout: float) -> str | None:
    exe = tesseract_exe()
    if exe is None:
        return None
    have = tesseract_languages()
    codes = [code for code in _lang_codes(lang) if code in have] or list(have[:1]) or ["eng"]
    data = tesseract_data_dir()
    with tempfile.TemporaryDirectory(prefix="landoc_tess_") as tmp:
        listing = Path(tmp) / "pages.txt"
        listing.write_text("\n".join(str(path) for _, path in items), encoding="utf-8")
        outbase = Path(tmp) / "out"
        cmd = [str(exe), str(listing), str(outbase), "-l", "+".join(codes), "--psm", "3"]
        if data is not None:
            cmd += ["--tessdata-dir", str(data)]
        try:
            done = _run(cmd, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        try:
            raw = Path(str(outbase) + ".txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        pages = raw.split("\f")
        if len(pages) < len(items):
            pages += [""] * (len(items) - len(pages))
        return "\f".join(page.strip() for page in pages[: len(items)])
