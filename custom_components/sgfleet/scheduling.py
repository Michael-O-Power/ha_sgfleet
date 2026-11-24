"""Pure tiered polling, smart reimbursement cadence, active-hours, and jitter logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
import random
from typing import Any

from .const import (
    DEFAULT_ACTIVE_HOURS_ENABLED,
    DEFAULT_ACTIVE_HOURS_END,
    DEFAULT_ACTIVE_HOURS_START,
    DEFAULT_BALANCES_INTERVAL_HOURS,
    DEFAULT_DETAILS_INTERVAL_HOURS,
    DEFAULT_REIMBURSEMENTS_INTERVAL_HOURS,
    DEFAULT_REIMBURSEMENTS_SMART_MODE,
    DETAILS_INTERVAL_NEVER,
    PENDING_REIMBURSEMENT_STATUSES,
    POLL_JITTER_FRACTION,
    TIER_BALANCES,
    TIER_DETAILS,
    TIER_REIMBURSEMENTS,
)

MIN_NEXT_POLL_SECONDS = 300  # Never schedule a poll faster than 5 minutes


@dataclass(frozen=True, slots=True)
class PollingConfig:
    """User-configured polling parameters for the three SG Fleet data tiers."""

    reimbursements_hours: float = DEFAULT_REIMBURSEMENTS_INTERVAL_HOURS
    reimbursements_smart_mode: bool = DEFAULT_REIMBURSEMENTS_SMART_MODE
    balances_hours: float = DEFAULT_BALANCES_INTERVAL_HOURS
    details_hours: float = DEFAULT_DETAILS_INTERVAL_HOURS  # 0 = Never (startup & manual only)
    active_hours_enabled: bool = DEFAULT_ACTIVE_HOURS_ENABLED
    active_hours_start: str = DEFAULT_ACTIVE_HOURS_START
    active_hours_end: str = DEFAULT_ACTIVE_HOURS_END
    jitter_fraction: float = POLL_JITTER_FRACTION

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> PollingConfig:
        """Build PollingConfig safely from Home Assistant config entry options."""
        from .const import (
            CONF_ACTIVE_HOURS_ENABLED,
            CONF_ACTIVE_HOURS_END,
            CONF_ACTIVE_HOURS_START,
            CONF_BALANCES_INTERVAL_HOURS,
            CONF_DETAILS_INTERVAL_HOURS,
            CONF_REIMBURSEMENTS_INTERVAL_HOURS,
            CONF_REIMBURSEMENTS_SMART_MODE,
        )

        return cls(
            reimbursements_hours=float(
                options.get(CONF_REIMBURSEMENTS_INTERVAL_HOURS, DEFAULT_REIMBURSEMENTS_INTERVAL_HOURS)
            ),
            reimbursements_smart_mode=bool(
                options.get(CONF_REIMBURSEMENTS_SMART_MODE, DEFAULT_REIMBURSEMENTS_SMART_MODE)
            ),
            balances_hours=float(
                options.get(CONF_BALANCES_INTERVAL_HOURS, DEFAULT_BALANCES_INTERVAL_HOURS)
            ),
            details_hours=float(
                options.get(CONF_DETAILS_INTERVAL_HOURS, DEFAULT_DETAILS_INTERVAL_HOURS)
            ),
            active_hours_enabled=bool(
                options.get(CONF_ACTIVE_HOURS_ENABLED, DEFAULT_ACTIVE_HOURS_ENABLED)
            ),
            active_hours_start=str(
                options.get(CONF_ACTIVE_HOURS_START, DEFAULT_ACTIVE_HOURS_START)
            ),
            active_hours_end=str(
                options.get(CONF_ACTIVE_HOURS_END, DEFAULT_ACTIVE_HOURS_END)
            ),
        )


def parse_clock_time(value: str, fallback: dt_time) -> dt_time:
    """Parse 'HH:MM' or 'HH:MM:SS' into a datetime.time."""
    try:
        parts = [int(p) for p in value.strip().split(":")]
        if len(parts) == 2:
            return dt_time(parts[0], parts[1], 0)
        if len(parts) >= 3:
            return dt_time(parts[0], parts[1], parts[2])
    except (ValueError, AttributeError):
        pass
    return fallback


def is_within_active_hours(now_local: datetime, config: PollingConfig) -> bool:
    """Return True if now_local falls within the configured active polling window."""
    if not config.active_hours_enabled:
        return True

    start = parse_clock_time(config.active_hours_start, dt_time(7, 0))
    end = parse_clock_time(config.active_hours_end, dt_time(21, 0))
    current = now_local.time().replace(tzinfo=None)

    if start == end:
        return True
    if start < end:
        return start <= current < end
    # Overnight window (e.g. 22:00 -> 06:00)
    return current >= start or current < end


def seconds_until_next_active_window(
    now_local: datetime,
    config: PollingConfig,
    *,
    rng: random.Random | None = None,
) -> float:
    """Compute seconds until the next active-hours start, plus a soft 5–30 min offset."""
    start = parse_clock_time(config.active_hours_start, dt_time(7, 0))
    candidate = now_local.replace(
        hour=start.hour,
        minute=start.minute,
        second=start.second,
        microsecond=0,
    )
    if candidate <= now_local:
        candidate += timedelta(days=1)

    rand = rng or random
    # Soft 5–30 min startup offset so we never hit Auth0 at 07:00:00 sharp
    soft_offset_secs = rand.uniform(300, 1800) if config.jitter_fraction > 0 else 0.0
    return max(MIN_NEXT_POLL_SECONDS, (candidate - now_local).total_seconds() + soft_offset_secs)


def has_pending_reimbursements(reimbursement_histories: list[list[dict[str, Any]]]) -> bool:
    """Return True if any vehicle has a reimbursement claim currently in review/pending."""
    for history in reimbursement_histories:
        for item in history:
            status = str(item.get("status") or "").strip().lower()
            action = str(item.get("proposedActionText") or "").strip().lower()
            if status in PENDING_REIMBURSEMENT_STATUSES or action in PENDING_REIMBURSEMENT_STATUSES:
                return True
    return False


def effective_tier_intervals_seconds(
    config: PollingConfig,
    *,
    has_pending_claims: bool,
) -> dict[str, float | None]:
    """Return effective polling interval in seconds for each tier (None = Never)."""
    balances_secs = max(3600.0, config.balances_hours * 3600.0)
    reimb_secs = max(1800.0, config.reimbursements_hours * 3600.0)
    if config.reimbursements_smart_mode and not has_pending_claims:
        # No pending claims: slow down reimbursement polling to match balances cadence
        reimb_secs = max(reimb_secs, balances_secs)

    details_secs: float | None = (
        None
        if config.details_hours <= DETAILS_INTERVAL_NEVER
        else max(3600.0, config.details_hours * 3600.0)
    )

    return {
        TIER_REIMBURSEMENTS: reimb_secs,
        TIER_BALANCES: balances_secs,
        TIER_DETAILS: details_secs,
    }


def compute_due_tiers(
    now_monotonic: float,
    last_fetched: dict[str, float],
    config: PollingConfig,
    *,
    has_pending_claims: bool,
    grace_seconds: float = 120.0,
) -> set[str]:
    """Return the set of tiers currently due for a scheduled refresh.

    A small grace window ensures tiers scheduled close together piggyback on the
    same Auth0/SG Fleet session instead of waking up twice back-to-back.
    """
    intervals = effective_tier_intervals_seconds(config, has_pending_claims=has_pending_claims)
    due: set[str] = set()

    for tier, interval_secs in intervals.items():
        prev = last_fetched.get(tier)
        if prev is None:
            # Never fetched yet (initial startup) — always due once
            due.add(tier)
            continue
        if interval_secs is None:
            # Configured as "Never" after initial fetch
            continue
        if (now_monotonic - prev) >= max(0.0, interval_secs - grace_seconds):
            due.add(tier)

    return due


def compute_next_update_interval(
    now_local: datetime,
    now_monotonic: float,
    last_fetched: dict[str, float],
    config: PollingConfig,
    *,
    has_pending_claims: bool,
    rng: random.Random | None = None,
) -> timedelta:
    """Compute the next coordinator update_interval with +/-15% jitter & active-hours alignment."""
    intervals = effective_tier_intervals_seconds(config, has_pending_claims=has_pending_claims)
    remaining_candidates: list[float] = []

    for tier, interval_secs in intervals.items():
        if interval_secs is None:
            continue
        prev = last_fetched.get(tier)
        if prev is None:
            remaining_candidates.append(MIN_NEXT_POLL_SECONDS)
        else:
            elapsed = max(0.0, now_monotonic - prev)
            remaining = max(float(MIN_NEXT_POLL_SECONDS), interval_secs - elapsed)
            remaining_candidates.append(remaining)

    base_wait = min(remaining_candidates) if remaining_candidates else 12 * 3600.0

    # Apply symmetric +/- jitter_fraction (e.g. 0.85x to 1.15x)
    rand = rng or random
    if config.jitter_fraction > 0:
        jitter_mult = rand.uniform(1.0 - config.jitter_fraction, 1.0 + config.jitter_fraction)
        wait_secs = max(float(MIN_NEXT_POLL_SECONDS), base_wait * jitter_mult)
    else:
        wait_secs = max(float(MIN_NEXT_POLL_SECONDS), base_wait)

    # If the target wake time falls outside active hours, push it to the next active window start
    projected_local = now_local + timedelta(seconds=wait_secs)
    if config.active_hours_enabled and not is_within_active_hours(projected_local, config):
        if not is_within_active_hours(now_local, config):
            wait_secs = seconds_until_next_active_window(now_local, config, rng=rand)
        else:
            extra = seconds_until_next_active_window(projected_local, config, rng=rand)
            wait_secs = wait_secs + extra

    return timedelta(seconds=round(wait_secs))
