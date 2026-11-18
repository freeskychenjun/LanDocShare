"""发布新版本：一条命令产出分发包 + 版本清单（设计 §9 自动更新）。

做四件事：
1. 用 PyInstaller 打 `dist/LanDocShare/`（`--skip-build` 可跳过，直接拿现成的）；
2. 压成 `distribution/LanDocShare-<版本>.zip`（外加 `LanDocShare.zip` 给首次安装）；
3. 算 sha256、字节数，写 `distribution/latest.json`（登记中心靠它判断"最新版是谁"）；
4. 打印同事侧会看到的变化，方便你贴到群里。

用法：
    python packaging/release.py --notes "修了 xx 问题" --notes "新增 yy"
    python packaging/release.py --skip-build --version 0.2.1 --notes "只改了一行"

规矩（故意做严）：版本号只能往前，同版本或降级会被拒绝（`--force` 才能覆盖）；
版本号必须和 `landoc/__init__.py` 里的 `__version__` 一致，避免"清单写 0.2.1、exe 还是 0.2.0"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from landoc import __version__  # noqa: E402 - 要在改完 sys.path 之后导入
from landoc.registry import MANIFEST_NAME as _DEFAULT_MANIFEST  # noqa: E402
from landoc.updater import is_newer, parse_version  # noqa: E402

DEFAULT_DIST = ROOT / "dist" / "LanDocShare"
DEFAULT_OUT_DIR = ROOT / "distribution"
SPEC = ROOT / "packaging" / "LanDocShare.spec"
PLATFORM = "windows-x64"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fix_console_text(text: str) -> str:
    """复原"UTF-8 字节被按 GBK 解码"的乱码。

    Windows 中文控制台给 Python 的 argv 是按 ANSI(GBK) 解的，直接传中文说明
    会变成 `淇锛氭墭鐩樻湁` 这种。这里反向试一次：能 GBK 编码、又能当 UTF-8
    解回来，就认为原本是 UTF-8 中文。
    """
    try:
        repaired = text.encode("cp936", "strict").decode("utf-8", "strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".manifest", suffix=".tmp", dir=path.parent)
    try:
        with open(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def build_dist() -> None:
    """跑 PyInstaller（spec 里 onedir + 排除重包，约 3-5 分钟）。"""
    print(f"[1/3] 打包：{SPEC.relative_to(ROOT)}")
    started = time.time()
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PyInstaller 失败（退出码 {result.returncode}）")
    print(f"      完成，用时 {time.time() - started:.0f} 秒")


def make_zip(dist_dir: Path, out_path: Path) -> Path:
    """按固定时间戳打包，同样的输入得到同样的 zip（便于比对）。"""
    if not (dist_dir / "LanDocShare.exe").is_file():
        raise FileNotFoundError(f"没找到 {dist_dir / 'LanDocShare.exe'}，先打包或去掉 --skip-build")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import zipfile

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(dist_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = f"LanDocShare/{path.relative_to(dist_dir).as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return out_path


def load_previous_manifest(out_dir: Path) -> dict:
    path = out_dir / _DEFAULT_MANIFEST
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def release(
    version: str,
    *,
    dist_dir: Path = DEFAULT_DIST,
    out_dir: Path = DEFAULT_OUT_DIR,
    notes: list[str] | None = None,
    min_from: str = "",
    mandatory: bool = False,
    skip_build: bool = False,
    force: bool = False,
) -> dict:
    parsed = parse_version(version)
    if parsed is None:
        raise ValueError(f"版本号不合规：{version!r}，要写成 1.2.3")
    if version.lstrip("v") != __version__:
        raise ValueError(
            f"版本号不一致：命令行给的是 {version}，代码里是 {__version__}。"
            f"先把 landoc/__init__.py 与 pyproject.toml 改成 {version}，再发布。"
        )

    out_dir = Path(out_dir)
    previous = load_previous_manifest(out_dir)
    old_version = str(previous.get("version") or "")
    if old_version and not is_newer(version, old_version) and not force:
        raise ValueError(
            f"已经发布过 v{old_version}，不能再发 {version}。"
            f"要么把版本号往上加，要么加 --force 覆盖（同事可能已经下载过旧包，慎用）。"
        )

    if not skip_build:
        build_dist()
    else:
        print("[1/3] 跳过打包，用现有 dist/")

    print(f"[2/3] 压缩：{dist_dir} → {out_dir}")
    zip_name = f"LanDocShare-{version}.zip"
    zip_path = make_zip(Path(dist_dir), out_dir / zip_name)
    # /download 给"第一次装"的老人用：保持旧文件名可用
    shutil.copy2(zip_path, out_dir / "LanDocShare.zip")

    size = zip_path.stat().st_size
    digest = _sha256(zip_path)
    manifest = {
        "version": version,
        "platform": PLATFORM,
        "file": zip_name,
        "size": size,
        "sha256": digest,
        "released_at": time.strftime("%Y-%m-%d %H:%M"),
        "notes": list(notes or []),
        "min_from": min_from.strip(),
        "mandatory": bool(mandatory),
    }
    _write_json(out_dir / _DEFAULT_MANIFEST, manifest)

    print(f"[3/3] 版本清单：{out_dir / _DEFAULT_MANIFEST}")
    print(f"      v{version} · {size / 1024 / 1024:.1f} MB · sha256 {digest[:16]}…")
    print("      登记中心无需重启：它按修改时间读清单，下一请求就是新版本。")
    print("      同事侧 6 小时内自动发现，或右键托盘 →「检查更新」。")
    if old_version:
        print(f"      上一版：v{old_version}")
    return manifest


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def prune(
    out_dir: Path = DEFAULT_OUT_DIR,
    dist_root: Path | None = None,
    *,
    apply: bool = False,
) -> list[tuple[Path, int]]:
    """清掉旧版本留下的包与构建产物，只保留最新那份（默认只打印，`apply=True` 才真删）。

    留：`latest.json`、当前版本的 zip、`LanDocShare.zip`（首次安装的固定下载名）、
    当前版本的说明文件，以及 `dist/LanDocShare`（下一次 `--skip-build` 还要用）。
    删：更早版本的 zip/说明、`dist/LanDocShare.old-*`、`dist/_old-*`。
    """
    out_dir = Path(out_dir)
    manifest = load_previous_manifest(out_dir)
    current = str(manifest.get("version") or "")
    root = Path(dist_root) if dist_root is not None else DEFAULT_DIST.parent

    doomed: list[tuple[Path, int]] = []
    # 不知道"最新是哪版"就一个 zip 都别删：宁可占点磁盘，也不能把要用的包删掉
    if current and out_dir.is_dir():
        for path in sorted(out_dir.glob("LanDocShare-*.zip")):
            if path.stem == f"LanDocShare-{current}":
                continue
            doomed.append((path, _path_size(path)))
        for path in sorted(out_dir.glob("notes-*.txt")):
            if path.stem == f"notes-{current}":
                continue
            doomed.append((path, _path_size(path)))
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.is_dir() and path.name.startswith(("LanDocShare.old-", "_old-")):
                doomed.append((path, _path_size(path)))

    total = 0
    for path, size in doomed:
        total += size
        action = "删除" if apply else "可删"
        print(f"  [{action}] {path} — {size / 1024 / 1024:.1f} MB")
    print(f"  合计 {len(doomed)} 项、{total / 1024 / 1024:.1f} MB{'（已删除）' if apply else '（--apply 才真删）'}")
    if apply:
        for path, _size in doomed:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    return doomed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release", description="发布 LanDocShare 新版本")
    parser.add_argument("--version", default=__version__, help=f"要发布的版本（默认 {__version__}）")
    parser.add_argument("--dist", default=str(DEFAULT_DIST), help="PyInstaller 产物目录")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="zip 与 latest.json 放哪")
    parser.add_argument("--notes", action="append", default=[], help="更新说明，可重复")
    parser.add_argument("--notes-file", default=None, help="更新说明文件（每行一条）")
    parser.add_argument("--min-from", default="", help="低于这个版本的客户端必须手动重装")
    parser.add_argument("--mandatory", action="store_true", help="标记为强烈建议升级")
    parser.add_argument("--skip-build", action="store_true", help="不跑 PyInstaller，直接压 dist/")
    parser.add_argument("--force", action="store_true", help="允许同版本/降级覆盖（慎用）")
    parser.add_argument("--prune", action="store_true", help="清掉旧版本的 zip 与历史构建产物（只打印，不动手）")
    parser.add_argument("--apply", action="store_true", help="配 --prune 用：真的删")
    args = parser.parse_args(argv)

    if args.prune:
        prune(Path(args.out_dir), Path(args.dist).parent, apply=args.apply)
        return 0

    notes = [_fix_console_text(note) for note in args.notes]
    if args.notes_file:
        try:
            notes += [
                line.strip()
                for line in Path(args.notes_file).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError as exc:
            print(f"[错误] 读不了说明文件：{exc}", file=sys.stderr)
            return 2
    try:
        release(
            args.version,
            dist_dir=Path(args.dist),
            out_dir=Path(args.out_dir),
            notes=notes,
            min_from=args.min_from,
            mandatory=args.mandatory,
            skip_build=args.skip_build,
            force=args.force,
        )
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
