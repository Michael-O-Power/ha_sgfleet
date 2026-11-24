"""Vehicle image entity platform for SG Fleet Novated Leasing."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SgfleetConfigEntry
from .api import SgfleetError
from .coordinator import SgfleetDataUpdateCoordinator
from .entity import SgfleetVehicleEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SgfleetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SG Fleet vehicle image entities."""
    coordinator = entry.runtime_data
    entities: list[ImageEntity] = []
    if coordinator.data:
        for vehicle_guid in coordinator.data.vehicles:
            entities.append(SgfleetVehicleImage(hass, coordinator, vehicle_guid))
    async_add_entities(entities)


class SgfleetVehicleImage(SgfleetVehicleEntity, ImageEntity):
    """Displays the SG Fleet Jato vehicle render image."""

    _attr_translation_key = "vehicle_image"
    _attr_name = "Vehicle Image"
    _attr_content_type = "image/png"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
    ) -> None:
        SgfleetVehicleEntity.__init__(self, coordinator, vehicle_guid, "vehicle_image")
        ImageEntity.__init__(self, hass)
        self._cached_image: bytes | None = None
        self._attr_image_last_updated = datetime.now(timezone.utc)

    async def async_image(self) -> bytes | None:
        """Return cached vehicle PNG bytes, fetching from SG Fleet on first request."""
        if self._cached_image is not None:
            return self._cached_image
        try:
            img_bytes = await self.coordinator.api.get_vehicle_image(self._vehicle_guid)
        except SgfleetError as err:
            _LOGGER.warning("Unable to fetch SG Fleet vehicle image for %s: %s", self._vehicle_guid, err)
            return None
        if img_bytes:
            self._cached_image = img_bytes
            self._attr_image_last_updated = datetime.now(timezone.utc)
        return self._cached_image
