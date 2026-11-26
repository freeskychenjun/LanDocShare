from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from landoc.config import Config
from landoc.index import Index
from landoc import server as server_module
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
    # 测试从 127.0.0.1 连入，故把本机回环列为允许网段（默认是 172.16.0.0/16）
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
    assert payload["deny_cidrs"] == []
    assert "source_path" not in json.dumps(payload)


def test_deny_cidrs_wins_over_allow(lib):
    """黑名单优先：即使来源在 allow 里，命中 deny 也一律 403。"""
    cfg, index = lib
    cfg.deny_cidrs = ["127.0.0.1"]
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(base + "/api/meta", timeout=3)
        assert excinfo.value.code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


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
    assert set(payload["files"][0]) == {"path", "title", "ext", "size", "withdrawn"}
    assert all(f["withdrawn"] is False for f in payload["files"])


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


def test_withdrawn_docs_are_owner_only_over_http(lib):
    cfg, index = lib
    index.mark_withdrawn("成果/乐平.md")
    index.commit()
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{httpd.server_address[1]}")
    try:
        # 本机（回环）是主人：计数、检索、目录里都还在
        assert client.json("/api/health")["doc_count"] == 2
        assert client.json("/api/search?q=" + urllib.parse.quote("乐平"))["count"] == 1
        assert {f["title"] for f in client.json("/api/list?path=" + urllib.parse.quote("成果"))["files"]} == {
            "乐平下闸蓄水",
            "溪霞水库",
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_withdrawn_docs_are_hidden_from_remote_clients(lib, monkeypatch):
    cfg, index = lib
    index.mark_withdrawn("成果/乐平.md")
    index.commit()
    # 把本机地址伪装成别的机器：127.0.0.1 仍是允许来源，但不再是 owner
    monkeypatch.setattr("landoc.server.cached_local_ips", lambda: frozenset({"192.0.2.9"}))
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{httpd.server_address[1]}")
    try:
        assert client.json("/api/health")["doc_count"] == 1
        assert client.json("/api/meta")["doc_count"] == 1
        assert client.json("/api/search?q=" + urllib.parse.quote("乐平"))["count"] == 0
        assert [f["title"] for f in client.json("/api/recent")["files"]] == ["溪霞水库"]
        assert client.json("/api/tree")["children"][0]["count"] == 1
        assert [f["title"] for f in client.json("/api/list?path=" + urllib.parse.quote("成果"))["files"]] == ["溪霞水库"]
        status, _ = client.get("/api/tables?path=" + urllib.parse.quote("成果/乐平.md"))
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_list_dir_returns_subdirectories_for_deep_folders(lib):
    """2011年项目这类"文档全在子目录里"的目录：直接子文件是 0，但子目录必须给出来。

    否则检索页点进去只有一句"这个目录下没有文档"，看着像 150 篇丢了。
    """
    cfg, index = lib
    index.upsert("2011年项目/报告编写/潦河意见.md", "潦河意见", "docx", "/secret/c.docx", 10, 1.0, "潦河")
    index.upsert("2011年项目/报告编写/锦江意见.md", "锦江意见", "docx", "/secret/d.docx", 10, 1.0, "锦江")
    index.commit()
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{httpd.server_address[1]}")
    try:
        payload = client.json("/api/list?path=" + urllib.parse.quote("2011年项目"))
        assert payload["count"] == 0
        assert payload["files"] == []
        assert payload["dirs"] == [
            {"name": "报告编写", "count": 2, "path": "2011年项目/报告编写"}
        ]
        # 进到子目录才看得到文件
        deeper = client.json("/api/list?path=" + urllib.parse.quote("2011年项目/报告编写"))
        assert deeper["count"] == 2
        assert deeper["dirs"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


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


def test_cidr_not_allowed_is_403(lib, monkeypatch):
    """网段不在白名单里就 403。

    本机自己那台走的是"主人永远进得来"，所以这里把本机 IP 清单清空，
    模拟一个真正的外来源地址（127.0.0.1 于是退化成普通来源）。
    """
    from landoc import server as server_module

    monkeypatch.setattr(server_module, "cached_local_ips", lambda: set())
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


def test_authorize_person_prefers_ip_and_name_is_not_a_credential():
    from landoc.server import authorize_person

    roster = {"赵六": "172.16.1.197", "张三": "", "李四": "172.16.1.50"}
    # 来源 IP 在名单里：不报姓名也放行，审计记那条的姓名
    assert authorize_person(roster, "172.16.1.197", None) == ("赵六", None)
    assert authorize_person(roster, "172.16.1.197", "赵六") == ("赵六", None)
    # 冒名：姓名的登记 IP 与来源不一致
    assert authorize_person(roster, "172.16.1.197", "李四") == ("李四", "ip mismatch for name")
    # 不在名单里的人
    assert authorize_person(roster, "172.16.1.197", "王五") == ("王五", "name not registered")
    # IP 不在名单、也没报姓名
    assert authorize_person(roster, "172.16.1.99", None) == (None, "ip not registered")
    # 空 IP 条目：知道姓名也不能进（姓名不是口令）
    assert authorize_person(roster, "172.16.1.99", "张三") == ("张三", "name has no IP registered")
    assert authorize_person(roster, "172.16.1.197", "张三") == ("张三", "name has no IP registered")


def test_authorize_person_lets_loopback_through_and_empty_roster_is_open():
    from landoc.server import authorize_person

    assert authorize_person({"赵六": "172.16.1.197"}, "127.0.0.1", None) == ("本机", None)
    assert authorize_person({"赵六": "172.16.1.197"}, "::1", None) == ("本机", None)
    # 名单为空 = 只靠网段把关，报不报姓名都无所谓
    assert authorize_person({}, "172.16.1.197", None) == (None, None)


def test_authorize_person_lets_the_owner_in_through_the_lan_address():
    """主人用本机网卡地址（http://10.x.x.x:8768/）打开自己的库也得进得去。

    否则一开「仅指定名单」就把自己关在门外：名单里通常只有同事的 IP，
    而浏览器访问本机服务时来源地址是自己的网卡 IP，不是 127.0.0.1。
    """
    from landoc.server import authorize_person

    roster = {"赵六": "172.16.1.197"}
    # 不是本机：照旧按名单拒
    assert authorize_person(roster, "172.16.1.99", None) == (None, "ip not registered")
    # 是本机：放行，审计记「本机」
    assert authorize_person(roster, "172.16.1.99", None, local=True) == ("本机", None)
    assert authorize_person(roster, "172.16.1.99", "张三", local=True) == ("张三", None)
    assert authorize_person({}, "172.16.1.197", "赵六") == ("赵六", None)


def test_roster_is_live_and_loopback_owner_always_passes(lib):
    """名单里有 IP 条目时，127.0.0.1 自检不被挡（主人本机调试、状态窗口都走回环）。"""
    cfg, index = lib
    cfg.allow_people = {"赵六": "172.16.1.197"}
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        assert client.get("/api/search?q=x")[0] == 200
        # 名单改动即时生效（服务端每请求现读 cfg，不用重启）
        cfg.allow_people = {"赵六": "172.16.1.197", "张三": "172.16.1.50"}
        assert client.get("/api/meta")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_meta_never_requires_name_even_with_blank_ip_entries(lib):
    cfg, index = lib
    cfg.allow_people = {"赵六": "127.0.0.1"}
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
        assert "赵六" not in raw
    finally:
        httpd.shutdown()
        httpd.server_close()

    cfg.allow_people = {"赵六": "127.0.0.1", "张三": ""}
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        status, raw = client.get("/api/meta")
        assert status == 200
        assert json.loads(raw)["name_required"] is False  # 空 IP 条目也不授权，不再要求报姓名
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


def test_access_log_rotates_without_overwriting(tmp_path):
    """常年开着服务时 access_log 会一直长；超限后按时间戳归档，旧记录一条都不能丢。"""
    from landoc.server import Audit

    path = tmp_path / "logs" / "access_log.jsonl"
    audit = Audit(path, max_bytes=300)
    for i in range(50):
        audit.log(ip="10.0.0.1", api="/api/search", status=200, q=f"第{i}次")
    assert path.is_file()
    assert path.stat().st_size <= 300

    archives = sorted((tmp_path / "logs").glob("access_log-*.jsonl"))
    assert archives, "超限后要滚出带时间戳的归档"
    # 不再有"每次轮转覆盖上一份"的 .1
    assert not path.with_name(path.name + ".1").exists()
    # 50 次请求一条不少：轮转只搬走，不删除
    text = "".join(p.read_text(encoding="utf-8") for p in [*archives, path])
    for i in range(50):
        assert f"第{i}次" in text


def test_make_server_passes_the_denied_callback(tmp_path):
    """"访问被拒"要能冒到 App（状态窗口显示），所以回调得挂到 Audit 上。"""
    cfg = Config(name="测试库", lib_dir=tmp_path, folders=[])
    cfg.allow_cidrs = ["127.0.0.1"]
    seen: list[dict] = []

    def on_denied(**fields):
        seen.append(fields)

    httpd = make_server(cfg, index=server_module.Index(cfg.db_path), port=0, on_denied=on_denied)
    try:
        assert httpd.RequestHandlerClass.audit.on_denied is on_denied
    finally:
        httpd.server_close()


def test_denied_request_notifies_the_app(tmp_path):
    """401/403/429 才通知；正常 200 不该惊动状态窗口。"""
    from landoc.server import Audit

    seen: list[dict] = []
    audit = Audit(tmp_path / "logs" / "access_log.jsonl", on_denied=lambda **kw: seen.append(kw))
    audit.log(ip="10.91.1.178", name="", api="/api/search", status=401)
    audit.log(ip="10.0.0.1", name="", api="/api/stats", status=200)
    audit.log(ip="10.91.1.178", name="张三", api="/api/list", status=403)

    assert seen == [
        {"ip": "10.91.1.178", "name": "", "api": "/api/search", "status": 401},
        {"ip": "10.91.1.178", "name": "张三", "api": "/api/list", "status": 403},
    ]


def test_search_record_carries_query_fallback_and_dir(running):
    """查询记录留明文，另带 fallback 与 dir：事后才知道同事搜了什么、为什么没命中。"""
    client, cfg, _ = running
    client.get("/api/search?q=" + urllib.parse.quote("马斯京根") + "&dir=" + urllib.parse.quote("成果"))

    log = Path(cfg.lib_dir) / "logs" / "access_log.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    record = [row for row in rows if row.get("api") == "/api/search"][-1]
    assert record["q"] == "马斯京根"
    assert record["dir"] == "成果"
    assert "fallback" in record
    assert record["count"] == 1


def test_empty_cidrs_denies_all(lib, monkeypatch):
    """I5: 清空 allow_cidrs = 暂停共享，对**别人**绝不能变成"全网放行"。

    本机自己那台不在此列（状态窗口写的是「本机之外都访问不到」），所以这里把
    "本机网卡 IP" 清空，让 127.0.0.1 走普通来源那条判定。
    """
    from landoc import server as server_module

    monkeypatch.setattr(server_module, "cached_local_ips", lambda: set())
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


def test_the_owner_still_gets_in_when_every_subnet_is_closed(lib, monkeypatch):
    """暂停共享/白名单写窄了也不该把主人关在门外：状态窗口写的是「本机之外都访问不到」。"""
    from landoc import server as server_module

    monkeypatch.setattr(server_module, "cached_local_ips", lambda: {"127.0.0.1"})
    cfg, index = lib
    cfg.allow_cidrs = []  # 暂停共享
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        status, _ = client.get("/api/meta")
        assert status == 200
        assert client.json("/api/meta")["port"] == port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_an_explicit_deny_still_blocks_the_owner(lib, monkeypatch):
    """deny_cidrs 是主人明确写下的"这段别来"，本机也不例外。"""
    from landoc import server as server_module

    monkeypatch.setattr(server_module, "cached_local_ips", lambda: {"127.0.0.1"})
    cfg, index = lib
    cfg.allow_cidrs = ["127.0.0.0/8"]
    cfg.deny_cidrs = ["127.0.0.1/32"]
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _, port = httpd.server_address[:2]
    try:
        client = Client(f"http://127.0.0.1:{port}")
        assert client.get("/api/meta")[0] == 403
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


def test_two_requests_can_share_one_connection(lib):
    """HTTP/1.1 keep-alive：响应都带 Content-Length，一条连接能连着发两次请求。

    检索页一次搜索要发 4~5 个请求（meta/tree/search），HTTP/1.0 下每次都要重连。
    """
    cfg, index = lib
    httpd = make_server(cfg, index=index, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    try:
        conn.request("GET", "/api/health")
        first = conn.getresponse()
        assert first.version == 11
        assert json.loads(first.read())["status"] == "online"
        assert first.will_close is False

        conn.request("GET", "/api/stats")
        second = conn.getresponse()
        assert json.loads(second.read())["service"] == "landoc"
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()


def test_page_sends_unregistered_visitors_to_the_owner_not_to_a_name_form():
    """0.2.10 起姓名不再授权：401 之后再让用户"填个名字重试"就是死循环。

    页面必须直接说清"IP 没登记，找库主人"，不能留任何重试入口。
    """
    page = (Path(server_module.__file__).with_name("web") / "index.html").read_text(encoding="utf-8")
    assert "这台电脑的 IP 不在授权名单里，请联系库主人登记 IP" in page
    assert "这个库需要登记姓名" not in page
    assert "ip mismatch for name" not in page
