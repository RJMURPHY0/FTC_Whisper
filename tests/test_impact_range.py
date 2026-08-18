"""Range-scoped impact snapshot.

The "Your impact" dropdown (Today / This week / This month / This year / All
time) must scope ALL three cards — time saved, dictation speed, day streak —
plus the words count, not just the footer words line. StatsStore.snapshot(rng)
does the scoping; rng='all' is lifetime and the ONLY window that folds in the
collapsed `carry` totals from trimmed old days.

Data and "today" are fixed (no datetime.now()) so the windows are deterministic.
2026-08-12 is a Wednesday: week starts Mon 2026-08-10, month 2026-08-01, year
2026-01-01.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stats
from stats import StatsStore


_TODAY = datetime.date(2026, 8, 12)  # Wednesday


def _store(days, carry_words=0):
    """A StatsStore backed by injected data — never touches the real file
    (snapshot()/_user_blob only load from disk when _data is None)."""
    s = StatsStore()
    s._user_key = "local"
    s._data = {"version": 1, "users": {"local": {
        "days": dict(days), "seeded": True,
        "carry_saved_min": 0.0, "carry_words": int(carry_words)}}}
    return s


class RangeScopingTests(unittest.TestCase):
    def setUp(self):
        self._real_today = stats._today
        stats._today = lambda: _TODAY
        self.addCleanup(lambda: setattr(stats, "_today", self._real_today))

    # One dictation per bucket, each in a different window, plus carry.
    DAYS = {
        "2026-08-12": {"w": 100, "s": 40.0, "v": 60.0, "vw": 100},  # today
        "2026-08-11": {"w": 50,  "s": 20.0},                        # this week
        "2026-08-05": {"w": 30,  "s": 12.0},                        # this month
        "2026-07-15": {"w": 20,  "s": 8.0},                         # this year
        "2025-12-20": {"w": 10,  "s": 4.0},                         # last year
    }

    def test_words_scope_to_the_window(self):
        s = _store(self.DAYS, carry_words=1000)
        self.assertEqual(100, s.snapshot("today")["total_words"])
        self.assertEqual(150, s.snapshot("week")["total_words"])
        self.assertEqual(180, s.snapshot("month")["total_words"])
        self.assertEqual(200, s.snapshot("year")["total_words"])
        # 'all' alone folds in the carry from trimmed old days.
        self.assertEqual(100 + 50 + 30 + 20 + 10 + 1000,
                         s.snapshot("all")["total_words"])

    def test_active_days_scope_to_the_window(self):
        s = _store(self.DAYS, carry_words=1000)
        self.assertEqual(1, s.snapshot("today")["active_in_range"])
        self.assertEqual(2, s.snapshot("week")["active_in_range"])
        self.assertEqual(3, s.snapshot("month")["active_in_range"])
        self.assertEqual(4, s.snapshot("year")["active_in_range"])
        self.assertEqual(5, s.snapshot("all")["active_in_range"])  # carry isn't a day

    def test_time_saved_grows_with_the_window(self):
        s = _store(self.DAYS)
        saved = [s.snapshot(r)["saved_minutes"]
                 for r in ("today", "week", "month", "year", "all")]
        self.assertEqual(saved, sorted(saved))          # monotonic
        self.assertLess(saved[0], saved[-1])            # and strictly wider

    def test_all_matches_the_default_and_is_lifetime(self):
        # Default arg is 'all', and it reproduces the pre-windowing lifetime
        # numbers (existing callers/tests rely on this).
        s = _store(self.DAYS, carry_words=1000)
        self.assertEqual(s.snapshot(), s.snapshot("all"))
        streak = s.snapshot()  # streak is always lifetime, never windowed
        self.assertEqual(sorted(self.DAYS), streak["active_days"])

    def test_thin_window_falls_back_to_the_lifetime_speed(self):
        # Today alone hasn't cleared the 50-word bar, so the speed card shows
        # the user's lifetime rate rather than dropping to the nominal 160.
        s = _store({
            "2026-08-12": {"w": 10, "s": 6.0, "v": 5.0, "vw": 10},     # thin
            "2026-06-01": {"w": 200, "s": 80.0, "v": 100.0, "vw": 200},  # measured
        })
        snap = s.snapshot("today")
        self.assertEqual(0, snap["avg_wpm_window"])     # window under threshold
        self.assertGreater(snap["lifetime_wpm"], 0)     # lifetime clears it
        self.assertEqual(snap["lifetime_wpm"], snap["avg_wpm"])  # so it is shown

    def test_unknown_range_is_treated_as_lifetime(self):
        s = _store(self.DAYS, carry_words=1000)
        self.assertEqual(s.snapshot("all")["total_words"],
                         s.snapshot("bogus")["total_words"])


if __name__ == "__main__":
    unittest.main()
