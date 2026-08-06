"""AI-refine time saving, and the raw numbers the breakdown panel shows.

The saving is the manual round trip (switch to a chat assistant, paste, type
the instruction, wait, copy back, paste over) minus what the refine ACTUALLY
took, measured. These tests pin that it stays computable from a day's
aggregates — the per-day record only stores counts and totals, so summing per
refinement and summing the aggregates must agree, or the Time saved card and
its breakdown would disagree with each other.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stats
from stats import (TYPING_WPM, _best_streak, _refine_saved_minutes,
                   refine_manual_seconds, refine_saved_seconds)


class RefineSavedSecondsTests(unittest.TestCase):
    def test_preset_refine_uses_the_default_instruction_length(self):
        # No instruction typed (user pressed Fix All) — by hand they would still
        # have had to say what they wanted, so the default word count applies.
        self.assertEqual(refine_manual_seconds(0),
                         refine_manual_seconds(stats.REFINE_DEFAULT_PROMPT_WORDS))

    def test_saving_is_manual_minus_measured(self):
        self.assertAlmostEqual(refine_manual_seconds(10) - 12.0,
                               refine_saved_seconds(12.0, 10))

    def test_a_slow_refine_never_reads_as_negative(self):
        self.assertEqual(0.0, refine_saved_seconds(600.0, 5))

    def test_longer_instructions_raise_the_manual_cost(self):
        # Typing more words takes longer by hand, so the saving grows with the
        # instruction length for the same measured in-app time.
        self.assertGreater(refine_saved_seconds(10.0, 40),
                           refine_saved_seconds(10.0, 4))

    def test_instruction_typing_is_priced_at_the_typing_speed(self):
        delta = refine_manual_seconds(TYPING_WPM * 2) - refine_manual_seconds(0)
        expected = ((TYPING_WPM * 2) / float(TYPING_WPM)) * 60.0 - (
            stats.REFINE_DEFAULT_PROMPT_WORDS / float(TYPING_WPM)) * 60.0
        self.assertAlmostEqual(expected, delta)


class RefineAggregateTests(unittest.TestCase):
    """Per-day aggregates must give the same answer as per-refinement maths."""

    def test_aggregate_matches_the_sum_of_individual_refinements(self):
        refines = [(9.0, 6), (14.5, 12), (7.25, 0)]
        per_item = sum(refine_saved_seconds(e, w) for e, w in refines) / 60.0
        total_secs = sum(e for e, _w in refines)
        total_words = sum(w or stats.REFINE_DEFAULT_PROMPT_WORDS
                          for _e, w in refines)
        self.assertAlmostEqual(
            per_item, _refine_saved_minutes(len(refines), total_secs, total_words),
            places=6)

    def test_no_refinements_is_no_saving(self):
        self.assertEqual(0.0, _refine_saved_minutes(0, 0.0, 0))

    def test_a_day_that_overran_is_floored_not_negative(self):
        self.assertEqual(0.0, _refine_saved_minutes(1, 9999.0, 8))


class RecordRefineTests(unittest.TestCase):
    """record_refine banks only what was applied, into today's bucket."""

    def _store(self):
        store = stats.StatsStore.__new__(stats.StatsStore)
        import threading
        store._lock = threading.RLock()
        store._data = {"version": 1, "users": {}}
        store._user_key = "local"
        store._listeners = []
        store._push_timer = None
        store._sync_running = set()
        store._db = None
        store._save_locked = lambda: None
        return store

    def test_counts_seconds_and_prompt_words_accumulate(self):
        store = self._store()
        store.record_refine(9.0, prompt_words=6, prompt_spoken=True)
        store.record_refine(11.0, prompt_words=4)
        day = stats._today().isoformat()
        rec = store._data["users"]["local"]["days"][day]
        self.assertEqual(2, rec["r"])
        self.assertAlmostEqual(20.0, rec["rs"])
        self.assertEqual(10, rec["rp"])
        self.assertEqual(1, rec["rv"])  # only the spoken one

    def test_a_preset_refine_banks_the_default_word_count(self):
        store = self._store()
        store.record_refine(8.0)
        rec = store._data["users"]["local"]["days"][stats._today().isoformat()]
        self.assertEqual(stats.REFINE_DEFAULT_PROMPT_WORDS, rec["rp"])

    def test_snapshot_splits_dictation_and_refine_saving(self):
        store = self._store()
        store.record_dictation(400, 120.0, voiced_seconds=100.0)
        store.record_refine(10.0, prompt_words=8)
        snap = store.snapshot()
        self.assertGreater(snap["dictation_saved_minutes"], 0)
        self.assertGreater(snap["refine_saved_minutes"], 0)
        self.assertAlmostEqual(
            snap["saved_minutes"],
            snap["dictation_saved_minutes"] + snap["refine_saved_minutes"])
        self.assertEqual(1, snap["refine_count"])


class BestStreakTests(unittest.TestCase):
    """Longest-ever run, under the live streak's rule: weekdays required,
    weekend days bridge. 2026-08-03 is a Monday."""

    def test_no_days_is_zero(self):
        self.assertEqual(0, _best_streak([]))

    def test_a_missed_weekday_breaks_the_run(self):
        # Mon Tue Wed used, Thu missed, Fri used -> best is 3.
        self.assertEqual(3, _best_streak(
            ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-07"]))

    def test_a_missed_weekend_bridges_the_run(self):
        # Fri, then skip Sat/Sun, then Mon -> the run continues to 2.
        self.assertEqual(2, _best_streak(["2026-08-07", "2026-08-10"]))

    def test_used_weekend_days_count_towards_the_number(self):
        self.assertEqual(4, _best_streak(
            ["2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"]))

    def test_best_survives_a_later_shorter_run(self):
        long_run = [(datetime.date(2026, 8, 3) + datetime.timedelta(days=i)).isoformat()
                    for i in range(5)]           # Mon-Fri
        later = ["2026-08-17"]                   # a lone Monday, after a broken week
        self.assertEqual(5, _best_streak(long_run + later))

    def test_ignores_unparseable_entries(self):
        self.assertEqual(1, _best_streak(["not-a-date", "2026-08-03"]))


if __name__ == "__main__":
    unittest.main()
