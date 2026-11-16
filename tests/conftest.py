"""pytest 共享夹具：仓库根加入 sys.path；slow 标记默认跳过。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TESTDATA = Path(__file__).resolve().parent / "testdata"
DESKTOP_DOCX = Path(r"C:\Users\Administrator\Desktop\2026.11.10 乐平下闸蓄水设计自检报告-水文.docx")


def pytest_collection_modifyitems(config, items):
    """没设 LANSHARE_IT=1 时跳过 slow（要真机 Office / 真实加密文件）的用例。"""
    if os.environ.get("LANSHARE_IT") == "1":
        return
    skip = pytest.mark.skip(reason="需要真机环境：设置 LANSHARE_IT=1 后运行")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def flat_opc() -> str:
    """一份小文档的 Flat OPC 字符串（由 tests/testdata/make_flat_opc.py 生成）。"""
    return (TESTDATA / "flat_opc_sample.xml").read_text(encoding="utf-8")


@pytest.fixture
def plain_docx_path(tmp_path: Path) -> Path:
    """python-docx 生成的明文 docx：12 段 + 1 表。"""
    import docx

    d = docx.Document()
    d.add_heading("1  工程概况", level=1)
    for i in range(10):
        d.add_paragraph(f"第 {i + 1} 段正文")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "A"
    t.cell(1, 1).text = "B"
    path = tmp_path / "plain.docx"
    d.save(path)
    return path


@pytest.fixture
def encrypted_docx_path() -> Path:
    """桌面上那份真实加密 docx（密文头 17da5fa0），仅 slow 用例使用。"""
    if not DESKTOP_DOCX.is_file():
        pytest.skip(f"真实加密文件不存在：{DESKTOP_DOCX}")
    return DESKTOP_DOCX
