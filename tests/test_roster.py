"""授权名单：本地增删排序、与登记中心之间的发布/拉取。"""

from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import pytest

from landoc import roster
from landoc.registry import RegistryStore, make_registry_server


@pytest.fixture()
def center(tmp_path):
    roster_path = tmp_path / "roster.csv"
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(
        store,
        port=0,
        allow_cidrs=["127.0.0.1"],
        roster_path=roster_path,
        roster_admin_ips={"127.0.0.1"},
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, roster_path, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_roster_url_requires_registry():
    with pytest.raises(roster.RosterError) as info:
        roster.roster_url("")
    assert "登记中心" in str(info.value)


def test_validate_entry_normalises_and_rejects():
    assert roster.validate_entry("  陈鋆 ", "") == ("陈鋆", "")
    assert roster.validate_entry("郑泽锋", " 10.91.1.206 ") == ("郑泽锋", "10.91.1.206")

    with pytest.raises(ValueError):
        roster.validate_entry("", "10.91.1.1")
    with pytest.raises(ValueError):
        roster.validate_entry("a=b", "10.91.1.1")
    with pytest.raises(ValueError):
        roster.validate_entry("张三", "10.91.1.999")


def test_sorted_entries_puts_registered_ips_first():
    people = {"老王": "", "郑泽锋": "10.91.1.206", "杨欢": "10.91.5.238", "小李": ""}
    order = [name for name, _ip in roster.sorted_entries(people)]
    assert order[:2] == ["郑泽锋", "杨欢"]  # 有 IP 的在前，按 IP 排
    assert set(order[2:]) == {"老王", "小李"}  # IP 留空的沉底


def test_apply_pull_merges_and_reports():
    current = {"陈鋆": "10.91.1.197", "老王": ""}
    incoming = {"郑泽锋": "10.91.1.206", "陈鋆": "10.91.1.198"}
    merged, note = roster.apply_pull(current, incoming)
    assert merged["郑泽锋"] == "10.91.1.206"
    assert merged["陈鋆"] == "10.91.1.198"  # 同名以中心为准
    assert merged["老王"] == ""  # 本地独有保留
    assert "已并入中心名单" in note


def test_apply_pull_replaces_when_asked():
    merged, note = roster.apply_pull({"老王": ""}, {"陈鋆": "10.91.1.197"}, replace=True)
    assert merged == {"陈鋆": "10.91.1.197"}
    assert "替换" in note


def test_apply_pull_rejects_empty_center():
    with pytest.raises(roster.RosterError):
        roster.apply_pull({"老王": ""}, {})


def test_fetch_before_publish_says_not_published(center):
    base, _path, _httpd = center
    with pytest.raises(roster.RosterError) as info:
        roster.fetch_roster(base)
    assert "还没发布" in str(info.value)


def test_publish_then_fetch_roundtrip(center):
    base, path, _httpd = center
    payload = roster.publish_roster(base, {"陈鋆": "10.91.1.197", "郑泽锋": "10.91.1.206"})
    assert payload["ok"] is True and payload["count"] == 2
    assert path.is_file()

    text, people = roster.fetch_roster(base)
    assert "陈鋆" in text
    assert people == {"陈鋆": "10.91.1.197", "郑泽锋": "10.91.1.206"}


def test_publish_rejects_empty_roster(center):
    base, _path, _httpd = center
    with pytest.raises(roster.RosterError):
        roster.publish_roster(base, {})


def test_publish_only_from_center_machine(tmp_path):
    httpd = make_registry_server(
        RegistryStore(tmp_path / "registry.json"),
        port=0,
        allow_cidrs=["127.0.0.1"],
        roster_path=tmp_path / "roster.csv",
        roster_admin_ips={"10.9.9.9"},
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(roster.RosterError) as info:
            roster.publish_roster(base, {"陈鋆": "10.91.1.197"})
        assert "只允许" in str(info.value)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_landing_page_lists_published_roster(center):
    base, _path, _httpd = center
    with urllib.request.urlopen(base + "/", timeout=3) as resp:
        before = resp.read().decode("utf-8")
    assert "还没发布" in before

    roster.publish_roster(base, {"陈鋆": "10.91.1.197"})

    with urllib.request.urlopen(base + "/", timeout=3) as resp:
        after = resp.read().decode("utf-8")
    assert "部门授权名单已发布：1 人" in after
    assert "/roster.csv" in after

    with urllib.request.urlopen(base + "/roster.csv", timeout=3) as resp:
        assert resp.status == 200
        assert "陈鋆" in resp.read().decode("utf-8-sig")
