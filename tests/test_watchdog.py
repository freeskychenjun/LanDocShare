"""看门狗：子进程命令、探活口径、退出码/卡死后的重启决策。"""

from __future__ import annotations

import io
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from landoc import watchdog


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeProc:
    """先返回几次 None（活着），再按 `codes` 依次给出退出码。"""

    def __init__(self, codes=(), *, alive_polls: int = 1, pid: int = 4242) -> None:
        self.pid = pid
        self._codes = list(codes) or [0]
        self._alive_polls = alive_polls
        self.polls = 0
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.terminated:
            return -15
        self.polls += 1
        if self.polls <= self._alive_polls:
            return None
        if len(self._codes) > 1:
            return self._codes.pop(0)
        return self._codes[0]

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):  # noqa: ANN001 - 模仿 Popen
        return 0


def run_supervise(tmp_path, procs, **kwargs):
    """跑一遍 supervise，返回 (退出码, 日志文本, 起过的子进程数)。"""
    spawned: list[FakeProc] = []

    def spawn(_cmd):
        proc = procs[len(spawned)]
        spawned.append(proc)
        return proc

    clock = FakeClock()
    log = tmp_path / "watchdog.log"
    options = {"health_interval": 0, **kwargs}  # 默认不探活：这些用例只关心退出码
    code = watchdog.supervise(
        ["--port", "8767"],
        log_path=log,
        spawn=spawn,
        sleep=clock.sleep,
        clock=clock,
        **options,
    )
    return code, log.read_text(encoding="utf-8"), len(spawned)


# ---------------------------------------------------------------- 命令行


def test_child_command_drops_watchdog_options_for_source_run(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    argv = [
        "--port",
        "8790",
        "--watchdog",
        "--watchdog-log",
        "x.log",
        "--watchdog-interval=12",
        "--watchdog-failures",
        "5",
        "--package-zip",
        "a.zip",
    ]
    assert watchdog.child_command(argv) == [
        sys.executable,
        "-m",
        "landoc.registry",
        "--port",
        "8790",
        "--package-zip",
        "a.zip",
    ]


def test_child_command_uses_exe_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\LanDocShare\LanDocShare.exe")
    assert watchdog.child_command(["--port", "8767"]) == [
        r"C:\Apps\LanDocShare\LanDocShare.exe",
        "registry",
        "--port",
        "8767",
    ]


def test_child_command_does_not_double_the_registry_subcommand(monkeypatch):
    # 打包版靠第一个参数分派：已经带了 "registry" 的 argv 不能再补一个
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\LanDocShare\LanDocShare.exe")
    assert watchdog.child_command(["registry", "--port", "8767", "--watchdog"]) == [
        r"C:\Apps\LanDocShare\LanDocShare.exe",
        "registry",
        "--port",
        "8767",
    ]


def test_restart_delay_caps_at_one_minute():
    assert watchdog.restart_delay(0) == 3
    assert watchdog.restart_delay(3) == 20
    assert watchdog.restart_delay(99) == 60


def test_describe_exit_code_explains_forced_kill():
    # 任务管理器/Stop-Process 强杀拿到的是无符号 0xFFFFFFFF，日志里说人话
    assert watchdog.describe_exit_code(4294967295) == (
        "退出码 -1（0xFFFFFFFF，通常是被强制结束）"
    )
    assert watchdog.describe_exit_code(watchdog.PORT_BUSY_CODE) == "退出码 3"
    assert watchdog.describe_exit_code(0) == "退出码 0"
    assert watchdog.describe_exit_code(None) == "退出码未知"


def test_spawn_pipes_child_output_through_the_watchdog(tmp_path, monkeypatch):
    captured = {}

    class FakePopen:
        stdout = None  # 模拟「管道已读完」：转抄线程就不会起

        def __init__(self, cmd, **kwargs):  # noqa: ANN001 - 模仿 Popen
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(watchdog.subprocess, "Popen", FakePopen)
    watchdog._spawn([sys.executable, "-c", "pass"], tmp_path / "wd.log", spawn=None)
    kwargs = captured["kwargs"]
    # 中心输出走管道、由看门狗转抄：两个进程各写同一个文件会互相覆盖
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.STDOUT
    env = kwargs["env"]
    # 不缓冲：中心被强杀时最后几行不至于烂在缓冲区里
    assert env["PYTHONUNBUFFERED"] == "1"
    # 统一 UTF-8：看门狗自己也是按 UTF-8 写同一个文件
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_forward_copies_child_output_into_log(tmp_path):
    log = tmp_path / "wd.log"
    stream = io.BytesIO("登记中心已启动\r\n第二行\n".encode("utf-8"))
    watchdog._forward(stream, log)
    text = log.read_text(encoding="utf-8")
    assert "[中心] 登记中心已启动" in text
    assert "[中心] 第二行" in text
    assert stream.closed


# ---------------------------------------------------------------- 探活


class _Forbidden(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server 约定
        self.send_response(403)
        self.end_headers()
        self.wfile.write(b"no")

    def log_message(self, *args):  # noqa: ANN002 - 别往 stderr 刷日志
        pass


@pytest.fixture()
def forbidding_health():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Forbidden)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/health"
    finally:
        server.shutdown()
        server.server_close()


def test_probe_health_treats_403_as_alive(forbidding_health):
    # 中心按来源网段回 403 也是「有人接电话」，不该判死
    assert watchdog.probe_health(forbidding_health, timeout=2) is True


def test_probe_health_false_when_nothing_listens():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert watchdog.probe_health(f"http://127.0.0.1:{port}/health", timeout=1) is False


# ---------------------------------------------------------------- 重启决策


def test_crash_restarts_with_backoff(tmp_path):
    code, log, spawned = run_supervise(
        tmp_path, [FakeProc([7]), FakeProc([7])], max_restarts=1
    )
    assert code == 1
    assert spawned == 2
    assert "3 秒后重启（第 1 次）" in log
    assert "已重启 1 次，达到上限" in log


def test_port_busy_exits_without_restarting(tmp_path):
    code, log, spawned = run_supervise(
        tmp_path, [FakeProc([watchdog.PORT_BUSY_CODE])], max_restarts=5
    )
    assert code == 0
    assert spawned == 1
    assert "被占用" in log


def test_graceful_stop_exit_code_zero_does_not_restart(tmp_path):
    # `registry --stop` 是优雅退出（退出码 0）：看门狗得跟着退，不能又拉起来
    code, log, spawned = run_supervise(tmp_path, [FakeProc([0])], max_restarts=5)
    assert code == 0
    assert spawned == 1
    assert "--stop" in log


def test_hang_triggers_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "probe_health", lambda *a, **k: False)
    procs = [FakeProc([7], alive_polls=10**6), FakeProc([7], alive_polls=10**6)]
    code, log, spawned = run_supervise(
        tmp_path,
        procs,
        max_restarts=1,
        health_interval=1.0,
        health_failures=2,
    )
    assert all(proc.terminated for proc in procs)
    assert spawned == 2
    assert code == 1
    assert "判定卡死，重启" in log
    assert "连续 2 次没有应答" in log
