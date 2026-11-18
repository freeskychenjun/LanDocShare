"""登记中心：心跳登记、在线/失效 TTL、来源网段限制、落盘恢复。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from landoc import __version__
from landoc.registry import RegistryStore, load_manifest, make_registry_server


@pytest.fixture()
def center(tmp_path):
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(store, port=0, allow_cidrs=["127.0.0.1"])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, store
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_register_then_list(center):
    base, _store = center
    code, body = post_json(
        f"{base}/register",
        {"name": "陈鋆", "lib_name": "陈鋆的资料库", "port": 8768, "doc_count": 13, "version": __version__,
         "allow_cidrs": ["127.0.0.1"]},
    )
    assert code == 200 and body["ok"] is True
    assert body["latest_version"] == __version__
    assert body["upgrade"] is False

    code, libs = get_json(f"{base}/libraries")
    assert code == 200
    assert libs["count"] == 1 and libs["online"] == 1
    entry = libs["libraries"][0]
    assert entry["name"] == "陈鋆"
    assert entry["ip"] == "127.0.0.1"  # 取连接对端，不信自报
    assert entry["port"] == 8768
    assert entry["doc_count"] == 13
    assert entry["online"] is True
    assert entry["url"] == "http://127.0.0.1:8768/"


def test_register_reports_upgrade_for_old_version(center):
    base, _store = center
    _code, body = post_json(f"{base}/register", {"name": "老王", "port": 8766, "version": "0.0.9"})
    assert body["upgrade"] is True


def test_health_and_info(center):
    base, _store = center
    code, health = get_json(f"{base}/health")
    assert code == 200 and health["status"] == "online" and health["service"] == "landoc-registry"
    code, info = get_json(f"{base}/api/info")
    assert code == 200 and "POST /register" in info["usage"] and info["skill_available"] is True


def get_text(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def get_bytes(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


def test_landing_page_is_html_with_download_links(center):
    base, _store = center
    post_json(f"{base}/register", {"name": "陈鋆", "port": 8768, "doc_count": 13, "allow_cidrs": ["127.0.0.1"]})
    code, page = get_text(f"{base}/")
    assert code == 200
    assert page.startswith("<!doctype html>") and "陈鋆" in page
    assert "/skill.zip" in page
    # 没配库软件包时给出"还没放好"的说明，而不是死链
    assert "库软件包暂未放好" in page and "/download" not in page


def test_landing_page_links_download_when_package_ready(tmp_path):
    package = tmp_path / "LanDocShare.zip"
    package.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(store, port=0, allow_cidrs=["127.0.0.1"], package_zip=package)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        _code, page = get_text(f"{base}/")
        assert "/download" in page
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_skill_zip_embeds_registry_address(center):
    import io
    import zipfile

    base, _store = center
    code, blob = get_bytes(f"{base}/skill.zip")
    assert code == 200
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        assert "lan-lib-search/SKILL.md" in names
        assert "lan-lib-search/scripts/query.py" in names
        config = json.loads(zf.read("lan-lib-search/scripts/config.json"))
    assert config["registry"] == base  # 请求 Host 推断出来的中心地址


def test_download_404_without_package(center):
    base, _store = center
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get_bytes(f"{base}/download")
    assert excinfo.value.code == 404


def test_download_streams_package_zip(tmp_path):
    package = tmp_path / "LanDocShare.zip"
    package.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # 空 zip 的签名，够验流式发送
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(store, port=0, allow_cidrs=["127.0.0.1"], package_zip=package)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/download", timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/zip"
            assert "LanDocShare.zip" in resp.headers["Content-Disposition"]
            assert resp.read() == package.read_bytes()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_advertise_overrides_request_host(tmp_path):
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(store, port=0, allow_cidrs=["127.0.0.1"], advertise="http://10.91.1.197:8767")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        _code, page = get_text(f"{base}/")
        assert "http://10.91.1.197:8767" in page
        import io
        import zipfile

        _code, blob = get_bytes(f"{base}/skill.zip")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            config = json.loads(zf.read("lan-lib-search/scripts/config.json"))
        assert config["registry"] == "http://10.91.1.197:8767"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_libraries_ttl_marks_offline_then_forgets(tmp_path):
    store = RegistryStore(tmp_path / "registry.json")
    entry = store.register("10.91.1.50", {"name": "小李", "port": 8768, "doc_count": 2})
    assert store.libraries()[0]["online"] is True

    store._entries[entry["key"]]["last_seen"] = time.time() - 600  # 10 分钟没心跳
    libs = store.libraries()
    assert len(libs) == 1 and libs[0]["online"] is False

    store._entries[entry["key"]]["last_seen"] = time.time() - 2000  # 超过 30 分钟
    assert store.libraries() == []


def test_registry_persists_and_reloads(tmp_path):
    path = tmp_path / "registry.json"
    store = RegistryStore(path)
    store.register("10.91.1.50", {"name": "小李", "port": 8768, "doc_count": 2})
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["libraries"][0]["name"] == "小李"

    again = RegistryStore(path)
    libs = again.libraries()
    assert len(libs) == 1 and libs[0]["name"] == "小李"


def test_rejects_sources_outside_allowed_cidrs(tmp_path):
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(store, port=0, allow_cidrs=["10.91.0.0/16"])  # 不含回环
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            get_json(f"{base}/libraries")
        assert excinfo.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            post_json(f"{base}/register", {"name": "陌生人", "port": 8768})
        assert excinfo.value.code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_rejects_bad_port(center):
    base, _store = center
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post_json(f"{base}/register", {"name": "老王", "port": 0})
    assert excinfo.value.code == 400


def register(base: str, **payload):
    """心跳注册的默认载荷：显式带上授权范围，否则中心会按"旧版"藏起来。"""
    body = {"name": "测试库", "port": 8768, **payload}
    code, reply = post_json(f"{base}/register", body)
    assert code == 200
    return reply


def test_libraries_filtered_by_cidr_and_legacy_hidden(center):
    base, _store = center
    register(base, name="开给本机", port=8768, allow_cidrs=["127.0.0.1"])
    register(base, name="开给别处", port=8769, allow_cidrs=["10.91.0.0/16"])
    register(base, name="旧版没上报", port=8770)
    register(base, name="暂停共享", port=8771, allow_cidrs=[])

    code, libs = get_json(f"{base}/libraries")
    assert code == 200
    assert libs["count"] == 1
    assert [lib["name"] for lib in libs["libraries"]] == ["开给本机"]
    # 授权名单只给中心判断用，不回传给客户端
    assert "allow_cidrs" not in libs["libraries"][0]
    assert "allow_people" not in libs["libraries"][0]


def test_health_info_and_landing_only_count_visible(center):
    base, _store = center
    register(base, name="可见库", port=8768, allow_cidrs=["127.0.0.1"])
    register(base, name="隐藏库", port=8769, allow_cidrs=["10.91.0.0/16"])

    _code, health = get_json(f"{base}/health")
    assert health["libraries"] == 1
    _code, info = get_json(f"{base}/api/info")
    assert info["libraries"] == 1
    _code, page = get_text(f"{base}/")
    assert "可见库" in page and "隐藏库" not in page


def test_landing_page_has_name_form(center):
    base, _store = center
    name = urllib.parse.quote("陈鋆")
    _code, page = get_text(f"{base}/?name={name}")
    assert "name='name'" in page
    assert "value='陈鋆'" in page


def test_store_audience_by_person_and_ip(tmp_path):
    store = RegistryStore(tmp_path / "registry.json")
    store.register(
        "10.91.1.50",
        {
            "name": "陈鋆",
            "port": 8768,
            "allow_cidrs": ["10.91.0.0/16"],
            "allow_people": {"陈鋆": "10.91.1.50"},
        },
    )
    visible = [lib["name"] for lib in store.libraries(viewer_ip="10.91.1.50", viewer_name="陈鋆")]
    assert visible == ["陈鋆"]
    assert store.libraries(viewer_ip="10.91.1.51", viewer_name="陈鋆") == []  # 姓名对但 IP 不对
    assert store.libraries(viewer_ip="10.91.1.50", viewer_name="王五") == []  # 姓名不在名单
    assert [lib["name"] for lib in store.libraries(viewer_ip="10.91.1.50")] == ["陈鋆"]  # 匿名按 IP
    assert store.libraries(viewer_ip="10.91.1.51") == []


@pytest.mark.parametrize(
    "extra",
    [
        {"allow_cidrs": "10.91.0.0/16"},
        {"allow_cidrs": ["not-a-cidr"]},
        {"allow_people": ["陈鋆"]},
    ],
)
def test_rejects_bad_audience(center, extra):
    base, _store = center
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post_json(f"{base}/register", {"name": "坏数据", "port": 8768, **extra})
    assert excinfo.value.code == 400


def test_sort_puts_online_first(tmp_path):
    store = RegistryStore(tmp_path / "registry.json")
    store.register("10.91.1.60", {"name": "B 库", "port": 8768})
    stale = store.register("10.91.1.50", {"name": "A 库", "port": 8768})
    store._entries[stale["key"]]["last_seen"] = time.time() - 600
    names = [lib["name"] for lib in store.libraries()]
    assert names == ["B 库", "A 库"]


# --------------------------------------------------------------------------- #
# 版本清单：自动更新的中心侧
# --------------------------------------------------------------------------- #
def _make_release(tmp_path, version="0.3.0", *, min_from="", notes=None) -> tuple[Path, Path]:
    """造一个真的分发目录：zip + latest.json（sha256/size 都按真实文件算）。"""
    import hashlib
    import zipfile

    out = tmp_path / "distribution"
    out.mkdir()
    zip_path = out / f"LanDocShare-{version}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("LanDocShare/LanDocShare.exe", b"MZ-fake")
        archive.writestr("LanDocShare/_internal/data.bin", b"x" * 1024)
    blob = zip_path.read_bytes()
    manifest = {
        "version": version,
        "platform": "windows-x64",
        "file": zip_path.name,
        "size": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "released_at": "2026-11-18 10:00",
        "notes": notes if notes is not None else ["修了检索偶发 500"],
        "min_from": min_from,
        "mandatory": False,
    }
    manifest_path = out / "latest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return zip_path, manifest_path


def _serve(tmp_path, **kwargs):
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(store, port=0, allow_cidrs=["127.0.0.1"], **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd, thread


def test_load_manifest_normalizes_and_caches(tmp_path):
    zip_path, manifest_path = _make_release(tmp_path)
    manifest = load_manifest(manifest_path)
    assert manifest is not None
    assert manifest["version"] == "0.3.0"
    assert manifest["size"] == zip_path.stat().st_size
    assert manifest["notes"] == ["修了检索偶发 500"]

    # 手写清单省掉 sha256 时中心自己算
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.pop("sha256")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    again = load_manifest(manifest_path)
    assert again is not None and len(again["sha256"]) == 64


def test_load_manifest_tolerates_utf8_bom(tmp_path):
    """用记事本改过 latest.json 会带 BOM：不该因此变成"没发布"。"""
    from landoc.registry import load_manifest

    _make_release(tmp_path, version="0.3.0")
    path = tmp_path / "distribution" / "latest.json"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    manifest = load_manifest(path)
    assert manifest is not None and manifest["version"] == "0.3.0"


def test_load_manifest_returns_none_when_broken(tmp_path):
    zip_path, manifest_path = _make_release(tmp_path)
    manifest_path.write_text("{not json", encoding="utf-8")
    assert load_manifest(manifest_path) is None
    manifest_path.write_text(json.dumps({"version": "abc"}), encoding="utf-8")
    assert load_manifest(manifest_path) is None
    zip_path.unlink()  # 清单指着包、包没了
    manifest_path.write_text(
        json.dumps({"version": "0.3.0", "file": zip_path.name}), encoding="utf-8"
    )
    assert load_manifest(manifest_path) is None
    assert load_manifest(None) is None


def test_api_update_reports_new_release(tmp_path):
    zip_path, manifest_path = _make_release(tmp_path)
    base, httpd, thread = _serve(tmp_path, manifest=manifest_path)
    try:
        code, payload = get_json(f"{base}/api/update?version=0.2.0&platform=windows-x64")
        assert code == 200
        assert payload["latest"] == "0.3.0" and payload["update"] is True
        assert payload["download"] == f"{base}/download/0.3.0"
        assert payload["sha256"] == load_manifest(manifest_path)["sha256"]
        assert payload["size"] == zip_path.stat().st_size
        assert payload["notes"] == ["修了检索偶发 500"]

        # 已是最新：update=False，客户端不该再提示
        _code, same = get_json(f"{base}/api/update?version=0.3.0")
        assert same["update"] is False and same["latest"] == "0.3.0"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_api_update_manual_only_when_too_old(tmp_path):
    _zip, manifest_path = _make_release(tmp_path, min_from="0.2.5")
    base, httpd, thread = _serve(tmp_path, manifest=manifest_path)
    try:
        _code, payload = get_json(f"{base}/api/update?version=0.1.0")
        assert payload["update"] is False and payload["manual_only"] is True
        assert "手动" in payload["hint"] or "重新下载" in payload["hint"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_api_update_404_without_manifest(center):
    base, _store = center
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get_json(f"{base}/api/update?version=0.1.0")
    assert excinfo.value.code == 404


def test_download_supports_range_and_sha_header(tmp_path):
    zip_path, manifest_path = _make_release(tmp_path)
    base, httpd, thread = _serve(tmp_path, manifest=manifest_path)
    try:
        blob = zip_path.read_bytes()
        start = 5
        request = urllib.request.Request(
            f"{base}/download/0.3.0", headers={"Range": f"bytes={start}-"}
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            assert resp.status == 206
            assert resp.headers["Content-Range"] == f"bytes {start}-{len(blob) - 1}/{len(blob)}"
            assert resp.headers["X-Landoc-Sha256"] == load_manifest(manifest_path)["sha256"]
            assert resp.read() == blob[start:]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_download_rejects_non_latest_version(tmp_path):
    _zip, manifest_path = _make_release(tmp_path)
    base, httpd, thread = _serve(tmp_path, manifest=manifest_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            get_bytes(f"{base}/download/0.1.0")
        assert excinfo.value.code == 404
        assert json.loads(excinfo.value.read().decode("utf-8"))["error"] == "version not available"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_libraries_report_manifest_version_not_process_version(tmp_path):
    _zip, manifest_path = _make_release(tmp_path, version="9.9.9")
    base, httpd, thread = _serve(tmp_path, manifest=manifest_path)
    try:
        _code, libs = get_json(f"{base}/libraries")
        assert libs["latest_version"] == "9.9.9"
        _code, body = post_json(f"{base}/register", {"name": "老王", "port": 8766, "version": __version__})
        assert body["upgrade"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


# ---------------------------------------------------------------- 优雅停止与自启参数


def test_stop_flag_only_leaves_a_request(tmp_path):
    from landoc import registry

    assert registry.main(["--stop", "--state-dir", str(tmp_path)]) == 0
    assert registry.stop_path(tmp_path).is_file()  # 没绑端口、没起服务，只是留记号


def test_stop_request_shuts_the_server_down(tmp_path, monkeypatch):
    from landoc import registry

    monkeypatch.setattr(registry, "STOP_POLL_SECONDS", 0.05)
    store = RegistryStore(tmp_path / "registry.json")
    httpd = make_registry_server(store, port=0, allow_cidrs=["127.0.0.1"])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        registry._start_stop_watcher(httpd, tmp_path)
        assert get_json(f"{base}/health")[0] == 200
        path = registry.request_stop(tmp_path)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not path.exists()  # 记号被消费掉，不会误杀下次启动
    finally:
        httpd.server_close()


def test_autostart_args_skip_defaults_and_resolve_paths(tmp_path):
    import argparse

    from landoc.registry import (
        DEFAULT_CIDRS,
        WATCHDOG_DEFAULT_FAILURES,
        WATCHDOG_DEFAULT_INTERVAL,
        _autostart_args,
    )

    bare = argparse.Namespace(
        state_dir=str(Path.home() / ".landoc-registry"),
        allow_cidrs=",".join(DEFAULT_CIDRS),
        package_zip=None,
        manifest=None,
        advertise="",
        roster=None,
        watchdog_log=None,
        watchdog_interval=WATCHDOG_DEFAULT_INTERVAL,
        watchdog_failures=WATCHDOG_DEFAULT_FAILURES,
    )
    assert _autostart_args(bare) == []

    custom = argparse.Namespace(
        state_dir=str(tmp_path),
        allow_cidrs="10.0.0.0/8",
        package_zip="distribution/LanDocShare.zip",
        manifest=None,
        advertise="http://10.91.1.197:8767",
        roster="roster.csv",
        watchdog_log="logs/registry.log",
        watchdog_interval=60.0,
        watchdog_failures=5,
    )
    args = _autostart_args(custom)
    assert args[:2] == ["--state-dir", str(tmp_path.resolve())]
    assert args[2:4] == ["--allow-cidrs", "10.0.0.0/8"]
    assert args[4:6] == ["--package-zip", str(Path("distribution/LanDocShare.zip").resolve())]
    assert args[6:8] == ["--advertise", "http://10.91.1.197:8767"]
    assert args[8:10] == ["--roster", str(Path("roster.csv").resolve())]
    # 看门狗自己的参数也要活过重启：否则开机后日志写到别处、探测间隔也回默认
    assert args[10:12] == ["--watchdog-log", str(Path("logs/registry.log").resolve())]
    assert args[12:14] == ["--watchdog-interval", "60"]
    assert args[14:16] == ["--watchdog-failures", "5"]
