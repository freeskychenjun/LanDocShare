"""授权名单管理窗口：库主人自己维护"谁能查"（姓名 / IP / 部门），并能和登记中心对账。

「发布到中心」只出现在中心那台机器上（`roster.is_center_machine()`）——名单只由管理员
维护一份，同事的窗口里只有「从中心拉取…」，不存在被谁顺手改掉的入口。

为什么单独一个窗口而不是塞进状态面板：名单要加人、改 IP、标部门、和中心同步，
是个"表格 + 按钮"的活；状态面板只负责"一眼看清现在什么状态"。

线程模型见 `panel.HostedWindow`：给了 host（状态窗口）时，本窗口作为它的 Toplevel
开在面板那条 Tk 线程上；没有 host 才自己当根窗口——一个进程里只该有一个 Tk 解释器，
两个解释器跨线程在 Windows 上会崩在 tcl86t.dll。
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from landoc import roster
from landoc.config import Config
from landoc.panel import HostedWindow, center_window, release_tk


def _icon_path() -> Path | None:
    for candidate in (
        Path(__file__).with_name("landoc.ico"),
        Path(__file__).resolve().parents[1] / "packaging" / "landoc.ico",
    ):
        if candidate.exists():
            return candidate
    return None


class RosterWindow(HostedWindow):
    """名单编辑窗口；`show()` 可从任意线程调用。"""

    def __init__(
        self,
        cfg: Config,
        *,
        on_saved=None,
        on_notice=None,
        host=None,
    ) -> None:
        self.cfg = cfg
        self.on_saved = on_saved
        self.on_notice = on_notice
        self._bind_host(host)

        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root = None
        self._tree = None
        self._status = None
        self._roster: roster.Roster = roster.Roster()
        self._dirty = False
        self._busy = False
        self._is_admin = False

    # ---------------------------------------------------------- 任意线程可调
    def show(self) -> None:
        if self.hosted:
            self._show_via_host()
            return
        self._ensure_thread()
        self._queue.put(("show", None))

    def close(self) -> None:
        if self.hosted:
            self._close_via_host()
            return
        self._queue.put(("close", None))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # 等窗口线程在自己的线程里销毁解释器，避免退出时 Tcl_AsyncDelete
            thread.join(timeout=2.0)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="landoc-roster", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------- 窗口
    def _run(self) -> None:
        """没有 host 时：自己当根窗口（单测/单独用）。"""
        import tkinter as tk

        root = tk.Tk()
        self._build(root)
        center_window(root)  # 弹在屏幕正中，别落在左上角
        root.mainloop()
        release_tk(self)

    def _on_show(self) -> None:
        self._reload()

    def _build(self, root) -> None:
        """在给定容器里搭界面并起队列泵；不 mainloop，关窗才返回。"""
        import tkinter as tk
        from tkinter import ttk

        self._root = root
        root.title(f"授权名单 · {self.cfg.name}")
        root.geometry("700x520")
        root.minsize(620, 420)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        icon = _icon_path()
        if icon is not None:
            try:
                root.iconbitmap(default=str(icon))
            except Exception:  # noqa: BLE001 - 图标失败不影响使用
                pass
        root.withdraw()

        # 是不是"中心那台机器"：只有它改中心名单，同事的窗口里干脆不出现「发布到中心」
        self._is_admin = roster.is_center_machine(getattr(self.cfg, "registry_url", "") or "")

        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Label(frame, text="授权名单", font=("Microsoft YaHei UI", 13, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            frame,
            text="按 IP 放行；姓名只用于日志/分组，不能当作访问凭证。IP 必填，空 IP 条目不授权。",
            foreground="#666",
        ).grid(row=1, column=0, sticky="w", pady=(0, 8))

        table = ttk.Frame(frame)
        table.grid(row=2, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self._tree = ttk.Treeview(
            table, columns=("name", "ip", "dept"), show="headings", selectmode="browse", height=14
        )
        self._tree.heading("name", text="姓名")
        self._tree.heading("ip", text="IP")
        self._tree.heading("dept", text="部门 / 备注")
        self._tree.column("name", width=140, anchor="w")
        self._tree.column("ip", width=150, anchor="w")
        self._tree.column("dept", width=220, anchor="w")
        self._tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self._tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.bind("<Double-1>", lambda _event: self._edit_selected())

        edit = ttk.Frame(frame)
        edit.grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Button(edit, text="添加", command=self._add).pack(side="left")
        ttk.Button(edit, text="修改", command=self._edit_selected).pack(side="left", padx=6)
        ttk.Button(edit, text="删除", command=self._remove_selected).pack(side="left")
        ttk.Button(edit, text="导入 CSV…", command=self._import_csv).pack(side="left", padx=(18, 6))
        ttk.Button(edit, text="导出 CSV…", command=self._export_csv).pack(side="left")

        sync = ttk.Frame(frame)
        sync.grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Button(sync, text="从中心拉取…", command=self._pull).pack(side="left")
        if self._is_admin:
            ttk.Button(sync, text="发布到中心", command=self._publish).pack(side="left", padx=6)
            ttk.Label(sync, text="（名单由你维护，发布后同事拉一次就同步）", foreground="#999").pack(
                side="left", padx=(6, 0)
            )
        else:
            ttk.Label(
                sync,
                text="名单由管理员在中心那台机器上统一维护：这里只能拉取，不能改中心名单",
                foreground="#999",
            ).pack(side="left", padx=(6, 0))

        bottom = ttk.Frame(frame)
        bottom.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        bottom.columnconfigure(0, weight=1)
        self._status = ttk.Label(bottom, text="", foreground="#666")
        self._status.grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="保存并使生效", command=self._save).grid(row=0, column=1, padx=6)
        ttk.Button(bottom, text="关闭", command=root.withdraw).grid(row=0, column=2)

        self._center_hint = ttk.Label(
            frame,
            text=f"登记中心：{getattr(self.cfg, 'registry_url', '') or '未配置'}",
            foreground="#888",
        )
        self._center_hint.grid(row=6, column=0, sticky="w", pady=(10, 0))

        self._pump()

    # ---------------------------------------------------------- 队列泵
    def _pump(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                command, payload = self._queue.get_nowait()
                if command == "show":
                    self._reload()
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                elif command == "close":
                    self._shutdown_window(root)
                    return
                elif command == "pulled":
                    self._on_pulled(payload)
                elif command == "published":
                    self._set_busy(False)
                    self._say(f"已发布到中心：{payload.get('count', '?')} 人（{payload.get('server_time', '')}）")
                elif command == "error":
                    self._set_busy(False)
                    self._say(f"没成功：{payload}")
        except queue.Empty:
            pass
        try:
            root.after(120, self._pump)
        except Exception:  # noqa: BLE001 - 窗口已销毁
            pass

    # ---------------------------------------------------------- 数据
    def _reload(self) -> None:
        self._roster = roster.Roster.from_config(self.cfg)
        self._dirty = False
        self._render()
        if not self._roster:
            self._say("名单为空：现在按允许的网段放行")
            return
        depts = len({*self._roster.departments.values()})
        extra = f"，{depts} 个部门" if depts else ""
        self._say(f"共 {len(self._roster)} 人{extra}；改完点「保存并使生效」")

    def _render(self) -> None:
        tree = self._tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for row, (name, ip, dept) in enumerate(roster.sorted_entries(self._roster)):
            tree.insert("", "end", iid=str(row), values=(name, ip or "（未填 IP，不授权）", dept or "—"))

    def _selected(self) -> tuple[str, str, str] | None:
        tree = self._tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        values = tree.item(selection[0], "values")
        ip = "" if values[1] == "（未填 IP，不授权）" else str(values[1])
        dept = "" if values[2] == "—" else str(values[2])
        return str(values[0]), ip, dept

    def _say(self, text: str) -> None:
        if self._status is not None:
            self._status.configure(text=text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if self._status is not None:
            self._status.configure(foreground="#b45309" if busy else "#666")

    # ---------------------------------------------------------- 编辑
    def _ask_entry(
        self, title: str, name: str = "", ip: str = "", dept: str = ""
    ) -> tuple[str, str, str] | None:
        import tkinter as tk
        from tkinter import messagebox, ttk

        root = self._root
        if root is None:
            return None
        dialog = tk.Toplevel(root)
        dialog.title(title)
        dialog.transient(root)
        dialog.resizable(False, False)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="姓名").grid(row=0, column=0, sticky="w", pady=3)
        name_var = tk.StringVar(master=dialog, value=name)
        name_entry = ttk.Entry(frame, textvariable=name_var, width=28)
        name_entry.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=3)

        ttk.Label(frame, text="IP（必填）").grid(row=1, column=0, sticky="w", pady=3)
        ip_var = tk.StringVar(master=dialog, value=ip)
        ttk.Entry(frame, textvariable=ip_var, width=28).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=3
        )
        ttk.Label(frame, text="姓名只用于日志/分组，不能代替 IP；空 IP 不授权", foreground="#888").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        ttk.Label(frame, text="部门 / 备注（可留空）").grid(row=3, column=0, sticky="w", pady=(8, 3))
        dept_var = tk.StringVar(master=dialog, value=dept)
        ttk.Entry(frame, textvariable=dept_var, width=28).grid(
            row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 3)
        )

        result: dict[str, tuple[str, str, str] | None] = {"value": None}

        def confirm() -> None:
            try:
                result["value"] = roster.validate_entry(
                    name_var.get(), ip_var.get(), dept_var.get()
                )
            except ValueError as exc:
                messagebox.showerror(title, str(exc), parent=dialog)
                return
            dialog.destroy()

        row = ttk.Frame(frame)
        row.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(row, text="取消", command=dialog.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="确定", command=confirm).pack(side="right")
        name_entry.focus_set()
        dialog.bind("<Return>", lambda _event: confirm())
        root.wait_window(dialog)
        return result["value"]

    def _add(self) -> None:
        entry = self._ask_entry("添加")
        if entry is None:
            return
        name, ip, dept = entry
        if name in self._roster.people:
            self._say(f"「{name}」已经在名单里了")
            return
        self._roster.people[name] = ip
        if dept:
            self._roster.departments[name] = dept
        self._dirty = True
        self._render()
        self._say(f"已添加 {name}，记得保存")

    def _edit_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            self._say("先选中一行")
            return
        old_name, old_ip, old_dept = selected
        entry = self._ask_entry("修改", old_name, old_ip, old_dept)
        if entry is None:
            return
        name, ip, dept = entry
        if name != old_name and name in self._roster.people:
            self._say(f"「{name}」已经在名单里了")
            return
        if name != old_name:
            self._roster.people.pop(old_name, None)
            self._roster.departments.pop(old_name, None)
        self._roster.people[name] = ip
        if dept:
            self._roster.departments[name] = dept
        else:
            self._roster.departments.pop(name, None)
        self._dirty = True
        self._render()
        self._say(f"已修改 {name}，记得保存")

    def _remove_selected(self) -> None:
        from tkinter import messagebox

        selected = self._selected()
        if selected is None:
            self._say("先选中一行")
            return
        name, _ip, _dept = selected
        if not messagebox.askyesno("删除", f"把「{name}」从名单里删掉？", parent=self._root):
            return
        self._roster.people.pop(name, None)
        self._roster.departments.pop(name, None)
        self._dirty = True
        self._render()
        self._say(f"已删除 {name}，记得保存")

    # ---------------------------------------------------------- 导入导出
    def _import_csv(self) -> None:
        from tkinter import filedialog, messagebox

        path = filedialog.askopenfilename(
            parent=self._root,
            title="导入名单 CSV（姓名,IP,部门）",
            filetypes=[("CSV / 文本", "*.csv *.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            try:
                text = Path(path).read_text(encoding="gbk")
            except (OSError, UnicodeDecodeError) as exc:
                messagebox.showerror("导入失败", f"读不了这个文件：{exc}", parent=self._root)
                return
        try:
            incoming, warnings = roster.parse_roster(text)
        except ValueError as exc:
            messagebox.showerror("导入失败", str(exc), parent=self._root)
            return
        if not incoming:
            self._say("这个文件里没解析出人名")
            return
        merged, note = roster.apply_pull(self._roster, incoming)
        self._roster = merged
        self._dirty = True
        self._render()
        extra = f"；{len(warnings)} 条警告" if warnings else ""
        self._say(f"导入 {len(incoming)} 人{extra}：{note}")

    def _export_csv(self) -> None:
        from tkinter import filedialog, messagebox

        path = filedialog.asksaveasfilename(
            parent=self._root,
            title="导出名单 CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        try:
            Path(path).write_text(roster.format_roster(self._roster), encoding="utf-8-sig")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self._root)
            return
        self._say(f"已导出 {len(self._roster)} 人到 {path}")

    # ---------------------------------------------------------- 中心同步
    def _pull(self) -> None:
        from tkinter import messagebox

        registry = getattr(self.cfg, "registry_url", "") or ""
        if not registry:
            messagebox.showinfo(
                "还没配登记中心",
                "config.json 里还没填 registry_url，无法从中心拉取名单。",
                parent=self._root,
            )
            return
        replace = messagebox.askyesno(
            "怎么并？",
            "点「是」= 用中心名单整体替换本地；\n点「否」= 合并（同名以中心为准）。",
            parent=self._root,
        )
        self._set_busy(True)
        self._say("正在从中心拉取…")

        def work() -> None:
            try:
                _text, incoming = roster.fetch_roster(registry)
                self._queue.put(("pulled", (incoming, replace)))
            except roster.RosterError as exc:
                self._queue.put(("error", str(exc)))

        threading.Thread(target=work, name="landoc-roster-pull", daemon=True).start()

    def _on_pulled(self, payload) -> None:
        self._set_busy(False)
        incoming, replace = payload
        try:
            merged, note = roster.apply_pull(self._roster, incoming, replace=replace)
        except roster.RosterError as exc:
            self._say(str(exc))
            return
        self._roster = merged
        self._dirty = True
        self._render()
        self._say(f"{note}，记得保存")

    def _publish(self) -> None:
        from tkinter import messagebox

        registry = getattr(self.cfg, "registry_url", "") or ""
        if not registry:
            messagebox.showinfo(
                "还没配登记中心",
                "config.json 里还没填 registry_url，无法发布名单。",
                parent=self._root,
            )
            return
        if not self._is_admin:
            self._say("名单由管理员在中心那台机器上维护；这里可以用「从中心拉取…」同步")
            return
        if not self._roster:
            self._say("名单是空的，发布没有意义")
            return
        if not messagebox.askyesno(
            "发布名单",
            f"把本地这份 {len(self._roster)} 人的名单发布到登记中心？\n"
            "发布后，全部门同事从中心拉到的名单都会跟着变。",
            parent=self._root,
        ):
            return
        self._set_busy(True)
        self._say("正在发布…")

        def work() -> None:
            try:
                payload = roster.publish_roster(registry, self._roster.copy())
                self._queue.put(("published", payload))
            except roster.RosterError as exc:
                self._queue.put(("error", str(exc)))

        threading.Thread(target=work, name="landoc-roster-publish", daemon=True).start()

    # ---------------------------------------------------------- 保存
    def _save(self) -> None:
        self._roster.to_config(self.cfg)
        try:
            self.cfg.save()
        except OSError as exc:
            self._say(f"保存失败：{exc}")
            return
        self._dirty = False
        count = len(self.cfg.allow_people)
        depts = len(self.cfg.people_departments)
        extra = f"，{depts} 个部门" if depts else ""
        self._say(f"已保存（{count} 人{extra}），立即生效")
        if self.on_saved is not None:
            self.on_saved()
