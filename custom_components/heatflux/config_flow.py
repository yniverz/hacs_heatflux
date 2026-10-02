"""Config and options flow for the Heat Flux integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_BEFORE,
    CONF_CALIBRATION_MODES,
    CONF_CLIMATE,
    CONF_COIL,
    CONF_COMPRESSOR,
    CONF_COOL_EER,
    CONF_COOL_REF,
    CONF_COP_MODE,
    CONF_FIT,
    CONF_HEAT_COP,
    CONF_HEAT_REF,
    CONF_LIVE_WINDOW,
    CONF_MANUAL_CAPACITY,
    CONF_MAX_UNCERTAINTY,
    CONF_MIN_EVENTS,
    CONF_MIN_STEP,
    CONF_OUTDOOR,
    CONF_OUTDOOR_SOURCE,
    CONF_PAUSE,
    CONF_POWER,
    CONF_POWER_TYPE,
    CONF_SETTLE,
    CONF_TEMPERATURE,
    COP_FIXED,
    COP_MODEL,
    DEFAULT_NAME,
    DEFAULTS,
    DOMAIN,
    MODES_ALL,
    MODES_COOL,
    MODES_HEAT,
    OUTDOOR_ENTITY,
    OUTDOOR_NONE,
    OUTDOOR_OPEN_METEO,
    POWER_ELECTRICAL,
    POWER_THERMAL,
    SOURCE_KEYS,
    merged_config,
)

# key: (min, max, step, unit)
NUMBERS: dict[str, tuple[float, float, float, str | None]] = {
    CONF_HEAT_COP: (1.0, 10.0, 0.1, None),
    CONF_HEAT_REF: (-20.0, 20.0, 0.5, "°C"),
    CONF_COOL_EER: (1.0, 15.0, 0.1, None),
    CONF_COOL_REF: (15.0, 45.0, 0.5, "°C"),
    CONF_LIVE_WINDOW: (5, 60, 1, "min"),
    CONF_BEFORE: (5, 120, 1, "min"),
    CONF_MIN_STEP: (50, 5000, 10, "W"),
    CONF_SETTLE: (0, 30, 0.5, "min"),
    CONF_FIT: (5, 60, 1, "min"),
    CONF_MAX_UNCERTAINTY: (1, 100, 1, "%"),
    CONF_MIN_EVENTS: (1, 30, 1, None),
    CONF_MANUAL_CAPACITY: (0, 20000, 1, "Wh/K"),
}
SELECTS: dict[str, list[str]] = {
    CONF_COP_MODE: [COP_MODEL, COP_FIXED],
    CONF_CALIBRATION_MODES: [MODES_ALL, MODES_HEAT, MODES_COOL],
}
SECTIONS: dict[str, tuple[str, ...]] = {
    "efficiency": (
        CONF_COP_MODE,
        CONF_HEAT_COP,
        CONF_HEAT_REF,
        CONF_COOL_EER,
        CONF_COOL_REF,
    ),
    "calibration": (
        CONF_CALIBRATION_MODES,
        CONF_MIN_EVENTS,
        CONF_BEFORE,
        CONF_MIN_STEP,
        CONF_SETTLE,
        CONF_FIT,
        CONF_MAX_UNCERTAINTY,
    ),
    "live": (CONF_LIVE_WINDOW, CONF_MANUAL_CAPACITY),
}
INT_KEYS = (CONF_MIN_EVENTS,)


def _entity(domains: list[str], multiple: bool = False) -> selector.Selector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domains, multiple=multiple)
    )


def _select(options: list[str], key: str) -> selector.Selector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=key,
        )
    )


def _number(key: str) -> selector.Selector:
    low, high, step, unit = NUMBERS[key]
    config: selector.NumberSelectorConfig = {
        "min": low,
        "max": high,
        "step": step,
        "mode": selector.NumberSelectorMode.BOX,
    }
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _sources_schema(current: dict[str, Any], options: bool) -> vol.Schema:
    """Input entities; the options step adds outdoor source and pause."""

    def suggested(key: str) -> dict[str, Any]:
        return {"suggested_value": current.get(key)}

    fields: dict[vol.Marker, Any] = {
        vol.Required(
            CONF_TEMPERATURE, description=suggested(CONF_TEMPERATURE)
        ): _entity(["sensor"]),
        vol.Required(CONF_POWER, description=suggested(CONF_POWER)): _entity(
            ["sensor"]
        ),
        vol.Required(
            CONF_POWER_TYPE,
            default=current.get(CONF_POWER_TYPE, DEFAULTS[CONF_POWER_TYPE]),
        ): _select([POWER_ELECTRICAL, POWER_THERMAL], CONF_POWER_TYPE),
        vol.Optional(CONF_CLIMATE, description=suggested(CONF_CLIMATE)): _entity(
            ["climate"]
        ),
        vol.Optional(CONF_COMPRESSOR, description=suggested(CONF_COMPRESSOR)): _entity(
            ["binary_sensor"]
        ),
        vol.Optional(CONF_COIL, description=suggested(CONF_COIL)): _entity(["sensor"]),
    }
    if options:
        fields[
            vol.Required(
                CONF_OUTDOOR_SOURCE,
                default=current.get(CONF_OUTDOOR_SOURCE, DEFAULTS[CONF_OUTDOOR_SOURCE]),
            )
        ] = _select(
            [OUTDOOR_OPEN_METEO, OUTDOOR_ENTITY, OUTDOOR_NONE], CONF_OUTDOOR_SOURCE
        )
        fields[vol.Optional(CONF_OUTDOOR, description=suggested(CONF_OUTDOOR))] = (
            _entity(["sensor", "weather"])
        )
        fields[vol.Optional(CONF_PAUSE, description=suggested(CONF_PAUSE))] = _entity(
            ["binary_sensor", "input_boolean", "switch", "sun"], multiple=True
        )
    return vol.Schema(fields)


def _validate_sources(sources: dict[str, Any]) -> str | None:
    if sources.get(CONF_POWER_TYPE) == POWER_ELECTRICAL and not sources.get(
        CONF_CLIMATE
    ):
        return "climate_required"
    if sources.get(CONF_OUTDOOR_SOURCE) == OUTDOOR_ENTITY and not sources.get(
        CONF_OUTDOOR
    ):
        return "outdoor_entity_required"
    return None


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Only the sources that were filled in (cleared ones are dropped)."""
    return {k: user_input[k] for k in SOURCE_KEYS if user_input.get(k)}


class HeatFluxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one entry per room."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name and input entities."""
        errors: dict[str, str] = {}
        current: dict[str, Any] = {}
        if user_input is not None:
            sources = _clean(user_input)
            if error := _validate_sources(sources):
                errors["base"] = error
                current = user_input
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=sources
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=current.get(CONF_NAME, DEFAULT_NAME)
                ): str,
                **_sources_schema(current, options=False).schema,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return HeatFluxOptionsFlow()


class HeatFluxOptionsFlow(OptionsFlow):
    """Step 1: input entities. Step 2: efficiency and calibration."""

    def __init__(self) -> None:
        """Initialize."""
        self._sources: dict[str, Any] = {}

    def _current(self) -> dict[str, Any]:
        return merged_config(
            dict(self.config_entry.data), dict(self.config_entry.options)
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Input entities, outdoor temperature source and pause entities."""
        current = self._current()
        errors: dict[str, str] = {}
        if user_input is not None:
            sources = _clean(user_input)
            if error := _validate_sources(sources):
                errors["base"] = error
                current = {**current, **user_input}
            else:
                self._sources = sources
                return await self.async_step_settings()
        return self.async_show_form(
            step_id="init",
            data_schema=_sources_schema(current, options=True),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Efficiency, calibration and live calculation settings."""
        current = self._current()
        if user_input is not None:
            flat: dict[str, Any] = {}
            for name in SECTIONS:
                flat.update(user_input.get(name, {}))
            for key in INT_KEYS:
                if key in flat:
                    flat[key] = int(flat[key])
            return self.async_create_entry(data={**flat, **self._sources})

        def field(key: str) -> tuple[vol.Marker, selector.Selector]:
            marker = vol.Required(key, default=current.get(key, DEFAULTS[key]))
            if key in SELECTS:
                return marker, _select(SELECTS[key], key)
            return marker, _number(key)

        schema = vol.Schema(
            {
                vol.Required(name): section(
                    vol.Schema(dict(field(k) for k in keys)),
                    {"collapsed": name != "efficiency"},
                )
                for name, keys in SECTIONS.items()
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)
