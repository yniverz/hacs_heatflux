"""Pure calculations: temperature slope, COP model and heat capacity calibration.

Nothing in here depends on Home Assistant, so it can be tested with synthetic
data. Times are in seconds, temperatures in °C, powers in W (thermal, positive
= heat into the room), slopes in K/h and heat capacities in Wh/K.

Energy balance of the room air (plus whatever follows it quickly):

    C × dT/dt = P_ac − Q_loss   →   Q_loss = P_ac − C × dT/dt

Across a sudden step of the AC power, Q_loss stays about the same, so

    C = ΔP_ac / Δ(dT/dt)
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
import math
from statistics import median, stdev

KELVIN = 273.15
SECONDS_PER_HOUR = 3600.0

MODE_HEAT = "heat"
MODE_COOL = "cool"
MODE_DRY = "dry"
MODE_OFF = "off"


@dataclass(frozen=True)
class Sample:
    """One reading of the room: thermal AC power and room temperature."""

    t: float
    power: float
    temperature: float
    mode: str = MODE_OFF
    # False while the AC defrosts, calibration is paused or the direction of
    # the AC power is unknown. Such samples never enter a calibration event.
    calibratable: bool = True


@dataclass(frozen=True)
class WindowStats:
    """Mean power and temperature slope over a time window."""

    count: int
    start: float
    end: float
    mean_power: float
    min_power: float
    max_power: float
    slope: float  # K/h

    @property
    def span(self) -> float:
        """Seconds between the first and the last sample."""
        return self.end - self.start


def linear_slope(times: list[float], values: list[float]) -> float | None:
    """Least-squares slope of values over times, per second."""
    n = len(times)
    if n < 2:
        return None
    mean_t = math.fsum(times) / n
    mean_v = math.fsum(values) / n
    sxx = math.fsum((t - mean_t) ** 2 for t in times)
    if sxx <= 0:
        return None
    sxy = math.fsum(
        (t - mean_t) * (v - mean_v) for t, v in zip(times, values, strict=True)
    )
    return sxy / sxx


def window_stats(samples: Iterable[Sample]) -> WindowStats | None:
    """Stats of the given samples, or None if there are fewer than three."""
    items = list(samples)
    if len(items) < 3:
        return None
    times = [s.t for s in items]
    slope = linear_slope(times, [s.temperature for s in items])
    if slope is None:
        return None
    powers = [s.power for s in items]
    return WindowStats(
        count=len(items),
        start=times[0],
        end=times[-1],
        mean_power=math.fsum(powers) / len(powers),
        min_power=min(powers),
        max_power=max(powers),
        slope=slope * SECONDS_PER_HOUR,
    )


def net_heat_loss(stats: WindowStats, capacity: float) -> float:
    """Net heat flow out of the room in W (negative: the room gains heat)."""
    return stats.mean_power - capacity * stats.slope


# --------------------------------------------------------------------------- COP


@dataclass(frozen=True)
class CopSettings:
    """Rated efficiencies and the conditions they were measured at.

    The defaults are the seasonal values of the EU energy label (SCOP, SEER)
    at the outdoor temperatures they roughly correspond to.
    """

    heat_rated: float = 4.0
    heat_ref_outdoor: float = 7.0
    cool_rated: float = 6.1
    cool_ref_outdoor: float = 25.0
    heat_ref_indoor: float = 20.0
    cool_ref_indoor: float = 27.0
    # Refrigerant temperatures relative to the air around the coils (K).
    condenser_approach: float = 15.0
    evaporator_approach_outdoor: float = 6.0
    evaporator_approach_indoor: float = 12.0
    condenser_approach_outdoor: float = 12.0
    min_cop: float = 1.0
    max_cop: float = 10.0
    min_lift: float = 8.0


@dataclass(frozen=True)
class CopResult:
    """COP for the current conditions and how it was found."""

    value: float
    source: str  # "model", "rated"
    condensing: float | None = None
    evaporating: float | None = None


def _carnot(hot: float, cold: float, heating: bool, min_lift: float) -> float:
    lift = max(hot - cold, min_lift)
    return (hot + KELVIN) / lift if heating else (cold + KELVIN) / lift


class CopModel:
    """COP of an air-to-air heat pump as a fixed share of the Carnot limit.

    The share (η) is fixed by the rated value at its reference conditions; the
    Carnot limit then follows the indoor and outdoor temperatures. With the
    indoor coil temperature the model uses the real condensing (heating) or
    evaporating (cooling) temperature, which also covers part load.
    """

    def __init__(self, settings: CopSettings) -> None:
        """Calibrate η for heating and cooling."""
        self.settings = s = settings
        hot, cold = self._refrigerant(
            MODE_HEAT, s.heat_ref_indoor, s.heat_ref_outdoor, None
        )
        self.eta_heat = s.heat_rated / _carnot(hot, cold, True, s.min_lift)
        hot, cold = self._refrigerant(
            MODE_COOL, s.cool_ref_indoor, s.cool_ref_outdoor, None
        )
        self.eta_cool = s.cool_rated / _carnot(hot, cold, False, s.min_lift)

    def _refrigerant(
        self, mode: str, indoor: float, outdoor: float, coil: float | None
    ) -> tuple[float, float]:
        """Condensing and evaporating temperature in °C."""
        s = self.settings
        if mode == MODE_HEAT:
            hot = (
                coil
                if coil is not None and coil > indoor
                else indoor + s.condenser_approach
            )
            return hot, outdoor - s.evaporator_approach_outdoor
        cold = (
            coil
            if coil is not None and coil < indoor
            else indoor - s.evaporator_approach_indoor
        )
        return outdoor + s.condenser_approach_outdoor, cold

    def rated(self, mode: str) -> float:
        """Rated COP (heating) or EER (cooling, dry)."""
        return (
            self.settings.heat_rated if mode == MODE_HEAT else self.settings.cool_rated
        )

    def cop(
        self,
        mode: str,
        indoor: float | None,
        outdoor: float | None,
        coil: float | None = None,
    ) -> CopResult:
        """COP for the mode and conditions; the rated value if any are missing."""
        s = self.settings
        if indoor is None or outdoor is None:
            return CopResult(self.rated(mode), "rated")
        heating = mode == MODE_HEAT
        hot, cold = self._refrigerant(mode, indoor, outdoor, coil)
        eta = self.eta_heat if heating else self.eta_cool
        value = eta * _carnot(hot, cold, heating, s.min_lift)
        value = min(max(value, s.min_cop), s.max_cop)
        return CopResult(value, "model", condensing=hot, evaporating=cold)


# ------------------------------------------------------------------- calibration


@dataclass(frozen=True)
class CalibrationSettings:
    """Thresholds of the step detection."""

    stable_minutes: float = 20.0
    stability_percent: float = 10.0
    stability_floor: float = 60.0  # W
    min_step: float = 300.0  # W
    settle_minutes: float = 4.0
    fit_minutes: float = 15.0
    min_slope_change: float = 0.2  # K/h
    min_coverage: float = 0.8
    max_gap_minutes: float = 3.0
    min_capacity: float = 5.0
    max_capacity: float = 20000.0

    def tolerance(self, power: float) -> float:
        """Allowed deviation from a mean power that still counts as stable."""
        return max(abs(power) * self.stability_percent / 100.0, self.stability_floor)


class CalibrationState(StrEnum):
    """What the step detection is doing."""

    WAITING = "waiting"
    SETTLING = "settling"
    MEASURING = "measuring"
    PAUSED = "paused"


@dataclass(frozen=True)
class CapacityEstimate:
    """One heat capacity measured across a power step."""

    t: float
    capacity: float
    mode: str
    power_before: float
    power_after: float
    slope_before: float
    slope_after: float

    def as_dict(self) -> dict[str, float | str]:
        """For storage."""
        return {
            "t": self.t,
            "capacity": self.capacity,
            "mode": self.mode,
            "power_before": self.power_before,
            "power_after": self.power_after,
            "slope_before": self.slope_before,
            "slope_after": self.slope_after,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CapacityEstimate:
        """From storage."""
        return cls(
            t=float(data["t"]),
            capacity=float(data["capacity"]),
            mode=str(data["mode"]),
            power_before=float(data["power_before"]),
            power_after=float(data["power_after"]),
            slope_before=float(data["slope_before"]),
            slope_after=float(data["slope_after"]),
        )


@dataclass(frozen=True)
class EventResult:
    """Outcome of a finished (or aborted) step event."""

    t: float
    accepted: bool
    reason: str
    estimate: CapacityEstimate | None = None
    capacity: float | None = None  # also set when rejected for its value


@dataclass
class _PendingEvent:
    t_step: float
    before: WindowStats


def _event_mode(before: WindowStats, after: WindowStats, samples: list[Sample]) -> str:
    """Mode of the side with more power (the side the AC was working)."""
    modes = [s.mode for s in samples if s.mode != MODE_OFF]
    if not modes:
        return MODE_HEAT if after.mean_power - before.mean_power > 0 else MODE_COOL
    return max(set(modes), key=modes.count)


class Calibrator:
    """Detects power steps and measures the heat capacity across each.

    1. Waiting: the power was stable for `stable_minutes`; a sample that
       leaves that band starts an event. The slope before comes from that
       stable window.
    2. Settling: the first `settle_minutes` after the step are skipped (air
       flow settles, inverters ramp up).
    3. Measuring: the next `fit_minutes` give the slope after. The power has
       to be stable in there and at least `min_step` away from before.
    """

    def __init__(self, settings: CalibrationSettings) -> None:
        """Initialize."""
        self.settings = settings
        self._samples: deque[Sample] = deque()
        self._pending: _PendingEvent | None = None
        self.state = CalibrationState.WAITING
        self.last_result: EventResult | None = None

    @property
    def event_start(self) -> float | None:
        """Time of the step of the running event."""
        return self._pending.t_step if self._pending else None

    def reset(self) -> None:
        """Forget the running event and the history."""
        self._samples.clear()
        self._pending = None
        self.state = CalibrationState.WAITING

    def _history_seconds(self) -> float:
        s = self.settings
        return (s.stable_minutes + s.settle_minutes + s.fit_minutes) * 60 + 300

    def _window(self, start: float, end: float) -> list[Sample]:
        return [x for x in self._samples if start <= x.t < end]

    def _covered(self, samples: list[Sample], seconds: float) -> bool:
        if len(samples) < 5:
            return False
        max_gap = self.settings.max_gap_minutes * 60
        if any(b.t - a.t > max_gap for a, b in pairwise(samples)):
            return False
        return samples[-1].t - samples[0].t >= seconds * self.settings.min_coverage

    def add(self, sample: Sample) -> EventResult | None:
        """Add a sample; returns the result when an event ends."""
        s = self.settings
        if self._samples and sample.t <= self._samples[-1].t:
            return None
        self._samples.append(sample)
        while self._samples and self._samples[0].t < sample.t - self._history_seconds():
            self._samples.popleft()

        if self._pending is not None:
            return self._advance(sample)

        if not sample.calibratable:
            self.state = CalibrationState.PAUSED
            return None
        self.state = CalibrationState.WAITING

        stable_seconds = s.stable_minutes * 60
        pre = self._window(sample.t - stable_seconds, sample.t)
        if not self._covered(pre, stable_seconds) or not all(
            x.calibratable for x in pre
        ):
            return None
        stats = window_stats(pre)
        if stats is None:
            return None
        tolerance = s.tolerance(stats.mean_power)
        if (
            stats.max_power - stats.mean_power > tolerance
            or stats.mean_power - stats.min_power > tolerance
        ):
            return None
        if abs(sample.power - stats.mean_power) <= tolerance:
            return None
        self._pending = _PendingEvent(t_step=sample.t, before=stats)
        self.state = CalibrationState.SETTLING
        return None

    def _abort(self, t: float, reason: str) -> EventResult:
        self._pending = None
        self.state = CalibrationState.WAITING
        self.last_result = EventResult(t=t, accepted=False, reason=reason)
        return self.last_result

    def _advance(self, sample: Sample) -> EventResult | None:
        s = self.settings
        pending = self._pending
        assert pending is not None
        if not sample.calibratable:
            return self._abort(sample.t, "interrupted")

        settle_end = pending.t_step + s.settle_minutes * 60
        fit_end = settle_end + s.fit_minutes * 60
        if sample.t < settle_end:
            self.state = CalibrationState.SETTLING
            return None
        self.state = CalibrationState.MEASURING
        if sample.t < fit_end:
            return None

        post = self._window(settle_end, sample.t + 0.001)
        event_samples = self._window(pending.t_step, sample.t + 0.001)
        if not all(x.calibratable for x in event_samples):
            return self._abort(sample.t, "interrupted")
        if not self._covered(post, s.fit_minutes * 60):
            return self._abort(sample.t, "missing_data")
        after = window_stats(post)
        if after is None:
            return self._abort(sample.t, "missing_data")
        before = pending.before
        delta_power = after.mean_power - before.mean_power
        if abs(delta_power) < s.min_step:
            return self._abort(sample.t, "step_too_small")
        tolerance = s.tolerance(after.mean_power)
        if (
            after.max_power - after.mean_power > tolerance
            or after.mean_power - after.min_power > tolerance
        ):
            return self._abort(sample.t, "power_unstable")
        delta_slope = after.slope - before.slope
        if abs(delta_slope) < s.min_slope_change:
            return self._abort(sample.t, "slope_change_too_small")
        capacity = delta_power / delta_slope
        self._pending = None
        self.state = CalibrationState.WAITING
        if not s.min_capacity <= capacity <= s.max_capacity:
            self.last_result = EventResult(
                t=sample.t,
                accepted=False,
                reason="implausible" if capacity > 0 else "wrong_direction",
                capacity=capacity,
            )
            return self.last_result
        estimate = CapacityEstimate(
            t=sample.t,
            capacity=capacity,
            mode=_event_mode(before, after, event_samples),
            power_before=before.mean_power,
            power_after=after.mean_power,
            slope_before=before.slope,
            slope_after=after.slope,
        )
        self.last_result = EventResult(
            t=sample.t, accepted=True, reason="ok", estimate=estimate, capacity=capacity
        )
        return self.last_result


# ------------------------------------------------------------------ estimates


OUTLIER_Z = 3.5


@dataclass(frozen=True)
class CapacitySummary:
    """Robust summary of the stored estimates."""

    capacity: float | None
    count: int
    inliers: int
    std_dev: float | None
    mad: float | None
    per_mode: dict[str, float] = field(default_factory=dict)
    per_mode_count: dict[str, int] = field(default_factory=dict)


def inliers(values: list[float]) -> list[float]:
    """Values without outliers (robust z-score on the median absolute deviation)."""
    if len(values) < 4:
        return list(values)
    center = median(values)
    mad = median(abs(v - center) for v in values)
    scale = max(1.4826 * mad, 0.05 * abs(center), 1e-9)
    return [v for v in values if abs(v - center) / scale <= OUTLIER_Z]


def summarize(
    estimates: Iterable[CapacityEstimate], modes: Iterable[str] | None = None
) -> CapacitySummary:
    """Median heat capacity of the estimates of the given modes (dry = cool)."""
    items = list(estimates)
    wanted = set(modes) if modes is not None else None

    def group(mode: str) -> str:
        return MODE_COOL if mode == MODE_DRY else mode

    per_mode: dict[str, float] = {}
    per_mode_count: dict[str, int] = {}
    for mode in (MODE_HEAT, MODE_COOL):
        values = inliers([e.capacity for e in items if group(e.mode) == mode])
        if values:
            per_mode[mode] = median(values)
            per_mode_count[mode] = len(values)

    values = [e.capacity for e in items if wanted is None or group(e.mode) in wanted]
    kept = inliers(values)
    if not kept:
        return CapacitySummary(
            None, len(values), 0, None, None, per_mode, per_mode_count
        )
    center = median(kept)
    std_dev = stdev(kept) if len(kept) > 1 else None
    mad = median(abs(v - center) for v in kept)
    return CapacitySummary(
        center, len(values), len(kept), std_dev, mad, per_mode, per_mode_count
    )
