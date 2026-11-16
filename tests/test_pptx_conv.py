from __future__ import annotations

from pathlib import Path

import pytest

from landoc import pptx_conv


def test_plain_pptx_extracts_shape_text(plain_pptx_path: Path):
    md = pptx_conv.pptx_to_md(plain_pptx_path, encrypted=False)
    assert "## 第 1 页" in md
    assert "汇报人：王昱东" in md


def test_plain_pptx_extracts_table(plain_pptx_path: Path):
    md = pptx_conv.pptx_to_md(plain_pptx_path, encrypted=False)
    assert "正常蓄水位" in md and "58.5" in md
    assert "| --- |" in md or "|---|---|" in md


def test_plain_pptx_has_one_section_per_slide(plain_pptx_path: Path):
    md = pptx_conv.pptx_to_md(plain_pptx_path, encrypted=False)
    assert "## 第 1 页" in md and "## 第 2 页" in md


def test_unreadable_file_raises_typed_error(tmp_path: Path):
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a pptx")
    with pytest.raises(pptx_conv.PptxError):
        pptx_conv.pptx_to_md(bad, encrypted=False)


@pytest.mark.slow
def test_encrypted_pptx_goes_through_com(encrypted_pptx_path: Path):
    md = pptx_conv.pptx_to_md(encrypted_pptx_path, encrypted=True)
    assert "## 第 1 页" in md
    assert len(md) > 5000
    assert "溪霞" in md
