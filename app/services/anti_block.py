"""Gramma — Anti-Block Protection (human-like behavior + error handling).

Implements the production hardening requirements:

* human-like pauses between actions (1.5–4.5 s) drawn from a per-account
  rate profile (slow ≈5 s / medium ≈3 s / fast ≈1.5 s averages);
* a random 5–15 min rest break every few hours (≈ every ~80 actions);
* response diversity — never send identical comment replies back-to-back:
  the reply text is lightly varied and at least ``MIN_REPLY_VARIANTS``
  pre-written phrasings exist per reply template;
* Instagram error classification → the caller stops replying and an alert
  is raised (codes 190 / 4 / 17 / 368 / 1200);
* smart backoff ladder — 5m → 15m → 1h → 24h, upgrades only after a fresh
  error and resets only after a success.

All caps and numbers come from :mod:`app.services.limits` (single source).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import limits
from app.services.reply_limits import DailyReplyCounter

logger = logging.getLogger("gramma.anti_block")

# Test/simulation hook: when set, all anti-block sleeps become no-ops so
# bulk simulations (e.g. 1000 comments/day) run in seconds instead of the
# real human-paced minutes. NEVER set this in production.
DISABLE_DELAYS = os.environ.get("GRAMMA_DISABLE_ANTIBLOCK_DELAYS") == "1"

# Pre-written Persian openers/closers used to vary replies so no two
# consecutive replies are byte-identical (diversity requirement).
_VARIANT_PREFIXES = {
    "fa": [
        "سلام! ",
        "سلام، ",
        "درود! ",
        "سلام عزیز، ",
        "سلام دوست عزیز، ",
    ],
    "en": [
        "Hi! ",
        "Hello! ",
        "Hey there, ",
        "Hi there, ",
        "Hello, ",
    ],
}
_VARIANT_SUFFIXES = {
    "fa": [
        " 🙏",
        " ✨",
        " 🌹",
        " 😊",
        "",
    ],
    "en": [
        "Thanks!",
        "Have a nice day!",
        "",
        "",
        "",
    ],
}


def _tehran_hour(now_utc: datetime | None = None) -> int:
    from zoneinfo import ZoneInfo

    from app.core.config import get_settings

    tz = ZoneInfo(get_settings().timezone)
    return (now_utc or datetime.now(timezone.utc)).astimezone(tz).hour


def business_hour_weight_factor(now_utc: datetime | None = None) -> float:
    """1.0 inside the active window (8–23), ~0.35 outside; peaks 10–22."""
    hour = _tehran_hour(now_utc)
    if limits.BUSINESS_HOURS_PEAK_START_H <= hour < limits.BUSINESS_HOURS_PEAK_END_H:
        return 1.0
    if limits.BUSINESS_HOURS_ACTIVE_START_H <= hour < limits.BUSINESS_HOURS_ACTIVE_END_H:
        return 0.8
    return 0.35


def draw_rate_profile() -> str:
    """Pick a daily rate profile per account (slow/medium/fast)."""
    return random.choice(limits.RATE_PROFILES)


def profile_average_seconds(profile: str) -> float:
    return {
        "slow": limits.RATE_PROFILE_SLOW_AVG_S,
        "medium": limits.RATE_PROFILE_MEDIUM_AVG_S,
        "fast": limits.RATE_PROFILE_FAST_AVG_S,
    }.get(profile, limits.RATE_PROFILE_MEDIUM_AVG_S)


def human_delay(profile: str) -> float:
    """Human-like jitter around the profile's average (1.5–4.5 s base)."""
    avg = profile_average_seconds(profile)
    base = max(limits.REPLY_MIN_DELAY_S, min(limits.REPLY_MAX_DELAY_S, avg))
    jitter = random.uniform(0.6, 1.4)
    return round(max(0.5, base * jitter), 2)


def sleep_between_actions(profile: str) -> float:
    """The pause applied between two queued actions."""
    return round(
        random.uniform(limits.QUEUE_HUMAN_DELAY_MIN_S, limits.QUEUE_HUMAN_DELAY_MAX_S),
        2,
    )


async def human_delay_wait(profile: str) -> float:
    """Sleep for a human-like jitter; returns seconds slept."""
    if DISABLE_DELAYS:
        return 0.0
    d = human_delay(profile)
    await asyncio.sleep(d)
    return d


async def maybe_rest_break(counter: DailyReplyCounter) -> float | None:
    """Every ~REST_BREAK_EVERY_MIN_ACTIONS actions, schedule a rest break.

    Returns the number of seconds to rest (5–15 min), or None.
    """
    if counter.rest_at_action is None or counter.rest_at_action <= 0:
        counter.rest_at_action = random.randint(
            limits.REST_BREAK_EVERY_MIN_ACTIONS - 15,
            limits.REST_BREAK_EVERY_MIN_ACTIONS + 15,
        )
        return None
    since_last = 0.0
    if counter.last_action_at is not None and counter.last_action_at.tzinfo is None:
        counter.last_action_at = counter.last_action_at.replace(tzinfo=timezone.utc)
    if counter.last_action_at is not None:
        since_last = (datetime.now(timezone.utc) - counter.last_action_at).total_seconds()
    if since_last >= limits.REST_BREAK_MIN_S * 0.6:
        return None  # a long gap already counts as a rest
    if (counter.replies_count or 0) >= counter.rest_at_action:
        return random.randint(limits.REST_BREAK_MIN_S, limits.REST_BREAK_MAX_S)
    return None


async def await_rest_if_needed(counter: DailyReplyCounter, profile: str) -> None:
    """Sleep through a scheduled rest break, then reset the cadence."""
    rest = await maybe_rest_break(counter)
    if rest and DISABLE_DELAYS:
        rest = None
    if rest:
        logger.info("anti-block rest break %.0fs (account %s)", rest, counter.account_id)
        await asyncio.sleep(rest)
        counter.rest_at_action = random.randint(
            limits.REST_BREAK_EVERY_MIN_ACTIONS - 15,
            limits.REST_BREAK_EVERY_MIN_ACTIONS + 15,
        )
        _ = profile


def vary_reply(text: str, locale: str = "fa", *, only_prefix_suffix: bool = False) -> str:
    """Return a variation of `text` so consecutive replies differ.

    For short canned replies we only swap the prefix/suffix (keeping the
    meaning); for longer text we also pick a synonym swap on a few common
    words. ``only_prefix_suffix`` keeps the transform conservative.
    """
    if not text:
        return text
    prefixes = _VARIANT_PREFIXES.get(locale, _VARIANT_PREFIXES["fa"])
    suffixes = _VARIANT_SUFFIXES.get(locale, _VARIANT_SUFFIXES["fa"])
    out = text
    if not only_prefix_suffix:
        swaps = {
            "سلام": random.choice(["سلام", "درود", "سلام عزیز"]),
            "متشکر": random.choice(["متشکر", "سپاسگزار", "ممنون"]),
        }
        for src, dst in swaps.items():
            if src in out:
                out = out.replace(src, dst, 1)
                break
    if not any(out.startswith(p) for p in prefixes) and len(out) < 240:
        out = random.choice(prefixes) + out
    if out.endswith(tuple(suffixes)):
        return out
    return out + random.choice(suffixes)


def classify_instagram_error(error: dict | Exception | None) -> str | None:
    """Map a Graph API error payload to a safety alert type (or None)."""
    err = error
    if isinstance(error, Exception):
        err = None
    if isinstance(err, dict):
        code = str(
            err.get("code")
            or (err.get("error") or {}).get("code")
            or (err.get("error") or {}).get("type")
            or ""
        )
        msg = str(err.get("message") or (err.get("error") or {}).get("message") or "")
        subcode = str(
            err.get("error_subcode") or (err.get("error") or {}).get("error_subcode") or ""
        )
        # rate-limit payloads carry code 4 / 17 or subcode markers
        if subcode in ("2446037", "2446079", "2446038", "2446053"):
            return limits.BLOCK_ERROR_CODES.get("4")  # rate_limit
        if code in limits.BLOCK_ERROR_CODES:
            return limits.BLOCK_ERROR_CODES[code]
        low = (msg or "").lower()
        if any(w in low for w in ("rate limit", "too many", "spam")):
            return "rate_limit"
        if "temporarily blocked" in low or "1683686" in str(err):
            return "temporarily_blocked"
        if "suspicious" in low or "1200" in code:
            return "suspicious_activity"
        if code in ("190",) or "oauth" in low or "token" in low and "expired" in low:
            return "oauth_exception"
    return None


def error_is_blocking(error: dict | Exception | None) -> bool:
    return classify_instagram_error(error) is not None


def next_backoff_until(current: datetime | None, runs: int) -> tuple[datetime, int]:
    """Advance the backoff ladder: 5m → 15m → 1h → 24h (cap at last step)."""
    idx = min(max(0, runs), len(limits.BACKOFF_STEPS_S) - 1)
    wait = limits.BACKOFF_STEPS_S[idx]
    return datetime.now(timezone.utc) + timedelta(seconds=wait), idx + 1


def in_backoff(counter: DailyReplyCounter | None, now_utc: datetime | None = None) -> bool:
    if counter is None or counter.backoff_until is None:
        return False
    now = now_utc or datetime.now(timezone.utc)
    if counter.backoff_until.tzinfo is None:
        counter.backoff_until = counter.backoff_until.replace(tzinfo=timezone.utc)
    return now < counter.backoff_until


async def mark_success(counter: DailyReplyCounter | None) -> None:
    """A successful reply clears the backoff ladder."""
    if counter is not None:
        counter.backoff_until = None
        counter.backoff_runs = 0


async def mark_error(counter: DailyReplyCounter | None) -> datetime:
    """Record a failure and return the new backoff_until timestamp."""
    until = datetime.now(timezone.utc)
    if counter is not None:
        until, runs = next_backoff_until(counter.backoff_until, counter.backoff_runs or 0)
        counter.backoff_until = until
        counter.backoff_runs = runs
    return until
