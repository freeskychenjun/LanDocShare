"""pytest 共享夹具：把仓库根加入 sys.path，并集中存放各任务新用的夹具。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
