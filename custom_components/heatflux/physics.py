"""Pure calculations: temperature slope, COP model and heat capacity calibration.

Nothing in here depends on Home Assistant, so it can be tested with synthetic
data. Times are in seconds, temperatures in °C, powers in W (thermal, positive
= heat into the room), slopes in K/h and heat capacities in Wh/K.

Energy balance of the room air (plus whatever follows it quickly):

    C × dT/dt = P_ac + Q_gain   →   Q_gain = C × dT/dt − P_ac

Q_gain is the net heat flow into the room from everything but the AC (walls,
windows, sun, people; negative: the room loses heat). Across a sudden step of
the AC power, Q_gain stays about the same, so (for constant power on both
sides) C = ΔP_ac / Δ(dT/dt). `fit_balance` generalizes this to modulating
power.
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


def net_heat_gain(stats: WindowStats, capacity: float) -> float:
    """Net heat flow into the room in W, without the AC (negative: it loses heat)."""
    return capacity * stats.slope - stats.mean_power


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

    before_minutes: float = 20.0
    min_step: float = 300.0  # W
    settle_minutes: float = 4.0
    fit_minutes: float = 15.0
    max_uncertainty: float = 25.0  # % (standard error of the heat capacity)
    min_coverage: float = 0.8
    max_gap_minutes: float = 3.0
    min_capacity: float = 5.0
    max_capacity: float = 20000.0


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
    uncertainty: float = 0.0  # % (standard error)

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
            "uncertainty": self.uncertainty,
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
            uncertainty=float(data.get("uncertainty", 0.0)),
        )


@dataclass(frozen=True)
class EventResult:
    """Outcome of a finished (or aborted) step event."""

    t: float
    accepted: bool
    reason: str
    estimate: CapacityEstimate | None = None
    capacity: float | None = None  # also set when rejected for its value
    power_step: float | None = None
    uncertainty: float | None = None


@dataclass(frozen=True)
class BalanceFit:
    """Energy balance fitted across a step."""

    capacity: float
    gain: float  # W, net heat flow into the room without the AC
    uncertainty: float  # % (standard error of the heat capacity)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Solve a small linear system (Gauss-Jordan with partial pivoting)."""
    n = len(vector)
    rows = [[*matrix[i], vector[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(n):
            if r != col:
                factor = rows[r][col] / rows[col][col]
                rows[r] = [
                    a - factor * b for a, b in zip(rows[r], rows[col], strict=True)
                ]
    return [rows[i][n] / rows[i][i] for i in range(n)]


def fit_balance(
    samples: list[Sample], before_end: float, after_start: float
) -> BalanceFit | None:
    """Fit C × dT/dt = P_ac + Q_gain over the samples around a step.

    With E(t) the heat the AC put in since the first sample (Wh) and h the
    hours since then, the room temperature follows

        T = a + (Q_gain / C) × h + E / C

    The samples before `before_end` and from `after_start` on get their own
    offset `a`: a lagging sensor or the air flow change after the step then
    can't bias the result, and the settling samples in between only count
    for E. For constant power on each side this is the same as
    C = ΔP / Δ(dT/dt), but the power may change within each side as well.
    """
    if len(samples) < 8:
        return None
    t0 = samples[0].t
    energy = 0.0
    rows: list[list[float]] = []
    ys: list[float] = []
    previous = samples[0]
    for sample in samples:
        energy += (previous.power + sample.power) / 2 * (sample.t - previous.t) / 3600
        previous = sample
        if before_end <= sample.t < after_start:
            continue
        before = 1.0 if sample.t < before_end else 0.0
        rows.append([before, 1.0 - before, (sample.t - t0) / 3600, energy])
        ys.append(sample.temperature)
    n, p = len(rows), 4
    if n <= p + 2:
        return None
    xtx = [[math.fsum(r[i] * r[j] for r in rows) for j in range(p)] for i in range(p)]
    xty = [math.fsum(r[i] * y for r, y in zip(rows, ys, strict=True)) for i in range(p)]
    coef = _solve(xtx, xty)
    if coef is None:
        return None
    k = coef[3]
    # Variance of k: residual variance × (XᵀX)⁻¹[3][3].
    unit = _solve(xtx, [0.0, 0.0, 0.0, 1.0])
    if unit is None or abs(k) < 1e-12:
        return None
    residuals = [
        y - math.fsum(c * x for c, x in zip(coef, r, strict=True))
        for r, y in zip(rows, ys, strict=True)
    ]
    variance = math.fsum(e * e for e in residuals) / (n - p)
    # Quantized sensors can fit perfectly; keep a floor of a rounding error.
    variance = max(variance, 0.01**2 / 12)
    se_k = math.sqrt(max(variance * unit[3], 0.0))
    capacity = 1 / k
    return BalanceFit(
        capacity=capacity,
        gain=coef[2] * capacity,
        uncertainty=abs(se_k / k) * 100,
    )


@dataclass
class _PendingEvent:
    t_step: float
    before: WindowStats


def _event_mode(delta_power: float, samples: list[Sample]) -> str:
    """Mode the AC was working in during the event."""
    modes = [s.mode for s in samples if s.mode != MODE_OFF]
    if not modes:
        return MODE_HEAT if delta_power > 0 else MODE_COOL
    return max(set(modes), key=modes.count)


class Calibrator:
    """Detects power steps and measures the heat capacity across each.

    1. Waiting: a sample at least `min_step` away from the mean power of the
       last `before_minutes` starts an event. That window must not reach back
       into the settling time of the previous event.
    2. Settling: the first `settle_minutes` after the step are skipped (air
       flow settles, inverters ramp up).
    3. Measuring: after `fit_minutes` more, the energy balance is fitted over
       the window before and the window after (see `fit_balance`). The power
       may keep modulating; the result has to be precise enough instead.
    """

    def __init__(self, settings: CalibrationSettings) -> None:
        """Initialize."""
        self.settings = settings
        self._samples: deque[Sample] = deque()
        self._pending: _PendingEvent | None = None
        # The window before a step must not reach back into the settling of
        # the previous one (a lagging sensor is still catching up there).
        self._quiet_from = -math.inf
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
        self._quiet_from = -math.inf
        self.state = CalibrationState.WAITING

    def _history_seconds(self) -> float:
        s = self.settings
        return (s.before_minutes + s.settle_minutes + s.fit_minutes) * 60 + 300

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

        before_seconds = s.before_minutes * 60
        pre = self._window(max(sample.t - before_seconds, self._quiet_from), sample.t)
        if not self._covered(pre, before_seconds) or not all(
            x.calibratable for x in pre
        ):
            return None
        stats = window_stats(pre)
        if stats is None or abs(sample.power - stats.mean_power) < s.min_step:
            return None
        self._pending = _PendingEvent(t_step=sample.t, before=stats)
        self._quiet_from = sample.t + s.settle_minutes * 60
        self.state = CalibrationState.SETTLING
        return None

    def _finish(self, result: EventResult) -> EventResult:
        self._pending = None
        self.state = CalibrationState.WAITING
        self.last_result = result
        return result

    def _abort(self, t: float, reason: str, **details: float) -> EventResult:
        return self._finish(EventResult(t=t, accepted=False, reason=reason, **details))

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

        before = pending.before
        post = self._window(settle_end, sample.t + 0.001)
        event = self._window(before.start, sample.t + 0.001)
        if not all(x.calibratable for x in event):
            return self._abort(sample.t, "interrupted")
        if not self._covered(post, s.fit_minutes * 60):
            return self._abort(sample.t, "missing_data")
        after = window_stats(post)
        if after is None:
            return self._abort(sample.t, "missing_data")
        delta_power = after.mean_power - before.mean_power
        if abs(delta_power) < s.min_step:
            return self._abort(sample.t, "step_too_small", power_step=delta_power)
        fit = fit_balance(event, pending.t_step, settle_end)
        if fit is None:
            return self._abort(sample.t, "missing_data", power_step=delta_power)
        details = {
            "capacity": fit.capacity,
            "power_step": delta_power,
            "uncertainty": fit.uncertainty,
        }
        if fit.capacity <= 0:
            return self._abort(sample.t, "wrong_direction", **details)
        if fit.uncertainty > s.max_uncertainty:
            return self._abort(sample.t, "too_uncertain", **details)
        if not s.min_capacity <= fit.capacity <= s.max_capacity:
            return self._abort(sample.t, "implausible", **details)
        estimate = CapacityEstimate(
            t=sample.t,
            capacity=fit.capacity,
            mode=_event_mode(delta_power, event),
            power_before=before.mean_power,
            power_after=after.mean_power,
            slope_before=before.slope,
            slope_after=after.slope,
            uncertainty=fit.uncertainty,
        )
        return self._finish(
            EventResult(
                t=sample.t, accepted=True, reason="ok", estimate=estimate, **details
            )
        )


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
