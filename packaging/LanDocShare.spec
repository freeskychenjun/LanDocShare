# -*- mode: python ; coding: utf-8 -*-
"""LanDocShare 的 PyInstaller 规格（onedir，不进单个 exe）。

为什么 onedir：设计 §6.1 —— 每次启动不用解压几十 MB，COM/资源定位也更省事。
产物：dist/LanDocShare/LanDocShare.exe（无控制台）+ LanDocShare-debug.exe（有控制台，排错用）。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - PyInstaller 注入

datas = [
    (str(ROOT / "landoc" / "web" / "index.html"), "landoc/web"),
    # 状态窗口标题栏要用它
    (str(ROOT / "packaging" / "landoc.ico"), "landoc"),
    # 登记中心的 /skill.zip 现场打包要用这份模板（设计 §9 分发）
    (str(ROOT / "skill" / "lan-lib-search"), "skill/lan-lib-search"),
    # 扫描件 OCR：Windows OCR 桥脚本 + Tesseract（exe + 35 个 DLL 的完整闭包 + 中英 fast
    # 模型，共约 27 MB）。系统缺中文识别包的机器靠它兜底，见 landoc/ocr.py；
    # 闭包必须完整 —— 出包前 packaging/release.py 会用净化 PATH 跑一遍 --version。
    (str(ROOT / "landoc" / "tools" / "ocr_win.ps1"), "landoc/tools"),
    (str(ROOT / "landoc" / "tools" / "tesseract"), "landoc/tools/tesseract"),
    (str(ROOT / "landoc" / "tools" / "tessdata"), "landoc/tools/tessdata"),
]

hiddenimports = [
    "win32com.client",
    "win32com.server",
    "pythoncom",
    "pywintypes",
    "win32timezone",
    "pystray._win32",
    "psutil",  # netinfo 用它挑默认允许网段；3.8 前没声明，只有开发机上碰巧装着
    "PIL._tkinter_finder",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "landoc.registry",
]

# 这台开发机上装了很多与库软件无关的重型包（torch/cv2/scipy/…）。PyInstaller 的
# 分析会顺着可选 import 把它们拖进来，整包会从 ~150 MB 膨胀到 1.2 GB。
# 库软件运行时不 import 它们（已用 sys.modules 验证），所以在这里整包排除。
EXCLUDES = [
    "jieba",  # 检索走 FTS5 trigram，代码里从来没 import 过它（旧依赖留下的）
    # OpenBLAS 一个 DLL 就 20 MB，numpy 全加起来 26.6 MB —— 库软件一行都不用到它，
    # 是被别的包的可选 import 拖进来的（3.7）。
    "numpy",
    "numpy.linalg",
    # pywin32 的 Pythonwin（mfc140u.dll 之类 11.8 MB）：托盘只要 pystray/win32api，
    # 从不 import win32ui，拖进来纯属白占（3.7）。
    "win32ui",
    "torch",
    "torchvision",
    "torchaudio",
    "functorch",
    "torchgen",
    "cv2",
    "scipy",
    "onnxruntime",
    "llvmlite",
    "numba",
    "skimage",
    "sklearn",
    "tifffile",
    "imageio",
    "imageio_ffmpeg",
    "av",
    "transformers",
    "botocore",
    "boto3",
    "s3transfer",
    "pandas",
    "matplotlib",
    "IPython",
    "jupyter",
    "notebook",
    "sympy",
    "pytest",
    "setuptools",
    "pip",
    "pkg_resources",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "landoc" / "tray.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

# PyInstaller 会把 `landoc/tools/` 下的 DLL 当成二进制依赖，除 datas 里那份之外再往
# `_internal/` 顶层平铺一份（实测多出 5 个文件、8.3 MB）。tesseract.exe 只按自己所在
# 目录找 DLL，顶层那份没人用，白占（1.2）。判据看**目标路径**：留在 `landoc/tools/`
# 里的要，落到顶层的丢 —— 只按源路径过滤会把该留的那份一起砍掉（踩过）。
_TOOLS = str(ROOT / "landoc" / "tools")
a.binaries = [
    entry
    for entry in a.binaries
    if not (
        str(entry[1]).startswith(_TOOLS)
        and not str(entry[0]).replace("\\", "/").startswith("landoc/tools/")
    )
]
pyz = PYZ(a.pure)  # noqa: F821

gui_exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LanDocShare",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "landoc.ico"),
)
debug_exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LanDocShare-debug",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ROOT / "packaging" / "landoc.ico"),
)
coll = COLLECT(  # noqa: F821
    gui_exe,
    debug_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LanDocShare",
)
