"""运行核心：起服务、只在空闲时跑批次、暂停共享、托盘可观察的状态。"""

from __future__ import annotations

import threading
import time

from landoc import updater
from landoc.app import LibraryApp
from landoc.config import Config
from landoc.pipeline import RunReport


class FakeServer:
    def __init__(self, port: int = 18080) -> None:
        self.server_address = ("0.0.0.0", port)
        self.started = threading.Event()
        self._release = threading.Event()

    def serve_forever(self) -> None:
        self.started.set()
        self._release.wait(5)

    def shutdown(self) -> None:
        self._release.set()

    def server_close(self) -> None:
        self._release.set()


class FakeIndex:
    def __init__(self, total: int = 3) -> None:
        self.total = total
        self.meta: dict[str, str] = {}

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value

    def get_meta(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default)

    def stats(self) -> dict:
        return {"total": self.total}


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_app(tmp_path, **kwargs) -> LibraryApp:
    cfg = Config(name="测试库", lib_dir=tmp_path, folders=[tmp_path])
    kwargs.setdefault("index", FakeIndex())
    kwargs.setdefault("poll_seconds", 0.02)
    return LibraryApp(cfg, **kwargs)


def test_start_binds_port_and_records_it(tmp_path):
    server = FakeServer(port=18080)
    app = make_app(tmp_path, server_factory=lambda cfg: server)
    app.start()
    try:
        assert server.started.wait(2)
        assert app.status.port == 18080
        assert app.index.meta["port"] == "18080"
        assert app.local_url() == "http://127.0.0.1:18080/"
    finally:
        app.stop()


def test_start_reports_existing_docs_before_first_scan(tmp_path):
    """已建好的库启动后状态窗口就该显示真实篇数，别等下一轮扫描（否则像没建库）。"""
    app = make_app(tmp_path, index=FakeIndex(total=13), idle_check=lambda: False)
    app.start()
    try:
        assert app.status.phase == "serving"
        assert app.status.doc_count == 13
    finally:
        app.stop()


def test_batches_run_only_while_idle_and_notify_once(tmp_path):
    calls: list[int] = []
    notes: list[str] = []

    def runner():
        calls.append(1)
        return RunReport(converted=2 if len(calls) == 1 else 0)

    idle = {"value": True}
    app = make_app(
        tmp_path,
        runner=runner,
        idle_check=lambda: idle["value"],
        on_notify=notes.append,
    )
    app.start()
    try:
        assert wait_for(lambda: notes)
        assert notes == ["已收录 2 篇，可被检索"]
        assert len(calls) == 2  # 第二批没东西可转，就停
        assert app.status.doc_count == 3
    finally:
        app.stop()


def test_user_activity_blocks_conversion(tmp_path):
    calls: list[int] = []
    app = make_app(tmp_path, runner=lambda: calls.append(1) or RunReport(), idle_check=lambda: False)
    app.start()
    try:
        time.sleep(0.15)
        assert calls == []
        assert app.status.phase == "serving"
    finally:
        app.stop()


def test_pause_clears_cidrs_and_resume_restores(tmp_path):
    app = make_app(tmp_path, idle_check=lambda: False)
    app.cfg.allow_cidrs = ["10.91.0.0/16", "127.0.0.1"]
    app.pause_share()
    assert app.cfg.allow_cidrs == []
    assert app.paused is True
    assert app.status.phase == "paused"
    app.pause_share()  # 重复暂停不覆盖备份
    app.resume_share()
    assert app.cfg.allow_cidrs == ["10.91.0.0/16", "127.0.0.1"]
    assert app.paused is False


def test_stop_shuts_down_server(tmp_path):
    server = FakeServer()
    app = make_app(tmp_path, server_factory=lambda cfg: server, idle_check=lambda: False)
    app.start()
    app.stop()
    assert app.status.phase == "stopped"


def test_heartbeat_registers_with_registry(tmp_path):
    calls: list[tuple[str, dict]] = []

    def poster(url, payload):
        calls.append((url, dict(payload)))
        return {"ok": True, "server_time": "2026-11-17 20:18:11"}

    app = make_app(
        tmp_path,
        index=FakeIndex(total=13),
        idle_check=lambda: False,
        server_factory=lambda cfg: FakeServer(port=18080),
        poster=poster,
        heartbeat_seconds=0.05,
    )
    app.cfg.registry_url = "http://10.91.1.197:8767/"
    app.start()
    try:
        assert wait_for(lambda: calls)
        url, payload = calls[0]
        assert url == "http://10.91.1.197:8767/register"
        assert payload["name"] == "测试库"
        assert payload["doc_count"] == 13
        assert payload["port"] == 18080
        assert payload["version"]
        assert payload["allow_cidrs"] == ["10.91.0.0/16"]
        assert payload["allow_people"] == {}
        assert app.last_register["ok"] is True
        assert wait_for(lambda: app.status.registry.startswith("已登记"))
        assert "2026-11-17 20:18:11" in app.status.registry
    finally:
        app.stop()


def test_heartbeat_reports_empty_audience_when_paused(tmp_path):
    """暂停共享时 allow_cidrs 为空，中心就该把这个库藏起来。"""
    calls: list[tuple[str, dict]] = []

    def poster(url, payload):
        calls.append((url, dict(payload)))
        return {"ok": True}

    app = make_app(
        tmp_path,
        idle_check=lambda: False,
        server_factory=lambda cfg: FakeServer(port=18081),
        poster=poster,
        heartbeat_seconds=0.05,
    )
    app.cfg.registry_url = "http://10.91.1.197:8767"
    app.pause_share()
    app.start()
    try:
        assert wait_for(lambda: calls)
        assert calls[0][1]["allow_cidrs"] == []
    finally:
        app.stop()


def test_no_heartbeat_without_registry_url(tmp_path):
    calls: list[tuple[str, dict]] = []
    app = make_app(tmp_path, idle_check=lambda: False, poster=lambda url, payload: calls.append((url, payload)) or {})
    app.start()
    try:
        time.sleep(0.1)
        assert calls == []
    finally:
        app.stop()


# --------------------------------------------------------------------------- #
# 自动更新：状态、摘要、安装前置条件
# --------------------------------------------------------------------------- #
def _staged(tmp_path) -> updater.StagedUpdate:
    return updater.StagedUpdate(version="0.3.0", app_dir=tmp_path / "app")


def _update_app(tmp_path, monkeypatch, *, fetch, prepare=None, **kwargs) -> LibraryApp:
    # 别让"上次下载好的包"从本机真实目录里被认领
    monkeypatch.setenv("LANDOC_UPDATE_DIR", str(tmp_path / "updates"))
    app = make_app(
        tmp_path,
        idle_check=lambda: False,
        server_factory=lambda cfg: FakeServer(port=18080),
        update_fetch=fetch,
        update_prepare=prepare or (lambda info, progress=None: _staged(tmp_path)),
        update_initial_delay=0.01,
        update_interval=3600,
        **kwargs,
    )
    app.cfg.registry_url = "http://10.91.1.197:8767"
    return app


def test_update_checker_reaches_ready_and_summary_mentions_it(tmp_path, monkeypatch):
    info = updater.UpdateInfo(version="0.3.0", url="http://x/p.zip", sha256="a" * 64, size=1)
    app = _update_app(tmp_path, monkeypatch, fetch=lambda url, version: info)
    app.start()
    try:
        assert wait_for(lambda: app.update_ready, 5.0)
        assert app.status.update_version == "0.3.0"
        assert app.staged_update is not None
        assert "可升级 v0.3.0" in app.status.summary()
        # 源码运行不该真去替换目录，要明确告诉用户为什么
        assert "源码运行" in app.install_update()
    finally:
        app.stop()


def test_update_checker_silent_when_up_to_date(tmp_path, monkeypatch):
    app = _update_app(tmp_path, monkeypatch, fetch=lambda url, version: None)
    app.start()
    try:
        time.sleep(0.2)
        assert app.status.update_phase == ""
        assert app.status.update_version == ""
        assert app.staged_update is None
    finally:
        app.stop()


def test_check_updates_switch_off(tmp_path, monkeypatch):
    calls: list[int] = []

    def fetch(url, version):
        calls.append(1)
        return None

    app = _update_app(tmp_path, monkeypatch, fetch=fetch)
    app.cfg.check_updates = False
    app.start()
    try:
        time.sleep(0.2)
        assert calls == []
    finally:
        app.stop()


def test_install_without_staged_update_explains(tmp_path, monkeypatch):
    app = _update_app(tmp_path, monkeypatch, fetch=lambda url, version: None)
    app.start()
    try:
        assert app.install_update() == "新版本还没下载好"
    finally:
        app.stop()


def test_manual_check_says_up_to_date_out_loud(tmp_path, monkeypatch):
    """点「检查更新」若一句话都不说，用户会以为菜单坏了。"""
    notes: list[str] = []
    app = _update_app(
        tmp_path, monkeypatch, fetch=lambda url, version: None, on_notify=notes.append
    )
    app.start()
    try:
        app.check_updates_async(announce=True)
        assert wait_for(lambda: any("已是最新版本" in note for note in notes), 5.0)
    finally:
        app.stop()


def test_auto_check_stays_quiet_but_manual_check_does_not(tmp_path, monkeypatch):
    """后台每 6 小时的自动检查不该弹气泡，只有用户主动点才提示。"""
    notes: list[str] = []
    app = _update_app(
        tmp_path, monkeypatch, fetch=lambda url, version: None, on_notify=notes.append
    )
    app.start()
    try:
        time.sleep(0.3)  # 自动检查已经跑过一轮
        assert not [note for note in notes if "已是最新版本" in note]
    finally:
        app.stop()


def test_downloaded_update_announces_ready(tmp_path, monkeypatch):
    info = updater.UpdateInfo(version="0.3.0", url="http://x/p.zip", sha256="a" * 64, size=1)
    notes: list[str] = []
    app = _update_app(
        tmp_path, monkeypatch, fetch=lambda url, version: info, on_notify=notes.append
    )
    app.start()
    try:
        assert wait_for(lambda: app.update_ready, 5.0)
        assert wait_for(lambda: any("已下载好" in note for note in notes), 5.0)
    finally:
        app.stop()
