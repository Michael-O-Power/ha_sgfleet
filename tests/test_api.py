"""Unit tests for the SG Fleet API client and Home Charging calculations."""

from __future__ import annotations

from datetime import date
import json
import time
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from custom_components.sgfleet.api import (
    SgfleetApiClient,
    SgfleetAuthError,
    SgfleetCaptchaRequiredError,
    SgfleetMfaRequiredError,
    SgfleetValidationError,
    build_authorize_url,
    build_terms_and_conditions_md,
    calculate_home_charging_claim,
    extract_code_from_callback,
    generate_pkce_pair,
)
from custom_components.sgfleet.const import (
    API_CONTRACT_BASE,
    API_PEOPLE_BASE,
    API_SALPACK_BASE,
    API_VEHICLE_BASE,
    AUTH0_TOKEN_URL,
    CLAIM_EMAIL,
    CLAIM_EXP,
    CLAIM_NAME,
    CLAIM_PERSON_UID,
)


class _MockResponse:
    """Minimal async context manager matching aiohttp.ClientResponse."""

    def __init__(
        self,
        *,
        url: str,
        status: int = 200,
        json_data: Any = None,
        text_data: str | None = None,
        bytes_data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self._json_data = json_data
        self._text_data = text_data
        self._bytes_data = bytes_data
        self.headers = dict(headers or {})
        if json_data is not None and "Content-Type" not in self.headers:
            self.headers["Content-Type"] = "application/json"

    async def __aenter__(self) -> _MockResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def json(self, content_type: Any = None) -> Any:
        if self._json_data is not None:
            return self._json_data
        return json.loads(self._text_data or "{}")

    async def text(self) -> str:
        if self._text_data is not None:
            return self._text_data
        if self._json_data is not None:
            return json.dumps(self._json_data)
        return ""

    async def read(self) -> bytes:
        if self._bytes_data is not None:
            return self._bytes_data
        return (await self.text()).encode("utf-8")


class _MockSession:
    """Queue-based mock aiohttp.ClientSession recording every outgoing call."""

    def __init__(self, responses: list[_MockResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _MockSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def _next(self, method: str, url: str, **kwargs: Any) -> _MockResponse:
        params = kwargs.get("params")
        full_url = f"{url}?{urlencode(params)}" if params else url
        self.calls.append({"method": method.upper(), "url": full_url, **kwargs})
        assert self._responses, f"Unexpected HTTP {method} {full_url}"
        return self._responses.pop(0)

    def request(self, method: str, url: str, **kwargs: Any) -> _MockResponse:
        return self._next(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> _MockResponse:
        return self._next("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _MockResponse:
        return self._next("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> _MockResponse:
        return self._next("PUT", url, **kwargs)


def test_pkce_and_callback_extraction() -> None:
    verifier, challenge = generate_pkce_pair()
    assert len(verifier) >= 43
    assert len(challenge) >= 43
    assert verifier != challenge

    url, state = build_authorize_url(challenge)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "id.sgfleet.com"
    assert qs["code_challenge"][0] == challenge
    assert qs["code_challenge_method"][0] == "S256"
    assert "offline_access" in qs["scope"][0]

    code = extract_code_from_callback(
        f"https://mysg.sgfleet.com/?code=abc123XYZ&state={state}",
        expected_state=state,
    )
    assert code == "abc123XYZ"
    assert extract_code_from_callback("raw_code_only") == "raw_code_only"

    with pytest.raises(SgfleetAuthError):
        extract_code_from_callback(
            "https://mysg.sgfleet.com/?code=abc&state=wrong",
            expected_state=state,
        )

    with pytest.raises(SgfleetAuthError):
        extract_code_from_callback("https://mysg.sgfleet.com/?error=access_denied&error_description=Denied")


def test_calculate_home_charging_claim_current_and_previous_fbt_year() -> None:
    settings = {
        "previousHomeChargingRate": 0.042,
        "currentHomeChargingRate": 0.0547,
        "fbtCutoffDate": "2026-04-01T00:00:00",
    }

    calc = calculate_home_charging_claim(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 1),
        start_odometer=24000,
        end_odometer=24500,
        reimbursement_settings=settings,
    )
    assert calc["distance_km"] == 500
    assert calc["rate_per_km"] == 0.0547
    assert calc["amount"] == 27.35

    calc_prev = calculate_home_charging_claim(
        start_date=date(2026, 2, 1),
        end_date=date(2026, 3, 15),
        start_odometer=10000,
        end_odometer=11000,
        reimbursement_settings=settings,
    )
    assert calc_prev["rate_per_km"] == 0.042
    assert calc_prev["amount"] == 42.0

    with pytest.raises(SgfleetValidationError, match="single FBT year"):
        calculate_home_charging_claim(
            start_date=date(2026, 3, 15),
            end_date=date(2026, 4, 15),
            start_odometer=10000,
            end_odometer=11000,
            reimbursement_settings=settings,
        )

    with pytest.raises(SgfleetValidationError, match="greater than start odometer"):
        calculate_home_charging_claim(
            start_date=date(2026, 10, 1),
            end_date=date(2026, 11, 1),
            start_odometer=25000,
            end_odometer=24000,
            reimbursement_settings=settings,
        )


def test_terms_and_conditions_md() -> None:
    with_ev = build_terms_and_conditions_md(True)
    assert "Home Charging Declaration:" in with_ev
    assert "General Declaration:" in with_ev
    assert "ATO PCG" in with_ev

    general_only = build_terms_and_conditions_md(False)
    assert "Home Charging Declaration:" not in general_only
    assert "General Declaration:" in general_only


async def test_pkce_exchange_web_token_and_api_requests() -> None:
    future_exp = int(time.time()) + 3600
    session = _MockSession(
        [
            _MockResponse(
                url=AUTH0_TOKEN_URL,
                json_data={
                    "access_token": "auth0_access_123",
                    "refresh_token": "auth0_refresh_456",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            ),
            _MockResponse(
                url=f"{API_PEOPLE_BASE}/auth/web-token",
                json_data={
                    "sessionId": "SESS12345678901",
                    "success": True,
                    "message": None,
                    "claims": [
                        {"type": CLAIM_PERSON_UID, "value": "person-guid-1"},
                        {"type": CLAIM_EMAIL, "value": "driver@example.com"},
                        {"type": CLAIM_NAME, "value": "Test Driver"},
                        {"type": CLAIM_EXP, "value": str(future_exp)},
                    ],
                },
            ),
            _MockResponse(
                url=f"{API_VEHICLE_BASE}/vehicles/novated",
                json_data={"result": [{"vehicleGuid": "veh-1", "contractGuid": "con-1", "registration": "EV1234"}]},
            ),
            _MockResponse(
                url=f"{API_SALPACK_BASE}/Balances/veh-1",
                json_data={"availableBalance": 1234.56, "actualBalance": {"balance": 1300.0}},
            ),
            _MockResponse(
                url=f"{API_CONTRACT_BASE}/Contracts/con-1",
                json_data={"contractGuid": "con-1", "contractStatus": "Running", "endDate": "2029-06-30"},
            ),
        ]
    )

    token_updates = []
    client = SgfleetApiClient(session, token_update_callback=token_updates.append)  # type: ignore[arg-type]
    tokens = await client.exchange_authorization_code("code123", "verifier123")
    assert tokens.session_id == "SESS12345678901"
    assert tokens.person_id == "person-guid-1"
    assert tokens.refresh_token == "auth0_refresh_456"
    assert len(token_updates) == 1

    vehicles = await client.get_novated_vehicles()
    assert vehicles[0]["vehicleGuid"] == "veh-1"
    assert session.calls[2]["headers"]["Authorization"] == "Bearer SESS12345678901"

    balance = await client.get_balance("veh-1")
    assert balance["availableBalance"] == 1234.56

    contract = await client.get_contract("con-1")
    assert contract["contractStatus"] == "Running"


async def test_401_triggers_automatic_refresh_and_retry() -> None:
    future_exp = int(time.time()) + 3600
    session = _MockSession(
        [
            _MockResponse(url=f"{API_SALPACK_BASE}/Balances/veh-1", status=401),
            _MockResponse(
                url=AUTH0_TOKEN_URL,
                json_data={"access_token": "auth0_new", "refresh_token": "rot_refresh_2", "expires_in": 3600},
            ),
            _MockResponse(
                url=f"{API_PEOPLE_BASE}/auth/web-token",
                json_data={
                    "sessionId": "NEW_SESS_999999",
                    "success": True,
                    "claims": [{"type": CLAIM_EXP, "value": str(future_exp)}],
                },
            ),
            _MockResponse(
                url=f"{API_SALPACK_BASE}/Balances/veh-1",
                json_data={"availableBalance": 555.0},
            ),
        ]
    )

    client = SgfleetApiClient(
        session,  # type: ignore[arg-type]
        refresh_token="old_refresh_1",
        access_token="expired_access",
        access_token_expires_at=time.time() + 1000,
    )
    client.tokens.session_id = "STALE_SESS"
    client.tokens.session_expires_at = time.time() + 1000

    bal = await client.get_balance("veh-1")
    assert bal["availableBalance"] == 555.0
    assert client.tokens.session_id == "NEW_SESS_999999"
    assert client.tokens.refresh_token == "rot_refresh_2"
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer NEW_SESS_999999"


async def test_submit_home_charging_dry_run_and_live() -> None:
    future_exp = int(time.time()) + 3600
    settings = {
        "previousHomeChargingRate": 0.042,
        "currentHomeChargingRate": 0.0547,
        "fbtCutoffDate": "2026-04-01T00:00:00",
    }
    session = _MockSession(
        [
            _MockResponse(
                url=f"{API_SALPACK_BASE}/Reimbursements",
                json_data={
                    "submittedOn": "2026-11-24T00:00:00Z",
                    "expenseReferences": [
                        {"expenseId": "e1", "isSuccessful": True, "referenceNumber": "REF999999", "errorMessage": None}
                    ],
                },
            )
        ]
    )

    client = SgfleetApiClient(session)  # type: ignore[arg-type]
    client.tokens.session_id = "VALID_SESS"
    client.tokens.session_expires_at = future_exp

    preview = await client.submit_home_charging_claim(
        vehicle_guid="veh-1",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 1),
        start_odometer=25000,
        end_odometer=25500,
        reimbursement_settings=settings,
        dry_run=True,
    )
    assert preview["dry_run"] is True
    assert preview["calculation"]["amount"] == 27.35
    assert len(session.calls) == 0

    submitted = await client.submit_home_charging_claim(
        vehicle_guid="veh-1",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 1),
        start_odometer=25000,
        end_odometer=25500,
        reimbursement_settings=settings,
        dry_run=False,
    )
    assert submitted["dry_run"] is False
    assert submitted["response"]["expenseReferences"][0]["referenceNumber"] == "REF999999"

    sent_body = session.calls[0]["json"]
    assert sent_body["vehicleGuid"] == "veh-1"
    assert sent_body["expenses"][0]["category"] == "homecharging"
    assert sent_body["expenses"][0]["amount"] == 27.35
    assert "Home Charging Declaration:" in sent_body["termsAndConditionsMd"]


async def test_credentials_login_captcha_and_mfa_detection() -> None:
    captcha_sess = _MockSession(
        [
            _MockResponse(
                url="https://id.sgfleet.com/u/login/identifier?state=st1",
                status=200,
                text_data='<html><div class="g-recaptcha"></div><input name="state" value="st1"/></html>',
            )
        ]
    )
    with patch("custom_components.sgfleet.api.aiohttp.ClientSession", return_value=captcha_sess):
        client = SgfleetApiClient(_MockSession([]), email="u@example.com", password="pw")  # type: ignore[arg-type]
        with pytest.raises(SgfleetCaptchaRequiredError):
            await client.authenticate_with_credentials()

    mfa_sess = _MockSession(
        [
            _MockResponse(
                url="https://id.sgfleet.com/u/login/identifier?state=st1",
                status=200,
                text_data='<html><form><input name="state" value="st1"/><input name="username"/></form></html>',
            ),
            _MockResponse(
                url="https://id.sgfleet.com/u/login/password?state=st2",
                status=200,
                text_data='<html><form><input name="state" value="st2"/><input type="password" name="password"/></form></html>',
            ),
            _MockResponse(
                url="https://id.sgfleet.com/u/login/password?state=st2",
                status=302,
                headers={"Location": "https://id.sgfleet.com/u/mfa-otp-challenge?state=st3"},
            ),
        ]
    )
    with patch("custom_components.sgfleet.api.aiohttp.ClientSession", return_value=mfa_sess):
        client = SgfleetApiClient(_MockSession([]), email="u@example.com", password="pw")  # type: ignore[arg-type]
        with pytest.raises(SgfleetMfaRequiredError):
            await client.authenticate_with_credentials()


async def test_receipt_upload_and_odometer_put() -> None:
    future_exp = int(time.time()) + 3600
    session = _MockSession(
        [
            _MockResponse(
                url=f"{API_SALPACK_BASE}/Reimbursements/form-1/files/commercialcharging",
                json_data={"fileId": "file-1", "uniqueFileName": "unique-invoice.pdf"},
            ),
            _MockResponse(
                url=f"{API_VEHICLE_BASE}/vehicles/veh-1/odometer",
                status=204,
            ),
        ]
    )
    client = SgfleetApiClient(session)  # type: ignore[arg-type]
    client.tokens.session_id = "VALID_SESS"
    client.tokens.session_expires_at = future_exp

    up = await client.upload_reimbursement_file(
        form_id="form-1",
        category="commercialcharging",
        file_name="invoice.pdf",
        file_bytes=b"%PDF-1.4 fake",
        file_id="file-1",
    )
    assert up["uniqueFileName"] == "unique-invoice.pdf"
    assert session.calls[0]["method"] == "POST"

    res = await client.register_odometer_reading("veh-1", 25321, reading_date=date(2026, 11, 24))
    assert res is None
    assert session.calls[1]["json"] == {
        "odometerReading": 25321,
        "readingDate": "2026-11-24T00:00:00.000Z",
    }


async def test_transient_499_retries_once_and_succeeds() -> None:
    future_exp = int(time.time()) + 3600
    session = _MockSession(
        [
            _MockResponse(url=f"{API_VEHICLE_BASE}/vehicles/novated", status=499, text_data=""),
            _MockResponse(
                url=f"{API_VEHICLE_BASE}/vehicles/novated",
                status=200,
                json_data={"result": [{"vehicleGuid": "veh-1"}]},
            ),
        ]
    )
    client = SgfleetApiClient(session)  # type: ignore[arg-type]
    client.tokens.session_id = "VALID_SESS"
    client.tokens.session_expires_at = future_exp

    with patch("custom_components.sgfleet.api.asyncio.sleep", return_value=None):
        vehicles = await client.get_novated_vehicles()
    assert len(vehicles) == 1
    assert vehicles[0]["vehicleGuid"] == "veh-1"
    assert len(session.calls) == 2
