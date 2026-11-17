"""Reads the input entities, keeps the history and runs the room model."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import time
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
    COIL_DELIVERY_MARGIN,
    COIL_DIRECTION_MARGIN,
    CONF_CLIMATE,
    CONF_COIL,
    CONF_COMPRESSOR,
    CONF_COOL_EER,
    CONF_COOL_REF,
    CONF_COP_MODE,
    CONF_HEAT_COP,
    CONF_HEAT_REF,
    CONF_HISTORY_DAYS,
    CONF_LIVE_WINDOW,
    CONF_OUTDOOR,
    CONF_OUTDOOR_SOURCE,
    CONF_PAUSE,
    CONF_POWER,
    CONF_POWER_TYPE,
    CONF_REFIT_HOURS,
    CONF_TEMPERATURE,
    COP_FIXED,
    DEFAULTS,
    DEFROST_MARGIN,
    DOMAIN,
    FILTER_RESET_HOURS,
    FIT_ACCEPT_RATIO,
    FIT_CHECK_MINUTES,
    FIT_EVALUATIONS,
    FIT_STEP_MINUTES,
    MIN_AC_CHANGES,
    MIN_HISTORY_HOURS,
    OPEN_METEO_INTERVAL_MINUTES,
    OPEN_METEO_URL,
    OUTDOOR_ENTITY,
    OUTDOOR_MAX_AGE_HOURS,
    OUTDOOR_OPEN_METEO,
    PAUSE_STATES,
    POWER_ELECTRICAL,
    REFIT_EVALUATIONS,
    SAMPLE_INTERVAL_SECONDS,
    STORAGE_VERSION,
)
from .history import MinuteHistory
from .model import (
    AIR,
    FitResult,
    RoomFilter,
    RoomParams,
    Series,
    fit,
    prediction_cost,
    warm_state,
)
from .physics import (
    MODE_COOL,
    MODE_DRY,
    MODE_HEAT,
    MODE_OFF,
    CopModel,
    CopResult,
    CopSettings,
    Sample,
    window_stats,
)

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY_SECONDS = 600

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


def coil_delivers(mode: str | None, coil: float | None, room: float | None) -> bool:
    """Whether the indoor coil is hot (heating) or cold (cooling) enough.

    The indoor fan waits for a warm coil after the compressor starts, and
    after a defrost the coil first has to warm up again: until then the
    compressor runs but no heat reaches the room. Without a coil sensor this
    can't be told and the AC always counts as delivering.
    """
    if coil is None or room is None:
        return True
    if mode == MODE_HEAT:
        return coil >= room + COIL_DELIVERY_MARGIN
    if mode in (MODE_COOL, MODE_DRY):
        return coil <= room - COIL_DELIVERY_MARGIN
    return True


@dataclass(frozen=True)
class Reading:
    """Current values of the inputs."""

    temperature: float | None
    raw_power: float | None
    direction: Direction
    thermal_power: float | None
    cop: CopResult | None
    paused: bool
    delivering: bool = True

    @property
    def fittable(self) -> bool:
        """Whether this reading may count for the model fit."""
        return not self.paused and not self.direction.defrost


class ModelStatus:
    """What the room model is doing."""

    LEARNING = "learning"
    FITTING = "fitting"
    READY = "ready"
    ALL = (LEARNING, FITTING, READY)


class HeatFluxEngine:
    """One room: samples every 30 s, publishes the results to the entities."""

    def __init__(self, hass: HomeAssistant, entry_id: str, config: dict[str, Any]):
        """Initialize from the merged entry data and options."""
        self.hass = hass
        self.config = {**DEFAULTS, **config}
        c = self.config
        self._entry_id = entry_id
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.model"
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
        self._live_seconds = float(c[CONF_LIVE_WINDOW]) * 60
        self._live: deque[Sample] = deque()
        self.history = MinuteHistory(int(float(c[CONF_HISTORY_DAYS]) * 24 * 60))
        self._refit_seconds = float(c[CONF_REFIT_HOURS]) * 3600

        self.params: RoomParams | None = None
        self.last_fit: FitResult | None = None
        self.last_fit_time: float | None = None
        self.last_fit_seconds: float | None = None
        self.last_fit_outcome: str | None = None
        self.fitting = False
        self._stored_errors: list[float] | None = None
        self._save_requested: float | None = None
        self.filter: RoomFilter | None = None
        self._filter_time: float | None = None
        self._last_power = 0.0

        self._outdoor_fetched: float | None = None
        self._outdoor_fetched_value: float | None = None
        self._outdoor_failed_logged = False

        # Published values.
        self.reading: Reading | None = None
        self.temperature_rate: float | None = None
        self.outdoor_temperature: float | None = None
        self.settled_temperature: float | None = None

    # ----------------------------------------------------------------- setup

    async def async_start(self) -> None:
        """Load the stored model and history and start listening."""
        stored = await self._store.async_load() or {}
        self.history = MinuteHistory.from_dict(
            stored.get("history") or {}, self.history.max_minutes
        )
        if params := stored.get("params"):
            try:
                self.params = RoomParams.from_dict(params)
            except (KeyError, TypeError, ValueError):
                self.params = None
        fit_info = stored.get("fit") or {}
        self.last_fit_time = fit_info.get("time")
        self.last_fit_outcome = fit_info.get("outcome")
        self._stored_errors = fit_info.get("forecast_errors")
        if self.params is not None:
            # Run the filter over the stored history: it starts settled.
            state = await self.hass.async_add_executor_job(
                warm_state, self.params, self.history.series(1)
            )
            self._apply(self.params, state)
        # Estimates of the old step calibration (before 0.4) are not used.
        await Store(self.hass, 1, f"{DOMAIN}.{self._entry_id}").async_remove()

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
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._on_fit_timer,
                timedelta(minutes=FIT_CHECK_MINUTES),
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
        self.hass.async_create_background_task(
            self.async_maybe_fit(), f"{DOMAIN} room model fit"
        )

    async def async_stop(self) -> None:
        """Stop listening and save the history."""
        while self._unsubs:
            self._unsubs.pop()()
        await self._store.async_save(self._data())

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
        delivering = coil_delivers(mode, coil, room)
        if raw is not None and mode is not None:
            if mode == MODE_OFF or direction.defrost or not delivering:
                thermal = 0.0
            else:
                magnitude = abs(raw)
                if self.electrical:
                    cop = self._cop(mode, room, coil)
                    magnitude *= cop.value
                thermal = magnitude if mode == MODE_HEAT else -magnitude
        return Reading(room, raw, direction, thermal, cop, paused, delivering)

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
        if sample:
            self._sample(now, reading)
        self._notify()

    def _sample(self, now: float, reading: Reading) -> None:
        power = reading.thermal_power
        self.history.add(now, reading.temperature, power, reading.fittable)
        if power is not None:
            self._last_power = power
        if reading.temperature is not None and power is not None:
            self._live.append(Sample(now, power, reading.temperature))
        while self._live and self._live[0].t < now - self._live_seconds:
            self._live.popleft()
        stats = window_stats(self._live)
        if stats is None or stats.span < self._live_seconds * 0.5:
            self.temperature_rate = None
        else:
            self.temperature_rate = stats.slope
        self._step_filter(now, reading.temperature)
        self._save()

    def _step_filter(self, now: float, measured: float | None) -> None:
        """Advance the live filter; the AC heat of the last interval counts."""
        if self.filter is None:
            return
        previous = self._filter_time
        self._filter_time = now
        if previous is None or not self.filter.ready:
            if measured is not None:
                self.filter.reset(measured)
            self._update_settled()
            return
        dt = (now - previous) / 3600
        if dt <= 0:
            return
        if dt > FILTER_RESET_HOURS:
            if measured is not None:
                self.filter.reset(measured, gain=self.filter.net_gain or 0.0)
            self._update_settled()
            return
        self.filter.step(dt, self._last_power, measured)
        self._update_settled()

    def _update_settled(self) -> None:
        if self.filter is None or not self.filter.ready:
            self.settled_temperature = None
            return
        self.settled_temperature = self.filter.forecast(1.0, 0.0)[AIR]

    # ----------------------------------------------------------------- model

    @property
    def status(self) -> str:
        """Learning (not enough data yet), fitting or ready."""
        if self.fitting:
            return ModelStatus.FITTING
        return ModelStatus.READY if self.params is not None else ModelStatus.LEARNING

    @property
    def ready(self) -> bool:
        """Whether the model has parameters."""
        return self.params is not None

    @property
    def capacity(self) -> float | None:
        """Total heat capacity, air and mass (Wh/K)."""
        return self.params.total_capacity if self.params else None

    @property
    def estimated_temperature(self) -> float | None:
        """The room's mass temperature (°C)."""
        return self.filter.mass_temperature if self.filter else None

    @property
    def air_temperature(self) -> float | None:
        """The filtered air temperature (°C)."""
        return self.filter.air_temperature if self.filter else None

    @property
    def net_heat_gain(self) -> float | None:
        """Net heat flow into the room without the AC (W)."""
        return self.filter.net_gain if self.filter else None

    @property
    def delivered_heat(self) -> float | None:
        """AC heat that reaches the room right now (W)."""
        return self.filter.delivered if self.filter else None

    @property
    def estimated_rate(self) -> float | None:
        """Rate of change of the mass temperature (K/h)."""
        return self.filter.mass_rate() if self.filter else None

    @property
    def forecast_errors(self) -> list[float] | None:
        """RMS forecast errors at 15, 30, 60 min of the last fit (K)."""
        if self.last_fit is not None:
            return self.last_fit.forecast_errors
        return self._stored_errors

    def _fit_due(self, now: float) -> bool:
        if self.fitting:
            return False
        if self.history.hours < MIN_HISTORY_HOURS:
            return False
        if self.history.active_changes() < MIN_AC_CHANGES:
            return False
        if self.params is None or self.last_fit_time is None:
            return True
        return now - self.last_fit_time >= self._refit_seconds

    async def _on_fit_timer(self, _now: datetime) -> None:
        await self.async_maybe_fit()

    async def async_maybe_fit(self, force: bool = False) -> None:
        """Fit the room model if it is due (or forced and there is data)."""
        now = dt_util.utcnow().timestamp()
        if not (self._fit_due(now) or (force and not self.fitting)):
            return
        series = self.history.series(FIT_STEP_MINUTES)
        fine = self.history.series(1)
        current = self.params
        self.fitting = True
        self._notify()
        started = time.monotonic()
        try:
            result, current_cost, state = await self.hass.async_add_executor_job(
                _run_fit, series, fine, current
            )
        finally:
            self.fitting = False
        self.last_fit_seconds = time.monotonic() - started
        self.last_fit_time = now
        if result is None:
            self.last_fit_outcome = "not_enough_data"
        elif current_cost is not None and result.cost > current_cost * FIT_ACCEPT_RATIO:
            self.last_fit_outcome = "kept_previous"
        else:
            self.last_fit_outcome = "updated"
            self.last_fit = result
            self._apply(result.params, state)
            _LOGGER.info(
                "Room model: air %.0f Wh/K, mass %.0f Wh/K, coupling %.0f W/K, "
                "heat delay %.1f min; forecast error %.2f K at 1 h (%.0f s)",
                result.params.air_capacity,
                result.params.mass_capacity,
                result.params.coupling,
                result.params.heat_delay,
                result.forecast_errors[-1],
                self.last_fit_seconds,
            )
        self._save(immediately=True)
        self._notify()

    def _apply(self, params: RoomParams, state: list[float] | None) -> None:
        """Use new parameters, starting from the state replayed on the history.

        The history ends with the last complete minute, so the live filter
        continues from there.
        """
        self.params = params
        self.filter = RoomFilter(params)
        if state is not None:
            self.filter.x = list(state)
            self._filter_time = self.history.end
        self._update_settled()

    # --------------------------------------------------------------- storage

    def _data(self) -> dict[str, Any]:
        errors = self.forecast_errors
        return {
            "params": self.params.as_dict() if self.params else None,
            "fit": {
                "time": self.last_fit_time,
                "outcome": self.last_fit_outcome,
                "forecast_errors": list(errors) if errors else None,
            },
            "history": self.history.as_dict(),
        }

    def _save(self, immediately: bool = False) -> None:
        # The history changes every minute; write it every 10 minutes (and
        # on shutdown), the model right away. A new delayed save request
        # restarts the delay, so only ask when none is pending.
        now = time.monotonic()
        if (
            not immediately
            and self._save_requested is not None
            and now - self._save_requested < SAVE_DELAY_SECONDS
        ):
            return
        self._save_requested = now
        self._store.async_delay_save(
            self._data, 1 if immediately else SAVE_DELAY_SECONDS
        )

    async def async_reset(self) -> None:
        """Forget the model and the history and start learning again."""
        self.params = None
        self.filter = None
        self.last_fit = None
        self.last_fit_time = None
        self.last_fit_outcome = None
        self._stored_errors = None
        self.settled_temperature = None
        self.history.clear()
        await self._store.async_save(self._data())
        self._notify()


def _run_fit(
    series: Series, fine: Series, current: RoomParams | None
) -> tuple[FitResult | None, float | None, list[float] | None]:
    """Fit in the executor.

    Also returns the cost of the current parameters (to compare) and the
    filter state of the new ones after the history at 1 min resolution.
    """
    current_cost = prediction_cost(current, series)[0] if current else None
    if current is None:
        result = fit(series, max_evaluations=FIT_EVALUATIONS)
    else:
        result = fit(
            series, start=current, max_evaluations=REFIT_EVALUATIONS, restarts=False
        )
    state = warm_state(result.params, fine) if result else None
    return result, current_cost, state
