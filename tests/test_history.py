"""Per-minute history for the model fit."""

from __future__ import annotations

import pytest

from custom_components.heatflux.history import GAP_SETTLE_MINUTES, MinuteHistory

T0 = 1_800_000_000.0  # a whole minute


def fill(history: MinuteHistory, minutes: int, start: float = T0, **kwargs) -> None:
    """Two readings per minute."""
    for i in range(minutes * 2):
        history.add(
            start + i * 30,
            kwargs.get("temperature", 20.0 + i / 1000),
            kwargs.get("power", 1000.0 if i % 4 < 2 else 0.0),
            kwargs.get("weight", True),
        )


def test_minutes_are_binned() -> None:
    h = MinuteHistory(1000)
    h.add(T0, 20.0, 100.0, True)
    h.add(T0 + 30, 20.2, 300.0, True)
    h.add(T0 + 60, 20.4, 0.0, True)  # closes the first minute
    assert len(h) == 1
    assert h.temperature == [20.2]  # end of the minute
    assert h.power == [200.0]  # mean


def test_settling_after_start_and_gap() -> None:
    h = MinuteHistory(10_000)
    fill(h, GAP_SETTLE_MINUTES + 10)
    assert h.weight[:GAP_SETTLE_MINUTES] == [False] * GAP_SETTLE_MINUTES
    assert all(h.weight[GAP_SETTLE_MINUTES : len(h)])
    # The last minute is still open; it closes with the next reading.
    length = len(h) + 1
    # 30 minutes without data.
    fill(h, 10, start=T0 + (GAP_SETTLE_MINUTES + 10 + 30) * 60)
    assert len(h) == length + 30 + 9
    assert h.temperature[length : length + 30] == [None] * 30
    assert h.weight[length - 1]
    assert not any(h.weight[length:])


def test_unweighted_readings() -> None:
    h = MinuteHistory(1000)
    fill(h, GAP_SETTLE_MINUTES + 5, weight=False)
    assert not any(h.weight)
    h = MinuteHistory(1000)
    for i in range(10):
        h.add(T0 + i * 60, 20.0, None, True)  # unknown AC power
    assert not any(h.weight)
    assert h.power == [0.0] * 9


def test_keeps_only_max_minutes() -> None:
    h = MinuteHistory(60)
    fill(h, 100)
    assert len(h) == 60
    assert h.start == int(T0 // 60) + 39


def test_series_downsampling() -> None:
    h = MinuteHistory(1000)
    fill(h, GAP_SETTLE_MINUTES + 21)
    s = h.series(2)
    assert len(s) == (GAP_SETTLE_MINUTES + 20) // 2
    assert s.dt == pytest.approx(1 / 30)
    assert s.power[0] == pytest.approx(500.0)
    assert s.temperature[0] == h.temperature[1]
    assert s.weight[-1] is True


def test_active_changes() -> None:
    h = MinuteHistory(1000)
    fill(h, 21)  # on, off alternating every minute
    assert h.active_changes() == 19


def test_round_trip() -> None:
    h = MinuteHistory(1000)
    fill(h, 30)
    copy = MinuteHistory.from_dict(h.as_dict(), 1000)
    assert copy.start == h.start
    assert copy.power == h.power
    assert copy.weight == h.weight
    assert copy.temperature == pytest.approx(h.temperature, abs=1e-3)
    assert len(MinuteHistory.from_dict(h.as_dict(), 10)) == 10
    assert len(MinuteHistory.from_dict({"start": 5}, 1000)) == 0
    assert len(MinuteHistory.from_dict({}, 1000)) == 0
