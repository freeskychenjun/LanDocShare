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


def test_health_and_root(center):
    base, _store = center
    code, health = get_json(f"{base}/health")
    assert code == 200 and health["status"] == "online" and health["service"] == "landoc-registry"
    code, root = get_json(f"{base}/")
    assert code == 200 and "POST /register" in root["usage"]


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
