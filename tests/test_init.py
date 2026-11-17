"""Setup, entities and the room model learned through Home Assistant states."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.heatflux.const import (
    CONF_CLIMATE,
    CONF_COIL,
    CONF_COMPRESSOR,
    CONF_OUTDOOR_SOURCE,
    CONF_POWER,
    CONF_POWER_TYPE,
    CONF_TEMPERATURE,
    DOMAIN,
    OPEN_METEO_URL,
    OUTDOOR_NONE,
    POWER_ELECTRICAL,
    POWER_THERMAL,
)
from custom_components.heatflux.engine import Direction, ac_direction, coil_delivers

from .room_sim import SimRoom

ROOM = "sensor.room_temperature"
POWER = "sensor.ac_power"
AC = "climate.ac"
COMPRESSOR = "binary_sensor.compressor"
COIL = "sensor.coil"

SOURCES = {
    CONF_TEMPERATURE: ROOM,
    CONF_POWER: POWER,
    CONF_POWER_TYPE: POWER_ELECTRICAL,
    CONF_CLIMATE: AC,
    CONF_COMPRESSOR: COMPRESSOR,
}


def set_inputs(
    hass: HomeAssistant,
    temperature: float,
    power: float,
    action: str = "heating",
    compressor: bool = True,
) -> None:
    hass.states.async_set(ROOM, f"{temperature:.1f}", {"unit_of_measurement": "°C"})
    hass.states.async_set(POWER, str(power), {"unit_of_measurement": "W"})
    hass.states.async_set(AC, "heat", {"hvac_action": action})
    hass.states.async_set(COMPRESSOR, "on" if compressor else "off")


async def setup(
    hass: HomeAssistant, options: dict[str, Any] | None = None, **data: Any
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={**SOURCES, **data},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def open_meteo(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    aioclient_mock.get(OPEN_METEO_URL, json={"current": {"temperature_2m": 7.0}})
    return aioclient_mock


def test_ac_direction() -> None:
    assert ac_direction("heating", "heat", None, None, 20) == Direction("heat")
    assert ac_direction("cooling", "auto", True, None, 20) == Direction("cool")
    assert ac_direction("drying", "dry", None, None, 20) == Direction("dry")
    assert ac_direction("idle", "heat", True, None, 20) == Direction("off")
    assert ac_direction("defrosting", "heat", True, None, 20) == Direction(
        "heat", defrost=True
    )
    # No hvac_action: the mode, then the coil decides.
    assert ac_direction(None, "cool", None, None, 20) == Direction("cool")
    assert ac_direction(None, "auto", None, 40, 20) == Direction("heat")
    assert ac_direction(None, "auto", None, 8, 20) == Direction("cool")
    assert ac_direction(None, "auto", None, 21, 20) == Direction(None)
    # The compressor overrides; a cold coil while heating is defrost.
    assert ac_direction("heating", "heat", False, None, 20) == Direction("off")
    assert ac_direction(None, "heat", True, 5, 20) == Direction("heat", defrost=True)


def test_coil_delivers() -> None:
    assert coil_delivers("heat", None, 20)
    assert not coil_delivers("heat", 23, 20)  # fan still waits for a warm coil
    assert coil_delivers("heat", 45, 20)
    assert not coil_delivers("cool", 22, 25)
    assert coil_delivers("cool", 10, 25)
    assert coil_delivers("off", 20, 20)


async def test_entities(hass: HomeAssistant, open_meteo) -> None:
    set_inputs(hass, 21.0, 250)
    entry = await setup(hass)
    registry = er.async_get(hass)
    ids = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert ids == {
        "sensor.living_room_net_heat_gain",
        "sensor.living_room_room_temperature_estimated",
        "sensor.living_room_temperature_rate",
        "sensor.living_room_heat_capacity",
        "sensor.living_room_air_heat_capacity",
        "sensor.living_room_mass_heat_capacity",
        "sensor.living_room_air_time_constant",
        "sensor.living_room_ac_heat_delay",
        "sensor.living_room_forecast_error_1_h",
        "sensor.living_room_ac_heat_output",
        "sensor.living_room_cop",
        "sensor.living_room_outdoor_temperature",
        "sensor.living_room_model_status",
        "binary_sensor.living_room_room_model_ready",
        "button.living_room_fit_room_model_now",
        "button.living_room_reset_room_model",
    }
    # Rated heating COP 4.0 at 7 °C outdoors from Open-Meteo, 20 °C inside.
    assert float(hass.states.get("sensor.living_room_outdoor_temperature").state) == 7
    output = hass.states.get("sensor.living_room_ac_heat_output")
    assert float(output.state) == pytest.approx(250 * 3.9, rel=0.05)
    assert output.attributes["mode"] == "heat"
    # Nothing learned yet.
    assert hass.states.get("sensor.living_room_heat_capacity").state == "unknown"
    assert hass.states.get("sensor.living_room_net_heat_gain").state == "unknown"
    assert (
        hass.states.get("sensor.living_room_room_temperature_estimated").state
        == "unknown"
    )
    assert hass.states.get("binary_sensor.living_room_room_model_ready").state == "off"
    status = hass.states.get("sensor.living_room_model_status")
    assert status.state == "learning"
    assert status.attributes["history_hours"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert (
        hass.states.get("sensor.living_room_net_heat_gain").state == STATE_UNAVAILABLE
    )


async def test_compressor_off_means_no_heat(hass: HomeAssistant, open_meteo) -> None:
    set_inputs(hass, 21.0, 12, action="heating", compressor=False)
    await setup(hass)
    assert float(hass.states.get("sensor.living_room_ac_heat_output").state) == 0


async def test_thermal_signed_power_without_climate(hass: HomeAssistant) -> None:
    hass.states.async_set(ROOM, "24.0", {"unit_of_measurement": "°C"})
    hass.states.async_set(POWER, "-0.8", {"unit_of_measurement": "kW"})
    await setup(
        hass,
        options={
            CONF_TEMPERATURE: ROOM,
            CONF_POWER: POWER,
            CONF_POWER_TYPE: POWER_THERMAL,
        },
    )
    state = hass.states.get("sensor.living_room_ac_heat_output")
    assert float(state.state) == -800
    assert state.attributes["mode"] == "cool"
    assert hass.states.get("sensor.living_room_cop") is None


async def test_cold_coil_delivers_no_heat(hass: HomeAssistant, open_meteo) -> None:
    set_inputs(hass, 21.0, 600)
    hass.states.async_set(COIL, "2.0", {"unit_of_measurement": "°C"})
    await setup(hass, **{CONF_COIL: COIL})
    state = hass.states.get("sensor.living_room_ac_heat_output")
    assert float(state.state) == 0
    assert state.attributes["defrost"] is True
    assert state.attributes["coil_ready"] is False
    # Warm coil: heat counts.
    hass.states.async_set(COIL, "44.0", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.living_room_ac_heat_output")
    assert float(state.state) > 1000
    assert state.attributes["coil_ready"] is True


async def test_model_learned_through_states(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A simulated room cycling on a thermostat; the model learns it."""
    cop = 4.0  # rated, no outdoor temperature
    room = SimRoom()

    def publish() -> None:
        set_inputs(
            hass,
            room.measured(),
            room.power / cop if room.on else 5.0,
            action="heating" if room.on else "idle",
            compressor=room.on,
        )

    publish()
    entry = await setup(hass, options={**SOURCES, CONF_OUTDOOR_SOURCE: OUTDOOR_NONE})
    for _ in range(13 * 120):  # 13 hours of 30 s ticks
        room.advance(30)
        publish()
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    # The fit runs in the executor in the background.
    await hass.async_block_till_done(wait_background_tasks=True)

    status = hass.states.get("sensor.living_room_model_status")
    assert status.state == "ready", status.attributes
    assert status.attributes["last_fit_result"] == "updated"
    assert hass.states.get("binary_sensor.living_room_room_model_ready").state == "on"

    capacity = hass.states.get("sensor.living_room_heat_capacity")
    total = room.air_capacity + room.mass_capacity
    assert float(capacity.state) == pytest.approx(total, rel=0.3)
    assert capacity.attributes["air_heat_capacity"] == pytest.approx(
        room.air_capacity, rel=0.3
    )
    assert capacity.attributes["forecast_error_60min"] < 0.2

    # Keep running a little with the model: live estimates.
    for _ in range(120):
        room.advance(30)
        publish()
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    estimated = hass.states.get("sensor.living_room_room_temperature_estimated")
    assert float(estimated.state) == pytest.approx(room.mass, abs=0.3)
    assert estimated.attributes["air_temperature"] == pytest.approx(room.air, abs=0.3)
    gain = float(hass.states.get("sensor.living_room_net_heat_gain").state)
    assert gain == pytest.approx(room.gain(room.t), abs=200)

    # Stored right away, and the model survives a reload.
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.model"]["data"]
    assert stored["params"]["air_capacity"] > 0
    assert len(stored["history"]["power"]) > 13 * 60
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_model_status").state == "ready"
    assert float(
        hass.states.get("sensor.living_room_heat_capacity").state
    ) == pytest.approx(float(capacity.state), rel=1e-3)

    # Reset, then a forced fit without data keeps learning.
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.living_room_reset_room_model"},
        blocking=True,
    )
    assert hass.states.get("sensor.living_room_heat_capacity").state == "unknown"
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}.model"]["data"]["params"] is None
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.living_room_fit_room_model_now"},
        blocking=True,
    )
    status = hass.states.get("sensor.living_room_model_status")
    assert status.state == "learning"
    assert status.attributes["last_fit_result"] == "not_enough_data"
