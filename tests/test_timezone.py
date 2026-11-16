import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from timezone_utils import (
    DEFAULT_TIMEZONE,
    get_app_timezone,
    get_app_timezone_name,
    now_app_tz,
    to_app_tz,
    parse_iso_datetime,
    format_friendly_datetime,
    format_relative_time,
    _tz_cache,
)


class TestTimezoneUtils(unittest.TestCase):
    def setUp(self):
        # Clear cache and environment for predictable testing
        _tz_cache.clear()
        self.orig_tz = os.environ.get("TZ")
        self.orig_timezone = os.environ.get("TIMEZONE")
        if "TZ" in os.environ:
            del os.environ["TZ"]
        if "TIMEZONE" in os.environ:
            del os.environ["TIMEZONE"]

    def tearDown(self):
        _tz_cache.clear()
        if self.orig_tz is not None:
            os.environ["TZ"] = self.orig_tz
        elif "TZ" in os.environ:
            del os.environ["TZ"]

        if self.orig_timezone is not None:
            os.environ["TIMEZONE"] = self.orig_timezone
        elif "TIMEZONE" in os.environ:
            del os.environ["TIMEZONE"]

    def test_default_timezone_is_asia_manila(self):
        self.assertEqual(DEFAULT_TIMEZONE, "Asia/Manila")
        tz = get_app_timezone()
        self.assertEqual(get_app_timezone_name(), "Asia/Manila")

    def test_timezone_from_tz_env(self):
        os.environ["TZ"] = "America/New_York"
        _tz_cache.clear()
        self.assertEqual(get_app_timezone_name(), "America/New_York")

    def test_timezone_from_timezone_env(self):
        os.environ["TIMEZONE"] = "Europe/London"
        _tz_cache.clear()
        self.assertEqual(get_app_timezone_name(), "Europe/London")

    def test_timezone_case_insensitivity(self):
        os.environ["TZ"] = "asia/manila"
        _tz_cache.clear()
        tz = get_app_timezone()
        now = datetime.now(tz)
        # Should resolve to Asia/Manila offset (+08:00)
        self.assertEqual(now.utcoffset(), timedelta(hours=8))

    def test_timezone_invalid_fallback(self):
        os.environ["TZ"] = "Nonexistent/Zone_12345"
        _tz_cache.clear()
        tz = get_app_timezone()
        # Should gracefully fall back to Asia/Manila
        now = datetime.now(tz)
        self.assertEqual(now.utcoffset(), timedelta(hours=8))

    def test_now_app_tz(self):
        os.environ["TZ"] = "Asia/Manila"
        _tz_cache.clear()
        now = now_app_tz()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), timedelta(hours=8))

    def test_to_app_tz(self):
        os.environ["TZ"] = "Asia/Manila"
        _tz_cache.clear()
        # Naive datetime is treated as UTC and converted to Asia/Manila (+8)
        naive_utc = datetime(2026, 11, 16, 8, 30, 0)
        aware = to_app_tz(naive_utc)
        self.assertEqual(aware.hour, 16)
        self.assertEqual(aware.minute, 30)

        # Aware UTC datetime
        aware_utc = datetime(2026, 11, 16, 8, 30, 0, tzinfo=timezone.utc)
        aware = to_app_tz(aware_utc)
        self.assertEqual(aware.hour, 16)
        self.assertEqual(aware.minute, 30)

    def test_parse_iso_datetime(self):
        os.environ["TZ"] = "Asia/Manila"
        _tz_cache.clear()

        # Parse ISO with 'Z'
        dt1 = parse_iso_datetime("2026-11-16T08:00:00Z")
        self.assertIsNotNone(dt1)
        self.assertEqual(dt1.hour, 16)
        self.assertEqual(dt1.utcoffset(), timedelta(hours=8))

        # Parse ISO with offset
        dt2 = parse_iso_datetime("2026-11-16T16:00:00+08:00")
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.hour, 16)

        # Parse invalid
        self.assertIsNone(parse_iso_datetime(None))
        self.assertIsNone(parse_iso_datetime(""))
        self.assertIsNone(parse_iso_datetime("not-a-datetime"))

    def test_format_friendly_datetime(self):
        os.environ["TZ"] = "Asia/Manila"
        _tz_cache.clear()

        formatted = format_friendly_datetime("2026-11-16T08:30:00Z")
        self.assertTrue(formatted.startswith("2026-11-16 16:30"))
        # Timezone label present
        self.assertTrue(len(formatted.split()) >= 3)

        # None or invalid input
        self.assertEqual(format_friendly_datetime(None), "Unknown")
        self.assertEqual(format_friendly_datetime("invalid"), "Unknown")

    def test_format_relative_time(self):
        os.environ["TZ"] = "Asia/Manila"
        _tz_cache.clear()

        now = now_app_tz()
        past_5m = (now - timedelta(minutes=5)).isoformat()
        past_2h = (now - timedelta(hours=2)).isoformat()
        past_3d = (now - timedelta(days=3)).isoformat()

        self.assertEqual(format_relative_time(past_5m, now), "5m ago")
        self.assertEqual(format_relative_time(past_2h, now), "2h ago")
        self.assertEqual(format_relative_time(past_3d, now), "3d ago")
        self.assertEqual(format_relative_time(None), "Unknown")


if __name__ == "__main__":
    unittest.main()
