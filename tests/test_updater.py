"""自动更新单测：版本比较、下载续传与校验、解包防穿越、换目录成功/回滚、状态机。

都是纯本地动作（临时目录 + 注入的 opener/launcher），不碰真实网络与真实程序目录。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from landoc import updater


@pytest.fixture()
def update_dir(tmp_path, monkeypatch):
    """把更新状态目录指到临时目录，别动本机 %LOCALAPPDATA% 里真下载的包。"""
    root = tmp_path / "updates"
    root.mkdir()
    monkeypatch.setenv("LANDOC_UPDATE_DIR", str(root))
    return root


class _Response:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict | None = None):
        self._stream = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _json_opener(payload: dict, *, calls: list | None = None):
    def open_url(request, timeout=0):
        if calls is not None:
            calls.append(request)
        return _Response(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    return open_url


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #
def test_parse_version_accepts_plain_and_rejects_junk():
    assert updater.parse_version("0.2.0") == (0, 2, 0)
    assert updater.parse_version(" v1.10.3 ") == (1, 10, 3)
    assert updater.parse_version("1.2") is None
    assert updater.parse_version("1.2.3.4") is None
    assert updater.parse_version("") is None


def test_is_newer_compares_numerically_not_as_text():
    assert updater.is_newer("0.10.0", "0.9.0")
    assert not updater.is_newer("0.9.0", "0.10.0")
    assert not updater.is_newer("0.2.0", "0.2.0")
    assert not updater.is_newer("", "0.2.0")


# --------------------------------------------------------------------------- #
# 问中心
# --------------------------------------------------------------------------- #
def _payload(version="0.3.0", **extra):
    data = {
        "version": version,
        "download": f"/download/{version}",
        "sha256": "a" * 64,
        "size": 123,
        "released_at": "2026-11-18 10:00",
        "notes": ["修了检索偶发 500"],
        "min_from": "",
        "mandatory": False,
        "platform": updater.PLATFORM,
    }
    data.update(extra)
    return {"update": data, "server_time": "x", "latest": version}


def test_fetch_update_info_builds_absolute_url():
    info = updater.fetch_update_info(
        "http://172.16.1.197:8767/", "0.2.0", opener=_json_opener(_payload())
    )
    assert info is not None
    assert info.version == "0.3.0"
    assert info.url == "http://172.16.1.197:8767/download/0.3.0"
    assert info.notes == ("修了检索偶发 500",)
    assert info.size == 123


def test_fetch_update_info_none_when_no_update_or_same_version():
    assert updater.fetch_update_info("http://x", "0.2.0", opener=_json_opener({"update": None})) is None
    same = updater.fetch_update_info("http://x", "0.3.0", opener=_json_opener(_payload("0.3.0")))
    assert same is None


def test_fetch_update_info_rejects_bad_sha():
    payload = _payload(sha256="not-a-hash")
    with pytest.raises(ValueError):
        updater.fetch_update_info("http://x", "0.2.0", opener=_json_opener(payload))


def test_fetch_update_info_understands_flat_center_payload():
    """中心的 /api/update 实际是扁平的：update: true + version/download/..."""
    flat = {
        "latest": "0.3.0",
        "version": "0.3.0",
        "update": True,
        "manual_only": False,
        "download": "http://172.16.1.197:8767/download/0.3.0",
        "sha256": "a" * 64,
        "size": 95,
        "notes": ["修了检索偶发 500"],
        "released_at": "2026-11-18 00:00",
        "platform": "windows-x64",
    }
    info = updater.fetch_update_info("http://172.16.1.197:8767", "0.2.0", opener=_json_opener(flat))
    assert info is not None
    assert info.version == "0.3.0"
    assert info.url == "http://172.16.1.197:8767/download/0.3.0"
    assert info.size == 95


def test_fetch_update_info_flat_manual_only_needs_reinstall():
    flat = {
        "latest": "0.9.0",
        "version": "0.9.0",
        "update": False,
        "manual_only": True,
        "download": "http://x/download/0.9.0",
        "min_from": "0.5.0",
    }
    info = updater.fetch_update_info("http://x", "0.2.0", opener=_json_opener(flat))
    assert info is not None and info.too_old is True


def test_fetch_update_info_flat_without_update_is_none():
    flat = {"latest": "0.2.0", "version": "0.2.0", "update": False, "manual_only": False}
    assert updater.fetch_update_info("http://x", "0.2.0", opener=_json_opener(flat)) is None


def test_too_old_marks_manual_reinstall():
    info = updater.UpdateInfo.from_dict(
        {"version": "0.9.0", "download": "http://x/p.zip", "min_from": "0.5.0"}
    )
    assert info.too_old is True


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def _zip_bytes() -> bytes:
    """一个最小更新包：LanDocShare/LanDocShare.exe + 一个数据文件。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("LanDocShare/LanDocShare.exe", b"new-exe")
        archive.writestr("LanDocShare/_internal/data.bin", b"x" * 64)
    return buffer.getvalue()


def test_download_resumes_from_part(tmp_path):
    body = _zip_bytes()
    digest = updater.sha256_file(_write(tmp_path / "full.zip", body))
    info = updater.UpdateInfo(
        version="0.3.0", url="http://x/pkg.zip", sha256=digest, size=len(body)
    )
    dest = tmp_path / "work"
    dest.mkdir()
    half = len(body) // 2
    (dest / "package.zip.part").write_bytes(body[:half])
    seen: list[str] = []

    def open_url(request, timeout=0):
        seen.append(request.get_header("Range") or "")
        assert (request.get_header("Range") or "") == f"bytes={half}-"
        return _Response(body[half:], status=206)

    progress: list[tuple[int, int]] = []
    path = updater.download_package(
        info, dest, opener=open_url, progress=lambda d, t: progress.append((d, t))
    )
    assert path.read_bytes() == body
    assert not (dest / "package.zip.part").exists()
    assert progress and progress[-1][0] == len(body)
    assert seen == [f"bytes={half}-"]


def test_download_restarts_when_server_ignores_range(tmp_path):
    body = _zip_bytes()
    info = updater.UpdateInfo(
        version="0.3.0", url="http://x/pkg.zip", sha256=updater.sha256_file(_write(tmp_path / "f.zip", body)), size=len(body)
    )
    dest = tmp_path / "work"
    dest.mkdir()
    (dest / "package.zip.part").write_bytes(b"garbage")

    def open_url(request, timeout=0):
        return _Response(body, status=200)  # 不认 Range

    assert updater.download_package(info, dest, opener=open_url).read_bytes() == body


def test_download_retries_on_checksum_mismatch(tmp_path, monkeypatch):
    body = _zip_bytes()
    info = updater.UpdateInfo(version="0.3.0", url="http://x/pkg.zip", sha256="b" * 64, size=len(body))
    calls = {"n": 0}

    def open_url(request, timeout=0):
        calls["n"] += 1
        return _Response(body, status=200)

    monkeypatch.setattr(updater.time, "sleep", lambda _s: None)
    with pytest.raises(ValueError):
        updater.download_package(info, tmp_path / "w", opener=open_url, attempts=2)
    assert calls["n"] == 2  # 校验失败会重试，重试用完才报错


# --------------------------------------------------------------------------- #
# 解包
# --------------------------------------------------------------------------- #
def test_extract_finds_nested_app_root(tmp_path):
    zip_path = _write(tmp_path / "pkg.zip", _zip_bytes())
    root = updater.extract_package(zip_path, tmp_path / "app")
    assert root.name == "LanDocShare"
    assert (root / updater.EXE_NAME).read_bytes() == b"new-exe"


def test_extract_rejects_zip_slip(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../evil.txt", b"x")
        archive.writestr("LanDocShare/LanDocShare.exe", b"x")
    zip_path = _write(tmp_path / "evil.zip", buffer.getvalue())
    with pytest.raises(ValueError):
        updater.extract_package(zip_path, tmp_path / "app")
    assert not (tmp_path / "evil.txt").exists()


def test_prepare_update_downloads_extracts_and_reuses(update_dir, tmp_path):
    body = _zip_bytes()
    info = updater.UpdateInfo(
        version="0.3.0", url="http://x/pkg.zip", sha256=updater.sha256_file(_write(tmp_path / "f.zip", body)), size=len(body)
    )
    first = updater.prepare_update(info, root=update_dir, opener=lambda request, timeout=0: _Response(body))
    assert first.exe.read_bytes() == b"new-exe"
    work = update_dir / "0.3.0"
    assert (work / "package.zip").is_file()
    assert (work / updater.READY_NAME).is_file()
    assert updater.read_staged(root=update_dir).version == "0.3.0"

    def boom(*_args, **_kwargs):
        raise AssertionError("已经就绪就不该再下载")

    second = updater.prepare_update(info, root=update_dir, opener=boom)
    assert second.app_dir == first.app_dir


def test_prepare_update_redownloads_when_sha_changes(update_dir, tmp_path):
    old_body = _zip_bytes()
    info = updater.UpdateInfo(
        version="0.3.0", url="http://x/pkg.zip", sha256=updater.sha256_file(_write(tmp_path / "a.zip", old_body)), size=len(old_body)
    )
    updater.prepare_update(info, root=update_dir, opener=lambda request, timeout=0: _Response(old_body))

    new_body = _zip_bytes() + b"changed"
    new_info = updater.UpdateInfo(
        version="0.3.0", url="http://x/pkg.zip", sha256=updater.sha256_file(_write(tmp_path / "b.zip", new_body)), size=len(new_body)
    )
    calls: list[int] = []

    def opener(request, timeout=0):
        calls.append(1)
        return _Response(new_body)

    staged = updater.prepare_update(new_info, root=update_dir, opener=opener)
    assert calls == [1]
    assert staged.info.sha256 == new_info.sha256


# --------------------------------------------------------------------------- #
# 换目录：成功 / 回滚
# --------------------------------------------------------------------------- #
def _make_dir(root: Path, marker: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / updater.EXE_NAME).write_text(marker)
    (root / "data.txt").write_text(marker)
    return root


class _FakeProc:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


def test_apply_update_swaps_and_marks_healthy(update_dir, tmp_path):
    target = _make_dir(tmp_path / "LanDocShare", "old")
    source = _make_dir(tmp_path / "staged" / "0.3.0" / "app" / "LanDocShare", "new")
    launched: list[Path] = []

    def launcher(exe, *, cwd):
        launched.append(Path(exe))
        updater.write_applied_marker("0.3.0")
        return _FakeProc()

    ok, message = updater.apply_update(
        source=source, target=target, expect_version="0.3.0", launcher=launcher, progress=lambda _t: None
    )
    assert ok, message
    assert (target / updater.EXE_NAME).read_text() == "new"
    assert (target / "data.txt").read_text() == "new"
    assert [path.read_text() for path in launched] == ["new"]
    assert not any(path.name.startswith("LanDocShare.old-") for path in tmp_path.iterdir())
    cleanup = json.loads((update_dir / updater.CLEANUP_NAME).read_text(encoding="utf-8"))
    assert any("0.3.0" in path for path in cleanup["paths"])


def test_apply_update_rolls_back_when_new_version_never_starts(update_dir, tmp_path):
    target = _make_dir(tmp_path / "LanDocShare", "old")
    source = _make_dir(tmp_path / "staged" / "0.3.0" / "app" / "LanDocShare", "new")
    procs: list[_FakeProc] = []

    def launcher(exe, *, cwd):
        proc = _FakeProc()
        procs.append(proc)
        return proc

    ok, message = updater.apply_update(
        source=source,
        target=target,
        expect_version="0.3.0",
        launcher=launcher,
        wait_ready=lambda _version: False,
        progress=lambda _t: None,
    )
    assert not ok
    assert "恢复旧版本" in message
    assert (target / updater.EXE_NAME).read_text() == "old"
    assert procs and procs[0].terminated is True
    assert len(procs) == 2  # 回滚后又把旧版拉起来了
    leftovers = [path.name for path in tmp_path.iterdir() if "LanDocShare." in path.name]
    assert leftovers == []


def test_apply_update_cancels_when_old_process_still_running(update_dir, tmp_path, monkeypatch):
    target = _make_dir(tmp_path / "LanDocShare", "old")
    source = _make_dir(tmp_path / "staged" / "0.3.0" / "app" / "LanDocShare", "new")
    monkeypatch.setattr(updater, "_wait_process_exit", lambda pid, timeout: False)
    ok, message = updater.apply_update(
        source=source, target=target, expect_version="0.3.0", wait_pid=4321
    )
    assert not ok
    assert "还在运行" in message
    assert (target / updater.EXE_NAME).read_text() == "old"


# --------------------------------------------------------------------------- #
# 健康标记 / 清理清单
# --------------------------------------------------------------------------- #
def test_marker_roundtrip(update_dir):
    assert updater.wait_for_marker("0.3.0", timeout=0.1) is False
    updater.write_applied_marker("0.3.0")
    assert updater.read_applied_marker()["version"] == "0.3.0"
    assert updater.wait_for_marker("0.3.0", timeout=0.1) is True


def test_run_pending_cleanup_only_touches_listed_paths(update_dir, tmp_path):
    stale = tmp_path / "updates" / "0.1.0"
    stale.mkdir()
    keep = tmp_path / "updates" / "0.2.0"
    keep.mkdir()
    updater.queue_cleanup([stale], root=update_dir)
    updater.queue_cleanup([stale], root=update_dir)  # 重复排队不该重复删
    removed = updater.run_pending_cleanup(root=update_dir)
    assert removed == 1
    assert not stale.exists()
    assert keep.is_dir()
    assert not (update_dir / updater.CLEANUP_NAME).exists()


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def test_checker_goes_ready_after_auto_download(update_dir, tmp_path):
    info = updater.UpdateInfo(version="0.3.0", url="http://x/p.zip", sha256="a" * 64, size=10)
    staged = updater.StagedUpdate(version="0.3.0", app_dir=tmp_path / "app")
    phases: list[dict] = []
    checker = updater.UpdateChecker(
        "http://x",
        fetch=lambda url, version: info,
        prepare=lambda item, progress=None: staged,
        on_change=phases.append,
    )
    assert checker.check() is info
    assert checker.staged == staged
    assert [item["phase"] for item in phases] == ["checking", "available", "downloading", "ready"]


def test_checker_manual_when_version_too_old(update_dir):
    info = updater.UpdateInfo(
        version="0.9.0", url="http://x/p.zip", sha256="a" * 64, min_from="0.5.0"
    )
    phases: list[dict] = []
    checker = updater.UpdateChecker(
        "http://x",
        fetch=lambda url, version: info,
        prepare=lambda item, progress=None: None,
        on_change=phases.append,
    )
    checker.check()
    assert phases[-1]["phase"] == "manual"
    assert "手动重装" in phases[-1]["message"]


def test_checker_reports_failure_without_raising(update_dir):
    def broken(*_args, **_kwargs):
        raise OSError("中心连不上")

    phases: list[dict] = []
    checker = updater.UpdateChecker("http://x", fetch=broken, on_change=phases.append)
    assert checker.check() is None
    assert phases[-1]["phase"] == ""
    assert "连不上" in phases[-1]["message"]


def test_checker_says_already_latest_instead_of_going_silent(update_dir):
    """没有新版本时也要留一句话：否则"检查更新"点下去毫无反馈。"""
    phases: list[dict] = []
    checker = updater.UpdateChecker("http://x", fetch=lambda url, version: None, on_change=phases.append)
    assert checker.check() is None
    assert phases[-1]["phase"] == ""
    assert "已是最新版本" in phases[-1]["message"]
    assert updater.__version__ in phases[-1]["message"]


def test_log_file_stays_inside_state_root(update_dir, monkeypatch):
    """跑测试时日志别写进真人档案：状态目录指到临时目录，日志也得跟着去。"""
    monkeypatch.setattr(updater, "local_app_data", lambda: Path("C:/Users/nobody/AppData/Local"))
    updater.log("测试写一行")
    written = update_dir / "logs" / "update.log"
    assert written.exists() and "测试写一行" in written.read_text(encoding="utf-8")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
