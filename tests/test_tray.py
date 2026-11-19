"""托盘程序的纯逻辑部分（不弹图标、不碰真注册表）。"""

from __future__ import annotations

import types
import uuid

from landoc import tray
from landoc.config import Config


def test_remember_and_reuse_last_library(tmp_path, monkeypatch):
    marker = tmp_path / "last_lib.txt"
    monkeypatch.setattr(tray, "LAST_LIB_MARKER", marker)
    lib_dir = tmp_path / "MyLib"
    lib_dir.mkdir()
    (lib_dir / "config.json").write_text("{}", encoding="utf-8")

    tray._remember_lib(lib_dir)

    assert marker.read_text(encoding="utf-8") == str(lib_dir)
    assert tray._default_lib_dir() == lib_dir


def test_default_library_falls_back_when_marker_points_nowhere(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "LAST_LIB_MARKER", tmp_path / "missing.txt")
    fallback = tmp_path / "Documents" / "LanDocShare" / "我的资料库"
    monkeypatch.setattr("landoc.wizard.default_lib_dir", lambda home=None: fallback)

    assert tray._default_lib_dir() == fallback


def test_second_instance_loses_the_mutex():
    name = f"LanDocShare-test-{uuid.uuid4()}"
    assert tray.acquire_single_instance(name) is True
    # 同一个进程再申请一次也要拒绝，否则"双击第二次"会起第二个服务
    assert tray.acquire_single_instance(name) is False


def _isolate_main(monkeypatch, tmp_path):
    """让 main() 走"没有可用配置"的分支，但不真起服务/托盘。"""
    started = {}
    monkeypatch.setattr(tray, "_remember_lib", lambda lib_dir: None)
    monkeypatch.setattr(tray, "acquire_single_instance", lambda *a, **k: True)
    monkeypatch.setattr(
        tray,
        "TrayApp",
        lambda cfg, **kw: types.SimpleNamespace(run=lambda: started.setdefault("cfg", cfg)),
    )
    return started


def test_first_run_opens_wizard_and_starts_with_its_config(tmp_path, monkeypatch):
    from landoc import wizard

    lib_dir = tmp_path / "lib"
    made = Config(name="新库", lib_dir=lib_dir, folders=[tmp_path])
    calls = []

    def fake_wizard(path, parent=None, **kwargs):
        calls.append((path, kwargs))
        return made

    monkeypatch.setattr(wizard, "run_wizard", fake_wizard)
    started = _isolate_main(monkeypatch, tmp_path)

    assert tray.main(["--lib-dir", str(lib_dir)]) == 0
    assert calls, "没有配置时应该弹向导"
    assert started["cfg"] is made


def test_wizard_cancel_exits_nonzero(tmp_path, monkeypatch):
    from landoc import wizard

    monkeypatch.setattr(wizard, "run_wizard", lambda *a, **k: None)
    _isolate_main(monkeypatch, tmp_path)

    assert tray.main(["--lib-dir", str(tmp_path / "lib")]) == 1


def test_wizard_flag_prefills_existing_config(tmp_path, monkeypatch):
    from landoc import wizard

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    Config(name="老库", lib_dir=lib_dir, folders=[tmp_path]).save()
    seen = {}

    def fake_wizard(path, parent=None, **kwargs):
        seen.update(kwargs)
        return Config(name="改过", lib_dir=lib_dir, allow_cidrs=["127.0.0.1"])

    monkeypatch.setattr(wizard, "run_wizard", fake_wizard)
    started = _isolate_main(monkeypatch, tmp_path)

    assert tray.main(["--lib-dir", str(lib_dir), "--wizard"]) == 0
    assert seen["current"] is not None and seen["current"].name == "老库"
    assert started["cfg"].name == "改过"


def test_existing_config_skips_wizard(tmp_path, monkeypatch):
    from landoc import wizard

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    cfg = Config(name="老库", lib_dir=lib_dir, folders=[tmp_path])
    cfg.save()
    monkeypatch.setattr(
        wizard, "run_wizard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该弹向导"))
    )
    started = _isolate_main(monkeypatch, tmp_path)

    assert tray.main(["--lib-dir", str(lib_dir)]) == 0
    assert started["cfg"].name == "老库"


class _FakeApp:
    building = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def update_now(self) -> None:
        self.calls.append("update")

    def stop_building(self) -> None:
        self.calls.append("stop")


def test_rebuild_menu_item_switches_between_rebuild_and_stop():
    """「立即重建索引」点下去会真的跑起来；正在跑时同一项变成「停止建库」。"""
    owner = types.SimpleNamespace(app=_FakeApp())
    assert tray.TrayApp._rebuild_label(owner) == "立即重建索引"
    tray.TrayApp._rebuild_or_stop(owner)
    assert owner.app.calls == ["update"]

    owner.app.building = True
    assert tray.TrayApp._rebuild_label(owner) == "停止建库"
    tray.TrayApp._rebuild_or_stop(owner)
    assert owner.app.calls == ["update", "stop"]


# --------------------------------------------------------------------------- #
# 「打开库目录」：资源管理器打开库目录，不依赖 .json 的文件关联
# --------------------------------------------------------------------------- #
class _Opened(types.SimpleNamespace):
    """够 _open_path 用的假托盘：只记通知，不建图标。"""

    def __init__(self) -> None:
        super().__init__()
        self.notes: list[str] = []

    def _notify(self, message: str) -> None:
        self.notes.append(message)


def _owner(cfg):
    """带上真的 _open_path 绑定，这样 _open_lib_dir / _open_logs 都能直接调。"""
    owner = _Opened()
    owner.cfg = cfg
    owner._open_path = lambda path, what: tray.TrayApp._open_path(owner, path, what)
    return owner


def test_open_lib_dir_starts_explorer_on_the_library(tmp_path, monkeypatch):
    """库目录存在 → 交给系统打开；这条路径必须"点了有反应"。"""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda path, *a: opened.append(path), raising=False)

    tray.TrayApp._open_lib_dir(_owner(Config(name="库", lib_dir=lib_dir)))

    assert opened == [str(lib_dir)]


def test_open_path_falls_back_to_explorer_when_the_shell_refuses(tmp_path, monkeypatch):
    """关联坏掉时 os.startfile 会抛 OSError —— 退回 explorer，别静默失败。"""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    spawned: list[list[str]] = []

    def boom(path, *a):
        raise OSError("没有应用程序与此操作的指定文件有关联")

    monkeypatch.setattr("os.startfile", boom, raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda cmd, *a, **k: spawned.append(cmd), raising=False)

    owner = _owner(Config(name="库", lib_dir=lib_dir))
    tray.TrayApp._open_path(owner, lib_dir, "库目录")

    assert spawned == [["explorer", str(lib_dir)]]
    assert owner.notes == []


def test_open_path_says_so_when_the_folder_is_gone(tmp_path):
    """目录不在了就说"找不到"，不能像旧版那样啥也不发生。"""
    owner = _owner(Config(name="库", lib_dir=tmp_path))
    tray.TrayApp._open_path(owner, tmp_path / "没有这个目录", "库目录")

    assert owner.notes and "找不到库目录" in owner.notes[0]
    assert str(tmp_path / "没有这个目录") in owner.notes[0]


def test_open_logs_creates_the_folder_first(tmp_path, monkeypatch):
    """刚建库还没写过日志时 logs 目录不存在：先建出来再打开。"""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda path, *a: opened.append(path), raising=False)

    tray.TrayApp._open_logs(_owner(Config(name="库", lib_dir=lib_dir)))

    assert (lib_dir / "logs").is_dir()
    assert opened == [str(lib_dir / "logs")]
