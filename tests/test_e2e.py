"""端到端验收：三份真实加密文档跑通 建库 → 转换 → 索引 → 检索。

slow 用例（需要真机 Office 与真实加密样本）：`set LANSHARE_IT=1` 后运行。
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from landoc import cli
from landoc.config import Config
from landoc.index import Index
from landoc.server import make_server


@pytest.mark.slow
def test_real_encrypted_docs_end_to_end(tmp_path: Path, real_encrypted_docs_dir: Path, acrobat_tool_dir: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    assert (
        cli.main(
            ["init", "--lib-dir", str(lib), "--name", "验收库", "--folder", str(real_encrypted_docs_dir)]
        )
        == 0
    )
    cfg = Config.load(lib)
    cfg.acrobat_tool_dir = acrobat_tool_dir
    cfg.save()

    assert cli.main(["convert", "--lib-dir", str(lib)]) == 0

    index = Index(cfg.db_path, md_root=cfg.md_dir)
    total = index.stats()["total"]
    assert total >= 3, f"只转换出 {total} 篇"

    # 加密 docx（COM → Flat OPC → 重建 docx）能被检索到
    rows, _ = index.search("乐平")
    assert any("乐平" in r["title"] or "乐平" in r["snippet"] for r in rows)

    # 加密 pptx（COM 形状文本）
    rows, _ = index.search("溪霞")
    assert rows

    # 加密 pdf（兜底工具解临时副本）
    rows, _ = index.search("水安全")
    assert rows


@pytest.mark.slow
def test_search_served_over_http(tmp_path: Path, real_encrypted_docs_dir: Path, acrobat_tool_dir: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    cli.main(["init", "--lib-dir", str(lib), "--name", "验收库", "--folder", str(real_encrypted_docs_dir)])
    cfg = Config.load(lib)
    cfg.acrobat_tool_dir = acrobat_tool_dir
    cfg.allow_cidrs = ["127.0.0.1"]
    cfg.save()
    cli.main(["convert", "--lib-dir", str(lib)])

    cfg = Config.load(lib)
    cfg.allow_cidrs = ["127.0.0.1"]
    httpd = make_server(cfg, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _, port = httpd.server_address[:2]
    try:
        url = f"http://127.0.0.1:{port}/api/search?q=" + urllib.parse.quote("溪霞") + "&limit=5"
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["count"] >= 1
        first = payload["results"][0]
        assert set(first) == {"path", "title", "ext", "score", "snippet"}
        assert "/secret" not in json.dumps(payload)
    finally:
        httpd.shutdown()
        httpd.server_close()
