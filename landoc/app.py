"""库软件的运行核心：本地检索服务 + 空闲门控的建库/增量更新。

托盘程序（`landoc/tray.py`）只负责界面，真正的服务与调度都在这里，
这样它也能被测试直接驱动，不必真的起一个托盘图标。

建库默认只在用户离开电脑时跑（`idle`）；托盘「立即重建索引」是例外：
`update_now()` 越过门控、一路跑到没有新增为止（见 `_drain`）。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from landoc import __version__
from landoc import idle as idle_module
from landoc import ocr, pipeline, updater
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


_CRASH_TRACE_LINES = 6  # 栈尾留 6 帧够定位问题，整段最多 2000 字符
_hooks_installed = False


def install_crash_hooks(lib_dir) -> None:
    """主线程与后台线程的未捕获异常都落 app.log：一条线程死了不能没人知道。

    - 主线程异常：记完照旧交给原来的 hook（该崩还是崩，只是留了痕）；
    - 后台线程异常：记完不重抛（原行为就是打一行 stderr）。

    用模块级标记保证只包一次：升级失败后会再跑一遍 `App.start()`。
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True

    original_main = sys.excepthook
    original_thread = threading.excepthook

    def _detail(exc_type, exc_value, frames) -> str:
        head = f"{exc_type.__name__}: {exc_value}"
        try:
            lines = traceback.format_exception(exc_type, exc_value, frames)
            tail = [line.rstrip("\n") for line in lines[-_CRASH_TRACE_LINES:] if line.strip()]
        except Exception:  # noqa: BLE001 - 栈都格式化不出来时至少留个类型
            tail = []
        return (head + "\n" + "\n".join(tail))[:2000]

    def main_hook(exc_type, exc_value, frames) -> None:
        log_event(lib_dir, f"未捕获异常（主线程）：{_detail(exc_type, exc_value, frames)}")
        original_main(exc_type, exc_value, frames)

    def thread_hook(args) -> None:
        name = getattr(getattr(args, "thread", None), "name", "") or "后台线程"
        log_event(
            lib_dir,
            f"未捕获异常（{name} 线程）：{_detail(args.exc_type, args.exc_value, args.exc_traceback)}",
        )

    sys.excepthook = main_hook
    threading.excepthook = thread_hook


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
    ocr: str = ""  # 扫描件 OCR 用哪条路（空 = 不显示，见 ocr.describe）
    registry: str = ""  # 登记中心的状态文字（空 = 没配中心）
    version: str = __version__  # 本机程序版本
    update_version: str = ""  # 中心上的新版本（空 = 没发现）
    update_phase: str = ""  # "" / checking / available / downloading / ready / failed / manual / installing
    update_message: str = ""
    denied: int = 0  # 本次启动以来被拒的请求数（401/403/429，明细在 logs/app.log）
    denied_note: str = ""  # 最近一次被拒的来源与时间，形如 "192.0.2.9 09:07"

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
        if self.denied:
            # 有人一直在试却被挡在外面：光在日志里记没人看，托盘悬停也要能看见
            tail += f" · {self.denied} 次访问被拒"
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

    _DENIED_LOG_GAP = 600.0  # 「访问被拒」日志的节流窗口（秒）
    _UPDATE_PING_GAP = 30 * 60.0  # 中心说"有新版"时，心跳触发的检查最多半小时一次

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
        self._last_update_ping = 0.0  # 心跳触发更新检查的节流（monotonic 秒）
        self._last_update_phase = ""  # 更新阶段上一秒还是什么：只记切换，不重复记
        self._denied_count = 0  # 本次启动以来被拒的请求数
        self._last_denied_log = 0.0  # 「访问被拒」日志的节流（monotonic 秒）

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
        install_crash_hooks(self.cfg.lib_dir)
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
            self.httpd = make_server(
                self.cfg, index=self.index, port=port, on_denied=self._note_denied
            )
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
        try:
            # 扫描件 OCR 走哪条路：状态窗里常驻显示，缺中文识别包时能一眼看出来
            self._update(ocr=ocr.describe())
        except Exception:  # noqa: BLE001 - 探测失败不影响服务
            pass
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

    def stop(self, reason: str = "") -> None:
        """停服务。`reason` 只进日志：正常退出与被杀要能分开查。

        幂等：托盘退出会连着调两次，第二次不该再记一行、也不该重复 join。
        """
        if self._stop.is_set():
            return
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
        log_event(
            self.cfg.lib_dir,
            f"库服务停止：{reason or '正常退出'}（本次收录 {self.status.converted} 篇）",
        )

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
        self._log_update_phase(fields)
        if fields.get("phase") == "ready" and fields.get("version"):
            self._notify_user(
                f"新版本 v{fields['version']} 已下载好，右键托盘图标可以升级"
            )

    def _log_update_phase(self, fields: dict) -> None:
        """更新阶段切换落一行 app.log：状态窗口只留最后一句话，重启就没了。

        按 phase 去重（`downloading` 会带进度文案反复来），只记切换那一刻。
        """
        phase = str(fields.get("phase") or "")
        note = {
            "available": "发现新版本",
            "downloading": "开始下载新版本",
            "ready": "新版本已下载好，等待升级",
            "manual": "版本跨度过大，需手动重装",
            "failed": "新版本下载失败",
        }.get(phase)
        if not note or phase == self._last_update_phase:
            return
        self._last_update_phase = phase
        version = str(fields.get("version") or "").strip()
        detail = str(fields.get("message") or "").strip()[:200]
        line = f"更新：{note}"
        if version:
            line += f" v{version}"
        if detail and detail != note:
            line += f"：{detail}"
        log_event(self.cfg.lib_dir, line)

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
        log_event(self.cfg.lib_dir, f"更新：开始安装 v{self.status.update_version or '?'}（先停服务）")
        self.stop("安装更新")
        try:
            updater.spawn_update(staged, target=target)
        except Exception as exc:  # noqa: BLE001 - 起不来就把服务恢复，别让库停了
            log_event(self.cfg.lib_dir, f"更新：启动更新器失败：{exc}")
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

    def _log_failure_details(self, report) -> None:
        """把失败的篇名与原因记下来（最多 3 条）：只报"N 篇失败"等于没报。

        `RunReport.failed` 里是 `(rel, reason)`，reason 是短码：
        unreadable / no_office / timeout / password_protected / parse_failed / empty_text / write_failed。
        """
        failed = list(getattr(report, "failed", None) or [])
        if not failed:
            return
        shown = "、".join(f"{rel}（{reason}）" for rel, reason in failed[:3])
        tail = f" 等 {len(failed)} 篇" if len(failed) > 3 else ""
        log_event(self.cfg.lib_dir, f"失败明细：{shown}{tail}")

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
                self._log_failure_details(report)
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
            # 源文件已经不在的失败记录不算"等着重试"：它既不会重试，也没法自动清掉，
            # 只会在状态窗里永远挂着"失败 N 篇"（由「清理…」连它一起清）。
            failed=sum(
                1
                for item in failures.values()
                if isinstance(item, dict) and not item.get("source_missing")
            ),
        )

    def _note_denied(self, *, ip: str = "", name: str = "", api: str = "", status: int = 0) -> None:
        """服务端挡下一个请求时叫一声：面板与日志都要能看见，不然同事问"我怎么搜不了"没法答。

        日志按 `_DENIED_LOG_GAP` 节流：扫描器一秒能试几十次，不节流会把 app.log 刷满。
        """
        self._denied_count += 1
        stamp = time.strftime("%H:%M")
        self._update(denied=self._denied_count, denied_note=f"{ip} {stamp}".strip())
        now = time.monotonic()
        if now - self._last_denied_log < self._DENIED_LOG_GAP:
            return
        self._last_denied_log = now
        hint = "（不在名单）" if status == 401 else ""
        who = f"{ip}({name})" if name else ip
        log_event(
            self.cfg.lib_dir,
            f"访问被拒：{status} {api or '-'}，来源 {who}{hint}",
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
                self._maybe_start_update_check(reply)
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

    def _maybe_start_update_check(self, reply) -> None:
        """中心在心跳回包里说"有新版"就立刻查一次，别等 6 小时那一轮。

        心跳每 60 s 一次，而检查更新要走一次 /update 请求，所以只在本地还没进入
        「下载中 / 已就绪 / 需手动重装」时触发，同一状态下半小时最多一次 —— 下载
        一直失败时也得留个重试的机会，但不能每分钟重来一遍。
        """
        if not isinstance(reply, dict) or not reply.get("upgrade"):
            return
        if not str(reply.get("latest_version") or "").strip():
            return
        if self.status.update_phase in ("downloading", "ready", "manual"):
            return
        now = time.monotonic()
        if now - self._last_update_ping < self._UPDATE_PING_GAP:
            return
        self._last_update_ping = now
        self.check_updates_async()

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
