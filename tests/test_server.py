from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from landoc.config import Config
from landoc.index import Index
from landoc.server import make_server


class Client:
    def __init__(self, base: str):
        self.base = base

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def json(self, path: str) -> dict:
        status, body = self.get(path)
        assert status == 200, f"{status}: {body}"
        return json.loads(body)


def gbk_urlencode(text: str) -> str:
    return urllib.parse.quote(text.encode("gbk"))


@pytest.fixture
def lib(tmp_path: Path):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    # 测试从 127.0.0.1 连入，故把本机回环列为允许网段（默认是 10.91.0.0/16）
    cfg = Config(name="测试库", lib_dir=lib_dir, allow_cidrs=["127.0.0.1"])
    index = Index(cfg.db_path, md_root=lib_dir / "md")
    index.upsert("成果/乐平.md", "乐平下闸蓄水", "docx", "/secret/a.docx", 10, 1.0, "马斯京根法河道洪水演算")
    index.upsert("成果/溪霞.md", "溪霞水库", "pptx", "/secret/b.pptx", 10, 1.0, "溪霞水库除险加固")
    index.commit()
    return cfg, index


@pytest.fixture
def running(lib):
    cfg, index = lib
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield Client(f"http://127.0.0.1:{port}"), cfg, index
    httpd.shutdown()
    httpd.server_close()


def test_health_returns_count_and_version(running):
    client, _, _ = running
    payload = client.json("/api/health")
    assert payload["status"] == "online"
    assert payload["doc_count"] == 2
    assert payload["version"]


def test_meta_exposes_scope_not_paths(running):
    client, _, _ = running
    payload = client.json("/api/meta")
    assert payload["allow_cidrs"] == ["127.0.0.1"]
    assert "source_path" not in json.dumps(payload)


def test_root_info_page(running):
    client, _, _ = running
    payload = client.json("/")
    assert payload["service"] == "landoc"
    assert "/api/search" in payload["endpoints"]


def test_search_returns_snippets(running):
    client, _, _ = running
    status, body = client.get("/api/search?q=" + urllib.parse.quote("马斯京根"))
    assert status == 200
    payload = json.loads(body)
    assert payload["count"] == 1
    assert "<<马斯京根>>" in payload["results"][0]["snippet"]


def test_source_path_never_leaks(running):
    client, _, _ = running
    status, body = client.get("/api/search?q=" + urllib.parse.quote("马斯京根"))
    assert status == 200
    assert "/secret/" not in body
    assert "source_path" not in body


def test_gbk_encoded_query_is_decoded(running):
    client, _, _ = running
    status, body = client.get("/api/search?q=" + gbk_urlencode("马斯京根"))
    assert status == 200
    assert json.loads(body)["count"] == 1


def test_search_without_q_is_400(running):
    client, _, _ = running
    status, _ = client.get("/api/search")
    assert status == 400


def test_tree_and_list(running):
    client, _, _ = running
    assert client.json("/api/tree")["children"][0]["name"] == "成果"
    files = client.json("/api/list?path=" + urllib.parse.quote("成果"))["files"]
    assert {f["title"] for f in files} == {"乐平下闸蓄水", "溪霞水库"}


def test_tables_unknown_path_is_404(running):
    client, _, _ = running
    status, _ = client.get("/api/tables?path=" + urllib.parse.quote("../etc/passwd"))
    assert status == 404


def test_cidr_not_allowed_is_403(lib):
    cfg, index = lib
    cfg.allow_cidrs = ["192.0.2.0/24"]  # 不含 127.0.0.1
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        status, _ = client.get("/api/search?q=x")
        assert status == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unknown_name_is_401_when_allowlist_used(lib):
    cfg, index = lib
    cfg.allow_people = {"陈鋆": "127.0.0.1"}
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        assert client.get("/api/search?q=x&name=" + urllib.parse.quote("陌生人"))[0] == 401
        assert client.get("/api/search?q=x")[0] == 401
        assert client.get("/api/search?q=x&name=" + urllib.parse.quote("陈鋆"))[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_rate_limit_returns_429(lib, monkeypatch):
    cfg, index = lib
    httpd = make_server(cfg, index=index, port=0)
    httpd.RequestHandlerClass.rate = __import__("landoc.server", fromlist=["RateLimiter"]).RateLimiter(max_req=3)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        codes = [client.get("/api/stats")[0] for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[-1] == 429
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_audit_written_per_request(running):
    client, cfg, _ = running
    client.get("/api/stats")
    log = Path(cfg.lib_dir) / "logs" / "access_log.jsonl"
    assert log.is_file()
    assert '"/api/stats"' in log.read_text(encoding="utf-8").replace('"api": ', "")
