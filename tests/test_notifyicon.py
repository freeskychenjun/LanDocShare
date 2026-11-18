import pytest

from landoc import notifyicon

pystray_win32 = pytest.importorskip("pystray._util.win32")


class _Entry:
    def __init__(self, uid=0):
        self.uID = uid
        self.uFlags = 0x7


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


def test_patch_retries_add_when_slot_already_taken(monkeypatch):
    """气泡通知可能先占了槽，导致 NIM_ADD 失败；此时应删掉再来一次。"""
    codes = []
    adds = {"n": 0}

    def fake_notify(code, data):
        codes.append(code)
        if code == 0:
            adds["n"] += 1
            return adds["n"] > 1  # 第一次 NIM_ADD 失败，重加成功
        return True

    monkeypatch.setattr(pystray_win32, "Shell_NotifyIcon", fake_notify)
    assert notifyicon.patch_pystray_uid() is True

    entry = _Entry()
    assert pystray_win32.Shell_NotifyIcon(0, entry) is True
    assert codes == [0, 2, 0]
    assert entry.uID == notifyicon.DEFAULT_UID


def test_patch_fills_icon_with_modify_when_add_keeps_failing(monkeypatch):
    """外壳已经建了空槽时，NIM_ADD 一直失败，改用 NIM_MODIFY 把图标补进去。"""
    codes = []

    def fake_notify(code, data):
        codes.append(code)
        return code != 0  # ADD 全失败，MODIFY/DELETE 成功

    monkeypatch.setattr(pystray_win32, "Shell_NotifyIcon", fake_notify)
    assert notifyicon.patch_pystray_uid() is True

    entry = _Entry()
    assert pystray_win32.Shell_NotifyIcon(0, entry) is True
    assert codes == [0, 2, 0, 1]
    assert entry.uFlags & 0x1  # NIF_MESSAGE 补上，右键才有回调


def test_patch_schedules_deferred_retry_when_everything_keeps_failing(monkeypatch):
    """删了重加、再补 NIM_MODIFY 都不行时，交给后台线程再补几次。"""
    scheduled = []

    def fake_notify(code, data):
        return False if code in (0, 1) else True

    monkeypatch.setattr(pystray_win32, "Shell_NotifyIcon", fake_notify)
    monkeypatch.setattr(notifyicon, "_retry_later",
                        lambda original, data, debug: scheduled.append(data))
    assert notifyicon.patch_pystray_uid() is True

    entry = _Entry()
    assert pystray_win32.Shell_NotifyIcon(0, entry) is False
    assert scheduled == [entry]
