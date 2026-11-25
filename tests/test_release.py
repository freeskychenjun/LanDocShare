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

def test_prune_keeps_latest_and_lists_the_rest(tmp_path):
    """4.5: 只留最新版 zip + 固定下载名 + 清单，历史包与旧构建目录可回收。"""
    out = tmp_path / "distribution"
    out.mkdir()
    (out / "LanDocShare-0.2.1.zip").write_bytes(b"x" * 10)
    (out / "LanDocShare-0.2.6.zip").write_bytes(b"x" * 20)
    (out / "LanDocShare.zip").write_bytes(b"x" * 20)
    (out / "notes-0.2.1.txt").write_text("old", encoding="utf-8")
    release._write_json(out / release._DEFAULT_MANIFEST, {"version": "0.2.6"})
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "LanDocShare").mkdir()
    (dist / "LanDocShare.old-20261118-095304").mkdir()
    (dist / "_old-0.1.0").mkdir()

    doomed = release.prune(out, dist, apply=False)
    names = sorted(path.name for path, _size in doomed)
    assert names == [
        "LanDocShare-0.2.1.zip",
        "LanDocShare.old-20261118-095304",
        "_old-0.1.0",
        "notes-0.2.1.txt",
    ]
    # 干跑只打印，不动文件
    assert (out / "LanDocShare-0.2.1.zip").exists()
    assert (dist / "_old-0.1.0").exists()
    # 该留的东西一个都不许碰
    assert (out / "LanDocShare-0.2.6.zip").exists()
    assert (out / "LanDocShare.zip").exists()
    assert (out / release._DEFAULT_MANIFEST).exists()
    assert (dist / "LanDocShare").exists()

    release.prune(out, dist, apply=True)
    assert not (out / "LanDocShare-0.2.1.zip").exists()
    assert not (out / "notes-0.2.1.txt").exists()
    assert not (dist / "_old-0.1.0").exists()
    assert (out / "LanDocShare-0.2.6.zip").exists()
    assert (dist / "LanDocShare").exists()


def test_prune_without_manifest_keeps_every_zip(tmp_path):
    """清单丢了（不知道最新是哪版）就一个 zip 都别删，宁可占点磁盘也别删错。"""
    out = tmp_path / "distribution"
    out.mkdir()
    (out / "LanDocShare-0.2.1.zip").write_bytes(b"x")
    (out / "LanDocShare-0.2.6.zip").write_bytes(b"x")
    dist = tmp_path / "dist"
    dist.mkdir()

    doomed = release.prune(out, dist, apply=True)
    assert doomed == []
    assert (out / "LanDocShare-0.2.1.zip").exists()
    assert (out / "LanDocShare-0.2.6.zip").exists()


def _fake_release_inputs(tmp_path):
    """造一份「已发过 0.2.1」的现场：旧 zip + 旧清单 + 一个假的 dist。"""
    out = tmp_path / "distribution"
    out.mkdir()
    (out / "LanDocShare-0.2.1.zip").write_bytes(b"x" * 10)
    release._write_json(out / release._DEFAULT_MANIFEST, {"version": "0.2.1"})
    dist = tmp_path / "dist" / "LanDocShare"
    dist.mkdir(parents=True)
    (dist / "LanDocShare.exe").write_bytes(b"exe")
    return out, dist


def test_release_prunes_old_packages_when_asked(tmp_path):
    """带 prune_old=True 发布：只留当前版本，旧包当场回收。"""
    out, dist = _fake_release_inputs(tmp_path)

    manifest = release.release(
        release.__version__,
        dist_dir=dist,
        out_dir=out,
        notes=["顺手清旧包"],
        skip_build=True,
        prune_old=True,
    )

    assert manifest["version"] == release.__version__
    assert (out / f"LanDocShare-{release.__version__}.zip").is_file()
    assert (out / "LanDocShare.zip").is_file()
    assert (out / release._DEFAULT_MANIFEST).is_file()
    assert not (out / "LanDocShare-0.2.1.zip").exists()


def test_release_keeps_old_packages_by_default(tmp_path):
    """默认不清：没给 prune_old 时旧包必须还在（免得有人还指着它回滚）。"""
    out, dist = _fake_release_inputs(tmp_path)

    release.release(
        release.__version__,
        dist_dir=dist,
        out_dir=out,
        notes=["默认不清"],
        skip_build=True,
    )

    assert (out / "LanDocShare-0.2.1.zip").exists()
    assert (out / f"LanDocShare-{release.__version__}.zip").is_file()
