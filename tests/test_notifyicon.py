import pytest

from landoc import notifyicon

pystray_win32 = pytest.importorskip("pystray._util.win32")


class _Entry:
    def __init__(self, uid=0):
        self.uID = uid


class _Pointer:
    def __init__(self, entry):
        self.contents = entry


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_notify(code, data):
        recorded.append((code, data))
        return True

    monkeypatch.setattr(pystray_win32, "Shell_NotifyIcon", fake_notify)
    return recorded


def test_patch_fills_missing_uid(calls):
    assert notifyicon.patch_pystray_uid() is True
    entry = _Entry()
    pystray_win32.Shell_NotifyIcon(0, entry)
    assert entry.uID == notifyicon.DEFAULT_UID
    assert calls and calls[0][0] == 0


def test_patch_also_handles_pointer_payload(calls):
    notifyicon.patch_pystray_uid(uid=0x1234)
    entry = _Entry()
    pystray_win32.Shell_NotifyIcon(1, _Pointer(entry))
    assert entry.uID == 0x1234


def test_patch_keeps_existing_uid(calls):
    notifyicon.patch_pystray_uid()
    entry = _Entry(uid=99)
    pystray_win32.Shell_NotifyIcon(0, entry)
    assert entry.uID == 99


def test_patch_is_idempotent(calls):
    assert notifyicon.patch_pystray_uid() is True
    first = pystray_win32.Shell_NotifyIcon
    assert notifyicon.patch_pystray_uid() is True
    assert pystray_win32.Shell_NotifyIcon is first
    entry = _Entry()
    pystray_win32.Shell_NotifyIcon(0, entry)
    assert len(calls) == 1


def test_entry_of_accepts_struct_and_pointer():
    entry = _Entry()
    assert notifyicon._entry_of(entry) is entry
    assert notifyicon._entry_of(_Pointer(entry)) is entry
