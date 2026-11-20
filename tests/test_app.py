"""运行核心：起服务、只在空闲时跑批次、暂停共享、托盘可观察的状态。"""

from __future__ import annotations

import json
import threading
import time

from landoc import updater
from landoc.app import AppStatus, LibraryApp, eta_phrase, human_eta
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
    app.cfg.allow_cidrs = ["172.16.0.0/16", "127.0.0.1"]
    app.pause_share()
    assert app.cfg.allow_cidrs == []
    assert app.paused is True
    assert app.status.phase == "paused"
    app.pause_share()  # 重复暂停不覆盖备份
    app.resume_share()
    assert app.cfg.allow_cidrs == ["172.16.0.0/16", "127.0.0.1"]
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
    app.cfg.registry_url = "http://172.16.1.197:8767/"
    app.cfg.allow_cidrs = ["10.91.0.0/16"]  # 显式给：默认值现在是按本机网卡现算的
    app.start()
    try:
        assert wait_for(lambda: calls)
        url, payload = calls[0]
        assert url == "http://172.16.1.197:8767/register"
        assert payload["name"] == "测试库"
        assert payload["doc_count"] == 13
        assert payload["port"] == 18080
        assert payload["version"]
        assert payload["allow_cidrs"] == ["10.91.0.0/16"]
        assert payload["deny_cidrs"] == []
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
    app.cfg.registry_url = "http://172.16.1.197:8767"
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
# 运行日志：状态窗口只说最后一句话，细节要留在 logs/app.log
# --------------------------------------------------------------------------- #
def test_start_writes_app_log(tmp_path):
    from landoc.app import app_log_path

    app = make_app(tmp_path, idle_check=lambda: False, server_factory=lambda cfg: FakeServer(port=18082))
    app.start()
    try:
        log = app_log_path(tmp_path)
        assert log.is_file()
        assert "库服务启动" in log.read_text(encoding="utf-8")
    finally:
        app.stop()


def test_failed_scan_is_logged_not_only_shown_in_status(tmp_path):
    """转换线程里的异常原来只写进状态窗口的 message，重启窗口就查不到了。"""

    def boom():
        raise RuntimeError("COM 挂了")

    app = make_app(
        tmp_path,
        idle_check=lambda: True,
        server_factory=lambda cfg: FakeServer(port=18083),
    )
    app._runner = boom
    app.start()
    try:
        from landoc.app import app_log_path

        log = app_log_path(tmp_path)
        assert wait_for(lambda: log.is_file() and "扫描失败" in log.read_text(encoding="utf-8"))
        assert "COM 挂了" in app.status.message  # 状态窗口那句还在（下一轮会把 phase 改回 serving）
        assert "COM 挂了" in log.read_text(encoding="utf-8")
    finally:
        app.stop()


def test_registry_outage_logged_once_per_transition(tmp_path):
    """中心断线只记一行：断一晚不能被 60 秒一条刷满整份日志。"""

    def poster(url, payload):
        raise OSError("中心不可达")

    app = make_app(
        tmp_path,
        idle_check=lambda: False,
        server_factory=lambda cfg: FakeServer(port=18084),
        poster=poster,
        heartbeat_seconds=0.02,
    )
    app.cfg.registry_url = "http://172.16.1.197:8767"
    app.start()
    try:
        from landoc.app import app_log_path

        log = app_log_path(tmp_path)
        assert wait_for(lambda: log.is_file() and "连不上登记中心" in log.read_text(encoding="utf-8"))
        time.sleep(0.2)  # 放开好几轮心跳，它们都该被合并成同一行
        rows = [line for line in log.read_text(encoding="utf-8").splitlines() if "连不上登记中心" in line]
        assert len(rows) == 1
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
        # 心跳得挡住：否则这些用例会真的往生产登记中心注册一个假库（172.16.1.197:18080）
        poster=lambda url, payload: {"ok": True},
        update_fetch=fetch,
        update_prepare=prepare or (lambda info, progress=None: _staged(tmp_path)),
        update_initial_delay=0.01,
        update_interval=3600,
        **kwargs,
    )
    app.cfg.registry_url = "http://172.16.1.197:8767"
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


# ============ 进度：剩余篇数与 ETA ============


def test_human_eta_reads_like_a_person():
    assert human_eta(0) == "不到 1 分钟"
    assert human_eta(30) == "不到 1 分钟"
    assert human_eta(130) == "2 分钟"
    assert human_eta(3700) == "1 小时 2 分"
    assert human_eta(7200) == "2 小时"
    # 「约 不到 1 分钟」读着别扭，短时间不加"约"
    assert eta_phrase(30) == "不到 1 分钟"
    assert eta_phrase(130) == "约 2 分钟"


def test_summary_adds_remaining_and_eta_while_converting():
    """上千篇时只报"已转换 N 篇"会被当成卡死。"""
    status = AppStatus(phase="converting", converted=300, pending=700, eta_seconds=130)
    assert status.summary() == "建库中：已转换 300 篇 · 还剩 700 篇（约 2 分钟）"
    # 不知道总数时保持老文案（托盘里那一行字不能被撑长）
    assert AppStatus(phase="converting", converted=5).summary() == "建库中：已转换 5 篇"


def test_round_passes_budget_and_stop_hook(tmp_path, monkeypatch):
    """一轮同时受"篇数上限"和"时间预算"约束，用户回来要能停。"""
    from landoc import app as app_module

    seen: dict = {}

    def fake_run(cfg, **kwargs):
        seen.update(kwargs)
        return RunReport(converted=1, pending=9, attempted=1, rate=0.5)

    monkeypatch.setattr(app_module.pipeline, "run", fake_run)
    lib = make_app(tmp_path, idle_check=lambda: True, batch=7, round_seconds=12.5)
    try:
        report = lib._run_round()
    finally:
        lib.stop()

    assert seen["limit"] == 7
    assert seen["budget"] == 12.5
    assert callable(seen["should_stop"]) and callable(seen["on_progress"])
    assert report.pending == 9


def test_progress_callback_updates_status_with_remaining(tmp_path):
    app = make_app(tmp_path, idle_check=lambda: True)
    try:
        app._round_base = 100
        app._on_round_progress(RunReport(converted=5, pending=395, attempted=5, rate=1.0))
        assert app.status.converted == 105  # 本轮之前 100 + 本次 5
        assert app.status.pending == 390  # 395 待转 - 5 已转
        assert app.status.eta_seconds == 390
    finally:
        app.stop()


# ============ 「立即重建索引」= 真的立即 ============


def test_stop_hook_ignores_idle_only_during_manual_build(tmp_path):
    """手动点的那次不受空闲门控：否则正在用电脑时点菜单一篇都转不了。"""
    app = make_app(tmp_path, idle_check=lambda: False)
    try:
        assert app._should_stop_round() is True  # 平时：用户还在用电脑
        app.update_now()
        assert app._should_stop_round() is False  # 手动：照转
        app.stop_building()
        assert app._should_stop_round() is True  # 停止建库：马上收工
        app._force.set()
        app.pause_share()
        assert app._should_stop_round() is True  # 暂停共享优先
    finally:
        app.stop()


def test_update_now_converts_while_user_is_active(tmp_path):
    """旧版这里只是叫醒 worker，仍要过空闲门控 —— 菜单名和实际行为是两回事。"""
    calls: list[int] = []
    notes: list[str] = []

    def runner():
        calls.append(1)
        if len(calls) == 1:
            return RunReport(converted=3, pending=3, attempted=3)
        return RunReport()

    app = make_app(
        tmp_path, runner=runner, idle_check=lambda: False, on_notify=notes.append
    )
    app.start()
    try:
        time.sleep(0.15)
        assert calls == []  # 自动批量在用户活动时不动
        app.update_now()
        assert wait_for(lambda: notes)
        assert notes == ["已收录 3 篇，可被检索"]
        assert len(calls) == 2  # 转完再确认一次没有新增，然后收工
        assert wait_for(lambda: app.status.phase == "serving")
    finally:
        app.stop()


def test_manual_build_keeps_going_until_backlog_done(tmp_path):
    """手动触发不是"跑一轮就收工"：新增几百篇时要一路转完，不能留下一半让用户再点。"""
    backlog = {"left": 250}
    calls: list[int] = []

    def runner():
        calls.append(1)
        if backlog["left"] <= 0:
            return RunReport()
        done = min(100, backlog["left"])
        backlog["left"] -= done
        return RunReport(converted=done, pending=done + backlog["left"], attempted=done)

    app = make_app(tmp_path, runner=runner, idle_check=lambda: False)
    app.start()
    try:
        app.update_now()
        assert wait_for(lambda: backlog["left"] == 0 and len(calls) == 4)
        # 跑完就把强制标记撤掉：用户没再点，不能一直占着 CPU
        assert wait_for(lambda: not app._force.is_set())
        time.sleep(0.1)
        assert len(calls) == 4
    finally:
        app.stop()


def test_manual_click_with_nothing_new_still_answers(tmp_path):
    """点了菜单却一点回音都没有，用户会以为按钮坏了。"""
    notes: list[str] = []
    app = make_app(
        tmp_path, runner=lambda: RunReport(), idle_check=lambda: False, on_notify=notes.append
    )
    app.start()
    try:
        app.update_now()
        assert wait_for(lambda: notes)
        assert notes == ["已是最新：没有需要收录的新文档"]
    finally:
        app.stop()


def test_manual_click_with_only_failures_reports_them(tmp_path):
    notes: list[str] = []
    app = make_app(
        tmp_path,
        runner=lambda: RunReport(failed=[("a.docx", "no_office")]),
        idle_check=lambda: False,
        on_notify=notes.append,
    )
    app.start()
    try:
        app.update_now()
        assert wait_for(lambda: notes)
        assert notes == ["没有新增；1 篇转换失败，详见 failures.json"]
    finally:
        app.stop()


def test_manual_click_reports_source_missing_docs(tmp_path):
    """库只增不减之后，"没新增"的回音不能盖住"有 N 篇源文件已不在"这件事。"""
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "files": {
                    "测试目录/搬走了.docx": {
                        "source_missing": True,
                        "missing_since": "2026-11-18 23:00:00",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    notes: list[str] = []
    app = make_app(
        tmp_path, runner=lambda: RunReport(), idle_check=lambda: False, on_notify=notes.append
    )
    app.start()
    try:
        app.update_now()
        assert wait_for(lambda: notes)
        assert notes == ["已是最新；1 篇源文件已不在，内容仍在库中"]
    finally:
        app.stop()


def test_pause_drops_a_pending_manual_request(tmp_path):
    """暂停期间点「立即重建索引」不该在恢复共享后突然开跑。"""
    app = make_app(tmp_path, idle_check=lambda: False)
    app.update_now()
    app.pause_share()
    assert app._force.is_set() is False
    app.resume_share()
    assert app._force.is_set() is False


def test_building_flag_follows_phase(tmp_path):
    app = make_app(tmp_path, idle_check=lambda: False)
    app._update(phase="converting")
    assert app.building is True
    app._update(phase="serving")
    assert app.building is False

def test_round_is_logged_to_app_log(tmp_path):
    """4.6: 状态窗口只留"最后一句话"，每轮摘要要落到 logs/app.log（重启后还能查）。"""
    from landoc.app import app_log_path

    rounds = [RunReport(converted=3, attempted=4, pending=7, elapsed=12.0, stop_reason="limit")]

    def runner():
        return rounds.pop(0) if rounds else RunReport()

    app = make_app(tmp_path, runner=runner, idle_check=lambda: True)
    assert app._drain() == (3, 0)
    text = app_log_path(tmp_path).read_text(encoding="utf-8")
    assert "一轮结束：收录 3 篇、失败 0 篇、尝试 4 篇、剩余 3 篇" in text
    assert "用时 12 秒（limit）" in text


def test_stopping_a_manual_build_is_reported(tmp_path):
    """途中点了「停止建库」：要明说停下了，不能报成「已是最新」。"""
    notes: list[str] = []
    state = {}

    def runner():
        state["app"].stop_building()  # 用户在第一轮建库途中点了「停止建库」
        return RunReport()

    app = make_app(tmp_path, runner=runner, idle_check=lambda: False, on_notify=notes.append)
    state["app"] = app
    app.start()
    try:
        app.update_now()
        assert wait_for(lambda: notes)
        assert notes == ["已停止建库，已收录的文档照常可检索"]
    finally:
        app.stop()


def test_abort_before_a_round_converts_nothing(tmp_path):
    calls: list[int] = []

    def runner():
        calls.append(1)
        return RunReport(converted=1, attempted=1)

    app = make_app(tmp_path, runner=runner, idle_check=lambda: False)
    app.stop_building()
    assert app._drain() == (0, 0)  # 已经喊停，一篇都不该再转
    assert calls == []
    assert app._abort.is_set() is False  # 收工要把标记清干净，别影响下一轮
