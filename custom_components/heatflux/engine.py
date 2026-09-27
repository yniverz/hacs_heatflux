"""Reads the input entities, samples them and runs the calculations."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter, TemperatureConverter

from .const import (
    COIL_DIRECTION_MARGIN,
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
    CONF_MIN_EVENTS,
    CONF_MIN_SLOPE_CHANGE,
    CONF_MIN_STEP,
    CONF_OUTDOOR,
    CONF_OUTDOOR_SOURCE,
    CONF_PAUSE,
    CONF_POWER,
    CONF_POWER_TYPE,
    CONF_SETTLE,
    CONF_STABILITY,
    CONF_STABILITY_FLOOR,
    CONF_STABLE,
    CONF_TEMPERATURE,
    COP_FIXED,
    DEFAULTS,
    DEFROST_MARGIN,
    DOMAIN,
    MAX_ESTIMATES,
    MODES_ALL,
    MODES_COOL,
    MODES_HEAT,
    OPEN_METEO_INTERVAL_MINUTES,
    OPEN_METEO_URL,
    OUTDOOR_ENTITY,
    OUTDOOR_MAX_AGE_HOURS,
    OUTDOOR_OPEN_METEO,
    PAUSE_STATES,
    POWER_ELECTRICAL,
    SAMPLE_INTERVAL_SECONDS,
    STORAGE_VERSION,
)
from .physics import (
    MODE_COOL,
    MODE_DRY,
    MODE_HEAT,
    MODE_OFF,
    CalibrationSettings,
    CalibrationState,
    Calibrator,
    CapacityEstimate,
    CapacitySummary,
    CopModel,
    CopResult,
    CopSettings,
    EventResult,
    Sample,
    net_heat_gain,
    summarize,
    window_stats,
)

_LOGGER = logging.getLogger(__name__)

_ACTIONS = {
    "heating": MODE_HEAT,
    "cooling": MODE_COOL,
    "drying": MODE_DRY,
    "idle": MODE_OFF,
    "off": MODE_OFF,
    "fan": MODE_OFF,
    "preheating": MODE_OFF,
}
_HVAC_MODES = {
    "heat": MODE_HEAT,
    "cool": MODE_COOL,
    "dry": MODE_DRY,
    "off": MODE_OFF,
    "fan_only": MODE_OFF,
}


@dataclass(frozen=True)
class Direction:
    """Which way the AC moves heat right now."""

    mode: str | None  # None: unknown
    defrost: bool = False


def ac_direction(
    action: str | None,
    hvac_mode: str | None,
    compressor: bool | None,
    coil: float | None,
    room: float | None,
) -> Direction:
    """Direction from the climate entity, the compressor and the indoor coil.

    hvac_action wins over the hvac mode. In auto mode without hvac_action the
    indoor coil tells: much warmer than the room is heating, colder is cooling.
    A running compressor in heating with a coil colder than the room defrosts.
    """
    if action == "defrosting":
        return Direction(MODE_HEAT, defrost=True)
    mode = _ACTIONS.get(action or "")
    if mode is None:
        mode = _HVAC_MODES.get(hvac_mode or "")
    if mode is None and coil is not None and room is not None:
        if coil > room + COIL_DIRECTION_MARGIN:
            mode = MODE_HEAT
        elif coil < room - COIL_DIRECTION_MARGIN:
            mode = MODE_COOL
    if compressor is False:
        return Direction(MODE_OFF)
    if mode is None:
        return Direction(None)
    defrost = (
        mode == MODE_HEAT
        and coil is not None
        and room is not None
        and coil < room - DEFROST_MARGIN
    )
    return Direction(mode, defrost=defrost)


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # NaN


@dataclass(frozen=True)
class Reading:
    """Current values of the inputs."""

    temperature: float | None
    raw_power: float | None
    direction: Direction
    thermal_power: float | None
    cop: CopResult | None
    paused: bool

    @property
    def calibratable(self) -> bool:
        """Whether this reading may enter a calibration event."""
        return not self.paused and not self.direction.defrost


class HeatFluxEngine:
    """One room: samples every 30 s, publishes the results to the entities."""

    def __init__(self, hass: HomeAssistant, entry_id: str, config: dict[str, Any]):
        """Initialize from the merged entry data and options."""
        self.hass = hass
        self.config = {**DEFAULTS, **config}
        c = self.config
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []

        self.electrical = c[CONF_POWER_TYPE] == POWER_ELECTRICAL
        self.cop_model = CopModel(
            CopSettings(
                heat_rated=float(c[CONF_HEAT_COP]),
                heat_ref_outdoor=float(c[CONF_HEAT_REF]),
                cool_rated=float(c[CONF_COOL_EER]),
                cool_ref_outdoor=float(c[CONF_COOL_REF]),
            )
        )
        self.calibrator = Calibrator(
            CalibrationSettings(
                stable_minutes=float(c[CONF_STABLE]),
                stability_percent=float(c[CONF_STABILITY]),
                stability_floor=float(c[CONF_STABILITY_FLOOR]),
                min_step=float(c[CONF_MIN_STEP]),
                settle_minutes=float(c[CONF_SETTLE]),
                fit_minutes=float(c[CONF_FIT]),
                min_slope_change=float(c[CONF_MIN_SLOPE_CHANGE]),
            )
        )
        self._live_seconds = float(c[CONF_LIVE_WINDOW]) * 60
        self._live: deque[Sample] = deque()
        self.estimates: list[CapacityEstimate] = []

        self._outdoor_fetched: float | None = None
        self._outdoor_fetched_value: float | None = None
        self._outdoor_failed_logged = False

        # Published values.
        self.reading: Reading | None = None
        self.temperature_rate: float | None = None
        self.mean_power: float | None = None
        self.net_heat_gain: float | None = None
        self.outdoor_temperature: float | None = None
        self.summary: CapacitySummary = summarize([])
        self.last_event: EventResult | None = None

    # ----------------------------------------------------------------- setup

    async def async_start(self) -> None:
        """Load the stored estimates and start listening."""
        stored = await self._store.async_load() or {}
        for item in stored.get("estimates", []):
            try:
                self.estimates.append(CapacityEstimate.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        self._update_summary()

        entities = [
            e
            for e in (
                self.config.get(CONF_TEMPERATURE),
                self.config.get(CONF_POWER),
                self.config.get(CONF_CLIMATE),
                self.config.get(CONF_COMPRESSOR),
                self.config.get(CONF_COIL),
                self.outdoor_entity,
                *(self.config.get(CONF_PAUSE) or []),
            )
            if e
        ]
        self._unsubs.append(
            async_track_state_change_event(self.hass, entities, self._on_state_change)
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._on_tick, timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
            )
        )
        if self.uses_open_meteo:
            self._unsubs.append(
                async_track_time_interval(
                    self.hass,
                    self._on_outdoor_timer,
                    timedelta(minutes=OPEN_METEO_INTERVAL_MINUTES),
                )
            )
            self.hass.async_create_background_task(
                self.async_refresh_outdoor(), f"{DOMAIN} outdoor temperature"
            )
        self._refresh(sample=True)

    async def async_stop(self) -> None:
        """Stop listening."""
        while self._unsubs:
            self._unsubs.pop()()

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Call update whenever the results change."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # --------------------------------------------------------------- outdoor

    @property
    def uses_open_meteo(self) -> bool:
        """Whether the outdoor temperature comes from Open-Meteo."""
        return (
            self.electrical
            and self.config[CONF_COP_MODE] != COP_FIXED
            and self.config.get(CONF_OUTDOOR_SOURCE) == OUTDOOR_OPEN_METEO
        )

    @property
    def outdoor_entity(self) -> str | None:
        """Entity with the outdoor temperature, if that is the source."""
        if self.config.get(CONF_OUTDOOR_SOURCE) == OUTDOOR_ENTITY:
            return self.config.get(CONF_OUTDOOR)
        return None

    async def _on_outdoor_timer(self, _now: datetime) -> None:
        await self.async_refresh_outdoor()

    async def async_refresh_outdoor(self) -> None:
        """Fetch the current temperature at the Home Assistant location."""
        params = {
            "latitude": f"{self.hass.config.latitude:.4f}",
            "longitude": f"{self.hass.config.longitude:.4f}",
            "current": "temperature_2m",
            "timeformat": "unixtime",
            "timezone": "UTC",
        }
        try:
            session = async_get_clientsession(self.hass)
            async with (
                asyncio.timeout(20),
                session.get(OPEN_METEO_URL, params=params) as response,
            ):
                response.raise_for_status()
                payload = await response.json()
            value = float(payload["current"]["temperature_2m"])
        except (
            aiohttp.ClientError,
            TimeoutError,
            KeyError,
            TypeError,
            ValueError,
        ) as err:
            if not self._outdoor_failed_logged:
                _LOGGER.warning(
                    "Outdoor temperature from Open-Meteo unavailable: %s", err
                )
                self._outdoor_failed_logged = True
            return
        self._outdoor_failed_logged = False
        self._outdoor_fetched_value = value
        self._outdoor_fetched = dt_util.utcnow().timestamp()
        self._refresh(sample=False)

    def _outdoor(self, now: float) -> float | None:
        if self.uses_open_meteo:
            if (
                self._outdoor_fetched is None
                or now - self._outdoor_fetched > OUTDOOR_MAX_AGE_HOURS * 3600
            ):
                return None
            return self._outdoor_fetched_value
        entity = self.outdoor_entity
        if not entity or (state := self.hass.states.get(entity)) is None:
            return None
        if state.domain == "weather":
            return self._celsius(
                state.attributes.get("temperature"),
                state.attributes.get("temperature_unit"),
            )
        return self._celsius(
            state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        )

    # ---------------------------------------------------------------- inputs

    @staticmethod
    def _celsius(value: Any, unit: str | None) -> float | None:
        number = _float(value)
        if number is None:
            return None
        if unit and unit != UnitOfTemperature.CELSIUS:
            try:
                return TemperatureConverter.convert(
                    number, unit, UnitOfTemperature.CELSIUS
                )
            except Exception:  # noqa: BLE001 - unknown unit: take the number
                return number
        return number

    def _temperature(self, key: str) -> float | None:
        entity = self.config.get(key)
        if not entity or (state := self.hass.states.get(entity)) is None:
            return None
        return self._celsius(
            state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        )

    def _power(self) -> float | None:
        state = self.hass.states.get(self.config[CONF_POWER])
        if state is None:
            return None
        number = _float(state.state)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if number is None or not unit or unit == UnitOfPower.WATT:
            return number
        try:
            return PowerConverter.convert(number, unit, UnitOfPower.WATT)
        except Exception:  # noqa: BLE001 - unknown unit: take the number
            return number

    def _direction(self, room: float | None, coil: float | None) -> Direction:
        climate_id = self.config.get(CONF_CLIMATE)
        compressor: bool | None = None
        if entity := self.config.get(CONF_COMPRESSOR):
            state = self.hass.states.get(entity)
            if state is not None and state.state in ("on", "off"):
                compressor = state.state == STATE_ON
        if not climate_id:
            return Direction(None)
        state = self.hass.states.get(climate_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return Direction(None)
        return ac_direction(
            state.attributes.get("hvac_action"), state.state, compressor, coil, room
        )

    def _paused(self) -> bool:
        for entity in self.config.get(CONF_PAUSE) or []:
            state = self.hass.states.get(entity)
            if state is not None and state.state in PAUSE_STATES:
                return True
        return False

    def read(self, now: float) -> Reading:
        """Current inputs and the thermal power of the AC."""
        room = self._temperature(CONF_TEMPERATURE)
        coil = self._temperature(CONF_COIL)
        raw = self._power()
        paused = self._paused()
        if self.config.get(CONF_CLIMATE):
            direction = self._direction(room, coil)
        elif raw is None:
            direction = Direction(None)
        else:
            direction = Direction(
                MODE_HEAT if raw > 0 else MODE_COOL if raw < 0 else MODE_OFF
            )

        self.outdoor_temperature = self._outdoor(now)
        mode = direction.mode
        cop: CopResult | None = None
        thermal: float | None = None
        if raw is not None and mode is not None:
            if mode == MODE_OFF or direction.defrost:
                thermal = 0.0
            else:
                magnitude = abs(raw)
                if self.electrical:
                    cop = self._cop(mode, room, coil)
                    magnitude *= cop.value
                thermal = magnitude if mode == MODE_HEAT else -magnitude
        return Reading(room, raw, direction, thermal, cop, paused)

    def _cop(self, mode: str, room: float | None, coil: float | None) -> CopResult:
        if self.config[CONF_COP_MODE] == COP_FIXED:
            return CopResult(self.cop_model.rated(mode), "rated")
        return self.cop_model.cop(mode, room, self.outdoor_temperature, coil)

    # ------------------------------------------------------------ processing

    @callback
    def _on_state_change(self, _event: Event[EventStateChangedData]) -> None:
        self._refresh(sample=False)

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self._refresh(sample=True)

    @callback
    def _refresh(self, sample: bool) -> None:
        now = dt_util.utcnow().timestamp()
        reading = self.read(now)
        self.reading = reading
        if (
            sample
            and reading.temperature is not None
            and reading.thermal_power is not None
        ):
            item = Sample(
                t=now,
                power=reading.thermal_power,
                temperature=reading.temperature,
                mode=reading.direction.mode or MODE_OFF,
                calibratable=reading.calibratable,
            )
            self._add_sample(item)
        self._update_live(now)
        self._notify()

    def _add_sample(self, sample: Sample) -> None:
        self._live.append(sample)
        while self._live and self._live[0].t < sample.t - self._live_seconds:
            self._live.popleft()
        result = self.calibrator.add(sample)
        if result is None:
            return
        self.last_event = result
        if result.estimate is not None:
            _LOGGER.info(
                "Heat capacity %.0f Wh/K from a step of %.0f W (%s)",
                result.estimate.capacity,
                result.estimate.power_after - result.estimate.power_before,
                result.estimate.mode,
            )
            self.estimates = [*self.estimates, result.estimate][-MAX_ESTIMATES:]
            self._update_summary()
            self._save()
        else:
            _LOGGER.debug("Calibration event rejected: %s", result.reason)

    def _update_live(self, now: float) -> None:
        samples = [s for s in self._live if s.t >= now - self._live_seconds]
        stats = window_stats(samples)
        if stats is None or stats.span < self._live_seconds * 0.5:
            self.temperature_rate = self.mean_power = self.net_heat_gain = None
            return
        self.temperature_rate = stats.slope
        self.mean_power = stats.mean_power
        capacity = self.capacity
        self.net_heat_gain = (
            None if capacity is None else net_heat_gain(stats, capacity)
        )

    # -------------------------------------------------------------- capacity

    @property
    def calibration_modes(self) -> tuple[str, ...]:
        """Modes whose events count for the heat capacity."""
        return {
            MODES_ALL: (MODE_HEAT, MODE_COOL),
            MODES_HEAT: (MODE_HEAT,),
            MODES_COOL: (MODE_COOL,),
        }[self.config.get(CONF_CALIBRATION_MODES, MODES_ALL)]

    def _update_summary(self) -> None:
        self.summary = summarize(self.estimates, self.calibration_modes)

    @property
    def manual_capacity(self) -> float | None:
        """Manual heat capacity override, if set."""
        value = _float(self.config.get(CONF_MANUAL_CAPACITY))
        return value if value else None

    @property
    def capacity(self) -> float | None:
        """Heat capacity in use (Wh/K)."""
        return self.manual_capacity or self.summary.capacity

    @property
    def calibrated(self) -> bool:
        """Enough valid events, or a manual heat capacity."""
        return bool(self.manual_capacity) or self.summary.inliers >= int(
            self.config[CONF_MIN_EVENTS]
        )

    @property
    def calibration_state(self) -> CalibrationState:
        """What the step detection is doing."""
        return self.calibrator.state

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"estimates": [e.as_dict() for e in self.estimates]}, 5
        )

    async def async_reset(self) -> None:
        """Forget all estimates and start calibrating again."""
        self.estimates = []
        self.last_event = None
        self.calibrator.reset()
        self._update_summary()
        await self._store.async_save({"estimates": []})
        self._update_live(dt_util.utcnow().timestamp())
        self._notify()
