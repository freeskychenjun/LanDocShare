"""名单解析与合并（people.py）的单元测试。"""

from __future__ import annotations

import pytest

from landoc.people import (
    dump_people_csv,
    load_people_csv,
    merge_people,
    parse_people_csv,
)


def test_parse_handles_header_bom_comments_and_fullwidth_commas():
    text = "\ufeff姓名,IP\n# 部门名单\n陈鋆,10.91.1.197\n杨欢，10.91.5.238\n张三,\n\n"
    people, warnings = parse_people_csv(text)
    assert people == {"陈鋆": "10.91.1.197", "杨欢": "10.91.5.238", "张三": ""}
    assert warnings == []


def test_parse_without_header_and_with_tabs():
    people, warnings = parse_people_csv("李四\t10.91.1.5\n王五\n")
    assert people == {"李四": "10.91.1.5", "王五": ""}
    assert warnings == []


def test_duplicate_name_keeps_first_and_warns():
    people, warnings = parse_people_csv("姓名,IP\n陈鋆,10.91.1.197\n陈鋆,10.91.1.198\n")
    assert people == {"陈鋆": "10.91.1.197"}
    assert len(warnings) == 1
    assert "重复" in warnings[0]


def test_bad_ip_reports_line_number():
    with pytest.raises(ValueError) as excinfo:
        parse_people_csv("姓名,IP\n陈鋆,10.91.1.197\n杨欢,10.91.5.999\n")
    assert "第 3 行" in str(excinfo.value)


def test_load_reads_gbk_csv(tmp_path):
    path = tmp_path / "名单.csv"
    path.write_bytes("姓名,IP\n陈鋆,10.91.1.197\n".encode("gbk"))
    people, _ = load_people_csv(path)
    assert people == {"陈鋆": "10.91.1.197"}


def test_dump_roundtrips_through_parse():
    people = {"陈鋆": "10.91.1.197", "张三": ""}
    again, _ = parse_people_csv(dump_people_csv(people))
    assert again == people


def test_merge_adds_and_updates_without_dropping():
    merged, notes = merge_people({"陈鋆": "10.91.1.197"}, {"陈鋆": "10.91.1.198", "杨欢": "10.91.5.238"})
    assert merged == {"陈鋆": "10.91.1.198", "杨欢": "10.91.5.238"}
    assert any("新增 1 人" in note for note in notes)
    assert any("更新 1 人" in note for note in notes)


def test_merge_replace_follows_incoming_only():
    merged, notes = merge_people(
        {"陈鋆": "10.91.1.197", "老同事": "10.91.1.9"},
        {"陈鋆": "10.91.1.197", "杨欢": "10.91.5.238"},
        replace=True,
    )
    assert merged == {"陈鋆": "10.91.1.197", "杨欢": "10.91.5.238"}
    assert any("移除 1 人" in note for note in notes)
