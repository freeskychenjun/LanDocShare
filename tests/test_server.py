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


def test_root_serves_search_page(running):
    """同事直接开浏览器就能搜：/ 必须是可用的单页，而不是 JSON。"""
    client, _, _ = running
    status, body = client.get("/")
    assert status == 200
    assert "<!doctype html>" in body.lower()
    assert "api/search" in body


def test_info_page_json_at_api_info(running):
    client, _, _ = running
    payload = client.json("/api/info")
    assert payload["service"] == "landoc"
    assert "/api/search" in payload["endpoints"]
    assert "/api/recent" in payload["endpoints"]


def test_recent_lists_newest_first(running):
    """5.1：检索页一打开靠它显示「最近收录」，不用先进目录。"""
    client, _, _ = running
    payload = client.json("/api/recent")
    assert payload["count"] == 2
    assert [f["path"] for f in payload["files"]] == ["成果/溪霞.md", "成果/乐平.md"]
    assert set(payload["files"][0]) == {"path", "title", "size"}


def test_recent_limit_is_clamped(running):
    client, _, _ = running
    assert client.json("/api/recent?limit=1")["count"] == 1
    assert client.json("/api/recent?limit=-5")["count"] == 1
    assert client.json("/api/recent?limit=abc")["count"] == 2  # 坏参数回落默认，不是 500


def test_root_page_renders_recent_and_loading_state(running):
    """5.2：页面要有最近收录入口，搜索时要有「搜索中…」而不是干等着。"""
    client, _, _ = running
    status, body = client.get("/")
    assert status == 200
    assert "api/recent" in body
    assert "最近收录" in body
    assert "搜索中…" in body


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


def test_authorize_person_prefers_ip_and_treats_name_as_label():
    from landoc.server import authorize_person

    roster = {"陈鋆": "10.91.1.197", "张三": "", "李四": "10.91.1.50"}
    # 来源 IP 在名单里：不报姓名也放行，审计记那条的姓名
    assert authorize_person(roster, "10.91.1.197", None) == ("陈鋆", None)
    assert authorize_person(roster, "10.91.1.197", "陈鋆") == ("陈鋆", None)
    # 冒名：姓名的登记 IP 与来源不一致
    assert authorize_person(roster, "10.91.1.197", "李四") == ("李四", "ip mismatch for name")
    # 不在名单里的人
    assert authorize_person(roster, "10.91.1.197", "王五") == ("王五", "name not registered")
    # IP 不在名单、也没报姓名
    assert authorize_person(roster, "10.91.1.99", None) == (None, "ip not registered")
    # 没登记 IP 的人：只能靠报姓名
    assert authorize_person(roster, "10.91.1.99", "张三") == ("张三", None)
    assert authorize_person(roster, "10.91.1.99", "陈鋆") == ("陈鋆", "ip mismatch for name")


def test_authorize_person_lets_loopback_through_and_empty_roster_is_open():
    from landoc.server import authorize_person

    assert authorize_person({"陈鋆": "10.91.1.197"}, "127.0.0.1", None) == ("本机", None)
    assert authorize_person({"陈鋆": "10.91.1.197"}, "::1", None) == ("本机", None)
    # 名单为空 = 只靠网段把关，报不报姓名都无所谓
    assert authorize_person({}, "10.91.1.197", None) == (None, None)
    assert authorize_person({}, "10.91.1.197", "陈鋆") == ("陈鋆", None)


def test_roster_is_live_and_loopback_owner_always_passes(lib):
    """名单里有 IP 条目时，127.0.0.1 自检不被挡（主人本机调试、状态窗口都走回环）。"""
    cfg, index = lib
    cfg.allow_people = {"陈鋆": "10.91.1.197"}
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        assert client.get("/api/search?q=x")[0] == 200
        # 名单改动即时生效（服务端每请求现读 cfg，不用重启）
        cfg.allow_people = {"陈鋆": "10.91.1.197", "张三": "10.91.1.50"}
        assert client.get("/api/meta")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_meta_says_whether_name_is_needed_without_leaking_roster(lib):
    cfg, index = lib
    cfg.allow_people = {"陈鋆": "127.0.0.1"}
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        status, raw = client.get("/api/meta")
        assert status == 200
        body = json.loads(raw)
        assert body["name_required"] is False  # 全是 IP 条目，不用报姓名
        assert "陈鋆" not in raw
    finally:
        httpd.shutdown()
        httpd.server_close()

    cfg.allow_people = {"陈鋆": "127.0.0.1", "张三": ""}
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        status, raw = client.get("/api/meta")
        assert status == 200
        assert json.loads(raw)["name_required"] is True  # 有人没登记 IP，需要报姓名
        assert "张三" not in raw
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


def test_rate_limiter_is_shared_with_registry():
    """库端与登记中心共用一份实现（原来两边各写一遍，改一处漏一处）。"""
    from landoc import ratelimit, server

    assert server.RateLimiter is ratelimit.RateLimiter
    limiter = ratelimit.RateLimiter(max_req=2, window=60.0)
    assert limiter.allow("10.0.0.1") is True
    assert limiter.allow("10.0.0.1") is True
    assert limiter.allow("10.0.0.1") is False  # 超限的请求不再占窗口
    assert limiter.allow("10.0.0.2") is True  # 别的来源不受牵连
    limiter.reset()
    assert limiter.allow("10.0.0.1") is True


def test_audit_rotates_instead_of_growing_forever(tmp_path):
    """常年开着服务时 access_log 会一直长；超限后滚成 .1，别把盘写满。"""
    from landoc.server import Audit

    path = tmp_path / "logs" / "access_log.jsonl"
    audit = Audit(path, max_bytes=300)
    for i in range(50):
        audit.log(ip="10.0.0.1", api="/api/search", status=200, q=f"第{i}次")
    assert path.is_file()
    assert path.stat().st_size <= 300
    rotated = path.with_name(path.name + ".1")
    assert rotated.is_file() and rotated.stat().st_size > 0
    # 最近的一行一定在新文件里（滚走的是旧的，不是刚写的）
    assert "第49次" in path.read_text(encoding="utf-8")


def test_empty_cidrs_denies_all(lib):
    """I5: 清空 allow_cidrs = 暂停共享，绝不能变成"全网放行"。"""
    cfg, index = lib
    cfg.allow_cidrs = []
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        assert client.get("/api/search?q=x")[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_meta_reports_actual_bound_port(lib):
    """I9: /api/meta 的端口必须是真正监听的端口（8766 被占时会顺延）。"""
    cfg, index = lib
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        assert client.json("/api/meta")["port"] == port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_health_reports_last_scan(running):
    """M1 顺带: /api/health 带上"最后更新"，库主人和客户端都能看到新鲜度。"""
    client, _, _ = running
    payload = client.json("/api/health")
    assert "last_scan" in payload


def test_snippet_tokens_from_config_is_honored(tmp_path: Path):
    """make_server 必须把 cfg.snippet_tokens 传给 Index：改配置不能静默不生效。"""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    cfg = Config(name="库", lib_dir=lib_dir, allow_cidrs=["127.0.0.1"], snippet_tokens=8)
    index = Index(cfg.db_path, md_root=cfg.md_dir)
    index.upsert(
        "a.md", "长文", "docx", "/s/a.docx", 10, 1.0, "开头" * 200 + "马斯京根法河道洪水演算" + "结尾" * 200
    )
    index.commit()

    httpd = make_server(cfg, port=0)  # 不传 index：走"按配置自建"的那条路
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        payload = client.json("/api/search?q=" + urllib.parse.quote("马斯京根"))
        snippet = payload["results"][0]["snippet"]
        assert "<<马斯京根>>" in snippet
        assert len(snippet) < 45  # 窗口 48 时这里会是 ~110 字
    finally:
        httpd.shutdown()
        httpd.server_close()
