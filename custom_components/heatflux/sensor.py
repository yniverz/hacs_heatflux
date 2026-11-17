"""Sensors: net heat gain, estimated room temperature, heat capacity, ..."""

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
from homeassistant.const import (
    EntityCategory,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HeatFluxConfigEntry
from .engine import HeatFluxEngine, ModelStatus
from .entity import HeatFluxEntity

UNIT_RATE = "K/h"
UNIT_CAPACITY = "Wh/K"
UNIT_COUPLING = "W/K"


def _time(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat()


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _param(name: str, digits: int = 0) -> Callable[[HeatFluxEngine], float | None]:
    def value(engine: HeatFluxEngine) -> float | None:
        params = engine.params
        return None if params is None else _round(getattr(params, name), digits)

    return value


def _gain_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    return {
        "ac_heat_delivered": _round(engine.delivered_heat, 0),
        "heat_capacity": _round(engine.capacity, 0),
    }


def _estimated_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    return {
        "air_temperature": _round(engine.air_temperature, 2),
        "rate": _round(engine.estimated_rate, 3),
        "air_in_1h_without_ac": _round(engine.settled_temperature, 2),
    }


def _capacity_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    p = engine.params
    errors = engine.forecast_errors or [None, None, None]
    return {
        "air_heat_capacity": _round(p.air_capacity, 0) if p else None,
        "mass_heat_capacity": _round(p.mass_capacity, 0) if p else None,
        "coupling": _round(p.coupling, 0) if p else None,
        "air_time_constant_min": _round(p.air_time_constant, 1) if p else None,
        "mass_time_constant_h": _round(p.mass_time_constant, 2) if p else None,
        "heat_delay_min": _round(p.heat_delay, 1) if p else None,
        "gain_noise": _round(p.gain_noise, 0) if p else None,
        "forecast_error_15min": _round(errors[0], 3),
        "forecast_error_30min": _round(errors[1], 3),
        "forecast_error_60min": _round(errors[2], 3),
    }


def _thermal_attrs(engine: HeatFluxEngine) -> dict[str, Any]:
    reading = engine.reading
    if reading is None:
        return {}
    return {
        "mode": reading.direction.mode,
        "defrost": reading.direction.defrost,
        "coil_ready": reading.delivering,
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
    return {
        "history_hours": round(engine.history.hours, 1),
        "ac_switches_in_history": engine.history.active_changes(),
        "last_fit": _time(engine.last_fit_time),
        "last_fit_result": engine.last_fit_outcome,
        "last_fit_seconds": _round(engine.last_fit_seconds, 1),
        "last_import": _time(engine.last_import_time),
        "last_import_result": engine.last_import_outcome,
    }


@dataclass(frozen=True, kw_only=True)
class HeatFluxSensorDescription(SensorEntityDescription):
    """Sensor with value and attribute functions."""

    value_fn: Callable[[HeatFluxEngine], Any]
    attrs_fn: Callable[[HeatFluxEngine], dict[str, Any]] | None = None
    electrical_only: bool = False


SENSORS: tuple[HeatFluxSensorDescription, ...] = (
    HeatFluxSensorDescription(
        key="net_heat_gain",
        translation_key="net_heat_gain",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda e: _round(e.net_heat_gain),
        attrs_fn=_gain_attrs,
    ),
    HeatFluxSensorDescription(
        key="estimated_temperature",
        translation_key="estimated_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda e: _round(e.estimated_temperature, 2),
        attrs_fn=_estimated_attrs,
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
        value_fn=lambda e: _round(e.capacity, 0),
        attrs_fn=_capacity_attrs,
    ),
    HeatFluxSensorDescription(
        key="air_heat_capacity",
        translation_key="air_heat_capacity",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UNIT_CAPACITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=_param("air_capacity"),
    ),
    HeatFluxSensorDescription(
        key="mass_heat_capacity",
        translation_key="mass_heat_capacity",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UNIT_CAPACITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=_param("mass_capacity"),
    ),
    HeatFluxSensorDescription(
        key="air_time_constant",
        translation_key="air_time_constant",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_param("air_time_constant", 1),
    ),
    HeatFluxSensorDescription(
        key="heat_delay",
        translation_key="heat_delay",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_param("heat_delay", 1),
    ),
    HeatFluxSensorDescription(
        key="forecast_error",
        translation_key="forecast_error",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="K",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        value_fn=lambda e: (
            _round(e.forecast_errors[-1], 3) if e.forecast_errors else None
        ),
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
        key="model_status",
        translation_key="model_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(ModelStatus.ALL),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda e: e.status,
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
