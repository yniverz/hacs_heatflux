"""Constants for the Heat Flux integration."""

from __future__ import annotations

from typing import Any

DOMAIN = "heatflux"
DEFAULT_NAME = "Room"
STORAGE_VERSION = 1

# Sources (config flow and first options step).
CONF_TEMPERATURE = "temperature_sensor"
CONF_POWER = "power_sensor"
CONF_POWER_TYPE = "power_type"
CONF_CLIMATE = "climate_entity"
CONF_COMPRESSOR = "compressor_sensor"
CONF_COIL = "coil_temperature_sensor"
CONF_OUTDOOR_SOURCE = "outdoor_source"
CONF_OUTDOOR = "outdoor_entity"
CONF_PAUSE = "pause_entities"

POWER_ELECTRICAL = "electrical"
POWER_THERMAL = "thermal"

OUTDOOR_OPEN_METEO = "open_meteo"
OUTDOOR_ENTITY = "entity"
OUTDOOR_NONE = "none"

# Efficiency.
CONF_COP_MODE = "cop_mode"
CONF_HEAT_COP = "heat_cop"
CONF_HEAT_REF = "heat_ref_outdoor"
CONF_COOL_EER = "cool_eer"
CONF_COOL_REF = "cool_ref_outdoor"
COP_MODEL = "model"
COP_FIXED = "fixed"

# Calibration.
CONF_STABLE = "stable_minutes"
CONF_STABILITY = "stability_percent"
CONF_STABILITY_FLOOR = "stability_floor"
CONF_MIN_STEP = "min_step"
CONF_SETTLE = "settle_minutes"
CONF_FIT = "fit_minutes"
CONF_MIN_SLOPE_CHANGE = "min_slope_change"
CONF_CALIBRATION_MODES = "calibration_modes"
CONF_MIN_EVENTS = "min_events"
CONF_MANUAL_CAPACITY = "manual_capacity"
CONF_LIVE_WINDOW = "live_minutes"

MODES_ALL = "all"
MODES_HEAT = "heat"
MODES_COOL = "cool"

SOURCE_KEYS = (
    CONF_TEMPERATURE,
    CONF_POWER,
    CONF_POWER_TYPE,
    CONF_CLIMATE,
    CONF_COMPRESSOR,
    CONF_COIL,
    CONF_OUTDOOR_SOURCE,
    CONF_OUTDOOR,
    CONF_PAUSE,
)

DEFAULTS: dict[str, Any] = {
    CONF_POWER_TYPE: POWER_ELECTRICAL,
    CONF_OUTDOOR_SOURCE: OUTDOOR_OPEN_METEO,
    CONF_PAUSE: [],
    CONF_COP_MODE: COP_MODEL,
    CONF_HEAT_COP: 4.0,
    CONF_HEAT_REF: 7.0,
    CONF_COOL_EER: 6.1,
    CONF_COOL_REF: 25.0,
    CONF_LIVE_WINDOW: 20,
    CONF_STABLE: 20,
    CONF_STABILITY: 10,
    CONF_STABILITY_FLOOR: 60,
    CONF_MIN_STEP: 300,
    CONF_SETTLE: 4,
    CONF_FIT: 15,
    CONF_MIN_SLOPE_CHANGE: 0.2,
    CONF_CALIBRATION_MODES: MODES_ALL,
    CONF_MIN_EVENTS: 5,
    CONF_MANUAL_CAPACITY: 0,
}

SAMPLE_INTERVAL_SECONDS = 30
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_INTERVAL_MINUTES = 15
OUTDOOR_MAX_AGE_HOURS = 2
MAX_ESTIMATES = 30
# The indoor coil is this much colder than the room while "heating": defrost.
DEFROST_MARGIN = 2.0
# Coil vs room in auto mode without hvac_action: which way the AC works.
COIL_DIRECTION_MARGIN = 3.0
PAUSE_STATES = frozenset({"on", "open", "above_horizon", "true"})


def merged_config(data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Settings of an entry. Once the options flow ran, its sources replace
    those of the config flow (so cleared optional entities stay cleared)."""
    if CONF_TEMPERATURE in options:
        data = {k: v for k, v in data.items() if k not in SOURCE_KEYS}
    return {**DEFAULTS, **data, **options}
