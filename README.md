<<<<<<< HEAD
# SG Fleet Novated Leasing (mySG) for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A custom Home Assistant integration for **SG Fleet Novated Leasing (Australia — `mysg.sgfleet.com`)**.

> **Disclaimer:** This is an unofficial community integration that uses the mySG driver portal's REST APIs (`api.sgfleet.com`). It is not affiliated with or endorsed by SG Fleet.

---

## Features

- **Lease Balances & Running-Cost Budgets (`AUD`)**
  - Available balance, actual balance, total contributions received, total running costs spent, pending transactions, and expected contributions.
  - Individual budget bucket sensors (Fuel & EV Charging, Maintenance, Tyres, Registration, Insurance, Salary Packaging Fee, Lease Rental, Roadside Assistance, Leaseguard, Contingency, etc.) with `received`, `spent`, and `pending` attributes.
- **Recent Transactions & Reimbursement Tracking**
  - Latest transaction amount & metadata plus the 15 most recent transactions as structured attributes.
  - Latest reimbursement status, pending reimbursements total (`AUD`), recent 6-month claim history, enabled reimbursement categories, and live ATO PCG EV home charging rate (`AUD/km`).
- **Vehicle & Lease Milestones**
  - Registration expiry date & days remaining (plus configurable **Registration Expiring Soon** problem binary sensor).
  - Lease end date, days remaining, lease progress `%`, residual value (incl./excl. GST) & due date, next service date, and last recorded odometer.
- **Lease & Vehicle Details + Vehicle Image + Car CI Link**
  - Contract reference, financier, finance amount, interest rate, FBT base value, monthly rental, payroll frequency, included/excluded services, VIN, fuel type, build/compliance dates, and Jato vehicle photo (`image` entity).
  - **Linked Vehicle Device (CI Link):** Link the SG Fleet lease device to your existing car integration device in Home Assistant (e.g., BYD, Tesla) via `via_device` (auto-suggested by matching VIN!). Creates a native parent/child device relationship (`Car Device → SG Fleet Lease → Budgets & Transactions / Reimbursements`) and automatically discovers your car's odometer sensor for **Lease Distance Variance** and **EV Home Charging** claims if no explicit odometer sensor is set.
- **Low-Profile Tiered Polling (Configurable in Integration Options)**
  - **Reimbursements & Claims** (default `4h`, configurable `1–24h`) with **Smart Mode** (enabled by default): automatically slows down to match the Balances interval when you have 0 pending/in-review claims.
  - **Balances & Transactions** (default `12h`, configurable `4–48h`).
  - **Vehicle, Lease & Contract Details** (default `Weekly / 7 days`, configurable `Never (startup & manual only)` up to `Monthly`).
  - Automatic **±15% randomised jitter** and configurable **Active Hours** (default `07:00–21:00` local time) so Home Assistant never polls SG Fleet overnight or on exact clockwork boundaries.
- **Home Assistant Actions (Services)**
  - `sgfleet.refresh_data`: Trigger an immediate refresh of `all`, `reimbursements`, `balances`, or `details` (bypasses quiet hours).

---

## Installation

### Option 1: HACS / Home Assistant Marketplace (Custom Repository)
1. Push this repository to GitHub (or use your forked URL).
2. In Home Assistant, open **HACS / Marketplace → Custom repositories**.
3. Add the repository URL with category **Integration**.
4. Install **SG Fleet Novated Leasing (mySG)** and restart Home Assistant.

### Option 2: Manual Install (Samba / SSH / File Editor)
1. Copy `custom_components/sgfleet` into `/config/custom_components/sgfleet` on your Home Assistant host.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration → SG Fleet Novated Leasing**.

---

## Authentication

SG Fleet uses **Auth0 (`id.sgfleet.com`)** followed by an SG Fleet session exchange (`POST /au-peopleandorg/v1.0/auth/web-token`).

During setup (and re-authentication), you can choose between:
1. **Email & Password (Auth0 Universal Login PKCE):** Signs in using Auth0 PKCE. If Auth0 prompts for MFA or CAPTCHA, setup automatically switches to Browser Sign-In.
2. **Browser Sign-In (PKCE Callback URL):** Opens the Auth0 sign-in URL in your own browser so you can complete MFA/SSO/CAPTCHA interactively, then paste the redirected `https://mysg.sgfleet.com/?code=...&state=...` URL into Home Assistant.

---

## Example: EV Home Charging Claim Action

Preview (`dry_run: true`) first in **Developer Tools → Actions** using your EV's odometer sensor:

```yaml
action: sgfleet.submit_home_charging_claim
data:
  start_date: "2026-10-01"
  end_date: "2026-11-01"
  start_odometer: 24500
  odometer_entity_id: sensor.byd_atto_3_odometer
  confirm_ato_declaration: true
  dry_run: true
```

Set `dry_run: false` (with `confirm_ato_declaration: true`) to submit the claim and receive the SG Fleet `referenceNumber` in the action response.

## Example: Registration Expiry Alert Automation

```yaml
alias: SG Fleet Registration Expiring Soon
trigger:
  - platform: state
    entity_id: binary_sensor.your_vehicle_registration_expiring_soon
    to: "on"
action:
  - action: notify.mobile_app_your_phone
    data:
      title: "SG Fleet Registration Renewal"
      message: >-
        Your novated lease registration expires on
        {{ states('sensor.your_vehicle_registration_expiry') }}
        ({{ states('sensor.your_vehicle_registration_days_remaining') }} days remaining).
```

