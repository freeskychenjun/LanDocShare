from __future__ import annotations

from landoc import probe


def test_classify_encrypted(tmp_path):
    p = tmp_path / "a.docx"
    p.write_bytes(bytes.fromhex("17da5fa01633cd9a") + b"x" * 64)
    assert probe.classify(p) == "encrypted"


def test_classify_plain_docx(tmp_path):
    p = tmp_path / "b.docx"
    p.write_bytes(b"PK\x03\x04" + b"x" * 64)
    assert probe.classify(p) == "plain"


def test_classify_plain_pdf(tmp_path):
    p = tmp_path / "c.pdf"
    p.write_bytes(b"%PDF-1.7" + b"x" * 64)
    assert probe.classify(p) == "plain"


def test_classify_plain_ole(tmp_path):
    p = tmp_path / "d.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0" + b"x" * 64)
    assert probe.classify(p) == "plain"


def test_classify_blocked_file_missing(tmp_path):
    assert probe.classify(tmp_path / "nope.docx") == "blocked"


def test_classify_blocked_unknown_magic(tmp_path):
    p = tmp_path / "e.docx"
    p.write_bytes(b"\x00\x01\x02\x03" + b"x" * 64)
    assert probe.classify(p) == "blocked"


def test_sample_report_counts(tmp_path):
    (tmp_path / "p.docx").write_bytes(b"PK\x03\x04" + b"x" * 32)
    (tmp_path / "e.docx").write_bytes(bytes.fromhex("17da5fa01633cd9a") + b"x" * 32)
    r = probe.sample_report([tmp_path], n=10)
    assert (r["plain"], r["encrypted"]) == (1, 1)
    assert set(r) >= {"sampled", "plain", "encrypted", "blocked", "has_office", "has_acrobat"}


def test_sample_report_skips_temp_and_non_docs(tmp_path):
    (tmp_path / "~$lock.docx").write_bytes(b"PK\x03\x04")
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "ok.docx").write_bytes(b"PK\x03\x04" + b"x" * 32)
    r = probe.sample_report([tmp_path], n=10)
    assert r["sampled"] == 1
