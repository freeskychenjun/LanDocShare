"""轻量状态窗口：托盘之外"一眼看清"的那一屏。

为什么需要它：托盘只有一个图标，双击最多开浏览器；库主人想确认"转了多少篇、
现在共享给谁、地址是多少"时不该每次都去开网页。

设计约束：
- tkinter 必须在自己那条线程里创建和刷新，所以托盘线程只往队列里塞命令
  （`show` / `hide` / `close`），窗口线程用 `after()` 定时抽队列；
- 关窗口 = 隐藏，不是退出；退出只走托盘菜单的"退出"。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path

from landoc.app import AppStatus, LibraryApp, eta_phrase
from landoc.config import Config


@dataclass(frozen=True)
class PanelRow:
    label: str
    value: str


_PHASE_TEXT = {
    "starting": "启动中…",
    "serving": "正在共享",
    "converting": "建库中",
    "paused": "共享已暂停",
    "error": "出错了",
    "stopped": "已停止",
}

_HINT_TEXT = "关掉这个窗口只是隐藏（双击托盘图标可再打开）；退出程序请用托盘菜单的“退出”。"


def phase_text(status: AppStatus) -> str:
    """第一行只讲"在干什么"，篇数/时间另有专行，别重复。"""
    text = _PHASE_TEXT.get(status.phase, status.phase)
    if status.phase == "converting" and status.converted:
        line = f"{text}：已转换 {status.converted} 篇"
        # 还剩多少/要多久跟"已转换"一起说，否则上千篇时这一行永远像卡住了
        if status.pending > 0:
            line += f" · 还剩 {status.pending} 篇"
            if status.eta_seconds > 0:
                line += f"（{eta_phrase(status.eta_seconds)}）"
        return line
    if status.message:
        return f"{text} · {status.message}"
    if status.gone:
        # 库只增不减：源文档删了/共享盘没插，内容仍在库里能搜 —— 但别让人以为一切照旧
        return f"{text} · {status.gone} 篇源文件已不在（内容仍在库中）"
    return text


def update_button_text(status: AppStatus) -> str:
    """「立即更新」在建库时变成「停止建库」：手动触发的建库可能跑很久，得能停。"""
    return "停止建库" if status.phase == "converting" else "立即更新"


def prune_button_state(status: AppStatus) -> tuple[str, bool]:
    """「清理…」按钮的文字与是否可点，返回 `(文字, 能不能点)`。

    建库那一轮整轮把 state 与索引攥在手里（收尾整份写回），清理插进去会被覆盖、
    还会撞上索引的 5 秒忙等，所以 `converting` 期间一律置灰。纯函数，便于单测。
    """
    if not status.gone:
        return "清理…", False
    return f"清理 {status.gone} 篇", status.phase != "converting"


def center_position(width: int, height: int, area: tuple[int, int, int, int]) -> tuple[int, int]:
    """把 `width×height` 的窗口摆到 `area=(left, top, right, bottom)` 的正中间。

    纯函数：坐标算错在界面上很难发现，所以它单独可测。窗口比屏幕还大时贴左上角，
    别算出负坐标把标题栏顶到屏幕外（那种窗口拖不回来）。
    """
    left, top, right, bottom = area
    x = left + max(0, (right - left - width) // 2)
    y = top + max(0, (bottom - top - height) // 2)
    return x, y


def _monitor_work_area() -> tuple[int, int, int, int] | None:
    """光标所在显示器的工作区（已去掉任务栏）；问不到就返回 None。

    用 `rcWork` 而不是整屏：1080 高的屏幕上任务栏占 40 px，按整屏居中会让窗口
    底边压在任务栏底下。多显示器时跟着鼠标走，否则第二块屏上的用户永远看不到窗口。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", _Rect),
                ("rcWork", _Rect),
                ("dwFlags", ctypes.c_ulong),
            ]

        user32 = ctypes.windll.user32
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("GetCursorPos 失败")
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        # 2 = MONITOR_DEFAULTTONEAREST：鼠标不在任何显示器上时给最近的那块
        monitor = user32.MonitorFromPoint(wintypes.POINT(point.x, point.y), 2)
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise OSError("GetMonitorInfoW 失败")
        rect = info.rcWork
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise OSError("工作区为空")
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:  # noqa: BLE001 - 拿不到就退回 Tk 的整屏
        return None


def work_area(root) -> tuple[int, int, int, int]:
    """可用的摆放区域：优先光标所在显示器的工作区，退路是 Tk 报的整屏。"""
    area = _monitor_work_area()
    if area is not None:
        return area
    return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())


def _window_size(root) -> tuple[int, int]:
    """窗口宽高：已显示用实际值，还没显示就用 `geometry()` 里写下的那个数。"""
    root.update_idletasks()
    width, height = root.winfo_width(), root.winfo_height()
    if width > 1 and height > 1:
        return width, height
    try:
        # 形如 "580x220+670+410"；只要前半截
        size = root.geometry().split("+", 1)[0]
        width_text, height_text = size.split("x")
        return max(int(width_text), 1), max(int(height_text), 1)
    except Exception:  # noqa: BLE001 - 解析不出就按布局要求的大小
        return max(root.winfo_reqwidth(), 1), max(root.winfo_reqheight(), 1)


def center_window(root, size: tuple[int, int] | None = None) -> None:
    """把窗口挪到屏幕正中：托盘「显示状态窗口」每次打开都回到中间，不用满屏找。

    `size` 给还没显示、Tk 也报不出尺寸的窗口用（建库向导就是先摆好再 mainloop
    的）：这时 `winfo_width()` 只有 1、`geometry()` 也是 "1x1"，照它居中会把
    窗口摆到屏幕左上角，所以调用方把布局要求的大小算好传进来。
    """
    if size is None:
        width, height = _window_size(root)
        x, y = center_position(width, height, work_area(root))
        root.geometry(f"+{x}+{y}")
        return
    width, height = size
    x, y = center_position(width, height, work_area(root))
    root.geometry(f"{width}x{height}+{x}+{y}")


def version_text(status: AppStatus) -> str:
    """版本行：本机版本，以及（发现新版本时）目标版本与当前进展。"""
    current = f"v{status.version}"
    if not status.update_version:
        return current
    if status.update_phase == "ready":
        return f"{current} → v{status.update_version}（已下载，可升级）"
    if status.update_phase == "downloading":
        return f"{current} → v{status.update_version}（{status.update_message or '下载中…'}）"
    if status.update_phase == "manual":
        return f"{current} → v{status.update_version}（版本跨度大，需手动重装）"
    return f"{current} → v{status.update_version}（{status.update_message or '发现新版本'}）"


def status_rows(
    cfg: Config, status: AppStatus, *, lan_ip: str, port: int | None = None
) -> list[PanelRow]:
    """窗口里显示哪几行（纯函数，方便单测）。"""
    actual_port = port if port is not None else status.port
    people = getattr(cfg, "allow_people", None) or {}
    if status.paused:
        scope = "已暂停（本机之外都访问不到）"
    elif people:
        scope = f"仅指定名单（{len(people)} 人）"
    elif cfg.allow_cidrs:
        scope = "、".join(cfg.allow_cidrs)
    else:
        scope = "仅本机（没有允许的网段）"
    deny = list(getattr(cfg, "deny_cidrs", None) or [])
    if deny:
        # 拒绝网段优先于允许网段：只显示允许范围会让人以为这些地址也能查
        scope += f" · 拒绝 {len(deny)} 段"
    if status.denied:
        # 有人在门外试着进来：不写这一行，主人只会在日志里"事后"发现
        where = f"（{status.denied_note}）" if status.denied_note else ""
        scope += f" · 最近有 {status.denied} 次访问被拒{where}"
    docs = f"{status.doc_count} 篇"
    if status.failed:
        # 失败常驻可见：不然只有日志里知道"有些文件一直没收进来"
        docs += f" · {status.failed} 篇转换失败"
    rows = [
        PanelRow("状态", phase_text(status)),
        PanelRow("文档", docs),
    ]
    if status.pending > 0:
        eta = f"（{eta_phrase(status.eta_seconds)}）" if status.eta_seconds > 0 else ""
        rows.append(PanelRow("待收录", f"{status.pending} 篇{eta}"))
    if status.ocr:
        # 扫描件 OCR 用哪条路：缺中文识别包时这里会直说"不可用"，省得翻日志
        rows.append(PanelRow("扫描件 OCR", status.ocr))
    rows += [
        PanelRow("共享范围", scope),
        PanelRow("服务地址", f"http://{lan_ip}:{actual_port}/"),
        PanelRow("上次扫描", status.last_scan or "还没扫过"),
    ]
    if getattr(cfg, "registry_url", ""):
        rows.append(PanelRow("登记中心", status.registry or "等待心跳…"))
    rows.append(PanelRow("版本", version_text(status)))
    return rows


def _icon_path() -> Path | None:
    """标题栏图标：源码跑在 packaging/，打包后跟着 datas 落在 landoc/。"""
    for candidate in (
        Path(__file__).with_name("landoc.ico"),
        Path(__file__).resolve().parents[1] / "packaging" / "landoc.ico",
    ):
        if candidate.exists():
            return candidate
    return None


def release_tk(window: object) -> None:
    """在窗口线程里把 tkinter 对象全部丢引用（必须在创建解释器的线程里调用）。

    Tk 解释器只能由创建它的线程销毁。窗口关掉后如果这些对象留给别的线程
    （退出时通常是主线程）回收，Tcl 会报
    "Tcl_AsyncDelete: async handler deleted by the wrong thread"，
    进程退出码变成 1——用户看到的就是"点退出，程序却报错"。

    容器（dict/list）里也可能存着 Label/Button（比如状态面板的 `_labels`），
    所以要连容器一起清，不能只看一层属性。
    """
    import gc
    import tkinter as tk

    def is_tk(value: object) -> bool:
        return isinstance(value, (tk.Misc, tk.Variable))

    for key, value in list(vars(window).items()):
        if is_tk(value):
            setattr(window, key, None)
        elif isinstance(value, dict):
            for inner_key, inner in list(value.items()):
                if is_tk(inner):
                    value.pop(inner_key, None)
        elif isinstance(value, list):
            value[:] = [item for item in value if not is_tk(item)]

    if getattr(tk, "_default_root", None) is not None:
        try:
            tk._default_root = None
        except Exception:  # noqa: BLE001 - 老版本 tkinter 没这个全局
            pass
    gc.collect()


def attach_transient(window, master) -> None:
    """把 `window` 挂成 `master` 的 transient（父窗口关了它也跟着关）。

    注意 Windows 上 Tk 的行为：父窗口如果正收着（withdraw），子窗口也会跟着
    不显示——看起来就是"点了没反应"。所以父窗口不可见时干脆不挂，让它当独立
    窗口自己显示。
    """
    try:
        if master is not None and master.winfo_viewable():
            window.transient(master.winfo_toplevel())
    except Exception:  # noqa: BLE001 - 挂不上就算了，不影响显示
        pass


def present(window) -> None:
    """把窗口摆到最前面一次（不长期置顶）。"""
    try:
        window.deiconify()
        window.lift()
        window.focus_force()
        window.attributes("-topmost", True)
        window.after(200, lambda: window.attributes("-topmost", False))
    except Exception:  # noqa: BLE001 - 窗口已经没了
        pass


class StatusPanel:
    """一个常驻线程里跑 tkinter；`show()` 可以从任意线程调用。"""

    def __init__(
        self,
        cfg: Config,
        app: LibraryApp,
        *,
        lan_ip: str = "",
        on_open_page=None,
        on_update=None,
        on_toggle_share=None,
        on_open_roster=None,
        on_open_access=None,
        on_install=None,
        on_prune=None,
    ) -> None:
        self.cfg = cfg
        self.app = app
        self.lan_ip = lan_ip
        self.on_open_page = on_open_page
        self.on_update = on_update
        self.on_toggle_share = on_toggle_share
        self.on_open_roster = on_open_roster
        self.on_open_access = on_open_access
        self.on_install = on_install
        self.on_prune = on_prune

        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root = None
        self._labels: dict[str, object] = {}
        self._refresh_job = None

    # ---- 任意线程可调 ----
    def show(self) -> None:
        self._ensure_thread()
        self._queue.put(("show", None))

    def hide(self) -> None:
        self._queue.put(("hide", None))

    def close(self) -> None:
        self._queue.put(("close", None))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # 等窗口线程把解释器在它自己那条线程里销毁干净，别把 Tk 对象
            # 留给主线程回收（那样退出时会报 Tcl_AsyncDelete）
            thread.join(timeout=2.0)

    def run_modal(self, fn):
        """在窗口线程里跑 `fn(master)`，等它返回，返回它的结果。

        Tk 解释器只能由创建它的那条线程碰。要在托盘里弹一个对话框（建库向导、
        选文件夹……）时，必须把这件事排进窗口线程，而不是自己另起线程再开一个
        `tk.Tk()`——两个解释器跨线程，Windows 上会直接崩在 tcl86t.dll。
        """
        self._ensure_thread()
        box: dict[str, object] = {}
        done = threading.Event()

        # 不弹状态窗口：对话框自己就是独立窗口，用不着把面板一起摆出来给
        # 用户看（早先为了绕开"transient 的父窗口收着、子窗口也不显示"，
        # 这里先 show 一次，结果用户右键"修改建库设置"时面板跟着冒出来）。

        def wrapper(root):
            try:
                box["value"] = fn(root)
            except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程
                box["error"] = exc
            finally:
                done.set()

        self._queue.put(("call", wrapper))
        done.wait()
        error = box.get("error")
        if error is not None:
            raise error  # type: ignore[misc]
        return box.get("value")

    def call(self, fn) -> None:
        """在窗口线程里执行 `fn(root)`，不等结果。

        用来开那些"不是对话框、开完就还给我"的窗口（授权名单、可访问的库）：
        它们必须建在这条 Tk 线程上，但调用方不该被卡住。
        """
        self._ensure_thread()
        self._queue.put(("call", fn))

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="landoc-panel", daemon=True)
        self._thread.start()

    # ---- 窗口线程 ----
    def _run(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        self._root = root
        root.title(f"LanDocShare · {self.cfg.name}")
        rows = status_rows(self.cfg, self.app.status, lan_ip=self.lan_ip or "127.0.0.1")
        root.minsize(520, 280)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        icon = _icon_path()
        if icon is not None:
            try:
                root.iconbitmap(default=str(icon))
            except Exception:  # noqa: BLE001 - 图标失败不影响使用
                pass
        root.withdraw()

        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        title = ttk.Label(frame, text=f"LanDocShare · {self.cfg.name}", font=("Microsoft YaHei UI", 13, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        row = 1
        for item in rows:
            ttk.Label(frame, text=item.label, foreground="#666").grid(
                row=row, column=0, sticky="w", pady=1
            )
            value = ttk.Label(frame, text=item.value)
            value.grid(row=row, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=1)
            self._labels[item.label] = value
            row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(buttons, text="打开检索页", command=self._open_page).pack(side="left")
        self._update_button = ttk.Button(buttons, text="立即更新", command=self._update_now)
        self._update_button.pack(side="left", padx=6)
        self._share_button = ttk.Button(buttons, text="暂停共享", command=self._toggle_share)
        self._share_button.pack(side="left")
        ttk.Button(buttons, text="授权名单", command=self._open_roster).pack(side="left", padx=6)
        ttk.Button(buttons, text="复制地址", command=self._copy_address).pack(side="left")
        ttk.Button(buttons, text="隐藏", command=root.withdraw).pack(side="left", padx=6)

        # 升级按钮单独一行：有新版时它是这里最该点的按钮，挤在第一行会被截断
        actions = ttk.Frame(frame)
        actions.grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._upgrade_button = ttk.Button(actions, text="检查更新", command=self._upgrade)
        self._upgrade_button.pack(side="left")
        ttk.Button(actions, text="可访问的库", command=self._open_access).pack(
            side="left", padx=6
        )
        # 库只增不减：源文件删了/改名了/移动硬盘没插，库里的副本要有人点头才删
        self._prune_button = ttk.Button(actions, text="清理…", command=self._prune)
        self._prune_button.pack(side="left")
        self._prune_button.state(["disabled"])

        hint = ttk.Label(
            frame,
            text=_HINT_TEXT,
            foreground="#888",
        )
        hint.grid(row=row + 2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._hint = hint

        # 先让 Tk 按内容算出自然高度，再固定宽度；否则会留下窗外空白。
        root.update_idletasks()
        width = max(580, root.winfo_reqwidth())
        root.geometry(f"{width}x{root.winfo_reqheight()}")

        self._pump()
        root.mainloop()
        release_tk(self)

    def _pump(self) -> None:
        """每 120 ms 抽一次命令；顺便每 1.5 s 刷一次状态。"""
        root = self._root
        if root is None:
            return
        try:
            while True:
                command, payload = self._queue.get_nowait()
                if command == "show":
                    # 先摆正中再显示：浮动窗口每次出现在屏幕中心，不用去边角找
                    center_window(root)
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                    self._refresh()
                elif command == "call":
                    # 调用方在自己的线程里等结果；这里可以直接跑（模态时它会
                    # 一直占着窗口线程，直到对话框关掉——正是想要的效果）
                    if callable(payload):
                        payload(root)
                elif command == "hide":
                    root.withdraw()
                elif command == "close":
                    root.quit()
                    root.destroy()
                    return
        except queue.Empty:
            pass
        self._refresh()
        try:
            root.after(120, self._pump)
        except Exception:  # noqa: BLE001 - 窗口已经销毁
            pass

    def _refresh(self) -> None:
        root = self._root
        if root is None:
            return
        status = self.app.status
        try:
            for item in status_rows(self.cfg, status, lan_ip=self.lan_ip or "127.0.0.1"):
                widget = self._labels.get(item.label)
                if widget is not None:
                    widget.configure(text=item.value)
            self._share_button.configure(text="恢复共享" if status.paused else "暂停共享")
            update_button = getattr(self, "_update_button", None)
            if update_button is not None:
                update_button.configure(text=update_button_text(status))
            button = getattr(self, "_upgrade_button", None)
            if button is not None:
                if status.update_phase == "ready" and status.update_version:
                    label = f"升级到 v{status.update_version}"
                elif status.update_phase == "downloading":
                    label = "下载中…"
                elif status.update_phase == "failed":
                    label = "重试下载"
                elif status.update_version:
                    label = "下载更新"
                else:
                    label = "检查更新"
                button.configure(text=label)
            prune_button = getattr(self, "_prune_button", None)
            if prune_button is not None:
                text, enabled = prune_button_state(status)
                prune_button.configure(text=text)
                prune_button.state(["!disabled"] if enabled else ["disabled"])
        except Exception:  # noqa: BLE001 - 窗口刚销毁时 configure 会抛
            pass

    # ---- 按钮回调 ----
    def _open_page(self) -> None:
        if self.on_open_page is not None:
            self.on_open_page()

    def _update_now(self) -> None:
        """同一个按钮两用：没在转就「立即更新」，正在转就是「停止建库」。"""
        if self.app.building:
            self.app.stop_building()
            self._refresh()
            return
        if self.on_update is not None:
            self.on_update()
        self._refresh()

    def _toggle_share(self) -> None:
        if self.on_toggle_share is not None:
            self.on_toggle_share()
        self._refresh()

    def _open_roster(self) -> None:
        if self.on_open_roster is not None:
            self.on_open_roster()

    def _open_access(self) -> None:
        if self.on_open_access is not None:
            self.on_open_access()

    def _upgrade(self) -> None:
        """一个按钮三种动作：没发现→检查；已发现→下载；已下载→确认后升级。"""
        status = self.app.status
        if self.app.update_ready:
            from tkinter import messagebox

            ok = messagebox.askyesno(
                "升级 LanDocShare",
                f"立即升级到 v{status.update_version}？\n\n"
                "程序会自动重启（约 3 秒）；资料库数据在独立目录里，不受影响。\n"
                "如果新版本启动失败，会自动回滚到现在的版本。",
                parent=self._root,
            )
            if not ok:
                return
            if self.on_install is not None:
                self.on_install()
                return
            result = self.app.install_update()
            if result != "ok":
                messagebox.showwarning("没能升级", result, parent=self._root)
            return
        if status.update_version:
            self.app.download_update_async()
            return
        self.app.check_updates_async(announce=True)

    def _prune(self) -> None:
        """删掉"源文件已不在"的文档：库只增不减，这是唯一的瘦身出口。

        要点一次确认：删掉的是库里的副本与索引行，删完就搜不到了；源文件不动。
        """
        from tkinter import messagebox

        gone = self.app.status.gone
        if gone <= 0:
            return
        ok = messagebox.askyesno(
            "清理库",
            f"有 {gone} 篇文档的源文件已经不在（被删掉、改名、移走，或移动硬盘没插）。\n\n"
            "要从库里删掉这些文档的副本和索引行吗？删完就搜不到它们了；"
            "源文件本身不受影响。",
            parent=self._root,
        )
        if not ok:
            return
        if self.on_prune is None:
            return
        try:
            removed = self.on_prune()
        except Exception as exc:  # noqa: BLE001 - 删库失败必须让人看见
            from landoc.pipeline import PruneBusy

            if isinstance(exc, PruneBusy):
                # 建库正在跑：不是错误，是"这次没轮到"，说清楚就好
                messagebox.showinfo("清理库", str(exc), parent=self._root)
            else:
                messagebox.showerror("清理库", f"清理失败：{exc}", parent=self._root)
            return
        self._refresh()
        messagebox.showinfo("清理库", f"已清理 {removed} 篇。", parent=self._root)

    def _copy_address(self) -> None:
        root = self._root
        if root is None:
            return
        status = self.app.status
        address = f"http://{self.lan_ip or '127.0.0.1'}:{status.port}/"
        root.clipboard_clear()
        root.clipboard_append(address)
        self._hint.configure(text=f"已复制：{address}", foreground="#2f7d32")

        def clear() -> None:
            try:
                self._hint.configure(
                    text=_HINT_TEXT,
                    foreground="#888",
                )
            except Exception:  # noqa: BLE001 - 窗口已关
                pass

        root.after(1800, clear)


class HostedWindow:
    """把"自己开一个 Tk 根窗口"的窗口类挂到状态面板那条 Tk 线程上。

    一个进程里只能有一个 Tk 解释器、也只能由一条线程碰它。早先状态窗口、授权
    名单、可访问的库各自 `tk.Tk()` + `mainloop()`，等于同时开了好几个解释器，
    保存/关闭时随机崩在 tcl86t.dll（0x80000003）。

    子类要做的事只有三件：
      1. `__init__` 里调 `self._bind_host(host)`（host 是 `StatusPanel`）；
      2. 把搭界面的那一段收进 `_build(container)`，别自己 mainloop；
      3. `show()` / `close()` 里用 `self._show_via_host()` / `self._close_via_host()`。

    没给 host 时（单测、或以后有人单独用）仍走老路：自己当根窗口。
    """

    _host = None
    _window = None

    def _bind_host(self, host) -> None:
        if host is not None and not hasattr(host, "call"):
            raise TypeError("host 需要提供 call(fn)：窗口要排在它的 Tk 线程上")
        self._host = host

    @property
    def hosted(self) -> bool:
        return self._host is not None

    # ---- 子类在 show()/close() 里调 ----
    def _show_via_host(self) -> None:
        self._host.call(self._open_hosted)

    def _close_via_host(self) -> None:
        self._host.call(self._close_hosted)

    # ---- 一定要在面板线程里跑的两个 ----
    def _open_hosted(self, root) -> None:
        import tkinter as tk

        window = self._window
        if window is not None:
            try:
                alive = bool(window.winfo_exists())
            except Exception:  # noqa: BLE001 - 解释器没了就别再碰
                alive = False
            if not alive:
                window = None
        if window is None:
            window = tk.Toplevel(root)
            self._window = window
            attach_transient(window, root)
            self._build(window)
        self._on_show()
        present(window)

    def _close_hosted(self, _root) -> None:
        window = self._window
        self._window = None
        self._root = None
        if window is not None:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001 - 已经没了
                pass
        # 控件引用要在这条线程里丢掉：留给主线程回收会在退出时报
        # "Tcl_AsyncDelete: async handler deleted by the wrong thread"
        release_tk(self)

    # ---- 关窗：挂载时只销毁自己的 Toplevel ----
    def _shutdown_window(self, container) -> None:
        if self._host is not None:
            self._window = None
            self._root = None
            try:
                container.destroy()
            except Exception:  # noqa: BLE001
                pass
            release_tk(self)
            return
        container.quit()
        container.destroy()
