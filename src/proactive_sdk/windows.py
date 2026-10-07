"""Local-time delivery windows (SPEC §10.4).

Pure wall-clock math for the two windows the product reasons about: the
user's quiet hours and the local day boundary the daily quota counts
against. Extracted from the policy layer so the policy engine and the
store's deterministic reminder admission compute *the same* window
without an import cycle (``policy`` imports ``store``; ``store`` must not
import ``policy``).

No I/O, no network, no store access.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .contracts import ErrorCode, PASError

__all__ = ["quiet_window", "quiet_end_ms", "local_day_start_ms", "local_day_end_ms"]


def quiet_window(policy: dict[str, Any]) -> tuple[tuple[int, int], Any] | None:
    """Parse a delivery_policy quiet-hours block.

    Either inline (``quiet_hours_start`` / ``quiet_hours_end`` /
    ``quiet_hours_timezone``) or nested (``quiet_hours``: start, end,
    timezone). Returns ``((start_min, end_min), tzinfo)`` or None when the
    job configures no window.
    """
    start = policy.get("quiet_hours_start")
    end = policy.get("quiet_hours_end")
    tzname = policy.get("quiet_hours_timezone") or policy.get("timezone")
    block = policy.get("quiet_hours")
    if isinstance(block, dict):
        start = block.get("start")
        end = block.get("end")
        tzname = block.get("timezone") or tzname
    if not (isinstance(start, str) and isinstance(end, str) and isinstance(tzname, str)):
        return None

    def _minutes(value: str) -> int:
        parts = value.split(":")
        if len(parts) != 2 or not all(p.isdigit() and len(p) == 2 for p in parts):
            raise PASError(ErrorCode.INVALID_CONFIG, f"quiet-hours time {value!r} must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise PASError(ErrorCode.INVALID_CONFIG, f"quiet-hours time {value!r} out of range")
        return hour * 60 + minute

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tzname)
    except Exception:
        raise PASError(
            ErrorCode.INVALID_CONFIG, f"quiet-hours timezone {tzname!r} invalid"
        ) from None
    return (_minutes(start), _minutes(end)), tz


def quiet_end_ms(policy: dict[str, Any], now_ms: int) -> int | None:
    """Epoch ms of the end of the quiet window covering ``now_ms``, or
    None when ``now_ms`` is outside quiet hours. Overnight windows
    (start > end) are handled on wall-clock local time; DST shifts move
    the boundary with the wall clock, which is the semantics users expect
    from a quiet-hours setting."""
    parsed = quiet_window(policy)
    if parsed is None:
        return None
    (start_min, end_min), tz = parsed
    local_now = datetime.fromtimestamp(now_ms / 1000, tz=tz)
    minute_of_day = local_now.hour * 60 + local_now.minute
    if start_min == end_min:
        return None  # a full-day window is a configuration error; treat as no window
    if start_min < end_min:
        if not (start_min <= minute_of_day < end_min):
            return None
        end_dt = local_now.replace(hour=end_min // 60, minute=end_min % 60, second=0, microsecond=0)
    else:
        in_evening = minute_of_day >= start_min
        in_morning = minute_of_day < end_min
        if not (in_evening or in_morning):
            return None
        day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = day + timedelta(days=1 if in_evening else 0)
        end_dt = end_dt.replace(hour=end_min // 60, minute=end_min % 60)
    return int(end_dt.timestamp() * 1000)


def local_day_start_ms(tzname: str | None, now_ms: int) -> int:
    """Epoch ms of the local day start used for the daily quota."""
    if not tzname:
        return now_ms - (now_ms % 86_400_000)
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tzname)
    except Exception:
        raise PASError(ErrorCode.INVALID_CONFIG, f"quota timezone {tzname!r} invalid") from None
    local_now = datetime.fromtimestamp(now_ms / 1000, tz=tz)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day_start.timestamp() * 1000)


def local_day_end_ms(tzname: str | None, now_ms: int) -> int:
    """Epoch ms of the *next* local day start (the end of the local day
    containing ``now_ms``). Re-resolved from the calendar rather than by
    adding 86 400 000 ms, so a DST transition cannot shift the boundary."""
    if not tzname:
        return local_day_start_ms(None, now_ms) + 86_400_000
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tzname)
    except Exception:
        raise PASError(ErrorCode.INVALID_CONFIG, f"quota timezone {tzname!r} invalid") from None
    local_now = datetime.fromtimestamp(now_ms / 1000, tz=tz)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = (day_start + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(next_day.timestamp() * 1000)
