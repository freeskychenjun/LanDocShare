"""把仓库里的 skill 同步到本机部署目录（以及 dist 里随包的那份）。

仓库 `skill/lan-lib-search/` 是唯一源：`/skill.zip` 现场从它打包给同事，本机
`~/.agents/skills/lan-lib-search` 是运行时那份。两边漂移过（2026-11-24 的
自适应超时、跨库去重只改了本机那份），所以给它一个能对账的命令：

    python packaging/sync-skill.py            # 只对比（等价 --check），不一致退出码 1
    python packaging/sync-skill.py --install  # 仓库 → 本机（顺带刷新 dist 里的随包副本）
    python packaging/sync-skill.py --pull     # 本机 → 仓库（本机手改过、想收回来时用）

只管源码文件（SKILL.md、scripts/*.py 等）；`__pycache__`、`*.pyc`、`latency*.json`
这类运行产物不参与对比，也不会被搬过去。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "lan-lib-search"
SOURCE = ROOT / "skill" / NAME
DEPLOYED = Path.home() / ".agents" / "skills" / NAME
DIST = ROOT / "dist" / "LanDocShare" / "_internal" / "skill" / NAME

SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def source_files(root: Path) -> dict[str, Path]:
    """目录里要同步的文件（相对路径 → 绝对路径），跳过运行产物。"""
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix in SKIP_SUFFIX or path.name.startswith("latency"):
            continue
        found[rel.as_posix()] = path
    return found


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(src: Path, dst: Path) -> tuple[list[str], list[str], list[str], list[str]]:
    """返回 (一致, 内容不同, 只在源里, 只在目标里) 的相对路径列表。"""
    left = source_files(src)
    right = source_files(dst)
    same, differ, missing, extra = [], [], [], []
    for rel, path in left.items():
        other = right.get(rel)
        if other is None:
            missing.append(rel)
        elif digest(path) == digest(other):
            same.append(rel)
        else:
            differ.append(rel)
    extra = sorted(set(right) - set(left))
    return same, differ, missing, extra


def report(title: str, src: Path, dst: Path) -> int:
    """打印一处对比结果，返回不一致的文件数。"""
    same, differ, missing, extra = compare(src, dst)
    if not dst.exists():
        print(f"{title}：目标不存在 {dst}")
        return len(missing) or 1
    print(f"{title}：{dst}")
    for rel in same:
        print(f"  一致    {rel}")
    for rel in differ:
        print(f"  不一致  {rel}")
    for rel in missing:
        print(f"  缺      {rel}")
    for rel in extra:
        print(f"  多出    {rel}（目标端独有，不回写）")
    return len(differ) + len(missing)


def install(src: Path, dst: Path) -> int:
    copied = 0
    for rel, path in source_files(src).items():
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and digest(target) == digest(path):
            continue
        shutil.copy2(path, target)
        copied += 1
    print(f"  写入 {copied} 个文件 → {dst}")
    return copied


def pull(src: Path, dst: Path) -> int:
    copied = 0
    for rel, path in source_files(src).items():
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and digest(target) == digest(path):
            continue
        shutil.copy2(path, target)
        copied += 1
    print(f"  收回 {copied} 个文件 → {dst}")
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description="仓库 skill ↔ 本机部署目录对账/同步")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只对比（默认）")
    group.add_argument("--install", action="store_true", help="仓库 → 本机部署目录")
    group.add_argument("--pull", action="store_true", help="本机部署目录 → 仓库")
    parser.add_argument("--dist", action="store_true", help="同时处理 dist 里的随包副本（--install 时自动）")
    args = parser.parse_args()

    if not SOURCE.is_dir():
        print(f"找不到仓库里的 skill 模板：{SOURCE}", file=sys.stderr)
        return 2

    if args.pull:
        if not DEPLOYED.is_dir():
            print(f"本机没有部署目录：{DEPLOYED}", file=sys.stderr)
            return 2
        print(f"本机 → 仓库：{SOURCE}")
        pull(DEPLOYED, SOURCE)
        report("仓库 ↔ 本机", SOURCE, DEPLOYED)
        return 0

    if args.install:
        print(f"仓库 → 本机：{DEPLOYED}")
        install(SOURCE, DEPLOYED)
        if DIST.is_dir() or args.dist:
            print(f"仓库 → dist 随包副本：{DIST}")
            install(SOURCE, DIST)
        return 0 if report("仓库 ↔ 本机", SOURCE, DEPLOYED) == 0 else 1

    problems = report("仓库 ↔ 本机", SOURCE, DEPLOYED)
    if args.dist and DIST.is_dir():
        problems += report("仓库 ↔ dist", SOURCE, DIST)
    if problems:
        print("\n两边不一致：改仓库那份，然后 python packaging/sync-skill.py --install")
        return 1
    print("\n一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
