"""Sensors: net heat loss, temperature rate, heat capacity and diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HeatFluxConfigEntry
from .engine import HeatFluxEngine
from .entity import HeatFluxEntity
from .physics import CalibrationState

UNIT_RATE = "K/h"
UNIT_CAPACITY = "Wh/K"


def _time(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat()


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _loss_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    return {
        "ac_power_average": _round(engine.mean_power),
        "heat_capacity": _round(engine.capacity),
        "temperature_rate": _round(engine.temperature_rate, 3),
    }


def _capacity_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    summary = engine.summary
    last = engine.estimates[-1] if engine.estimates else None
    return {
        "source": "manual" if engine.manual_capacity else "estimated",
        "estimated": _round(summary.capacity),
        "events": summary.count,
        "valid_events": summary.inliers,
        "std_dev": _round(summary.std_dev),
        "median_abs_deviation": _round(summary.mad),
        "heating_median": _round(summary.per_mode.get("heat")),
        "heating_events": summary.per_mode_count.get("heat", 0),
        "cooling_median": _round(summary.per_mode.get("cool")),
        "cooling_events": summary.per_mode_count.get("cool", 0),
        "last_event": _time(last.t if last else None),
        "last_event_capacity": _round(last.capacity if last else None),
        "last_event_mode": last.mode if last else None,
    }


def _thermal_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    reading = engine.reading
    if reading is None:
        return {}
    return {
        "mode": reading.direction.mode,
        "defrost": reading.direction.defrost,
        "input_power": reading.raw_power,
        "cop": _round(reading.cop.value, 2) if reading.cop else None,
    }


def _cop_value(engine: HeatFluxEngine) -> float | None:
    reading = engine.reading
    return _round(reading.cop.value, 2) if reading and reading.cop else None


def _cop_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    reading = engine.reading
    cop = reading.cop if reading else None
    model = engine.cop_model
    return {
        "source": cop.source if cop else None,
        "condensing_temperature": _round(cop.condensing) if cop else None,
        "evaporating_temperature": _round(cop.evaporating) if cop else None,
        "outdoor_temperature": engine.outdoor_temperature,
        "carnot_efficiency_heating": round(model.eta_heat, 3),
        "carnot_efficiency_cooling": round(model.eta_cool, 3),
    }


def _status_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    last = engine.last_event
    return {
        "event_start": _time(engine.calibrator.event_start),
        "last_result": last.reason if last else None,
        "last_result_time": _time(last.t if last else None),
        "last_result_capacity": _round(last.capacity if last else None),
    }


@dataclass(frozen=True, kw_only=True)
class HeatFluxSensorDescription(SensorEntityDescription):
    """Sensor with value and attribute functions."""

    value_fn: Callable[[HeatFluxEngine], Any]
    attrs_fn: Callable[[HeatFluxEngine], dict[str, Any]] | None = None
    electrical_only: bool = False


SENSORS: tuple[HeatFluxSensorDescription, ...] = (
    HeatFluxSensorDescription(
        key="net_heat_loss",
        translation_key="net_heat_loss",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda e: _round(e.net_heat_loss),
        attrs_fn=_loss_attrs,
    ),
    HeatFluxSensorDescription(
        key="temperature_rate",
        translation_key="temperature_rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UNIT_RATE,
        suggested_display_precision=2,
        value_fn=lambda e: _round(e.temperature_rate, 3),
    ),
    HeatFluxSensorDescription(
        key="heat_capacity",
        translation_key="heat_capacity",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UNIT_CAPACITY,
        suggested_display_precision=0,
        value_fn=lambda e: _round(e.capacity),
        attrs_fn=_capacity_attrs,
    ),
    HeatFluxSensorDescription(
        key="ac_heat_output",
        translation_key="ac_heat_output",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda e: _round(e.reading.thermal_power) if e.reading else None,
        attrs_fn=_thermal_attrs,
    ),
    HeatFluxSensorDescription(
        key="cop",
        translation_key="cop",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        value_fn=_cop_value,
        attrs_fn=_cop_attrs,
        electrical_only=True,
    ),
    HeatFluxSensorDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=lambda e: e.outdoor_temperature,
        electrical_only=True,
    ),
    HeatFluxSensorDescription(
        key="calibration_status",
        translation_key="calibration_status",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in CalibrationState],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda e: e.calibration_state.value,
        attrs_fn=_status_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeatFluxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors."""
    engine = entry.runtime_data
    async_add_entities(
        HeatFluxSensor(entry, description)
        for description in SENSORS
        if engine.electrical or not description.electrical_only
    )


class HeatFluxSensor(HeatFluxEntity, SensorEntity):
    """A value of the engine."""

    entity_description: HeatFluxSensorDescription

    @property
    def native_value(self) -> Any:
        """Current value."""
        return self.entity_description.value_fn(self._engine)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Details."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._engine)
