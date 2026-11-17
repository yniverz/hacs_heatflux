"""Binary sensor: whether the room model is ready."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HeatFluxConfigEntry
from .entity import HeatFluxEntity

READY = BinarySensorEntityDescription(
    key="model_ready",
    translation_key="model_ready",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeatFluxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensor."""
    async_add_entities([ReadySensor(entry, READY)])


class ReadySensor(HeatFluxEntity, BinarySensorEntity):
    """On once the room model has been fitted."""

    @property
    def is_on(self) -> bool:
        """Whether the model has parameters."""
        return self._engine.ready
