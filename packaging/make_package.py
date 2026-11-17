"""把 dist/LanDocShare/ 打成给同事下载的 zip（设计 §9 分发）。

用法：
    python packaging/make_package.py            # 产物 distribution/LanDocShare.zip
    python packaging/make_package.py --out D:\\share\\LanDocShare.zip

登记中心用 `--package-zip` 指向这个文件（不给时自动在 exe 同目录、distribution/ 里找）。
"""

from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = ROOT / "dist" / "LanDocShare"
DEFAULT_OUT = ROOT / "distribution" / "LanDocShare.zip"


def build(dist_dir: Path, out_path: Path) -> Path:
    if not (dist_dir / "LanDocShare.exe").is_file():
        raise FileNotFoundError(f"没找到 {dist_dir / 'LanDocShare.exe'}，先跑 PyInstaller")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    count = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(dist_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = f"LanDocShare/{path.relative_to(dist_dir).as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
            count += 1
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"[OK] {out_path} · {count} 个文件 · {size_mb:.1f} MB · {time.time() - started:.1f} 秒")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make_package", description="打库软件分发 zip")
    parser.add_argument("--dist", default=str(DEFAULT_DIST))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    try:
        build(Path(args.dist), Path(args.out))
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
