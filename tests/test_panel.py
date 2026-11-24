"""状态窗口：行内容与线程只起一次（不真开 Tk，GUI 留给人工验收）。"""

from __future__ import annotations

import threading

import pytest

from landoc.app import AppStatus
from landoc import __version__
from landoc import panel as panel_module
from landoc.config import Config
from landoc.discover_ui import DiscoverWindow
from landoc.panel import (
    HostedWindow,
    StatusPanel,
    center_position,
    center_window,
    phase_text,
    status_rows,
    update_button_text,
    version_text,
)
from landoc.roster_ui import RosterWindow


def _cfg(tmp_path, **overrides):
    data = dict(name="赵六的资料库", lib_dir=str(tmp_path), folders=[])
    data.update(overrides)
    return Config(**data)


def test_status_rows_show_scope_and_address(tmp_path):
    cfg = _cfg(tmp_path, allow_cidrs=["172.16.0.0/16", "127.0.0.1"])
    status = AppStatus(phase="serving", doc_count=13, last_scan="2026-11-17 12:40", port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["文档"] == "13 篇"
    assert rows["共享范围"] == "172.16.0.0/16、127.0.0.1"
    assert rows["服务地址"] == "http://172.16.1.197:8768/"
    assert rows["上次扫描"] == "2026-11-17 12:40"


def test_status_rows_show_deny_count_in_scope(tmp_path):
    """拒绝网段优先于允许网段：只显示允许范围会让人以为这些地址也能查。"""
    cfg = _cfg(tmp_path, allow_cidrs=["172.16.0.0/16"], deny_cidrs=["172.16.1.50/32", "172.16.9.0/24"])
    status = AppStatus(phase="serving", doc_count=13, port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["共享范围"] == "172.16.0.0/16 · 拒绝 2 段"


def test_status_rows_show_denied_visits_in_scope(tmp_path):
    """有人在门外试：状态窗口要看得到，不能只在日志里事后发现。"""
    cfg = _cfg(tmp_path, allow_cidrs=["172.16.0.0/16"], deny_cidrs=["172.16.1.50/32"])
    status = AppStatus(phase="serving", doc_count=13, denied=3, denied_note="10.91.1.178 09:07")
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["共享范围"] == (
        "172.16.0.0/16 · 拒绝 1 段 · 最近有 3 次访问被拒（10.91.1.178 09:07）"
    )


def test_status_rows_surface_failed_conversions(tmp_path):
    """失败篇数要常驻可见，不然只有翻日志才知道有些文件一直没收进来。"""
    cfg = _cfg(tmp_path, allow_cidrs=["172.16.0.0/16"])
    status = AppStatus(phase="serving", doc_count=13, failed=3, port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["文档"] == "13 篇 · 3 篇转换失败"


def test_status_rows_mark_paused_and_empty_scan(tmp_path):
    cfg = _cfg(tmp_path, allow_cidrs=["172.16.0.0/16"])
    status = AppStatus(phase="paused", paused=True, doc_count=0, port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert "暂停" in rows["共享范围"]
    assert rows["上次扫描"] == "还没扫过"
    assert rows["文档"] == "0 篇"


def test_status_rows_show_roster_instead_of_cidrs(tmp_path):
    cfg = _cfg(tmp_path, allow_cidrs=["172.16.0.0/16"])
    cfg.allow_people = {"赵六": "172.16.1.197", "张三": ""}
    status = AppStatus(phase="serving", doc_count=13, port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["共享范围"] == "仅指定名单（2 人）"


def test_status_rows_show_registry_when_configured(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.registry_url = "http://172.16.1.197:8767"
    status = AppStatus(phase="serving", doc_count=13, port=8768, registry="已登记 2026-11-17 20:18:11")
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["登记中心"] == "已登记 2026-11-17 20:18:11"

    status.registry = ""
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["登记中心"] == "等待心跳…"


def test_status_rows_hide_registry_when_not_configured(tmp_path):
    cfg = _cfg(tmp_path)
    rows = {row.label for row in status_rows(cfg, AppStatus(), lan_ip="172.16.1.197")}
    assert "登记中心" not in rows


def test_phase_text_does_not_repeat_counts(tmp_path):
    cfg = _cfg(tmp_path)
    status = AppStatus(phase="serving", doc_count=13, last_scan="2026-11-17 17:39", port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["状态"] == "正在共享"
    assert "13 篇" not in rows["状态"]
    assert phase_text(AppStatus(phase="converting", converted=5)) == "建库中：已转换 5 篇"
    assert phase_text(AppStatus(phase="error", message="端口被占用")) == "出错了 · 端口被占用"


def test_phase_text_and_rows_show_backlog(tmp_path):
    """上千篇时窗口要能看到"还剩多少、还要多久"，否则跟死机没区别。"""
    cfg = _cfg(tmp_path)
    status = AppStatus(phase="converting", converted=300, pending=700, eta_seconds=3720)

    assert phase_text(status) == "建库中：已转换 300 篇 · 还剩 700 篇（约 1 小时 2 分）"

    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["待收录"] == "700 篇（约 1 小时 2 分）"
    # 没积压时不显示这一行（否则空闲的库窗口里永远挂着一行 0 篇）
    rows = {
        row.label: row.value
        for row in status_rows(cfg, AppStatus(phase="serving"), lan_ip="172.16.1.197")
    }
    assert "待收录" not in rows


def test_show_starts_one_panel_thread(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    panel = StatusPanel(cfg, app=None)
    started = []

    def fake_run():
        started.append(threading.current_thread().name)
        panel._queue.get(timeout=5)  # 等 show 命令，模拟窗口线程活着

    monkeypatch.setattr(panel, "_run", fake_run)
    panel.show()
    panel.show()
    for _ in range(50):
        if started:
            break
        threading.Event().wait(0.02)
    assert started == ["landoc-panel"]
    assert panel._thread is not None and panel._thread.daemon


def test_close_waits_for_window_thread(tmp_path, monkeypatch):
    """close() 要等窗口线程退出：Tk 解释器必须在那条线程里销毁。

    不等的话退出时主线程回收 Tk 对象，Tcl 会报
    "Tcl_AsyncDelete: async handler deleted by the wrong thread"。
    """
    cfg = _cfg(tmp_path)
    panel = StatusPanel(cfg, app=None)
    done = threading.Event()

    def fake_run():
        while True:
            command, _payload = panel._queue.get(timeout=5)
            if command == "close":
                done.set()
                return

    monkeypatch.setattr(panel, "_run", fake_run)
    panel.show()
    panel.close()
    assert done.is_set()
    assert panel._thread is not None and not panel._thread.is_alive()


# --------------------------------------------------------------------------- #
# 版本行：发现新版本时把"到哪一步了"讲清楚
# --------------------------------------------------------------------------- #
def test_version_row_shows_current_version_only(tmp_path):
    cfg = _cfg(tmp_path)
    status = AppStatus(phase="serving", doc_count=13, port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="172.16.1.197")}
    assert rows["版本"] == f"v{__version__}"
    assert status_rows(cfg, status, lan_ip="x")[-1].label == "版本"  # 放最后一行


def test_version_text_describes_update_progress():
    assert version_text(AppStatus(version="0.2.0", update_version="0.3.0", update_phase="ready")) == (
        "v0.2.0 → v0.3.0（已下载，可升级）"
    )
    assert version_text(
        AppStatus(version="0.2.0", update_version="0.3.0", update_phase="downloading", update_message="正在下载新版本 40%")
    ) == "v0.2.0 → v0.3.0（正在下载新版本 40%）"
    assert version_text(
        AppStatus(version="0.2.0", update_version="0.9.0", update_phase="manual")
    ) == "v0.2.0 → v0.9.0（版本跨度大，需手动重装）"
    assert version_text(
        AppStatus(version="0.2.0", update_version="0.3.0", update_message="检查更新失败：连不上中心")
    ) == "v0.2.0 → v0.3.0（检查更新失败：连不上中心）"


def test_update_button_turns_into_stop_while_building():
    """「立即更新」在建库时变「停止建库」：手动触发的建库可能跑很久，得能停。"""
    assert update_button_text(AppStatus(phase="serving")) == "立即更新"
    assert update_button_text(AppStatus(phase="converting", converted=3)) == "停止建库"
    assert update_button_text(AppStatus(phase="paused")) == "立即更新"


def test_source_missing_docs_are_reported_but_not_hidden():
    """库只增不减：源文档没了，内容还在库里能搜 —— 状态行得说清楚，别让人以为一切照旧。"""
    assert phase_text(AppStatus(phase="serving", gone=3)) == (
        "正在共享 · 3 篇源文件已不在（内容仍在库中）"
    )
    assert phase_text(AppStatus(phase="serving", gone=0)) == "正在共享"
    assert "3 篇源已不在" in AppStatus(phase="serving", doc_count=12, gone=3).summary()
    assert "源已不在" not in AppStatus(phase="serving", doc_count=12).summary()


# --------------------------------------------------------------------------- #
# 弹窗位置：托盘「显示状态窗口」每次出现在屏幕正中
# --------------------------------------------------------------------------- #
class _FakeRoot:
    """够 center_window 用的假窗口（不启 Tk，GUI 留给人工验收）。"""

    def __init__(self, width=1, height=1, geometry="", screen=(1920, 1080)):
        self._width = width
        self._height = height
        self._geometry = geometry
        self._screen = screen
        self.moved: list[str] = []

    def update_idletasks(self):
        pass

    def winfo_width(self):
        return self._width

    def winfo_height(self):
        return self._height

    def winfo_reqwidth(self):
        return 580

    def winfo_reqheight(self):
        return 220

    def winfo_screenwidth(self):
        return self._screen[0]

    def winfo_screenheight(self):
        return self._screen[1]

    def geometry(self, value=None):
        if value is None:
            return self._geometry
        self.moved.append(value)


def _fixed_area(area):
    return lambda root: area


def test_center_position_puts_the_window_in_the_middle():
    # 1920×1040 的工作区（1080 减去任务栏 40），580×220 的窗口
    assert center_position(580, 220, (0, 0, 1920, 1040)) == (670, 410)


def test_center_position_follows_the_monitor_under_the_cursor():
    """第二块屏从 x=1920 起：居中要加这块屏的原点，否则窗口跑到左屏上去了。"""
    assert center_position(580, 220, (1920, 0, 3840, 1040)) == (2590, 410)


def test_center_position_never_leaves_a_negative_offset():
    """窗口比屏幕还大时贴左上角就行：负坐标会把标题栏顶出屏幕，窗口再也拖不回来。"""
    assert center_position(4000, 3000, (0, 0, 1920, 1040)) == (0, 0)


def test_center_window_centers_even_before_the_window_is_mapped(monkeypatch):
    root = _FakeRoot(geometry="580x220")  # 还没显示：winfo_width() 只有 1
    monkeypatch.setattr(panel_module, "work_area", _fixed_area((0, 0, 1920, 1040)))
    center_window(root)
    assert root.moved == ["+670+410"]


def test_center_window_uses_the_real_size_once_shown(monkeypatch):
    root = _FakeRoot(width=900, height=600, geometry="580x220")
    monkeypatch.setattr(panel_module, "work_area", _fixed_area((0, 0, 1920, 1040)))
    center_window(root)
    assert root.moved == ["+510+220"]


def test_center_window_accepts_an_explicit_size_for_unmapped_windows(monkeypatch):
    """建库向导还没 mainloop：Tk 报 1x1，必须用调用方算好的尺寸居中并定大小。"""
    root = _FakeRoot(geometry="1x1")
    monkeypatch.setattr(panel_module, "work_area", _fixed_area((0, 0, 1920, 1040)))
    center_window(root, (800, 600))
    assert root.moved == ["800x600+560+220"]


def test_center_window_falls_back_to_the_layout_size_when_geometry_is_1x1(monkeypatch):
    """从没摆过的 Toplevel（授权名单、可访问的资料库）报的是 "1x1+0+0"：
    照它居中会把窗口顶到左上角，得退回按布局算的尺寸。"""
    root = _FakeRoot(geometry="1x1")
    monkeypatch.setattr(panel_module, "work_area", _fixed_area((0, 0, 1920, 1040)))
    center_window(root)
    # 580×220（_FakeRoot 的 req 尺寸）居中
    assert root.moved == ["+670+410"]


def test_center_window_falls_back_to_the_tk_screen(monkeypatch):
    """拿不到显示器工作区（非 Windows / 被策略挡）也要能算出位置，不能把窗口开不出来。"""
    root = _FakeRoot(geometry="580x220", screen=(1920, 1080))
    monkeypatch.setattr(panel_module, "_monitor_work_area", lambda: None)
    assert panel_module.work_area(root) == (0, 0, 1920, 1080)
    center_window(root)
    assert root.moved == ["+670+430"]


# ---------------------------------------------------------------- 挂载到面板线程
class _FakeHost:
    """冒充 StatusPanel：只记下"排队进来的活"，不跑（跑起来就要真 Tk 了）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def call(self, fn) -> None:
        self.calls.append(fn)


class _FakeWindow:
    def __init__(self) -> None:
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


def test_list_windows_run_on_the_panel_thread(tmp_path):
    """授权名单、可访问的库都排进面板线程，自己不再起线程开第二个 Tk 解释器。"""
    cfg = _cfg(tmp_path)
    host = _FakeHost()
    roster_window = RosterWindow(cfg, host=host)
    discover_window = DiscoverWindow(cfg, host=host)

    roster_window.show()
    discover_window.show()
    roster_window.close()

    assert len(host.calls) == 3
    assert roster_window.hosted and discover_window.hosted
    assert roster_window._thread is None
    assert discover_window._thread is None


def test_hosted_close_destroys_only_its_own_window(tmp_path):
    """挂载时关窗只能销毁自己的 Toplevel——销毁面板根窗口等于把程序关了。"""
    window_helper = HostedWindow()
    window_helper._bind_host(_FakeHost())
    window = _FakeWindow()
    window_helper._window = window

    window_helper._shutdown_window(window)

    assert window.destroyed
    assert window_helper._window is None


def test_bind_host_requires_a_thread_to_queue_on(tmp_path):
    with pytest.raises(TypeError):
        RosterWindow(_cfg(tmp_path), host=object())


# ------------------------------------------------- 对话框不把状态窗口带出来
class _FakeMaster:
    """冒充面板根窗口：记下有没有被挂 transient。"""

    def __init__(self, viewable: bool) -> None:
        self.viewable = viewable
        self.transients: list = []

    def winfo_viewable(self) -> bool:
        return self.viewable

    def winfo_toplevel(self):
        return self

    def transient(self, other) -> None:
        self.transients.append(other)


class _FakeDialog:
    def __init__(self) -> None:
        self.transients: list = []

    def transient(self, master) -> None:
        self.transients.append(master)


def test_transient_is_attached_only_when_the_panel_is_open():
    """面板开着时对话框挂在它上面（关面板一起关）；面板收着就别挂，
    否则 Windows 上子窗口跟着不显示，用户看到的是"点了没反应"。"""
    dialog = _FakeDialog()
    panel_module.attach_transient(dialog, _FakeMaster(viewable=False))
    assert dialog.transients == []

    master = _FakeMaster(viewable=True)
    panel_module.attach_transient(dialog, master)
    assert dialog.transients == [master]


def test_transient_skips_a_master_that_cannot_answer():
    class _Broken:
        def winfo_viewable(self):
            raise RuntimeError("解释器没了")

    dialog = _FakeDialog()
    panel_module.attach_transient(dialog, _Broken())  # 不该抛
    assert dialog.transients == []


def test_prune_button_is_disabled_while_building():
    """建库那一轮把 state/索引攥在手里：清理按钮必须置灰，点了也只会被覆盖。"""
    idle = AppStatus(phase="serving", doc_count=13, gone=2)
    assert panel_module.prune_button_state(idle) == ("清理 2 篇", True)

    building = AppStatus(phase="converting", doc_count=13, gone=2)
    assert panel_module.prune_button_state(building) == ("清理 2 篇", False)

    nothing_gone = AppStatus(phase="serving", doc_count=13, gone=0)
    assert panel_module.prune_button_state(nothing_gone) == ("清理…", False)
