"""状态窗口：行内容与线程只起一次（不真开 Tk，GUI 留给人工验收）。"""

from __future__ import annotations

import threading

from landoc.app import AppStatus
from landoc import __version__
from landoc.config import Config
from landoc.panel import StatusPanel, phase_text, status_rows, version_text


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


def test_status_rows_show_registry_when_configured(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.registry_url = "http://10.91.1.197:8767"
    status = AppStatus(phase="serving", doc_count=13, port=8768, registry="已登记 2026-11-17 20:18:11")
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="10.91.1.197")}
    assert rows["登记中心"] == "已登记 2026-11-17 20:18:11"

    status.registry = ""
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="10.91.1.197")}
    assert rows["登记中心"] == "等待心跳…"


def test_status_rows_hide_registry_when_not_configured(tmp_path):
    cfg = _cfg(tmp_path)
    rows = {row.label for row in status_rows(cfg, AppStatus(), lan_ip="10.91.1.197")}
    assert "登记中心" not in rows


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


# --------------------------------------------------------------------------- #
# 版本行：发现新版本时把"到哪一步了"讲清楚
# --------------------------------------------------------------------------- #
def test_version_row_shows_current_version_only(tmp_path):
    cfg = _cfg(tmp_path)
    status = AppStatus(phase="serving", doc_count=13, port=8768)
    rows = {row.label: row.value for row in status_rows(cfg, status, lan_ip="10.91.1.197")}
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
