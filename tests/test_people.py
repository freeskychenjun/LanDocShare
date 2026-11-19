"""名单解析与合并（people.py）的单元测试。"""

from __future__ import annotations

import pytest

from landoc.people import (
    dump_people_csv,
    load_people_csv,
    merge_people,
    parse_people_csv,
    parse_people_table,
)


def test_parse_handles_header_bom_comments_and_fullwidth_commas():
    text = "\ufeff姓名,IP\n# 部门名单\n赵六,172.16.1.197\n杨欢，172.16.5.238\n\n"
    people, warnings = parse_people_csv(text)
    assert people == {"赵六": "172.16.1.197", "杨欢": "172.16.5.238"}
    assert warnings == []


def test_parse_without_header_and_with_tabs():
    people, warnings = parse_people_csv("李四\t172.16.1.5\n")
    assert people == {"李四": "172.16.1.5"}
    assert warnings == []


def test_duplicate_name_keeps_first_and_warns():
    people, warnings = parse_people_csv("姓名,IP\n赵六,172.16.1.197\n赵六,172.16.1.198\n")
    assert people == {"赵六": "172.16.1.197"}
    assert len(warnings) == 1
    assert "重复" in warnings[0]


def test_bad_ip_reports_line_number():
    with pytest.raises(ValueError) as excinfo:
        parse_people_csv("姓名,IP\n赵六,172.16.1.197\n杨欢,172.16.5.999\n")
    assert "第 3 行" in str(excinfo.value)


def test_load_reads_gbk_csv(tmp_path):
    path = tmp_path / "名单.csv"
    path.write_bytes("姓名,IP\n赵六,172.16.1.197\n".encode("gbk"))
    people, _ = load_people_csv(path)
    assert people == {"赵六": "172.16.1.197"}


def test_dump_roundtrips_through_parse():
    people = {"赵六": "172.16.1.197", "张三": "172.16.1.201"}
    again, _ = parse_people_csv(dump_people_csv(people))
    assert again == people


def test_merge_adds_and_updates_without_dropping():
    merged, notes = merge_people({"赵六": "172.16.1.197"}, {"赵六": "172.16.1.198", "杨欢": "172.16.5.238"})
    assert merged == {"赵六": "172.16.1.198", "杨欢": "172.16.5.238"}
    assert any("新增 1 人" in note for note in notes)
    assert any("更新 1 人" in note for note in notes)


def test_merge_replace_follows_incoming_only():
    merged, notes = merge_people(
        {"赵六": "172.16.1.197", "老同事": "172.16.1.9"},
        {"赵六": "172.16.1.197", "杨欢": "172.16.5.238"},
        replace=True,
    )
    assert merged == {"赵六": "172.16.1.197", "杨欢": "172.16.5.238"}
    assert any("移除 1 人" in note for note in notes)


def test_parse_people_table_reads_department_column():
    text = "姓名,IP,部门\n赵六,172.16.1.197,规划一所\n郑泽锋,172.16.1.206,水文所\n"
    people, departments, warnings = parse_people_table(text)
    assert people == {"赵六": "172.16.1.197", "郑泽锋": "172.16.1.206"}
    assert departments == {"赵六": "规划一所", "郑泽锋": "水文所"}
    assert warnings == []


def test_parse_people_table_rejects_blank_ip():
    text = "姓名,IP,部门\n赵六,172.16.1.197,规划一所\n老王,,\n"
    with pytest.raises(ValueError, match="老王.*IP 为空"):
        parse_people_table(text)


def test_dump_people_csv_adds_department_only_when_present():
    two = dump_people_csv({"赵六": "172.16.1.197"})
    assert two.splitlines()[0] == "姓名,IP"

    three = dump_people_csv({"赵六": "172.16.1.197"}, {"赵六": "规划一所"})
    assert three.splitlines()[0] == "姓名,IP,部门"
    assert "赵六,172.16.1.197,规划一所" in three


def test_people_csv_roundtrip_keeps_departments():
    people = {"赵六": "172.16.1.197", "老王": "172.16.1.50"}
    departments = {"赵六": "规划一所"}
    again_people, again_departments, _ = parse_people_table(
        dump_people_csv(people, departments)
    )
    assert again_people == people
    assert again_departments == departments
