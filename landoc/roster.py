"""授权名单的读取、校验、拉取与发布（设计 §6.2 / §9 名单分发）。

名单存在 `Config.allow_people`（姓名 → IP，IP 空 = 只认姓名），授权判定在
`server.authorize_person`。这里只负责"怎么维护这份数据"：

- 库主人在自己电脑上维护完整名单（界面/CSV/命令行都行）；
- 把名单**发布**到登记中心（`POST /roster` 存成 `roster.csv`）；
- 其他同事的库从中心**拉取**（`GET /roster.csv`），合并或整体替换。

纯逻辑放这里（可单测），tkinter 界面在 `roster_ui.py`。
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request

from landoc.people import dump_people_csv, merge_people, parse_people_csv

ROSTER_NAME = "roster.csv"
MAX_ROSTER_BYTES = 1024 * 1024


class RosterError(RuntimeError):
    """拉取/发布失败，消息可直接给用户看。"""


def roster_url(registry_url: str) -> str:
    base = (registry_url or "").strip().rstrip("/")
    if not base:
        raise RosterError("还没配登记中心地址（config.json 的 registry_url）")
    return f"{base}/{ROSTER_NAME}"


def format_roster(people: dict[str, str]) -> str:
    return dump_people_csv(people)


def parse_roster(text: str) -> tuple[dict[str, str], list[str]]:
    return parse_people_csv(text)


def decode_roster(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def validate_entry(name: str, ip: str) -> tuple[str, str]:
    """校验"姓名 + IP"。IP 可以留空；返回规范化后的 (姓名, IP)。"""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("姓名不能为空")
    if any(ch in clean_name for ch in "=,:;|/\\"):
        raise ValueError("姓名里不能有 = , : ; | / \\ 这些符号")
    clean_ip = (ip or "").strip()
    if clean_ip:
        try:
            clean_ip = str(ipaddress.ip_address(clean_ip))
        except ValueError:
            raise ValueError(f"IP 格式不对：{clean_ip}") from None
    return clean_name, clean_ip


def sorted_entries(people: dict[str, str]) -> list[tuple[str, str]]:
    """界面表格的顺序：先按 IP，没有 IP 的排最后，再按姓名。"""

    def key(item: tuple[str, str]) -> tuple[int, str, str]:
        name, ip = item
        return (1 if not ip else 0, ip, name)

    return sorted(people.items(), key=key)


def apply_pull(
    current: dict[str, str], incoming: dict[str, str], *, replace: bool = False
) -> tuple[dict[str, str], str]:
    """把中心拉下来的名单并到本地；返回 (新名单, 给用户看的说明)。"""
    if not incoming:
        raise RosterError("中心上的名单是空的")
    merged, notes = merge_people(current, incoming, replace=replace)
    prefix = "已用中心名单替换" if replace else "已并入中心名单"
    return merged, f"{prefix}：{len(incoming)} 人（{'；'.join(notes)}）"


def _open(url: str, *, data: bytes | None = None, timeout: float = 3.0, content_type: str = "text/csv"):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": content_type} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - 内网地址


def fetch_roster(registry_url: str, timeout: float = 3.0) -> tuple[str, dict[str, str]]:
    """从中心拉名单，返回 (原始 CSV 文本, 解析好的名单)。"""
    url = roster_url(registry_url)
    try:
        with _open(url, timeout=timeout) as resp:
            raw = resp.read(MAX_ROSTER_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RosterError("中心上还没有名单（库主人还没发布）") from None
        if exc.code == 403:
            raise RosterError("这台机器不在中心的允许网段里") from None
        raise RosterError(f"拉取名单失败：HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RosterError(f"连不上登记中心：{getattr(exc, 'reason', exc)}") from None
    if len(raw) > MAX_ROSTER_BYTES:
        raise RosterError("名单太大（超过 1 MB）")
    text = decode_roster(raw)
    try:
        people, _warnings = parse_roster(text)
    except ValueError as exc:
        raise RosterError(f"中心上的名单格式不对：{exc}") from None
    if not people:
        raise RosterError("中心上的名单是空的")
    return text, people


def publish_roster(registry_url: str, people: dict[str, str], timeout: float = 5.0) -> dict:
    """把本地名单发布到中心；返回中心响应（含人数）。"""
    if not people:
        raise RosterError("名单是空的，先加人再发布")
    url = roster_url(registry_url)
    body = format_roster(people).encode("utf-8")
    try:
        with _open(url, data=body, timeout=timeout, content_type="text/csv; charset=utf-8") as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8")).get("error") or "")
        except (ValueError, OSError):
            detail = ""
        raise RosterError(f"发布失败：HTTP {exc.code}{(' · ' + detail) if detail else ''}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RosterError(f"连不上登记中心：{getattr(exc, 'reason', exc)}") from None
    if not payload.get("ok"):
        raise RosterError(f"发布失败：{payload.get('error') or '中心没接受'}")
    return payload
