"""Per-minute history of room temperature and AC heat for the model fit."""

from __future__ import annotations

from typing import Any

from .model import Series

# After a gap the filter needs a while to find the room's state again.
GAP_SETTLE_MINUTES = 120


class MinuteHistory:
    """Room temperature (end of each minute) and mean AC heat per minute.

    `weight` is False for minutes that must not count for the fit: paused,
    unknown AC direction, missing data and the time right after a gap.
    """

    def __init__(self, max_minutes: int) -> None:
        """Initialize empty."""
        self.max_minutes = max_minutes
        self.start: int | None = None  # epoch minute of the first entry
        self.temperature: list[float | None] = []
        self.power: list[float] = []
        self.weight: list[bool] = []
        self._minute: int | None = None
        self._temperature: float | None = None
        self._power_sum = 0.0
        self._power_count = 0
        self._weight = True
        self._settle_until = -1

    def __len__(self) -> int:
        """Stored minutes."""
        return len(self.power)

    @property
    def end(self) -> float | None:
        """Time (s) at the end of the last stored minute."""
        return None if self.start is None else (self.start + len(self)) * 60.0

    @property
    def hours(self) -> float:
        """Stored hours."""
        return len(self) / 60

    def add(
        self, t: float, temperature: float | None, power: float | None, weight: bool
    ) -> None:
        """Add a reading at time t (s); readings are binned per minute."""
        minute = int(t // 60)
        if self._minute is not None and minute < self._minute:
            return
        if self._minute is not None and minute != self._minute:
            self._flush()
        if self._minute is None or minute != self._minute:
            self._open(minute)
        if temperature is not None:
            self._temperature = temperature
        if power is None:
            self._weight = False
        else:
            self._power_sum += power
            self._power_count += 1
        self._weight = self._weight and weight

    def _open(self, minute: int) -> None:
        self._minute = minute
        self._temperature = None
        self._power_sum = 0.0
        self._power_count = 0
        self._weight = True

    def _flush(self) -> None:
        minute = self._minute
        assert minute is not None
        if self.start is None:
            self.start = minute
            self._settle_until = minute + GAP_SETTLE_MINUTES
        expected = self.start + len(self)
        if minute > expected:
            # Missing minutes: no temperature, power unknown (held), no weight.
            held = self.power[-1] if self.power else 0.0
            for _ in range(min(minute - expected, self.max_minutes)):
                self.temperature.append(None)
                self.power.append(held)
                self.weight.append(False)
            if minute - expected > self.max_minutes:
                self.start = minute - len(self)
            self._settle_until = minute + GAP_SETTLE_MINUTES
        power = self._power_sum / self._power_count if self._power_count else None
        self.temperature.append(self._temperature)
        self.power.append(
            power if power is not None else (self.power[-1] if self.power else 0.0)
        )
        self.weight.append(
            self._weight
            and power is not None
            and self._temperature is not None
            and minute >= self._settle_until
        )
        excess = len(self) - self.max_minutes
        if excess > 0:
            del self.temperature[:excess]
            del self.power[:excess]
            del self.weight[:excess]
            self.start += excess

    def series(self, step_minutes: int = 1) -> Series:
        """The history on a grid of step_minutes (last temperature, mean power)."""
        step = max(1, step_minutes)
        count = len(self) // step
        temperature: list[float | None] = []
        power: list[float] = []
        weight: list[bool] = []
        for i in range(count):
            lo, hi = i * step, (i + 1) * step
            temperature.append(self.temperature[hi - 1])
            power.append(sum(self.power[lo:hi]) / step)
            weight.append(all(self.weight[lo:hi]))
        return Series(dt=step / 60, temperature=temperature, power=power, weight=weight)

    def active_changes(self, threshold: float = 100.0) -> int:
        """How often the AC switched between off and on (|power| > threshold)."""
        changes = 0
        previous: bool | None = None
        for power in self.power:
            on = abs(power) > threshold
            if previous is not None and on != previous:
                changes += 1
            previous = on
        return changes

    def clear(self) -> None:
        """Forget everything."""
        self.__init__(self.max_minutes)  # type: ignore[misc]

    def as_dict(self) -> dict[str, Any]:
        """For storage."""
        return {
            "start": self.start,
            "temperature": [
                None if t is None else round(t, 3) for t in self.temperature
            ],
            "power": [round(p, 1) for p in self.power],
            "weight": [1 if w else 0 for w in self.weight],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], max_minutes: int) -> MinuteHistory:
        """From storage; missing or broken data gives an empty history."""
        history = cls(max_minutes)
        try:
            start = data.get("start")
            temperature = [None if t is None else float(t) for t in data["temperature"]]
            power = [float(p) for p in data["power"]]
            weight = [bool(w) for w in data["weight"]]
        except (KeyError, TypeError, ValueError):
            return history
        if start is None or not len(temperature) == len(power) == len(weight):
            return history
        keep = min(len(power), max_minutes)
        history.start = int(start) + len(power) - keep
        history.temperature = temperature[len(power) - keep :]
        history.power = power[len(power) - keep :]
        history.weight = weight[len(power) - keep :]
        history._settle_until = history.start + len(history) + GAP_SETTLE_MINUTES
        return history
