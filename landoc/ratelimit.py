"""每来源 IP 的滑动窗口限流（库端检索服务与登记中心共用）。

两处都要它，但语义不同：
- 库端：挡住"某个同事的脚本把库拖死"，所以默认 60 次/分钟、在认证之前就判；
- 中心：挡住"有人狂刷 /register 或 /download"，同样按来源 IP，但阈值放宽
  （一台机器可能同时挂着好几个库，都得能按时心跳）。
"""

from __future__ import annotations

import threading
import time
from collections import deque

DEFAULT_RATE = 60  # 每分钟每 IP 的默认额度（库端检索）
DEFAULT_WINDOW = 60.0


class RateLimiter:
    """每 IP 滑动窗口限流；线程安全，超限的请求不计入窗口。"""

    def __init__(self, max_req: int = DEFAULT_RATE, window: float = DEFAULT_WINDOW):
        self.max = max_req
        self.window = window
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            dq = self._hits.setdefault(ip, deque())
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return False
            dq.append(now)
            # 来源 IP 会随 DHCP/端口变化不断新增，攒多了要清一次，免成小泄漏
            if len(self._hits) > 4096:
                self._hits = {
                    k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window
                }
            return True

    def reset(self) -> None:
        """清空窗口（测试用；运行时不该调）。"""
        with self._lock:
            self._hits.clear()
