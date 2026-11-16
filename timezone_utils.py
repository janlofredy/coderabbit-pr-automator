"""
Timezone utility module for CodeRabbit PR Auto-Reviewer.

Provides timezone resolution based on environment variables (TZ, TIMEZONE),
defaulting to 'Asia/Manila', with helper functions for parsing, formatting,
and generating dates and timestamps across the entire application.
"""

import os
import zoneinfo
from datetime import datetime, timezone, timedelta
from typing import Optional, Union

DEFAULT_TIMEZONE = "Asia/Manila"

_tz_cache = {}
_ZONEINFO_LOWER_MAP = None


def _get_lower_map() -> dict:
    global _ZONEINFO_LOWER_MAP
    if _ZONEINFO_LOWER_MAP is None:
        try:
            _ZONEINFO_LOWER_MAP = {tz.lower(): tz for tz in zoneinfo.available_timezones()}
        except Exception:
            _ZONEINFO_LOWER_MAP = {}
    return _ZONEINFO_LOWER_MAP


def get_app_timezone() -> zoneinfo.ZoneInfo:
    """
    Returns the configured application timezone based on the TZ (or TIMEZONE) environment variable.
    Defaults to 'Asia/Manila'.
    Supports case-insensitivity (e.g. 'asia/manila' -> 'Asia/Manila') and falls back gracefully.
    """
    tz_env = os.environ.get("TZ") or os.environ.get("TIMEZONE") or DEFAULT_TIMEZONE
    tz_env = tz_env.strip()
    if tz_env in _tz_cache:
        return _tz_cache[tz_env]

    # 1. Direct lookup
    try:
        tz = zoneinfo.ZoneInfo(tz_env)
        _tz_cache[tz_env] = tz
        return tz
    except Exception:
        pass

    # 2. Case-insensitive match from available timezones
    lower_map = _get_lower_map()
    candidate = lower_map.get(tz_env.lower())
    if candidate:
        try:
            tz = zoneinfo.ZoneInfo(candidate)
            _tz_cache[tz_env] = tz
            return tz
        except Exception:
            pass

    # 3. Fallback to default (Asia/Manila)
    try:
        tz = zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)
        _tz_cache[tz_env] = tz
        return tz
    except Exception:
        pass

    # 4. Ultimate fallback to UTC
    _tz_cache[tz_env] = timezone.utc
    return timezone.utc


def get_app_timezone_name() -> str:
    """Returns the string identifier of the application timezone (e.g. 'Asia/Manila')."""
    tz = get_app_timezone()
    return getattr(tz, "key", str(tz))


def now_app_tz() -> datetime:
    """Returns datetime.now() in the configured application timezone."""
    return datetime.now(get_app_timezone())


def to_app_tz(dt: datetime) -> datetime:
    """Converts any datetime (naive or aware) to the configured application timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_app_timezone())


def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Safely parses an ISO datetime string into a timezone-aware datetime in the app timezone."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        clean = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return to_app_tz(dt)
    except Exception:
        return None


def format_friendly_datetime(dt_or_str: Union[datetime, str, None]) -> str:
    """Formats an ISO timestamp or datetime into 'YYYY-MM-DD HH:MM %Z' in the app timezone."""
    if dt_or_str is None:
        return "Unknown"

    if isinstance(dt_or_str, str):
        dt = parse_iso_datetime(dt_or_str)
        if not dt:
            return "Unknown"
    elif isinstance(dt_or_str, datetime):
        dt = to_app_tz(dt_or_str)
    else:
        return "Unknown"

    tz = get_app_timezone()
    tz_label = dt.strftime("%Z") or getattr(tz, "key", "UTC")
    return dt.strftime(f"%Y-%m-%d %H:%M {tz_label}")


def format_relative_time(dt_str: Optional[str], now: Optional[datetime] = None) -> str:
    """Formats an ISO timestamp into a human-friendly relative string."""
    dt = parse_iso_datetime(dt_str)
    if not dt:
        return "Unknown"
    if now is None:
        now = now_app_tz()
    elif now.tzinfo is None:
        now = to_app_tz(now)

    diff = int((now - dt).total_seconds())

    if diff < 0:
        return "Just now"
    if diff < 60:
        return f"{diff}s ago"
    minutes = diff // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    return dt.strftime("%Y-%m-%d")
