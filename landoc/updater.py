"""自动更新：检查 → 断点续传下载 → 校验 → 解压暂存 → 换目录重启 → 失败回滚。

为什么不需要单独的更新器：新版本的 `LanDocShare.exe` 自己就能当更新器——
更新包先解压到 `%LOCALAPPDATA%`，再用 `--apply-update` 从那里启动，
它等老进程退出后把整个程序目录换掉。这样老机器第一步不用先装一版"带更新器"的软件，
只要它已经是支持自动更新的版本（0.2.0 起），以后所有升级都由新版本自己完成。

约定（实现时踩过的坑，别改）：
- **不能删自己正在跑的目录**：更新器进程最后只能把"待删目录"写进 `cleanup.json`，
  由新版本启动后删（Windows 锁住 exe，`shutil.rmtree` 会留个空壳）。
- **不能先删旧目录再复制**：复制到同级的 `X.new-<ts>` 完成后再两次改名，任何时刻
  `X` 都是完整可运行的；复制失败时旧目录一点没动。
- **健康标记**：新版本启动时写 `updates/applied.json`；更新器等不到它（20 s）就回滚，
  所以"新版本一启动就崩"不会把同事的软件留在不可用状态。
- 下载和暂存都在用户目录，程序目录只读（如 Program Files）时也不会写到一半才失败。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from landoc import __version__

APP_NAME = "LanDocShare"
EXE_NAME = "LanDocShare.exe"
PLATFORM = "windows-x64"
PACKAGE_NAME = "package.zip"
MARKER_NAME = "applied.json"
READY_NAME = "ready.json"
CLEANUP_NAME = "cleanup.json"
STATE_DIR_NAME = "LanDocShare"

#: 检查新版本失败/没配中心时不要吓人，留空即可
CHECK_INTERVAL = 6 * 3600.0
INITIAL_DELAY = 25.0
CHUNK = 256 * 1024

Progress = Callable[[str], None] | None


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #
_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.\-]+))?\s*$")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`0.2.0` / `v0.2.0` / `0.2.0-beta1` → (0, 2, 0)；不是版本号返回 None。"""
    match = _VERSION_RE.match(str(text or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer(candidate: str, current: str) -> bool:
    """candidate 比 current 新才返回 True；任一方不是版本号时按字符串不等处理。"""
    left, right = parse_version(candidate), parse_version(current)
    if left is None or right is None:
        return bool(candidate) and candidate != current
    return left > right


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def local_app_data() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return Path(base) if base else Path.home() / "AppData" / "Local"


def state_root() -> Path:
    """更新状态总目录：可用 `LANDOC_UPDATE_DIR` 改（测试与绿色版用）。"""
    override = os.environ.get("LANDOC_UPDATE_DIR", "").strip()
    if override:
        return Path(override)
    return local_app_data() / STATE_DIR_NAME / "updates"


def updates_root() -> Path:
    return state_root()


def log_path() -> Path:
    return local_app_data() / STATE_DIR_NAME / "logs" / "update.log"


def log(message: str) -> None:
    """更新过程单独记一份日志：出问题时同事只要把 update.log 发回来。"""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1024 * 1024:
            path.replace(path.with_suffix(".log.1"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 版本信息
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str = ""
    size: int = 0
    notes: tuple[str, ...] = ()
    released_at: str = ""
    mandatory: bool = False
    min_from: str = ""
    platform: str = PLATFORM

    @classmethod
    def from_dict(cls, payload: dict, *, base_url: str = "") -> "UpdateInfo":
        download = str(payload.get("download") or "").strip()
        if download and not download.startswith(("http://", "https://")):
            download = base_url.rstrip("/") + "/" + download.lstrip("/")
        notes = payload.get("notes") or ()
        if isinstance(notes, str):
            notes = (notes,)
        sha = str(payload.get("sha256") or "").strip().lower()
        if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError(f"sha256 格式不对：{sha[:16]}…")
        return cls(
            version=str(payload.get("version") or "").strip(),
            url=download,
            sha256=sha,
            size=int(payload.get("size") or 0),
            notes=tuple(str(item) for item in notes),
            released_at=str(payload.get("released_at") or ""),
            mandatory=bool(payload.get("mandatory")),
            min_from=str(payload.get("min_from") or ""),
            platform=str(payload.get("platform") or PLATFORM),
        )

    @property
    def too_old(self) -> bool:
        """当前版本低于 min_from 时不能直接自动升级（跨太多版本只保留手动重装）。"""
        if not self.min_from:
            return False
        return is_newer(self.min_from, __version__)


def fetch_update_info(
    registry_url: str,
    current_version: str = __version__,
    *,
    timeout: float = 4.0,
    opener: Callable | None = None,
    platform: str = PLATFORM,
) -> UpdateInfo | None:
    """问登记中心有没有比本机新的版本；没有（或中心没发布版本清单）返回 None。"""
    base = str(registry_url or "").strip().rstrip("/")
    if not base:
        return None
    query = urllib.parse.urlencode({"version": current_version, "platform": platform})
    url = f"{base}/api/update?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": f"LanDocShare/{current_version}"})
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=timeout) as response:  # noqa: S310 - 地址来自本机配置
        payload = json.loads(response.read().decode("utf-8") or "{}")
    if not isinstance(payload, dict):
        return None
    raw = payload.get("update")
    if isinstance(raw, dict):
        candidate = dict(raw)
    else:
        # 中心现在的 /api/update 是扁平格式：update: bool + version/download/...
        if not raw and not payload.get("manual_only"):
            return None
        candidate = {
            key: payload[key]
            for key in ("version", "download", "sha256", "size", "notes", "released_at", "mandatory", "min_from", "platform")
            if payload.get(key) not in (None, "")
        }
        if payload.get("manual_only"):
            # 跨越太大时中心只给"手动重装"：把 min_from 当成本版要求，让上层走 manual 分支
            candidate.setdefault("min_from", str(candidate.get("version") or ""))
    info = UpdateInfo.from_dict(candidate, base_url=base)
    if not info.version or not info.url:
        return None
    if not is_newer(info.version, current_version):
        return None
    return info


# --------------------------------------------------------------------------- #
# 下载 / 校验 / 解压
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_package(
    info: UpdateInfo,
    dest_dir: Path,
    *,
    timeout: float = 60.0,
    opener: Callable | None = None,
    progress: Callable[[int, int], None] | None = None,
    attempts: int = 3,
    retry_wait: float = 1.5,
) -> Path:
    """下载到 `<dest_dir>/package.zip`，支持 Range 断点续传；校验不过就重下。"""
    work = Path(dest_dir)
    work.mkdir(parents=True, exist_ok=True)
    final = work / PACKAGE_NAME
    part = work / f"{PACKAGE_NAME}.part"
    if final.is_file() and (not info.sha256 or sha256_file(final) == info.sha256):
        return final
    if final.exists():
        final.unlink()

    open_url = opener or urllib.request.urlopen
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            start = part.stat().st_size if part.is_file() else 0
            if info.size and start > info.size:
                part.unlink()
                start = 0
            headers = {"User-Agent": f"LanDocShare/{__version__}"}
            if start:
                headers["Range"] = f"bytes={start}-"
            request = urllib.request.Request(info.url, headers=headers)
            with open_url(request, timeout=timeout) as response:  # noqa: S310
                status = int(getattr(response, "status", 200) or 200)
                if status == 206:
                    mode, done = "ab", start
                else:  # 服务器不认 Range，只能从头来
                    mode, done = "wb", 0
                total = info.size or (int(response.headers.get("Content-Length") or 0) + done)
                with part.open(mode) as handle:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
            size = part.stat().st_size
            if info.size and size != info.size:
                raise IOError(f"下载不完整：{size}/{info.size} 字节")
            if info.sha256:
                actual = sha256_file(part)
                if actual != info.sha256:
                    part.unlink(missing_ok=True)
                    raise ValueError(f"校验失败：sha256 是 {actual[:16]}…，应为 {info.sha256[:16]}…")
            os.replace(part, final)
            return final
        except Exception as exc:  # noqa: BLE001 - 重试后再抛
            last_error = exc
            log(f"下载失败（第 {attempt}/{attempts} 次）：{exc}")
            if attempt < attempts:
                time.sleep(retry_wait * attempt)
    assert last_error is not None
    raise last_error


def _safe_member(name: str) -> bool:
    """挡住 zip-slip：绝对路径、盘符、`..` 一律不许。"""
    text = name.replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return False
    parts = [part for part in text.split("/") if part]
    return bool(parts) and all(part not in ("..", ".") for part in parts)


def extract_package(zip_path: Path, dest_dir: Path) -> Path:
    """解压到 `<dest_dir>`，返回里面真正的程序目录（含 LanDocShare.exe）。"""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if not names:
            raise ValueError("更新包是空的")
        bad = [name for name in names if not _safe_member(name)]
        if bad:
            raise ValueError(f"更新包里有不安全的路径：{bad[0]}")
        for name in names:
            target = dest / name.replace("\\", "/")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, CHUNK)
    return find_app_root(dest)


def find_app_root(folder: Path) -> Path:
    """兼容两种包：顶层就是程序目录，或外面还套了一层 `LanDocShare/`。"""
    folder = Path(folder)
    if (folder / EXE_NAME).is_file():
        return folder
    nested = folder / APP_NAME
    if (nested / EXE_NAME).is_file():
        return nested
    raise FileNotFoundError(f"更新包里找不到 {EXE_NAME}：{folder}")


# --------------------------------------------------------------------------- #
# 暂存
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StagedUpdate:
    version: str
    app_dir: Path
    zip_path: Path | None = None
    info: UpdateInfo | None = None

    @property
    def exe(self) -> Path:
        return Path(self.app_dir) / EXE_NAME


def _version_dir(root: Path, version: str) -> Path:
    if parse_version(version) is None:
        raise ValueError(f"版本号不合法：{version!r}")
    return Path(root) / version


def prepare_update(
    info: UpdateInfo,
    *,
    root: Path | None = None,
    progress: Callable[[str], None] | None = None,
    force: bool = False,
    opener: Callable | None = None,
) -> StagedUpdate:
    """下载 + 校验 + 解压，可重复调用（已就绪就直接复用，不重复下 95 MB）。"""
    base = Path(root) if root is not None else updates_root()
    work = _version_dir(base, info.version)
    work.mkdir(parents=True, exist_ok=True)
    ready = _read_ready_file(work)
    if ready is not None and not force:
        if info.sha256 and ready.info and ready.info.sha256 != info.sha256:
            log(f"{info.version} 的包已变（sha 不同），重新下载")
        elif ready.exe.is_file():
            return ready

    if progress is not None:
        progress("正在下载新版本…")
    zip_path = download_package(
        info,
        work,
        progress=_percent_progress(progress, "正在下载新版本"),
        opener=opener,
    )
    if progress is not None:
        progress("正在解压…")
    app_dir = extract_package(zip_path, work / "app")
    staged = StagedUpdate(version=info.version, app_dir=app_dir, zip_path=zip_path, info=info)
    _write_ready_file(work, staged)
    cleanup_old_versions(base, keep=info.version)
    return staged


def _percent_progress(progress, label: str):
    if progress is None:
        return None
    state = {"bucket": -1}

    def report(done: int, total: int) -> None:
        if not total:
            return
        percent = min(100, int(done * 100 / total))
        bucket = percent // 5
        if bucket == state["bucket"]:
            return
        state["bucket"] = bucket
        progress(f"{label} {percent}%")

    return report


def _write_ready_file(work: Path, staged: StagedUpdate) -> None:
    payload = {
        "version": staged.version,
        "app_dir": str(staged.app_dir),
        "zip_path": str(staged.zip_path) if staged.zip_path else "",
        "staged_at": _now(),
        "sha256": staged.info.sha256 if staged.info else "",
    }
    _atomic_json(Path(work) / READY_NAME, payload)


def _read_ready_file(work: Path) -> StagedUpdate | None:
    try:
        payload = json.loads((Path(work) / READY_NAME).read_text(encoding="utf-8"))
        app_dir = Path(str(payload.get("app_dir") or ""))
        if not app_dir.is_absolute():
            app_dir = Path(work) / app_dir
        if not (app_dir / EXE_NAME).is_file():
            return None
        info = None
        if payload.get("sha256"):
            info = UpdateInfo(
                version=str(payload.get("version") or ""),
                url="",
                sha256=str(payload.get("sha256") or ""),
            )
        return StagedUpdate(
            version=str(payload.get("version") or ""),
            app_dir=app_dir,
            zip_path=Path(payload["zip_path"]) if payload.get("zip_path") else None,
            info=info,
        )
    except (OSError, ValueError, TypeError):
        return None


def read_staged(root: Path | None = None) -> StagedUpdate | None:
    """启动时找回上次已经下载好、但还没安装的版本。"""
    base = Path(root) if root is not None else updates_root()
    try:
        candidates = [
            path for path in base.iterdir() if path.is_dir() and parse_version(path.name)
        ]
    except OSError:
        return None
    candidates.sort(key=lambda path: parse_version(path.name) or (0, 0, 0), reverse=True)
    for path in candidates:
        staged = _read_ready_file(path)
        if staged is not None and staged.version == path.name:
            return staged
    return None


def cleanup_old_versions(root: Path, *, keep: str) -> None:
    """只在 <root> 里删"版本号命名的目录"，别的一概不碰。"""
    try:
        children = list(Path(root).iterdir())
    except OSError:
        return
    for path in children:
        if path.name == keep or not path.is_dir() or parse_version(path.name) is None:
            continue
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 健康标记 / 待删清单
# --------------------------------------------------------------------------- #
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _atomic_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def marker_path() -> Path:
    return updates_root() / MARKER_NAME


def write_applied_marker(version: str = __version__, path: Path | None = None) -> None:
    """新版本启动第一件事：写"我起来了"。更新器靠它决定是成功还是回滚。"""
    target = Path(path) if path is not None else marker_path()
    _atomic_json(
        target,
        {
            "version": version,
            "pid": os.getpid(),
            "exe": str(Path(sys.executable).resolve()),
            "applied_at": _now(),
        },
    )


def read_applied_marker(path: Path | None = None) -> dict:
    try:
        return json.loads((Path(path) if path is not None else marker_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wait_for_marker(
    version: str, *, timeout: float = 25.0, interval: float = 0.25, path: Path | None = None
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = read_applied_marker(path)
        if str(payload.get("version") or "") == version:
            return True
        time.sleep(interval)
    return False


def queue_cleanup(paths, *, root: Path | None = None) -> None:
    """把自己删不掉的目录（正在跑的 exe 所在目录）排进清单，交给新版本删。"""
    base = Path(root) if root is not None else updates_root()
    target = base / CLEANUP_NAME
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {"paths": []}
    existing = [str(item) for item in payload.get("paths") or []]
    for path in paths:
        text = str(Path(path))
        if text not in existing:
            existing.append(text)
    payload = {"paths": existing, "queued_at": _now()}
    _atomic_json(target, payload)


def run_pending_cleanup(*, root: Path | None = None) -> int:
    base = Path(root) if root is not None else updates_root()
    target = base / CLEANUP_NAME
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    removed = 0
    running = Path(sys.executable).resolve().parent
    for item in payload.get("paths") or []:
        path = Path(item)
        try:
            if not path.is_absolute() or path == running:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed += not path.exists()
            elif path.is_file():
                path.unlink(missing_ok=True)
        except OSError:
            continue
    target.unlink(missing_ok=True)
    return removed


# --------------------------------------------------------------------------- #
# 安装：拉起更新器 / 更新器本体
# --------------------------------------------------------------------------- #
CREATE_DETACHED = 0x00000008  # DETACHED_PROCESS
CREATE_NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP


def install_supported() -> bool:
    """源码目录里“替换程序目录”没有意义，只在打包后的 exe 里启用。"""
    return bool(getattr(sys, "frozen", False))


def current_install_dir() -> Path | None:
    if not install_supported():
        return None
    return Path(sys.executable).resolve().parent


def spawn_update(staged: StagedUpdate, *, target: Path, pid: int | None = None) -> subprocess.Popen:
    """从暂存目录启动新版本 exe，让它当更新器；本进程随后应尽快退出。"""
    exe = staged.exe
    if not exe.is_file():
        raise FileNotFoundError(f"暂存的程序不完整：{exe}")
    args = [
        str(exe),
        "--apply-update",
        "--wait-pid",
        str(pid if pid is not None else os.getpid()),
        "--target",
        str(Path(target)),
        "--source",
        str(staged.app_dir),
        "--expect-version",
        staged.version,
    ]
    env = dict(os.environ)
    env["LANDOC_UPDATE_FROM"] = __version__
    log(f"启动更新器：{' '.join(args)}")
    return subprocess.Popen(  # noqa: S603 - 参数全是本机路径
        args,
        cwd=str(staged.app_dir),
        env=env,
        close_fds=True,
        creationflags=CREATE_DETACHED | CREATE_NEW_GROUP,
    )


def _wait_process_exit(pid: int, timeout: float) -> bool:
    """等旧进程退出；等到返回 True。只用 OpenProcess+WaitForSingleObject，不杀进程。"""
    if not pid or pid == os.getpid() or sys.platform != "win32":
        return True
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return True  # 已经没了
    try:
        result = kernel32.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000))
        return result == 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _is_alive(proc) -> bool:
    try:
        return proc is not None and proc.poll() is None
    except Exception:  # noqa: BLE001
        return False


def _default_launcher(exe: Path, *, cwd: Path):
    return subprocess.Popen(  # noqa: S603
        [str(exe)],
        cwd=str(cwd),
        close_fds=True,
        creationflags=CREATE_DETACHED | CREATE_NEW_GROUP,
    )


def apply_update(
    *,
    source: Path,
    target: Path,
    expect_version: str,
    wait_pid: int = 0,
    launcher: Callable | None = None,
    wait_ready: Callable | None = None,
    progress: Callable[[str], None] | None = None,
    wait_pid_timeout: float = 90.0,
    ready_timeout: float = 25.0,
) -> tuple[bool, str]:
    """更新器本体：等旧进程退出 → 复制到同级 .new → 两次改名 → 启动新版 → 等健康标记。

    返回 (成功?, 说明)。失败时保证旧版本还在原来的位置并且被重新启动。
    依赖可注入（launcher / wait_ready），这样回滚路径能单测。
    """
    say = progress or (lambda _text: None)
    source = Path(source)
    target = Path(target)
    launch = launcher or _default_launcher
    probe_ready = wait_ready or (lambda version: wait_for_marker(version, timeout=ready_timeout))

    if not (source / EXE_NAME).is_file():
        return False, f"暂存目录不完整：{source}"
    if not target.is_dir():
        return False, f"程序目录不存在：{target}"
    if not _wait_process_exit(wait_pid, wait_pid_timeout):
        return False, "旧程序还在运行，升级已取消（先退出 LanDocShare 再试）"

    stamp = time.strftime("%Y%m%d-%H%M%S")
    new_dir = target.with_name(f"{target.name}.new-{stamp}")
    old_dir = target.with_name(f"{target.name}.old-{stamp}")
    try:
        free = shutil.disk_usage(str(target.parent)).free
        need = _tree_size(source)
        if free < need * 1.2:
            return False, f"磁盘空间不足：还需要约 {(need * 1.2 - free) / 1024 / 1024:.0f} MB"
        say("正在准备新版本…")
        if new_dir.exists():
            shutil.rmtree(new_dir, ignore_errors=True)
        _copy_tree(source, new_dir, say)
        say("正在切换到新版本…")
        target.rename(old_dir)
        try:
            new_dir.rename(target)
        except OSError:
            old_dir.rename(target)
            raise
    except Exception as exc:  # noqa: BLE001 - 换目录前失败：旧目录原封不动
        log(f"升级准备失败：{exc}")
        shutil.rmtree(new_dir, ignore_errors=True)
        return False, f"升级没做成，原程序未改动：{exc}"

    say("正在启动新版本…")
    proc = None
    try:
        proc = launch(target / EXE_NAME, cwd=target)
    except Exception as exc:  # noqa: BLE001
        log(f"启动新版本失败：{exc}")
        _restore(target, old_dir)
        return False, f"新版本启动失败，已恢复旧版本：{exc}"

    say("正在确认新版本可用…")
    healthy = bool(probe_ready(expect_version))
    if healthy:
        log(f"升级成功：{expect_version}")
        shutil.rmtree(old_dir, ignore_errors=True)
        leftovers = [_staging_version_dir(source)]
        if old_dir.exists():  # 有文件还被占着（杀毒软件/资源管理器预览），留给下次启动删
            leftovers.append(old_dir)
        queue_cleanup(leftovers)
        return True, "升级完成"
    log("新版本没写出健康标记，回滚")
    try:
        if proc is not None:
            proc.terminate()
            time.sleep(1.0)
    except Exception:  # noqa: BLE001
        pass
    failed_dir = target.with_name(f"{target.name}.failed-{stamp}")
    try:
        target.rename(failed_dir)
        old_dir.rename(target)
    except OSError as exc:
        log(f"回滚改名失败：{exc}")
        return False, f"新版本没起来，且回滚失败；请手动把 {old_dir} 改回 {target}"
    try:
        launch(target / EXE_NAME, cwd=target)
    except Exception:  # noqa: BLE001 - 启动旧版失败也只能提示
        pass
    shutil.rmtree(failed_dir, ignore_errors=True)
    if failed_dir.exists():
        queue_cleanup([failed_dir])
    queue_cleanup([_staging_version_dir(source)])
    return False, "新版本没能正常启动，已恢复旧版本"


def _staging_version_dir(source: Path) -> Path:
    """`updates/0.2.0/app/LanDocShare` → `updates/0.2.0`（整个版本目录都能删）。"""
    parent = Path(source).parent
    if parent.name == "app" and parse_version(parent.parent.name):
        return parent.parent
    return parent


def _restore(target: Path, old_dir: Path) -> None:
    try:
        target.rename(target.with_name(target.name + ".failed"))
    except OSError:
        pass
    try:
        old_dir.rename(target)
    except OSError:
        pass


def _tree_size(folder: Path) -> int:
    total = 0
    for path in Path(folder).rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _copy_tree(source: Path, dest: Path, say) -> None:
    """按文件复制并报进度；比 copytree 多一个"到哪一步了"的显示。"""
    files = [path for path in Path(source).rglob("*") if path.is_file()]
    total = max(1, len(files))
    dest.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(files, start=1):
        relative = path.relative_to(source)
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if index % 25 == 0 or index == total:
            say(f"正在复制新版本… {index * 100 // total}%")
    # 空目录也复制过去（onedir 里通常没有，但别丢）
    for path in Path(source).rglob("*"):
        if path.is_dir():
            (dest / path.relative_to(source)).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# 更新器入口（--apply-update）
# --------------------------------------------------------------------------- #
class _Splash:
    """更新过程中的小窗口：不追求好看，只要用户知道"还在动、别关"。"""

    def __init__(self, version: str) -> None:
        self.root = None
        self.label = None
        self.bar = None
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.title(f"LanDocShare 升级到 v{version}")
            root.geometry("420x140")
            root.resizable(False, False)
            root.attributes("-topmost", True)
            root.protocol("WM_DELETE_WINDOW", lambda: None)  # 升级中不许关
            try:
                root.eval("tk::PlaceWindow . center")
            except Exception:  # noqa: BLE001
                pass
            ttk.Label(root, text=f"正在升级到 v{version}，请不要关闭…", padding=16).pack(anchor="w")
            self.label = ttk.Label(root, text="准备中…", padding=(16, 0))
            self.label.pack(anchor="w")
            self.bar = ttk.Progressbar(root, mode="indeterminate", length=380)
            self.bar.pack(padx=16, pady=12)
            self.bar.start(80)
            self.root = root
            root.update()
        except Exception as exc:  # noqa: BLE001 - 没有桌面就静默升级
            log(f"升级窗口打不开（继续静默升级）：{exc}")
            self.root = None

    def set(self, text: str) -> None:
        if self.root is None:
            return
        try:
            self.label.configure(text=text)
            self.root.update()
        except Exception:  # noqa: BLE001
            pass

    def error(self, message: str) -> None:
        if self.root is None:
            return
        try:
            from tkinter import messagebox

            messagebox.showerror("升级失败", message)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self.root is None:
            return
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def apply_update_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="landoc-updater", description="LanDocShare 更新器（内部用）")
    parser.add_argument("--apply-update", action="store_true")
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--target", required=True)
    parser.add_argument("--source", default="")
    parser.add_argument("--expect-version", required=True)
    parser.add_argument("--silent", action="store_true")
    args = parser.parse_args([item for item in argv if item != "--apply-update"])

    source = Path(args.source) if args.source else Path(sys.executable).resolve().parent
    target = Path(args.target)
    log(f"更新器启动：{__version__} → 目标 {target}（等待 pid {args.wait_pid}）")
    splash = _Splash(args.expect_version) if not args.silent else None
    try:
        ok, message = apply_update(
            source=source,
            target=target,
            expect_version=args.expect_version,
            wait_pid=args.wait_pid,
            progress=splash.set if splash is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都别让窗口无声消失
        log(f"更新器异常：{exc!r}")
        if splash is not None:
            splash.error(f"升级出错：{exc}")
        return 2
    log(message)
    if splash is not None:
        if not ok:
            splash.error(message)
        splash.close()
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 客户端侧的小状态机（LibraryApp 用）
# --------------------------------------------------------------------------- #
class UpdateChecker:
    """后台定时检查 + 自动下载；状态通过回调交给 LibraryApp 显示。

    单独成类是为了能脱开 http 单测：fetch/prepare 都可注入。
    """

    def __init__(
        self,
        registry_url: str,
        *,
        fetch: Callable | None = None,
        prepare: Callable | None = None,
        interval: float = CHECK_INTERVAL,
        initial_delay: float = INITIAL_DELAY,
        auto_download: bool = True,
        on_change: Callable[[dict], None] | None = None,
    ) -> None:
        self.registry_url = registry_url
        self._fetch = fetch or fetch_update_info
        self._prepare = prepare or prepare_update
        self.interval = interval
        self.initial_delay = initial_delay
        self.auto_download = auto_download
        self.on_change = on_change
        self.staged: StagedUpdate | None = None
        self.info: UpdateInfo | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()  # check() 里会再进 download()，必须可重入

    def _emit(self, **fields) -> None:
        if self.on_change is not None:
            try:
                self.on_change(fields)
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        if not self.registry_url:
            return
        self._thread = threading.Thread(target=self._loop, name="landoc-update", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self.initial_delay > 0:
            self._stop.wait(self.initial_delay * random.uniform(0.7, 1.3))
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001 - 更新检查失败绝不影响检索
                log(f"检查更新失败：{exc}")
            self._stop.wait(self.interval * random.uniform(0.9, 1.1))

    def check(self) -> UpdateInfo | None:
        if not self.registry_url:
            return None
        with self._lock:
            self._emit(phase="checking", message="正在检查新版本…")
            try:
                info = self._fetch(self.registry_url, __version__)
            except Exception as exc:  # noqa: BLE001
                self._emit(phase="", message=f"检查更新失败：{exc}")
                return None
            if info is None:
                self._emit(phase="", version="", message="")
                return None
            self.info = info
            note = f"发现新版本 v{info.version}"
            if info.too_old:
                note = f"v{info.version} 需要手动重装（版本跨度太大）"
                self._emit(phase="manual", version=info.version, message=note)
                return info
            self._emit(phase="available", version=info.version, message=note)
            if self.auto_download:
                return self.download()
            return info

    def download(self) -> UpdateInfo | None:
        info = self.info
        if info is None:
            return None
        with self._lock:
            self._emit(phase="downloading", version=info.version, message="正在下载新版本…")
            try:
                staged = self._prepare(
                    info,
                    progress=lambda text: self._emit(
                        phase="downloading", version=info.version, message=text
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                log(f"下载/解压失败：{exc}")
                self._emit(phase="failed", version=info.version, message=f"更新下载失败：{exc}")
                return info
            self.staged = staged
            self._emit(
                phase="ready",
                version=info.version,
                message=f"v{info.version} 已下载，可升级",
            )
            return info

    def adopt_existing(self) -> None:
        """启动时找回上次下载好的版本（下载完没点升级就关机的场景）。"""
        try:
            staged = read_staged()
        except Exception:  # noqa: BLE001
            staged = None
        if staged is None:
            return
        self.staged = staged
        self._emit(
            phase="ready",
            version=staged.version,
            message=f"v{staged.version} 已下载，可升级",
        )


__all__ = [
    "APP_NAME",
    "EXE_NAME",
    "PLATFORM",
    "UpdateInfo",
    "StagedUpdate",
    "UpdateChecker",
    "apply_update",
    "apply_update_main",
    "current_install_dir",
    "download_package",
    "extract_package",
    "fetch_update_info",
    "install_supported",
    "is_newer",
    "parse_version",
    "prepare_update",
    "queue_cleanup",
    "read_staged",
    "run_pending_cleanup",
    "sha256_file",
    "spawn_update",
    "wait_for_marker",
    "write_applied_marker",
]
