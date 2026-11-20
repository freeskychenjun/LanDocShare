"""默认允许网段：源码里只留占位，真默认按"本机所在网段"现算。

脱敏时把单位网段写成了别的网段，结果打包版中心默认只放行那个假网段，
10.91 网段全被 403 挡掉。这些用例把"默认 = 本机所在网段"钉死，
既避免源码里出现真实内网，也避免再退回某个写死的网段。
"""

from __future__ import annotations

import socket
import sys
import types
import urllib.request
from pathlib import Path

from landoc import cli, netinfo, registry, wizard
from landoc.config import Config


def _adapter(address: str, netmask: str | None, family: int = socket.AF_INET):
    return types.SimpleNamespace(family=family, address=address, netmask=netmask)


def test_site_networks_takes_the_block_not_the_single_slash_24():
    """单位里同一层楼常分在好几个 /24：默认给 /16 才不会把隔壁的同事挡掉。"""
    assert netinfo.site_networks([("10.91.35.22", "255.255.255.0")]) == ("10.91.0.0/16",)


def test_site_networks_maps_172_16_range_and_skips_non_lan_addresses():
    nets = netinfo.site_networks(
        [
            ("172.20.3.4", "255.255.255.0"),
            ("127.0.0.1", None),  # 回环不算
            ("169.254.1.2", None),  # 链路本地不算
            ("not-an-ip", None),
        ]
    )
    assert nets == ("172.20.0.0/16",)


def test_site_networks_follows_the_mask_for_192_168():
    assert netinfo.site_networks([("192.168.5.9", "255.255.0.0")]) == ("192.168.0.0/16",)
    assert netinfo.site_networks([("192.168.5.9", "255.255.255.0")]) == ("192.168.5.0/24",)
    assert netinfo.site_networks([("192.168.5.9", None)]) == ("192.168.5.0/24",)


def test_local_subnets_ignores_virtual_adapters(monkeypatch):
    addrs = {
        "以太网": [_adapter("10.91.35.22", "255.255.255.0")],
        "vEthernet (WSL)": [_adapter("172.28.0.1", "255.255.240.0")],
        "VMware Network Adapter VMnet8": [_adapter("192.168.80.1", "255.255.255.0")],
    }
    stub = types.SimpleNamespace(net_if_addrs=lambda: addrs)
    monkeypatch.setitem(sys.modules, "psutil", stub)
    assert netinfo.local_subnets() == ("10.91.0.0/16",)


def test_local_subnets_falls_back_to_local_ips_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # 等价于没装：import 直接失败
    monkeypatch.setattr(netinfo, "local_ips", lambda: frozenset({"10.4.5.6"}))
    assert netinfo.local_subnets() == ("10.4.0.0/16",)


def test_wizard_default_lan_cidrs_follow_this_machine(monkeypatch):
    monkeypatch.setattr(netinfo, "local_subnets", lambda: ("10.42.0.0/16",))
    assert wizard.default_lan_cidrs() == ["10.42.0.0/16"]


def test_registry_default_cidrs_follow_this_machine(monkeypatch):
    monkeypatch.setattr(netinfo, "local_subnets", lambda: ("10.42.0.0/16",))
    assert registry.default_cidrs() == ["10.42.0.0/16"]


def test_registry_default_cidrs_fall_back_when_no_network(monkeypatch):
    monkeypatch.setattr(netinfo, "local_subnets", lambda: ())
    assert registry.default_cidrs() == list(registry.DEFAULT_CIDRS)


def test_registry_center_uses_this_machines_block_by_default(tmp_path, monkeypatch):
    """打了 --allow-cidrs 就听它的，没打就读本机网段（这里伪装成 127/8 好让本机访问）。"""
    monkeypatch.setattr(netinfo, "local_subnets", lambda: ("127.0.0.0/8",))
    store = registry.RegistryStore(tmp_path / "registry.json")
    httpd = registry.make_registry_server(store, port=0)
    import threading

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            assert response.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_init_uses_this_machines_block_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "default_lan_cidrs", lambda: ["10.55.0.0/16"])
    lib = tmp_path / "lib"
    assert cli.main(["init", "--lib-dir", str(lib), "--name", "库"]) == 0
    assert Config.load(lib).allow_cidrs == ["10.55.0.0/16"]


def test_cli_init_keeps_existing_cidrs_and_respects_explicit_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "default_lan_cidrs", lambda: ["10.55.0.0/16"])
    lib = tmp_path / "lib"
    cli.main(["init", "--lib-dir", str(lib), "--name", "库", "--allow-cidrs", "10.1.0.0/16"])
    # 再跑一次 init 改名字：不能把用户手工填的网段打回默认
    cli.main(["init", "--lib-dir", str(lib), "--name", "新名"])
    assert Config.load(lib).allow_cidrs == ["10.1.0.0/16"]


def test_no_real_network_is_baked_into_the_source():
    """源码里不许出现单位真实网段（脱敏前是 10.91.0.0/16）。"""
    root = Path(__file__).resolve().parents[1] / "landoc"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "10.91." not in text, f"{path} 里还有单位真实网段"
