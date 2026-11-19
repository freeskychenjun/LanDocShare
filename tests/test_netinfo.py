"""网段判定只有一份实现（`netinfo.cidr_allowed`），库端服务与登记中心都靠它。"""

from __future__ import annotations

from landoc.netinfo import cidr_allowed
from landoc import netinfo


def test_cached_local_ips_probes_once_per_ttl(monkeypatch):
    """`_is_owner` 每个请求都要问一次本机地址：60 秒内不许反复 getaddrinfo。"""
    calls: list[int] = []

    def fake() -> frozenset[str]:
        calls.append(1)
        return frozenset({"172.16.1.197"})

    monkeypatch.setattr(netinfo, "local_ips", fake)
    netinfo.clear_local_ips_cache()

    assert netinfo.cached_local_ips() == frozenset({"172.16.1.197"})
    assert netinfo.cached_local_ips() == frozenset({"172.16.1.197"})
    assert len(calls) == 1


def test_cached_local_ips_reprobes_after_clear(monkeypatch):
    """地址变了（换网、插网线）要能立刻重探，不必干等 TTL 到期。"""
    answers = [frozenset({"172.16.1.197"}), frozenset({"172.16.2.5"})]
    calls: list[int] = []

    def fake() -> frozenset[str]:
        calls.append(1)
        return answers[min(len(calls), 2) - 1]

    monkeypatch.setattr(netinfo, "local_ips", fake)
    netinfo.clear_local_ips_cache()
    assert netinfo.cached_local_ips() == frozenset({"172.16.1.197"})

    netinfo.clear_local_ips_cache()
    assert netinfo.cached_local_ips() == frozenset({"172.16.2.5"})
    assert len(calls) == 2


def test_cached_local_ips_honours_a_short_ttl(monkeypatch):
    """ttl=0 等于不缓存——留给"必须当场看"的调用方。"""
    calls: list[int] = []

    def fake() -> frozenset[str]:
        calls.append(1)
        return frozenset({"127.0.0.1"})

    monkeypatch.setattr(netinfo, "local_ips", fake)
    netinfo.clear_local_ips_cache()
    netinfo.cached_local_ips(ttl=0.0)
    netinfo.cached_local_ips(ttl=0.0)
    assert len(calls) == 2


def test_empty_list_denies_everything():
    """"暂停共享"是空清单，绝不能反过来变成"全网放行"。"""
    assert cidr_allowed("172.16.1.20", []) is False
    assert cidr_allowed("172.16.1.20", None) is False


def test_matches_address_inside_the_cidr():
    assert cidr_allowed("172.16.1.20", ["172.16.0.0/16"]) is True
    assert cidr_allowed("10.92.1.20", ["172.16.0.0/16"]) is False
    assert cidr_allowed("172.16.1.20", ["10.92.0.0/16", "172.16.0.0/16"]) is True


def test_single_address_entries_are_accepted():
    """名单里写一个具体 IP（不带掩码）也要认——ip_network 会自动收成 /32。"""
    assert cidr_allowed("172.16.1.20", ["172.16.1.20"]) is True
    assert cidr_allowed("172.16.1.21", ["172.16.1.20"]) is False


def test_broken_cidr_is_skipped_not_fatal():
    assert cidr_allowed("172.16.1.20", ["不是网段", "172.16.0.0/16"]) is True
    assert cidr_allowed("172.16.1.20", ["不是网段"]) is False


def test_broken_ip_is_rejected():
    assert cidr_allowed("not-an-ip", ["0.0.0.0/0"]) is False
    assert cidr_allowed("", ["0.0.0.0/0"]) is False


def test_ipv6_addresses_work_too():
    assert cidr_allowed("::1", ["::1/128"]) is True
    assert cidr_allowed("fe80::1", ["fe80::/10"]) is True
    assert cidr_allowed("fe80::1", ["172.16.0.0/16"]) is False
