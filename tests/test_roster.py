"""授权名单：本地增删排序、部门列、与登记中心之间的发布/拉取。"""

from __future__ import annotations

import threading
import urllib.request

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


def test_center_host_parsing():
    assert roster.center_host("http://10.91.1.197:8767") == "10.91.1.197"
    assert roster.center_host("10.91.1.197:8767/") == "10.91.1.197"
    assert roster.center_host("") == ""


def test_is_center_machine_only_on_the_registry_host():
    # 中心地址就是本机地址 → 管理员机器，能发布
    assert roster.is_center_machine("http://127.0.0.1:8767") is True
    assert roster.is_center_machine("http://localhost:8767") is True
    assert roster.is_center_machine("http://10.91.1.197:8767", ips={"10.91.1.197"}) is True
    # 同事的机器：中心在别人那里 → 只能拉取，不给发布
    assert roster.is_center_machine("http://10.91.1.197:8767", ips={"10.91.1.50"}) is False
    # 没配中心 / 认不出 → 当"不是"
    assert roster.is_center_machine("") is False


def test_validate_entry_normalises_and_rejects():
    assert roster.validate_entry("  陈鋆 ", "", "") == ("陈鋆", "", "")
    assert roster.validate_entry("郑泽锋", " 10.91.1.206 ", " 规划一所 ") == (
        "郑泽锋",
        "10.91.1.206",
        "规划一所",
    )

    with pytest.raises(ValueError):
        roster.validate_entry("", "10.91.1.1")
    with pytest.raises(ValueError):
        roster.validate_entry("a=b", "10.91.1.1")
    with pytest.raises(ValueError):
        roster.validate_entry("张三", "10.91.1.999")
    with pytest.raises(ValueError):
        roster.validate_entry("张三", "", "规划一所,水文所")


def test_sorted_entries_groups_by_department_then_ip():
    book = roster.Roster(
        {"老王": "", "郑泽锋": "10.91.1.206", "杨欢": "10.91.5.238", "陈鋆": "10.91.1.197"},
        {"郑泽锋": "水文所", "陈鋆": "水文所", "杨欢": "规划二所"},
    )
    order = [name for name, _ip, _dept in roster.sorted_entries(book)]
    # 部门内按 IP 排，没部门的沉底
    assert order[:2] == ["陈鋆", "郑泽锋"]
    assert order[2] == "杨欢"
    assert order[3] == "老王"


def test_sorted_entries_keeps_department_insertion_order():
    # 部门先后取决于名单里第一次出现的先后（跟导入的 CSV 走），不是按名称字符排
    book = roster.Roster(
        {"a": "10.91.1.5", "b": "10.91.1.6"},
        {"a": "规划二所", "b": "水文所"},
    )
    assert [dept for _n, _ip, dept in roster.sorted_entries(book)] == ["规划二所", "水文所"]


def test_department_counts_and_unnamed_group():
    book = roster.Roster({"a": "", "b": "", "c": ""}, {"a": "规划一所", "b": "规划一所"})
    assert book.department_counts() == [("规划一所", 2), ("未分部门", 1)]


def test_apply_pull_merges_people_and_departments():
    current = roster.Roster({"陈鋆": "10.91.1.197", "老王": ""}, {"老王": "水文所"})
    incoming = roster.Roster({"郑泽锋": "10.91.1.206", "陈鋆": "10.91.1.198"}, {"郑泽锋": "规划一所"})
    merged, note = roster.apply_pull(current, incoming)
    assert merged.people["郑泽锋"] == "10.91.1.206"
    assert merged.people["陈鋆"] == "10.91.1.198"  # 同名以中心为准
    assert merged.people["老王"] == ""  # 本地独有保留
    assert merged.departments == {"老王": "水文所", "郑泽锋": "规划一所"}
    assert "已并入中心名单" in note


def test_apply_pull_replaces_when_asked():
    merged, note = roster.apply_pull(
        roster.Roster({"老王": ""}, {"老王": "水文所"}),
        roster.Roster({"陈鋆": "10.91.1.197"}, {"陈鋆": "规划一所"}),
        replace=True,
    )
    assert merged.people == {"陈鋆": "10.91.1.197"}
    assert merged.departments == {"陈鋆": "规划一所"}
    assert "替换" in note


def test_apply_pull_rejects_empty_center():
    with pytest.raises(roster.RosterError):
        roster.apply_pull(roster.Roster({"老王": ""}), roster.Roster())


def test_roster_to_config_prunes_orphans(tmp_path):
    from landoc.config import Config

    cfg = Config(name="库", lib_dir=tmp_path)
    roster.Roster({"陈鋆": "10.91.1.197"}, {"陈鋆": "规划一所", "已删除的人": "水文所"}).to_config(cfg)
    assert cfg.allow_people == {"陈鋆": "10.91.1.197"}
    assert cfg.people_departments == {"陈鋆": "规划一所"}


def test_fetch_before_publish_says_not_published(center):
    base, _path, _httpd = center
    with pytest.raises(roster.RosterError) as info:
        roster.fetch_roster(base)
    assert "还没发布" in str(info.value)


def test_publish_then_fetch_roundtrip_keeps_departments(center):
    base, path, _httpd = center
    book = roster.Roster(
        {"陈鋆": "10.91.1.197", "郑泽锋": "10.91.1.206"},
        {"陈鋆": "规划一所", "郑泽锋": "水文所"},
    )
    payload = roster.publish_roster(base, book)
    assert payload["ok"] is True and payload["count"] == 2
    assert payload["departments"] == [["水文所", 1], ["规划一所", 1]]
    assert path.is_file()

    text, fetched = roster.fetch_roster(base)
    assert "部门" in text
    assert fetched.people == book.people
    assert fetched.departments == book.departments


def test_publish_rejects_empty_roster(center):
    base, _path, _httpd = center
    with pytest.raises(roster.RosterError):
        roster.publish_roster(base, roster.Roster())


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
            roster.publish_roster(base, roster.Roster({"陈鋆": "10.91.1.197"}))
        assert "只允许" in str(info.value)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_landing_page_lists_published_roster_with_departments(center):
    base, _path, _httpd = center
    with urllib.request.urlopen(base + "/", timeout=3) as resp:
        before = resp.read().decode("utf-8")
    assert "还没发布" in before

    roster.publish_roster(
        base,
        roster.Roster({"陈鋆": "10.91.1.197", "郑泽锋": "10.91.1.206"}, {"陈鋆": "规划一所"}),
    )

    with urllib.request.urlopen(base + "/", timeout=3) as resp:
        after = resp.read().decode("utf-8")
    assert "部门授权名单已发布：2 人" in after
    assert "规划一所 1" in after
    assert "/roster.csv" in after

    with urllib.request.urlopen(base + "/roster.csv", timeout=3) as resp:
        assert resp.status == 200
        csv_text = resp.read().decode("utf-8-sig")
    assert "陈鋆" in csv_text
    assert "规划一所" in csv_text
