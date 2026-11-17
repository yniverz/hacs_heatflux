"""Buttons: fit the room model now, rebuild its history, or forget it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HeatFluxConfigEntry
from .engine import HeatFluxEngine
from .entity import HeatFluxEntity


@dataclass(frozen=True, kw_only=True)
class HeatFluxButtonDescription(ButtonEntityDescription):
    """Button with its action."""

    press_fn: Callable[[HeatFluxEngine], Awaitable[None]]


BUTTONS = (
    HeatFluxButtonDescription(
        key="fit_model",
        translation_key="fit_model",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda engine: engine.async_maybe_fit(force=True),
    ),
    HeatFluxButtonDescription(
        key="rebuild_history",
        translation_key="rebuild_history",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda engine: engine.async_import_history(rebuild=True),
    ),
    HeatFluxButtonDescription(
        key="reset_model",
        translation_key="reset_model",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda engine: engine.async_reset(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeatFluxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the buttons."""
    async_add_entities(HeatFluxButton(entry, description) for description in BUTTONS)


class HeatFluxButton(HeatFluxEntity, ButtonEntity):
    """Runs an action of the engine."""

    entity_description: HeatFluxButtonDescription

    async def async_press(self) -> None:
        """Run the action."""
        await self.entity_description.press_fn(self._engine)
