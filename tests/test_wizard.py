"""向导的纯逻辑：拼配置、拦掉不该共享的盘（设计 D8）。"""

from __future__ import annotations

from pathlib import Path

from landoc import wizard


def test_build_config_falls_back_to_default_name(tmp_path):
    cfg = wizard.build_config("   ", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    assert cfg.name == "我的资料库"
    assert cfg.folders == [tmp_path]
    assert cfg.lib_dir == tmp_path / "lib"
    assert cfg.allow_cidrs == ["10.91.0.0/16"]


def test_build_config_local_scope_allows_only_loopback(tmp_path):
    cfg = wizard.build_config("小库", [tmp_path], wizard.SCOPE_LOCAL, tmp_path / "lib")
    assert cfg.allow_cidrs == ["127.0.0.1"]


def test_validate_folder_rejects_unc():
    assert "UNC" in wizard.validate_folder(Path(r"\\server\share\docs"))


def test_validate_folder_rejects_missing(tmp_path):
    assert wizard.validate_folder(tmp_path / "nope") == "目录不存在"


def test_validate_folder_rejects_network_drive(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "_drive_type", lambda path: wizard.DRIVE_REMOTE)
    assert "网络盘" in wizard.validate_folder(tmp_path)


def test_validate_folder_rejects_removable(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "_drive_type", lambda path: wizard.DRIVE_REMOVABLE)
    assert "可移动盘" in wizard.validate_folder(tmp_path)


def test_validate_folder_accepts_local_fixed_drive(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "_drive_type", lambda path: wizard.DRIVE_FIXED)
    assert wizard.validate_folder(tmp_path) is None


def test_estimate_folders_counts_only_convertible_docs(tmp_path):
    (tmp_path / "a.docx").write_bytes(b"x" * 100)
    (tmp_path / "b.pdf").write_bytes(b"y" * 300)
    (tmp_path / "note.txt").write_text("不算", encoding="utf-8")
    estimate = wizard.estimate_folders([tmp_path])
    assert estimate.docs == 2
    assert estimate.size_bytes == 400
    assert estimate.seconds > wizard._SECONDS_PER_ROUND
    assert "2 篇" in estimate.describe()


def test_estimate_folders_empty_folder_says_so(tmp_path):
    estimate = wizard.estimate_folders([tmp_path])
    assert estimate.docs == 0
    assert "没扫到" in estimate.describe()


def test_apply_config_keeps_storage_policy(tmp_path):
    target = wizard.build_config("旧名", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    target.store_content = False
    target.snippet_only_chars = 5000
    source = wizard.build_config("新名", [tmp_path / "other"], wizard.SCOPE_LOCAL, tmp_path / "lib")
    wizard.apply_config(target, source)
    assert target.name == "新名"
    assert target.folders == [tmp_path / "other"]
    assert target.allow_cidrs == ["127.0.0.1"]
    # 存储策略不归向导管，不能被重开向导冲掉
    assert target.store_content is False
    assert target.snippet_only_chars == 5000


def test_build_config_people_scope_keeps_roster(tmp_path):
    cfg = wizard.build_config(
        "小库",
        [tmp_path],
        wizard.SCOPE_PEOPLE,
        tmp_path / "lib",
        people={"陈鋆": "10.91.1.197", "张三": ""},
    )
    assert cfg.allow_cidrs == ["10.91.0.0/16"]  # 先过网段，再核姓名
    assert cfg.allow_people == {"陈鋆": "10.91.1.197", "张三": ""}


def test_build_config_ignores_roster_when_scope_is_lan(tmp_path):
    cfg = wizard.build_config(
        "小库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", people={"陈鋆": "10.91.1.197"}
    )
    assert cfg.allow_people == {}


def test_parse_cidrs_normalises_and_rejects_junk():
    assert wizard.parse_cidrs("10.91.1.0/24, 10.91.10.0/24") == (
        ["10.91.1.0/24", "10.91.10.0/24"],
        None,
    )
    # 只给 IP 时按单主机网段处理；全角逗号/空格也认
    assert wizard.parse_cidrs("10.91.1.5") == (["10.91.1.5/32"], None)
    assert wizard.parse_cidrs("10.91.1.0/24，10.91.2.0/24")[0] == ["10.91.1.0/24", "10.91.2.0/24"]
    assert wizard.parse_cidrs("")[0] == ["10.91.0.0/16"]
    assert wizard.parse_cidrs("10.91.300.0/24")[1] is not None


def test_build_config_uses_custom_cidrs(tmp_path):
    cfg = wizard.build_config(
        "小库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", cidrs=["10.91.1.0/24"]
    )
    assert cfg.allow_cidrs == ["10.91.1.0/24"]


def test_build_config_local_scope_ignores_custom_cidrs(tmp_path):
    cfg = wizard.build_config(
        "小库", [tmp_path], wizard.SCOPE_LOCAL, tmp_path / "lib", cidrs=["10.91.0.0/16"]
    )
    assert cfg.allow_cidrs == ["127.0.0.1"]


def test_validate_person_rules():
    cidrs = wizard.SCOPE_CIDRS[wizard.SCOPE_PEOPLE]
    assert wizard.validate_person("", "10.91.1.197", cidrs) == "姓名不能为空"
    assert "不要带" in wizard.validate_person("陈=鋆", "", cidrs)
    assert "不是合法的 IP" in wizard.validate_person("陈鋆", "10.91.1", cidrs)
    assert wizard.validate_person("陈鋆", "", cidrs) is None
    assert wizard.validate_person("陈鋆", "10.91.1.197", cidrs) is None
    assert "不在允许的网段" in wizard.validate_person("陈鋆", "192.168.1.9", cidrs)


def test_apply_config_copies_people(tmp_path):
    target = wizard.build_config("旧名", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    source = wizard.build_config(
        "新名", [tmp_path], wizard.SCOPE_PEOPLE, tmp_path / "lib", people={"陈鋆": "10.91.1.197"}
    )
    wizard.apply_config(target, source)
    assert target.allow_people == {"陈鋆": "10.91.1.197"}
    assert target.allow_cidrs == ["10.91.0.0/16"]


def test_build_config_keeps_departments_for_people_only(tmp_path):
    cfg = build_config(
        "库",
        [tmp_path],
        SCOPE_PEOPLE,
        tmp_path / "lib",
        people={"陈鋆": "10.91.1.197", "老王": ""},
        departments={"陈鋆": "规划一所", "不相干的人": "水文所"},
    )
    assert cfg.people_departments == {"陈鋆": "规划一所"}


def test_apply_config_copies_departments(tmp_path):
    source = build_config(
        "库",
        [tmp_path],
        SCOPE_PEOPLE,
        tmp_path / "lib",
        people={"陈鋆": "10.91.1.197"},
        departments={"陈鋆": "规划一所"},
    )
    target = Config(name="旧", lib_dir=tmp_path / "old")
    apply_config(target, source)
    assert target.people_departments == {"陈鋆": "规划一所"}


def test_build_config_keeps_departments_for_people_only(tmp_path):
    cfg = wizard.build_config(
        "库",
        [tmp_path],
        wizard.SCOPE_PEOPLE,
        tmp_path / "lib",
        people={"陈鋆": "10.91.1.197", "老王": ""},
        departments={"陈鋆": "规划一所", "不相干的人": "水文所"},
    )
    assert cfg.people_departments == {"陈鋆": "规划一所"}


def test_apply_config_copies_departments(tmp_path):
    source = wizard.build_config(
        "库",
        [tmp_path],
        wizard.SCOPE_PEOPLE,
        tmp_path / "lib",
        people={"陈鋆": "10.91.1.197"},
        departments={"陈鋆": "规划一所"},
    )
    target = wizard.Config(name="旧", lib_dir=tmp_path / "old")
    wizard.apply_config(target, source)
    assert target.people_departments == {"陈鋆": "规划一所"}
