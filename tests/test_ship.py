"""随包默认值：登记中心地址由打包时生成，源码里不留真实内网地址。

以前这一项只活在 `config.json` 里，装完的库根本不知道自己该登记到哪，
在中心的列表里看不见、也收不到新版本。现在打包时写 `site_defaults.json`，
向导与 `init` 都从这里取默认值；源码运行时读不到就是空串（= 不登记）。
"""

from __future__ import annotations

import json

from landoc import ship


def test_shipped_registry_url_is_empty_without_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ship, "site_dir", lambda: tmp_path)
    assert ship.shipped_registry_url() == ""


def test_shipped_registry_url_reads_the_packaged_file(tmp_path, monkeypatch):
    (tmp_path / ship.SITE_FILE).write_text(
        json.dumps({"registry_url": "http://192.0.2.5:8767"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(ship, "site_dir", lambda: tmp_path)
    assert ship.shipped_registry_url() == "http://192.0.2.5:8767"


def test_shipped_registry_url_ignores_a_broken_file(tmp_path, monkeypatch):
    (tmp_path / ship.SITE_FILE).write_text("{ 不是 json", encoding="utf-8")
    monkeypatch.setattr(ship, "site_dir", lambda: tmp_path)
    assert ship.shipped_registry_url() == ""


def test_write_site_defaults_leaves_no_temp_file(tmp_path):
    target = tmp_path / ship.SITE_FILE
    ship.write_site_defaults(target, registry_url=" http://192.0.2.5:8767 ")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {"registry_url": "http://192.0.2.5:8767"}
    assert list(tmp_path.iterdir()) == [target]
