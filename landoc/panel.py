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

from landoc.app import AppStatus, LibraryApp
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


def phase_text(status: AppStatus) -> str:
    """第一行只讲"在干什么"，篇数/时间另有专行，别重复。"""
    text = _PHASE_TEXT.get(status.phase, status.phase)
    if status.phase == "converting" and status.converted:
        return f"{text}：已转换 {status.converted} 篇"
    if status.message:
        return f"{text} · {status.message}"
    return text


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
    rows = [
        PanelRow("状态", phase_text(status)),
        PanelRow("文档", f"{status.doc_count} 篇"),
        PanelRow("共享范围", scope),
        PanelRow("服务地址", f"http://{lan_ip}:{actual_port}/"),
        PanelRow("上次扫描", status.last_scan or "还没扫过"),
    ]
    if getattr(cfg, "registry_url", ""):
        rows.append(PanelRow("登记中心", status.registry or "等待心跳…"))
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
    ) -> None:
        self.cfg = cfg
        self.app = app
        self.lan_ip = lan_ip
        self.on_open_page = on_open_page
        self.on_update = on_update
        self.on_toggle_share = on_toggle_share
        self.on_open_roster = on_open_roster

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
        root.geometry("580x360" if len(rows) > 5 else "580x330")
        root.minsize(520, 300)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        icon = _icon_path()
        if icon is not None:
            try:
                root.iconbitmap(default=str(icon))
            except Exception:  # noqa: BLE001 - 图标失败不影响使用
                pass
        root.withdraw()

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        title = ttk.Label(frame, text=f"LanDocShare · {self.cfg.name}", font=("Microsoft YaHei UI", 13, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        row = 1
        for item in rows:
            ttk.Label(frame, text=item.label, foreground="#666").grid(
                row=row, column=0, sticky="w", pady=2
            )
            value = ttk.Label(frame, text=item.value)
            value.grid(row=row, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=2)
            self._labels[item.label] = value
            row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="w", pady=(16, 0))
        ttk.Button(buttons, text="打开检索页", command=self._open_page).pack(side="left")
        ttk.Button(buttons, text="立即更新", command=self._update_now).pack(side="left", padx=6)
        self._share_button = ttk.Button(buttons, text="暂停共享", command=self._toggle_share)
        self._share_button.pack(side="left")
        ttk.Button(buttons, text="授权名单", command=self._open_roster).pack(side="left", padx=6)
        ttk.Button(buttons, text="复制地址", command=self._copy_address).pack(side="left")
        ttk.Button(buttons, text="隐藏", command=root.withdraw).pack(side="left", padx=6)

        self._copy_hint = ttk.Label(frame, text="", foreground="#2f7d32")
        self._copy_hint.grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        hint = ttk.Label(
            frame,
            text="关掉这个窗口只是隐藏（双击托盘图标可再打开）；退出程序请用托盘菜单的“退出”。",
            foreground="#888",
        )
        hint.grid(row=row + 2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self._pump()
        root.mainloop()

    def _pump(self) -> None:
        """每 120 ms 抽一次命令；顺便每 1.5 s 刷一次状态。"""
        root = self._root
        if root is None:
            return
        try:
            while True:
                command, _payload = self._queue.get_nowait()
                if command == "show":
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                    self._refresh()
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
        except Exception:  # noqa: BLE001 - 窗口刚销毁时 configure 会抛
            pass

    # ---- 按钮回调 ----
    def _open_page(self) -> None:
        if self.on_open_page is not None:
            self.on_open_page()

    def _update_now(self) -> None:
        if self.on_update is not None:
            self.on_update()

    def _toggle_share(self) -> None:
        if self.on_toggle_share is not None:
            self.on_toggle_share()
        self._refresh()

    def _open_roster(self) -> None:
        if self.on_open_roster is not None:
            self.on_open_roster()

    def _copy_address(self) -> None:
        root = self._root
        if root is None:
            return
        status = self.app.status
        address = f"http://{self.lan_ip or '127.0.0.1'}:{status.port}/"
        root.clipboard_clear()
        root.clipboard_append(address)
        self._copy_hint.configure(text=f"已复制：{address}")

        def clear() -> None:
            try:
                self._copy_hint.configure(text="")
            except Exception:  # noqa: BLE001 - 窗口已关
                pass

        root.after(1800, clear)
