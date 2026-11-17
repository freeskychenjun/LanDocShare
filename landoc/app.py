"""库软件的运行核心：本地检索服务 + 空闲门控的建库/增量更新。

托盘程序（`landoc/tray.py`）只负责界面，真正的服务与调度都在这里，
这样它也能被测试直接驱动，不必真的起一个托盘图标。
"""

from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass
from typing import Callable

from landoc import __version__
from landoc import idle as idle_module
from landoc import pipeline, updater
from landoc.cli import pick_port
from landoc.config import Config


def _post_json(url: str, payload: dict, timeout: float = 3.0) -> dict:
    """POST 一段 JSON 并读回响应；失败抛异常，由调用方决定吞不吞。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 地址来自本机配置
        return json.loads(resp.read().decode("utf-8") or "{}")


@dataclass
class AppStatus:
    phase: str = "starting"  # starting / serving / converting / paused / error / stopped
    doc_count: int = 0
    last_scan: str = ""
    message: str = ""
    port: int = 0
    paused: bool = False
    converted: int = 0  # 本次空闲窗口里转换成功的篇数
    registry: str = ""  # 登记中心的状态文字（空 = 没配中心）
    version: str = __version__  # 本机程序版本
    update_version: str = ""  # 中心上的新版本（空 = 没发现）
    update_phase: str = ""  # "" / checking / available / downloading / ready / failed / manual / installing
    update_message: str = ""

    def summary(self) -> str:
        if self.phase == "converting":
            return f"建库中：已转换 {self.converted} 篇"
        if self.phase == "paused":
            return "已暂停共享"
        if self.phase == "error":
            return f"出错：{self.message}"
        if self.update_phase == "ready" and self.update_version:
            return f"我的库：{self.doc_count} 篇 · 可升级 v{self.update_version}"
        return f"我的库：{self.doc_count} 篇 · 上次 {self.last_scan or '从未'}"


class LibraryApp:
    """起服务 + 后台按空闲门控跑建库批次。

    依赖都可注入（idle_check / runner / server_factory），测试里换成假的即可。
    """

    def __init__(
        self,
        cfg: Config,
        *,
        index=None,
        idle_check: Callable[[], bool] | None = None,
        runner: Callable | None = None,
        server_factory: Callable | None = None,
        batch: int = 20,
        poll_seconds: float = 30.0,
        idle_minutes: float = 5.0,
        on_status: Callable[[AppStatus], None] | None = None,
        on_notify: Callable[[str], None] | None = None,
        poster: Callable[[str, dict], dict] | None = None,
        heartbeat_seconds: float = 60.0,
        update_fetch: Callable | None = None,
        update_prepare: Callable | None = None,
        update_interval: float = updater.CHECK_INTERVAL,
        update_initial_delay: float = updater.INITIAL_DELAY,
    ) -> None:
        self.cfg = cfg
        self.index = index
        self._idle_check = idle_check or (lambda: idle_module.is_idle(idle_minutes))
        self._runner = runner or (
            lambda: pipeline.run(self.cfg, index=self.index, limit=batch)
        )
        self._server_factory = server_factory
        self._batch = batch
        self._poll = poll_seconds
        self._on_status = on_status
        self._on_notify = on_notify
        self._poster = poster or _post_json
        self._heartbeat_seconds = heartbeat_seconds
        self._update_fetch = update_fetch
        self._update_prepare = update_prepare
        self._update_interval = update_interval
        self._update_initial_delay = update_initial_delay
        self.last_register: dict | None = None
        self._updates: updater.UpdateChecker | None = None

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = False
        self._saved_cidrs: list[str] | None = None
        self._lock = threading.Lock()
        self._status = AppStatus()
        self._threads: list[threading.Thread] = []
        self.httpd = None

    # ---- 生命周期 ----
    def start(self) -> None:
        from landoc.index import Index
        from landoc.server import make_server

        self._stop.clear()
        self._threads.clear()
        self._update(phase="starting", version=__version__)
        if updater.install_supported():
            # 更新器正在等这个标记：新版本一起来就写，等不到它会自动回滚
            updater.write_applied_marker(__version__)
        self.index = self.index or Index(
            self.cfg.db_path,
            snippet_tokens=self.cfg.snippet_tokens,
            md_root=self.cfg.md_dir,
        )
        port = pick_port(self.cfg.port) if self._server_factory is None else self.cfg.port
        if self._server_factory is None:
            self.httpd = make_server(self.cfg, index=self.index, port=port)
        else:
            self.httpd = self._server_factory(self.cfg)
        actual = self.httpd.server_address[1]
        try:
            self.index.set_meta("port", str(actual))
        except Exception:  # noqa: BLE001 - 端口写不进去不影响服务
            pass
        # 启动时先把现有篇数/上次扫描显示出来：已经建好的库不会立刻触发转换，
        # 不初始化的话状态窗口会一直显示"0 篇 / 还没扫过"，看着像没建库。
        try:
            total = self.index.stats().get("total", 0)
            state = pipeline._read_state(self.cfg)
            self._update(phase="serving", port=actual, doc_count=total, last_scan=state.get("last_scan_ts", ""))
        except Exception:  # noqa: BLE001 - 读不到就用默认值，别拦住启动
            self._update(phase="serving", port=actual)
        self._spawn(self.httpd.serve_forever, "landoc-server")
        self._spawn(self._worker, "landoc-worker")
        if str(getattr(self.cfg, "registry_url", "") or "").strip():
            self._spawn(self._heartbeat, "landoc-heartbeat")
        self._start_updates()

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._updates is not None:
            self._updates.stop()
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        for thread in self._threads:
            thread.join(timeout=3)
        self._update(phase="stopped")

    # ---- 托盘动作 ----
    def update_now(self) -> None:
        self._wake.set()

    @property
    def paused(self) -> bool:
        return self._paused

    def pause_share(self) -> None:
        if self._paused:
            return
        self._saved_cidrs = list(self.cfg.allow_cidrs)
        self.cfg.allow_cidrs = []
        self._paused = True
        self._update(phase="paused", paused=True)

    def resume_share(self) -> None:
        if not self._paused:
            return
        if self._saved_cidrs is not None:
            self.cfg.allow_cidrs = list(self._saved_cidrs)
        self._paused = False
        self._update(phase="serving", paused=False)

    def toggle_share(self) -> None:
        self.resume_share() if self._paused else self.pause_share()

    # ---- 自动更新 ----
    @property
    def staged_update(self) -> updater.StagedUpdate | None:
        return self._updates.staged if self._updates is not None else None

    @property
    def update_ready(self) -> bool:
        status = self.status
        return status.update_phase == "ready" and bool(status.update_version)

    def _ensure_updates(self) -> updater.UpdateChecker | None:
        url = str(getattr(self.cfg, "registry_url", "") or "").strip()
        if not url:
            return None
        if self._updates is None:
            self._updates = updater.UpdateChecker(
                url,
                fetch=self._update_fetch,
                prepare=self._update_prepare,
                interval=self._update_interval,
                initial_delay=self._update_initial_delay,
                auto_download=bool(getattr(self.cfg, "auto_download", True)),
                on_change=self._on_update_change,
            )
        return self._updates

    def _start_updates(self) -> None:
        """启动后台检查；已经下载好的版本先认领，别让同事重启一次就白下 95 MB。"""
        if not getattr(self.cfg, "check_updates", True):
            return
        checker = self._ensure_updates()
        if checker is None:
            return
        checker.adopt_existing()
        checker.start()

    def _on_update_change(self, fields: dict) -> None:
        """更新器的字段名（phase/version/message）和状态窗口的不一样，这里翻译一次。

        直接 `_update(**fields)` 会把服务状态 phase 和本机版本 version 覆盖掉，
        表现为"版本号变成新版本了、却没有升级提示"。
        """
        mapping = {
            "phase": "update_phase",
            "version": "update_version",
            "message": "update_message",
        }
        mapped = {target: fields[source] for source, target in mapping.items() if source in fields}
        if mapped:
            self._update(**mapped)

    def check_updates_async(self) -> None:
        """托盘"检查更新"用：不阻塞界面，结果通过状态字段显示。"""
        checker = self._ensure_updates()
        if checker is None:
            self._update(update_message="没配置登记中心，无法检查更新")
            return
        threading.Thread(
            target=checker.check, name="landoc-update-check", daemon=True
        ).start()

    def download_update_async(self) -> None:
        checker = self._ensure_updates()
        if checker is None or checker.info is None:
            return
        threading.Thread(
            target=checker.download, name="landoc-update-download", daemon=True
        ).start()

    def install_update(self) -> str:
        """「升级」按钮：停服务 → 拉起暂存的新版本当更新器 → 本进程退出。

        返回 "ok" 表示更新器已起来（调用方应立即退出）；其他字符串是给用户看的原因。
        """
        staged = self.staged_update
        if staged is None:
            return "新版本还没下载好"
        if self.status.phase == "converting":
            return "正在建库，等这一批转换完再升级"
        if not updater.install_supported():
            return "源码运行时不会替换程序；请打包后再用自动升级"
        target = updater.current_install_dir()
        if target is None:
            return "找不到程序目录"
        self._update(update_phase="installing", update_message="正在重启升级…")
        self.stop()
        try:
            updater.spawn_update(staged, target=target)
        except Exception as exc:  # noqa: BLE001 - 起不来就把服务恢复，别让库停了
            self.start()
            return f"启动更新器失败：{exc}"
        return "ok"

    def local_url(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self._status.port}/"

    @property
    def status(self) -> AppStatus:
        with self._lock:
            return AppStatus(**self._status.__dict__)

    # ---- 后台调度 ----
    def _worker(self) -> None:
        while not self._stop.is_set():
            if self._paused:
                self._update(phase="paused", paused=True)
                self._wait(self._poll)
                continue
            if not self._idle():
                self._update(phase="serving")
                self._wait(self._poll)
                continue
            converted = self._drain_while_idle()
            if converted and self._on_notify is not None:
                try:
                    self._on_notify(f"已收录 {converted} 篇，可被检索")
                except Exception:  # noqa: BLE001 - 通知失败不该拖垮调度
                    pass
            self._update(phase="serving", converted=0)
            self._wait(self._poll)

    def _drain_while_idle(self) -> int:
        total = 0
        while not self._stop.is_set() and not self._paused and self._idle():
            try:
                report = self._runner()
            except Exception as exc:  # noqa: BLE001 - 单轮失败等下一轮
                self._update(phase="error", message=f"{exc.__class__.__name__}: {exc}")
                return total
            total += report.converted
            self._refresh_counts()
            self._update(phase="converting", converted=total)
            if report.converted == 0:
                break
            self._wake.clear()
        return total

    def _refresh_counts(self) -> None:
        try:
            stats = self.index.stats() if self.index is not None else {"total": 0}
            state = pipeline._read_state(self.cfg)
        except Exception:  # noqa: BLE001
            return
        self._update(doc_count=stats.get("total", 0), last_scan=state.get("last_scan_ts", ""))

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    # ---- 登记中心心跳 ----
    def _register_payload(self) -> dict:
        status = self.status
        return {
            "name": self.cfg.name,
            "lib_name": self.cfg.name,
            "port": status.port or self.cfg.port,
            "doc_count": status.doc_count,
            "version": __version__,
            # 中心按这两项决定"谁能看到这个库"；没上报的中心会把库藏起来。
            "allow_cidrs": list(self.cfg.allow_cidrs or []),
            "allow_people": dict(self.cfg.allow_people or {}),
        }

    def _heartbeat(self) -> None:
        """每 60 s 向登记中心报一次"我在线、我有多少篇"；中心挂了不影响检索。"""
        url = str(self.cfg.registry_url).strip().rstrip("/") + "/register"
        while not self._stop.is_set():
            try:
                reply = self._poster(url, self._register_payload())
                self.last_register = reply
                stamp = str((reply or {}).get("server_time") or "")
                self._update(registry=f"已登记 {stamp}".strip())
            except Exception:  # noqa: BLE001 - 中心不可达就跳过这一轮
                self.last_register = None
                self._update(registry="连不上登记中心")
            self._stop.wait(self._heartbeat_seconds)

    def _idle(self) -> bool:
        try:
            return bool(self._idle_check())
        except Exception:  # noqa: BLE001 - 判定失败按"不空闲"，宁可不跑
            return False

    def _update(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._status, key, value)
            snapshot = AppStatus(**self._status.__dict__)
        if self._on_status is not None:
            try:
                self._on_status(snapshot)
            except Exception:  # noqa: BLE001
                pass
