"""首次运行向导（tkinter，一屏三项：库名、文件夹、共享范围）。

设计 §6.2 要求 ≤3 步；这里合成一屏，少点一次"下一步"。没有桌面环境时
（比如远程会话里跑测试）只提供 `build_config()`，不弹窗。
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from landoc.config import Config

SCOPE_LAN = "lan"
SCOPE_LOCAL = "local"
SCOPE_CIDRS = {SCOPE_LAN: ["10.91.0.0/16"], SCOPE_LOCAL: ["127.0.0.1"]}

DRIVE_REMOVABLE = 2
DRIVE_REMOTE = 4
DRIVE_FIXED = 3


def _drive_type(path: Path) -> int:
    """盘类型（kernel32.GetDriveTypeW）；没有盘符时按固定盘处理。"""
    drive = path.drive or ""
    if not drive:
        return DRIVE_FIXED
    return int(ctypes.WinDLL("kernel32").GetDriveTypeW(drive + "\\"))


def validate_folder(path: Path) -> str | None:
    """返回不能共享的原因；能共享返回 None（设计 D8：v1 只支持本地固定盘）。"""
    if str(path).startswith("\\\\"):
        return "不支持网络共享路径（UNC），请选本机固定盘上的文件夹"
    if not path.exists():
        return "目录不存在"
    if not path.is_dir():
        return "不是文件夹"
    kind = _drive_type(path)
    if kind == DRIVE_REMOTE:
        return "不支持网络盘（同事的数据应在自己本机的固定盘上）"
    if kind == DRIVE_REMOVABLE:
        return "不支持 U 盘等可移动盘（拔出后库就断了）"
    return None


def build_config(
    name: str,
    folders: list[Path],
    scope: str,
    lib_dir: Path,
    *,
    acrobat_tool_dir: str | None = None,
) -> Config:
    """把向导里的选择拼成 Config（纯函数，方便测试）。"""
    clean_name = (name or "").strip() or "我的资料库"
    cidrs = list(SCOPE_CIDRS.get(scope, SCOPE_CIDRS[SCOPE_LAN]))
    return Config(
        name=clean_name,
        lib_dir=Path(lib_dir),
        folders=[Path(f) for f in folders],
        allow_cidrs=cidrs,
        acrobat_tool_dir=acrobat_tool_dir or "",
    )


def run_wizard(lib_dir: Path, parent=None) -> Config | None:  # pragma: no cover - 需要桌面
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from landoc.cli import _find_acrobat_tool_dir

    result: dict[str, Config | None] = {"config": None}

    root = tk.Tk()
    root.title("LanDocShare · 建库向导")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()

    ttk.Label(frame, text="库名称").grid(row=0, column=0, sticky="w")
    name_var = tk.StringVar(value=f"{Path.home().name}的资料库")
    ttk.Entry(frame, textvariable=name_var, width=42).grid(row=0, column=1, columnspan=2, sticky="we", pady=4)

    ttk.Label(frame, text="共享哪些文件夹").grid(row=1, column=0, sticky="nw", pady=(8, 0))
    listbox = tk.Listbox(frame, height=5, width=42, selectmode="extended")
    listbox.grid(row=1, column=1, sticky="we", pady=4)

    def add_folder() -> None:
        chosen = filedialog.askdirectory(title="选择要共享的文件夹")
        if not chosen:
            return
        problem = validate_folder(Path(chosen))
        if problem:
            messagebox.showwarning("这个文件夹不行", problem)
            return
        if chosen not in listbox.get(0, "end"):
            listbox.insert("end", chosen)

    def remove_folder() -> None:
        for index in reversed(listbox.curselection()):
            listbox.delete(index)

    buttons = ttk.Frame(frame)
    buttons.grid(row=1, column=2, sticky="n")
    ttk.Button(buttons, text="添加…", command=add_folder).grid(row=0, column=0, sticky="we")
    ttk.Button(buttons, text="移除", command=remove_folder).grid(row=1, column=0, sticky="we", pady=4)

    ttk.Label(frame, text="谁能查").grid(row=2, column=0, sticky="w", pady=(8, 0))
    scope_var = tk.StringVar(value=SCOPE_LAN)
    ttk.Radiobutton(frame, text="规划院内网 10.91.0.0/16", variable=scope_var, value=SCOPE_LAN).grid(
        row=2, column=1, sticky="w"
    )
    ttk.Radiobutton(frame, text="仅本机", variable=scope_var, value=SCOPE_LOCAL).grid(
        row=3, column=1, sticky="w"
    )

    note = (
        "索引与 md 副本都是文档全文明文，只落在本机；\n"
        "对外只提供命中片段与表格，不提供全文或下载。"
    )
    ttk.Label(frame, text=note, foreground="#666").grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))

    def confirm() -> None:
        folders = [Path(p) for p in listbox.get(0, "end")]
        if not folders:
            messagebox.showwarning("还没选文件夹", "至少选一个要共享的文件夹。")
            return
        tool = _find_acrobat_tool_dir()
        result["config"] = build_config(
            name_var.get(),
            folders,
            scope_var.get(),
            lib_dir,
            acrobat_tool_dir=str(tool) if tool else "",
        )
        root.destroy()

    actions = ttk.Frame(frame)
    actions.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
    ttk.Button(actions, text="取消", command=root.destroy).grid(row=0, column=0, padx=4)
    ttk.Button(actions, text="建库", command=confirm).grid(row=0, column=1)

    root.mainloop()
    cfg = result["config"]
    if cfg is not None:
        cfg.lib_dir.mkdir(parents=True, exist_ok=True)
        cfg.save()
    return cfg
