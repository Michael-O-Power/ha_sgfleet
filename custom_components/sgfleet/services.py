"""Home Assistant services for SG Fleet Novated Leasing (mySG)."""

from __future__ import annotations

from datetime import date, datetime
import logging
from pathlib import Path
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .api import SgfleetError, SgfleetValidationError
from .const import (
    ALL_TIERS,
    CATEGORY_HOME_CHARGING,
    CONF_ODOMETER_ENTITY_ID,
    DOMAIN,
    MAX_CLAIM_AMOUNT,
    REIMBURSEMENT_CATEGORIES,
    SERVICE_REFRESH_DATA,
    SERVICE_REGISTER_ODOMETER,
    SERVICE_SUBMIT_HOME_CHARGING_CLAIM,
    SERVICE_SUBMIT_REIMBURSEMENT_CLAIM,
    TIER_ALL,
    TIER_BALANCES,
    TIER_DETAILS,
    TIER_REIMBURSEMENTS,
)
from .coordinator import SgfleetDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

_RECEIPT_CATEGORIES = tuple(c for c in REIMBURSEMENT_CATEGORIES if c != CATEGORY_HOME_CHARGING)

SUBMIT_HOME_CHARGING_SCHEMA = vol.Schema(
    {
        vol.Optional("vehicle_guid"): cv.string,
        vol.Optional("device_id"): cv.string,
        vol.Required("start_date"): cv.date,
        vol.Required("end_date"): cv.date,
        vol.Required("start_odometer"): vol.Coerce(float),
        vol.Optional("end_odometer"): vol.Coerce(float),
        vol.Optional("odometer_entity_id"): cv.entity_id,
        vol.Optional("confirm_ato_declaration", default=False): cv.boolean,
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)

SUBMIT_REIMBURSEMENT_SCHEMA = vol.Schema(
    {
        vol.Optional("vehicle_guid"): cv.string,
        vol.Optional("device_id"): cv.string,
        vol.Required("category"): vol.In(_RECEIPT_CATEGORIES),
        vol.Required("amount"): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=MAX_CLAIM_AMOUNT)),
        vol.Required("receipt_files"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("confirm_declaration", default=False): cv.boolean,
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)

REGISTER_ODOMETER_SCHEMA = vol.Schema(
    {
        vol.Optional("vehicle_guid"): cv.string,
        vol.Optional("device_id"): cv.string,
        vol.Optional("odometer"): vol.Coerce(float),
        vol.Optional("odometer_entity_id"): cv.entity_id,
        vol.Optional("reading_date"): cv.date,
    }
)

REFRESH_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("tier", default=TIER_ALL): vol.In(
            (TIER_ALL, TIER_REIMBURSEMENTS, TIER_BALANCES, TIER_DETAILS)
        ),
    }
)


def _resolve_vehicle_target(
    hass: HomeAssistant,
    *,
    vehicle_guid: str | None = None,
    device_id: str | None = None,
) -> tuple[SgfleetDataUpdateCoordinator, str]:
    """Resolve (coordinator, vehicle_guid) from explicit args or single-vehicle default."""
    coordinators: list[SgfleetDataUpdateCoordinator] = list(hass.data.get(DOMAIN, {}).values())
    if not coordinators:
        raise HomeAssistantError("No SG Fleet accounts are currently configured.")

    if device_id and not vehicle_guid:
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get(device_id)
        if device:
            for ident_domain, ident_id in device.identifiers:
                if ident_domain == DOMAIN:
                    for suffix in ("_budgets", "_reimbursements"):
                        if ident_id.endswith(suffix):
                            ident_id = ident_id[: -len(suffix)]
                            break
                    vehicle_guid = ident_id
                    break

    all_pairs: list[tuple[SgfleetDataUpdateCoordinator, str]] = []
    for coord in coordinators:
        if coord.data and coord.data.vehicles:
            for vguid in coord.data.vehicles:
                all_pairs.append((coord, vguid))

    if vehicle_guid:
        for coord, vguid in all_pairs:
            if vguid.lower() == vehicle_guid.lower():
                return coord, vguid
        raise ServiceValidationError(f"Vehicle GUID '{vehicle_guid}' not found in configured SG Fleet accounts.")

    if len(all_pairs) == 1:
        return all_pairs[0]
    if not all_pairs:
        raise HomeAssistantError("No novated lease vehicles found in SG Fleet coordinator data.")
    raise ServiceValidationError(
        "Multiple SG Fleet vehicles are configured; please specify 'vehicle_guid' or 'device_id'."
    )


def _read_odometer_entity(hass: HomeAssistant, entity_id: str) -> float:
    """Read a numeric odometer reading from a Home Assistant sensor entity."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        raise ServiceValidationError(f"Odometer entity '{entity_id}' is unavailable or unknown.")
    try:
        return float(state.state)
    except ValueError as err:
        raise ServiceValidationError(
            f"Odometer entity '{entity_id}' state '{state.state}' is not a valid number."
        ) from err


async def async_register_services(hass: HomeAssistant) -> None:
    """Register SG Fleet domain services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SUBMIT_HOME_CHARGING_CLAIM):
        return

    async def handle_submit_home_charging_claim(call: ServiceCall) -> ServiceResponse:
        coord, vguid = _resolve_vehicle_target(
            hass,
            vehicle_guid=call.data.get("vehicle_guid"),
            device_id=call.data.get("device_id"),
        )
        dry_run: bool = call.data["dry_run"]
        confirmed: bool = call.data["confirm_ato_declaration"]
        if not dry_run and not confirmed:
            raise ServiceValidationError(
                "You must set 'confirm_ato_declaration: true' to submit an EV home charging claim "
                "(or set 'dry_run: true' to preview the calculation first)."
            )

        end_odometer = call.data.get("end_odometer")
        if end_odometer is None:
            from .sensor import _resolve_odometer_entity_id

            odo_entity = (
                call.data.get("odometer_entity_id")
                or _resolve_odometer_entity_id(hass, coord)
            )
            if not odo_entity:
                raise ServiceValidationError(
                    "Provide either 'end_odometer' or 'odometer_entity_id' "
                    "(or link your vehicle device / odometer sensor in SG Fleet integration options)."
                )
            end_odometer = _read_odometer_entity(hass, odo_entity)

        settings = coord.data.reimbursement_settings if coord.data else None
        try:
            result = await coord.api.submit_home_charging_claim(
                vehicle_guid=vguid,
                start_date=call.data["start_date"],
                end_date=call.data["end_date"],
                start_odometer=float(call.data["start_odometer"]),
                end_odometer=float(end_odometer),
                reimbursement_settings=settings,
                dry_run=dry_run,
            )
        except SgfleetValidationError as err:
            raise ServiceValidationError(str(err)) from err
        except SgfleetError as err:
            raise HomeAssistantError(f"SG Fleet home charging claim failed: {err}") from err

        if not dry_run:
            await coord.async_request_tier_refresh({TIER_REIMBURSEMENTS, TIER_BALANCES})
        return result

    async def handle_submit_reimbursement_claim(call: ServiceCall) -> ServiceResponse:
        coord, vguid = _resolve_vehicle_target(
            hass,
            vehicle_guid=call.data.get("vehicle_guid"),
            device_id=call.data.get("device_id"),
        )
        dry_run: bool = call.data["dry_run"]
        confirmed: bool = call.data["confirm_declaration"]
        if not dry_run and not confirmed:
            raise ServiceValidationError(
                "You must set 'confirm_declaration: true' to submit a reimbursement claim "
                "(or set 'dry_run: true' to validate files and preview first)."
            )

        category: str = call.data["category"]
        amount: float = round(float(call.data["amount"]), 2)
        file_paths: list[str] = call.data["receipt_files"]
        if not file_paths:
            raise ServiceValidationError("At least one receipt file path is required.")

        settings = (coord.data.reimbursement_settings if coord.data else {}) or {}
        max_files = int(settings.get("maxFilesPerClaim", 10))
        max_size_mb = int(settings.get("maxFileSizeMb", 30))
        allowed_exts = {ext.lower() for ext in (settings.get("allowedFileExtensions") or [])}

        if len(file_paths) > max_files:
            raise ServiceValidationError(f"Maximum {max_files} files allowed per reimbursement claim.")

        resolved_files: list[tuple[str, bytes]] = []
        for raw_path in file_paths:
            if not hass.config.is_allowed_path(raw_path):
                raise ServiceValidationError(
                    f"Path '{raw_path}' is not in Home Assistant's allowlist_external_dirs."
                )
            path = Path(raw_path)
            if not path.is_file():
                raise ServiceValidationError(f"Receipt file does not exist: {raw_path}")
            if allowed_exts and path.suffix.lower() not in allowed_exts:
                raise ServiceValidationError(
                    f"File extension '{path.suffix}' is not allowed by SG Fleet ({', '.join(sorted(allowed_exts))})."
                )
            data_bytes = await hass.async_add_executor_job(path.read_bytes)
            if len(data_bytes) > max_size_mb * 1024 * 1024:
                raise ServiceValidationError(f"File '{path.name}' exceeds SG Fleet limit of {max_size_mb} MB.")
            resolved_files.append((path.name, data_bytes))

        form_id = str(uuid.uuid4())
        today_iso = f"{date.today().isoformat()}T00:00:00.000Z"

        if dry_run:
            return {
                "dry_run": True,
                "vehicle_guid": vguid,
                "form_id": form_id,
                "category": category,
                "amount": amount,
                "files": [{"name": name, "size_bytes": len(b)} for name, b in resolved_files],
            }

        try:
            unique_file_names: list[str] = []
            for file_name, file_bytes in resolved_files:
                upload_resp = await coord.api.upload_reimbursement_file(
                    form_id=form_id,
                    category=category,
                    file_name=file_name,
                    file_bytes=file_bytes,
                )
                unique_name = (upload_resp or {}).get("uniqueFileName")
                if not unique_name:
                    raise HomeAssistantError(f"SG Fleet did not return a uniqueFileName for '{file_name}'.")
                unique_file_names.append(unique_name)

            expense = {
                "expenseId": str(uuid.uuid4()),
                "category": category,
                "uniqueFileNames": unique_file_names,
                "dateOfStartOdometerReading": today_iso,
                "dateOfEndOdometerReading": today_iso,
                "startOdometerReading": None,
                "endOdometerReading": None,
                "amount": amount,
            }
            response = await coord.api.submit_reimbursement(
                vehicle_guid=vguid,
                expenses=[expense],
                form_id=form_id,
                include_home_charging_declaration=False,
            )
        except SgfleetError as err:
            raise HomeAssistantError(f"SG Fleet reimbursement submission failed: {err}") from err

        await coord.async_request_tier_refresh({TIER_REIMBURSEMENTS, TIER_BALANCES})
        return {
            "dry_run": False,
            "vehicle_guid": vguid,
            "form_id": form_id,
            "uploaded_files": unique_file_names,
            "response": response,
        }

    async def handle_register_odometer(call: ServiceCall) -> ServiceResponse:
        coord, vguid = _resolve_vehicle_target(
            hass,
            vehicle_guid=call.data.get("vehicle_guid"),
            device_id=call.data.get("device_id"),
        )
        odometer = call.data.get("odometer")
        if odometer is None:
            odo_entity = (
                call.data.get("odometer_entity_id")
                or coord.config_entry.options.get(CONF_ODOMETER_ENTITY_ID)
            )
            if not odo_entity:
                raise ServiceValidationError("Provide either 'odometer' or 'odometer_entity_id'.")
            odometer = _read_odometer_entity(hass, odo_entity)

        reading_date: date = call.data.get("reading_date") or datetime.now().date()
        try:
            resp = await coord.api.register_odometer_reading(
                vehicle_guid=vguid,
                odometer=float(odometer),
                reading_date=reading_date,
            )
        except SgfleetValidationError as err:
            raise ServiceValidationError(str(err)) from err
        except SgfleetError as err:
            raise HomeAssistantError(f"Failed registering odometer reading: {err}") from err

        # Refresh just this vehicle's record immediately so updated odometer/details reflect right away
        await coord.async_refresh_vehicle(vguid)
        return {
            "vehicle_guid": vguid,
            "odometer": int(round(float(odometer))),
            "reading_date": reading_date.isoformat(),
            "response": resp,
        }

    async def handle_refresh_data(call: ServiceCall) -> None:
        tier_choice: str = call.data.get("tier", TIER_ALL)
        tiers = set(ALL_TIERS) if tier_choice == TIER_ALL else {tier_choice}
        for coord in list(hass.data.get(DOMAIN, {}).values()):
            await coord.async_request_tier_refresh(tiers)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SUBMIT_HOME_CHARGING_CLAIM,
        handle_submit_home_charging_claim,
        schema=SUBMIT_HOME_CHARGING_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SUBMIT_REIMBURSEMENT_CLAIM,
        handle_submit_reimbursement_claim,
        schema=SUBMIT_REIMBURSEMENT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REGISTER_ODOMETER,
        handle_register_odometer,
        schema=REGISTER_ODOMETER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_DATA,
        handle_refresh_data,
        schema=REFRESH_DATA_SCHEMA,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove SG Fleet services when the last config entry is unloaded."""
    for service_name in (
        SERVICE_SUBMIT_HOME_CHARGING_CLAIM,
        SERVICE_SUBMIT_REIMBURSEMENT_CLAIM,
        SERVICE_REGISTER_ODOMETER,
        SERVICE_REFRESH_DATA,
    ):
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)
