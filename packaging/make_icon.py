"""生成打包用的 landoc.ico（同一张图的多尺寸版本）。

跑法：python packaging/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from landoc.tray import make_icon_image  # noqa: E402


def main() -> int:
    target = Path(__file__).with_name("landoc.ico")
    image = make_icon_image(256)
    image.save(target, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"[OK] {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
