"""Two-node room model: discretization, filter and fit on a simulated room."""

from __future__ import annotations

import math

import pytest

from custom_components.heatflux.model import (
    AIR,
    MASS,
    RoomFilter,
    RoomParams,
    Series,
    _expm,
    discretize,
    fit,
    nelder_mead,
    prediction_cost,
)

from .room_sim import SimRoom

TRUE = RoomParams(
    air_capacity=150.0, mass_capacity=1500.0, coupling=850.0, heat_delay=4.0
)


def simulate(
    hours: float, step_minutes: int = 2, **kwargs
) -> tuple[Series, list[float]]:
    """Series of the simulated room and its true mass temperature."""
    room = SimRoom(**kwargs)
    temperature: list[float | None] = []
    power: list[float] = []
    mass: list[float] = []
    for _ in range(round(hours * 60 / step_minutes)):
        # Mean AC power over the interval, temperature at its end.
        on_seconds = 0.0
        for _ in range(step_minutes * 6):
            on_seconds += 10.0 if room.on else 0.0
            room.advance(10.0)
        power.append(room.ac_power * on_seconds / (step_minutes * 60))
        temperature.append(room.measured())
        mass.append(room.mass)
    series = Series(
        dt=step_minutes / 60,
        temperature=temperature,
        power=power,
        weight=[True] * len(power),
    )
    return series, mass


@pytest.fixture(scope="module")
def two_days() -> tuple[Series, list[float]]:
    return simulate(48)


@pytest.fixture(scope="module")
def fitted(two_days) -> RoomParams:
    result = fit(two_days[0])
    assert result is not None
    return result.params


def test_expm_matches_closed_form() -> None:
    # Rotation generator: exp([[0, -a], [a, 0]]) = [[cos a, -sin a], [sin a, cos a]].
    a = 2.5
    e = _expm([[0.0, -a], [a, 0.0]])
    assert e[0][0] == pytest.approx(math.cos(a), abs=1e-10)
    assert e[1][0] == pytest.approx(math.sin(a), abs=1e-10)
    e = _expm([[-30.0, 0.0], [0.0, 0.5]])
    assert e[0][0] == pytest.approx(math.exp(-30), abs=1e-12)
    assert e[1][1] == pytest.approx(math.exp(0.5), rel=1e-12)


def test_discretize_steady_state() -> None:
    # Constant AC power P and gain Q: both nodes end at the same temperature
    # rise rate (P + Q) / (C_air + C_mass).
    d = discretize(TRUE, 1 / 60)
    x = [20.0, 20.0, -500.0, 0.0]
    for _ in range(600):
        x = [
            sum(d.f[i][j] * x[j] for j in range(4)) + d.g[i] * 2000.0 for i in range(4)
        ]
    before = x[:]
    x = [sum(d.f[i][j] * x[j] for j in range(4)) + d.g[i] * 2000.0 for i in range(4)]
    rate = (x[MASS] - before[MASS]) * 60
    assert rate == pytest.approx(1500 / TRUE.total_capacity, rel=1e-3)
    assert x[3] == pytest.approx(2000.0)


def test_nelder_mead_rosenbrock() -> None:
    def rosen(v):
        return (1 - v[0]) ** 2 + 100 * (v[1] - v[0] ** 2) ** 2

    best, value, _ = nelder_mead(
        rosen, [-1.2, 1.0], max_evaluations=2000, tolerance=1e-12
    )
    assert value < 1e-8
    assert best == pytest.approx([1.0, 1.0], abs=1e-3)


def test_params_round_trip() -> None:
    assert RoomParams.from_dict(TRUE.as_dict()) == TRUE
    assert TRUE.air_time_constant == pytest.approx(150 / 850 * 60)
    assert TRUE.total_capacity == 1650


def test_fit_recovers_the_room(fitted: RoomParams) -> None:
    assert fitted.air_capacity == pytest.approx(TRUE.air_capacity, rel=0.1)
    assert fitted.mass_capacity == pytest.approx(TRUE.mass_capacity, rel=0.1)
    assert fitted.coupling == pytest.approx(TRUE.coupling, rel=0.1)
    assert fitted.heat_delay == pytest.approx(TRUE.heat_delay, abs=0.5)
    assert fitted.air_time_constant == pytest.approx(TRUE.air_time_constant, rel=0.1)


def test_fit_forecasts_well(two_days, fitted: RoomParams) -> None:
    series, _ = two_days
    _, rms = prediction_cost(fitted, series)
    # Sensor resolution alone is ~0.03 K; persistence would be off by ~1 K.
    assert max(rms) < 0.08


def test_filter_estimates_mass_and_gain(two_days, fitted: RoomParams) -> None:
    series, mass = two_days
    room = SimRoom()
    f = RoomFilter(fitted)
    errors = []
    for i, (measured, power) in enumerate(
        zip(series.temperature, series.power, strict=True)
    ):
        f.step(series.dt, power, measured)
        if i * series.dt > 6:  # after the filter settled
            errors.append(abs(f.mass_temperature - mass[i]))
    assert sum(errors) / len(errors) < 0.05
    assert max(errors) < 0.15
    t = len(series) * series.dt * 3600
    assert f.net_gain == pytest.approx(room.gain(t), abs=150)


def test_forecast_without_ac_settles_on_mass(fitted: RoomParams) -> None:
    f = RoomFilter(fitted)
    f.x = [23.0, 20.0, 0.0, 0.0]  # just after a heating spike
    x = f.forecast(1.0)
    # The air falls back to the mass, which barely moved.
    assert x[AIR] == pytest.approx(x[MASS], abs=0.05)
    assert x[MASS] == pytest.approx(20.0 + 3.0 * 150 / 1650, abs=0.15)


def test_filter_reset_and_gaps() -> None:
    f = RoomFilter(TRUE)
    assert not f.ready
    f.step(1 / 60, 0.0, None)
    assert not f.ready
    f.step(1 / 60, 0.0, 21.0)
    assert f.air_temperature == 21.0
    assert f.mass_temperature == 21.0
    f.step(1 / 60, 0.0, None)  # missing reading: prediction only
    assert f.ready


def test_fit_needs_enough_data() -> None:
    series, _ = simulate(4)
    assert fit(series) is None
