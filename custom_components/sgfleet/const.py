"""Constants for the SG Fleet Novated Leasing (mySG) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sgfleet"
MANUFACTURER: Final = "SG Fleet"

# Auth0 configuration (from https://mysg.sgfleet.com/appsettings.json)
AUTH0_DOMAIN: Final = "id.sgfleet.com"
AUTH0_CLIENT_ID: Final = "tnI1h8nX9GFDKissRGQvWrP5tQqEmJ6H"
AUTH0_AUDIENCE: Final = "sgau-dpl-api"
AUTH0_REDIRECT_URI: Final = "https://mysg.sgfleet.com"
AUTH0_SCOPE: Final = "openid profile email offline_access"
AUTH0_APP_ENV: Final = "prd"

AUTH0_AUTHORIZE_URL: Final = f"https://{AUTH0_DOMAIN}/authorize"
AUTH0_TOKEN_URL: Final = f"https://{AUTH0_DOMAIN}/oauth/token"

# SG Fleet API base URLs (from https://mysg.sgfleet.com/appsettings.json)
API_BASE_URL: Final = "https://api.sgfleet.com"
API_VEHICLE_BASE: Final = f"{API_BASE_URL}/au-vehicle/v1.0"
API_PEOPLE_BASE: Final = f"{API_BASE_URL}/au-peopleandorg/v1.0"
API_CONTRACT_BASE: Final = f"{API_BASE_URL}/au-contract/v1.0"
API_SALPACK_BASE: Final = f"{API_BASE_URL}/au-salpack/v1.0"

SESSION_TIMEOUT_MINS: Final = 60
SESSION_REFRESH_BUFFER_SECS: Final = 300  # Refresh 5 mins before expiry

# SG Fleet session claims URIs
CLAIM_PERSON_UID: Final = "http://schema.sgfleet.com/2022/01/identity/claims/personuid"
CLAIM_EXP: Final = "http://schema.sgfleet.com/2022/01/identity/claims/exp"
CLAIM_EMAIL: Final = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
CLAIM_NAME: Final = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"
CLAIM_EMPLOYER_NAME: Final = "http://schema.sgfleet.com/2022/01/identity/claims/employername"
CLAIM_EMPLOYER_ID: Final = "http://schema.sgfleet.com/2022/01/identity/claims/employerid"

# Config entry keys
CONF_AUTH_METHOD: Final = "auth_method"
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_TOKEN_EXPIRES_AT: Final = "token_expires_at"
CONF_PERSON_ID: Final = "person_id"
CONF_CALLBACK_URL: Final = "callback_url"
CONF_MFA_CODE: Final = "mfa_code"

AUTH_METHOD_CREDENTIALS: Final = "credentials"
AUTH_METHOD_BROWSER_PKCE: Final = "browser_pkce"

# Options keys
CONF_REIMBURSEMENTS_INTERVAL_HOURS: Final = "reimbursements_interval_hours"
CONF_REIMBURSEMENTS_SMART_MODE: Final = "reimbursements_smart_mode"
CONF_BALANCES_INTERVAL_HOURS: Final = "balances_interval_hours"
CONF_DETAILS_INTERVAL_HOURS: Final = "details_interval_hours"
CONF_ACTIVE_HOURS_ENABLED: Final = "active_hours_enabled"
CONF_ACTIVE_HOURS_START: Final = "active_hours_start"
CONF_ACTIVE_HOURS_END: Final = "active_hours_end"
CONF_REGO_WARNING_DAYS: Final = "rego_warning_days"
CONF_ODOMETER_ENTITY_ID: Final = "odometer_entity_id"
CONF_LINKED_VEHICLE_DEVICE_ID: Final = "linked_vehicle_device_id"

# Polling tier identifiers
TIER_REIMBURSEMENTS: Final = "reimbursements"
TIER_BALANCES: Final = "balances"
TIER_DETAILS: Final = "details"
TIER_ALL: Final = "all"
ALL_TIERS: Final[frozenset[str]] = frozenset({TIER_REIMBURSEMENTS, TIER_BALANCES, TIER_DETAILS})

# Default polling intervals & safeguards
DEFAULT_REIMBURSEMENTS_INTERVAL_HOURS: Final = 4
MIN_REIMBURSEMENTS_INTERVAL_HOURS: Final = 1
MAX_REIMBURSEMENTS_INTERVAL_HOURS: Final = 24
DEFAULT_REIMBURSEMENTS_SMART_MODE: Final = True

DEFAULT_BALANCES_INTERVAL_HOURS: Final = 12
MIN_BALANCES_INTERVAL_HOURS: Final = 4
MAX_BALANCES_INTERVAL_HOURS: Final = 48

# 0 means Never (startup & manual/service refresh only)
DEFAULT_DETAILS_INTERVAL_HOURS: Final = 168  # 7 days
DETAILS_INTERVAL_NEVER: Final = 0

DEFAULT_ACTIVE_HOURS_ENABLED: Final = True
DEFAULT_ACTIVE_HOURS_START: Final = "07:00:00"
DEFAULT_ACTIVE_HOURS_END: Final = "21:00:00"
POLL_JITTER_FRACTION: Final = 0.15  # +/- 15% random jitter

DEFAULT_REGO_WARNING_DAYS: Final = 30
DEFAULT_CURRENCY: Final = "AUD"
DEFAULT_TRANSACTIONS_PAGE_SIZE: Final = 15

# Reimbursement categories (exact keys expected by POST /au-salpack/v1.0/Reimbursements)
REIMBURSEMENT_CATEGORIES: Final[tuple[str, ...]] = (
    "homecharging",
    "commercialcharging",
    "fuel",
    "serviceandmaintenance",
    "tyres",
    "carwash",
    "registration",
    "nswctp",
    "insurance",
    "insuranceexcess",
)

CATEGORY_HOME_CHARGING: Final = "homecharging"

# Pending/active reimbursement status strings
PENDING_REIMBURSEMENT_STATUSES: Final[frozenset[str]] = frozenset(
    {"in review", "pending", "submitted", "awaiting balance", "in progress"}
)

# Budget buckets returned by GET /au-salpack/v1.0/Balances/{vehicleGuid}
# Tuple: (json_key, translation_key, friendly_name, icon, enabled_by_default_core)
BUDGET_CATEGORIES: Final[tuple[tuple[str, str, str, str, bool], ...]] = (
    ("fuel", "budget_fuel", "Fuel & EV Charging Budget", "mdi:ev-station", True),
    ("repairAndMaintenance", "budget_maintenance", "Maintenance Budget", "mdi:wrench", True),
    ("tyres", "budget_tyres", "Tyres Budget", "mdi:tire", True),
    ("registration", "budget_registration", "Registration Budget", "mdi:card-account-details", True),
    ("insurance", "budget_insurance", "Insurance Budget", "mdi:shield-car", True),
    ("roadsideAssistance", "budget_roadside", "Roadside Assistance Budget", "mdi:tow-truck", False),
    ("salaryPackagingFee", "budget_salpack_fee", "Salary Packaging Fee Budget", "mdi:receipt-text", True),
    ("leaseRental", "budget_lease_rental", "Lease Rental Budget", "mdi:car-key", True),
    ("contingency", "budget_contingency", "Contingency Budget", "mdi:piggy-bank", False),
    ("leaseguard", "budget_leaseguard", "Leaseguard Budget", "mdi:shield-check", False),
    ("carbonOffset", "budget_carbon_offset", "Carbon Offset Budget", "mdi:leaf", False),
    ("extendedWarranty", "budget_extended_warranty", "Extended Warranty Budget", "mdi:certificate", False),
    ("scratchAndDent", "budget_scratch_and_dent", "Scratch & Dent Budget", "mdi:car-wrench", False),
    ("tyreAndRim", "budget_tyre_and_rim", "Tyre & Rim Budget", "mdi:car-tire-alert", False),
    ("gstOnEmployeeContribution", "budget_gst_ecm", "GST on Employee Contribution", "mdi:percent", False),
    ("otherServices", "budget_other_services", "Other Services Budget", "mdi:dots-horizontal-circle", False),
)

# Official mySG declarations sent in termsAndConditionsMd for POST /Reimbursements
HOME_CHARGING_DECLARATION_TEXT: Final = (
    "By submitting this form, I declare and confirm that the following information is true and accurate:\n\n\n"
    "* This is a true and accurate record of the kilometres I have travelled in my Battery Electric Novated Lease Vehicle.\n\n\n"
    "* I have not already claimed any commercial charging costs via my lease package for the period reported in this declaration.\n\n\n"
    "* I have an agreement/account with an electricity retailer in my name (joint names are allowable) at my primary residential address, which matches the registered address of my battery electric novated lease vehicle.\n\n\n"
    "* If requested, I can provide a copy of the electricity account to substantiate the claim and information.\n\n\n"
    "* I have checked and confirm that my bank details for reimbursement within SG Fleet’s mySG system are correct.\n\n\n"
    "* I understand and agree that the amount reimbursed will be calculated as outlined in the ATO PCG in the period reported.\n\n\n"
    "* I understand and agree that the reimbursement of this amount will come from my current salary packaging account balance, and that if required, I may need to re-cost my package"
)

GENERAL_DECLARATION_TEXT: Final = "\n".join(
    [
        "My bank account details are current in mySG. Please click [here](/account) to update details if needed.",
        "the attached invoice(s) and receipts are valid records of personal expenses incurred by me against the nominated vehicle and that the amounts claimed meet the ATO guidelines seen [here](https://www.ato.gov.au/individuals-and-families/income-deductions-offsets-and-records/deductions-you-can-claim/work-related-deductions/cars-transport-and-travel/motor-vehicle-and-car-expenses/expenses-for-a-vehicle-that-isn-t-yours-or-isn-t-a-car).",
        "I am seeking a reimbursement of this cost from SG Fleet and authorise the same to be included within my lease in accordance with the contract terms.",
        "In the event that I claim and I am reimbursed for any amount not properly claimable per the ATO guidelines, I acknowledge that I am liable to repay any such reimbursement.",
        "The calculation of stamp duty and GST on my comprehensive insurance reimbursement is based on the state or territory in which the vehicle is registered.",
        "I confirm that Mythe vehicle registration details provided are accurate and current.",
    ]
)

# Service names
SERVICE_SUBMIT_HOME_CHARGING_CLAIM: Final = "submit_home_charging_claim"
SERVICE_SUBMIT_REIMBURSEMENT_CLAIM: Final = "submit_reimbursement_claim"
SERVICE_REGISTER_ODOMETER: Final = "register_odometer_reading"
SERVICE_REFRESH_DATA: Final = "refresh_data"

MAX_CLAIM_AMOUNT: Final = 50000.0
