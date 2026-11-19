"""向导的纯逻辑：拼配置、拦掉不该共享的盘（设计 D8）。"""

from __future__ import annotations

from pathlib import Path

from landoc import wizard


def test_build_config_falls_back_to_default_name(tmp_path):
    cfg = wizard.build_config("   ", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    assert cfg.name == "我的资料库"
    assert cfg.folders == [tmp_path]
    assert cfg.lib_dir == tmp_path / "lib"
    assert cfg.allow_cidrs == ["172.16.0.0/16"]


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
        people={"赵六": "172.16.1.197", "张三": ""},
    )
    assert cfg.allow_cidrs == ["172.16.0.0/16"]  # 先过网段，再核姓名
    assert cfg.allow_people == {"赵六": "172.16.1.197", "张三": ""}


def test_build_config_ignores_roster_when_scope_is_lan(tmp_path):
    cfg = wizard.build_config(
        "小库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", people={"赵六": "172.16.1.197"}
    )
    assert cfg.allow_people == {}


def test_parse_cidrs_normalises_and_rejects_junk():
    assert wizard.parse_cidrs("172.16.1.0/24, 172.16.10.0/24") == (
        ["172.16.1.0/24", "172.16.10.0/24"],
        None,
    )
    # 只给 IP 时按单主机网段处理；全角逗号/空格也认
    assert wizard.parse_cidrs("172.16.1.5") == (["172.16.1.5/32"], None)
    assert wizard.parse_cidrs("172.16.1.0/24，172.16.2.0/24")[0] == ["172.16.1.0/24", "172.16.2.0/24"]
    assert wizard.parse_cidrs("")[0] == ["172.16.0.0/16"]
    assert wizard.parse_cidrs("172.16.300.0/24")[1] is not None


def test_build_config_uses_custom_cidrs(tmp_path):
    cfg = wizard.build_config(
        "小库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", cidrs=["172.16.1.0/24"]
    )
    assert cfg.allow_cidrs == ["172.16.1.0/24"]


def test_build_config_local_scope_ignores_custom_cidrs(tmp_path):
    cfg = wizard.build_config(
        "小库", [tmp_path], wizard.SCOPE_LOCAL, tmp_path / "lib", cidrs=["172.16.0.0/16"]
    )
    assert cfg.allow_cidrs == ["127.0.0.1"]


def test_validate_person_rules():
    cidrs = wizard.SCOPE_CIDRS[wizard.SCOPE_PEOPLE]
    assert wizard.validate_person("", "172.16.1.197", cidrs) == "姓名不能为空"
    assert "不要带" in wizard.validate_person("陈=鋆", "", cidrs)
    assert "不是合法的 IP" in wizard.validate_person("赵六", "172.16.1", cidrs)
    assert "IP 不能为空" in wizard.validate_person("赵六", "", cidrs)
    assert wizard.validate_person("赵六", "172.16.1.197", cidrs) is None
    assert "不在允许的网段" in wizard.validate_person("赵六", "192.168.1.9", cidrs)


def test_apply_config_copies_people(tmp_path):
    target = wizard.build_config("旧名", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    source = wizard.build_config(
        "新名", [tmp_path], wizard.SCOPE_PEOPLE, tmp_path / "lib", people={"赵六": "172.16.1.197"}
    )
    wizard.apply_config(target, source)
    assert target.allow_people == {"赵六": "172.16.1.197"}
    assert target.allow_cidrs == ["172.16.0.0/16"]


def _make_home(tmp_path: Path, *subdirs: str) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for sub in subdirs:
        home.joinpath(*sub.split("/")).mkdir(parents=True)
    return home


def test_default_lib_dir_lands_in_documents(tmp_path):
    home = _make_home(tmp_path, "Documents")
    assert wizard.default_lib_dir(home) == home / "Documents" / "LanDocShare" / f"{home.name}的资料库"


def test_default_lib_dir_accepts_chinese_documents_folder(tmp_path):
    home = _make_home(tmp_path, "文档")
    assert wizard.default_lib_dir(home) == home / "文档" / "LanDocShare" / f"{home.name}的资料库"


def test_default_lib_dir_accepts_onedrive_documents(tmp_path):
    home = _make_home(tmp_path, "OneDrive/Documents")
    got = wizard.default_lib_dir(home)
    assert got == home / "OneDrive" / "Documents" / "LanDocShare" / f"{home.name}的资料库"


def test_default_lib_dir_falls_back_to_localappdata(tmp_path, monkeypatch):
    home = _make_home(tmp_path)  # 没有"文档"目录
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert wizard.default_lib_dir(home) == local / "LanDocShare" / "library" / f"{home.name}的资料库"


def test_default_lib_dir_falls_back_to_home_when_no_localappdata(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert wizard.default_lib_dir(home) == home / ".landoc" / "library" / f"{home.name}的资料库"


def test_validate_lib_dir_rejects_empty():
    assert wizard.validate_lib_dir("   ")[1] == "库位置不能为空"


def test_validate_lib_dir_rejects_relative(tmp_path):
    path, error = wizard.validate_lib_dir("资料库")
    assert path is None
    assert "完整路径" in error


def test_validate_lib_dir_rejects_drive_root(tmp_path):
    path, error = wizard.validate_lib_dir(tmp_path.anchor)
    assert path is None
    assert "盘符根目录" in error


def test_validate_lib_dir_rejects_existing_library(tmp_path):
    lib = tmp_path / "已有库"
    lib.mkdir()
    cfg = wizard.build_config("老王的库", [tmp_path], wizard.SCOPE_LOCAL, lib)
    cfg.save()

    path, error = wizard.validate_lib_dir(str(lib))
    assert path is None
    assert "老王的库" in error


def test_validate_lib_dir_accepts_fresh_folder_and_strips_quotes(tmp_path):
    target = tmp_path / "父目录" / "新库位置"
    path, error = wizard.validate_lib_dir(f'"{target}"')
    assert error is None
    assert path == target
    # 校验没有副作用：父目录与目标都留到真正建库时再建（点取消不该在磁盘上留空目录）
    assert not target.parent.exists()
    assert not target.exists()


def test_parse_deny_cidrs_empty_means_no_deny():
    """空输入=不额外拒绝；绝不能像允许名单那样回落到 172.16.0.0/16。"""
    assert wizard.parse_deny_cidrs("") == ([], None)
    assert wizard.parse_deny_cidrs("   ") == ([], None)


def test_parse_deny_cidrs_normalises_and_rejects_junk():
    assert wizard.parse_deny_cidrs("172.16.1.50，172.16.2.0/24") == (
        ["172.16.1.50/32", "172.16.2.0/24"],
        None,
    )
    networks, error = wizard.parse_deny_cidrs("172.16.0.0/16, 乱写")
    assert networks == []
    assert "乱写" in error


def test_build_config_carries_deny_cidrs(tmp_path):
    cfg = wizard.build_config(
        "库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", deny_cidrs=["172.16.9.0/24"]
    )
    assert cfg.deny_cidrs == ["172.16.9.0/24"]
    # 不给就是"不额外拒绝"，不是继承某个默认网段
    assert wizard.build_config("库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib").deny_cidrs == []


def test_build_config_normalises_acrobat_tool_dir_to_path(tmp_path):
    """Config 声明的是 Path|None：别塞一个 str 或 "" 进去（4.1）。"""
    cfg = wizard.build_config(
        "库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", acrobat_tool_dir=str(tmp_path / "tool")
    )
    assert cfg.acrobat_tool_dir == tmp_path / "tool"
    empty = wizard.build_config(
        "库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", acrobat_tool_dir=""
    )
    assert empty.acrobat_tool_dir is None


def test_apply_config_copies_deny_cidrs(tmp_path):
    """改建库设置时，向导里显示并回写的拒绝网段要真的生效（不能只改不存）。"""
    target = wizard.build_config("旧库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    source = wizard.build_config(
        "新库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", deny_cidrs=["172.16.1.50/32"]
    )

    wizard.apply_config(target, source)

    assert target.deny_cidrs == ["172.16.1.50/32"]


def test_validate_lib_dir_allows_current_library_in_edit_mode(tmp_path):
    lib = tmp_path / "库"
    lib.mkdir()
    cfg = wizard.build_config("库", [tmp_path], wizard.SCOPE_LOCAL, lib)
    cfg.save()

    path, error = wizard.validate_lib_dir(str(lib), cfg)
    assert error is None
    assert path == lib


def test_build_config_keeps_departments_for_people_only(tmp_path):
    cfg = wizard.build_config(
        "库",
        [tmp_path],
        wizard.SCOPE_PEOPLE,
        tmp_path / "lib",
        people={"赵六": "172.16.1.197", "老王": ""},
        departments={"赵六": "规划一所", "不相干的人": "水文所"},
    )
    assert cfg.people_departments == {"赵六": "规划一所"}


def test_apply_config_copies_departments(tmp_path):
    source = wizard.build_config(
        "库",
        [tmp_path],
        wizard.SCOPE_PEOPLE,
        tmp_path / "lib",
        people={"赵六": "172.16.1.197"},
        departments={"赵六": "规划一所"},
    )
    target = wizard.Config(name="旧", lib_dir=tmp_path / "old")
    wizard.apply_config(target, source)
    assert target.people_departments == {"赵六": "规划一所"}
