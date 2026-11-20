"""内网地址探测与网段判定（给 CLI、库端服务与登记中心共用）。"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time

#: `cached_local_ips()` 的默认有效期（秒）。60 秒足够挡掉"每个请求一次 getaddrinfo"，
#: 又不至于让换了网络的笔记本在中心那边被挡太久。
LOCAL_IPS_TTL = 60.0

_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_ips: frozenset[str] = frozenset()


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


def cached_local_ips(ttl: float = LOCAL_IPS_TTL) -> frozenset[str]:
    """`local_ips()` 的 TTL 缓存版：库端服务每个请求都要判"是不是本机"。

    服务端每个 `/api/*` 请求都会走一次 `_is_owner()`，中心的说明页也每屏都判一次；
    `getaddrinfo(hostname)` 在网卡多、DNS/NetBIOS 慢的机器上能拖到几百毫秒。
    缓存下沉到这里，服务端与中心共用同一份，免得各自维护一个 TTL。
    """
    global _cache_at, _cache_ips
    now = time.monotonic()
    with _cache_lock:
        if _cache_ips and now - _cache_at < ttl:
            return _cache_ips
    fresh = local_ips()
    with _cache_lock:
        _cache_at = time.monotonic()
        _cache_ips = fresh
    return fresh


def clear_local_ips_cache() -> None:
    """让下一次 `cached_local_ips()` 重新探测（测试与"刚换了网络"时用）。"""
    global _cache_at, _cache_ips
    with _cache_lock:
        _cache_at = 0.0
        _cache_ips = frozenset()


def site_networks(addresses) -> tuple[str, ...]:
    """把一串 (地址, 掩码) 折成"本站网段"，用作允许网段的默认值。

    为什么不用网卡上那个 /24：单位里同一层楼常分在好几个 /24（甚至好几个 /16），
    默认只放行本机那个 /24 会把自己的同事挡在外面。这里按私有地址的常见划法取：

    - `10.x.y.z` → `10.x.0.0/16`
    - `172.16~31.x.y` → `172.n.0.0/16`
    - `192.168.x.y` → 按掩码（至少 /24）
    - 其它地址 → 按掩码，至少 /24

    这样默认值等价于"和我在同一个地址段"，既不会把整个公司网段写进源码，
    也不至于换个办公室就要手工改一遍。
    """
    found: set[str] = set()
    for address, netmask in addresses:
        network = _site_network(address, netmask)
        if network is not None:
            found.add(str(network))
    return tuple(sorted(found))


def _site_network(address: str, netmask: str | None) -> ipaddress.IPv4Network | None:
    try:
        addr = ipaddress.ip_address(str(address))
    except ValueError:
        return None
    if addr.version != 4 or addr.is_loopback or addr.is_link_local:
        return None
    if addr in ipaddress.ip_network("10.0.0.0/8"):
        prefix = 16
    elif addr in ipaddress.ip_network("172.16.0.0/12"):
        prefix = 16
    else:
        # 192.168/16 里 /16 往往横跨好几栋楼，按掩码走、至少 /24 更稳妥
        prefix = min(_prefix_from_mask(netmask, fallback=24), 24)
    try:
        return ipaddress.ip_network(f"{address}/{prefix}", strict=False)
    except ValueError:
        return None


def _prefix_from_mask(netmask: str | None, *, fallback: int = 24) -> int:
    if not netmask:
        return fallback
    try:
        return ipaddress.ip_network(f"0.0.0.0/{netmask}", strict=False).prefixlen
    except ValueError:
        return fallback


_VIRTUAL_HINTS = (
    "vethernet",
    "vmware",
    "virtualbox",
    "hyper-v",
    "wsl",
    "docker",
    "loopback",
    "tap-",
    "tun",
)


def local_subnets() -> tuple[str, ...]:
    """本机各网卡所在的"本站网段"（见 `site_networks`）。

    优先用 psutil 读网卡地址与掩码；没装 psutil 时退回 `local_ips()` 的 /16、/24 估算。
    虚拟网卡（WSL、Hyper-V、VMware、Docker…）不算"本站"——它们的网段只连着
    本机里的虚拟机/容器，写进允许名单等于白放宽。
    """
    pairs: list[tuple[str, str | None]] = []
    try:  # psutil 只在装了的机器上有；没装也不影响，下面有兜底。
        import psutil

        for name, items in psutil.net_if_addrs().items():
            lowered = str(name).lower()
            if any(hint in lowered for hint in _VIRTUAL_HINTS):
                continue
            for item in items:
                if item.family != socket.AF_INET or not item.address:
                    continue
                pairs.append((item.address, item.netmask))
    except Exception:  # pragma: no cover - psutil 缺失或接口枚举失败
        pairs = []
    if not pairs:
        pairs = [(ip, None) for ip in local_ips()]
    return site_networks(pairs)


def cidr_allowed(ip: str, cidrs) -> bool:
    """来源 IP 是否落在允许的网段里。

    库端服务（`server._ip_allowed`）与登记中心（旧 `registry._cidr_allowed`）是同一套判定，
    所以只有这一份实现。语义上偏保守：**空清单 = 拒绝所有来源**（对应"暂停共享"），
    绝不能反过来变成"全网放行"；清单里写错的网段跳过，不让它把整台机器挡死或放开。
    """
    if not cidrs:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(str(cidr), strict=False):
                return True
        except ValueError:
            continue
    return False


def cidr_blocked(ip: str, cidrs) -> bool:
    """来源 IP 是否命中黑名单；空清单 = 没人被挡。

    与 `cidr_allowed` 的"空清单 = 拒绝"相反：黑名单为空表示不额外拒绝任何来源。
    """
    if not cidrs:
        return False
    return cidr_allowed(ip, cidrs)


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
