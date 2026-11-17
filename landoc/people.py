"""人员名单（姓名 → IP）的读写。

名单是"按 IP 授权、姓名只作备注"的落地形式：`Config.allow_people` 存
`{姓名: IP}`，IP 留空表示"这个人只能靠报姓名识别"（IP 会变的情况）。

CSV 是最容易维护的格式（Excel 另存即可），也是以后"库主人统一维护、客户端
拉取"的交换格式，所以解析放在这里而不是塞进向导。
"""

from __future__ import annotations

import csv
import io
import ipaddress
from pathlib import Path

_NAME_HEADERS = {"姓名", "名字", "name", "人员"}
_IP_HEADERS = {"ip", "ip地址", "ip 地址", "地址", "内网ip"}
_SEPARATORS = (",", "\t", "，", ";", "；")


def _split(line: str) -> list[str]:
    for sep in _SEPARATORS:
        if sep in line:
            return [cell.strip() for cell in line.split(sep)]
    return [line.strip()]


def _clean_ip(raw: str) -> str:
    """校验并规范化 IP；空串表示"不登记 IP"。"""
    text = (raw or "").strip()
    if not text:
        return ""
    return str(ipaddress.ip_address(text))


def parse_people_csv(text: str) -> tuple[dict[str, str], list[str]]:
    """解析名单文本，返回 (名单, 警告)。

    容忍：BOM、空行、`#` 注释、逗号/制表符/全角逗号分隔、表头（姓名,IP）
    或没有表头。重复姓名以第一次出现为准并记一条警告；IP 不合法直接抛
    `ValueError`（整批不导入，免得半份名单悄悄生效）。
    """
    people: dict[str, str] = {}
    warnings: list[str] = []
    header_seen = False

    for lineno, raw in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        cells = _split(line)
        if not header_seen and cells and cells[0].strip().lower() in _NAME_HEADERS:
            header_seen = True
            continue
        name = cells[0].strip() if cells else ""
        if not name:
            continue
        if name in people:
            warnings.append(f"第 {lineno} 行：{name} 重复，沿用第一次的 IP")
            continue
        try:
            people[name] = _clean_ip(cells[1] if len(cells) > 1 else "")
        except ValueError as exc:
            raise ValueError(f"第 {lineno} 行 IP 不合法：{cells[1] if len(cells) > 1 else ''}（{exc}）") from None
    return people, warnings


def load_people_csv(path: Path) -> tuple[dict[str, str], list[str]]:
    """读 CSV：先按 UTF-8（含 BOM）解，失败退回 GBK（Excel 中文另存常见）。"""
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return parse_people_csv(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    return parse_people_csv(data.decode("utf-8", "replace"))


def dump_people_csv(people: dict[str, str]) -> str:
    """导出成同样的 CSV（以后给客户端分发用）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["姓名", "IP"])
    for name, ip in people.items():
        writer.writerow([name, ip])
    return buf.getvalue()


def merge_people(
    current: dict[str, str], incoming: dict[str, str], *, replace: bool = False
) -> tuple[dict[str, str], list[str]]:
    """合并名单：默认在现有基础上覆盖同名项，`replace=True` 则以新名单为准。

    返回 (新名单, 变更说明)，便于命令行打印"新增/更新/保留"各多少。
    """
    if replace:
        added = [name for name in incoming if name not in current]
        updated = [name for name in incoming if name in current and current[name] != incoming[name]]
        dropped = [name for name in current if name not in incoming]
        notes = []
        if added:
            notes.append(f"新增 {len(added)} 人：{'、'.join(added)}")
        if updated:
            notes.append(f"更新 {len(updated)} 人：{'、'.join(updated)}")
        if dropped:
            notes.append(f"移除 {len(dropped)} 人：{'、'.join(dropped)}")
        return dict(incoming), notes

    merged = dict(current)
    added, updated = [], []
    for name, ip in incoming.items():
        if name not in merged:
            added.append(name)
        elif merged[name] != ip:
            updated.append(name)
        merged[name] = ip
    notes = []
    if added:
        notes.append(f"新增 {len(added)} 人：{'、'.join(added)}")
    if updated:
        notes.append(f"更新 {len(updated)} 人：{'、'.join(updated)}")
    if not notes:
        notes.append("名单没有变化")
    return merged, notes
