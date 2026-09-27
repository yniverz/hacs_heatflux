"""Button: forget the heat capacity estimates."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HeatFluxConfigEntry
from .entity import HeatFluxEntity

RESET = ButtonEntityDescription(
    key="reset_calibration",
    translation_key="reset_calibration",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeatFluxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the button."""
    async_add_entities([ResetButton(entry, RESET)])


class ResetButton(HeatFluxEntity, ButtonEntity):
    """Delete all heat capacity estimates and calibrate from scratch."""

    async def async_press(self) -> None:
        """Reset."""
        await self._engine.async_reset()
