from __future__ import annotations

import json
from pathlib import Path

from landoc import cli
from landoc.config import Config
from landoc.index import Index


def make_lib(tmp_path: Path, *, missing: bool = True) -> tuple[Config, Index]:
    """一个只有"库内副本"的库：md 在库目录里，源路径指向早就不存在的地方。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config(name="测试库", lib_dir=lib, folders=[tmp_path / "成果"])
    cfg.save()
    md_path = Path(cfg.md_dir) / "成果" / "a.docx.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("# 一份还在库里的文档\n", encoding="utf-8")
    index = Index(cfg.db_path, md_root=cfg.md_dir)
    index.upsert("成果/a.docx.md", "a", "docx", str(tmp_path / "成果" / "a.docx"), 10, 1.0, "正文")
    index.commit()
    entry = {"mtime": 1.0, "size": 10, "via": "office"}
    if missing:
        entry["source_missing"] = True
    state = {"last_scan_ts": "2026-11-18 22:00:00", "files": {"成果/a.docx": entry}}
    (lib / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return cfg, index


def test_prune_without_gone_only_reports(tmp_path, capsys):
    cfg, index = make_lib(tmp_path)
    assert cli.main(["prune", "--lib-dir", str(cfg.lib_dir)]) == 0
    out = capsys.readouterr().out
    assert "1 篇源文件已不在" in out
    assert "加 --gone" in out
    # 只是提示，一篇都不许动
    assert (Path(cfg.md_dir) / "成果" / "a.docx.md").is_file()
    assert index.stats()["total"] == 1


def test_prune_gone_deletes_the_copy_and_the_row(tmp_path, capsys):
    cfg, index = make_lib(tmp_path)
    assert cli.main(["prune", "--lib-dir", str(cfg.lib_dir), "--gone"]) == 0
    assert "已清理 1 篇" in capsys.readouterr().out
    assert not (Path(cfg.md_dir) / "成果" / "a.docx.md").exists()
    assert index.stats()["total"] == 0


def test_prune_keeps_docs_whose_source_is_still_there(tmp_path):
    """源文件还在的文档，哪怕 --gone 也不许动。"""
    cfg, index = make_lib(tmp_path, missing=False)
    assert cli.main(["prune", "--lib-dir", str(cfg.lib_dir), "--gone"]) == 0
    assert (Path(cfg.md_dir) / "成果" / "a.docx.md").is_file()
    assert index.stats()["total"] == 1


def test_prune_gone_also_forgets_failure_records(tmp_path, capsys):
    """删掉一篇以后，它的转换失败记录也得跟着走。

    否则 state.json 与 failures.json 里留着永远不会重试的死条目，
    状态窗的"失败 N 篇"永远清不掉。
    """
    cfg, index = make_lib(tmp_path)
    state = json.loads((cfg.lib_dir / "state.json").read_text(encoding="utf-8"))
    state["failures"] = {"成果/a.docx": {"reason": "boom", "attempts": 3}}
    (cfg.lib_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    (cfg.lib_dir / "failures.json").write_text(
        json.dumps({"generated_at": "x", "failures": state["failures"]}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert cli.main(["prune", "--lib-dir", str(cfg.lib_dir), "--gone"]) == 0

    after = json.loads((cfg.lib_dir / "state.json").read_text(encoding="utf-8"))
    assert after["failures"] == {}
    mirror = json.loads((cfg.lib_dir / "failures.json").read_text(encoding="utf-8"))
    assert mirror["failures"] == {}


def test_prune_keeps_failures_of_docs_it_does_not_touch(tmp_path):
    cfg, index = make_lib(tmp_path, missing=False)
    state = json.loads((cfg.lib_dir / "state.json").read_text(encoding="utf-8"))
    state["failures"] = {"成果/a.docx": {"reason": "boom", "attempts": 1}}
    (cfg.lib_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["prune", "--lib-dir", str(cfg.lib_dir), "--gone"]) == 0

    after = json.loads((cfg.lib_dir / "state.json").read_text(encoding="utf-8"))
    assert list(after["failures"]) == ["成果/a.docx"]
