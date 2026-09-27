"""The Heat Flux integration.

Net heat flow into or out of a room from the AC power and the room
temperature, with the room's heat capacity learned from power steps.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION, merged_config
from .engine import HeatFluxEngine

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

type HeatFluxConfigEntry = ConfigEntry[HeatFluxEngine]


async def async_setup_entry(hass: HomeAssistant, entry: HeatFluxConfigEntry) -> bool:
    """Start the engine and the entities."""
    engine = HeatFluxEngine(
        hass, entry.entry_id, merged_config(dict(entry.data), dict(entry.options))
    )
    await engine.async_start()
    entry.runtime_data = engine
    entry.async_on_unload(engine.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HeatFluxConfigEntry) -> bool:
    """Unload the entities."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored heat capacity estimates."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _async_reload(hass: HomeAssistant, entry: HeatFluxConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
