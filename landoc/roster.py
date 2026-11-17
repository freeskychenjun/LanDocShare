"""授权名单的读取、校验、拉取与发布（设计 §6.2 / §9 名单分发）。

名单存在 `Config.allow_people`（姓名 → IP，IP 空 = 只认姓名）与
`Config.people_departments`（姓名 → 部门/备注，只用于分组查看），授权判定在
`server.authorize_person`。这里负责"怎么维护这份数据"：

- 库主人在自己电脑上维护完整名单（界面/CSV/命令行都行）；
- 把名单**发布**到登记中心（`POST /roster` 存成 `roster.csv`）；
- 其他同事的库从中心**拉取**（`GET /roster.csv`），合并或整体替换。

`Roster` 把"人 + 部门"绑在一起传，省得每个调用点都同时拿两个 dict 再拼错。
纯逻辑放这里（可单测），tkinter 界面在 `roster_ui.py`。
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from landoc.people import dump_people_csv, merge_people, parse_people_table

ROSTER_NAME = "roster.csv"
MAX_ROSTER_BYTES = 1024 * 1024
MAX_DEPARTMENT_CHARS = 40


class RosterError(RuntimeError):
    """拉取/发布失败，消息可直接给用户看。"""


@dataclass
class Roster:
    """一份名单：`people` 参与授权，`departments` 只用来分组/备注。"""

    people: dict[str, str] = field(default_factory=dict)
    departments: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.people)

    def __bool__(self) -> bool:
        return bool(self.people)

    def copy(self) -> "Roster":
        return Roster(dict(self.people), dict(self.departments))

    def department(self, name: str) -> str:
        return (self.departments.get(name) or "").strip()

    def department_counts(self) -> list[tuple[str, int]]:
        """按部门统计人数，人数多的在前；没填部门的拼成"未分部门"。"""
        counts: dict[str, int] = {}
        for name in self.people:
            key = self.department(name) or "未分部门"
            counts[key] = counts.get(key, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    @classmethod
    def from_config(cls, cfg) -> "Roster":
        people = {name: ip for name, ip in (getattr(cfg, "allow_people", None) or {}).items()}
        raw = getattr(cfg, "people_departments", None) or {}
        departments = {
            name: str(value).strip()
            for name, value in raw.items()
            if name in people and str(value or "").strip()
        }
        return cls(people, departments)

    def to_config(self, cfg) -> None:
        """写回配置：只保留还有效（姓名仍在名单里）的部门，避免留下孤儿。"""
        people = {name: ip for name, ip in self.people.items() if str(name).strip()}
        cfg.allow_people = people
        cfg.people_departments = {
            name: dept.strip()
            for name, dept in self.departments.items()
            if name in people and str(dept).strip()
        }


def roster_url(registry_url: str) -> str:
    base = (registry_url or "").strip().rstrip("/")
    if not base:
        raise RosterError("还没配登记中心地址（config.json 的 registry_url）")
    return f"{base}/{ROSTER_NAME}"


def format_roster(roster: Roster) -> str:
    return dump_people_csv(roster.people, roster.departments)


def parse_roster(text: str) -> tuple[Roster, list[str]]:
    people, departments, warnings = parse_people_table(text)
    return Roster(people, departments), warnings


def decode_roster(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def validate_entry(name: str, ip: str, department: str = "") -> tuple[str, str, str]:
    """校验"姓名 + IP + 部门"。IP/部门都可以留空；返回规范化后的三元组。"""
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
    clean_dept = " ".join((department or "").split())
    if any(ch in clean_dept for ch in ",;\t"):
        raise ValueError("部门里不能有 , ; 或制表符")
    if len(clean_dept) > MAX_DEPARTMENT_CHARS:
        raise ValueError(f"部门最多 {MAX_DEPARTMENT_CHARS} 个字，太长就写简称")
    return clean_name, clean_ip, clean_dept


def sorted_entries(roster: Roster) -> list[tuple[str, str, str]]:
    """界面表格顺序：部门按在名单里第一次出现的顺序聚在一起（与导入的 CSV 一致），
    部门内按 IP，没部门/没 IP 的沉底。"""

    order: dict[str, int] = {}
    for dept in roster.departments.values():
        clean = str(dept).strip()
        if clean and clean not in order:
            order[clean] = len(order)
    missing = len(order)

    def key(item: tuple[str, str]) -> tuple[int, int, str, str]:
        name, ip = item
        dept = roster.department(name)
        return (missing if not dept else order.get(dept, missing), 1 if not ip else 0, ip, name)

    return [(name, ip, roster.department(name)) for name, ip in sorted(roster.people.items(), key=key)]


def apply_pull(
    current: Roster, incoming: Roster, *, replace: bool = False, origin: str = "中心"
) -> tuple[Roster, str]:
    """把外面拿到的名单并到本地；返回 (新名单, 给用户看的说明)。"""
    if not incoming:
        raise RosterError("中心上的名单是空的")
    merged_people, notes = merge_people(current.people, incoming.people, replace=replace)
    if replace:
        merged_depts = dict(incoming.departments)
    else:
        merged_depts = {**current.departments, **incoming.departments}
    merged_depts = {
        name: dept for name, dept in merged_depts.items() if name in merged_people and str(dept).strip()
    }
    prefix = f"已用{origin}名单替换" if replace else f"已并入{origin}名单"
    dept_note = f"，{len({*merged_depts.values()})} 个部门" if merged_depts else ""
    return Roster(merged_people, merged_depts), f"{prefix}：{len(incoming)} 人{dept_note}（{'；'.join(notes)}）"


def _open(url: str, *, data: bytes | None = None, timeout: float = 3.0, content_type: str = "text/csv"):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": content_type} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - 内网地址


def fetch_roster(registry_url: str, timeout: float = 3.0) -> tuple[str, Roster]:
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
        roster, _warnings = parse_roster(text)
    except ValueError as exc:
        raise RosterError(f"中心上的名单格式不对：{exc}") from None
    if not roster:
        raise RosterError("中心上的名单是空的")
    return text, roster


def publish_roster(registry_url: str, roster: Roster, timeout: float = 5.0) -> dict:
    """把本地名单发布到中心；返回中心响应（含人数）。"""
    if not roster:
        raise RosterError("名单是空的，先加人再发布")
    url = roster_url(registry_url)
    body = format_roster(roster).encode("utf-8")
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
