"""运行核心：起服务、只在空闲时跑批次、暂停共享、托盘可观察的状态。"""

from __future__ import annotations

import threading
import time

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
