"""Rebuild the per-minute history from Home Assistant's recorder.

The recorded states of the input entities are replayed every 30 s through
the same code as the live readings (direction, compressor, coil, COP), so
the model can be fitted right away instead of after half a day.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections.abc import Callable
from datetime import timedelta
from functools import partial
import logging
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.core import State
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import CONF_POWER, CONF_TEMPERATURE, OPEN_METEO_URL, SAMPLE_INTERVAL_SECONDS
from .history import MinuteHistory

if TYPE_CHECKING:
    from .engine import HeatFluxEngine, StateGetter

_LOGGER = logging.getLogger(__name__)

OutdoorAt = Callable[[float, "StateGetter"], float | None]


class Timeline:
    """Recorded states per entity; gives the state valid at a time."""

    def __init__(self, states: dict[str, list[State]]) -> None:
        """Sort each entity's states by time."""
        self._states: dict[str, list[State]] = {}
        self._times: dict[str, list[float]] = {}
        for entity_id, items in states.items():
            ordered = sorted(
                (s for s in items if isinstance(s, State)),
                key=lambda s: s.last_updated_timestamp,
            )
            self._states[entity_id] = ordered
            self._times[entity_id] = [s.last_updated_timestamp for s in ordered]

    def getter(self, t: float) -> StateGetter:
        """The states as they were at time t (s)."""

        def get(entity_id: str) -> State | None:
            times = self._times.get(entity_id)
            if not times:
                return None
            i = bisect_right(times, t) - 1
            return self._states[entity_id][i] if i >= 0 else None

        return get


def replay(
    engine: HeatFluxEngine,
    timeline: Timeline,
    start: float,
    end: float,
    outdoor_at: OutdoorAt,
) -> MinuteHistory:
    """Readings every 30 s from start to end, binned like the live ones."""
    history = MinuteHistory(engine.history.max_minutes)
    t = start
    while t <= end:
        get = timeline.getter(t)
        reading = engine.evaluate(get, outdoor_at(t, get))
        history.add(t, reading.temperature, reading.thermal_power, reading.fittable)
        t += SAMPLE_INTERVAL_SECONDS
    return history


def interpolator(
    times: list[float], values: list[float]
) -> Callable[[float], float | None]:
    """Linear interpolation inside the given points, None outside."""

    def at(t: float) -> float | None:
        if not times or t < times[0] or t > times[-1]:
            return None
        i = bisect_right(times, t)
        if i >= len(times):
            return values[-1]
        t0, t1 = times[i - 1], times[i]
        v0, v1 = values[i - 1], values[i]
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0) if t1 > t0 else v0

    return at


async def async_open_meteo_history(
    engine: HeatFluxEngine, days: float
) -> Callable[[float], float | None] | None:
    """Hourly outdoor temperature of the last days at the HA location."""
    hass = engine.hass
    params = {
        "latitude": f"{hass.config.latitude:.4f}",
        "longitude": f"{hass.config.longitude:.4f}",
        "hourly": "temperature_2m",
        "past_days": str(min(92, int(days) + 1)),
        "forecast_days": "1",
        "timeformat": "unixtime",
        "timezone": "UTC",
    }
    try:
        session = async_get_clientsession(hass)
        async with (
            asyncio.timeout(30),
            session.get(OPEN_METEO_URL, params=params) as response,
        ):
            response.raise_for_status()
            payload = await response.json()
        hourly = payload["hourly"]
        points = [
            (float(t), float(v))
            for t, v in zip(hourly["time"], hourly["temperature_2m"], strict=True)
            if v is not None
        ]
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as err:
        _LOGGER.warning(
            "Outdoor temperature history from Open-Meteo unavailable: %s", err
        )
        return None
    return interpolator([p[0] for p in points], [p[1] for p in points])


async def async_rebuild_history(
    engine: HeatFluxEngine, days: float
) -> MinuteHistory | None:
    """History of the last days from the recorder, or None without data."""
    hass = engine.hass
    if "recorder" not in hass.config.components:
        return None
    # Imported here: the recorder is optional (after_dependencies).
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.history import (  # noqa: PLC0415
        get_significant_states,
    )

    end = dt_util.utcnow()
    start = end - timedelta(days=days)
    entities = engine.input_entities
    states = await get_instance(hass).async_add_executor_job(
        partial(
            get_significant_states,
            hass,
            start,
            end,
            entities,
            include_start_time_state=True,
            significant_changes_only=False,
        )
    )
    temperature = engine.config[CONF_TEMPERATURE]
    power = engine.config[CONF_POWER]
    if not states.get(temperature) or not states.get(power):
        return None

    weather: Callable[[float], float | None] | None = None
    if engine.uses_open_meteo:
        weather = await async_open_meteo_history(engine, days)

    def outdoor_at(t: float, get: StateGetter) -> float | None:
        if engine.uses_open_meteo:
            return weather(t) if weather else None
        return engine.outdoor_from_entity(get)

    timeline = Timeline(states)
    first = min(
        s.last_updated_timestamp for s in states[temperature] if isinstance(s, State)
    )
    replay_start = max(start.timestamp(), first)
    return await hass.async_add_executor_job(
        replay, engine, timeline, replay_start, end.timestamp(), outdoor_at
    )
