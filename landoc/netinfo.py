"""内网地址探测（给 CLI 与状态窗口共用）。"""

from __future__ import annotations

import socket


def local_ips() -> frozenset[str]:
    """本机所有地址（含回环）：判断"这台机器是不是登记中心那台"用。

    和登记中心里那份 `_local_ips()` 是同一套口径——中心的名单发布鉴权也认这些地址，
    所以"能不能改中心名单"和"从哪台机器发"判断的是同一件事。
    """
    found = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            address = info[4][0]
            if address:
                found.add(address)
    except OSError:
        pass
    return frozenset(found)


def lan_ip() -> str:
    """让系统帮我们选一张有内网路由的网卡，取出它的地址（不真的发包）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"
