"""状态窗口：行内容与线程只起一次（不真开 Tk，GUI 留给人工验收）。"""

from __future__ import annotations

import threading

from landoc.app import AppStatus
from landoc.config import Config
from landoc.panel import StatusPanel, phase_text, status_rows


def _cfg(tmp_path, **overrides):
    data = dict(name="陈鋆的资料库", lib_dir=str(tmp_path), folders=[])
    data.update(overrides)
    return Config(**data)


def test_status_rows_show_scope_and_address(tmp_path):
    cfg = _cfg(tmp_path, allow_cidrs=["10.91.0.0/16", "127.0.0.1"])
    status = AppStatus(phase="serving", doc_count=13, last_scan="2026-11-17 12:40", port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="10.91.1.197")}
    assert rows["文档"] == "13 篇"
    assert rows["共享范围"] == "10.91.0.0/16、127.0.0.1"
    assert rows["服务地址"] == "http://10.91.1.197:8768/"
    assert rows["上次扫描"] == "2026-11-17 12:40"


def test_status_rows_mark_paused_and_empty_scan(tmp_path):
    cfg = _cfg(tmp_path, allow_cidrs=["10.91.0.0/16"])
    status = AppStatus(phase="paused", paused=True, doc_count=0, port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="10.91.1.197")}
    assert "暂停" in rows["共享范围"]
    assert rows["上次扫描"] == "还没扫过"
    assert rows["文档"] == "0 篇"


def test_status_rows_show_roster_instead_of_cidrs(tmp_path):
    cfg = _cfg(tmp_path, allow_cidrs=["10.91.0.0/16"])
    cfg.allow_people = {"陈鋆": "10.91.1.197", "张三": ""}
    status = AppStatus(phase="serving", doc_count=13, port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="10.91.1.197")}
    assert rows["共享范围"] == "仅指定名单（2 人）"


def test_phase_text_does_not_repeat_counts(tmp_path):
    cfg = _cfg(tmp_path)
    status = AppStatus(phase="serving", doc_count=13, last_scan="2026-11-17 17:39", port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="10.91.1.197")}
    assert rows["状态"] == "正在共享"
    assert "13 篇" not in rows["状态"]
    assert phase_text(AppStatus(phase="converting", converted=5)) == "建库中：已转换 5 篇"
    assert phase_text(AppStatus(phase="error", message="端口被占用")) == "出错了 · 端口被占用"


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
