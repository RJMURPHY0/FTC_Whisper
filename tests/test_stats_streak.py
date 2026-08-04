"""Weekend-aware day streak and words-by-range bucketing.

_compute_streak(): Monday-Friday are required days; Saturday/Sunday are
optional and bridge the streak (a missed weekend day never breaks the run,
a used one counts towards the number). Today, and any trailing weekend not
yet used, is a grace period rather than a break — the same "streak alive if
it ended yesterday" grace the old plain-consecutive-days loop gave.

_words_by_range(): buckets a user's daily word counts into today/week
(Monday start)/month/year/all-time totals, folding carry_words (words from
days trimmed out of the per-day dict) into "all" only.

All dates are fixed/constructed — no datetime.now() — so these tests are
deterministic regardless of when they run. 2026-08-03 is a Monday.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stats import _compute_streak, _is_weekend, _words_by_range


def _d(month: int, day: int, year: int = 2026) -> datetime.date:
    return datetime.date(year, month, day)


def _used_set(dates):
    """Build a used(date)->bool callable from an iterable of dates."""
    s = set(dates)
    return lambda d: d in s


class IsWeekendTests(unittest.TestCase):
    def test_weekdays_are_not_weekend(self):
        # 2026-08-03 .. 08-07 is Mon .. Fri.
        for day in range(3, 8):
            self.assertFalse(_is_weekend(_d(8, day)), f"08-{day:02d} should be a weekday")

    def test_weekend_days_are_weekend(self):
        self.assertTrue(_is_weekend(_d(8, 8)))   # Saturday
        self.assertTrue(_is_weekend(_d(8, 9)))   # Sunday


class ComputeStreakTests(unittest.TestCase):
    def test_full_working_week_ending_friday(self):
        # Mon-Fri all used, today is Friday -> streak 5.
        used = _used_set([_d(8, 3), _d(8, 4), _d(8, 5), _d(8, 6), _d(8, 7)])
        self.assertEqual(_compute_streak(used, _d(8, 7)), 5)

    def test_weekend_bridges_when_monday_used(self):
        # Used Thu+Fri, weekend NOT used, today Monday and used -> bridges:
        # streak 3 (Thu, Fri, Mon) — the unused Sat/Sun never breaks it.
        used = _used_set([_d(8, 6), _d(8, 7), _d(8, 10)])
        self.assertEqual(_compute_streak(used, _d(8, 10)), 3)

    def test_weekend_grace_when_monday_not_used_yet(self):
        # Used Thu+Fri, today Monday NOT used yet -> streak 2 (grace period
        # for today, weekend bridged same as above).
        used = _used_set([_d(8, 6), _d(8, 7)])
        self.assertEqual(_compute_streak(used, _d(8, 10)), 2)

    def test_missed_weekday_breaks_streak(self):
        # Used Mon+Wed, NOT Tue (a required weekday) -> today Wed sees the
        # break at Tuesday: streak 1.
        used = _used_set([_d(8, 3), _d(8, 5)])
        self.assertEqual(_compute_streak(used, _d(8, 5)), 1)

    def test_used_saturday_counts(self):
        used = _used_set([_d(8, 8)])
        self.assertEqual(_compute_streak(used, _d(8, 8)), 1)

    def test_zero_when_no_grace_and_nothing_used(self):
        # Today is a weekday, not used, nothing before it used either ->
        # the very first non-excused miss ends the walk-back at 0.
        used = _used_set([])
        self.assertEqual(_compute_streak(used, _d(8, 5)), 0)

    def test_past_weekday_gap_ends_walkback_before_reaching_older_streak(self):
        # An older run (last week) must not leak through a broken weekday.
        used = _used_set([_d(7, 27), _d(7, 28), _d(7, 29), _d(7, 30), _d(7, 31),
                           # Mon-Fri prior week used, but this week's Monday
                           # (08-03) is a no-show and today is Wednesday.
                           _d(8, 5)])
        self.assertEqual(_compute_streak(used, _d(8, 5)), 1)


class WordsByRangeTests(unittest.TestCase):
    def test_buckets_across_today_week_month_year(self):
        today = _d(8, 5)  # Wednesday
        days = {
            "2026-08-05": {"w": 100},   # today
            "2026-08-04": {"w": 50},    # this week (Tue)
            "2026-08-03": {"w": 25},    # this week (Mon, week start)
            "2026-08-01": {"w": 40},    # earlier this month (Sat), not this week
            "2026-01-10": {"w": 10},    # earlier this year
            "2025-12-31": {"w": 999},   # last year — excluded from year
        }
        totals = _words_by_range(days, carry_words=0, today=today)
        self.assertEqual(totals["today"], 100)
        self.assertEqual(totals["week"], 100 + 50 + 25)
        self.assertEqual(totals["month"], 100 + 50 + 25 + 40)
        self.assertEqual(totals["year"], 100 + 50 + 25 + 40 + 10)
        self.assertEqual(totals["all"], 100 + 50 + 25 + 40 + 10 + 999)

    def test_carry_words_flows_into_all_only(self):
        today = _d(8, 5)
        days = {"2026-08-05": {"w": 10}}
        totals = _words_by_range(days, carry_words=500, today=today)
        self.assertEqual(totals["today"], 10)
        self.assertEqual(totals["week"], 10)
        self.assertEqual(totals["month"], 10)
        self.assertEqual(totals["year"], 10)
        self.assertEqual(totals["all"], 510)

    def test_future_dated_row_excluded_from_windows_but_not_all(self):
        today = _d(8, 5)
        days = {
            "2026-08-05": {"w": 10},
            "2026-12-25": {"w": 77},  # future — never counts in a window
        }
        totals = _words_by_range(days, carry_words=0, today=today)
        self.assertEqual(totals["today"], 10)
        self.assertEqual(totals["week"], 10)
        self.assertEqual(totals["month"], 10)
        self.assertEqual(totals["year"], 10)
        self.assertEqual(totals["all"], 87)

    def test_malformed_date_key_is_skipped_not_fatal(self):
        today = _d(8, 5)
        days = {"2026-08-05": {"w": 10}, "not-a-date": {"w": 5}}
        totals = _words_by_range(days, carry_words=0, today=today)
        self.assertEqual(totals["today"], 10)
        self.assertEqual(totals["all"], 10)

    def test_week_start_is_monday(self):
        # Today Sunday 08-09: the week window must reach back to Monday
        # 08-03, not just the last 7 days from an arbitrary anchor.
        today = _d(8, 9)
        days = {"2026-08-03": {"w": 1}, "2026-08-02": {"w": 1000}}
        totals = _words_by_range(days, carry_words=0, today=today)
        self.assertEqual(totals["week"], 1)


if __name__ == "__main__":
    unittest.main()
