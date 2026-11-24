"""Tiered DataUpdateCoordinator for SG Fleet Novated Leasing (mySG)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    SgfleetApiClient,
    SgfleetAuthError,
    SgfleetConnectionError,
    SgfleetError,
)
from .const import (
    ALL_TIERS,
    DOMAIN,
    TIER_BALANCES,
    TIER_DETAILS,
    TIER_REIMBURSEMENTS,
)
from .scheduling import (
    PollingConfig,
    compute_due_tiers,
    compute_next_update_interval,
    has_pending_reimbursements,
    is_within_active_hours,
    seconds_until_next_active_window,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SgfleetVehicleData:
    """Aggregated data for a single SG Fleet novated lease vehicle."""

    vehicle_guid: str
    contract_guid: str | None
    vehicle: dict[str, Any] = field(default_factory=dict)
    balance: dict[str, Any] = field(default_factory=dict)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    contract: dict[str, Any] = field(default_factory=dict)
    contract_version: dict[str, Any] = field(default_factory=dict)
    contract_services: list[dict[str, Any]] = field(default_factory=list)
    registration_details: dict[str, Any] = field(default_factory=dict)
    reimbursement_categories: list[dict[str, Any]] = field(default_factory=list)
    reimbursement_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def registration(self) -> str:
        """Return vehicle registration plate."""
        return (
            self.vehicle.get("registration")
            or (self.balance.get("vehicleSummary") or {}).get("registration")
            or self.vehicle_guid[:8]
        )

    @property
    def display_name(self) -> str:
        """Return human-friendly device name."""
        year = self.vehicle.get("modelYear") or (self.balance.get("vehicleSummary") or {}).get("year") or ""
        make = self.vehicle.get("make") or (self.balance.get("vehicleSummary") or {}).get("make") or ""
        model = self.vehicle.get("model") or (self.balance.get("vehicleSummary") or {}).get("model") or ""
        base = " ".join(str(p).strip() for p in (year, make, model) if p).strip()
        rego = self.registration
        if base and rego:
            return f"{base} ({rego})"
        return base or f"SG Fleet Vehicle {rego}"


@dataclass(slots=True)
class SgfleetCoordinatorData:
    """Top-level coordinator snapshot for an SG Fleet account."""

    person_id: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    employer: dict[str, Any] = field(default_factory=dict)
    has_valid_bank_account: bool | None = None
    data_load_status: dict[str, Any] = field(default_factory=dict)
    reimbursement_settings: dict[str, Any] = field(default_factory=dict)
    vehicles: dict[str, SgfleetVehicleData] = field(default_factory=dict)
    tier_last_updated_utc: dict[str, str] = field(default_factory=dict)


class SgfleetDataUpdateCoordinator(DataUpdateCoordinator[SgfleetCoordinatorData]):
    """Coordinator managing tiered, jittered, active-hours-aware polling of SG Fleet."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: SgfleetApiClient,
    ) -> None:
        self.api = api
        self.polling_config = PollingConfig.from_options(dict(entry.options))
        self._tier_last_fetched: dict[str, float] = {}
        self._tier_last_updated_utc: dict[str, str] = {}
        self._forced_tiers: set[str] = set()

        initial_interval = timedelta(hours=self.polling_config.reimbursements_hours)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            config_entry=entry,
            update_interval=initial_interval,
        )

    @property
    def tier_last_updated_utc(self) -> dict[str, str]:
        """Return UTC ISO timestamps of the last update per tier."""
        return dict(self._tier_last_updated_utc)

    def _has_pending_claims(self, data: SgfleetCoordinatorData | None) -> bool:
        if not data or not data.vehicles:
            return False
        return has_pending_reimbursements(
            [v.reimbursement_history for v in data.vehicles.values()]
        )

    async def async_request_tier_refresh(self, tiers: set[str] | None = None) -> None:
        """Force an immediate refresh of specific tier(s), bypassing quiet hours."""
        requested = set(ALL_TIERS) if not tiers else (set(tiers) & set(ALL_TIERS))
        if not requested:
            requested = set(ALL_TIERS)
        self._forced_tiers.update(requested)
        await self.async_refresh()

    async def async_refresh_vehicle(self, vehicle_guid: str) -> None:
        """Immediately refresh a single vehicle's details (e.g., after odometer update)."""
        try:
            await self.api.ensure_session()
            updated_vehicle = await self.api.get_vehicle(vehicle_guid)
        except SgfleetAuthError as err:
            raise ConfigEntryAuthFailed(f"SG Fleet authentication expired or invalid: {err}") from err
        except (SgfleetConnectionError, SgfleetError) as err:
            _LOGGER.warning("Failed refreshing vehicle %s after update: %s", vehicle_guid, err)
            return

        if self.data and vehicle_guid in self.data.vehicles and isinstance(updated_vehicle, dict):
            existing = self.data.vehicles[vehicle_guid]
            existing.vehicle = updated_vehicle
            now_utc = datetime.now(timezone.utc).isoformat()
            self._tier_last_updated_utc["vehicle_single"] = now_utc
            self.data.tier_last_updated_utc = dict(self._tier_last_updated_utc)
            self.async_set_updated_data(self.data)

    async def _fetch_vehicle_tiers(
        self,
        vehicle_summary: dict[str, Any] | None,
        existing: SgfleetVehicleData | None,
        tiers: set[str],
    ) -> SgfleetVehicleData:
        """Fetch only the requested tiers for a single novated lease vehicle."""
        vehicle_guid = str(
            (vehicle_summary or {}).get("vehicleGuid")
            or (existing.vehicle_guid if existing else "")
        )
        contract_guid = (
            (vehicle_summary or {}).get("contractGuid")
            or (existing.contract_guid if existing else None)
        )

        tasks: dict[str, Any] = {}
        if TIER_DETAILS in tiers:
            tasks["vehicle"] = self.api.get_vehicle(vehicle_guid)
            tasks["reimbursement_categories"] = self.api.get_reimbursement_categories(vehicle_guid)
            if contract_guid:
                tasks["contract"] = self.api.get_contract(contract_guid)
                tasks["contract_version"] = self.api.get_contract_current_version(contract_guid)
                tasks["contract_services"] = self.api.get_contract_services(contract_guid)
                tasks["registration_details"] = self.api.get_registration_details(contract_guid)

        if TIER_BALANCES in tiers:
            tasks["balance"] = self.api.get_balance(vehicle_guid)
            tasks["transactions"] = self.api.get_balance_transactions(vehicle_guid)

        if TIER_REIMBURSEMENTS in tiers:
            tasks["reimbursement_history"] = self.api.get_reimbursement_history(vehicle_guid)

        keys = list(tasks.keys())
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        parsed: dict[str, Any] = {}
        for key, res in zip(keys, results, strict=True):
            if isinstance(res, SgfleetAuthError):
                raise res
            if isinstance(res, Exception):
                _LOGGER.warning(
                    "SG Fleet partial fetch failure for %s (%s): %s",
                    key,
                    vehicle_guid,
                    res,
                )
                parsed[key] = None
            else:
                parsed[key] = res

        prev_vehicle = existing.vehicle if existing else {}
        vehicle_detail = (
            parsed["vehicle"]
            if isinstance(parsed.get("vehicle"), dict)
            else (vehicle_summary if isinstance(vehicle_summary, dict) and TIER_DETAILS in tiers else prev_vehicle)
        )

        return SgfleetVehicleData(
            vehicle_guid=vehicle_guid,
            contract_guid=contract_guid or vehicle_detail.get("contractGuid"),
            vehicle=vehicle_detail,
            balance=(
                parsed["balance"]
                if isinstance(parsed.get("balance"), dict)
                else (existing.balance if existing else {})
            ),
            transactions=(
                parsed["transactions"]
                if isinstance(parsed.get("transactions"), list)
                else (existing.transactions if existing else [])
            ),
            contract=(
                parsed["contract"]
                if isinstance(parsed.get("contract"), dict)
                else (existing.contract if existing else {})
            ),
            contract_version=(
                parsed["contract_version"]
                if isinstance(parsed.get("contract_version"), dict)
                else (existing.contract_version if existing else {})
            ),
            contract_services=(
                parsed["contract_services"]
                if isinstance(parsed.get("contract_services"), list)
                else (existing.contract_services if existing else [])
            ),
            registration_details=(
                parsed["registration_details"]
                if isinstance(parsed.get("registration_details"), dict)
                else (existing.registration_details if existing else {})
            ),
            reimbursement_categories=(
                parsed["reimbursement_categories"]
                if isinstance(parsed.get("reimbursement_categories"), list)
                else (existing.reimbursement_categories if existing else [])
            ),
            reimbursement_history=(
                parsed["reimbursement_history"]
                if isinstance(parsed.get("reimbursement_history"), list)
                else (existing.reimbursement_history if existing else [])
            ),
        )

    async def _async_update_data(self) -> SgfleetCoordinatorData:
        """Fetch only due SG Fleet data tiers while respecting active hours and jitter."""
        now_local = dt_util.now()
        now_mono = time.monotonic()
        is_initial = self.data is None

        forced = set(self._forced_tiers)
        self._forced_tiers.clear()

        # Quiet hours check for scheduled polls (initial startup and explicit refreshes bypass)
        if not is_initial and not forced and not is_within_active_hours(now_local, self.polling_config):
            wait_secs = seconds_until_next_active_window(now_local, self.polling_config)
            self.update_interval = timedelta(seconds=round(wait_secs))
            _LOGGER.debug(
                "SG Fleet scheduled poll deferred outside active hours; next poll in %s",
                self.update_interval,
            )
            return self.data  # type: ignore[return-value]

        pending_before = self._has_pending_claims(self.data)
        if is_initial:
            tiers_to_fetch = set(ALL_TIERS)
        else:
            due = compute_due_tiers(
                now_mono,
                self._tier_last_fetched,
                self.polling_config,
                has_pending_claims=pending_before,
            )
            tiers_to_fetch = due | forced

        if not tiers_to_fetch and self.data is not None:
            self.update_interval = compute_next_update_interval(
                now_local,
                now_mono,
                self._tier_last_fetched,
                self.polling_config,
                has_pending_claims=pending_before,
            )
            _LOGGER.debug(
                "No SG Fleet tiers due; skipping API calls. Next poll in %s",
                self.update_interval,
            )
            return self.data

        _LOGGER.debug("SG Fleet polling tiers: %s", sorted(tiers_to_fetch))

        try:
            await self.api.ensure_session()
            person_id = self.api.person_id or (self.data.person_id if self.data else None)

            account_tasks: dict[str, Any] = {}
            if TIER_DETAILS in tiers_to_fetch or is_initial or not (self.data and self.data.vehicles):
                account_tasks["vehicles"] = self.api.get_novated_vehicles()
                account_tasks["reimbursement_settings"] = self.api.get_reimbursement_settings()
                if person_id:
                    account_tasks["profile"] = self.api.get_person_profile(person_id)
                    account_tasks["employer"] = self.api.get_employer(person_id)
                    account_tasks["has_valid_bank_account"] = self.api.get_has_valid_bank_account(person_id)

            if TIER_BALANCES in tiers_to_fetch:
                account_tasks["data_load_status"] = self.api.get_data_load_status()

            keys = list(account_tasks.keys())
            results = await asyncio.gather(*account_tasks.values(), return_exceptions=True)

            acct: dict[str, Any] = {}
            for key, res in zip(keys, results, strict=True):
                if isinstance(res, SgfleetAuthError):
                    raise res
                if isinstance(res, Exception):
                    if key == "vehicles" and is_initial:
                        raise res
                    _LOGGER.warning("SG Fleet account fetch partial failure (%s): %s", key, res)
                    acct[key] = None
                else:
                    acct[key] = res

            existing_vehicles = self.data.vehicles if self.data else {}
            raw_vehicles: list[dict[str, Any]] | None = (
                acct["vehicles"] if isinstance(acct.get("vehicles"), list) else None
            )

            if raw_vehicles is not None:
                vehicle_bundles = await asyncio.gather(
                    *(
                        self._fetch_vehicle_tiers(
                            v,
                            existing_vehicles.get(str(v["vehicleGuid"])),
                            tiers_to_fetch,
                        )
                        for v in raw_vehicles
                        if isinstance(v, dict) and v.get("vehicleGuid")
                    )
                )
            else:
                vehicle_bundles = await asyncio.gather(
                    *(
                        self._fetch_vehicle_tiers(None, existing_v, tiers_to_fetch)
                        for existing_v in existing_vehicles.values()
                    )
                )

            vehicles_map = {vb.vehicle_guid: vb for vb in vehicle_bundles}

            now_utc = datetime.now(timezone.utc).isoformat()
            finish_mono = time.monotonic()
            for tier in tiers_to_fetch:
                self._tier_last_fetched[tier] = finish_mono
                self._tier_last_updated_utc[tier] = now_utc

            new_data = SgfleetCoordinatorData(
                person_id=person_id,
                profile=(
                    acct["profile"]
                    if isinstance(acct.get("profile"), dict)
                    else (self.data.profile if self.data else {})
                ),
                employer=(
                    acct["employer"]
                    if isinstance(acct.get("employer"), dict)
                    else (self.data.employer if self.data else {})
                ),
                has_valid_bank_account=(
                    acct["has_valid_bank_account"]
                    if acct.get("has_valid_bank_account") is not None
                    else (self.data.has_valid_bank_account if self.data else None)
                ),
                data_load_status=(
                    acct["data_load_status"]
                    if isinstance(acct.get("data_load_status"), dict)
                    else (self.data.data_load_status if self.data else {})
                ),
                reimbursement_settings=(
                    acct["reimbursement_settings"]
                    if isinstance(acct.get("reimbursement_settings"), dict)
                    else (self.data.reimbursement_settings if self.data else {})
                ),
                vehicles=vehicles_map,
                tier_last_updated_utc=dict(self._tier_last_updated_utc),
            )

            pending_after = self._has_pending_claims(new_data)
            self.update_interval = compute_next_update_interval(
                dt_util.now(),
                finish_mono,
                self._tier_last_fetched,
                self.polling_config,
                has_pending_claims=pending_after,
            )
            _LOGGER.debug(
                "SG Fleet tiers %s updated (pending_claims=%s); next poll in %s",
                sorted(tiers_to_fetch),
                pending_after,
                self.update_interval,
            )
            return new_data

        except SgfleetAuthError as err:
            raise ConfigEntryAuthFailed(f"SG Fleet authentication expired or invalid: {err}") from err
        except (SgfleetConnectionError, SgfleetError) as err:
            raise UpdateFailed(f"Error communicating with SG Fleet API: {err}") from err
