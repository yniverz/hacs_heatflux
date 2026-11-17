"""Importing the history from Home Assistant's recorder."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heatflux.backfill import interpolator
from custom_components.heatflux.const import CONF_OUTDOOR_SOURCE, DOMAIN, OUTDOOR_NONE

from .room_sim import SimRoom
from .test_init import AC, COMPRESSOR, POWER, ROOM, SOURCES

COP = 4.0


class FakeRecorder:
    """Recorded states, served like the recorder's get_significant_states.

    (The real recorder test fixtures don't work on Python 3.14 yet.)
    """

    def __init__(self) -> None:
        """Empty."""
        self.states: dict[str, list[State]] = {}

    def record(self, when: datetime, entity_id: str, state: str, attrs: dict) -> None:
        """Add a state change (attribute-only changes count too)."""
        items = self.states.setdefault(entity_id, [])
        if items and items[-1].state == state and items[-1].attributes == attrs:
            return
        items.append(
            State(entity_id, state, attrs, last_changed=when, last_updated=when)
        )

    def query(self, hass, start, end, entity_ids, **kwargs) -> dict[str, list[State]]:
        """States of the entities in the period (and the one valid at start)."""
        result: dict[str, list[State]] = {}
        for entity_id in entity_ids:
            items = self.states.get(entity_id, [])
            before = [s for s in items if s.last_updated <= start]
            inside = [s for s in items if start < s.last_updated <= end]
            chosen = before[-1:] + inside
            if chosen:
                result[entity_id] = chosen
        return result


@pytest.fixture
def recorder(hass: HomeAssistant) -> Iterator[FakeRecorder]:
    fake = FakeRecorder()
    instance = MagicMock()

    async def run(job):
        return job()

    instance.async_add_executor_job = run
    hass.config.components.add("recorder")
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.components.recorder.history.get_significant_states",
            side_effect=fake.query,
        ),
    ):
        yield fake


def test_interpolator() -> None:
    at = interpolator([0.0, 3600.0, 7200.0], [10.0, 12.0, 11.0])
    assert at(1800) == pytest.approx(11.0)
    assert at(7200) == 11.0
    assert at(-1) is None
    assert at(8000) is None
    assert interpolator([], [])(5) is None


def _record_room(recorder: FakeRecorder, hours: float) -> SimRoom:
    """Run a simulated room for the last hours; its states are recorded."""
    room = SimRoom()
    now = dt_util.utcnow()
    steps = round(hours * 120)
    for i in range(steps):
        room.advance(30)
        when = now - timedelta(seconds=30 * (steps - 1 - i))
        recorder.record(
            when, ROOM, f"{room.measured():.1f}", {"unit_of_measurement": "°C"}
        )
        power = room.power / COP if room.on else 5.0
        recorder.record(when, POWER, str(power), {"unit_of_measurement": "W"})
        recorder.record(
            when, AC, "heat", {"hvac_action": "heating" if room.on else "idle"}
        )
        recorder.record(when, COMPRESSOR, "on" if room.on else "off", {})
    return room


def _publish_current(hass: HomeAssistant, recorder: FakeRecorder) -> None:
    """The current states are the last recorded ones."""
    for entity_id, items in recorder.states.items():
        hass.states.async_set(entity_id, items[-1].state, items[-1].attributes)


async def test_model_ready_right_after_setup(
    hass: HomeAssistant, recorder: FakeRecorder
) -> None:
    """14 h of recorded data: no learning period."""
    room = _record_room(recorder, 14)
    _publish_current(hass, recorder)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data=SOURCES,
        options={**SOURCES, CONF_OUTDOOR_SOURCE: OUTDOOR_NONE},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    status = hass.states.get("sensor.living_room_model_status")
    assert status.state == "ready", status.attributes
    assert status.attributes["last_import_result"] == "imported"
    assert status.attributes["history_hours"] == pytest.approx(14, abs=0.2)
    assert status.attributes["last_fit_result"] == "updated"

    capacity = hass.states.get("sensor.living_room_heat_capacity")
    total = room.air_capacity + room.mass_capacity
    assert float(capacity.state) == pytest.approx(total, rel=0.3)
    # The filter was replayed over the imported history: settled right away.
    estimated = hass.states.get("sensor.living_room_room_temperature_estimated")
    assert float(estimated.state) == pytest.approx(room.mass, abs=0.3)
    gain = float(hass.states.get("sensor.living_room_net_heat_gain").state)
    assert gain == pytest.approx(room.gain(room.t), abs=250)


async def test_rebuild_replaces_history(
    hass: HomeAssistant, recorder: FakeRecorder
) -> None:
    """Without enough recorded data the model keeps learning; a rebuild
    replaces the history with the recorder's."""
    _record_room(recorder, 2)
    _publish_current(hass, recorder)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data=SOURCES,
        options={**SOURCES, CONF_OUTDOOR_SOURCE: OUTDOOR_NONE},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    status = hass.states.get("sensor.living_room_model_status")
    assert status.state == "learning"
    assert status.attributes["last_import_result"] == "imported"
    assert status.attributes["history_hours"] == pytest.approx(2, abs=0.2)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.living_room_rebuild_history_from_recorder"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    status = hass.states.get("sensor.living_room_model_status")
    assert status.attributes["last_import_result"] == "imported"
    assert status.attributes["history_hours"] == pytest.approx(2, abs=0.2)
    assert status.attributes["last_fit_result"] == "not_enough_data"


async def test_no_recorded_data(hass: HomeAssistant, recorder: FakeRecorder) -> None:
    hass.states.async_set(ROOM, "21.0", {"unit_of_measurement": "°C"})
    hass.states.async_set(POWER, "5.0", {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data=SOURCES,
        options={**SOURCES, CONF_OUTDOOR_SOURCE: OUTDOOR_NONE},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    status = hass.states.get("sensor.living_room_model_status")
    assert status.state == "learning"
    assert status.attributes["last_import_result"] == "no_data"


async def test_outdoor_history_from_open_meteo(
    hass: HomeAssistant, recorder: FakeRecorder, aioclient_mock
) -> None:
    """With Open-Meteo as the source, past hours use its hourly history."""
    from custom_components.heatflux.const import OPEN_METEO_URL

    now = dt_util.utcnow().timestamp()
    hours = [now - 3600 * k for k in range(30, -2, -1)]
    aioclient_mock.get(
        OPEN_METEO_URL,
        json={
            "current": {"temperature_2m": 5.0},
            "hourly": {"time": hours, "temperature_2m": [5.0] * len(hours)},
        },
    )
    _record_room(recorder, 14)
    _publish_current(hass, recorder)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Living room", data=SOURCES, options={**SOURCES}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert any("past_days" in str(call[1]) for call in aioclient_mock.mock_calls)
    status = hass.states.get("sensor.living_room_model_status")
    assert status.attributes["last_import_result"] == "imported"
    assert status.state == "ready"
