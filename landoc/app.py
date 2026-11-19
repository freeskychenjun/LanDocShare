"""库软件的运行核心：本地检索服务 + 空闲门控的建库/增量更新。

托盘程序（`landoc/tray.py`）只负责界面，真正的服务与调度都在这里，
这样它也能被测试直接驱动，不必真的起一个托盘图标。

建库默认只在用户离开电脑时跑（`idle`）；托盘「立即重建索引」是例外：
`update_now()` 越过门控、一路跑到没有新增为止（见 `_drain`）。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
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


_LOG_MAX_BYTES = 1024 * 1024
_LOG_LOCK = threading.Lock()


def app_log_path(lib_dir) -> Path:
    """状态窗口给库主人看的是"最后一句话"，一重启就没了；细节落到 logs/app.log。"""
    return Path(lib_dir) / "logs" / "app.log"


def log_event(lib_dir, message: str) -> None:
    """写一行运行日志（超过 1 MB 滚成 .1）；写不进去就算了，不能让日志拖垮服务。"""
    path = app_log_path(lib_dir)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    try:
        with _LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
                path.replace(path.with_name(path.name + ".1"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


@dataclass
class AppStatus:
    phase: str = "starting"  # starting / serving / converting / paused / error / stopped
    doc_count: int = 0
    last_scan: str = ""
    message: str = ""
    port: int = 0
    paused: bool = False
    converted: int = 0  # 本次建库（空闲窗口或手动触发）里转换成功的篇数
    pending: int = 0  # 本轮还剩多少篇没转（0 = 不知道/没有积压）
    eta_seconds: int = 0  # 按实测速度估的剩余秒数（0 = 还没测出速度）
    gone: int = 0  # 源文件已不在、内容仍留在库里的篇数（库只增不减，见 pipeline.run）
    failed: int = 0  # 转换失败、还等着重试的篇数（state.json 的 failures）
    registry: str = ""  # 登记中心的状态文字（空 = 没配中心）
    version: str = __version__  # 本机程序版本
    update_version: str = ""  # 中心上的新版本（空 = 没发现）
    update_phase: str = ""  # "" / checking / available / downloading / ready / failed / manual / installing
    update_message: str = ""

    def summary(self) -> str:
        if self.phase == "converting":
            text = f"建库中：已转换 {self.converted} 篇"
            # 上千篇时只报"已转换 N 篇"会被当成卡死：有总数就补上剩余与预计时间
            if self.pending > 0:
                text += f" · 还剩 {self.pending} 篇"
                if self.eta_seconds > 0:
                    text += f"（{eta_phrase(self.eta_seconds)}）"
            return text
        if self.phase == "paused":
            return "已暂停共享"
        if self.phase == "error":
            return f"出错：{self.message}"
        tail = f" · {self.gone} 篇源已不在" if self.gone else ""
        if self.failed:
            tail += f" · {self.failed} 篇转换失败"
        if self.update_phase == "ready" and self.update_version:
            return f"我的库：{self.doc_count} 篇{tail} · 可升级 v{self.update_version}"
        return f"我的库：{self.doc_count} 篇{tail} · 上次 {self.last_scan or '从未'}"


def human_eta(seconds: float) -> str:
    """把剩余秒数说成人话（状态窗口与短消息共用）。"""
    seconds = int(seconds)
    if seconds <= 0:
        return "不到 1 分钟"
    if seconds < 60:
        return "不到 1 分钟"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if minutes and hours < 10:
        return f"{hours} 小时 {minutes} 分"
    return f"{hours} 小时"


def eta_phrase(seconds: float) -> str:
    """带"约"的说法：「约 2 分钟」，但「不到 1 分钟」前面不加"约"（读着别扭）。"""
    text = human_eta(seconds)
    return text if text.startswith("不到") else f"约 {text}"


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
        batch: int = 100,
        round_seconds: float = 600.0,
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
        self._runner = runner or self._run_round
        self._server_factory = server_factory
        self._batch = batch
        self._round_seconds = round_seconds
        self._round_base = 0  # 本轮之前累计转换的篇数（给进度回调换算总数）
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
        # 手动「立即重建索引」的请求：不认空闲门控，跑到没有新增为止（见 _drain）
        self._force = threading.Event()
        # 手动建库的"停下"请求：空闲任务用户一回来自然就停，手动跑就得有个停的按钮
        self._abort = threading.Event()
        self._stop_request = False  # 用户点过「停止建库」：停下后要回一句话
        self._paused = False
        self._saved_cidrs: list[str] | None = None
        self._lock = threading.Lock()
        self._status = AppStatus()
        self._threads: list[threading.Thread] = []
        self.httpd = None
        self._registry_ok: bool | None = None  # None = 还没试过（用来只记状态切换）

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
        log_event(self.cfg.lib_dir, f"库服务启动：端口 {actual}，版本 {__version__}")
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
        """「立即重建索引」：马上跑，不等用户离开电脑。

        早先这里只 `_wake.set()`，worker 仍要先过 `_idle()` —— 正在用电脑时点它
        什么都不会发生，菜单名和实际行为是两回事。现在置 `_force`，本轮门控与
        每篇的收工检查都放行，一直跑到没有新增为止（转换起的是独立隐藏 Office
        进程，不碰用户正开着的窗口，只是花 CPU）。
        """
        self._force.set()
        self._wake.set()

    def prune_gone(self) -> int:
        """删掉"源文件已不在"的文档（状态窗「清理…」按钮），返回删了几篇。

        这是删库的唯一出口：扫描器自己从不删东西，库要能整份拷到另一台机器上。
        """
        removed = pipeline.prune_gone(self.cfg, index=self.index)
        if removed:
            log_event(self.cfg.lib_dir, f"清理源文件已不在的文档：{removed} 篇")
        self._refresh_counts()
        return removed

    def stop_building(self) -> None:
        """停下正在进行的建库（托盘/状态窗在"建库中"时把按钮换成这个）。"""
        self._force.clear()
        self._abort.set()
        self._stop_request = True  # 停下来之后要说一句，别让用户盯着不动的状态栏猜
        self._wake.set()

    @property
    def building(self) -> bool:
        """是不是正在转换（界面据此把「立即」换成「停止建库」）。"""
        return self.status.phase == "converting"

    @property
    def paused(self) -> bool:
        return self._paused

    def pause_share(self) -> None:
        if self._paused:
            return
        self._saved_cidrs = list(self.cfg.allow_cidrs)
        self.cfg.allow_cidrs = []
        self._paused = True
        # 暂停就是"先别干活"：连手动建库的请求一起撤掉，否则恢复共享时会突然开跑
        self._force.clear()
        self._abort.clear()
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
        if fields.get("phase") == "ready" and fields.get("version"):
            self._notify_user(
                f"新版本 v{fields['version']} 已下载好，右键托盘图标可以升级"
            )

    def check_updates_async(self, *, announce: bool = False) -> None:
        """托盘"检查更新"用：不阻塞界面，结果通过状态字段显示。

        announce=True 时，检查完若没有可升级的新版本就弹一条通知：否则用户点了
        菜单看不到任何变化，会以为这个按钮坏了（"已是最新版本"必须说出来）。
        """
        checker = self._ensure_updates()
        if checker is None:
            message = "没配置登记中心，无法检查更新"
            self._update(update_message=message)
            if announce:
                self._notify_user(message)
            return

        def _run() -> None:
            info = checker.check()
            if not announce or info is not None:
                return
            message = self.status.update_message or f"已是最新版本（v{__version__}）"
            self._notify_user(message)

        threading.Thread(target=_run, name="landoc-update-check", daemon=True).start()

    def _notify_user(self, message: str) -> None:
        """气泡提示；没有通知通道（无托盘/单测）时静默。"""
        if self._on_notify is None:
            return
        try:
            self._on_notify(message)
        except Exception:  # noqa: BLE001 - 通知失败不该影响主流程
            pass

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
                self._force.clear()
                self._abort.clear()
                self._update(phase="paused", paused=True)
                self._wait(self._poll)
                continue
            clicked = self._force.is_set()  # 这一轮是用户点了「立即重建索引」吗
            if not clicked and not self._idle():
                self._update(phase="serving")
                self._wait(self._poll)
                continue
            converted, failed = self._drain()
            # 手动点的那次必须有回音：没新增也要说一句，否则用户以为按钮坏了
            stopped = self._stop_request  # 这一轮是被「停止建库」喊停的吗
            self._stop_request = False
            message = ""
            if converted:
                message = f"已收录 {converted} 篇，可被检索"
            elif stopped:
                # 停下时还有积压：既别说"已是最新"，也别静悄悄什么都不说
                message = "已停止建库，已收录的文档照常可检索"
            elif clicked and failed:
                message = f"没有新增；{failed} 篇转换失败，详见 failures.json"
            elif clicked and self.status.gone:
                # 源文档不在了但副本还在：光说"已是最新"会让人以为库被清过
                message = f"已是最新；{self.status.gone} 篇源文件已不在，内容仍在库中"
            elif clicked:
                message = "已是最新：没有需要收录的新文档"
            if message:
                self._notify_user(message)
            self._update(phase="serving", converted=0)
            self._wait(self._poll)

    def _run_round(self):
        """一轮建库：最多 `batch` 篇「尝试」，最长 `round_seconds` 秒，用户回来就收工。

        早先这里只传 `limit=batch`，而 limit 数的是**成功**篇数：一批 100 篇里
        全是带口令/损坏文件时，单次 run 会一直转下去，用户坐下用电脑也停不下来。
        """
        return pipeline.run(
            self.cfg,
            index=self.index,
            limit=self._batch,
            budget=self._round_seconds,
            should_stop=self._should_stop_round,
            on_progress=self._on_round_progress,
        )

    def _should_stop_round(self) -> bool:
        """每篇开始前问一次：该收工了吗（停止/暂停/手动的"停下"/用户回来了）。

        手动「立即重建索引」期间不看空闲：用户点了就是要现在跑，否则正在用电脑时
        连一篇都转不了（这正是旧版"点了没反应"的根因）。
        """
        if self._stop.is_set() or self._paused or self._abort.is_set():
            return True
        return not self._force.is_set() and not self._idle()

    def _on_round_progress(self, report) -> None:
        """转换途中刷状态：已转换按"本轮之前 + 本次"算，另报剩余篇数与 ETA。"""
        self._update(
            phase="converting",
            converted=self._round_base + report.converted,
            pending=report.remaining,
            eta_seconds=int(report.eta_seconds),
        )

    def _drain(self) -> tuple[int, int]:
        """连着跑轮次，直到没活干；返回 (本次共收录篇数, 本次失败篇数)。

        空闲时按门控跑，用户一回来就停；手动「立即重建索引」（`_force`）时不受
        空闲限制，一直跑到某轮既没转换也没尝试（= 没有新增）为止。
        """
        total = 0
        failed = 0
        try:
            while not self._stop.is_set() and not self._paused and not self._abort.is_set():
                if not (self._force.is_set() or self._idle()):
                    break
                self._round_base = total
                try:
                    report = self._runner()
                except Exception as exc:  # noqa: BLE001 - 单轮失败等下一轮
                    message = f"{exc.__class__.__name__}: {exc}"
                    log_event(self.cfg.lib_dir, f"扫描失败：{message}")
                    self._update(phase="error", message=message)
                    return total, failed
                total += report.converted
                failed += len(report.failed or [])
                # 每轮一行运行日志：状态窗口只留"最后一句话"，重启就没了，排障要看这个
                if report.attempted:
                    log_event(
                        self.cfg.lib_dir,
                        f"一轮结束：收录 {report.converted} 篇、失败 {len(report.failed or [])} 篇、"
                        f"尝试 {report.attempted} 篇、剩余 {report.remaining} 篇，"
                        f"用时 {report.elapsed:.0f} 秒（{report.stop_reason or '-'}）",
                    )
                self._refresh_counts()
                self._update(
                    phase="converting",
                    converted=total,
                    pending=report.remaining,
                    eta_seconds=int(report.eta_seconds),
                )
                if report.converted == 0 and not report.attempted:
                    break
                self._wake.clear()
            return total, failed
        finally:
            self._force.clear()
            self._abort.clear()

    def _refresh_counts(self) -> None:
        try:
            stats = self.index.stats() if self.index is not None else {"total": 0}
            state = pipeline._read_state(self.cfg)
        except Exception:  # noqa: BLE001
            return
        failures = state.get("failures") or {}
        self._update(
            doc_count=stats.get("total", 0),
            last_scan=state.get("last_scan_ts", ""),
            gone=pipeline.missing_count(self.cfg),
            failed=sum(1 for item in failures.values() if isinstance(item, dict)),
        )

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
            "deny_cidrs": list(getattr(self.cfg, "deny_cidrs", []) or []),
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
                if self._registry_ok is False:
                    log_event(self.cfg.lib_dir, "登记中心恢复连接")
                self._registry_ok = True
                self._update(registry=f"已登记 {stamp}".strip())
            except Exception as exc:  # noqa: BLE001 - 中心不可达就跳过这一轮
                # 只在"从通到不通"这一刻记一行：否则中心断一晚，日志会被 60 s 一条刷满
                if self._registry_ok is not False:
                    log_event(
                        self.cfg.lib_dir,
                        f"连不上登记中心（{url}）：{exc.__class__.__name__}: {exc}",
                    )
                self._registry_ok = False
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
