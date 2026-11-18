"""网段判定只有一份实现（`netinfo.cidr_allowed`），库端服务与登记中心都靠它。"""

from __future__ import annotations

from landoc.netinfo import cidr_allowed


def test_empty_list_denies_everything():
    """"暂停共享"是空清单，绝不能反过来变成"全网放行"。"""
    assert cidr_allowed("10.91.1.20", []) is False
    assert cidr_allowed("10.91.1.20", None) is False


def test_matches_address_inside_the_cidr():
    assert cidr_allowed("10.91.1.20", ["10.91.0.0/16"]) is True
    assert cidr_allowed("10.92.1.20", ["10.91.0.0/16"]) is False
    assert cidr_allowed("10.91.1.20", ["10.92.0.0/16", "10.91.0.0/16"]) is True


def test_single_address_entries_are_accepted():
    """名单里写一个具体 IP（不带掩码）也要认——ip_network 会自动收成 /32。"""
    assert cidr_allowed("10.91.1.20", ["10.91.1.20"]) is True
    assert cidr_allowed("10.91.1.21", ["10.91.1.20"]) is False


def test_broken_cidr_is_skipped_not_fatal():
    assert cidr_allowed("10.91.1.20", ["不是网段", "10.91.0.0/16"]) is True
    assert cidr_allowed("10.91.1.20", ["不是网段"]) is False


def test_broken_ip_is_rejected():
    assert cidr_allowed("not-an-ip", ["0.0.0.0/0"]) is False
    assert cidr_allowed("", ["0.0.0.0/0"]) is False


def test_ipv6_addresses_work_too():
    assert cidr_allowed("::1", ["::1/128"]) is True
    assert cidr_allowed("fe80::1", ["fe80::/10"]) is True
    assert cidr_allowed("fe80::1", ["10.91.0.0/16"]) is False
