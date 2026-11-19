"""可访问库清单：拉中心、缓存、探活、以及给窗口用的纯函数。"""

from __future__ import annotations

import json
import threading
import time

import pytest

from landoc import discover
from landoc.config import Config
from landoc.registry import RegistryStore, make_registry_server


class Center:
    def __init__(self, tmp_path):
        self.store = RegistryStore(tmp_path / "registry.json")
        self.httpd = make_registry_server(self.store, port=0, allow_cidrs=["127.0.0.1"])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


@pytest.fixture()
def center(tmp_path):
    running = Center(tmp_path)
    try:
        yield running
    finally:
        running.stop()


def add_library(store: RegistryStore, **overrides):
    payload = {
        "name": "陈鋆",
        "lib_name": "陈鋆的资料库",
        "port": 8768,
        "doc_count": 13,
        "version": "0.2.6",
        "allow_cidrs": ["127.0.0.1"],
    }
    payload.update(overrides)
    store.register("127.0.0.1", payload)


def test_fetch_from_registry_keeps_center_fields(center, tmp_path):
    add_library(center.store)
    cache = tmp_path / "cache.json"

    result = discover.fetch_libraries(center.base, cache_file=cache)

    assert result.source == "registry"
    assert len(result) == 1 and result.online_count == 1
    lib = result.libraries[0]
    assert (lib.name, lib.lib_name, lib.doc_count, lib.online) == (
        "陈鋆",
        "陈鋆的资料库",
        13,
        True,
    )
    assert lib.url == "http://127.0.0.1:8768/"
    assert result.latest_version
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["registry"] == center.base
    assert written["libraries"][0]["lib_name"] == "陈鋆的资料库"


def test_fetch_leaves_an_offline_list_next_to_the_cache(center, tmp_path):
    add_library(center.store)
    cache = tmp_path / "cache.json"

    discover.fetch_libraries(center.base, cache_file=cache)

    backup = tmp_path / discover.OFFLINE_NAME
    page = backup.read_text(encoding="utf-8")
    assert "陈鋆的资料库" in page and "http://127.0.0.1:8768/" in page
    assert "本机缓存" not in page  # 这是刚查到的实时名单，不是旧缓存


def test_failed_offline_backup_never_breaks_the_query(center, tmp_path, monkeypatch):
    add_library(center.store)

    def explode(*_args, **_kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(discover, "export_offline", explode)

    result = discover.fetch_libraries(center.base, cache_file=tmp_path / "cache.json")

    assert result.source == "registry"
    assert [lib.lib_name for lib in result] == ["陈鋆的资料库"]


def test_cache_is_per_viewer(center, tmp_path, monkeypatch):
    # 姓名不再能单独授权；这里只用一条登记了 IP 的条目验证缓存按查看者隔离
    add_library(center.store, allow_people={"老王": "10.91.1.50"})
    cache = tmp_path / "cache.json"

    seen = discover.fetch_libraries(center.base, "老王", cache_file=cache)
    assert [lib.lib_name for lib in seen] == ["陈鋆的资料库"]

    center.stop()

    # 同一个缓存文件，换个姓名不能复用（否则等于绕过中心的名单）
    missing = discover.fetch_libraries(center.base, "张三", cache_file=cache, timeout=0.5)
    assert missing.source == "none"
    assert missing.libraries == ()
    assert "连不上" in missing.error

    # 姓名对得上才允许退回缓存
    monkeypatch.setattr(discover, "_health_ok", lambda url, timeout=0: True)
    cached = discover.fetch_libraries(center.base, "老王", cache_file=cache, timeout=0.5)
    assert cached.source == "cache"
    assert cached.libraries[0].online is True


def test_falls_back_to_cache_and_probes(center, tmp_path, monkeypatch):
    add_library(center.store)
    cache = tmp_path / "cache.json"
    first = discover.fetch_libraries(center.base, cache_file=cache, now=1000.0)
    center.stop()

    monkeypatch.setattr(discover, "_health_ok", lambda url, timeout=0: True)
    cached = discover.fetch_libraries(
        center.base, cache_file=cache, now=first.fetched_at + 120, timeout=0.5
    )

    assert cached.source == "cache"
    assert cached.cached_at == pytest.approx(1000.0)
    assert cached.online_count == 1
    assert cached.libraries[0].age_seconds == pytest.approx(120.0, abs=1.0)
    assert "连不上" in cached.error

    monkeypatch.setattr(discover, "_health_ok", lambda url, timeout=0: False)
    offline = discover.fetch_libraries(
        center.base, cache_file=cache, now=first.fetched_at + 180, timeout=0.5
    )
    assert offline.source == "cache"
    assert offline.online_count == 0
    assert "已离线" in discover.rows(offline)[0].status


def test_expired_cache_is_not_used(center, tmp_path):
    add_library(center.store)
    cache = tmp_path / "cache.json"
    discover.fetch_libraries(center.base, cache_file=cache)
    center.stop()

    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["saved_at"] = time.time() - discover.CACHE_TTL - 60
    cache.write_text(json.dumps(payload), encoding="utf-8")

    result = discover.fetch_libraries(center.base, cache_file=cache, timeout=0.5)

    assert result.source == "none"
    assert "24 小时" in result.error


def test_unreachable_without_cache_explains(tmp_path):
    result = discover.fetch_libraries(
        "http://127.0.0.1:1", cache_file=tmp_path / "nothing.json", timeout=0.5
    )
    assert result.source == "none"
    assert "连不上" in result.error
    assert discover.rows(result) == ()


def test_empty_registry_url_tells_user_what_to_fill():
    result = discover.fetch_libraries("", "")
    assert result.source == "none"
    assert "registry_url" in result.error
    assert "登记中心" in discover.summary_text(result)


def test_403_hint_suggests_lan_address(tmp_path):
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(store, port=0, allow_cidrs=["10.91.0.0/16"])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        result = discover.fetch_libraries(
            base, cache_file=tmp_path / "cache.json", timeout=2.0
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)

    assert result.source == "none"
    assert "403" in result.error
    assert "127.0.0.1" in result.error


def test_rows_sort_online_first_and_mark_self():
    offline = discover.VisibleLibrary(
        key="b",
        name="老王",
        ip="10.91.1.50",
        port=8769,
        lib_name="老王的资料库",
        doc_count=3,
        version="0.2.0",
        last_seen="",
        age_seconds=7200,
        online=False,
        url="http://10.91.1.50:8769/",
    )
    online = discover.VisibleLibrary(
        key="a",
        name="陈鋆",
        ip="10.91.1.197",
        port=8768,
        lib_name="陈鋆的资料库",
        doc_count=13,
        version="0.2.6",
        last_seen="",
        age_seconds=42,
        online=True,
        url="http://10.91.1.197:8768/",
    )
    result = discover.LibraryList(
        (offline, online), "registry", "陈鋆", "0.2.6", "", 1000.0, 1000.0
    )

    rows = discover.rows(result, self_name="陈鋆的资料库")
    assert [row.lib_name for row in rows] == ["陈鋆的资料库", "老王的资料库"]
    assert rows[0].note == "（本机）" and rows[0].heartbeat == "心跳 42 秒前"
    assert rows[1].status == "已离线" and rows[1].note == ""
    assert "2 个库，1 个在线" in discover.summary_text(result)


def test_upgrade_note_names_stale_libraries():
    old = discover.VisibleLibrary(
        key="a",
        name="老王",
        ip="10.91.1.50",
        port=8769,
        lib_name="老王的资料库",
        doc_count=3,
        version="0.2.0",
        last_seen="",
        age_seconds=10,
        online=True,
        url="http://10.91.1.50:8769/",
    )
    result = discover.LibraryList((old,), "registry", "", "0.2.6", "", 0.0, 0.0)
    note = discover.upgrade_note(result)
    assert "老王的资料库" in note and "0.2.6" not in note


def test_empty_hint_explains_ip_only_auth():
    result = discover.LibraryList((), "registry", "", "", "", 0.0, 0.0)
    hint = discover.empty_hint(result)
    assert "按来源 IP" in hint and "不影响授权" in hint
    assert (
        discover.empty_hint(discover.LibraryList((), "none", "", "", "连不上", 0.0, 0.0))
        == "连不上"
    )


def test_viewer_name_roundtrip_and_skill_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("LANLIB_HOME", raising=False)

    assert discover.load_viewer_name() == ""
    discover.save_viewer_name("陈鋆")
    assert discover.load_viewer_name() == "陈鋆"

    discover.viewer_name_path().unlink()
    skill = tmp_path / "lan-lib-search"
    skill.mkdir()
    (skill / "identity.txt").write_text("老王\n", encoding="utf-8")
    assert discover.load_viewer_name() == "老王"


def test_window_starts_one_daemon_thread(tmp_path, monkeypatch):
    from landoc import discover_ui

    started = threading.Event()

    def fake_run(self):
        started.set()
        time.sleep(0.05)

    monkeypatch.setattr(discover_ui.DiscoverWindow, "_run", fake_run)
    window = discover_ui.DiscoverWindow(Config(name="库", lib_dir=tmp_path))

    window.show()

    assert started.wait(2.0)
    thread = window._thread
    assert thread is not None
    assert thread.name == "landoc-discover"
    assert thread.daemon is True


# ---------------------------------------------------------------- 离线地址清单


def _lib(**overrides) -> discover.VisibleLibrary:
    payload = dict(
        key="10.91.1.197:8768",
        name="陈鋆",
        ip="10.91.1.197",
        port=8768,
        lib_name="陈鋆的资料库",
        doc_count=13,
        version="0.2.6",
        last_seen="",
        age_seconds=42.0,
        online=True,
        url="http://10.91.1.197:8768/",
    )
    payload.update(overrides)
    return discover.VisibleLibrary(**payload)


def test_render_offline_html_lists_addresses_and_marks_offline():
    result = discover.LibraryList(
        (_lib(), _lib(key="g", name="老王", ip="10.91.1.50", port=8769,
                      lib_name="老王的资料库", doc_count=3, online=False, url="http://10.91.1.50:8769/")),
        "cache",
        "陈鋆",
        "",
        "连不上登记中心（timed out）",
        2000.0,
        1000.0,
    )
    page = discover.render_offline_html(result, generated_at=2000.0)

    assert page.startswith("<!doctype html>")
    assert "http://10.91.1.197:8768/" in page and "http://10.91.1.50:8769/" in page
    assert 'class="offline"' in page  # 已离线那行整行灰掉
    assert "陈鋆" in page and "老王的资料库" in page
    assert "连不上登记中心" in page
    assert "本机缓存" in page  # 来源写清，别让人以为是实时名单


def test_render_offline_html_escapes_and_drops_unsafe_links():
    result = discover.LibraryList(
        (_lib(name='<img src=x onerror="alert(1)">', url="javascript:alert(1)"),),
        "registry",
        "",
        "",
        "",
        0.0,
        0.0,
    )
    page = discover.render_offline_html(result, generated_at=0.0)

    assert "<img src=x" not in page  # 库名/姓名是外部数据，必须转义
    assert "&lt;img" in page
    assert 'href="javascript:' not in page


def test_render_offline_html_says_what_to_do_when_empty():
    page = discover.render_offline_html(
        discover.LibraryList((), "none", "", "", "连不上登记中心", 0.0, 0.0), generated_at=0.0
    )
    assert "没有取到任何库的地址" in page


def test_export_offline_writes_default_path_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    result = discover.LibraryList((_lib(),), "registry", "", "", "", 0.0, 0.0)

    path = discover.export_offline(result, generated_at=2000.0)

    assert path == tmp_path / "LanDocShare" / discover.OFFLINE_NAME
    assert "<!doctype html>" in path.read_text(encoding="utf-8")
    assert not list(path.parent.glob("*.tmp"))  # 原子写不留半截文件

    custom = tmp_path / "发给同事" / "库地址清单.html"
    assert discover.export_offline(result, custom, generated_at=2000.0).read_text(
        encoding="utf-8"
    ) == path.read_text(encoding="utf-8")
