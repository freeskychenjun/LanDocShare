"""首次运行向导（tkinter，一屏三项：库名、文件夹、共享范围）。

设计 §6.2 要求 ≤3 步；这里合成一屏，少点一次"下一步"。没有桌面环境时
（比如远程会话里跑测试）只提供 `build_config()` / `apply_config()`，不弹窗。
"""

from __future__ import annotations

import ctypes
import ipaddress
import queue
import threading
from dataclasses import dataclass
from pathlib import Path

from landoc import probe
from landoc.config import Config
from landoc.roster import MAX_DEPARTMENT_CHARS

SCOPE_LAN = "lan"
SCOPE_PEOPLE = "people"
SCOPE_LOCAL = "local"
SCOPE_CIDRS = {
    SCOPE_LAN: ["10.91.0.0/16"],
    # 名单是在网段之上再叠一层：先过网段，再认 IP（认不出 IP 时才看姓名）
    SCOPE_PEOPLE: ["10.91.0.0/16"],
    SCOPE_LOCAL: ["127.0.0.1"],
}

DRIVE_REMOVABLE = 2
DRIVE_REMOTE = 4
DRIVE_FIXED = 3

# 粗略估算用的常数（来自实测：Office 启动 ~6 秒一次，之后热路径 ~2 秒/篇）
_SECONDS_PER_DOC = 2.0
_SECONDS_PER_ROUND = 6.0
_ESTIMATE_CAP = 20000


def _drive_type(path: Path) -> int:
    """盘类型（kernel32.GetDriveTypeW）；没有盘符时按固定盘处理。"""
    drive = path.drive or ""
    if not drive:
        return DRIVE_FIXED
    return int(ctypes.WinDLL("kernel32").GetDriveTypeW(drive + "\\"))


@dataclass(frozen=True)
class FolderEstimate:
    """一个（或一组）文件夹的粗估，只用来给用户一个"大概多少"的感觉。"""

    docs: int
    size_bytes: int
    seconds: float
    capped: bool = False

    def describe(self) -> str:
        if self.docs == 0:
            return "没扫到可转换的文档（下面这些扩展名才算：doc/docx/wps/ppt/pptx/dps/pdf）"
        size = self.size_bytes / (1024 * 1024)
        size_text = f"{size / 1024:.1f} GB" if size >= 1024 else f"{size:.0f} MB"
        minutes = self.seconds / 60
        time_text = f"约 {minutes:.0f} 分钟" if minutes >= 1 else f"约 {self.seconds:.0f} 秒"
        prefix = "≥" if self.capped else "约"
        return f"{prefix} {self.docs} 篇 · {size_text} · {time_text}"


def estimate_folders(folders: list[Path], cap: int = _ESTIMATE_CAP) -> FolderEstimate:
    """数一遍候选文档并累加体积（只 walk + stat，不打开文件）。"""
    docs = 0
    total = 0
    capped = False
    for path in probe._iter_docs(folders):
        if docs >= cap:
            capped = True
            break
        total += path.stat().st_size
        docs += 1
    return FolderEstimate(
        docs=docs,
        size_bytes=total,
        seconds=_SECONDS_PER_ROUND + _SECONDS_PER_DOC * docs,
        capped=capped,
    )


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


def validate_person(name: str, ip: str, cidrs: list[str] | None = None) -> str | None:
    """名单一项的校验；返回不能加的原因，能加返回 None。

    IP 必填——授权只看来源 IP，姓名只用于日志/分组，知道姓名不能代替 IP；
    填了就必须是合法 IPv4 且落在允许的网段里，否则他永远连不上。
    """
    clean = (name or "").strip()
    if not clean:
        return "姓名不能为空"
    if any(ch in clean for ch in "=,:;|/\\"):
        return "姓名里不要带 = , : ; | / \\ 之类的分隔符"
    address = (ip or "").strip()
    if not address:
        return "IP 不能为空（姓名不能当作访问凭证）"
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return f"“{address}” 不是合法的 IP 地址"
    for cidr in cidrs or []:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if parsed.version == network.version and parsed in network:
            return None
    return f"{address} 不在允许的网段（{'、'.join(cidrs or [])}）里，这个人会连不上"


def parse_cidrs(text: str) -> tuple[list[str], str | None]:
    """把"10.91.0.0/16, 10.91.1.0/24"这类输入解析成规范网段列表。

    返回 (网段列表, 错误说明)；空输入按默认网段处理。
    """
    raw = (text or "").replace("，", ",").replace(";", ",").replace(" ", ",")
    parts = [part for part in (p.strip() for p in raw.split(",")) if part]
    if not parts:
        return list(SCOPE_CIDRS[SCOPE_LAN]), None
    networks: list[str] = []
    for part in parts:
        try:
            networks.append(str(ipaddress.ip_network(part, strict=False)))
        except ValueError:
            return [], f"“{part}” 不是网段（示例：10.91.0.0/16 或 10.91.1.0/24）"
    return networks, None


def build_config(
    name: str,
    folders: list[Path],
    scope: str,
    lib_dir: Path,
    *,
    acrobat_tool_dir: str | None = None,
    people: dict[str, str] | None = None,
    departments: dict[str, str] | None = None,
    cidrs: list[str] | None = None,
) -> Config:
    """把向导里的选择拼成 Config（纯函数，方便测试）。"""
    clean_name = (name or "").strip() or "我的资料库"
    if scope == SCOPE_LOCAL:
        # "仅本机"是硬语义，不接受外部网段
        allowed = list(SCOPE_CIDRS[SCOPE_LOCAL])
    else:
        allowed = list(cidrs or SCOPE_CIDRS.get(scope, SCOPE_CIDRS[SCOPE_LAN]))
    allow_people: dict[str, str] = {}
    people_departments: dict[str, str] = {}
    if scope == SCOPE_PEOPLE:
        for person, address in (people or {}).items():
            if (person or "").strip():
                allow_people[person.strip()] = (address or "").strip()
        for person, department in (departments or {}).items():
            clean = _clean_department(department)
            if person in allow_people and clean:
                people_departments[person] = clean
    return Config(
        name=clean_name,
        lib_dir=Path(lib_dir),
        folders=[Path(f) for f in folders],
        allow_cidrs=allowed,
        allow_people=allow_people,
        people_departments=people_departments,
        acrobat_tool_dir=acrobat_tool_dir or "",
    )


def apply_config(target: Config, source: Config) -> Config:
    """把向导结果写回**正在运行**的 Config（就地改，不换对象）。

    服务端每个请求现读 cfg、调度线程每轮现读 cfg.folders，所以改完即生效；
    存储策略（store_content/render_md/snippet_only_chars）不在向导里，保持原样。
    """
    target.name = source.name
    target.folders = [Path(f) for f in source.folders]
    target.allow_cidrs = list(source.allow_cidrs)
    target.allow_people = dict(source.allow_people)
    target.people_departments = dict(source.people_departments)
    target.acrobat_tool_dir = source.acrobat_tool_dir
    return target


def _clean_department(text: str) -> str:
    """部门/备注只做轻校验：去掉分隔符、压缩空白、限长。"""
    clean = " ".join((text or "").split())
    if any(ch in clean for ch in ",;\t"):
        clean = clean.replace(",", "").replace(";", "").replace("\t", "")
    return clean[:MAX_DEPARTMENT_CHARS]


def run_wizard(
    lib_dir: Path,
    parent=None,
    *,
    save: bool = True,
    current: Config | None = None,
) -> Config | None:  # pragma: no cover - 需要桌面
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from landoc.cli import _find_acrobat_tool_dir

    result: dict[str, Config | None] = {"config": None}

    root = tk.Tk()
    root.title("LanDocShare · 建库向导" if current is None else "LanDocShare · 修改建库设置")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()

    ttk.Label(frame, text="库名称").grid(row=0, column=0, sticky="w")
    default_name = current.name if current is not None else f"{Path.home().name}的资料库"
    # 必须显式绑定 master=root：向导可能从正在运行的托盘/状态窗口里打开，
    # 此时进程里已经有一个 Tk 根窗口；不写 master 的 StringVar 会绑到那个
    # 旧根上，新向导里的输入框/单选钮就会显示空白、没有选中项。
    name_var = tk.StringVar(master=root, value=default_name)
    ttk.Entry(frame, textvariable=name_var, width=42).grid(row=0, column=1, columnspan=2, sticky="we", pady=4)

    ttk.Label(frame, text="共享哪些文件夹").grid(row=1, column=0, sticky="nw", pady=(8, 0))
    listbox = tk.Listbox(frame, height=5, width=42, selectmode="extended")
    listbox.grid(row=1, column=1, sticky="we", pady=4)
    if current is not None:
        for folder in current.folders:
            listbox.insert("end", str(folder))

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
            kick_estimate()

    def remove_folder() -> None:
        for index in reversed(listbox.curselection()):
            listbox.delete(index)
        kick_estimate()

    buttons = ttk.Frame(frame)
    buttons.grid(row=1, column=2, sticky="n")
    ttk.Button(buttons, text="添加…", command=add_folder).grid(row=0, column=0, sticky="we")
    ttk.Button(buttons, text="移除", command=remove_folder).grid(row=1, column=0, sticky="we", pady=4)

    estimate_label = ttk.Label(frame, text="还没选文件夹", foreground="#666")
    estimate_label.grid(row=2, column=1, columnspan=2, sticky="w")

    # 估算在后台线程做（大目录 walk 要一两秒），结果用队列送回 tk 线程
    est_queue: queue.Queue[tuple[int, FolderEstimate]] = queue.Queue()
    est_token = {"n": 0}

    def kick_estimate() -> None:
        est_token["n"] += 1
        token = est_token["n"]
        chosen = [Path(p) for p in listbox.get(0, "end")]
        if not chosen:
            estimate_label.configure(text="还没选文件夹")
            return
        estimate_label.configure(text="正在估算…")

        def work() -> None:
            try:
                estimate = estimate_folders(chosen)
            except OSError:
                estimate = FolderEstimate(0, 0, 0.0)
            est_queue.put((token, estimate))

        threading.Thread(target=work, name="landoc-estimate", daemon=True).start()

    def poll_estimate() -> None:
        try:
            while True:
                token, estimate = est_queue.get_nowait()
                if token == est_token["n"]:
                    estimate_label.configure(text=estimate.describe())
        except queue.Empty:
            pass
        root.after(250, poll_estimate)

    ttk.Label(frame, text="谁能查").grid(row=3, column=0, sticky="w", pady=(8, 0))
    default_scope = SCOPE_LAN
    if current is not None:
        if current.allow_people:
            default_scope = SCOPE_PEOPLE
        elif list(current.allow_cidrs) == SCOPE_CIDRS[SCOPE_LOCAL]:
            default_scope = SCOPE_LOCAL
    scope_var = tk.StringVar(master=root, value=default_scope)
    ttk.Radiobutton(frame, text="公司内网（网段见下一行）", variable=scope_var, value=SCOPE_LAN).grid(
        row=3, column=1, columnspan=2, sticky="w"
    )
    ttk.Radiobutton(frame, text="仅指定名单（按 IP 放行，姓名仅备注）", variable=scope_var, value=SCOPE_PEOPLE).grid(
        row=4, column=1, columnspan=2, sticky="w"
    )
    ttk.Radiobutton(frame, text="仅本机", variable=scope_var, value=SCOPE_LOCAL).grid(
        row=5, column=1, columnspan=2, sticky="w"
    )

    ttk.Label(frame, text="允许的网段").grid(row=6, column=0, sticky="w", pady=(6, 0))
    default_cidrs = "、".join(
        current.allow_cidrs
        if current is not None and list(current.allow_cidrs) != SCOPE_CIDRS[SCOPE_LOCAL]
        else SCOPE_CIDRS[SCOPE_LAN]
    )
    cidr_var = tk.StringVar(master=root, value=default_cidrs.replace("、", ", "))
    cidr_entry = ttk.Entry(frame, textvariable=cidr_var, width=28)
    cidr_entry.grid(row=6, column=1, sticky="w", pady=(6, 0))
    ttk.Label(frame, text="多个用逗号分隔", foreground="#666").grid(
        row=6, column=2, sticky="w", padx=(6, 0), pady=(6, 0)
    )

    scope_hint = ttk.Label(frame, text="", foreground="#666", wraplength=390, justify="left")
    scope_hint.grid(row=7, column=1, columnspan=2, sticky="w", pady=(6, 0))

    def sync_scope_state() -> None:
        scope = scope_var.get()
        cidr_entry.configure(state="disabled" if scope == SCOPE_LOCAL else "normal")
        if scope == SCOPE_PEOPLE:
            if current is None:
                text = (
                    "首次建库先选「公司内网」或「仅本机」。\n"
                    "建好后在「授权名单…」窗口添加，再回来改「仅指定名单」。"
                )
            elif current.allow_people:
                text = (
                    f"名单：已登记 {len(current.allow_people)} 人。\n"
                    "在「授权名单…」窗口维护，这里不重复编辑。"
                )
            else:
                text = (
                    "名单：还没有登记任何人。\n"
                    "先在「授权名单…」窗口添加，再回来选「仅指定名单」。"
                )
        elif scope == SCOPE_LOCAL:
            text = "仅本机：只有这台电脑能查；同事打不开。"
        else:
            text = "公司内网：网段内、且不在拒绝网段里的机器都能查；不启用姓名名单。"
        scope_hint.configure(text=text)

    for radio in frame.winfo_children():
        if isinstance(radio, ttk.Radiobutton):
            radio.configure(command=sync_scope_state)

    note = (
        "索引与 md 副本都是文档全文明文，只落在本机；\n"
        "对外只提供命中片段与表格，不提供全文或下载。"
    )
    ttk.Label(frame, text=note, foreground="#666").grid(
        row=8, column=0, columnspan=3, sticky="w", pady=(10, 4)
    )

    def confirm() -> None:
        folders = [Path(p) for p in listbox.get(0, "end")]
        if not folders:
            messagebox.showwarning("还没选文件夹", "至少选一个要共享的文件夹。")
            return
        people: dict[str, str] = {}
        departments: dict[str, str] = {}
        if scope_var.get() == SCOPE_PEOPLE:
            if current is None or not current.allow_people:
                messagebox.showwarning(
                    "名单还是空的",
                    "「仅指定名单」需要先在库软件窗口或托盘菜单「授权名单…」里添加人员。\n\n"
                    "首次建库请先选「公司内网」或「仅本机」，建好后再去添加。",
                )
                return
            people = dict(current.allow_people)
            departments = dict(current.people_departments)
        tool = _find_acrobat_tool_dir()
        cidrs = None
        if scope_var.get() != SCOPE_LOCAL:
            cidrs, cidr_error = parse_cidrs(cidr_var.get())
            if cidr_error:
                messagebox.showwarning("网段填得不对", cidr_error)
                return
        result["config"] = build_config(
            name_var.get(),
            folders,
            scope_var.get(),
            lib_dir,
            acrobat_tool_dir=str(tool) if tool else "",
            people=people,
            departments=departments,
            cidrs=cidrs,
        )
        root.destroy()

    actions = ttk.Frame(frame)
    actions.grid(row=9, column=0, columnspan=3, sticky="e", pady=(12, 0))
    ttk.Button(actions, text="取消", command=root.destroy).grid(row=0, column=0, padx=4)
    ttk.Button(actions, text="保存" if current is not None else "建库", command=confirm).grid(
        row=0, column=1
    )

    sync_scope_state()
    poll_estimate()
    kick_estimate()
    root.mainloop()
    cfg = result["config"]
    if cfg is not None and save:
        cfg.lib_dir.mkdir(parents=True, exist_ok=True)
        cfg.save()
    return cfg
