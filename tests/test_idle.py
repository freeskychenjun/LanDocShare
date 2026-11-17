"""空闲判定：锁屏或长时间无输入才算空闲，读不到输入时间按"不空闲"。"""

from __future__ import annotations

from landoc import idle


def _stub(monkeypatch, *, locked=False, tick=1_000_000, last=1_000_000):
    monkeypatch.setattr(idle, "is_session_locked", lambda: locked)
    monkeypatch.setattr(idle, "_tick_ms", lambda: tick)
    monkeypatch.setattr(idle, "_last_input_ms", lambda: last)


def test_locked_session_counts_as_idle(monkeypatch):
    _stub(monkeypatch, locked=True, tick=1000, last=1000)
    assert idle.is_idle(5) is True


def test_recent_input_is_not_idle(monkeypatch):
    _stub(monkeypatch, tick=1_000_000, last=1_000_000 - 60_000)
    assert idle.is_idle(5) is False


def test_five_minutes_without_input_is_idle(monkeypatch):
    _stub(monkeypatch, tick=1_000_000, last=1_000_000 - 5 * 60 * 1000)
    assert idle.is_idle(5) is True


def test_just_below_threshold_is_not_idle(monkeypatch):
    _stub(monkeypatch, tick=1_000_000, last=1_000_000 - (5 * 60 * 1000 - 1))
    assert idle.is_idle(5) is False


def test_tick_counter_wraparound_does_not_go_negative(monkeypatch):
    # dwTime 是 32 位：49.7 天回绕后 elapsed 仍然算得对（这里只有 2 秒）
    _stub(monkeypatch, tick=1000, last=0xFFFFFFFF - 1000)
    assert idle.seconds_since_input() == 2.001


def test_unknown_last_input_is_not_idle(monkeypatch):
    monkeypatch.setattr(idle, "is_session_locked", lambda: False)
    monkeypatch.setattr(idle, "_last_input_ms", lambda: None)
    assert idle.is_idle(0) is False
