"""扫描件 OCR：选路、收尾与兜底顺序。

这些用例都不真跑识别（那要几百毫秒到几秒，且依赖机器装了哪种语言包）；
真机识别另有一条 slow 用例，`LANSHARE_IT=1` 时才跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from landoc import ocr


def test_tidy_removes_spaces_windows_ocr_inserts_between_chinese():
    raw = "江 西 省 水 文 监 测 中 心 关 于\n第 2 页 ， 表 1. 1. 1 第 23 行"
    assert ocr.tidy(raw) == "江西省水文监测中心关于\n第2页，表1.1.1第23行"


def test_tidy_keeps_spaces_between_english_words():
    assert ocr.tidy("Adobe Acrobat Reader DC") == "Adobe Acrobat Reader DC"


def test_tidy_collapses_blank_lines_and_strips():
    assert ocr.tidy("  第一段  \n\n\n\n第二段  ") == "第一段\n\n第二段"


def test_windows_available_accepts_language_variant(monkeypatch):
    monkeypatch.setattr(ocr, "available_languages", lambda **kwargs: ("zh-Hans",))
    assert ocr.windows_available("zh-Hans-CN")


def test_backend_prefers_windows_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "available_languages", lambda **kwargs: ("zh-Hans-CN",))
    monkeypatch.setattr(ocr, "tesseract_exe", lambda: Path("tesseract.exe"))
    assert ocr.backend() == "windows"


def test_backend_falls_back_to_tesseract(monkeypatch):
    monkeypatch.setattr(ocr, "available_languages", lambda **kwargs: ())
    monkeypatch.setattr(ocr, "tesseract_exe", lambda: Path("tesseract.exe"))
    monkeypatch.setattr(ocr, "tesseract_languages", lambda: ("chi_sim", "eng"))
    assert ocr.backend() == "tesseract"


def test_backend_is_empty_when_no_engine(monkeypatch):
    monkeypatch.setattr(ocr, "available_languages", lambda **kwargs: ())
    monkeypatch.setattr(ocr, "tesseract_exe", lambda: None)
    assert ocr.backend() == ""
    assert "不可用" in ocr.describe()


def test_language_codes_map_chinese_variants():
    assert ocr._lang_codes("zh-Hans-CN") == ("chi_sim",)
    assert ocr._lang_codes("zh-Hant-TW") == ("chi_tra",)
    assert ocr._lang_codes("en-US") == ("en",)


def test_ocr_images_uses_windows_first(monkeypatch, tmp_path: Path):
    page = tmp_path / "p0001.png"
    page.write_bytes(b"x")
    monkeypatch.setattr(ocr, "backend", lambda lang=ocr.DEFAULT_LANG: "windows")
    monkeypatch.setattr(ocr, "_windows_ocr", lambda *a, **k: "第一页识别结果" * 5 + "\f第二页识别结果" * 5)

    def boom(*args, **kwargs):
        raise AssertionError("Windows OCR 成功时不该再跑 Tesseract")

    monkeypatch.setattr(ocr, "_tesseract_ocr", boom)
    text = ocr.ocr_images([("p0001", page)])
    assert text.startswith("第一页识别结果")
    assert "\f" in text


def test_ocr_images_falls_back_when_windows_returns_too_little(monkeypatch, tmp_path: Path):
    page = tmp_path / "p0001.png"
    page.write_bytes(b"x")
    monkeypatch.setattr(ocr, "backend", lambda lang=ocr.DEFAULT_LANG: "windows")
    monkeypatch.setattr(ocr, "_windows_ocr", lambda *a, **k: "短")
    monkeypatch.setattr(ocr, "tesseract_exe", lambda: Path("tesseract.exe"))
    monkeypatch.setattr(ocr, "_tesseract_ocr", lambda *a, **k: "很长的一段识别结果" * 10)
    assert ocr.ocr_images([("p0001", page)]).startswith("很长的一段识别结果")


def test_ocr_images_returns_none_without_engine(monkeypatch, tmp_path: Path):
    page = tmp_path / "p0001.png"
    page.write_bytes(b"x")
    monkeypatch.setattr(ocr, "backend", lambda lang=ocr.DEFAULT_LANG: "")
    monkeypatch.setattr(ocr, "tesseract_exe", lambda: None)
    assert ocr.ocr_images([("p0001", page)]) is None


def test_bundled_tesseract_ships_with_models():
    """随包的 Tesseract 必须能自洽：exe 在、中英模型在（否则"兜底"是假的）。"""
    exe = ocr.tools_dir() / "tesseract" / "tesseract.exe"
    if not exe.is_file():
        pytest.skip("源码树里没带 Tesseract（只打包版带）")
    assert "chi_sim" in ocr.tesseract_languages()
    assert "eng" in ocr.tesseract_languages()


def test_bundled_tesseract_runs_without_the_installed_one():
    """净化 PATH 之后随包那份还跑得起来：缺 DLL 只在这条路径上暴露（1.2）。"""
    exe = ocr.tools_dir() / "tesseract" / "tesseract.exe"
    if not exe.is_file():
        pytest.skip("源码树里没带 Tesseract（只打包版带）")
    ok, detail = ocr.verify_bundled_tesseract()
    assert ok, detail


@pytest.mark.slow
def test_real_ocr_reads_chinese_from_a_rendered_pdf(tmp_path: Path):
    """真机：把一行中文渲染成 PNG，走真实 OCR 路径看能不能认出来。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 120), "江西省水利厅文件", fontname="china-s", fontsize=32)
    pdf = tmp_path / "scan.pdf"
    doc.save(pdf)
    doc.close()
    if ocr.backend() == "":
        pytest.skip("这台机器没有可用的 OCR")
    text = ocr.ocr_images([("p0001", _render_first_page(pdf, tmp_path))])
    assert text and "江西省水利厅文件" in ocr.tidy(text).replace(" ", "")


def _render_first_page(pdf: Path, tmp_path: Path) -> Path:
    import fitz

    doc = fitz.open(pdf)
    try:
        target = tmp_path / "page.png"
        doc[0].get_pixmap(dpi=300).save(str(target))
        return target
    finally:
        doc.close()


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    sys.exit(pytest.main([__file__, "-q"]))
