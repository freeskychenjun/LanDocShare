"""登记中心：心跳登记、在线/失效 TTL、来源网段限制、落盘恢复。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from landoc.registry import RegistryStore, make_registry_server


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
        {"name": "陈鋆", "lib_name": "陈鋆的资料库", "port": 8768, "doc_count": 13, "version": "0.1.0"},
    )
    assert code == 200 and body["ok"] is True
    assert body["latest_version"] == "0.1.0"
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
    post_json(f"{base}/register", {"name": "陈鋆", "port": 8768, "doc_count": 13})
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


def test_sort_puts_online_first(tmp_path):
    store = RegistryStore(tmp_path / "registry.json")
    store.register("10.91.1.60", {"name": "B 库", "port": 8768})
    stale = store.register("10.91.1.50", {"name": "A 库", "port": 8768})
    store._entries[stale["key"]]["last_seen"] = time.time() - 600
    names = [lib["name"] for lib in store.libraries()]
    assert names == ["B 库", "A 库"]
