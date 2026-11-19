"""客户端 skill 的查询脚本：合并排序、失败隔离、缓存回落。"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

QUERY_PATH = Path(__file__).resolve().parents[1] / "skill" / "lan-lib-search" / "scripts" / "query.py"


@pytest.fixture()
def q(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("lanlib_query", QUERY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("LANLIB_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LANLIB_REGISTRY", raising=False)
    return module


def found(lib_name: str, url: str, scores: list[float], ok: bool = True) -> dict:
    return {
        "lib": {"name": lib_name, "lib_name": f"{lib_name}的资料库", "url": url, "doc_count": 13},
        "ok": ok,
        "error": "" if ok else "timeout",
        "results": [
            {"title": f"{lib_name}-文档{n}", "path": f"dir/{lib_name}-{n}.md", "ext": "docx", "score": s, "snippet": "…"}
            for n, s in enumerate(scores)
        ],
    }


def test_merge_normalises_each_library_to_its_best(q):
    merged = q.merge([found("甲", "http://a:8768/", [-10.0, -5.0]), found("乙", "http://b:8768/", [-2.0, -1.0])])
    ranks = {(row["lib_name"], row["title"]): row["rank"] for row in merged}
    assert ranks[("甲", "甲-文档0")] == 1.0
    assert ranks[("甲", "甲-文档1")] == 0.5
    assert ranks[("乙", "乙-文档0")] == 1.0  # 乙库最差的一条也是乙库里最好的
    assert ranks[("乙", "乙-文档1")] == 0.5


def test_merge_applies_library_weight(q):
    merged = q.merge(
        [found("甲", "http://a:8768/", [-10.0]), found("乙", "http://b:8768/", [-10.0])],
        weights={"乙": 1.5},
    )
    assert [row["lib_name"] for row in merged] == ["乙", "甲"]


def test_merge_skips_failed_and_empty_libraries(q):
    merged = q.merge([found("甲", "http://a:8768/", [-1.0], ok=False), found("乙", "http://b:8768/", [])])
    assert merged == []


def test_merge_keeps_in_library_order_and_takes_top_n(q):
    # 库端已按 bm25 排好：越靠前越相关（score 越负）
    results = [{"title": f"t{n}", "path": f"p{n}", "ext": "pdf", "score": -float(10 - n), "snippet": ""} for n in range(10)]
    one = {"lib": {"name": "甲", "url": "http://a/", "doc_count": 10}, "ok": True, "error": "", "results": results}
    merged = q.merge([one], take=5)
    assert [row["title"] for row in merged] == ["t0", "t1", "t2", "t3", "t4"]


def test_search_all_survives_dead_library(q):
    libs = [{"name": "死库", "url": "http://127.0.0.1:1/", "online": True, "doc_count": 1}]
    out = q.search_all(libs, "乐平")
    assert len(out) == 1 and out[0]["ok"] is False and out[0]["error"]


def test_fetch_libraries_falls_back_to_cache_with_health_check(q, monkeypatch, tmp_path):
    cache = tmp_path / "home" / "cache_libraries.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps({"libraries": [{"name": "甲", "url": "http://a:8768/", "online": True, "doc_count": 3}]}),
        encoding="utf-8",
    )

    def boom(url, timeout=3.0):
        raise OSError("center down")

    monkeypatch.setattr(q, "_get_json", boom)
    monkeypatch.setattr(q, "_health_ok", lambda url, timeout=1.5: True)
    libraries, source = q.fetch_libraries()
    assert source == "cache+health"
    assert libraries[0]["online"] is True


def test_fetch_libraries_probes_downed_center_in_parallel(q, monkeypatch, tmp_path):
    """中心挂了退回缓存时，逐库探活要并发——串行 20 个库就要等 30 秒。"""
    libs = [{"name": f"库{i}", "url": f"http://h{i}:8768/", "online": True} for i in range(6)]
    cache = tmp_path / "home" / "cache_libraries.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"libraries": libs}), encoding="utf-8")

    def boom(url, timeout=3.0):
        raise OSError("center down")

    state = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def slow_ok(url, timeout=1.5):
        with guard:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with guard:
            state["now"] -= 1
        return "ok" in url

    monkeypatch.setattr(q, "_get_json", boom)
    monkeypatch.setattr(q, "_health_ok", slow_ok)
    libraries, source = q.fetch_libraries()
    assert source == "cache+health"
    assert state["peak"] > 1, "探活还是串行的"
    # 顺序不能变（缓存里的登记顺序），在线状态按各自结果写回
    assert [lib["name"] for lib in libraries] == [lib["name"] for lib in libs]
    assert all(lib["online"] is False for lib in libraries)


def test_fetch_libraries_passes_name_and_separates_cache(q, monkeypatch, tmp_path):
    calls: list[str] = []

    def fake(url, timeout=3.0):
        calls.append(url)
        return {"libraries": [], "latest_version": "0.2.0"}

    monkeypatch.setattr(q, "_get_json", fake)
    libraries, source = q.fetch_libraries("赵六")
    assert (libraries, source) == ([], "registry")
    assert f"name={urllib.parse.quote('赵六')}" in calls[0]
    assert list((tmp_path / "home").glob("cache_libraries_*.json"))
    # 匿名查询写自己的缓存，不会蹭到姓名缓存
    q.fetch_libraries()
    assert (tmp_path / "home" / "cache_libraries.json").exists()


def test_cmd_libs_sends_identity(q, monkeypatch, tmp_path):
    (tmp_path / "home").mkdir(parents=True)
    (tmp_path / "home" / "identity.txt").write_text("赵六", encoding="utf-8")
    urls: list[str] = []

    def fake(url, timeout=3.0):
        urls.append(url)
        return {"libraries": []}

    monkeypatch.setattr(q, "_get_json", fake)
    assert q.cmd_libs(type("A", (), {"json": False})()) == 3
    assert f"name={urllib.parse.quote('赵六')}" in urls[0]


def test_fetch_libraries_reports_none_without_cache(q, monkeypatch):
    def boom(url, timeout=3.0):
        raise OSError("center down")

    monkeypatch.setattr(q, "_get_json", boom)
    libraries, source = q.fetch_libraries()
    assert libraries == [] and source == "none"
    assert q.cmd_libs(type("A", (), {"json": False})()) == 3


def test_registry_url_env_wins(q, monkeypatch):
    monkeypatch.setenv("LANLIB_REGISTRY", "http://10.0.0.9:8767/")
    assert q.registry_url() == "http://10.0.0.9:8767"


def test_upgrade_note_names_stale_libraries(q):
    q._LATEST["version"] = "0.2.0"
    libs = [
        {"name": "赵六", "version": "0.1.0"},
        {"name": "老王", "version": "0.2.0"},
        {"name": "小李", "version": ""},  # 老版本没上报，不判定
    ]
    note = q.upgrade_note(libs)
    assert "1 个库版本落后" in note and "赵六" in note and "老王" not in note

    q._LATEST["version"] = "0.2.0"
    assert q.upgrade_note([{"name": "老王", "version": "0.2.0"}]) == ""


def test_script_dir_config_is_used_for_distribution(q, monkeypatch, tmp_path):
    """分发包里 scripts/config.json 写了中心地址，用户不用手工配。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "config.json").write_text(json.dumps({"registry": "http://172.16.1.197:8767"}), encoding="utf-8")
    monkeypatch.setattr(q, "script_dir", lambda: scripts)
    assert q.load_config()["registry"] == "http://172.16.1.197:8767"
    assert q.registry_url() == "http://172.16.1.197:8767"
