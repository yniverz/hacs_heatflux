"""Setup, entities and a full calibration through Home Assistant states."""

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
    CONF_MIN_EVENTS,
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
from custom_components.heatflux.engine import Direction, ac_direction

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


async def test_entities(hass: HomeAssistant, open_meteo) -> None:
    set_inputs(hass, 21.0, 250)
    entry = await setup(hass)
    registry = er.async_get(hass)
    ids = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert ids == {
        "sensor.living_room_net_heat_loss",
        "sensor.living_room_temperature_rate",
        "sensor.living_room_heat_capacity",
        "sensor.living_room_ac_heat_output",
        "sensor.living_room_cop",
        "sensor.living_room_outdoor_temperature",
        "sensor.living_room_calibration_status",
        "binary_sensor.living_room_heat_capacity_calibrated",
        "button.living_room_reset_calibration",
    }
    # Rated heating COP 4.0 at 7 °C outdoors from Open-Meteo, 20 °C inside.
    assert float(hass.states.get("sensor.living_room_outdoor_temperature").state) == 7
    output = hass.states.get("sensor.living_room_ac_heat_output")
    assert float(output.state) == pytest.approx(250 * 3.9, rel=0.05)
    assert output.attributes["mode"] == "heat"
    assert hass.states.get("sensor.living_room_heat_capacity").state == "unknown"
    assert hass.states.get("sensor.living_room_net_heat_loss").state == "unknown"
    assert (
        hass.states.get("binary_sensor.living_room_heat_capacity_calibrated").state
        == "off"
    )
    assert hass.states.get("sensor.living_room_calibration_status").state == "waiting"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert (
        hass.states.get("sensor.living_room_net_heat_loss").state == STATE_UNAVAILABLE
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


async def test_defrost_from_coil(hass: HomeAssistant, open_meteo) -> None:
    set_inputs(hass, 21.0, 600)
    hass.states.async_set(COIL, "2.0", {"unit_of_measurement": "°C"})
    await setup(hass, **{CONF_COIL: COIL})
    state = hass.states.get("sensor.living_room_ac_heat_output")
    assert float(state.state) == 0
    assert state.attributes["defrost"] is True
    assert hass.states.get("sensor.living_room_calibration_status").state == "paused"


async def test_calibration_through_states(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Room with C = 200 Wh/K and a loss of 300 W; the AC starts heating."""
    capacity, loss, electrical, cop = 200.0, 300.0, 250.0, 4.0
    temperature = 21.0
    set_inputs(hass, temperature, 5, action="idle", compressor=False)
    entry = await setup(
        hass,
        options={
            **SOURCES,
            CONF_OUTDOOR_SOURCE: OUTDOOR_NONE,  # rated COP: 4.0
            CONF_MIN_EVENTS: 1,
        },
    )

    for tick in range(1, 2 * 60 + 1):  # 60 minutes, 30 s ticks
        heating = tick > 60
        thermal = electrical * cop if heating else 0.0
        temperature += (thermal - loss) / capacity * 30 / 3600
        set_inputs(
            hass,
            round(temperature, 1),
            electrical if heating else 5,
            action="heating" if heating else "idle",
            compressor=heating,
        )
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.living_room_heat_capacity")
    assert float(state.state) == pytest.approx(capacity, rel=0.15)
    assert state.attributes["valid_events"] == 1
    assert state.attributes["heating_events"] == 1
    assert (
        hass.states.get("binary_sensor.living_room_heat_capacity_calibrated").state
        == "on"
    )
    rate = float(hass.states.get("sensor.living_room_temperature_rate").state)
    assert rate == pytest.approx((electrical * cop - loss) / capacity, rel=0.15)
    # The live window still reaches back before the step; the balance holds.
    loss_state = hass.states.get("sensor.living_room_net_heat_loss")
    assert float(loss_state.state) == pytest.approx(loss, abs=80)

    # Persisted after the save delay.
    freezer.tick(timedelta(seconds=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["estimates"]
    assert len(stored) == 1

    # Survives a reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(
        hass.states.get("sensor.living_room_heat_capacity").state
    ) == pytest.approx(capacity, rel=0.15)

    # Reset.
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.living_room_reset_calibration"},
        blocking=True,
    )
    assert hass.states.get("sensor.living_room_heat_capacity").state == "unknown"
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["estimates"] == []


async def test_manual_capacity(hass: HomeAssistant) -> None:
    set_inputs(hass, 21.0, 250)
    await setup(
        hass,
        options={**SOURCES, CONF_OUTDOOR_SOURCE: OUTDOOR_NONE, "manual_capacity": 350},
    )
    state = hass.states.get("sensor.living_room_heat_capacity")
    assert float(state.state) == 350
    assert state.attributes["source"] == "manual"
    assert (
        hass.states.get("binary_sensor.living_room_heat_capacity_calibrated").state
        == "on"
    )
