"""托盘程序：常驻、单实例、开机自启、空闲门控建库（设计 §6.1）。

打包后同事双击的就是这个入口（`python -m landoc.tray` 等价）。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import webbrowser
from pathlib import Path

from PIL import Image, ImageDraw

from landoc import __version__, autostart
from landoc.app import LibraryApp, AppStatus
from landoc.config import Config
from landoc.discover_ui import DiscoverWindow
from landoc.netinfo import lan_ip
from landoc.panel import StatusPanel
from landoc.roster_ui import RosterWindow

MUTEX_NAME = "Local\\LanDocShare-single-instance"
ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = ctypes.c_void_p
_mutex_handle: int | None = None


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """命名互斥量：已经有一个在跑就返回 False（并保留句柄不让它被回收）。"""
    global _mutex_handle
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True  # 创建失败就别拦着自己
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    if _mutex_handle:  # 换名字（测试用）时别把上一个句柄漏了
        _kernel32.CloseHandle(ctypes.c_void_p(_mutex_handle))
    _mutex_handle = handle
    return True


def make_icon_image(size: int = 64) -> Image.Image:
    """托盘图标：一页纸 + 绿色共享点（自己画，省一个资源文件）。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = size // 10
    draw.rounded_rectangle(
        (margin, margin // 2, size - margin * 2, size - margin),
        radius=size // 10,
        fill=(246, 247, 249, 255),
        outline=(60, 90, 150, 255),
        width=max(2, size // 24),
    )
    line_color = (60, 90, 150, 255)
    for index in range(4):
        y = size // 3 + index * (size // 12)
        draw.line(
            (size // 4, y, size - size // 4 - (index % 2) * size // 8, y),
            fill=line_color,
            width=max(2, size // 32),
        )
    radius = size // 7
    draw.ellipse(
        (size - radius * 2 - margin // 2, size - radius * 2 - margin // 2, size - margin // 2, size - margin // 2),
        fill=(22, 160, 90, 255),
        outline=(255, 255, 255, 255),
        width=max(1, size // 32),
    )
    return image


class TrayApp:
    def __init__(self, cfg: Config, *, show_panel_on_start: bool = True) -> None:
        self.cfg = cfg
        self.icon = None
        self.show_panel_on_start = show_panel_on_start
        self.app = LibraryApp(
            cfg, on_status=self._on_status, on_notify=self._on_notify
        )
        self.panel = StatusPanel(
            cfg,
            self.app,
            lan_ip=lan_ip(),
            on_open_page=self._open_page,
            on_update=self._update_now,
            on_toggle_share=self._toggle_share,
            on_open_roster=self._show_roster,
            on_open_access=self._show_libraries,
            on_install=self._install_update,
        )
        self.roster = RosterWindow(cfg, on_saved=self._roster_saved, host=self.panel)
        self.libraries = DiscoverWindow(cfg, host=self.panel)

    # ---- pystray 回调 ----
    def _on_status(self, status: AppStatus) -> None:
        if self.icon is not None:
            try:
                self.icon.title = f"LanDocShare · {status.summary()}"
                self.icon.update_menu()
            except Exception:  # noqa: BLE001 - 图标还没建好时忽略
                pass

    def _on_notify(self, message: str) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(message, "LanDocShare")
            except Exception:  # noqa: BLE001
                pass

    def _open_page(self) -> None:
        webbrowser.open(self.app.local_url())

    def _show_panel(self) -> None:
        self.panel.show()

    def _show_roster(self) -> None:
        self.roster.show()

    def _show_libraries(self) -> None:
        self.libraries.show()

    def _roster_saved(self) -> None:
        """名单改完：状态窗口的"共享范围"要及时反映人数变化。"""
        self.panel.show()
        if self.icon is not None:
            try:
                self.icon.notify("授权名单已更新，立即生效", "LanDocShare")
            except Exception:  # noqa: BLE001
                pass

    def _update_now(self) -> None:
        self.app.update_now()

    def _rebuild_label(self) -> str:
        """没在转换就「立即重建索引」，正在转换就变成能停下来的「停止建库」。"""
        return "停止建库" if self.app.building else "立即重建索引"

    def _rebuild_or_stop(self) -> None:
        if self.app.building:
            self.app.stop_building()
        else:
            self.app.update_now()

    def _update_label(self) -> str:
        status = self.app.status
        if self.app.update_ready:
            return f"升级到 v{status.update_version}（重启程序）"
        if status.update_phase == "downloading":
            return "正在下载新版本…"
        if status.update_version:
            return f"下载新版本 v{status.update_version}"
        return "检查更新"

    def _menu_update(self) -> None:
        """托盘里的更新入口：检查 → 下载 → 升级，按当前状态自动选一步。"""
        status = self.app.status
        if self.app.update_ready:
            self._install_update()
        elif status.update_version:
            self.app.download_update_async()
        else:
            self.app.check_updates_async(announce=True)

    def _install_update(self) -> None:
        staged = self.app.staged_update
        result = self.app.install_update()
        if result != "ok":
            self._notify(result)
            self.panel.show()
            return
        self._notify(f"正在升级到 v{staged.version}，程序会自动重启…")
        self.panel.close()
        self.roster.close()
        self.libraries.close()
        if self.icon is not None:
            self.icon.stop()
        # 更新器要拿单实例互斥量，这里尽快退出，别和它抢
        threading.Timer(1.5, lambda: os._exit(0)).start()

    def _notify(self, message: str) -> None:
        if self.icon is None:
            return
        try:
            self.icon.notify(message, "LanDocShare")
        except Exception:  # noqa: BLE001 - 通知失败不影响功能
            pass

    def _toggle_share(self) -> None:
        self.app.toggle_share()

    def _edit_library(self) -> None:
        """改库名/文件夹/共享范围：向导在后台线程里开，改完当场生效。"""
        threading.Thread(
            target=self._apply_library_edits, name="landoc-wizard", daemon=True
        ).start()

    def _apply_library_edits(self) -> None:
        """弹向导 → 应用改动 → 落盘 → 立刻开扫。

        单拎出来是为了能不开线程地测：保存后必须真的**马上**开始解析新文件夹，
        而不是等下一次空闲扫描（用户加了目录就想立刻能搜到）。

        向导必须开在状态窗口那条 Tk 线程上（`panel.run_modal`）：自己另起线程
        再 tk.Tk() 会和状态窗口组成两个解释器，保存时进程直接崩在 tcl86t.dll。
        """
        from landoc.wizard import apply_config, run_wizard

        def ask(master):
            return run_wizard(self.cfg.lib_dir, master, save=False, current=self.cfg)

        chosen = self.panel.run_modal(ask)
        if chosen is None:
            return
        apply_config(self.cfg, chosen)
        self.cfg.save()
        # 新加的文件夹不该等"用户离开电脑"：这里请求一次强制扫描，越过空闲门控，
        # 一轮轮跑到没有新增为止（见 LibraryApp.update_now / _drain）。
        self.app.update_now()
        self.panel.show()
        self._notify("建库设置已更新，正在收录新文件夹里的文档")

    def _toggle_autostart(self) -> None:
        autostart.set_enabled(not autostart.is_enabled())
        if self.icon is not None:
            self.icon.update_menu()

    def _open_path(self, path: Path, what: str) -> None:
        """用资源管理器打开一个文件夹；打不开就直说。

        旧版这里叫「权限设置（打开配置文件）」，靠 `os.startfile` 把 json 交给系统
        关联的程序——本机 .json 关联到 Trae CN，而它拿到文件后自己退出，界面上就是
        "点了没反应"。打开文件夹没有这个问题，也顺带把 config.json、logs、md 都摆在
        用户面前（想手改高级参数的人自己就找得到）。
        """
        import os
        import subprocess

        if not path.is_dir():
            self._notify(f"找不到{what}：{path}")
            return
        try:
            os.startfile(str(path))  # noqa: S606 - 用户显式点击
            return
        except OSError:
            pass
        try:
            # 关联被改坏时的退路：explorer 打开目录不依赖任何文件关联
            subprocess.Popen(["explorer", str(path)])  # noqa: S603,S607
        except OSError as exc:
            self._notify(f"打不开{what}：{exc}")

    def _open_lib_dir(self) -> None:
        self._open_path(Path(self.cfg.lib_dir), "库目录")

    def _open_logs(self) -> None:
        logs = Path(self.cfg.lib_dir) / "logs"
        try:
            logs.mkdir(parents=True, exist_ok=True)  # 刚建库还没写过日志时目录可能不在
        except OSError:
            pass
        self._open_path(logs, "日志目录")

    def _quit(self) -> None:
        self.panel.close()
        self.roster.close()
        self.libraries.close()
        self.app.stop()
        if self.icon is not None:
            self.icon.stop()

    def _menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem(lambda item: self.app.status.summary(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("显示状态窗口", self._show_panel, default=True),
            pystray.MenuItem("打开库主页", self._open_page),
            pystray.MenuItem("可访问的资料库…", self._show_libraries),
            pystray.MenuItem(lambda item: self._update_label(), self._menu_update),
            pystray.MenuItem(lambda item: self._rebuild_label(), self._rebuild_or_stop),
            pystray.MenuItem("修改建库设置…", self._edit_library),
            pystray.MenuItem("授权名单…", self._show_roster),
            pystray.MenuItem(
                "暂停共享", self._toggle_share, checked=lambda item: self.app.paused
            ),
            pystray.MenuItem("打开库目录", self._open_lib_dir),
            pystray.MenuItem(
                "开机自启",
                self._toggle_autostart,
                checked=lambda item: autostart.is_enabled(),
            ),
            pystray.MenuItem("查看日志", self._open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._quit),
        )

    def run(self) -> None:
        import pystray

        # pystray 发 NIM_ADD 时把 uID 传成 0（结构体字段叫 uID，它传的是 hID），
        # Windows 就只建一个空槽：托盘占位、没图标、右键也点不出来。
        from landoc import notifyicon

        notifyicon.patch_pystray_uid()
        self.icon = pystray.Icon(
            "landoc",
            make_icon_image(),
            f"LanDocShare · {self.cfg.name}",
            self._menu(),
        )
        self.app.start()
        # 上次升级留下的暂存目录/旧目录：等程序稳定几秒后再删，免得影响启动
        threading.Timer(8.0, _cleanup_after_update).start()
        # 托盘没有主窗口，不主动给个东西的话用户会以为没启动：第一次就弹状态窗口
        if self.show_panel_on_start:
            self.panel.show()
        threading.Timer(1.5, self._announce).start()
        self.icon.run()

    def _announce(self) -> None:
        if self.icon is None:
            return
        try:
            self.icon.notify("已在托盘运行；双击图标打开状态窗口", "LanDocShare")
        except Exception:  # noqa: BLE001 - 气泡被系统关掉也不影响使用
            pass


def _cleanup_after_update() -> None:
    """升级成功后删掉暂存包与旧目录（更新器自己删不掉正在跑的目录）。"""
    try:
        from landoc import updater

        updater.run_pending_cleanup()
    except Exception:  # noqa: BLE001 - 清理失败不影响使用
        pass


def _existing_port(cfg: Config) -> int:
    """另一个实例已经在跑：从它的索引 meta 里读出真实端口。"""
    try:
        from landoc.index import Index

        value = Index(cfg.db_path, md_root=cfg.md_dir).get_meta("port")
        if value:
            return int(value)
    except Exception:  # noqa: BLE001
        pass
    return cfg.port


CLI_COMMANDS = frozenset(
    {"init", "probe", "convert", "serve", "status", "people", "registry", "update"}
)

LAST_LIB_MARKER = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LanDocShare" / "last_lib.txt"
)


def _remember_lib(lib_dir: Path) -> None:
    """记住上次打开的库，双击 exe 时直接回到它（而不是写死某个盘符）。"""
    try:
        LAST_LIB_MARKER.parent.mkdir(parents=True, exist_ok=True)
        LAST_LIB_MARKER.write_text(str(lib_dir), encoding="utf-8")
    except OSError:
        pass


def _default_lib_dir() -> Path:
    try:
        remembered = Path(LAST_LIB_MARKER.read_text(encoding="utf-8").strip())
        if (remembered / "config.json").exists():
            return remembered
    except (OSError, ValueError):
        pass
    # 以前写死 D:/MyLib：没有 D 盘的机器建不了库。现在给"我的文档\LanDocShare"下的建议位置，
    # 用户还能在向导里改。懒导入，免得起 tray 就牵连 wizard 的 tkinter 依赖。
    from landoc.wizard import default_lib_dir

    return default_lib_dir()


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--apply-update" in argv:
        # 新版本 exe 从暂存目录以这个参数启动，由它等旧进程退出、换目录、启动新版
        from landoc.updater import apply_update_main

        return apply_update_main(argv)
    if argv and argv[0] in CLI_COMMANDS:
        # 打包成一个 exe 后，排错时仍能直接跑 init/convert/status
        from landoc import cli

        return cli.main(argv)

    from landoc.wizard import run_wizard

    parser = argparse.ArgumentParser(prog="landoc-tray", description="LanDocShare 托盘程序")
    parser.add_argument("--lib-dir", default=None, help="库目录（默认用上次的库）")
    parser.add_argument("--wizard", action="store_true", help="强制重开建库向导")
    parser.add_argument(
        "--autostart", action="store_true", help="开机自启模式：只驻留托盘，不自动打开页面"
    )
    parser.add_argument("--headless", action="store_true", help="不显示托盘，只起服务与调度（排错用）")
    parser.add_argument(
        "--edit-settings",
        action="store_true",
        help="直接打开「修改建库设置」（等同托盘菜单里那一条，排错/做快捷方式用）",
    )
    args = parser.parse_args(argv)

    lib_dir = Path(args.lib_dir) if args.lib_dir else _default_lib_dir()
    cfg = None
    try:
        cfg = Config.load(lib_dir)
    except (OSError, ValueError):
        cfg = None
    if args.wizard or cfg is None:
        # --wizard 强制重开：有旧配置就预填，省得库主人重填一遍
        cfg = run_wizard(lib_dir, current=cfg)
        if cfg is None:
            print("已取消：没有建库", file=sys.stderr)
            return 1
    _remember_lib(cfg.lib_dir)

    if args.headless:
        app = LibraryApp(cfg)
        app.start()
        print(f"[OK] 无托盘模式：{app.local_url()}  （Ctrl+C 退出）")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\n[停止] 已退出")
        finally:
            app.stop()
        return 0

    if args.edit_settings:
        # 走的就是托盘菜单「修改建库设置」那条路（同一个 _apply_library_edits），
        # 只是从命令行喊起来——排错时不必去点托盘图标。
        if not acquire_single_instance():
            print("已经在运行：请从托盘菜单打开「修改建库设置」", file=sys.stderr)
            return 1
        tray_app = TrayApp(cfg, show_panel_on_start=False)
        tray_app.app.start()
        threading.Thread(
            target=tray_app._apply_library_edits, name="landoc-wizard", daemon=True
        ).start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\n[停止] 已退出")
        finally:
            tray_app.panel.close()
            tray_app.app.stop()
        return 0

    if not acquire_single_instance():
        url = f"http://127.0.0.1:{_existing_port(cfg)}/"
        print(f"已经在运行，打开 {url}")
        webbrowser.open(url)
        return 0

    TrayApp(cfg, show_panel_on_start=not args.autostart).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
