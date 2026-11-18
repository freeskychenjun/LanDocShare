"""登记中心看门狗：`--watchdog` 模式下常驻，把中心当成子进程管起来。

为什么不用 Windows 计划任务的「失败后重启」：那玩意儿只在进程**退出**时触发，
中心卡死（端口还听着、请求却没人答）它看不出来；而且「用什么参数启动」还得
再维护一份。这里直接用同一个 exe 再跑一次自己：

* 子进程退出 → 记退出码，按 3/5/10/20/30/60 秒退避后重启；
* 活过 5 分钟算稳定，退避重新从 3 秒开始；
* 子进程活着但 `/health` 连续 3 次没应答（默认每 30 秒探一次）→ 判卡死，
  杀掉重启；
* 子进程报「端口被占用」（退出码 3）→ 重启也没用，记日志后退出，免得每
  分钟刷一条没意义的日志；
* 子进程以退出码 0 自己退出（管理员敲了 `registry --stop`）→ 看门狗也退出，
  不停在一个“刚停就被拉起”的循环里——重新打包期间要的就是它别动。

看门狗自己的输出和子进程的 stdout/stderr 写进同一个日志文件（默认
`%LOCALAPPDATA%\\LanDocShare\\logs\\registry-watchdog.log`）——开机自启时
没有控制台可看，出了问题只能翻这个文件。中心的输出由看门狗逐行转抄，带
`[中心]` 前缀；子进程按 UTF-8、不缓冲写，被强杀时最后的输出也不会丢。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

RESTART_DELAYS = (3.0, 5.0, 10.0, 20.0, 30.0, 60.0)
STABLE_SECONDS = 300.0
POLL_SECONDS = 0.5
FIRST_CHECK_SECONDS = 15.0
KILL_GRACE_SECONDS = 10.0

# 子进程用这个退出码告诉看门狗「端口被人占了，再起多少次都一样」
PORT_BUSY_CODE = 3

# 这些是看门狗自己的参数，拼子进程命令时要摘掉，否则子进程会再套一层看门狗
_WATCHDOG_OPTIONS = (
    "--watchdog",
    "--watchdog-log",
    "--watchdog-interval",
    "--watchdog-failures",
)


def default_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "LanDocShare" / "logs" / "registry-watchdog.log"


def strip_watchdog_args(argv: Sequence[str]) -> list[str]:
    """去掉看门狗自己的参数，留下真正给中心用的。"""
    kept: list[str] = []
    skip_value = False
    for token in argv:
        if skip_value:
            skip_value = False
            continue
        name = token.split("=", 1)[0]
        if name not in _WATCHDOG_OPTIONS:
            kept.append(token)
            continue
        # `--watchdog-failures 5` 这种把值也一起吃掉；`--watchdog` 本身没有值
        if "=" not in token and name != "--watchdog":
            skip_value = True
    return kept


def child_command(argv: Sequence[str]) -> list[str]:
    """子进程命令行：打包版是 exe 自己，源码运行时是 `python -m landoc.registry`。"""
    args = strip_watchdog_args(argv)
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "landoc.registry", *args]


def restart_delay(attempt: int) -> float:
    """第 `attempt` 次（从 0 数）连续崩溃后等多久再起。"""
    if attempt < 0:
        attempt = 0
    return RESTART_DELAYS[min(attempt, len(RESTART_DELAYS) - 1)]


def describe_exit_code(code: int | None) -> str:
    """把退出码写成日志里看得懂的样子。

    Windows 的退出码是无符号 DWORD，被 `taskkill /f`、任务管理器「结束任务」
    干掉时拿到的是 4294967295；原样打进日志容易被当成"程序自己的错误码"，
    所以顺手给出有符号值和十六进制，并点明这通常是强制结束。
    """
    if code is None:
        return "退出码未知"
    if code < 0:
        return f"退出码 {code}"
    if code > 0x7FFFFFFF:
        signed = code - 0x1_0000_0000
        return f"退出码 {signed}（0x{code:08X}，通常是被强制结束）"
    return f"退出码 {code}"


def probe_health(url: str, *, timeout: float = 5.0) -> bool:
    """能拿到任何 HTTP 应答就算活着；连接被拒/超时才算没气。

    用 127.0.0.1 探，中心可能按来源网段回 403——403 也是应答，说明它在
    accept。这里只看「有没有人接电话」，不认身份。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:  # noqa: S310 - 本机地址
            response.read(4096)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 - 超时/拒绝/协议错都算没应答
        return False


# ---------------------------------------------------------------- 日志与子进程


_LOG_LOCK = threading.Lock()


def _append(path: Path, line: str, *, echo: bool = True) -> None:
    """往日志里追加一整行。

    看门狗自己和「转抄中心输出」的线程都走这里，一把锁保证同一进程内不交错。
    子进程**不**直接写这个文件：两个进程各自 seek 到文件尾再写，实测会把对方
    刚写的那行盖掉（日志里突然少一行，像是没发生过）。
    """
    with _LOG_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
    if not echo:
        return
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001 - 打包成窗口程序时 stdout 是 None
        pass


def make_logger(path: Path) -> Callable[[str], None]:
    def say(message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _append(path, f"{stamp} [watchdog] {message}")

    return say


def _forward(stream, log_path: Path) -> None:
    """把中心子进程的 stdout/stderr 逐行转抄进日志，直到它退出（管道 EOF）。"""
    try:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            _append(log_path, f"{stamp} [中心] {text}", echo=False)
    except Exception:  # noqa: BLE001 - 管道被关、进程已经退了
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _child_env() -> dict[str, str]:
    """子进程输出要转进日志，那就别让它攒着、也别用 GBK 写。

    默认的 stdout 一接管道就是块缓冲：中心被人强杀时最后几行（往往是崩在哪里的
    线索）还在缓冲区里，跟着进程一起没了。UTF-8 是为了让整个日志文件只有一种
    编码——否则看门狗写 UTF-8、子进程按 cp936 写，翻日志得来回换编码。
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _spawn(
    cmd: Sequence[str], log_path: Path, *, spawn: Callable[[Sequence[str]], object] | None
):
    """起子进程，并把它的输出转进日志——崩溃堆栈才不会没处看。"""
    if spawn is not None:
        return spawn(cmd)
    proc = subprocess.Popen(  # noqa: S603 - 命令行由本进程拼出，不含外部输入
        list(cmd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=_creation_flags(),
        env=_child_env(),
        close_fds=True,
    )
    if proc.stdout is not None:
        threading.Thread(
            target=_forward,
            args=(proc.stdout, log_path),
            name="watchdog-pump",
            daemon=True,
        ).start()
    return proc


def _terminate(proc, *, clock, sleep) -> None:
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 - 已经退出
        return
    deadline = clock() + KILL_GRACE_SECONDS
    while clock() < deadline:
        if proc.poll() is not None:
            return
        sleep(POLL_SECONDS)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _pause(seconds: float, *, sleep, is_stopping) -> bool:
    """分小段睡，期间能及时响应停止请求。"""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if is_stopping is not None and is_stopping():
            return False
        step = min(POLL_SECONDS, remaining)
        sleep(step)
        remaining -= step
    return True


def _watch(
    proc,
    *,
    health_url: str,
    interval: float,
    timeout: float,
    failures: int,
    clock,
    sleep,
    is_stopping,
) -> tuple[str, str]:
    """盯一个子进程，返回 `(原因, 说明)`：exit / hang / stop。"""
    next_check = clock() + max(FIRST_CHECK_SECONDS, interval)
    bad = 0
    while True:
        if is_stopping is not None and is_stopping():
            return "stop", "收到停止请求"
        code = proc.poll()
        if code is not None:
            return "exit", describe_exit_code(code)
        if health_url and clock() >= next_check:
            if probe_health(health_url, timeout=timeout):
                bad = 0
            else:
                bad += 1
                if bad >= max(1, failures):
                    return "hang", f"/health 连续 {bad} 次没有应答"
            next_check = clock() + interval
        sleep(POLL_SECONDS)


# ---------------------------------------------------------------- 主循环


def supervise(
    argv: Sequence[str],
    *,
    log_path: Path | None = None,
    port: int = 8767,
    health_interval: float = 30.0,
    health_timeout: float = 5.0,
    health_failures: int = 3,
    restart_delays: Sequence[float] = RESTART_DELAYS,
    max_restarts: int | None = None,
    spawn: Callable[[Sequence[str]], object] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    is_stopping: Callable[[], bool] | None = None,
) -> int:
    """常驻看门狗；返回 0 表示正常退出（含「端口已有一个健康的中心」）。"""
    log_file = Path(log_path) if log_path else default_log_path()
    say = make_logger(log_file)
    cmd = child_command(argv)
    health_url = (
        f"http://127.0.0.1:{port}/health" if port and health_interval > 0 else ""
    )
    say(f"看门狗启动（pid={os.getpid()}）：{' '.join(cmd)}")
    if not health_url:
        say("未启用 /health 探活：只管子进程退出，卡死不会自动重启")

    restarts = 0
    delay_index = 0
    while True:
        if is_stopping is not None and is_stopping():
            say("收到停止请求，看门狗退出")
            return 0
        started = clock()
        proc = _spawn(cmd, log_file, spawn=spawn)
        if proc is None:
            say("子进程起不来（系统资源不足？）")
            return 1
        say(f"中心已启动（pid={getattr(proc, 'pid', '?')}）")
        reason, detail = _watch(
            proc,
            health_url=health_url,
            interval=health_interval,
            timeout=health_timeout,
            failures=health_failures,
            clock=clock,
            sleep=sleep,
            is_stopping=is_stopping,
        )
        if reason == "stop":
            _terminate(proc, clock=clock, sleep=sleep)
            say("收到停止请求，已停掉中心")
            return 0
        if reason == "hang":
            say(f"{detail}，判定卡死，重启")
            _terminate(proc, clock=clock, sleep=sleep)
        code = proc.poll()
        say(f"中心退出：{detail}")
        if reason == "exit" and code == 0:
            say("中心是收到 --stop 后自己退出的（退出码 0），看门狗一起退；重打包完再启动一次")
            return 0
        if code == PORT_BUSY_CODE:
            say(f"端口 {port} 被占用（或没权限），重启也没用；换个 --port 或先停掉占用者")
            return 0

        uptime = max(0.0, clock() - started)
        if uptime >= STABLE_SECONDS:
            delay_index = 0
        if max_restarts is not None and restarts >= max_restarts:
            say(f"已重启 {restarts} 次，达到上限，看门狗退出")
            return 1
        delay = (
            restart_delays[min(delay_index, len(restart_delays) - 1)]
            if restart_delays
            else 0.0
        )
        delay_index += 1
        restarts += 1
        say(f"{delay:.0f} 秒后重启（第 {restarts} 次）")
        if not _pause(delay, sleep=sleep, is_stopping=is_stopping):
            say("收到停止请求，看门狗退出")
            return 0


__all__ = [
    "PORT_BUSY_CODE",
    "RESTART_DELAYS",
    "child_command",
    "default_log_path",
    "describe_exit_code",
    "make_logger",
    "probe_health",
    "restart_delay",
    "strip_watchdog_args",
    "supervise",
]
