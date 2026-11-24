"""Shared entity base class for SG Fleet Novated Leasing."""

from __future__ import annotations

from typing import Literal

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LINKED_VEHICLE_DEVICE_ID, DOMAIN, MANUFACTURER
from .coordinator import SgfleetDataUpdateCoordinator, SgfleetVehicleData

DeviceGroup = Literal["vehicle", "budgets", "reimbursements"]


def resolve_linked_vehicle_identifier(
    coordinator: SgfleetDataUpdateCoordinator,
    vehicle_guid: str,
) -> tuple[str, str] | None:
    """Resolve the primary (domain, id) identifier of the user-linked HA vehicle device."""
    linked_device_id = coordinator.config_entry.options.get(CONF_LINKED_VEHICLE_DEVICE_ID)
    if not linked_device_id:
        return None

    dev_reg = dr.async_get(coordinator.hass)
    target = dev_reg.async_get(linked_device_id)
    if target is None or not target.identifiers:
        return None

    # Guard against accidental self-link to an SG Fleet device for this vehicle
    for ident_domain, ident_val in target.identifiers:
        if ident_domain == DOMAIN and ident_val.startswith(vehicle_guid):
            return None

    return next(iter(target.identifiers))


class SgfleetVehicleEntity(CoordinatorEntity[SgfleetDataUpdateCoordinator]):
    """Base entity associated with a specific SG Fleet novated lease vehicle."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
        unique_suffix: str,
        *,
        device_group: DeviceGroup = "vehicle",
    ) -> None:
        super().__init__(coordinator)
        self._vehicle_guid = vehicle_guid
        self._device_group: DeviceGroup = device_group
        self._attr_unique_id = f"{vehicle_guid}_{unique_suffix}"

    @property
    def vehicle_data(self) -> SgfleetVehicleData | None:
        """Return the latest SgfleetVehicleData for this entity's vehicle."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.vehicles.get(self._vehicle_guid)

    @property
    def available(self) -> bool:
        """Return True if coordinator is healthy and vehicle data exists."""
        return super().available and self.vehicle_data is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return DeviceInfo grouping entities into Vehicle, Budgets, or Reimbursements."""
        vdata = self.vehicle_data
        base_name = vdata.display_name if vdata else f"SG Fleet Vehicle {self._vehicle_guid[:8]}"

        if self._device_group == "budgets":
            return DeviceInfo(
                identifiers={(DOMAIN, f"{self._vehicle_guid}_budgets")},
                name=f"{base_name} Budgets & Transactions",
                manufacturer=MANUFACTURER,
                model="Novated Lease Budgets",
                via_device=(DOMAIN, self._vehicle_guid),
                configuration_url=f"https://mysg.sgfleet.com/packages/{self._vehicle_guid}/balances",
            )

        if self._device_group == "reimbursements":
            return DeviceInfo(
                identifiers={(DOMAIN, f"{self._vehicle_guid}_reimbursements")},
                name=f"{base_name} Reimbursements",
                manufacturer=MANUFACTURER,
                model="Novated Lease Reimbursements",
                via_device=(DOMAIN, self._vehicle_guid),
                configuration_url=f"https://mysg.sgfleet.com/packages/{self._vehicle_guid}/reimbursements",
            )

        linked_via = resolve_linked_vehicle_identifier(self.coordinator, self._vehicle_guid)

        if vdata is None:
            return DeviceInfo(
                identifiers={(DOMAIN, self._vehicle_guid)},
                name=base_name,
                manufacturer=MANUFACTURER,
                via_device=linked_via,
            )

        vehicle = vdata.vehicle
        summary = vdata.balance.get("vehicleSummary") or {}
        make = vehicle.get("make") or summary.get("make") or MANUFACTURER
        model_parts = [
            vehicle.get("model") or summary.get("model"),
            vehicle.get("badge") or summary.get("badge"),
        ]
        model = " ".join(str(p).strip() for p in model_parts if p).strip() or "Novated Vehicle"
        contract_ref = vdata.contract.get("reference") or vdata.balance.get("contractReference")

        return DeviceInfo(
            identifiers={(DOMAIN, self._vehicle_guid)},
            name=base_name,
            manufacturer=str(make),
            model=model,
            serial_number=vdata.registration,
            sw_version=f"Contract {contract_ref}" if contract_ref else None,
            via_device=linked_via,
            configuration_url=f"https://mysg.sgfleet.com/packages/{self._vehicle_guid}/details",
        )
