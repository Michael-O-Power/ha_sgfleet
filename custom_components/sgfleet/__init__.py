"""The SG Fleet Novated Leasing (mySG) integration."""

from __future__ import annotations

import logging
from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuthTokens, SgfleetApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_LINKED_VEHICLE_DEVICE_ID,
    CONF_PASSWORD,
    CONF_PERSON_ID,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
)
from .coordinator import SgfleetDataUpdateCoordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.BUTTON,
    Platform.TODO,
]

SgfleetConfigEntry: TypeAlias = ConfigEntry[SgfleetDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SgfleetConfigEntry) -> bool:
    """Set up SG Fleet Novated Leasing from a config entry."""
    session = async_get_clientsession(hass)

    def _persist_tokens(tokens: AuthTokens) -> None:
        """Persist rotated refresh/access tokens back to the config entry."""
        new_data = dict(entry.data)
        changed = False
        updates = {
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_TOKEN_EXPIRES_AT: tokens.access_token_expires_at,
            CONF_PERSON_ID: tokens.person_id or new_data.get(CONF_PERSON_ID),
        }
        for key, val in updates.items():
            if val is not None and new_data.get(key) != val:
                new_data[key] = val
                changed = True
        if changed:
            hass.config_entries.async_update_entry(entry, data=new_data)

    api = SgfleetApiClient(
        session,
        email=entry.data.get(CONF_EMAIL),
        password=entry.data.get(CONF_PASSWORD),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        access_token_expires_at=float(entry.data.get(CONF_TOKEN_EXPIRES_AT, 0.0) or 0.0),
        token_update_callback=_persist_tokens,
    )
    if entry.data.get(CONF_PERSON_ID):
        api.tokens.person_id = entry.data[CONF_PERSON_ID]

    coordinator = SgfleetDataUpdateCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_sync_linked_vehicle_devices(hass, entry, coordinator)
    await async_register_services(hass)

    # Snapshot current options so background token rotations (entry.data updates)
    # do NOT trigger an integration reload and cancel in-flight API requests.
    last_options = dict(entry.options)

    async def _async_options_updated(
        hass_inner: HomeAssistant, updated_entry: SgfleetConfigEntry
    ) -> None:
        nonlocal last_options
        new_options = dict(updated_entry.options)
        if new_options != last_options:
            last_options = new_options
            await hass_inner.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _async_sync_linked_vehicle_devices(
    hass: HomeAssistant,
    entry: SgfleetConfigEntry,
    coordinator: SgfleetDataUpdateCoordinator,
) -> None:
    """Sync via_device_id on SG Fleet lease devices to match the linked car CI option."""
    if not coordinator.data:
        return

    dev_reg = dr.async_get(hass)
    raw_linked_id = entry.options.get(CONF_LINKED_VEHICLE_DEVICE_ID)
    target_device = dev_reg.async_get(raw_linked_id) if raw_linked_id else None

    for vehicle_guid in coordinator.data.vehicles:
        sg_device = dev_reg.async_get_device(identifiers={(DOMAIN, vehicle_guid)})
        if sg_device is None:
            continue

        desired_via_id: str | None = None
        if target_device is not None and target_device.id != sg_device.id:
            # Avoid circular links to our own child devices
            is_own_device = any(
                dom == DOMAIN and ident.startswith(vehicle_guid)
                for dom, ident in target_device.identifiers
            )
            if not is_own_device:
                desired_via_id = target_device.id

        if sg_device.via_device_id != desired_via_id:
            dev_reg.async_update_device(sg_device.id, via_device_id=desired_via_id)


async def async_unload_entry(hass: HomeAssistant, entry: SgfleetConfigEntry) -> bool:
    """Unload an SG Fleet config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_entries = hass.data.get(DOMAIN, {})
        domain_entries.pop(entry.entry_id, None)
        if not domain_entries:
            async_unregister_services(hass)
    return unload_ok
