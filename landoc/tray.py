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
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.icon = None
        self.app = LibraryApp(
            cfg, on_status=self._on_status, on_notify=self._on_notify
        )

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

    def _update_now(self) -> None:
        self.app.update_now()

    def _toggle_share(self) -> None:
        self.app.toggle_share()

    def _toggle_autostart(self) -> None:
        autostart.set_enabled(not autostart.is_enabled())
        if self.icon is not None:
            self.icon.update_menu()

    def _open_config(self) -> None:
        try:
            import os

            os.startfile(str(self.cfg.config_path))  # noqa: S606 - 用户显式点击
        except Exception:  # noqa: BLE001
            pass

    def _open_logs(self) -> None:
        try:
            import os

            os.startfile(str(Path(self.cfg.lib_dir) / "logs"))  # noqa: S606
        except Exception:  # noqa: BLE001
            pass

    def _quit(self) -> None:
        self.app.stop()
        if self.icon is not None:
            self.icon.stop()

    def _menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem(lambda item: self.app.status.summary(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开库主页", self._open_page, default=True),
            pystray.MenuItem("立即更新", self._update_now),
            pystray.MenuItem(
                "暂停共享", self._toggle_share, checked=lambda item: self.app.paused
            ),
            pystray.MenuItem("权限设置（打开配置文件）", self._open_config),
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

        self.icon = pystray.Icon(
            "landoc",
            make_icon_image(),
            f"LanDocShare · {self.cfg.name}",
            self._menu(),
        )
        self.app.start()
        self.icon.run()


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


CLI_COMMANDS = frozenset({"init", "probe", "convert", "serve", "status"})

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
    return Path("D:/MyLib")


def main(argv: list[str] | None = None) -> int:
    import argparse

    from landoc.wizard import run_wizard

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in CLI_COMMANDS:
        # 打包成一个 exe 后，排错时仍能直接跑 init/convert/status
        from landoc import cli

        return cli.main(argv)

    parser = argparse.ArgumentParser(prog="landoc-tray", description="LanDocShare 托盘程序")
    parser.add_argument("--lib-dir", default=None, help="库目录（默认用上次的库）")
    parser.add_argument("--wizard", action="store_true", help="强制重开建库向导")
    parser.add_argument("--headless", action="store_true", help="不显示托盘，只起服务与调度（排错用）")
    args = parser.parse_args(argv)

    lib_dir = Path(args.lib_dir) if args.lib_dir else _default_lib_dir()
    cfg = None
    if not args.wizard:
        try:
            cfg = Config.load(lib_dir)
        except (OSError, ValueError):
            cfg = None
    if cfg is None:
        cfg = run_wizard(lib_dir)
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

    if not acquire_single_instance():
        url = f"http://127.0.0.1:{_existing_port(cfg)}/"
        print(f"已经在运行，打开 {url}")
        webbrowser.open(url)
        return 0

    TrayApp(cfg).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
