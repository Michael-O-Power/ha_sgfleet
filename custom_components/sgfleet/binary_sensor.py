"""Binary sensor platform for SG Fleet Novated Leasing (mySG)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SgfleetConfigEntry
from .const import (
    CONF_REGO_WARNING_DAYS,
    DEFAULT_REGO_WARNING_DAYS,
    PENDING_REIMBURSEMENT_STATUSES,
)
from .coordinator import SgfleetCoordinatorData, SgfleetDataUpdateCoordinator, SgfleetVehicleData
from .entity import DeviceGroup, SgfleetVehicleEntity
from .sensor import _days_until


@dataclass(frozen=True, kw_only=True)
class SgfleetBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an SG Fleet binary sensor."""

    is_on_fn: Callable[[SgfleetVehicleData, SgfleetCoordinatorData, int], bool | None]
    device_group: DeviceGroup = "vehicle"


BINARY_SENSOR_DESCRIPTIONS: tuple[SgfleetBinarySensorDescription, ...] = (
    SgfleetBinarySensorDescription(
        key="registration_expiring_soon",
        translation_key="registration_expiring_soon",
        name="Registration Expiring Soon",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:car-clock",
        device_group="vehicle",
        is_on_fn=lambda v, c, warn_days: (
            (days := _days_until(
                v.registration_details.get("registrationExpiryDate")
                or v.vehicle.get("registrationExpiryDate")
            )) is not None
            and days <= warn_days
        ),
    ),
    SgfleetBinarySensorDescription(
        key="negative_available_balance",
        translation_key="negative_available_balance",
        name="Negative Available Balance",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:alert-circle-outline",
        device_group="budgets",
        is_on_fn=lambda v, c, warn_days: (
            (bal := v.balance.get("availableBalance")) is not None and float(bal) < 0
        ),
    ),
    SgfleetBinarySensorDescription(
        key="pending_reimbursements",
        translation_key="pending_reimbursements",
        name="Pending Reimbursement Claims",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:file-clock-outline",
        device_group="reimbursements",
        is_on_fn=lambda v, c, warn_days: any(
            str(cl.get("status") or "").strip().lower() in PENDING_REIMBURSEMENT_STATUSES
            or str(cl.get("proposedActionText") or "").strip().lower() in PENDING_REIMBURSEMENT_STATUSES
            for cl in v.reimbursement_history
        ),
    ),
    SgfleetBinarySensorDescription(
        key="lease_active",
        translation_key="lease_active",
        name="Lease Active",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:check-decagram",
        device_group="vehicle",
        is_on_fn=lambda v, c, warn_days: bool(
            v.contract.get("isActive")
            if "isActive" in v.contract
            else str(v.vehicle.get("contractStatus") or "").lower() == "running"
        ),
    ),
    SgfleetBinarySensorDescription(
        key="valid_bank_account",
        translation_key="valid_bank_account",
        name="Valid Bank Account",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:bank-check",
        device_group="reimbursements",
        is_on_fn=lambda v, c, warn_days: c.has_valid_bank_account,
    ),
    SgfleetBinarySensorDescription(
        key="data_load_in_progress",
        translation_key="data_load_in_progress",
        name="SG Fleet Data Load In Progress",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:database-sync",
        device_group="budgets",
        is_on_fn=lambda v, c, warn_days: bool(c.data_load_status.get("loadInProgress")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SgfleetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SG Fleet binary sensor entities."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    if coordinator.data:
        for vehicle_guid in coordinator.data.vehicles:
            for desc in BINARY_SENSOR_DESCRIPTIONS:
                entities.append(SgfleetBinarySensor(coordinator, vehicle_guid, desc))
    async_add_entities(entities)


class SgfleetBinarySensor(SgfleetVehicleEntity, BinarySensorEntity):
    """Binary sensor for SG Fleet novated lease alerts and status."""

    entity_description: SgfleetBinarySensorDescription

    def __init__(
        self,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
        description: SgfleetBinarySensorDescription,
    ) -> None:
        super().__init__(
            coordinator,
            vehicle_guid,
            description.key,
            device_group=description.device_group,
        )
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        vdata = self.vehicle_data
        if vdata is None or not self.coordinator.data:
            return None
        warn_days = int(
            self.coordinator.config_entry.options.get(
                CONF_REGO_WARNING_DAYS, DEFAULT_REGO_WARNING_DAYS
            )
        )
        return self.entity_description.is_on_fn(vdata, self.coordinator.data, warn_days)
