"""发版时注入的"随包默认值"。

源码里不写单位内网地址：`tests/test_site_networks.py` 会扫 `landoc/**/*.py`，
出现真实网段就报错。所以真实地址放在打包时生成的 `site_defaults.json` 里
（`packaging/release.py --registry-url …` 写出来），随包一起分发；源码运行时
读不到这个文件，就退回空串（= 不登记，行为和以前一样）。

这样新同事解压、双击、跟着向导走完，库就直接登记到中心，不用再手改
`config.json`——以前正是这一步没人知道，装完的库在中心列表里根本看不见。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SITE_FILE = "site_defaults.json"


def site_dir() -> Path:
    """site_defaults.json 所在目录（打包后是 `_internal/landoc`）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "landoc"
    return Path(__file__).resolve().parent


def site_file() -> Path:
    return site_dir() / SITE_FILE


def shipped_registry_url() -> str:
    """打包时写进包的登记中心地址；源码运行或没写时返回空串。"""
    try:
        raw = site_file().read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("registry_url") or "").strip()


def write_site_defaults(path: Path | str, *, registry_url: str) -> Path:
    """打包脚本用：把这次要发出去的登记中心地址写成数据文件。

    先写临时文件再替换：中断时不会留下半个 JSON，被打进包里。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"registry_url": str(registry_url or "").strip()}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target
