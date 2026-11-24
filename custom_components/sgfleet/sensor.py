"""Sensor platform for SG Fleet Novated Leasing (mySG)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SgfleetConfigEntry
from .const import (
    BUDGET_CATEGORIES,
    CONF_LINKED_VEHICLE_DEVICE_ID,
    CONF_ODOMETER_ENTITY_ID,
    DEFAULT_CURRENCY,
    PENDING_REIMBURSEMENT_STATUSES,
)
from .coordinator import SgfleetCoordinatorData, SgfleetDataUpdateCoordinator, SgfleetVehicleData
from .entity import DeviceGroup, SgfleetVehicleEntity


def _parse_date(raw: Any) -> date | None:
    """Safely parse ISO date or datetime strings into a date object."""
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    try:
        text = str(raw).strip()
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _days_until(raw: Any) -> int | None:
    """Return integer days from today until the given ISO date."""
    target = _parse_date(raw)
    if target is None:
        return None
    return (target - date.today()).days


def _lease_progress_pct(vdata: SgfleetVehicleData) -> float | None:
    """Calculate percentage of lease term elapsed."""
    start = _parse_date(vdata.contract.get("startDate") or vdata.balance.get("contractStartDate"))
    end = _parse_date(vdata.contract.get("endDate") or vdata.balance.get("contractEndDate"))
    if not start or not end or end <= start:
        return None
    total_days = (end - start).days
    elapsed_days = max(0, min(total_days, (date.today() - start).days))
    return round((elapsed_days / total_days) * 100.0, 1)


def _pending_claims(vdata: SgfleetVehicleData) -> list[dict[str, Any]]:
    """Return reimbursement claims currently pending / in review."""
    result: list[dict[str, Any]] = []
    for item in vdata.reimbursement_history:
        status = str(item.get("status") or "").strip().lower()
        action = str(item.get("proposedActionText") or "").strip().lower()
        if status in PENDING_REIMBURSEMENT_STATUSES or action in PENDING_REIMBURSEMENT_STATUSES:
            result.append(item)
    return result


def _signed_transaction_amount(tx: dict[str, Any]) -> float | None:
    """Return signed AUD amount for a transaction ('E' = negative, 'I' = positive)."""
    amt = tx.get("amountInclTax")
    if amt is None:
        return None
    val = float(amt)
    if tx.get("incomeExpense") == "E" and val > 0:
        return -val
    return val


def _contract_distance_km(vdata: SgfleetVehicleData) -> float | None:
    """Return total contracted distance in km."""
    total = vdata.contract.get("totalDistance") or vdata.contract_version.get("totalDistance")
    if total is not None:
        return float(total)
    raw = str(vdata.contract.get("contractDistance") or "")
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
    return float(digits) if digits else None


def _resolve_odometer_entity_id(
    hass: HomeAssistant,
    coordinator: SgfleetDataUpdateCoordinator,
) -> str | None:
    """Resolve explicit odometer sensor or auto-discover from the linked HA vehicle device."""
    explicit = coordinator.config_entry.options.get(CONF_ODOMETER_ENTITY_ID)
    if explicit:
        return str(explicit)

    linked_dev_id = coordinator.config_entry.options.get(CONF_LINKED_VEHICLE_DEVICE_ID)
    if not linked_dev_id:
        return None

    ent_reg = er.async_get(hass)
    candidates = [
        entry
        for entry in er.async_entries_for_device(ent_reg, linked_dev_id)
        if entry.domain == "sensor" and entry.disabled_by is None
    ]
    # Prefer a non-diagnostic sensor with 'odometer' in entity_id (avoiding _v2 if primary exists)
    odo_matches = [e for e in candidates if "odometer" in e.entity_id.lower()]
    if odo_matches:
        odo_matches.sort(
            key=lambda e: (
                e.entity_category is not None,
                e.entity_id.lower().endswith("_v2"),
                len(e.entity_id),
            )
        )
        return odo_matches[0].entity_id
    return None


def _get_linked_odometer_km(hass: HomeAssistant, coordinator: SgfleetDataUpdateCoordinator) -> float | None:
    """Read optional linked Home Assistant odometer sensor state if configured."""
    entity_id = _resolve_odometer_entity_id(hass, coordinator)
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _linked_vehicle_info_attrs(
    hass: HomeAssistant,
    coordinator: SgfleetDataUpdateCoordinator,
) -> dict[str, Any]:
    """Return linked HA vehicle CI metadata attributes."""
    linked_dev_id = coordinator.config_entry.options.get(CONF_LINKED_VEHICLE_DEVICE_ID)
    if not linked_dev_id:
        return {}
    dev = dr.async_get(hass).async_get(linked_dev_id)
    return {
        "linked_vehicle_device_id": linked_dev_id,
        "linked_vehicle_name": (dev.name_by_user or dev.name) if dev else None,
        "linked_odometer_entity_id": _resolve_odometer_entity_id(hass, coordinator),
    }


@dataclass(frozen=True, kw_only=True)
class SgfleetSensorDescription(SensorEntityDescription):
    """Describes an SG Fleet vehicle sensor."""

    value_fn: Callable[[SgfleetVehicleData, SgfleetCoordinatorData, HomeAssistant, SgfleetDataUpdateCoordinator], Any]
    attrs_fn: Callable[[SgfleetVehicleData, SgfleetCoordinatorData, HomeAssistant, SgfleetDataUpdateCoordinator], dict[str, Any]] | None = None
    exists_fn: Callable[[SgfleetVehicleData, SgfleetCoordinatorData], bool] | None = None
    device_group: DeviceGroup = "vehicle"


def _format_claim_summary(claims: list[dict[str, Any]]) -> str:
    """Return a concise human-readable summary of active reimbursement claims."""
    if not claims:
        return "None"
    parts = []
    for cl in claims:
        ref = cl.get("referenceNumber") or "Claim"
        cat = cl.get("category") or "Expense"
        amt = float(cl.get("amount") or 0.0)
        status = cl.get("status") or "Pending"
        parts.append(f"{ref}: {cat} (${amt:,.2f} - {status})")
    return "; ".join(parts)


def _build_core_sensor_descriptions() -> tuple[SgfleetSensorDescription, ...]:
    """Return core sensor descriptions for each SG Fleet novated lease vehicle."""
    return (
        # --- Balances & Totals (Budgets & Transactions device) ---
        SgfleetSensorDescription(
            key="available_balance",
            translation_key="available_balance",
            name="Available Balance",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            state_class=SensorStateClass.TOTAL,
            icon="mdi:wallet",
            device_group="budgets",
            value_fn=lambda v, c, h, co: v.balance.get("availableBalance"),
            attrs_fn=lambda v, c, h, co: {
                "as_of_utc": v.balance.get("asOfUtc"),
                "expected_contributions": v.balance.get("expectedContributions"),
                "client_config_tolerance": v.balance.get("clientConfigTolerance"),
                "package_id": v.balance.get("packageId"),
                "employer_name": v.balance.get("employerName") or c.employer.get("employerName"),
            },
        ),
        SgfleetSensorDescription(
            key="actual_balance",
            translation_key="actual_balance",
            name="Actual Balance",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            state_class=SensorStateClass.TOTAL,
            icon="mdi:cash-multiple",
            device_group="budgets",
            value_fn=lambda v, c, h, co: (v.balance.get("actualBalance") or {}).get("balance"),
            attrs_fn=lambda v, c, h, co: {
                "received": (v.balance.get("actualBalance") or {}).get("received"),
                "spent": (v.balance.get("actualBalance") or {}).get("spent"),
                "pending": (v.balance.get("actualBalance") or {}).get("pending"),
            },
        ),
        SgfleetSensorDescription(
            key="total_received",
            translation_key="total_received",
            name="Total Contributions Received",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            state_class=SensorStateClass.TOTAL,
            icon="mdi:cash-plus",
            device_group="budgets",
            value_fn=lambda v, c, h, co: (v.balance.get("actualBalance") or {}).get("received"),
        ),
        SgfleetSensorDescription(
            key="total_spent",
            translation_key="total_spent",
            name="Total Running Costs Spent",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            state_class=SensorStateClass.TOTAL,
            icon="mdi:cash-minus",
            device_group="budgets",
            value_fn=lambda v, c, h, co: (v.balance.get("actualBalance") or {}).get("spent"),
        ),
        SgfleetSensorDescription(
            key="total_pending",
            translation_key="total_pending",
            name="Pending Transactions Total",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            state_class=SensorStateClass.TOTAL,
            icon="mdi:clock-outline",
            device_group="budgets",
            value_fn=lambda v, c, h, co: (v.balance.get("actualBalance") or {}).get("pending"),
        ),
        SgfleetSensorDescription(
            key="expected_contributions",
            translation_key="expected_contributions",
            name="Expected Contributions",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            state_class=SensorStateClass.TOTAL,
            icon="mdi:cash-clock",
            entity_registry_enabled_default=False,
            device_group="budgets",
            value_fn=lambda v, c, h, co: v.balance.get("expectedContributions"),
        ),
        SgfleetSensorDescription(
            key="last_transaction",
            translation_key="last_transaction",
            name="Last Transaction",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            icon="mdi:swap-horizontal",
            device_group="budgets",
            value_fn=lambda v, c, h, co: (
                _signed_transaction_amount(v.transactions[0]) if v.transactions else None
            ),
            attrs_fn=lambda v, c, h, co: (
                {
                    "transaction_id": v.transactions[0].get("transactionId"),
                    "expense_type": (v.transactions[0].get("expenseType") or {}).get("description"),
                    "expense_type_id": (v.transactions[0].get("expenseType") or {}).get("id"),
                    "description": v.transactions[0].get("description"),
                    "transaction_date": v.transactions[0].get("transactionDate"),
                    "processed_date": v.transactions[0].get("processedDate"),
                    "amount_incl_tax": v.transactions[0].get("amountInclTax"),
                    "amount_excl_tax": v.transactions[0].get("amountExclTax"),
                    "amount_tax": v.transactions[0].get("amountTax"),
                    "pending": v.transactions[0].get("pending"),
                    "income_expense": v.transactions[0].get("incomeExpense"),
                    "running_total_balance": v.transactions[0].get("runningTotalBalance"),
                    "recent_transactions": [
                        {
                            "date": tx.get("transactionDate"),
                            "type": (tx.get("expenseType") or {}).get("description"),
                            "description": tx.get("description"),
                            "amount": _signed_transaction_amount(tx),
                            "pending": tx.get("pending"),
                            "balance": tx.get("runningTotalBalance"),
                        }
                        for tx in v.transactions[:15]
                    ],
                }
                if v.transactions
                else {"recent_transactions": []}
            ),
        ),
        # --- Reimbursements (Reimbursements device) ---
        SgfleetSensorDescription(
            key="active_reimbursement_claims",
            translation_key="active_reimbursement_claims",
            name="Active Reimbursement Claims",
            native_unit_of_measurement="claims",
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:format-list-bulleted-square",
            device_group="reimbursements",
            value_fn=lambda v, c, h, co: len(_pending_claims(v)),
            attrs_fn=lambda v, c, h, co: {
                "total_amount": round(
                    sum(float(cl.get("amount") or 0.0) for cl in _pending_claims(v)), 2
                ),
                "summary": _format_claim_summary(_pending_claims(v)),
                "claims": [
                    {
                        "reimbursement_id": cl.get("reimbursementId"),
                        "reference": cl.get("referenceNumber"),
                        "category": cl.get("category"),
                        "amount": cl.get("amount"),
                        "status": cl.get("status"),
                        "proposed_action": cl.get("proposedActionText"),
                        "created_date": cl.get("createdDate"),
                    }
                    for cl in _pending_claims(v)
                ],
            },
        ),
        SgfleetSensorDescription(
            key="last_reimbursement_status",
            translation_key="last_reimbursement_status",
            name="Last Reimbursement Status",
            icon="mdi:file-document-check-outline",
            device_group="reimbursements",
            value_fn=lambda v, c, h, co: (
                v.reimbursement_history[0].get("status") if v.reimbursement_history else "None"
            ),
            attrs_fn=lambda v, c, h, co: {
                **(
                    {
                        "reimbursement_id": v.reimbursement_history[0].get("reimbursementId"),
                        "reference_number": v.reimbursement_history[0].get("referenceNumber"),
                        "category": v.reimbursement_history[0].get("category"),
                        "amount": v.reimbursement_history[0].get("amount"),
                        "proposed_action": v.reimbursement_history[0].get("proposedActionText"),
                        "created_date": v.reimbursement_history[0].get("createdDate"),
                    }
                    if v.reimbursement_history
                    else {}
                ),
                "total_recent_claims": len(v.reimbursement_history),
                "pending_claims_count": len(_pending_claims(v)),
                "enabled_categories": [
                    cat["category"]
                    for cat in v.reimbursement_categories
                    if cat.get("enabled") and cat.get("category")
                ],
                "recent_claims": [
                    {
                        "reference": cl.get("referenceNumber"),
                        "category": cl.get("category"),
                        "amount": cl.get("amount"),
                        "status": cl.get("status"),
                        "proposed_action": cl.get("proposedActionText"),
                        "created_date": cl.get("createdDate"),
                    }
                    for cl in v.reimbursement_history[:10]
                ],
            },
        ),
        SgfleetSensorDescription(
            key="pending_reimbursements_total",
            translation_key="pending_reimbursements_total",
            name="Pending Reimbursements",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            state_class=SensorStateClass.TOTAL,
            icon="mdi:receipt-clock",
            device_group="reimbursements",
            value_fn=lambda v, c, h, co: round(
                sum(float(cl.get("amount") or 0.0) for cl in _pending_claims(v)), 2
            ),
            attrs_fn=lambda v, c, h, co: {
                "count": len(_pending_claims(v)),
                "claims": [
                    {
                        "reference": cl.get("referenceNumber"),
                        "category": cl.get("category"),
                        "amount": cl.get("amount"),
                        "status": cl.get("status"),
                        "created_date": cl.get("createdDate"),
                    }
                    for cl in _pending_claims(v)
                ],
            },
        ),
        SgfleetSensorDescription(
            key="ev_home_charging_rate",
            translation_key="ev_home_charging_rate",
            name="EV Home Charging Rate",
            native_unit_of_measurement="AUD/km",
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:ev-plug-type2",
            device_group="reimbursements",
            value_fn=lambda v, c, h, co: c.reimbursement_settings.get("currentHomeChargingRate"),
            attrs_fn=lambda v, c, h, co: {
                "previous_rate": c.reimbursement_settings.get("previousHomeChargingRate"),
                "fbt_cutoff_date": c.reimbursement_settings.get("fbtCutoffDate"),
                "home_charging_enabled": any(
                    str(cat.get("category", "")).lower() == "homecharging" and cat.get("enabled")
                    for cat in v.reimbursement_categories
                ),
                "max_files_per_claim": c.reimbursement_settings.get("maxFilesPerClaim"),
                "max_file_size_mb": c.reimbursement_settings.get("maxFileSizeMb"),
            },
        ),
        # --- Vehicle & Lease Milestones ---
        SgfleetSensorDescription(
            key="registration_expiry",
            translation_key="registration_expiry",
            name="Registration Expiry",
            device_class=SensorDeviceClass.DATE,
            icon="mdi:card-account-details-outline",
            value_fn=lambda v, c, h, co: _parse_date(
                v.registration_details.get("registrationExpiryDate")
                or v.vehicle.get("registrationExpiryDate")
            ),
            attrs_fn=lambda v, c, h, co: {
                "days_remaining": _days_until(
                    v.registration_details.get("registrationExpiryDate")
                    or v.vehicle.get("registrationExpiryDate")
                ),
                "registration_state": v.vehicle.get("registrationState"),
                "number_of_renewal": v.registration_details.get("numberOfRenewal"),
                "renew_registration_flag": v.registration_details.get("renewRegistrationFlag"),
                "opt_out_enabled": v.registration_details.get("optOutEnabled"),
                "renewal_email_sent": v.registration_details.get("dateEmailSent"),
            },
        ),
        SgfleetSensorDescription(
            key="registration_days_remaining",
            translation_key="registration_days_remaining",
            name="Registration Days Remaining",
            native_unit_of_measurement=UnitOfTime.DAYS,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:calendar-clock",
            value_fn=lambda v, c, h, co: _days_until(
                v.registration_details.get("registrationExpiryDate")
                or v.vehicle.get("registrationExpiryDate")
            ),
        ),
        SgfleetSensorDescription(
            key="lease_end_date",
            translation_key="lease_end_date",
            name="Lease End Date",
            device_class=SensorDeviceClass.DATE,
            icon="mdi:calendar-end",
            value_fn=lambda v, c, h, co: _parse_date(
                v.contract.get("endDate") or v.balance.get("contractEndDate")
            ),
            attrs_fn=lambda v, c, h, co: {
                "start_date": v.contract.get("startDate") or v.balance.get("contractStartDate"),
                "expected_end_date": v.contract.get("expectedEndDate"),
                "contract_term": v.contract.get("contractTerm"),
                "total_term_months": v.contract.get("totalTerm"),
                "days_remaining": _days_until(
                    v.contract.get("endDate") or v.balance.get("contractEndDate")
                ),
                "percent_elapsed": _lease_progress_pct(v),
            },
        ),
        SgfleetSensorDescription(
            key="lease_days_remaining",
            translation_key="lease_days_remaining",
            name="Lease Days Remaining",
            native_unit_of_measurement=UnitOfTime.DAYS,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:calendar-range",
            value_fn=lambda v, c, h, co: _days_until(
                v.contract.get("endDate") or v.balance.get("contractEndDate")
            ),
        ),
        SgfleetSensorDescription(
            key="lease_progress",
            translation_key="lease_progress",
            name="Lease Progress",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:progress-clock",
            value_fn=lambda v, c, h, co: _lease_progress_pct(v),
        ),
        SgfleetSensorDescription(
            key="residual_value",
            translation_key="residual_value",
            name="Residual Value (Incl. GST)",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            icon="mdi:bank-transfer",
            value_fn=lambda v, c, h, co: (
                v.contract.get("residualValueInclGst")
                if v.contract.get("residualValueInclGst") is not None
                else v.contract_version.get("residualValueInclGst")
            ),
            attrs_fn=lambda v, c, h, co: {
                "residual_value_excl_gst": v.contract.get("residualValue")
                or v.contract_version.get("residualValue"),
                "residual_due_date": v.contract.get("residualValueDueDate")
                or v.contract_version.get("residualDueDate"),
                "days_until_due": _days_until(
                    v.contract.get("residualValueDueDate")
                    or v.contract_version.get("residualDueDate")
                ),
            },
        ),
        SgfleetSensorDescription(
            key="residual_due_date",
            translation_key="residual_due_date",
            name="Residual Value Due Date",
            device_class=SensorDeviceClass.DATE,
            icon="mdi:calendar-alert",
            entity_registry_enabled_default=False,
            value_fn=lambda v, c, h, co: _parse_date(
                v.contract.get("residualValueDueDate") or v.contract_version.get("residualDueDate")
            ),
        ),
        SgfleetSensorDescription(
            key="next_service_date",
            translation_key="next_service_date",
            name="Next Service Date",
            device_class=SensorDeviceClass.DATE,
            icon="mdi:car-cog",
            exists_fn=lambda v, c: bool(v.vehicle.get("nextServiceDate")),
            value_fn=lambda v, c, h, co: _parse_date(v.vehicle.get("nextServiceDate")),
            attrs_fn=lambda v, c, h, co: {
                "last_service_date": v.vehicle.get("lastServiceDate"),
                "days_remaining": _days_until(v.vehicle.get("nextServiceDate")),
            },
        ),
        SgfleetSensorDescription(
            key="last_odometer_reading",
            translation_key="last_odometer_reading",
            name="Last Recorded Odometer",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.KILOMETERS,
            state_class=SensorStateClass.TOTAL_INCREASING,
            icon="mdi:counter",
            exists_fn=lambda v, c: v.vehicle.get("lastValidOdoReading") is not None,
            value_fn=lambda v, c, h, co: v.vehicle.get("lastValidOdoReading"),
            attrs_fn=lambda v, c, h, co: {
                "reading_date": v.vehicle.get("lastValidOdoReadingDate"),
                "forecast_km_to_lease_end": v.vehicle.get("forecastKmToLeaseEnd"),
            },
        ),
        # --- Lease & Vehicle Details ---
        SgfleetSensorDescription(
            key="lease_status",
            translation_key="lease_status",
            name="Lease Status",
            icon="mdi:file-sign",
            value_fn=lambda v, c, h, co: (
                v.contract.get("contractStatus") or v.vehicle.get("contractStatus")
            ),
            attrs_fn=lambda v, c, h, co: {
                "contract_reference": v.contract.get("reference") or v.balance.get("contractReference"),
                "contract_guid": v.contract_guid,
                "contract_type": v.contract.get("contractType") or v.vehicle.get("contractType"),
                "contract_term": v.contract.get("contractTerm"),
                "total_term_months": v.contract.get("totalTerm"),
                "start_date": v.contract.get("startDate"),
                "end_date": v.contract.get("endDate"),
                "financier": v.contract.get("externalFinancier") or v.contract_version.get("financierName"),
                "financial_product": v.contract.get("externalFinancialProduct")
                or v.contract_version.get("financialProduct"),
                "product_name": v.contract.get("productName"),
                "finance_amount": v.contract_version.get("financeAmount"),
                "interest_rate": v.contract_version.get("interestRate"),
                "fbt_base_value": v.contract_version.get("benefitTaxBaseValue"),
                "fbt_effective_date": v.contract_version.get("benefitTaxEffectiveDate"),
                "insurance_type": v.contract.get("insuranceType"),
                "fuel_card_status": v.contract.get("fuelCardStatus"),
                "payroll_frequency": c.employer.get("payrollFrequency")
                or (c.profile.get("details") or {}).get("payrollFrequency"),
                "employer_name": c.employer.get("employerName") or v.balance.get("employerName"),
                "included_services": [
                    svc.get("serviceDescription")
                    for svc in v.contract_services
                    if svc.get("serviceDeliveryType") != "Not Included" and svc.get("serviceDescription")
                ],
                "excluded_services": [
                    svc.get("serviceDescription")
                    for svc in v.contract_services
                    if svc.get("serviceDeliveryType") == "Not Included" and svc.get("serviceDescription")
                ],
            },
        ),
        SgfleetSensorDescription(
            key="monthly_lease_rental",
            translation_key="monthly_lease_rental",
            name="Monthly Lease Rental",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement=DEFAULT_CURRENCY,
            icon="mdi:calendar-month",
            value_fn=lambda v, c, h, co: (
                v.balance.get("leaseRentalAmount")
                or v.contract_version.get("leaseRentalAmount")
                or v.contract.get("netMonthlyRental")
            ),
            attrs_fn=lambda v, c, h, co: {
                "net_monthly_rental": v.contract.get("netMonthlyRental"),
                "contract_version_monthly_rental": v.contract_version.get("monthlyRental"),
                "next_monthly_instalment": v.contract.get("nextMonthlyInstalment"),
                "next_finance_payment": v.contract.get("nextFinancePayment"),
            },
        ),
        SgfleetSensorDescription(
            key="contract_distance_allowance",
            translation_key="contract_distance_allowance",
            name="Contract Distance Allowance",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.KILOMETERS,
            icon="mdi:map-marker-distance",
            value_fn=lambda v, c, h, co: _contract_distance_km(v),
            attrs_fn=_distance_allowance_attrs,
        ),
        SgfleetSensorDescription(
            key="vehicle_details",
            translation_key="vehicle_details",
            name="Vehicle Status",
            entity_category=EntityCategory.DIAGNOSTIC,
            icon="mdi:car-info",
            value_fn=lambda v, c, h, co: v.vehicle.get("vehicleStatus") or "Unknown",
            attrs_fn=lambda v, c, h, co: {
                "vehicle_guid": v.vehicle_guid,
                "vehicle_id": v.vehicle.get("vehicleId"),
                "registration": v.registration,
                "make": v.vehicle.get("make"),
                "model": v.vehicle.get("model"),
                "series": v.vehicle.get("series"),
                "badge": v.vehicle.get("badge"),
                "model_year": v.vehicle.get("modelYear"),
                "description": v.vehicle.get("description"),
                "vin": (v.vehicle.get("vehicleSpecification") or {}).get("chassisNumber"),
                "fuel_type": (v.vehicle.get("vehicleSpecification") or {}).get("fuelPrimaryType"),
                "exterior_colour": v.vehicle.get("exteriorColour"),
                "interior_trim": v.vehicle.get("interiorTrim"),
                "asset_class": v.vehicle.get("assetClass"),
                "first_registered_date": v.vehicle.get("firstRegisteredDate"),
                "build_date": v.vehicle.get("buildDate"),
                "compliance_date": v.vehicle.get("complianceDate"),
                "catalogue_price": v.vehicle.get("cataloguePrice"),
                "cost_centre": v.vehicle.get("costCentre"),
                "vehicle_location": v.vehicle.get("vehicleLocation"),
                **_linked_vehicle_info_attrs(h, co),
            },
        ),
    )


def _distance_allowance_attrs(
    vdata: SgfleetVehicleData,
    cdata: SgfleetCoordinatorData,
    hass: HomeAssistant,
    coordinator: SgfleetDataUpdateCoordinator,
) -> dict[str, Any]:
    """Compute pro-rata lease distance tracking attributes (and variance if HA odometer linked)."""
    total_km = _contract_distance_km(vdata)
    pct = _lease_progress_pct(vdata)
    total_term = vdata.contract.get("totalTerm") or vdata.contract_version.get("totalTerm")
    attrs: dict[str, Any] = {
        "contract_distance_label": vdata.contract.get("contractDistance"),
        "distance_unit": vdata.contract.get("distanceUnit") or "Km",
    }
    if total_km and total_term:
        attrs["annual_km_allowance"] = round(total_km / (float(total_term) / 12.0), 0)
    if total_km and pct is not None:
        pro_rata = round(total_km * (pct / 100.0), 1)
        attrs["pro_rata_expected_km"] = pro_rata
        linked_odo = _get_linked_odometer_km(hass, coordinator)
        if linked_odo is not None:
            attrs["linked_odometer_entity_id"] = _resolve_odometer_entity_id(hass, coordinator)
            attrs["linked_odometer_km"] = linked_odo
            attrs["variance_from_pro_rata_km"] = round(linked_odo - pro_rata, 1)
            attrs["remaining_allowance_km"] = round(total_km - linked_odo, 1)
    return attrs


CORE_SENSOR_DESCRIPTIONS = _build_core_sensor_descriptions()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SgfleetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SG Fleet sensor entities from a config entry."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []

    if coordinator.data:
        for vehicle_guid, vdata in coordinator.data.vehicles.items():
            for desc in CORE_SENSOR_DESCRIPTIONS:
                if desc.exists_fn is None or desc.exists_fn(vdata, coordinator.data):
                    entities.append(SgfleetSensor(coordinator, vehicle_guid, desc))

            # Add per-category budget balance sensors
            for json_key, trans_key, friendly_name, icon, core_enabled in BUDGET_CATEGORIES:
                bucket = vdata.balance.get(json_key)
                if not isinstance(bucket, dict):
                    continue
                has_activity = any(
                    float(bucket.get(field) or 0.0) != 0.0
                    for field in ("received", "spent", "pending", "balance")
                )
                entities.append(
                    SgfleetBudgetCategorySensor(
                        coordinator=coordinator,
                        vehicle_guid=vehicle_guid,
                        json_key=json_key,
                        translation_key=trans_key,
                        name=friendly_name,
                        icon=icon,
                        enabled_default=has_activity and core_enabled,
                    )
                )

            # Optional linked HA odometer distance variance sensor
            if entry.options.get(CONF_ODOMETER_ENTITY_ID) or entry.options.get(
                CONF_LINKED_VEHICLE_DEVICE_ID
            ):
                entities.append(SgfleetDistanceVarianceSensor(coordinator, vehicle_guid))

    async_add_entities(entities)


class SgfleetSensor(SgfleetVehicleEntity, SensorEntity):
    """Generic SG Fleet novated lease sensor."""

    entity_description: SgfleetSensorDescription

    def __init__(
        self,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
        description: SgfleetSensorDescription,
    ) -> None:
        super().__init__(
            coordinator,
            vehicle_guid,
            description.key,
            device_group=description.device_group,
        )
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        vdata = self.vehicle_data
        if vdata is None or not self.coordinator.data:
            return None
        return self.entity_description.value_fn(vdata, self.coordinator.data, self.hass, self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        vdata = self.vehicle_data
        if vdata is None or not self.coordinator.data or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(vdata, self.coordinator.data, self.hass, self.coordinator)


class SgfleetBudgetCategorySensor(SgfleetVehicleEntity, SensorEntity):
    """Sensor representing an individual running-cost budget bucket."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = DEFAULT_CURRENCY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        *,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
        json_key: str,
        translation_key: str,
        name: str,
        icon: str,
        enabled_default: bool,
    ) -> None:
        super().__init__(
            coordinator,
            vehicle_guid,
            translation_key,
            device_group="budgets",
        )
        self._json_key = json_key
        self._attr_translation_key = translation_key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = enabled_default

    @property
    def _bucket(self) -> dict[str, Any]:
        vdata = self.vehicle_data
        if vdata is None:
            return {}
        raw = vdata.balance.get(self._json_key)
        return raw if isinstance(raw, dict) else {}

    @property
    def native_value(self) -> float | None:
        val = self._bucket.get("balance")
        return float(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        bucket = self._bucket
        return {
            "received": bucket.get("received"),
            "spent": bucket.get("spent"),
            "pending": bucket.get("pending"),
        }


class SgfleetDistanceVarianceSensor(SgfleetVehicleEntity, SensorEntity):
    """Sensor tracking linked HA odometer vs pro-rata novated lease distance allowance."""

    _attr_translation_key = "lease_distance_variance"
    _attr_name = "Lease Distance Variance"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:speedometer"

    def __init__(
        self,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
    ) -> None:
        super().__init__(coordinator, vehicle_guid, "lease_distance_variance")

    @property
    def native_value(self) -> float | None:
        vdata = self.vehicle_data
        if vdata is None:
            return None
        total_km = _contract_distance_km(vdata)
        pct = _lease_progress_pct(vdata)
        odo = _get_linked_odometer_km(self.hass, self.coordinator)
        if total_km is None or pct is None or odo is None:
            return None
        pro_rata = total_km * (pct / 100.0)
        return round(odo - pro_rata, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        vdata = self.vehicle_data
        if vdata is None or not self.coordinator.data:
            return None
        return _distance_allowance_attrs(vdata, self.coordinator.data, self.hass, self.coordinator)
