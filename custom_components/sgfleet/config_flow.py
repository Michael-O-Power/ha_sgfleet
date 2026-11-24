"""Config flow and options flow for SG Fleet Novated Leasing (mySG)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .api import (
    AuthTokens,
    SgfleetApiClient,
    SgfleetAuthError,
    SgfleetCaptchaRequiredError,
    SgfleetConnectionError,
    SgfleetMfaRequiredError,
    SgfleetValidationError,
    build_authorize_url,
    extract_code_from_callback,
    generate_pkce_pair,
)
from .const import (
    AUTH_METHOD_BROWSER_PKCE,
    AUTH_METHOD_CREDENTIALS,
    CONF_ACCESS_TOKEN,
    CONF_ACTIVE_HOURS_ENABLED,
    CONF_ACTIVE_HOURS_END,
    CONF_ACTIVE_HOURS_START,
    CONF_AUTH_METHOD,
    CONF_BALANCES_INTERVAL_HOURS,
    CONF_CALLBACK_URL,
    CONF_DETAILS_INTERVAL_HOURS,
    CONF_EMAIL,
    CONF_LINKED_VEHICLE_DEVICE_ID,
    CONF_ODOMETER_ENTITY_ID,
    CONF_PASSWORD,
    CONF_PERSON_ID,
    CONF_REFRESH_TOKEN,
    CONF_REGO_WARNING_DAYS,
    CONF_REIMBURSEMENTS_INTERVAL_HOURS,
    CONF_REIMBURSEMENTS_SMART_MODE,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_ACTIVE_HOURS_ENABLED,
    DEFAULT_ACTIVE_HOURS_END,
    DEFAULT_ACTIVE_HOURS_START,
    DEFAULT_BALANCES_INTERVAL_HOURS,
    DEFAULT_DETAILS_INTERVAL_HOURS,
    DEFAULT_REGO_WARNING_DAYS,
    DEFAULT_REIMBURSEMENTS_INTERVAL_HOURS,
    DEFAULT_REIMBURSEMENTS_SMART_MODE,
    DOMAIN,
    MAX_BALANCES_INTERVAL_HOURS,
    MAX_REIMBURSEMENTS_INTERVAL_HOURS,
    MIN_BALANCES_INTERVAL_HOURS,
    MIN_REIMBURSEMENTS_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)


class SgfleetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SG Fleet Novated Leasing."""

    VERSION = 1

    def __init__(self) -> None:
        self._pkce_verifier: str | None = None
        self._pkce_state: str | None = None
        self._pkce_auth_url: str | None = None
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SgfleetOptionsFlowHandler:
        """Create the options flow."""
        return SgfleetOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose how to authenticate with mySG."""
        if user_input is not None:
            method = user_input[CONF_AUTH_METHOD]
            if method == AUTH_METHOD_BROWSER_PKCE:
                return await self.async_step_browser_pkce()
            return await self.async_step_credentials()

        schema = vol.Schema(
            {
                vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_CREDENTIALS): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=AUTH_METHOD_CREDENTIALS,
                                label="Email & Password (Auth0 Universal Login)",
                            ),
                            SelectOptionDict(
                                value=AUTH_METHOD_BROWSER_PKCE,
                                label="Browser Sign-In (PKCE callback URL — recommended for MFA/SSO)",
                            ),
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate using SG Fleet email and password via Auth0 PKCE."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            client = SgfleetApiClient(session, email=email, password=password)

            try:
                tokens = await client.authenticate_with_credentials()
            except SgfleetCaptchaRequiredError:
                _LOGGER.info("SG Fleet Auth0 requested CAPTCHA; switching to browser PKCE step")
                return await self.async_step_browser_pkce(reason="captcha_required")
            except SgfleetMfaRequiredError:
                _LOGGER.info("SG Fleet Auth0 requested MFA; switching to browser PKCE step")
                return await self.async_step_browser_pkce(reason="mfa_required")
            except SgfleetAuthError as err:
                _LOGGER.warning("SG Fleet credential authentication failed: %s", err)
                errors["base"] = "invalid_auth"
            except SgfleetConnectionError as err:
                _LOGGER.warning("SG Fleet connection error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during SG Fleet authentication")
                errors["base"] = "unknown"
            else:
                return await self._finish_auth(
                    tokens=tokens,
                    auth_method=AUTH_METHOD_CREDENTIALS,
                    email=email,
                    password=password,
                )

        default_email = ""
        if self._reauth_entry is not None:
            default_email = self._reauth_entry.data.get(CONF_EMAIL, "")

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=default_email): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                ),
            }
        )
        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_browser_pkce(
        self,
        user_input: dict[str, Any] | None = None,
        *,
        reason: str | None = None,
    ) -> ConfigFlowResult:
        """Authenticate via browser PKCE by pasting the redirected callback URL or code."""
        errors: dict[str, str] = {}
        if reason:
            errors["base"] = reason

        if not self._pkce_verifier or not self._pkce_auth_url:
            verifier, challenge = generate_pkce_pair()
            auth_url, state = build_authorize_url(challenge)
            self._pkce_verifier = verifier
            self._pkce_state = state
            self._pkce_auth_url = auth_url

        if user_input is not None:
            raw_callback = user_input[CONF_CALLBACK_URL]
            session = async_get_clientsession(self.hass)
            client = SgfleetApiClient(session)
            try:
                code = extract_code_from_callback(raw_callback, expected_state=self._pkce_state)
                tokens = await client.exchange_authorization_code(code, self._pkce_verifier)
            except SgfleetValidationError as err:
                _LOGGER.warning("Invalid callback URL/code: %s", err)
                errors["base"] = "invalid_callback"
            except SgfleetAuthError as err:
                _LOGGER.warning("SG Fleet PKCE exchange failed: %s", err)
                errors["base"] = "invalid_auth"
            except SgfleetConnectionError as err:
                _LOGGER.warning("SG Fleet connection error during PKCE exchange: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during SG Fleet PKCE exchange")
                errors["base"] = "unknown"
            else:
                return await self._finish_auth(
                    tokens=tokens,
                    auth_method=AUTH_METHOD_BROWSER_PKCE,
                    email=tokens.email or "",
                    password=None,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_CALLBACK_URL): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=False)
                ),
            }
        )
        return self.async_show_form(
            step_id="browser_pkce",
            data_schema=schema,
            errors=errors,
            description_placeholders={"auth_url": self._pkce_auth_url},
        )

    async def _finish_auth(
        self,
        *,
        tokens: AuthTokens,
        auth_method: str,
        email: str,
        password: str | None,
    ) -> ConfigFlowResult:
        """Create or update the config entry after successful authentication."""
        unique_id = (tokens.person_id or email).lower()
        title = tokens.name or tokens.email or email or "SG Fleet"

        entry_data: dict[str, Any] = {
            CONF_AUTH_METHOD: auth_method,
            CONF_EMAIL: tokens.email or email,
            CONF_PERSON_ID: tokens.person_id,
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_TOKEN_EXPIRES_AT: tokens.access_token_expires_at,
        }
        if password is not None:
            entry_data[CONF_PASSWORD] = password

        if self._reauth_entry is not None:
            # Preserve existing options and update credentials/tokens
            self.hass.config_entries.async_update_entry(
                self._reauth_entry,
                data={**self._reauth_entry.data, **entry_data},
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title=f"SG Fleet ({title})", data=entry_data)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication triggered by ConfigEntryAuthFailed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt user to re-authenticate using preferred method."""
        if user_input is not None:
            method = user_input[CONF_AUTH_METHOD]
            if method == AUTH_METHOD_BROWSER_PKCE:
                return await self.async_step_browser_pkce()
            return await self.async_step_credentials()

        default_method = AUTH_METHOD_CREDENTIALS
        if self._reauth_entry is not None:
            default_method = self._reauth_entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_CREDENTIALS)

        schema = vol.Schema(
            {
                vol.Required(CONF_AUTH_METHOD, default=default_method): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=AUTH_METHOD_CREDENTIALS,
                                label="Email & Password (Auth0 Universal Login)",
                            ),
                            SelectOptionDict(
                                value=AUTH_METHOD_BROWSER_PKCE,
                                label="Browser Sign-In (PKCE callback URL)",
                            ),
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema)


class SgfleetOptionsFlowHandler(OptionsFlow):
    """Handle SG Fleet integration options."""

    def _suggest_vehicle_device_by_vin(self) -> str | None:
        """Auto-detect an existing HA car device whose VIN matches the leased vehicle."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if not coordinator or not coordinator.data:
            return None

        vins: set[str] = set()
        for vdata in coordinator.data.vehicles.values():
            vin = (vdata.vehicle.get("vehicleSpecification") or {}).get("chassisNumber")
            if vin:
                vins.add(str(vin).strip().upper())
        if not vins:
            return None

        dev_reg = dr.async_get(self.hass)
        for device in dev_reg.devices.values():
            if any(dom == DOMAIN for dom, _ in device.identifiers):
                continue
            if device.serial_number and str(device.serial_number).strip().upper() in vins:
                return device.id
            for _, ident_val in device.identifiers:
                if str(ident_val).strip().upper() in vins:
                    return device.id
        return None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage tiered polling cadences, active hours, rego alert threshold, and vehicle links."""
        if user_input is not None:
            cleaned = dict(user_input)
            if CONF_DETAILS_INTERVAL_HOURS in cleaned:
                cleaned[CONF_DETAILS_INTERVAL_HOURS] = int(cleaned[CONF_DETAILS_INTERVAL_HOURS])
            if not cleaned.get(CONF_ODOMETER_ENTITY_ID):
                cleaned.pop(CONF_ODOMETER_ENTITY_ID, None)
            if not cleaned.get(CONF_LINKED_VEHICLE_DEVICE_ID):
                cleaned.pop(CONF_LINKED_VEHICLE_DEVICE_ID, None)
            return self.async_create_entry(title="", data=cleaned)

        opts = self.config_entry.options
        current_details = str(int(opts.get(CONF_DETAILS_INTERVAL_HOURS, DEFAULT_DETAILS_INTERVAL_HOURS)))
        suggested_linked_device = (
            opts.get(CONF_LINKED_VEHICLE_DEVICE_ID)
            or self._suggest_vehicle_device_by_vin()
        )

        schema_fields: dict[Any, Any] = {
            vol.Required(
                CONF_REIMBURSEMENTS_INTERVAL_HOURS,
                default=opts.get(
                    CONF_REIMBURSEMENTS_INTERVAL_HOURS,
                    DEFAULT_REIMBURSEMENTS_INTERVAL_HOURS,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_REIMBURSEMENTS_INTERVAL_HOURS,
                    max=MAX_REIMBURSEMENTS_INTERVAL_HOURS,
                    step=1,
                    unit_of_measurement="h",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_REIMBURSEMENTS_SMART_MODE,
                default=opts.get(
                    CONF_REIMBURSEMENTS_SMART_MODE,
                    DEFAULT_REIMBURSEMENTS_SMART_MODE,
                ),
            ): BooleanSelector(),
            vol.Required(
                CONF_BALANCES_INTERVAL_HOURS,
                default=opts.get(
                    CONF_BALANCES_INTERVAL_HOURS,
                    DEFAULT_BALANCES_INTERVAL_HOURS,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_BALANCES_INTERVAL_HOURS,
                    max=MAX_BALANCES_INTERVAL_HOURS,
                    step=1,
                    unit_of_measurement="h",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_DETAILS_INTERVAL_HOURS,
                default=current_details,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value="0", label="Never (startup & manual refresh only)"),
                        SelectOptionDict(value="24", label="Daily (24 hours)"),
                        SelectOptionDict(value="72", label="Every 3 days (72 hours)"),
                        SelectOptionDict(value="168", label="Weekly (7 days)"),
                        SelectOptionDict(value="336", label="Fortnightly (14 days)"),
                        SelectOptionDict(value="720", label="Monthly (30 days)"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_ACTIVE_HOURS_ENABLED,
                default=opts.get(CONF_ACTIVE_HOURS_ENABLED, DEFAULT_ACTIVE_HOURS_ENABLED),
            ): BooleanSelector(),
            vol.Required(
                CONF_ACTIVE_HOURS_START,
                default=opts.get(CONF_ACTIVE_HOURS_START, DEFAULT_ACTIVE_HOURS_START),
            ): TimeSelector(),
            vol.Required(
                CONF_ACTIVE_HOURS_END,
                default=opts.get(CONF_ACTIVE_HOURS_END, DEFAULT_ACTIVE_HOURS_END),
            ): TimeSelector(),
            vol.Required(
                CONF_REGO_WARNING_DAYS,
                default=opts.get(CONF_REGO_WARNING_DAYS, DEFAULT_REGO_WARNING_DAYS),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=180,
                    step=1,
                    unit_of_measurement="days",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_LINKED_VEHICLE_DEVICE_ID,
                description={"suggested_value": suggested_linked_device},
            ): DeviceSelector(),
            vol.Optional(
                CONF_ODOMETER_ENTITY_ID,
                description={"suggested_value": opts.get(CONF_ODOMETER_ENTITY_ID)},
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor")
            ),
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
        )
