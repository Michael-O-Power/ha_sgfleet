"""Diagnostics support for SG Fleet Novated Leasing (mySG)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SgfleetConfigEntry
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PERSON_ID,
    CONF_REFRESH_TOKEN,
)

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_PERSON_ID,
    "personId",
    "firstName",
    "middleName",
    "lastName",
    "fullName",
    "salutation",
    "birthDate",
    "primaryEmail",
    "secondaryEmail",
    "email",
    "name",
    "contactNumber",
    "phoneNumbers",
    "addresses",
    "line1",
    "line2",
    "line3",
    "line4",
    "city",
    "postcode",
    "employeeNumber",
    "registration",
    "chassisNumber",
    "engineNumber",
    "bankAccount",
    "accountName",
    "accountNumber",
    "bsb",
    "driverGuid",
    "sessionId",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SgfleetConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for an SG Fleet config entry."""
    coordinator = entry.runtime_data
    data_dict = asdict(coordinator.data) if coordinator.data else {}
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "polling": {
            "config": asdict(coordinator.polling_config),
            "tier_last_updated_utc": coordinator.tier_last_updated_utc,
            "next_update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        },
        "coordinator_last_update_success": coordinator.last_update_success,
        "data": async_redact_data(data_dict, TO_REDACT),
    }
