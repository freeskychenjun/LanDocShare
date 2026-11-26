"""向导的纯逻辑：拼配置、拦掉不该共享的盘（设计 D8）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from landoc import wizard


@pytest.fixture
def site_cidrs(monkeypatch):
    """把"本机所在网段"钉死：默认允许网段现在按网卡现算，断言不能靠恰好是某台机器。"""
    cidrs = ["10.91.0.0/16"]
    monkeypatch.setattr(wizard, "default_lan_cidrs", lambda: list(cidrs))
    return cidrs


def test_build_config_falls_back_to_default_name(tmp_path, site_cidrs):
    cfg = wizard.build_config("   ", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    assert cfg.name == "我的资料库"
    assert cfg.folders == [tmp_path]
    assert cfg.lib_dir == tmp_path / "lib"
    assert cfg.allow_cidrs == site_cidrs


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


def test_same_home_name_conflict_detects_two_folders_with_the_same_leaf(tmp_path):
    """两个「资料」会让库内键撞车，必须建库前就拦下来。"""
    first = tmp_path / "甲" / "资料"
    second = tmp_path / "乙" / "资料"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    message = wizard.same_home_name_conflict([first, second])
    assert message is not None
    assert "「资料」" in message
    assert "改名" in message


def test_same_home_name_conflict_is_quiet_for_distinct_names(tmp_path):
    a = tmp_path / "资料"
    b = tmp_path / "成果"
    a.mkdir()
    b.mkdir()
    assert wizard.same_home_name_conflict([a, b]) is None
    # 同一个路径写两遍不算冲突
    assert wizard.same_home_name_conflict([a, a]) is None


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


def _indexed_records(tmp_path, files: dict[str, tuple[int, float]]) -> dict[str, tuple[int, float]]:
    """造一份"索引里已有这些文档"的清单（键是库内 md 相对路径）。"""
    top = tmp_path.name
    return {f"{top}/{name}.md": record for name, record in files.items()}


def test_estimate_folders_edit_mode_reports_pending(tmp_path):
    """已建好库：要报"还差多少篇"，不是"从零建要多久"。"""
    (tmp_path / "a.docx").write_bytes(b"x" * 100)
    (tmp_path / "b.pdf").write_bytes(b"y" * 300)
    (tmp_path / "c.docx").write_bytes(b"z" * 50)
    stats = {}
    for name in ("a.docx", "b.pdf"):
        path = tmp_path / name
        stats[name] = (path.stat().st_size, path.stat().st_mtime)
    known = _indexed_records(tmp_path, stats)

    estimate = wizard.estimate_folders([tmp_path], known=known)

    assert estimate.docs == 3
    assert estimate.pending == 1  # c.docx 还没收
    assert estimate.indexed == 2
    text = estimate.describe()
    assert "当前库 3 篇" in text
    assert "已收录 2" in text
    assert "待补收" in text and "1 篇" in text
    assert "从零" not in text


def test_estimate_folders_edit_mode_all_indexed_says_incremental(tmp_path):
    (tmp_path / "a.docx").write_bytes(b"x" * 100)
    path = tmp_path / "a.docx"
    known = _indexed_records(tmp_path, {"a.docx": (path.stat().st_size, path.stat().st_mtime)})

    estimate = wizard.estimate_folders([tmp_path], known=known)

    assert estimate.pending == 0
    assert "都已收录" in estimate.describe()


def test_estimate_folders_edit_mode_counts_changed_doc_as_pending(tmp_path):
    """源文件改过（体积/修改时间变了）保存后会被重转，也该算待补收。"""
    path = tmp_path / "a.docx"
    path.write_bytes(b"x" * 100)
    known = _indexed_records(tmp_path, {"a.docx": (100, 1.0)})  # 老记录

    assert wizard.estimate_folders([tmp_path], known=known).pending == 1


def test_estimate_folders_first_build_has_no_pending(tmp_path):
    """首次建库没有清单可比：pending 必须是 None，文案不能出现"已收录"。"""
    (tmp_path / "a.docx").write_bytes(b"x" * 100)

    estimate = wizard.estimate_folders([tmp_path])

    assert estimate.pending is None
    assert estimate.indexed is None
    assert "已收录" not in estimate.describe()


def test_wizard_module_exposes_index_for_edit_mode_estimate():
    """`kick_estimate` 里直接调 `index.read_records`，这个名字没导入只在点开窗口时炸。"""
    from landoc import index as index_module

    assert wizard.index is index_module
    assert callable(wizard.index.read_records)


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


def test_build_config_people_scope_keeps_roster(tmp_path, site_cidrs):
    cfg = wizard.build_config(
        "小库",
        [tmp_path],
        wizard.SCOPE_PEOPLE,
        tmp_path / "lib",
        people={"赵六": "172.16.1.197", "张三": ""},
    )
    assert cfg.allow_cidrs == site_cidrs  # 先过网段，再核姓名
    assert cfg.allow_people == {"赵六": "172.16.1.197", "张三": ""}


def test_build_config_ignores_roster_when_scope_is_lan(tmp_path):
    cfg = wizard.build_config(
        "小库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", people={"赵六": "172.16.1.197"}
    )
    assert cfg.allow_people == {}


def test_parse_cidrs_normalises_and_rejects_junk(site_cidrs):
    assert wizard.parse_cidrs("172.16.1.0/24, 172.16.10.0/24") == (
        ["172.16.1.0/24", "172.16.10.0/24"],
        None,
    )
    # 只给 IP 时按单主机网段处理；全角逗号/空格也认
    assert wizard.parse_cidrs("172.16.1.5") == (["172.16.1.5/32"], None)
    assert wizard.parse_cidrs("172.16.1.0/24，172.16.2.0/24")[0] == ["172.16.1.0/24", "172.16.2.0/24"]
    assert wizard.parse_cidrs("")[0] == site_cidrs  # 空输入回落到"本机所在网段"
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
    # 显式给网段：默认值现在按本机网卡现算，不能再拿 SCOPE_CIDRS 里的占位值当断言依据
    cidrs = ["172.16.0.0/16"]
    assert wizard.validate_person("", "172.16.1.197", cidrs) == "姓名不能为空"
    assert "不要带" in wizard.validate_person("陈=鋆", "", cidrs)
    assert "不是合法的 IP" in wizard.validate_person("赵六", "172.16.1", cidrs)
    assert "IP 不能为空" in wizard.validate_person("赵六", "", cidrs)
    assert wizard.validate_person("赵六", "172.16.1.197", cidrs) is None
    assert "不在允许的网段" in wizard.validate_person("赵六", "192.168.1.9", cidrs)


def test_apply_config_copies_people(tmp_path, site_cidrs):
    target = wizard.build_config("旧名", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    source = wizard.build_config(
        "新名", [tmp_path], wizard.SCOPE_PEOPLE, tmp_path / "lib", people={"赵六": "172.16.1.197"}
    )
    wizard.apply_config(target, source)
    assert target.allow_people == {"赵六": "172.16.1.197"}
    assert target.allow_cidrs == site_cidrs


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


def test_parse_registry_url_accepts_and_normalises():
    """登记中心地址：留空 = 不登记；裸 host:port 自动补 http://；末尾斜杠去掉。"""
    assert wizard.parse_registry_url("") == ("", None)
    assert wizard.parse_registry_url("   ") == ("", None)
    assert wizard.parse_registry_url("http://192.0.2.5:8767") == ("http://192.0.2.5:8767", None)
    assert wizard.parse_registry_url(" http://192.0.2.5:8767/ ") == ("http://192.0.2.5:8767", None)
    assert wizard.parse_registry_url("192.0.2.5:8767") == ("http://192.0.2.5:8767", None)
    assert wizard.parse_registry_url("https://libraries.example.cn") == (
        "https://libraries.example.cn",
        None,
    )


def test_parse_registry_url_rejects_junk():
    for bad in (
        "ftp://192.0.2.5:8767",  # 只收 http/https
        "http://",  # 没有主机名
        "http://192.0.2.5:端 口",  # 端口不是数字
        "http://192.0.2.5:8767?x=1",  # 带查询串
        "http://192.0.2.5:8767#top",
    ):
        value, error = wizard.parse_registry_url(bad)
        assert value == "", bad
        assert error, bad


def test_build_config_carries_registry_url(tmp_path):
    cfg = wizard.build_config(
        "库",
        [tmp_path],
        wizard.SCOPE_LAN,
        tmp_path / "lib",
        registry_url="http://192.0.2.5:8767",
    )
    assert cfg.registry_url == "http://192.0.2.5:8767"
    assert wizard.build_config("库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib").registry_url == ""


def test_apply_config_copies_registry_url(tmp_path):
    """改建库设置时改了登记中心，要真的回写进正在运行的 Config。"""
    target = wizard.build_config("旧库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib")
    source = wizard.build_config(
        "旧库",
        [tmp_path],
        wizard.SCOPE_LAN,
        tmp_path / "lib",
        registry_url="http://192.0.2.9:8767",
    )

    wizard.apply_config(target, source)

    assert target.registry_url == "http://192.0.2.9:8767"


def test_wizard_prefills_registry_url_from_the_shipped_default(tmp_path, monkeypatch):
    """打包版里 site_defaults.json 写好了中心地址：首次建库的向导一打开就该是填好的。"""
    from landoc import ship

    monkeypatch.setattr(ship, "shipped_registry_url", lambda: "http://192.0.2.5:8767")
    assert wizard.default_registry_url(None) == "http://192.0.2.5:8767"

    # 改建库设置时不能被随包默认值冲掉：用配置里已有的地址
    existing = wizard.build_config(
        "库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", registry_url="http://192.0.2.9:8767"
    )
    assert wizard.default_registry_url(existing) == "http://192.0.2.9:8767"


def test_new_libraries_default_to_the_roster(tmp_path):
    """新建库默认「仅指定名单」：全内网开放太松，不知道谁在查。"""
    assert wizard.default_scope(None) == wizard.SCOPE_PEOPLE


def test_existing_library_keeps_its_own_scope():
    """改已有库的设置时不能替人做主：按它现在是什么档就选什么档。"""
    lan = wizard.build_config("库", [], wizard.SCOPE_LAN, Path("lib"), cidrs=["10.91.0.0/16"])
    assert wizard.default_scope(lan) == wizard.SCOPE_LAN
    only_me = wizard.build_config("库", [], wizard.SCOPE_LOCAL, Path("lib"))
    assert wizard.default_scope(only_me) == wizard.SCOPE_LOCAL
    roster = wizard.build_config(
        "库", [], wizard.SCOPE_PEOPLE, Path("lib"), people={"张三": "10.91.1.23"}
    )
    assert wizard.default_scope(roster) == wizard.SCOPE_PEOPLE


def test_person_row_error_catches_the_usual_mistakes():
    assert wizard.person_row_error("张三", "10.91.1.23", {}, ["10.91.0.0/16"]) is None
    # 重名：改名或先删掉旧的
    assert "已经有" in (wizard.person_row_error("张三", "10.91.1.99", {"张三": "10.91.1.23"}) or "")
    # 不合法的 IP
    assert wizard.person_row_error("张三", "10.91.1", {}, ["10.91.0.0/16"])
    # 落在允许网段之外
    assert wizard.person_row_error("张三", "192.0.2.9", {}, ["10.91.0.0/16"])


def test_people_from_rows_keeps_the_first_and_skips_blanks():
    rows = [("张三", "10.91.1.23"), ("", "10.91.1.9"), ("张三", "10.91.1.99"), ("李四", "10.91.1.24")]
    assert wizard.people_from_rows(rows) == {"张三": "10.91.1.23", "李四": "10.91.1.24"}


def test_build_config_keeps_the_roster_only_for_the_people_scope(tmp_path):
    people = {"张三": "10.91.1.23"}
    kept = wizard.build_config(
        "库", [tmp_path], wizard.SCOPE_PEOPLE, tmp_path / "lib", people=people, cidrs=["10.91.0.0/16"]
    )
    assert kept.allow_people == people
    dropped = wizard.build_config(
        "库", [tmp_path], wizard.SCOPE_LAN, tmp_path / "lib", people=people, cidrs=["10.91.0.0/16"]
    )
    assert dropped.allow_people == {}
