from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import docx
import pytest

from landoc import office_bridge


def test_flat_opc_to_docx_bytes_produces_valid_zip(flat_opc):
    data = office_bridge.flat_opc_to_docx_bytes(flat_opc)
    assert data[:4] == b"PK\x03\x04"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "[Content_Types].xml" in names  # 必须自动合成
    assert "word/document.xml" in names


def test_flat_opc_to_docx_bytes_keeps_binary_parts(flat_opc):
    names = zipfile.ZipFile(io.BytesIO(office_bridge.flat_opc_to_docx_bytes(flat_opc))).namelist()
    assert all(n.startswith("/") is False for n in names)


def test_flat_opc_to_docx_bytes_parsable_by_python_docx(flat_opc, tmp_path):
    path = tmp_path / "rebuilt.docx"
    path.write_bytes(office_bridge.flat_opc_to_docx_bytes(flat_opc))
    d = docx.Document(path)
    assert len(d.paragraphs) > 10
    assert d.tables and d.tables[0].cell(0, 0).text == "R0C0"


def test_extract_wordopenxml_times_out(monkeypatch, plain_docx_path):
    """超时必须抛 OfficeTimeout，而不是把整批卡死。"""
    monkeypatch.setattr(office_bridge, "_open_document", lambda app, path: time.sleep(3))
    monkeypatch.setattr(office_bridge, "_dispatch_office", lambda: object())
    with pytest.raises(office_bridge.OfficeTimeout):
        office_bridge.extract_wordopenxml(plain_docx_path, timeout=0.5)


def test_extract_wordopenxml_without_office_raises(monkeypatch, plain_docx_path):
    def boom():
        raise office_bridge.OfficeUnavailable("no office")

    monkeypatch.setattr(office_bridge, "_dispatch_office", boom)
    with pytest.raises(office_bridge.OfficeUnavailable):
        office_bridge.extract_wordopenxml(plain_docx_path)


@pytest.mark.slow
def test_extract_wordopenxml_on_plaintext_docx(plain_docx_path: Path):
    xml = office_bridge.extract_wordopenxml(plain_docx_path)
    assert "<pkg:package" in xml
    assert "word/document.xml" in xml


@pytest.mark.slow
def test_extract_wordopenxml_on_encrypted_docx(encrypted_docx_path: Path):
    xml = office_bridge.extract_wordopenxml(encrypted_docx_path)
    assert "<pkg:package" in xml
    assert len(xml) > 1_000_000


@pytest.mark.slow
def test_encrypted_docx_roundtrip_yields_plaintext_docx(encrypted_docx_path: Path, tmp_path):
    xml = office_bridge.extract_wordopenxml(encrypted_docx_path)
    out = tmp_path / "rebuilt.docx"
    out.write_bytes(office_bridge.flat_opc_to_docx_bytes(xml))
    assert out.read_bytes()[:4] == b"PK\x03\x04"  # python 写盘 = 明文
    assert len(docx.Document(out).paragraphs) > 50


def test_password_dialog_maps_to_password_protected(monkeypatch, plain_docx_path):
    """I4: 口令文档是"弹框阻塞→超时"，必须识别成 password_protected 而不是 timeout。"""
    import time as _time

    monkeypatch.setattr(office_bridge, "_open_document", lambda app, path: _time.sleep(3))
    monkeypatch.setattr(office_bridge, "_dispatch_office", lambda: object())
    monkeypatch.setattr(office_bridge, "_office_pids", lambda: set())
    monkeypatch.setattr(office_bridge, "_kill_office_processes", lambda pids=None: None)
    monkeypatch.setattr(office_bridge, "_has_password_dialog", lambda pids: True)
    with pytest.raises(office_bridge.PasswordProtected):
        office_bridge.extract_wordopenxml(plain_docx_path, timeout=0.4)


def test_timeout_without_dialog_stays_office_timeout(monkeypatch, plain_docx_path):
    import time as _time

    monkeypatch.setattr(office_bridge, "_open_document", lambda app, path: _time.sleep(3))
    monkeypatch.setattr(office_bridge, "_dispatch_office", lambda: object())
    monkeypatch.setattr(office_bridge, "_office_pids", lambda: set())
    monkeypatch.setattr(office_bridge, "_kill_office_processes", lambda pids=None: None)
    monkeypatch.setattr(office_bridge, "_has_password_dialog", lambda pids: False)
    with pytest.raises(office_bridge.OfficeTimeout):
        office_bridge.extract_wordopenxml(plain_docx_path, timeout=0.4)
