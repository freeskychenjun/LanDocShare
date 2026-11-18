"""发布脚本的编码与清单单测。

历史坑：在 GBK 控制台下 `--notes "修复…"` 传进来的中文，Python 拿到的是按
GBK 解过的 UTF-8 字节，写进 latest.json 就成了 `淇锛氭墭鐩樻湁` 这种乱码。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_release():
    spec = importlib.util.spec_from_file_location(
        "landoc_release", ROOT / "packaging" / "release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _load_release()


def test_fix_console_text_recovers_utf8_bytes_decoded_as_gbk():
    original = "托盘更新"
    mojibake = original.encode("utf-8").decode("cp936")
    assert mojibake != original
    assert release._fix_console_text(mojibake) == original


def test_fix_console_text_leaves_normal_text_alone():
    assert release._fix_console_text("修复：托盘里没有图标") == "修复：托盘里没有图标"
    assert release._fix_console_text("plain ascii") == "plain ascii"
    assert release._fix_console_text("") == ""


def test_manifest_is_written_as_readable_utf8(tmp_path):
    path = tmp_path / "latest.json"
    release._write_json(path, {"notes": ["修复：托盘图标有时是空槽"]})
    text = path.read_text(encoding="utf-8")
    assert "修复：托盘图标有时是空槽" in text
    assert "\\u" not in text  # 没被 ensure_ascii 转成 \uXXXX
