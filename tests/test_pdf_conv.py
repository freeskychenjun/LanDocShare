from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from landoc import pdf_conv
from landoc.config import Config


def test_plain_pdf_uses_pymupdf(plain_pdf_path: Path):
    result = pdf_conv.pdf_to_text(plain_pdf_path)
    assert result.via == "pymupdf"
    assert "附件1" in result.text


def test_encrypted_pdf_without_acrobat_uses_disguise(
    encrypted_pdf_path, cfg_with_tool_dir, monkeypatch, plaintext_pdf_bytes
):
    """没有 Acrobat 时走兜底：解临时副本 → 抽文本 → 删副本。"""
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: False)
    monkeypatch.setattr(
        pdf_conv, "_run_tool", lambda tool, target, cwd, timeout: target.write_bytes(plaintext_pdf_bytes)
    )
    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_with_tool_dir)
    assert result.via == "acrobat_disguise"
    assert "附件1" in result.text


def test_disguise_leaves_no_plaintext_copy(
    encrypted_pdf_path, cfg_with_tool_dir, monkeypatch, plaintext_pdf_bytes, tmp_path
):
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: False)
    monkeypatch.setattr(
        pdf_conv, "_run_tool", lambda tool, target, cwd, timeout: target.write_bytes(plaintext_pdf_bytes)
    )
    pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_with_tool_dir)
    leftovers = list(Path(tmp_path).rglob("*.pdf"))
    assert all(p.read_bytes()[:4] != b"%PDF" or p.name == "plain.pdf" for p in leftovers)


def test_encrypted_pdf_without_tool_is_skipped(encrypted_pdf_path, cfg_without_tool, monkeypatch):
    monkeypatch.setattr(pdf_conv, "has_acrobat", lambda: False)
    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg_without_tool)
    assert result.via == "skipped"
    assert result.reason == "no_reader"


def test_unreadable_path_is_skipped(tmp_path: Path):
    result = pdf_conv.pdf_to_text(tmp_path / "nope.pdf")
    assert result.via == "skipped"
    assert result.reason == "unreadable"


def test_pdf_without_text_layer_reports_reason(tmp_path: Path):
    import fitz

    doc = fitz.open()
    doc.new_page()  # 空白页：没有文本层
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    result = pdf_conv.pdf_to_text(path)
    assert result.via == "pymupdf"
    assert result.reason == "no_text_layer"


def test_decrypt_via_disguise_rejects_tool_that_changed_nothing(
    encrypted_pdf_path: Path, cfg_with_tool_dir, monkeypatch
):
    monkeypatch.setattr(pdf_conv, "_run_tool", lambda tool, target, cwd, timeout: None)
    with pytest.raises(pdf_conv.PDFDecryptError):
        pdf_conv.decrypt_via_disguise(encrypted_pdf_path, cfg_with_tool_dir.acrobat_tool_dir)


@pytest.mark.slow
def test_real_encrypted_pdf_decrypts_with_real_tool(encrypted_pdf_path: Path):
    """真机：拿库主人现成的 acrobat.exe 解一份副本，验证文本层可读。"""
    tool_dir = Path(r"D:\应用软件\右键解密")
    if not (tool_dir / "acrobat.exe").is_file():
        pytest.skip(f"兜底工具不存在：{tool_dir}")
    cfg = Config(name="t", lib_dir=encrypted_pdf_path.parent, acrobat_tool_dir=tool_dir)
    result = pdf_conv.pdf_to_text(encrypted_pdf_path, cfg=cfg)
    assert result.via in {"acrobat_disguise", "acrobat"}
    assert len(result.text) > 3000
    assert "十五五" in result.text
