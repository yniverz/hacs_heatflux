"""Temperature slope and COP model."""

from __future__ import annotations

import pytest

from custom_components.heatflux.physics import (
    MODE_COOL,
    MODE_DRY,
    MODE_HEAT,
    CopModel,
    CopSettings,
    Sample,
    linear_slope,
    window_stats,
)

# ---------------------------------------------------------------- regression


def test_linear_slope_exact() -> None:
    times = [0.0, 1.0, 2.0, 3.0]
    assert linear_slope(times, [1.0, 3.0, 5.0, 7.0]) == pytest.approx(2.0)
    assert linear_slope([1.0], [1.0]) is None
    assert linear_slope([1.0, 1.0], [1.0, 2.0]) is None


def test_window_stats_slope_in_kelvin_per_hour() -> None:
    samples = [Sample(t * 60, 100.0, 20 + t / 60) for t in range(31)]
    stats = window_stats(samples)
    assert stats is not None
    assert stats.slope == pytest.approx(1.0)
    assert stats.mean_power == 100.0


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
