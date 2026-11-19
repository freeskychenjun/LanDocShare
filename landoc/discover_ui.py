"""可访问的资料库窗口：托盘里一眼看清"全院哪些库、我能看哪些"。

和状态窗口/授权名单窗口一样，tkinter 只能在自己那条线程里创建和刷新，所以
托盘线程只往队列里塞 `show` / `close`，窗口线程定时抽队列；网络查询放在
临时线程里跑，结果再经队列回传——查中心慢一点也不会把界面卡住。
"""

from __future__ import annotations

import os
import queue
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from landoc import discover
from landoc.config import Config
from landoc.panel import _icon_path, release_tk


class DiscoverWindow:
    """`show()` 可以从任意线程调用；关窗只是隐藏，退出走托盘菜单。"""

    AUTO_REFRESH_SECONDS = 60.0
    SHOW_REFRESH_SECONDS = 30.0

    def __init__(self, cfg: Config, *, on_notice=None) -> None:
        self.cfg = cfg
        self.on_notice = on_notice
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root = None
        self._entry = None
        self._tree = None
        self._summary = None
        self._detail = None
        self._rows: dict[str, discover.VisibleRow] = {}
        self._result: discover.LibraryList | None = None
        self._fetching = False
        self._last_attempt = 0.0

    # ---- 任意线程可调 ----
    def show(self) -> None:
        self._ensure_thread()
        self._queue.put(("show", None))

    def close(self) -> None:
        self._queue.put(("close", None))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # 等窗口线程在自己的线程里销毁解释器，避免退出时 Tcl_AsyncDelete
            thread.join(timeout=2.0)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="landoc-discover", daemon=True
        )
        self._thread.start()

    # ---- 窗口线程 ----
    def _run(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        self._root = root
        root.title(f"可访问的资料库 · {self.cfg.name}")
        root.geometry("900x540")
        root.minsize(760, 420)
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
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        title = ttk.Label(
            frame,
            text="可访问的资料库",
            font=("Microsoft YaHei UI", 13, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")
        self._center_label = ttk.Label(frame, text=self._center_text(), foreground="#666")
        self._center_label.grid(row=1, column=0, sticky="w", pady=(2, 10))

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=2, column=0, sticky="we", pady=(0, 8))
        ttk.Label(toolbar, text="姓名（可留空，只按本机 IP 认）").pack(side="left")
        self._entry = ttk.Entry(toolbar, width=18)
        self._entry.pack(side="left", padx=(8, 6))
        self._entry.insert(0, discover.load_viewer_name())
        self._entry.bind("<Return>", lambda _event: self._start_load())
        self._query_button = ttk.Button(toolbar, text="查询", command=self._start_load)
        self._query_button.pack(side="left")
        self._refresh_button = ttk.Button(toolbar, text="刷新", command=self._start_load)
        self._refresh_button.pack(side="left", padx=6)

        table = ttk.Frame(frame)
        table.grid(row=3, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ("owner", "lib", "docs", "status", "heartbeat", "url")
        tree = ttk.Treeview(
            table, columns=columns, show="headings", selectmode="browse", height=12
        )
        headings = {
            "owner": ("库主人", 90),
            "lib": ("库名", 210),
            "docs": ("篇数", 56),
            "status": ("状态", 64),
            "heartbeat": ("上次心跳", 110),
            "url": ("地址", 300),
        }
        for name, (label, width) in headings.items():
            tree.heading(name, text=label, anchor="center")
            tree.column(name, width=width, anchor="center", stretch=name in {"lib", "url"})
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        tree.tag_configure("offline", foreground="#999999")
        tree.bind("<Double-1>", lambda _event: self._open_selected())
        tree.bind("<Return>", lambda _event: self._open_selected())
        self._tree = tree

        self._summary = ttk.Label(frame, text="", foreground="#333")
        self._summary.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self._detail = ttk.Label(
            frame, text="", foreground="#777", wraplength=840, justify="left"
        )
        self._detail.grid(row=5, column=0, sticky="w", pady=(2, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, sticky="w", pady=(10, 0))
        ttk.Button(buttons, text="打开", command=self._open_selected).pack(side="left")
        ttk.Button(buttons, text="复制地址", command=self._copy_selected).pack(
            side="left", padx=6
        )
        ttk.Button(buttons, text="打开中心网页", command=self._open_center).pack(side="left")
        ttk.Button(buttons, text="导出清单…", command=self._export_offline).pack(
            side="left", padx=6
        )
        ttk.Button(buttons, text="关闭", command=root.withdraw).pack(side="left")

        hint = ttk.Label(
            frame,
            text="名单由登记中心按你的 IP（和填写的姓名）过滤；双击一行用浏览器打开对方的库主页。"
            "关掉这个窗口只是隐藏，退出程序请用托盘菜单的“退出”。",
            foreground="#888",
            wraplength=840,
            justify="left",
        )
        hint.grid(row=7, column=0, sticky="w", pady=(8, 0))

        root.bind("<Escape>", lambda _event: root.withdraw())
        self._pump()
        root.mainloop()
        release_tk(self)

    def _center_text(self) -> str:
        registry = (self.cfg.registry_url or "").strip()
        if not registry:
            return "登记中心：未配置（在 config.json 里填 registry_url，或让管理员下发配置）"
        return f"登记中心：{registry}"

    def _pump(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                command, payload = self._queue.get_nowait()
                if command == "show":
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                    self._maybe_load()
                elif command == "close":
                    root.quit()
                    root.destroy()
                    return
                elif command == "loaded":
                    self._on_loaded(payload)  # type: ignore[arg-type]
                elif command == "failed":
                    self._on_failed(str(payload))
        except queue.Empty:
            pass
        self._auto_refresh()
        try:
            root.after(120, self._pump)
        except Exception:  # noqa: BLE001 - 窗口已经销毁
            pass

    def _maybe_load(self) -> None:
        """刚打开时不必每次都问中心：半分钟内查过就沿用上次结果。"""
        if self._fetching:
            return
        if self._result is not None and time.time() - self._last_attempt < self.SHOW_REFRESH_SECONDS:
            return
        self._start_load()

    def _auto_refresh(self) -> None:
        root = self._root
        if root is None or self._fetching:
            return
        try:
            visible = bool(root.winfo_viewable())
        except Exception:  # noqa: BLE001
            return
        if not visible or self._last_attempt <= 0:
            return
        if time.time() - self._last_attempt >= self.AUTO_REFRESH_SECONDS:
            self._start_load()

    def _start_load(self) -> None:
        if self._fetching:
            self._say("正在查询，稍等…")
            return
        viewer = self._entry.get().strip() if self._entry is not None else ""
        try:
            discover.save_viewer_name(viewer)
        except OSError:
            pass
        registry = (self.cfg.registry_url or "").strip()
        self._fetching = True
        self._last_attempt = time.time()
        self._set_busy(True)
        self._say(f"正在向 {registry} 查询…" if registry else "还没有配置登记中心地址，先看看说明…")

        def work() -> None:
            try:
                result = discover.fetch_libraries(registry, viewer)
            except Exception as exc:  # noqa: BLE001 - 任何意外都别炸窗口线程
                self._queue.put(("failed", f"{type(exc).__name__}: {exc}"))
            else:
                self._queue.put(("loaded", result))

        threading.Thread(target=work, name="landoc-discover-fetch", daemon=True).start()

    def _on_loaded(self, result: discover.LibraryList) -> None:
        self._fetching = False
        self._set_busy(False)
        self._result = result
        self._render(result)
        self._summary.configure(text=discover.summary_text(result))
        detail = discover.upgrade_note(result)
        if result.source == "cache" and result.error:
            detail = f"{result.error}" + (f" · {detail}" if detail else "")
        if not result.libraries:
            detail = discover.empty_hint(result, viewer=result.viewer)
        self._detail.configure(text=detail, foreground="#b45309" if result.error else "#777")

    def _on_failed(self, message: str) -> None:
        self._fetching = False
        self._set_busy(False)
        self._summary.configure(text="查询失败")
        self._detail.configure(text=message, foreground="#b00020")

    def _render(self, result: discover.LibraryList) -> None:
        tree = self._tree
        if tree is None:
            return
        selected = tree.selection()
        keep = None
        if selected:
            row = self._rows.get(selected[0])
            keep = row.key if row is not None else None
        tree.delete(*tree.get_children())
        self._rows.clear()
        for row in discover.rows(result, self_name=self.cfg.name):
            label = row.lib_name + row.note
            item = tree.insert(
                "",
                "end",
                values=(row.owner, label, row.doc_count, row.status, row.heartbeat, row.url),
                tags=() if row.status == "在线" else ("offline",),
            )
            self._rows[item] = row
            if keep and row.key == keep:
                tree.selection_set(item)

    def _set_busy(self, busy: bool) -> None:
        for button in (getattr(self, "_query_button", None), getattr(self, "_refresh_button", None)):
            if button is None:
                continue
            try:
                button.configure(state="disabled" if busy else "normal")
            except Exception:  # noqa: BLE001 - 窗口刚销毁
                pass
        button = getattr(self, "_query_button", None)
        if button is not None:
            try:
                button.configure(text="查询中…" if busy else "查询")
            except Exception:  # noqa: BLE001
                pass

    def _say(self, text: str) -> None:
        if self._detail is not None:
            try:
                self._detail.configure(text=text, foreground="#777")
            except Exception:  # noqa: BLE001
                pass

    def _selected_row(self) -> discover.VisibleRow | None:
        tree = self._tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        return self._rows.get(selection[0])

    def _open_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            self._say("先选一个库，再点“打开”。")
            return
        webbrowser.open(row.url)

    def _copy_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            self._say("先选一个库，再点“复制地址”。")
            return
        root = self._root
        if root is None:
            return
        root.clipboard_clear()
        root.clipboard_append(row.url)
        self._say(f"已复制：{row.url}")

    def _open_center(self) -> None:
        registry = (self.cfg.registry_url or "").strip()
        if not registry:
            self._say("还没有配置登记中心地址（config.json 里的 registry_url）。")
            return
        viewer = self._entry.get().strip() if self._entry is not None else ""
        url = registry.rstrip("/") + "/"
        if viewer:
            url += "?name=" + quote(viewer, safe="")
        webbrowser.open(url)

    def _export_offline(self) -> None:
        """把当前名单存成能双击打开的网页——中心挂了也有份地址备份。"""
        result = self._result
        if result is None:
            self._say("先点“查询”拿到名单，再导出。")
            return
        from tkinter import filedialog

        target = filedialog.asksaveasfilename(
            parent=self._root,
            title="把库地址清单存成网页",
            initialdir=str(discover.app_data_dir()),
            initialfile=discover.OFFLINE_NAME,
            defaultextension=".html",
            filetypes=[("网页", "*.html"), ("所有文件", "*.*")],
        )
        if not target:
            return
        try:
            path = discover.export_offline(result, Path(target))
        except OSError as exc:
            self._say(f"导出失败：{exc}")
            return
        self._say(f"已导出：{path}（双击用浏览器打开，也可以直接发给同事）")
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 打不开就只提示路径
            pass


__all__ = ["DiscoverWindow"]
