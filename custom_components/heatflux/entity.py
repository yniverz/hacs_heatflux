"""Shared base entity for Heat Flux."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN

if TYPE_CHECKING:
    from . import HeatFluxConfigEntry


class HeatFluxEntity(Entity):
    """Base entity: one device per room, state comes from the engine."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: HeatFluxConfigEntry, description: EntityDescription
    ) -> None:
        """Initialize the entity."""
        self.entity_description = description
        self._engine = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Custom",
            model="Heat flux",
        )

    async def async_added_to_hass(self) -> None:
        """Update whenever the engine has new results."""
        await super().async_added_to_hass()
        self.async_on_remove(self._engine.async_add_listener(self.async_write_ha_state))
