"""Binary sensor: whether the heat capacity is calibrated."""

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

CALIBRATED = BinarySensorEntityDescription(
    key="heat_capacity_calibrated",
    translation_key="heat_capacity_calibrated",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeatFluxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensor."""
    async_add_entities([CalibratedSensor(entry, CALIBRATED)])


class CalibratedSensor(HeatFluxEntity, BinarySensorEntity):
    """On after enough valid step events, or with a manual heat capacity."""

    @property
    def is_on(self) -> bool:
        """Whether the heat capacity can be trusted."""
        return self._engine.calibrated
