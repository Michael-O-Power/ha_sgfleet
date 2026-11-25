"""Button platform for SG Fleet Novated Leasing."""

from __future__ import annotations

# from datetime import date

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
# from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SgfleetConfigEntry
# from .api import SgfleetError
from .const import (
    # CONF_ODOMETER_ENTITY_ID,
    TIER_BALANCES,
    TIER_DETAILS,
    TIER_REIMBURSEMENTS,
)
from .coordinator import SgfleetDataUpdateCoordinator
from .entity import DeviceGroup, SgfleetVehicleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SgfleetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SG Fleet button entities across Vehicle, Budgets, and Reimbursements devices."""
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = []
    if coordinator.data:
        for vehicle_guid in coordinator.data.vehicles:
            entities.extend(
                [
                    # Disabled: PUT /au-vehicle/v1.0/vehicles/{VehicleGuid}/odometer is not used
                    # by the mySG driver portal and returns HTTP 500 for novated accounts.
                    # SgfleetSubmitOdometerButton(coordinator, vehicle_guid),
                    SgfleetReauthButton(coordinator, vehicle_guid),
                    SgfleetRefreshButton(
                        coordinator,
                        vehicle_guid,
                        unique_suffix="refresh",
                        name="Refresh All Data",
                        device_group="vehicle",
                        tiers=None,
                    ),
                    SgfleetRefreshButton(
                        coordinator,
                        vehicle_guid,
                        unique_suffix="refresh_details",
                        name="Refresh Vehicle & Lease",
                        device_group="vehicle",
                        tiers={TIER_DETAILS},
                    ),
                    SgfleetRefreshButton(
                        coordinator,
                        vehicle_guid,
                        unique_suffix="refresh_balances",
                        name="Refresh Balances & Transactions",
                        device_group="budgets",
                        tiers={TIER_BALANCES},
                    ),
                    SgfleetRefreshButton(
                        coordinator,
                        vehicle_guid,
                        unique_suffix="refresh_reimbursements",
                        name="Refresh Reimbursements",
                        device_group="reimbursements",
                        tiers={TIER_REIMBURSEMENTS},
                    ),
                ]
            )
    async_add_entities(entities)


# class SgfleetSubmitOdometerButton(SgfleetVehicleEntity, ButtonEntity):
#     """Control button that pushes the linked Home Assistant odometer sensor to SG Fleet.
#
#     Currently disabled because PUT /au-vehicle/v1.0/vehicles/{VehicleGuid}/odometer
#     is not exposed in the mySG portal UI and returns HTTP 500 for novated driver sessions.
#     """
#
#     _attr_translation_key = "submit_odometer"
#     _attr_name = "Submit Odometer Reading"
#     _attr_icon = "mdi:counter"
#
#     def __init__(
#         self,
#         coordinator: SgfleetDataUpdateCoordinator,
#         vehicle_guid: str,
#     ) -> None:
#         super().__init__(
#             coordinator,
#             vehicle_guid,
#             "submit_odometer",
#             device_group="vehicle",
#         )
#
#     async def async_press(self) -> None:
#         """Read the linked odometer sensor, submit to SG Fleet, and refresh the vehicle."""
#         entity_id = self.coordinator.config_entry.options.get(CONF_ODOMETER_ENTITY_ID)
#         if not entity_id:
#             raise HomeAssistantError(
#                 "No linked odometer sensor configured. Open SG Fleet -> Configure and select "
#                 "your vehicle's odometer sensor (or call the sgfleet.register_odometer_reading action)."
#             )
#
#         state = self.hass.states.get(entity_id)
#         if state is None or state.state in ("unknown", "unavailable", ""):
#             raise HomeAssistantError(
#                 f"Linked odometer sensor '{entity_id}' is currently unavailable."
#             )
#
#         try:
#             odometer_km = float(state.state)
#         except ValueError as err:
#             raise HomeAssistantError(
#                 f"Linked odometer sensor '{entity_id}' has non-numeric state '{state.state}'."
#             ) from err
#
#         try:
#             await self.coordinator.api.register_odometer_reading(
#                 vehicle_guid=self._vehicle_guid,
#                 odometer=odometer_km,
#                 reading_date=date.today(),
#             )
#         except SgfleetError as err:
#             raise HomeAssistantError(f"Failed submitting odometer reading to SG Fleet: {err}") from err
#
#         await self.coordinator.async_refresh_vehicle(self._vehicle_guid)


class SgfleetReauthButton(SgfleetVehicleEntity, ButtonEntity):
    """Button that starts Home Assistant's interactive SG Fleet re-authentication flow."""

    _attr_translation_key = "log_back_in"
    _attr_name = "Log Back In"
    _attr_icon = "mdi:login"

    def __init__(
        self,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
    ) -> None:
        super().__init__(
            coordinator,
            vehicle_guid,
            "log_back_in",
            device_group="vehicle",
        )

    @property
    def available(self) -> bool:
        """Keep Log Back In clickable even if SG Fleet polling or auth has failed."""
        return True

    async def async_press(self) -> None:
        """Trigger the native Home Assistant reauth flow for this SG Fleet account."""
        self.coordinator.config_entry.async_start_reauth(self.hass)


class SgfleetRefreshButton(SgfleetVehicleEntity, ButtonEntity):
    """Button to trigger an immediate refresh of all or a specific SG Fleet data tier."""

    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
        *,
        unique_suffix: str,
        name: str,
        device_group: DeviceGroup,
        tiers: set[str] | None,
    ) -> None:
        super().__init__(
            coordinator,
            vehicle_guid,
            unique_suffix,
            device_group=device_group,
        )
        self._attr_name = name
        self._tiers = tiers

    async def async_press(self) -> None:
        """Force an immediate refresh of the configured tier(s), bypassing quiet hours."""
        await self.coordinator.async_request_tier_refresh(self._tiers)
