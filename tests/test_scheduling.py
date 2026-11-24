"""Unit tests for tiered polling, smart reimbursement cadence, active hours, and jitter."""

from __future__ import annotations

from datetime import datetime
import random

from custom_components.sgfleet.const import (
    TIER_BALANCES,
    TIER_DETAILS,
    TIER_REIMBURSEMENTS,
)
from custom_components.sgfleet.scheduling import (
    PollingConfig,
    compute_due_tiers,
    compute_next_update_interval,
    effective_tier_intervals_seconds,
    has_pending_reimbursements,
    is_within_active_hours,
    seconds_until_next_active_window,
)


def test_smart_mode_adjusts_reimbursement_interval() -> None:
    cfg = PollingConfig(
        reimbursements_hours=4,
        reimbursements_smart_mode=True,
        balances_hours=12,
        details_hours=168,
    )

    # Without pending claims -> slows down to balances interval (12h)
    no_pending = effective_tier_intervals_seconds(cfg, has_pending_claims=False)
    assert no_pending[TIER_REIMBURSEMENTS] == 12 * 3600
    assert no_pending[TIER_BALANCES] == 12 * 3600
    assert no_pending[TIER_DETAILS] == 168 * 3600

    # With pending claims -> uses fast 4h interval
    with_pending = effective_tier_intervals_seconds(cfg, has_pending_claims=True)
    assert with_pending[TIER_REIMBURSEMENTS] == 4 * 3600
    assert with_pending[TIER_BALANCES] == 12 * 3600

    # Smart mode disabled -> always uses 4h interval
    cfg_no_smart = PollingConfig(
        reimbursements_hours=4,
        reimbursements_smart_mode=False,
        balances_hours=12,
    )
    assert effective_tier_intervals_seconds(cfg_no_smart, has_pending_claims=False)[TIER_REIMBURSEMENTS] == 4 * 3600


def test_details_never_option() -> None:
    cfg = PollingConfig(details_hours=0, balances_hours=12, reimbursements_hours=4)
    intervals = effective_tier_intervals_seconds(cfg, has_pending_claims=False)
    assert intervals[TIER_DETAILS] is None

    # Initial fetch (not in last_fetched) -> still due once at startup
    initial_due = compute_due_tiers(1000.0, {}, cfg, has_pending_claims=False)
    assert TIER_DETAILS in initial_due

    # Even 30 days later -> never due again automatically
    thirty_days = 30 * 86400.0
    later_due = compute_due_tiers(
        1000.0 + thirty_days,
        {TIER_DETAILS: 1000.0, TIER_BALANCES: 1000.0, TIER_REIMBURSEMENTS: 1000.0},
        cfg,
        has_pending_claims=False,
    )
    assert TIER_DETAILS not in later_due
    assert TIER_BALANCES in later_due
    assert TIER_REIMBURSEMENTS in later_due


def test_active_hours_and_quiet_window_deferral() -> None:
    cfg = PollingConfig(
        active_hours_enabled=True,
        active_hours_start="07:00:00",
        active_hours_end="21:00:00",
        jitter_fraction=0.15,
    )

    assert is_within_active_hours(datetime(2026, 11, 24, 10, 30), cfg) is True
    assert is_within_active_hours(datetime(2026, 11, 24, 6, 59), cfg) is False
    assert is_within_active_hours(datetime(2026, 11, 24, 21, 0), cfg) is False
    assert is_within_active_hours(datetime(2026, 11, 24, 2, 15), cfg) is False

    # At 23:00, next active window is 07:00 next day (8h) + 5..30 min soft offset
    rng = random.Random(42)
    wait = seconds_until_next_active_window(datetime(2026, 11, 24, 23, 0), cfg, rng=rng)
    assert 8 * 3600 + 300 <= wait <= 8 * 3600 + 1800

    # Active hours disabled -> always active
    cfg_disabled = PollingConfig(active_hours_enabled=False)
    assert is_within_active_hours(datetime(2026, 11, 24, 3, 0), cfg_disabled) is True


def test_jitter_stays_within_bounds_and_avoids_quiet_hours() -> None:
    cfg = PollingConfig(
        reimbursements_hours=4,
        reimbursements_smart_mode=True,
        balances_hours=12,
        details_hours=0,
        active_hours_enabled=True,
        active_hours_start="07:00:00",
        active_hours_end="21:00:00",
        jitter_fraction=0.15,
    )

    now_local = datetime(2026, 11, 24, 10, 0)
    now_mono = 5000.0
    last_fetched = {
        TIER_REIMBURSEMENTS: now_mono,
        TIER_BALANCES: now_mono,
        TIER_DETAILS: now_mono,
    }

    # With pending claim at 10:00 -> next poll ~4h (+/-15%: 3.4h..4.6h = 13:24..14:36, inside active hours)
    rng = random.Random(123)
    delta = compute_next_update_interval(
        now_local, now_mono, last_fetched, cfg, has_pending_claims=True, rng=rng
    )
    assert 4 * 3600 * 0.85 <= delta.total_seconds() <= 4 * 3600 * 1.15

    # Without pending claim at 14:00 -> 12h (+/-15%: 10.2h..13.8h) always lands overnight,
    # so it must automatically defer to next morning after 07:00!
    afternoon = datetime(2026, 11, 24, 14, 0)
    delta_quiet = compute_next_update_interval(
        afternoon, now_mono, last_fetched, cfg, has_pending_claims=False, rng=random.Random(123)
    )
    wake_time = afternoon + delta_quiet
    assert is_within_active_hours(wake_time, cfg) is True
    assert wake_time.day == 25
    assert wake_time.hour == 7
    assert 5 <= wake_time.minute <= 30


def test_has_pending_reimbursements_detection() -> None:
    assert has_pending_reimbursements([]) is False
    assert has_pending_reimbursements([[{"status": "Paid", "proposedActionText": "Completed"}]]) is False
    assert has_pending_reimbursements([[{"status": "Declined", "proposedActionText": "Cancelled"}]]) is False
    assert has_pending_reimbursements([[{"status": "In review", "proposedActionText": "In progress"}]]) is True
    assert has_pending_reimbursements([[{"status": "Awaiting balance"}]]) is True


def test_polling_config_from_options_coerces_dropdown_string() -> None:
    cfg = PollingConfig.from_options(
        {
            "reimbursements_interval_hours": 3,
            "reimbursements_smart_mode": True,
            "balances_interval_hours": 16,
            "details_interval_hours": "0",
            "active_hours_enabled": True,
            "active_hours_start": "08:00:00",
            "active_hours_end": "20:00:00",
        }
    )
    assert cfg.reimbursements_hours == 3.0
    assert cfg.balances_hours == 16.0
    assert cfg.details_hours == 0.0
    assert cfg.active_hours_start == "08:00:00"
    assert cfg.active_hours_end == "20:00:00"
