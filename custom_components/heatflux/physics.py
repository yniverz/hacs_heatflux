"""Pure calculations: temperature slope and COP model.

Times are in seconds, temperatures in °C, powers in W (thermal, positive =
heat into the room) and slopes in K/h. The room model is in model.py.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math

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
