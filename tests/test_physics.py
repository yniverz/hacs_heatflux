"""Step detection, heat capacity estimation and COP model on synthetic data."""

from __future__ import annotations

from collections.abc import Callable
import math
import random

import pytest

from custom_components.heatflux.physics import (
    MODE_COOL,
    MODE_DRY,
    MODE_HEAT,
    MODE_OFF,
    CalibrationSettings,
    CalibrationState,
    Calibrator,
    CapacityEstimate,
    CopModel,
    CopSettings,
    EventResult,
    Sample,
    inliers,
    linear_slope,
    net_heat_loss,
    summarize,
    window_stats,
)

DT = 30.0
MIN = 60.0


def simulate(
    power: Callable[[float], float],
    *,
    capacity: float = 200.0,
    loss: Callable[[float], float] = lambda t: 300.0,
    minutes: float = 90,
    start_temp: float = 21.0,
    resolution: float = 0.1,
    noise: float = 0.0,
    seed: int = 1,
    calibratable: Callable[[float], bool] = lambda t: True,
) -> list[Sample]:
    """Room with heat capacity (Wh/K) and a loss (W); sensor rounds to resolution."""
    rng = random.Random(seed)
    temp = start_temp
    samples = []
    t = 0.0
    while t <= minutes * MIN:
        p = power(t)
        measured = temp + rng.gauss(0, noise) if noise else temp
        if resolution:
            measured = round(measured / resolution) * resolution
        mode = MODE_HEAT if p > 0 else MODE_COOL if p < 0 else MODE_OFF
        samples.append(Sample(t, p, measured, mode, calibratable(t)))
        # K/h = W / (Wh/K); integrate over DT seconds.
        temp += (p - loss(t)) / capacity * DT / 3600
        t += DT
    return samples


def run(samples: list[Sample], settings: CalibrationSettings | None = None):
    calibrator = Calibrator(settings or CalibrationSettings())
    results: list[EventResult] = []
    states: list[CalibrationState] = []
    for sample in samples:
        if (result := calibrator.add(sample)) is not None:
            results.append(result)
        states.append(calibrator.state)
    return calibrator, results, states


def step(at_minutes: float, before: float, after: float, ramp_minutes: float = 0):
    def power(t: float) -> float:
        start = at_minutes * MIN
        if t < start:
            return before
        if ramp_minutes and t < start + ramp_minutes * MIN:
            return before + (after - before) * (t - start) / (ramp_minutes * MIN)
        return after

    return power


def accepted(results: list[EventResult]) -> list[CapacityEstimate]:
    return [r.estimate for r in results if r.accepted and r.estimate]


# ---------------------------------------------------------------- regression


def test_linear_slope_exact() -> None:
    times = [0.0, 1.0, 2.0, 3.0]
    assert linear_slope(times, [1.0, 3.0, 5.0, 7.0]) == pytest.approx(2.0)
    assert linear_slope([1.0], [1.0]) is None
    assert linear_slope([1.0, 1.0], [1.0, 2.0]) is None


def test_window_stats_slope_in_kelvin_per_hour() -> None:
    samples = [Sample(t * 60, 100.0, 20 + t / 60, MODE_HEAT) for t in range(31)]
    stats = window_stats(samples)
    assert stats is not None
    assert stats.slope == pytest.approx(1.0)
    assert stats.mean_power == 100.0
    assert net_heat_loss(stats, 150.0) == pytest.approx(-50.0)


# --------------------------------------------------------------- calibration


def test_heating_step_from_off() -> None:
    samples = simulate(step(30, 0, 1000), resolution=0)
    _, results, _ = run(samples)
    estimates = accepted(results)
    assert len(estimates) == 1
    assert estimates[0].capacity == pytest.approx(200, rel=0.02)
    assert estimates[0].mode == MODE_HEAT
    assert estimates[0].slope_before == pytest.approx(-1.5, abs=0.01)
    assert estimates[0].slope_after == pytest.approx(3.5, abs=0.01)


def test_heating_step_with_sensor_resolution_and_noise() -> None:
    errors = []
    for seed in range(10):
        samples = simulate(step(30, 0, 1000), noise=0.03, seed=seed)
        _, results, _ = run(samples)
        estimates = accepted(results)
        assert len(estimates) == 1
        errors.append(estimates[0].capacity / 200 - 1)
    assert max(abs(e) for e in errors) < 0.25
    assert abs(sum(errors) / len(errors)) < 0.1


def test_step_with_inverter_ramp() -> None:
    samples = simulate(step(30, 0, 1000, ramp_minutes=3), resolution=0)
    _, results, _ = run(samples)
    estimates = accepted(results)
    assert len(estimates) == 1
    assert estimates[0].capacity == pytest.approx(200, rel=0.03)


def test_step_down_while_heating() -> None:
    samples = simulate(step(30, 1200, 400), loss=lambda t: 600.0, resolution=0)
    _, results, _ = run(samples)
    (estimate,) = accepted(results)
    assert estimate.capacity == pytest.approx(200, rel=0.02)
    assert estimate.mode == MODE_HEAT


def test_cooling_step() -> None:
    samples = simulate(
        step(30, 0, -900), loss=lambda t: -250.0, start_temp=26, resolution=0
    )
    _, results, _ = run(samples)
    (estimate,) = accepted(results)
    assert estimate.capacity == pytest.approx(200, rel=0.02)
    assert estimate.mode == MODE_COOL


def test_slowly_changing_loss_is_tolerated() -> None:
    samples = simulate(
        step(30, 0, 1000),
        loss=lambda t: 300 + 100 * math.sin(t / (6 * 3600) * 2 * math.pi),
        resolution=0,
    )
    _, results, _ = run(samples)
    (estimate,) = accepted(results)
    assert estimate.capacity == pytest.approx(200, rel=0.1)


def test_states_follow_the_event() -> None:
    samples = simulate(step(30, 0, 1000), resolution=0, minutes=60)
    _, _, states = run(samples)
    by_minute = {
        int(s.t // MIN): state for s, state in zip(samples, states, strict=True)
    }
    assert by_minute[10] == CalibrationState.WAITING
    assert by_minute[32] == CalibrationState.SETTLING
    assert by_minute[40] == CalibrationState.MEASURING
    assert by_minute[55] == CalibrationState.WAITING


def test_small_step_is_not_counted() -> None:
    samples = simulate(step(30, 500, 650), resolution=0)
    _, results, _ = run(samples)
    assert not accepted(results)
    assert {r.reason for r in results} == {"step_too_small"}


def test_unstable_power_after_step_aborts() -> None:
    def power(t: float) -> float:
        if t < 30 * MIN:
            return 0.0
        return 1000.0 if int(t // (3 * MIN)) % 2 else 600.0

    _, results, _ = run(simulate(power, resolution=0))
    assert not accepted(results)
    assert results[0].reason == "power_unstable"


def test_no_event_without_stable_power_before() -> None:
    def power(t: float) -> float:
        if t < 30 * MIN:
            return 400.0 if int(t // (5 * MIN)) % 2 else 0.0
        return 1500.0

    samples = [s for s in simulate(power, resolution=0) if s.t <= 45 * MIN]
    _, results, states = run(samples)
    # The first stable window only starts after the flapping stopped.
    assert not results
    assert CalibrationState.SETTLING not in states[: int(30 * MIN / DT) + 1]


def test_pause_interrupts_event() -> None:
    samples = simulate(
        step(30, 0, 1000),
        resolution=0,
        calibratable=lambda t: not 38 * MIN <= t < 40 * MIN,
    )
    _, results, _ = run(samples)
    assert not accepted(results)
    assert results[0].reason == "interrupted"


def test_paused_state_while_waiting() -> None:
    samples = simulate(lambda t: 0.0, calibratable=lambda t: t < 10 * MIN, minutes=20)
    calibrator, _, _ = run(samples)
    assert calibrator.state == CalibrationState.PAUSED


def test_data_gap_aborts() -> None:
    samples = [
        s
        for s in simulate(step(30, 0, 1000), resolution=0)
        if not 36 * MIN <= s.t < 46 * MIN
    ]
    _, results, _ = run(samples)
    assert not accepted(results)
    assert results[0].reason == "missing_data"


def test_implausible_capacity_is_rejected() -> None:
    settings = CalibrationSettings(max_capacity=100)
    _, results, _ = run(simulate(step(30, 0, 1000), resolution=0), settings)
    assert not accepted(results)
    assert results[0].reason == "implausible"
    assert results[0].capacity == pytest.approx(200, rel=0.02)


def test_two_steps_give_two_estimates() -> None:
    def power(t: float) -> float:
        return 1000.0 if 30 * MIN <= t < 80 * MIN else 0.0

    _, results, _ = run(simulate(power, resolution=0, minutes=130))
    estimates = accepted(results)
    assert len(estimates) == 2
    for estimate in estimates:
        assert estimate.capacity == pytest.approx(200, rel=0.03)


def test_estimate_round_trip() -> None:
    estimate = CapacityEstimate(1.0, 200.0, MODE_HEAT, 0.0, 1000.0, -1.5, 3.5)
    assert CapacityEstimate.from_dict(estimate.as_dict()) == estimate


# ----------------------------------------------------------------- summary


def _estimate(capacity: float, mode: str = MODE_HEAT) -> CapacityEstimate:
    return CapacityEstimate(0.0, capacity, mode, 0.0, 1000.0, 0.0, 5.0)


def test_inliers_drop_outliers() -> None:
    assert inliers([200, 210, 190, 205, 1000]) == [200, 210, 190, 205]
    assert inliers([200, 1000]) == [200, 1000]


def test_summary_median_and_spread() -> None:
    summary = summarize([_estimate(v) for v in (200, 210, 190, 205, 195, 900)])
    assert summary.capacity == 200
    assert summary.count == 6
    assert summary.inliers == 5
    assert summary.std_dev == pytest.approx(7.9, abs=0.1)


def test_summary_per_mode_and_filter() -> None:
    estimates = [
        _estimate(200),
        _estimate(220),
        _estimate(150, MODE_COOL),
        _estimate(160, MODE_DRY),
    ]
    summary = summarize(estimates, (MODE_HEAT,))
    assert summary.capacity == 210
    assert summary.per_mode == {MODE_HEAT: 210, MODE_COOL: 155}
    assert summary.per_mode_count == {MODE_HEAT: 2, MODE_COOL: 2}
    assert summarize(estimates).capacity == 180
    empty = summarize([])
    assert empty.capacity is None
    assert empty.inliers == 0


# --------------------------------------------------------------------- COP


def test_cop_matches_rated_values_at_reference() -> None:
    model = CopModel(CopSettings(heat_rated=4.1, cool_rated=7.0))
    assert model.cop(MODE_HEAT, 20, 7).value == pytest.approx(4.1)
    assert model.cop(MODE_COOL, 27, 25).value == pytest.approx(7.0)


def test_cop_drops_with_lift() -> None:
    model = CopModel(CopSettings(heat_rated=4.1, cool_rated=7.0))
    assert model.cop(MODE_HEAT, 20, -5).value < model.cop(MODE_HEAT, 20, 7).value
    assert 2.5 < model.cop(MODE_HEAT, 20, -5).value < 3.5
    assert model.cop(MODE_COOL, 27, 35).value < 7.0
    assert model.cop(MODE_DRY, 27, 35).value == model.cop(MODE_COOL, 27, 35).value


def test_cop_uses_the_coil_temperature() -> None:
    model = CopModel(CopSettings(heat_rated=4.1))
    part_load = model.cop(MODE_HEAT, 20, 7, coil=30)
    full_load = model.cop(MODE_HEAT, 20, 7, coil=48)
    assert part_load.condensing == 30
    assert part_load.value > 4.1 > full_load.value
    # A coil colder than the room while heating is ignored (defrost, idle).
    assert model.cop(MODE_HEAT, 20, 7, coil=10).value == pytest.approx(4.1)
    cooling = model.cop(MODE_COOL, 27, 25, coil=8)
    assert cooling.evaporating == 8


def test_cop_without_outdoor_is_rated_and_clamped() -> None:
    model = CopModel(CopSettings(heat_rated=4.0, cool_rated=6.0))
    assert model.cop(MODE_HEAT, 20, None).value == 4.0
    assert model.cop(MODE_HEAT, 20, None).source == "rated"
    assert model.cop(MODE_COOL, 27, None).value == 6.0
    assert model.cop(MODE_HEAT, 20, 19).value <= 10.0
    assert model.cop(MODE_HEAT, 20, -40).value >= 1.0
