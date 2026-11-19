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
    # 扫描件 OCR：Windows OCR 桥脚本 + Tesseract 最小集（exe + 6 个 DLL + 中英 fast 模型，
    # 共约 15 MB）。系统缺中文识别包的机器靠它兜底，见 landoc/ocr.py。
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
