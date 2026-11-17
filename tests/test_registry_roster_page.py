"""中心说明页上的「名单编辑页」：只有中心本机能打开、能改、改完立刻生效。"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from landoc.registry import RegistryStore, make_registry_server, raw_rows_from_form, roster_from_form
from landoc.roster import Roster, format_roster


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随跳转：让 303 变成 HTTPError，好断言「保存后回页面」这个约定。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # pragma: no cover - 只是关掉跟随
        return None


def _start(tmp_path, *, admin_ips, roster_path=None):
    store = RegistryStore(tmp_path / "registry.json")
    path = roster_path if roster_path is not None else tmp_path / "roster.csv"
    httpd = make_registry_server(
        store,
        port=0,
        allow_cidrs=["127.0.0.1"],
        roster_path=path,
        roster_admin_ips=admin_ips,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", path, httpd, thread


@pytest.fixture()
def center(tmp_path):
    base, path, httpd, thread = _start(tmp_path, admin_ips={"127.0.0.1"})
    try:
        yield base, path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


@pytest.fixture()
def other_machine(tmp_path):
    """名单操作被收窄到别的 IP —— 模拟「同事从自己机器上访问中心」。"""
    base, path, httpd, thread = _start(tmp_path, admin_ips={"10.9.9.9"})
    try:
        yield base, path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def get_text(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def post_form(url: str, fields, *, follow: bool = True):
    """提交表单。follow=False 时不跟随跳转，直接返回 303 的状态/头/正文。"""
    data = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    if follow:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", errors="replace")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=3) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8", errors="replace")


def post_expect_error(url: str, fields) -> tuple[int, str]:
    with pytest.raises(urllib.error.HTTPError) as info:
        post_form(url, fields)
    return info.value.code, info.value.read().decode("utf-8", errors="replace")


def _published(path, *, replace_lines: str | None = None):
    book = Roster(
        {"陈鋆": "10.91.1.197", "郑泽锋": "10.91.1.206"},
        {"陈鋆": "规划一所", "郑泽锋": "水文所"},
    )
    path.write_text(replace_lines if replace_lines is not None else format_roster(book), encoding="utf-8-sig")


# ---- 纯函数 ----


def test_raw_rows_from_form_pairs_repeated_fields_and_skips_blank_rows():
    fields = {
        "name": ["陈鋆", "", "郑泽锋"],
        "ip": ["10.91.1.197", "", ""],
        "dept": ["规划一所", "", ""],
    }
    assert raw_rows_from_form(fields) == [
        ("陈鋆", "10.91.1.197", "规划一所"),
        ("郑泽锋", "", ""),
    ]


def test_roster_from_form_reports_all_errors_at_once():
    book, errors = roster_from_form(
        [
            ("陈鋆", "10.91.1.197", "规划一所"),
            ("郑泽锋", "10.91.1.999", "水文所"),
            ("陈鋆", "10.91.1.200", ""),
            ("", "10.91.1.201", ""),
        ]
    )
    assert len(book) == 1 and book.people["陈鋆"] == "10.91.1.197"
    assert len(errors) == 3
    assert any("第 2 行" in text for text in errors)
    assert any("重复" in text for text in errors)


# ---- 只有中心本机能用 ----


def test_edit_page_lists_published_roster_for_center_machine(center):
    base, path = center
    _published(path)
    code, page = get_text(f"{base}/roster/edit")
    assert code == 200
    assert "部门授权名单 · 编辑" in page
    assert "陈鋆" in page and "10.91.1.197" in page and "规划一所" in page
    assert "2 人 · 2 个部门" in page
    assert "<form method=\"post\" action=\"/roster/edit\">" in page


def test_edit_page_refuses_other_machines_without_leaking_roster(other_machine):
    base, path = other_machine
    _published(path)
    with pytest.raises(urllib.error.HTTPError) as info:
        get_text(f"{base}/roster/edit")
    assert info.value.code == 403
    page = info.value.read().decode("utf-8")
    assert "只有登记中心那台机器能编辑名单" in page
    assert "/roster/edit" in page  # 告诉同事去中心那台机器上打开
    assert "陈鋆" not in page and "10.91.1.197" not in page


def test_landing_page_only_links_editor_on_center_machine(center, other_machine):
    base, _path = center
    _code, page = get_text(f"{base}/")
    assert "编辑名单" in page and "/roster/edit" in page

    base2, _path2 = other_machine
    _code, page2 = get_text(f"{base2}/")
    assert "/roster/edit" not in page2


# ---- 保存 ----


def test_save_from_table_writes_roster_and_redirects(center):
    base, path = center
    fields = [
        ("action", "save"),
        ("name", "陈鋆"),
        ("ip", "10.91.1.197"),
        ("dept", "规划一所"),
        ("name", "郑泽锋"),
        ("ip", "10.91.1.206"),
        ("dept", "水文所"),
    ]
    code, headers, _body = post_form(f"{base}/roster/edit", fields, follow=False)
    assert code == 303
    assert headers["Location"] == "/roster/edit?saved=2&warn=0"

    saved = path.read_text(encoding="utf-8-sig")
    assert "陈鋆,10.91.1.197,规划一所" in saved
    assert "郑泽锋,10.91.1.206,水文所" in saved

    # 303 之后跟着跳回来能看到提示，且刷新不会重复提交
    code, page = get_text(f"{base}/roster/edit?saved=2&warn=0")
    assert code == 200 and "已保存：2 人" in page


def test_save_accepts_rows_without_ip_or_department(center):
    base, path = center
    _published(path)
    code, _headers, _body = post_form(
        f"{base}/roster/edit",
        [("action", "save"), ("name", "老王"), ("ip", ""), ("dept", "")],
        follow=False,
    )
    assert code == 303
    saved = path.read_text(encoding="utf-8-sig")
    assert "老王," in saved and "陈鋆" not in saved


def test_save_rejects_bad_ip_and_keeps_published_file(center):
    base, path = center
    _published(path)
    before = path.read_bytes()
    code, page = post_expect_error(
        f"{base}/roster/edit",
        [("action", "save"), ("name", "老王"), ("ip", "10.91.1.999"), ("dept", "水文所")],
    )
    assert code == 400
    assert "第 1 行" in page
    assert "value='老王'" in page  # 用户填的内容原样回填，不用重打
    assert path.read_bytes() == before


def test_save_rejects_duplicate_names(center):
    base, _path = center
    code, page = post_expect_error(
        f"{base}/roster/edit",
        [
            ("action", "save"),
            ("name", "陈鋆"),
            ("ip", "10.91.1.197"),
            ("dept", ""),
            ("name", "陈鋆"),
            ("ip", "10.91.1.198"),
            ("dept", ""),
        ],
    )
    assert code == 400 and "重复" in page


def test_save_rejects_empty_roster(center):
    base, path = center
    _published(path)
    before = path.read_bytes()
    code, page = post_expect_error(f"{base}/roster/edit", [("action", "save"), ("name", ""), ("ip", ""), ("dept", "")])
    assert code == 400 and "名单是空的" in page
    assert path.read_bytes() == before


def test_other_machine_cannot_save(other_machine):
    base, path = other_machine
    _published(path)
    before = path.read_bytes()
    code, _page = post_expect_error(
        f"{base}/roster/edit",
        [("action", "save"), ("name", "陌生人"), ("ip", "10.91.1.9"), ("dept", "")],
    )
    assert code == 403
    assert path.read_bytes() == before


def test_rejected_post_with_large_body_still_gets_the_403_page(other_machine):
    """拒绝前必须把请求体读掉。

    中心是 HTTP/1.0，回完响应就关连接；body 没读完就关，客户端正在写的那一段会收到
    RST（Windows 上抛 ConnectionAbortedError），403 说明页根本到不了浏览器。
    """
    base, path = other_machine
    _published(path)
    code, page = post_expect_error(
        f"{base}/roster/edit",
        [("action", "import"), ("csv", "姓名,IP,部门\n" + "老王,10.91.1.50,水文所\n" * 4000)],
    )
    assert code == 403
    assert "只有登记中心那台机器能编辑名单" in page


# ---- 粘贴导入 ----


def test_import_csv_replaces_published_roster(center):
    base, path = center
    _published(path)
    csv_text = "姓名,IP,部门\n老王,10.91.1.50,水文所\n小李,,规划二所\n"
    code, _headers, _body = post_form(
        f"{base}/roster/edit",
        [("action", "import"), ("csv", csv_text), ("replace", "1")],
        follow=False,
    )
    assert code == 303
    saved = path.read_text(encoding="utf-8-sig")
    assert "老王,10.91.1.50,水文所" in saved
    assert "小李" in saved
    assert "陈鋆" not in saved  # 整体替换


def test_import_csv_merges_when_replace_unchecked(center):
    base, path = center
    _published(path)
    code, _headers, _body = post_form(
        f"{base}/roster/edit",
        [("action", "import"), ("csv", "老王,10.91.1.50,水文所")],
        follow=False,
    )
    assert code == 303
    saved = path.read_text(encoding="utf-8-sig")
    assert "陈鋆,10.91.1.197,规划一所" in saved  # 原有保留
    assert "老王,10.91.1.50,水文所" in saved


def test_import_csv_without_names_is_rejected(center):
    base, path = center
    _published(path)
    before = path.read_bytes()
    code, page = post_expect_error(f"{base}/roster/edit", [("action", "import"), ("csv", "\n\n# 只有注释\n")])
    assert code == 400 and "没解析出人名" in page
    assert path.read_bytes() == before
