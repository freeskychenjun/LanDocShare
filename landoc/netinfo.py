"""内网地址探测（给 CLI 与状态窗口共用）。"""

from __future__ import annotations

import socket


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
