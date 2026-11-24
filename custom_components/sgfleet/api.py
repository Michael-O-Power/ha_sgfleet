"""Async API client for SG Fleet Novated Leasing (mySG)."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import logging
import mimetypes
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import uuid

import aiohttp

from .const import (
    API_CONTRACT_BASE,
    API_PEOPLE_BASE,
    API_SALPACK_BASE,
    API_VEHICLE_BASE,
    AUTH0_APP_ENV,
    AUTH0_AUDIENCE,
    AUTH0_AUTHORIZE_URL,
    AUTH0_CLIENT_ID,
    AUTH0_DOMAIN,
    AUTH0_REDIRECT_URI,
    AUTH0_SCOPE,
    AUTH0_TOKEN_URL,
    CATEGORY_HOME_CHARGING,
    CLAIM_EMAIL,
    CLAIM_EMPLOYER_ID,
    CLAIM_EMPLOYER_NAME,
    CLAIM_EXP,
    CLAIM_NAME,
    CLAIM_PERSON_UID,
    DEFAULT_TRANSACTIONS_PAGE_SIZE,
    GENERAL_DECLARATION_TEXT,
    HOME_CHARGING_DECLARATION_TEXT,
    MAX_CLAIM_AMOUNT,
    SESSION_REFRESH_BUFFER_SECS,
    SESSION_TIMEOUT_MINS,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "Accept": "application/json, image/png, image/*",
    "Content-Type": "application/json",
}


class SgfleetError(Exception):
    """Base exception for SG Fleet API errors."""


class SgfleetAuthError(SgfleetError):
    """Authentication or authorization error."""


class SgfleetCaptchaRequiredError(SgfleetAuthError):
    """Raised when Auth0 requires an interactive CAPTCHA challenge."""


class SgfleetMfaRequiredError(SgfleetAuthError):
    """Raised when Auth0 requires multi-factor authentication."""


class SgfleetConnectionError(SgfleetError):
    """Network or connectivity error communicating with SG Fleet."""


class SgfleetValidationError(SgfleetError):
    """Client-side validation error before submitting a request."""


@dataclass(slots=True)
class AuthTokens:
    """Container for Auth0 and SG Fleet session tokens."""

    access_token: str | None = None
    refresh_token: str | None = None
    access_token_expires_at: float = 0.0
    session_id: str | None = None
    session_expires_at: float = 0.0
    person_id: str | None = None
    email: str | None = None
    name: str | None = None
    employer_name: str | None = None
    employer_id: str | None = None


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE (code_verifier, code_challenge) S256 pair."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    code_challenge: str,
    state: str | None = None,
    scope: str = AUTH0_SCOPE,
) -> tuple[str, str]:
    """Build the Auth0 PKCE authorization URL and return (url, state)."""
    state_val = state or secrets.token_urlsafe(24)
    params = {
        "client_id": AUTH0_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": AUTH0_REDIRECT_URI,
        "scope": scope,
        "audience": AUTH0_AUDIENCE,
        "ext-env": AUTH0_APP_ENV,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state_val,
    }
    return f"{AUTH0_AUTHORIZE_URL}?{urlencode(params)}", state_val


def extract_code_from_callback(callback_input: str, expected_state: str | None = None) -> str:
    """Extract the OAuth2 authorization code from a redirected URL or raw code string."""
    raw = callback_input.strip()
    if not raw:
        raise SgfleetValidationError("Authorization callback URL or code is empty.")

    if "://" in raw or raw.startswith("?") or "code=" in raw:
        parsed = urlparse(raw if "://" in raw else f"https://placeholder/{raw.lstrip('/')}")
        params = parse_qs(parsed.query)
        if "error" in params:
            desc = params.get("error_description", params["error"])[0]
            raise SgfleetAuthError(f"Auth0 returned error: {desc}")
        if expected_state and "state" in params and params["state"][0] != expected_state:
            raise SgfleetAuthError("OAuth state parameter mismatch.")
        codes = params.get("code")
        if not codes or not codes[0]:
            raise SgfleetValidationError("No 'code' parameter found in the callback URL.")
        return codes[0]

    return raw


def build_terms_and_conditions_md(include_home_charging: bool) -> str:
    """Build the exact termsAndConditionsMd payload used by mySG."""
    if include_home_charging:
        return (
            f"Home Charging Declaration: \n{HOME_CHARGING_DECLARATION_TEXT}\n\n"
            f"General Declaration: \n{GENERAL_DECLARATION_TEXT}"
        )
    return f"General Declaration: \n{GENERAL_DECLARATION_TEXT}"


def calculate_home_charging_claim(
    start_date: date,
    end_date: date,
    start_odometer: float,
    end_odometer: float,
    reimbursement_settings: dict[str, Any],
) -> dict[str, Any]:
    """Validate and calculate an EV home charging claim using ATO PCG & SG Fleet rules."""
    if end_date <= start_date:
        raise SgfleetValidationError("End date must be after start date.")
    if start_odometer <= 0 or end_odometer <= 0:
        raise SgfleetValidationError("Odometer readings must be positive numbers.")
    if end_odometer <= start_odometer:
        raise SgfleetValidationError("End odometer reading must be greater than start odometer reading.")

    fbt_cutoff_raw = reimbursement_settings.get("fbtCutoffDate")
    current_rate = float(reimbursement_settings.get("currentHomeChargingRate", 0.0547))
    previous_rate = float(reimbursement_settings.get("previousHomeChargingRate", 0.042))

    rate = current_rate
    if fbt_cutoff_raw:
        cutoff_dt = datetime.fromisoformat(str(fbt_cutoff_raw).replace("Z", "+00:00")).date()
        start_before = start_date < cutoff_dt
        end_before = end_date < cutoff_dt
        if start_before != end_before:
            raise SgfleetValidationError(
                f"Start date ({start_date.isoformat()}) and end date ({end_date.isoformat()}) "
                f"must fall within a single FBT year (cutoff: {cutoff_dt.isoformat()})."
            )
        rate = previous_rate if start_before else current_rate

    distance_km = end_odometer - start_odometer
    amount = round(distance_km * rate, 2)
    if amount <= 0 or amount > MAX_CLAIM_AMOUNT:
        raise SgfleetValidationError(
            f"Calculated claim amount (${amount:.2f}) must be between $0.01 and ${MAX_CLAIM_AMOUNT:,.2f}."
        )

    return {
        "distance_km": round(distance_km, 2),
        "rate_per_km": rate,
        "amount": amount,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "start_odometer": start_odometer,
        "end_odometer": end_odometer,
    }


def _extract_hidden_state(html: str) -> str | None:
    """Extract the Auth0 hidden `state` input value from a Universal Login form."""
    match = re.search(
        r'<input[^>]*name=["\']state["\'][^>]*value=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    match = re.search(
        r'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']state["\']',
        html,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _has_captcha_challenge(html: str) -> bool:
    """Detect whether Auth0 Universal Login has rendered a CAPTCHA challenge."""
    lower = html.lower()
    return any(
        marker in lower
        for marker in (
            "g-recaptcha",
            "h-captcha",
            "cf-turnstile",
            "arkose",
            "captcha-container",
            'name="captcha"',
        )
    )


class SgfleetApiClient:
    """Async client for SG Fleet mySG REST APIs and Auth0 session lifecycle."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str | None = None,
        password: str | None = None,
        refresh_token: str | None = None,
        access_token: str | None = None,
        access_token_expires_at: float = 0.0,
        token_update_callback: Callable[[AuthTokens], None] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token_update_callback = token_update_callback
        self._auth_lock = asyncio.Lock()
        self.tokens = AuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at=access_token_expires_at,
            email=email,
        )

    @property
    def person_id(self) -> str | None:
        """Return the authenticated SG Fleet person GUID."""
        return self.tokens.person_id

    def _notify_token_update(self) -> None:
        if self._token_update_callback is not None:
            self._token_update_callback(self.tokens)

    # ------------------------------------------------------------------
    # Authentication: Auth0 PKCE + SG Fleet web-token session exchange
    # ------------------------------------------------------------------

    async def exchange_authorization_code(self, code: str, code_verifier: str) -> AuthTokens:
        """Exchange an Auth0 PKCE authorization code for Auth0 tokens and a web-token session."""
        payload = {
            "grant_type": "authorization_code",
            "client_id": AUTH0_CLIENT_ID,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": AUTH0_REDIRECT_URI,
        }
        await self._post_auth0_token(payload)
        await self.exchange_web_token()
        self._notify_token_update()
        return self.tokens

    async def refresh_auth0_token(self) -> AuthTokens:
        """Refresh the Auth0 access token using the stored refresh_token."""
        if not self.tokens.refresh_token:
            raise SgfleetAuthError("No refresh token available.")

        payload = {
            "grant_type": "refresh_token",
            "client_id": AUTH0_CLIENT_ID,
            "refresh_token": self.tokens.refresh_token,
        }
        await self._post_auth0_token(payload)
        self._notify_token_update()
        return self.tokens

    async def authenticate_with_credentials(
        self,
        email: str | None = None,
        password: str | None = None,
    ) -> AuthTokens:
        """Authenticate via Auth0 Universal Login (identifier-first) with PKCE.

        Note: If Auth0 presents a CAPTCHA or MFA challenge, this method does not
        attempt to bypass it; it raises SgfleetCaptchaRequiredError or
        SgfleetMfaRequiredError so the user can authenticate interactively.
        """
        user_email = email or self._email
        user_password = password or self._password
        if not user_email or not user_password:
            raise SgfleetAuthError("Email and password are required for credential authentication.")

        self._email = user_email
        self._password = user_password

        verifier, challenge = generate_pkce_pair()
        authorize_url, expected_state = build_authorize_url(challenge)

        # Use an isolated cookie jar for the Auth0 Universal Login handshake
        jar = aiohttp.CookieJar()
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {
            "User-Agent": "Mozilla/5.0 (HomeAssistant; SG Fleet Integration)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout, headers=headers) as login_sess:
                # Step 1: /authorize -> /u/login/identifier?state=...
                async with login_sess.get(authorize_url, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        raise SgfleetAuthError(f"Auth0 authorize failed with HTTP {resp.status}")
                    id_url = str(resp.url)
                    id_html = await resp.text()

                if _has_captcha_challenge(id_html):
                    raise SgfleetCaptchaRequiredError(
                        "Auth0 requested a CAPTCHA challenge. Please use the Browser Sign-In option."
                    )

                state_token = _extract_hidden_state(id_html) or parse_qs(urlparse(id_url).query).get("state", [None])[0]
                if not state_token:
                    raise SgfleetAuthError("Unable to locate Auth0 login state token on identifier page.")

                # Step 2: POST /u/login/identifier
                form_id = {
                    "state": state_token,
                    "username": user_email,
                    "js-available": "true",
                    "webauthn-available": "false",
                    "is-brave": "false",
                    "webauthn-platform-available": "false",
                    "action": "default",
                }
                async with login_sess.post(id_url, data=form_id, allow_redirects=True) as resp:
                    pw_url = str(resp.url)
                    pw_html = await resp.text()

                if _has_captcha_challenge(pw_html):
                    raise SgfleetCaptchaRequiredError(
                        "Auth0 requested a CAPTCHA challenge. Please use the Browser Sign-In option."
                    )
                if "/u/login/password" not in pw_url and "password" not in pw_html.lower():
                    raise SgfleetAuthError(
                        "Email was not accepted by Auth0 or requires SSO/interactive login."
                    )

                pw_state = _extract_hidden_state(pw_html) or parse_qs(urlparse(pw_url).query).get("state", [state_token])[0]

                # Step 3: POST /u/login/password and follow redirects manually until mysg.sgfleet.com?code=...
                form_pw = {
                    "state": pw_state,
                    "username": user_email,
                    "password": user_password,
                    "action": "default",
                }
                next_url: str | None = pw_url
                code: str | None = None

                async with login_sess.post(pw_url, data=form_pw, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        next_url = urljoin(pw_url, resp.headers["Location"])
                    else:
                        body = await resp.text()
                        if _has_captcha_challenge(body):
                            raise SgfleetCaptchaRequiredError(
                                "Auth0 requested a CAPTCHA challenge. Please use the Browser Sign-In option."
                            )
                        raise SgfleetAuthError("Invalid email or password.")

                # Follow redirect chain on id.sgfleet.com until redirected to AUTH0_REDIRECT_URI
                for _ in range(10):
                    if not next_url:
                        break
                    if next_url.startswith(AUTH0_REDIRECT_URI):
                        code = extract_code_from_callback(next_url, expected_state=expected_state)
                        break
                    if "/u/mfa" in next_url or "mfa" in urlparse(next_url).path.lower():
                        raise SgfleetMfaRequiredError(
                            "Multi-factor authentication is required. Please use the Browser Sign-In option."
                        )

                    async with login_sess.get(next_url, allow_redirects=False) as resp:
                        if resp.status in (301, 302, 303, 307, 308) and "Location" in resp.headers:
                            next_url = urljoin(next_url, resp.headers["Location"])
                        else:
                            body = await resp.text()
                            if _has_captcha_challenge(body):
                                raise SgfleetCaptchaRequiredError(
                                    "Auth0 requested a CAPTCHA challenge. Please use the Browser Sign-In option."
                                )
                            raise SgfleetAuthError(
                                f"Unexpected Auth0 response (HTTP {resp.status}) at {resp.url}"
                            )

                if not code:
                    raise SgfleetAuthError("Did not receive an authorization code from Auth0.")

        except SgfleetError:
            raise
        except aiohttp.ClientError as err:
            raise SgfleetConnectionError(f"Failed connecting to Auth0 ({AUTH0_DOMAIN}): {err}") from err

        return await self.exchange_authorization_code(code, verifier)

    async def _post_auth0_token(self, payload: dict[str, str]) -> None:
        """Call Auth0 /oauth/token and update token fields."""
        try:
            async with self._session.post(
                AUTH0_TOKEN_URL,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    err_desc = data.get("error_description") or data.get("error") or f"HTTP {resp.status}"
                    raise SgfleetAuthError(f"Auth0 token error: {err_desc}")
        except SgfleetError:
            raise
        except aiohttp.ClientError as err:
            raise SgfleetConnectionError(f"Error contacting Auth0 token endpoint: {err}") from err

        access_token = data.get("access_token")
        if not access_token:
            raise SgfleetAuthError("Auth0 token response did not contain an access_token.")

        expires_in = int(data.get("expires_in", 3600))
        self.tokens.access_token = access_token
        self.tokens.access_token_expires_at = time.time() + expires_in
        if data.get("refresh_token"):
            self.tokens.refresh_token = data["refresh_token"]

    async def exchange_web_token(self) -> AuthTokens:
        """Exchange the Auth0 access token for an SG Fleet API sessionId."""
        if not self.tokens.access_token:
            raise SgfleetAuthError("Cannot exchange web-token without an Auth0 access token.")

        url = f"{API_PEOPLE_BASE}/auth/web-token"
        headers = {
            **_DEFAULT_HEADERS,
            "Authorization": f"Bearer {self.tokens.access_token}",
        }
        try:
            async with self._session.post(
                url,
                params={"sessionTimeOutInMins": SESSION_TIMEOUT_MINS},
                headers=headers,
            ) as resp:
                if resp.status in (401, 403):
                    raise SgfleetAuthError(f"SG Fleet web-token exchange rejected (HTTP {resp.status}).")
                if resp.status >= 400:
                    text = await resp.text()
                    raise SgfleetError(f"SG Fleet web-token error (HTTP {resp.status}): {text[:200]}")
                data = await resp.json(content_type=None)
        except SgfleetError:
            raise
        except aiohttp.ClientError as err:
            raise SgfleetConnectionError(f"Error contacting SG Fleet web-token endpoint: {err}") from err

        if not data or not data.get("success") or not data.get("sessionId"):
            raise SgfleetAuthError(data.get("message") or "SG Fleet web-token exchange was unsuccessful.")

        self.tokens.session_id = data["sessionId"]
        claims_map = {c["type"]: c["value"] for c in (data.get("claims") or []) if "type" in c and "value" in c}
        self.tokens.person_id = claims_map.get(CLAIM_PERSON_UID, self.tokens.person_id)
        self.tokens.email = claims_map.get(CLAIM_EMAIL, self.tokens.email)
        self.tokens.name = claims_map.get(CLAIM_NAME, self.tokens.name)
        self.tokens.employer_name = claims_map.get(CLAIM_EMPLOYER_NAME, self.tokens.employer_name)
        self.tokens.employer_id = claims_map.get(CLAIM_EMPLOYER_ID, self.tokens.employer_id)

        exp_raw = claims_map.get(CLAIM_EXP)
        if exp_raw and str(exp_raw).isdigit():
            self.tokens.session_expires_at = float(exp_raw)
        else:
            self.tokens.session_expires_at = time.time() + (SESSION_TIMEOUT_MINS * 60)

        return self.tokens

    async def ensure_session(self, *, force_refresh: bool = False) -> str:
        """Ensure a valid SG Fleet sessionId is available, refreshing transparently."""
        now = time.time()
        if (
            not force_refresh
            and self.tokens.session_id
            and now < (self.tokens.session_expires_at - SESSION_REFRESH_BUFFER_SECS)
        ):
            return self.tokens.session_id

        async with self._auth_lock:
            now = time.time()
            if (
                not force_refresh
                and self.tokens.session_id
                and now < (self.tokens.session_expires_at - SESSION_REFRESH_BUFFER_SECS)
            ):
                return self.tokens.session_id

            # Step 1: Ensure valid Auth0 access token
            access_valid = (
                not force_refresh
                and self.tokens.access_token is not None
                and now < (self.tokens.access_token_expires_at - SESSION_REFRESH_BUFFER_SECS)
            )
            if not access_valid:
                if self.tokens.refresh_token:
                    try:
                        await self.refresh_auth0_token()
                    except SgfleetAuthError:
                        if self._email and self._password:
                            await self.authenticate_with_credentials()
                            return self.tokens.session_id  # type: ignore[return-value]
                        raise
                elif self._email and self._password:
                    await self.authenticate_with_credentials()
                    return self.tokens.session_id  # type: ignore[return-value]
                else:
                    raise SgfleetAuthError(
                        "SG Fleet session expired and no refresh token or credentials are available."
                    )

            # Step 2: Exchange Auth0 access token for SG Fleet sessionId
            try:
                await self.exchange_web_token()
            except SgfleetAuthError:
                # Access token may have been revoked early; try refresh_token or credentials once
                if self.tokens.refresh_token:
                    await self.refresh_auth0_token()
                    await self.exchange_web_token()
                elif self._email and self._password:
                    await self.authenticate_with_credentials()
                else:
                    raise

            self._notify_token_update()
            if not self.tokens.session_id:
                raise SgfleetAuthError("Failed to establish SG Fleet session.")
            return self.tokens.session_id

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        expect_bytes: bool = False,
        extra_headers: dict[str, str] | None = None,
        allow_404_none: bool = False,
    ) -> Any:
        """Execute an authenticated request to api.sgfleet.com with automatic 401 retry."""
        for attempt in range(2):
            session_id = await self.ensure_session(force_refresh=(attempt > 0))
            headers = {
                "Accept": "application/json, image/png, image/*",
                "Authorization": f"Bearer {session_id}",
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if extra_headers:
                headers.update(extra_headers)

            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=headers,
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        _LOGGER.debug("SG Fleet API returned 401 for %s; refreshing session", url)
                        self.tokens.session_id = None
                        continue
                    if resp.status in (401, 403):
                        raise SgfleetAuthError(f"SG Fleet API authorization failed (HTTP {resp.status}) for {url}")
                    if resp.status == 404 and allow_404_none:
                        return None
                    if resp.status == 204:
                        return None
                    if resp.status >= 400:
                        body_text = await resp.text()
                        raise SgfleetError(f"SG Fleet API error HTTP {resp.status} for {url}: {body_text[:300]}")
                    if expect_bytes:
                        return await resp.read()
                    content_type = resp.headers.get("Content-Type", "")
                    if "application/json" in content_type or content_type == "":
                        text = await resp.text()
                        if not text.strip():
                            return None
                        return await resp.json(content_type=None)
                    return await resp.text()
            except SgfleetError:
                raise
            except aiohttp.ClientError as err:
                raise SgfleetConnectionError(f"Error calling {url}: {err}") from err

        raise SgfleetAuthError(f"Repeated 401 Unauthorized responses for {url}")

    # ------------------------------------------------------------------
    # Read endpoints
    # ------------------------------------------------------------------

    async def get_person_profile(self, person_id: str | None = None) -> dict[str, Any]:
        """GET /au-peopleandorg/v1.0/people/{personId}?Profile=detailed&ContactType=personal."""
        pid = person_id or self.person_id
        if not pid:
            await self.ensure_session()
            pid = self.person_id
        return await self._request(
            "GET",
            f"{API_PEOPLE_BASE}/people/{pid}",
            params={"Profile": "detailed", "ContactType": "personal"},
        )

    async def get_employer(self, person_id: str | None = None) -> dict[str, Any] | None:
        """GET /au-peopleandorg/v1.0/people/{personId}/employer."""
        pid = person_id or self.person_id
        return await self._request("GET", f"{API_PEOPLE_BASE}/people/{pid}/employer", allow_404_none=True)

    async def get_bank_account(self, person_id: str | None = None) -> dict[str, Any] | None:
        """GET /au-peopleandorg/v1.0/people/{personId}/bankaccount."""
        pid = person_id or self.person_id
        return await self._request("GET", f"{API_PEOPLE_BASE}/people/{pid}/bankaccount", allow_404_none=True)

    async def get_novated_vehicles(self) -> list[dict[str, Any]]:
        """GET /au-vehicle/v1.0/vehicles/novated."""
        data = await self._request("GET", f"{API_VEHICLE_BASE}/vehicles/novated")
        if isinstance(data, dict):
            return list(data.get("result") or [])
        if isinstance(data, list):
            return data
        return []

    async def get_vehicle(self, vehicle_guid: str) -> dict[str, Any]:
        """GET /au-vehicle/v1.0/vehicles/{vehicleGuid}."""
        return await self._request("GET", f"{API_VEHICLE_BASE}/vehicles/{vehicle_guid}")

    async def get_vehicle_image(self, vehicle_guid: str) -> bytes | None:
        """GET /au-vehicle/v1.0/vehicles/{vehicleGuid}/image."""
        return await self._request(
            "GET",
            f"{API_VEHICLE_BASE}/vehicles/{vehicle_guid}/image",
            expect_bytes=True,
            allow_404_none=True,
        )

    async def get_data_load_status(self) -> dict[str, Any] | None:
        """GET /au-contract/v1.0/Admin/dataloadstatus."""
        return await self._request("GET", f"{API_CONTRACT_BASE}/Admin/dataloadstatus", allow_404_none=True)

    async def get_contract(self, contract_guid: str) -> dict[str, Any] | None:
        """GET /au-contract/v1.0/Contracts/{contractGuid}."""
        return await self._request("GET", f"{API_CONTRACT_BASE}/Contracts/{contract_guid}", allow_404_none=True)

    async def get_contract_current_version(self, contract_guid: str) -> dict[str, Any] | None:
        """GET /au-contract/v1.0/Contracts/{contractGuid}/currentVersion."""
        return await self._request(
            "GET",
            f"{API_CONTRACT_BASE}/Contracts/{contract_guid}/currentVersion",
            allow_404_none=True,
        )

    async def get_contract_services(self, contract_guid: str) -> list[dict[str, Any]]:
        """GET /au-contract/v1.0/Contracts/{contractGuid}/services."""
        data = await self._request(
            "GET",
            f"{API_CONTRACT_BASE}/Contracts/{contract_guid}/services",
            allow_404_none=True,
        )
        return list(data) if isinstance(data, list) else []

    async def get_registration_details(self, contract_guid: str) -> dict[str, Any] | None:
        """GET /au-contract/v1.0/Registration/{contractGuid}/registrationdetails."""
        return await self._request(
            "GET",
            f"{API_CONTRACT_BASE}/Registration/{contract_guid}/registrationdetails",
            allow_404_none=True,
        )

    async def get_balance(self, vehicle_guid: str) -> dict[str, Any] | None:
        """GET /au-salpack/v1.0/Balances/{vehicleGuid}."""
        return await self._request("GET", f"{API_SALPACK_BASE}/Balances/{vehicle_guid}", allow_404_none=True)

    async def get_balance_transactions(
        self,
        vehicle_guid: str,
        *,
        page_size: int = DEFAULT_TRANSACTIONS_PAGE_SIZE,
        cursor: str | None = None,
        expense_type: str | None = None,
        start: datetime | None = None,
        finish: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """GET /au-salpack/v1.0/Balances/{vehicleGuid}/transactions."""
        params: dict[str, Any] = {"PageSize": page_size}
        if cursor:
            params["Cursor"] = cursor
        if expense_type:
            params["ExpenseType"] = expense_type
        if start:
            params["Start"] = start.astimezone(timezone.utc).isoformat()
        if finish:
            params["Finish"] = finish.astimezone(timezone.utc).isoformat()

        data = await self._request(
            "GET",
            f"{API_SALPACK_BASE}/Balances/{vehicle_guid}/transactions",
            params=params,
            allow_404_none=True,
        )
        if isinstance(data, dict):
            return list(data.get("result") or [])
        if isinstance(data, list):
            return data
        return []

    async def get_reimbursement_settings(self) -> dict[str, Any]:
        """GET /au-salpack/v1.0/Reimbursements."""
        data = await self._request("GET", f"{API_SALPACK_BASE}/Reimbursements", allow_404_none=True)
        return dict(data) if isinstance(data, dict) else {}

    async def get_reimbursement_categories(self, vehicle_guid: str) -> list[dict[str, Any]]:
        """GET /au-salpack/v1.0/Reimbursements/{vehicleGuid}/categoryvisibilities."""
        data = await self._request(
            "GET",
            f"{API_SALPACK_BASE}/Reimbursements/{vehicle_guid}/categoryvisibilities",
            allow_404_none=True,
        )
        if isinstance(data, dict):
            return list(data.get("categoryVisibilities") or [])
        return []

    async def get_has_valid_bank_account(self, person_id: str | None = None) -> bool | None:
        """GET /au-salpack/v1.0/Reimbursements/{personId}/hasvalidbankaccount."""
        pid = person_id or self.person_id
        data = await self._request(
            "GET",
            f"{API_SALPACK_BASE}/Reimbursements/{pid}/hasvalidbankaccount",
            allow_404_none=True,
        )
        if isinstance(data, dict) and "hasValidBankAccount" in data:
            return bool(data["hasValidBankAccount"])
        return None

    async def get_reimbursement_history(self, vehicle_guid: str) -> list[dict[str, Any]]:
        """GET /au-salpack/v1.0/Reimbursements/{vehicleGuid}/historytransactions."""
        data = await self._request(
            "GET",
            f"{API_SALPACK_BASE}/Reimbursements/{vehicle_guid}/historytransactions",
            allow_404_none=True,
        )
        if isinstance(data, dict):
            return list(data.get("reimbursementTransactions") or [])
        return []

    # ------------------------------------------------------------------
    # Write / Action endpoints
    # ------------------------------------------------------------------

    async def upload_reimbursement_file(
        self,
        form_id: str,
        category: str,
        file_name: str,
        file_bytes: bytes,
        *,
        file_id: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """POST /au-salpack/v1.0/Reimbursements/{FormId}/files/{Category}."""
        fid = file_id or str(uuid.uuid4())
        mime = content_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        form = aiohttp.FormData()
        form.add_field("fileId", fid)
        form.add_field("file", file_bytes, filename=file_name, content_type=mime)

        return await self._request(
            "POST",
            f"{API_SALPACK_BASE}/Reimbursements/{form_id}/files/{category}",
            data=form,
        )

    async def submit_reimbursement(
        self,
        vehicle_guid: str,
        expenses: list[dict[str, Any]],
        *,
        form_id: str | None = None,
        include_home_charging_declaration: bool | None = None,
    ) -> dict[str, Any]:
        """POST /au-salpack/v1.0/Reimbursements."""
        has_home_charging = (
            include_home_charging_declaration
            if include_home_charging_declaration is not None
            else any(e.get("category") == CATEGORY_HOME_CHARGING for e in expenses)
        )
        payload = {
            "vehicleGuid": vehicle_guid,
            "formId": form_id or str(uuid.uuid4()),
            "expenses": expenses,
            "termsAndConditionsMd": build_terms_and_conditions_md(has_home_charging),
        }
        return await self._request(
            "POST",
            f"{API_SALPACK_BASE}/Reimbursements",
            json_body=payload,
        )

    async def submit_home_charging_claim(
        self,
        vehicle_guid: str,
        start_date: date,
        end_date: date,
        start_odometer: float,
        end_odometer: float,
        *,
        reimbursement_settings: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Validate, calculate, and submit an EV Home Charging reimbursement claim."""
        settings = reimbursement_settings or await self.get_reimbursement_settings()
        calc = calculate_home_charging_claim(
            start_date=start_date,
            end_date=end_date,
            start_odometer=start_odometer,
            end_odometer=end_odometer,
            reimbursement_settings=settings,
        )

        form_id = str(uuid.uuid4())
        expense_id = str(uuid.uuid4())
        expense = {
            "expenseId": expense_id,
            "category": CATEGORY_HOME_CHARGING,
            "uniqueFileNames": [],
            "dateOfStartOdometerReading": f"{start_date.isoformat()}T00:00:00.000Z",
            "dateOfEndOdometerReading": f"{end_date.isoformat()}T00:00:00.000Z",
            "startOdometerReading": start_odometer,
            "endOdometerReading": end_odometer,
            "amount": calc["amount"],
        }

        if dry_run:
            return {
                "dry_run": True,
                "vehicle_guid": vehicle_guid,
                "form_id": form_id,
                "calculation": calc,
                "expense": expense,
            }

        response = await self.submit_reimbursement(
            vehicle_guid=vehicle_guid,
            expenses=[expense],
            form_id=form_id,
            include_home_charging_declaration=True,
        )
        return {
            "dry_run": False,
            "vehicle_guid": vehicle_guid,
            "form_id": form_id,
            "calculation": calc,
            "response": response,
        }

    async def register_odometer_reading(
        self,
        vehicle_guid: str,
        odometer: int | float,
        *,
        reading_date: date | None = None,
    ) -> Any:
        """PUT /au-vehicle/v1.0/vehicles/{VehicleGuid}/odometer."""
        if odometer <= 0:
            raise SgfleetValidationError("Odometer reading must be a positive number.")
        dt = reading_date or date.today()
        payload = {
            "odometerReading": int(round(odometer)),
            "readingDate": f"{dt.isoformat()}T00:00:00.000Z",
        }
        return await self._request(
            "PUT",
            f"{API_VEHICLE_BASE}/vehicles/{vehicle_guid}/odometer",
            json_body=payload,
        )
