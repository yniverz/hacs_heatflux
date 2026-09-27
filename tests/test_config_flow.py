"""Config and options flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heatflux.const import DOMAIN, merged_config


@pytest.fixture(autouse=True)
def no_setup():
    """Creating an entry must not start the engine."""
    with patch("custom_components.heatflux.async_setup_entry", return_value=True):
        yield


SOURCES = {
    "temperature_sensor": "sensor.room",
    "power_sensor": "sensor.power",
    "power_type": "electrical",
    "climate_entity": "climate.ac",
}


async def test_user_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Living room",
            **{k: v for k, v in SOURCES.items() if k != "climate_entity"},
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "climate_required"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Living room", **SOURCES}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room"
    assert result["data"] == SOURCES


async def test_thermal_power_needs_no_climate(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    sources = {k: v for k, v in SOURCES.items() if k != "climate_entity"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Office", **sources, "power_type": "thermal"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={**SOURCES, "compressor_sensor": "binary_sensor.compressor"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**SOURCES, "outdoor_source": "entity"}
    )
    assert result["errors"] == {"base": "outdoor_entity_required"}

    # The compressor is cleared, an outdoor sensor and a pause entity added.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **SOURCES,
            "outdoor_source": "entity",
            "outdoor_entity": "sensor.outside",
            "pause_entities": ["binary_sensor.window"],
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "efficiency": {
                "cop_mode": "model",
                "heat_cop": 4.1,
                "heat_ref_outdoor": 7,
                "cool_eer": 7.0,
                "cool_ref_outdoor": 25,
            },
            "calibration": {
                "calibration_modes": "heat",
                "min_events": 3.0,
                "stable_minutes": 20,
                "stability_percent": 10,
                "stability_floor": 60,
                "min_step": 300,
                "settle_minutes": 4,
                "fit_minutes": 15,
                "min_slope_change": 0.2,
            },
            "live": {"live_minutes": 20, "manual_capacity": 0},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = entry.options
    assert options["heat_cop"] == 4.1
    assert options["min_events"] == 3
    assert options["calibration_modes"] == "heat"
    assert options["outdoor_entity"] == "sensor.outside"
    assert options["pause_entities"] == ["binary_sensor.window"]
    assert "compressor_sensor" not in options

    merged = merged_config(dict(entry.data), dict(entry.options))
    assert "compressor_sensor" not in merged
    assert merged["temperature_sensor"] == "sensor.room"
