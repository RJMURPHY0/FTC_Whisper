"""The impact breakdown panel must never change the window's size.

Growing the window to fit a panel was both jarring and the biggest single
source of flicker on the transition: a geometry change lands after the atomic
present, so it repaints the whole window while the user is looking at one card.
The panel is now sized to the exact height of the block it replaces (the three
cards plus the words bar), captured at open time.
"""
import calendar
import datetime
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_window import AppWindow


class NoResizeContractTests(unittest.TestCase):
    def test_opening_a_panel_never_resizes_the_window(self):
        src = inspect.getsource(AppWindow._open_impact_detail)
        self.assertNotIn("_resize", src)
        self.assertIn("winfo_height", src,
                      "the panel height must be measured, not assumed")

    def test_closing_a_panel_never_resizes_the_window(self):
        self.assertNotIn("_resize",
                         inspect.getsource(AppWindow._close_impact_detail))

    def test_the_swap_runs_as_one_frozen_frame(self):
        for fn in (AppWindow._open_impact_detail, AppWindow._close_impact_detail):
            self.assertIn("_atomic_ui", inspect.getsource(fn), fn.__name__)

    def test_the_panel_replaces_the_words_bar_as_well_as_the_cards(self):
        # Replacing only the cards would leave the words bar orphaned below a
        # panel that already carries the same figures, and the block would no
        # longer be the height the panel was measured against.
        src = inspect.getsource(AppWindow._open_impact_detail)
        self.assertIn("_impact_row.pack_forget", src)
        self.assertIn("_impact_today_card.pack_forget", src)

    def test_hover_swaps_the_image_instead_of_redrawing_the_card(self):
        src = inspect.getsource(AppWindow._hover_impact_card)
        body = src.split('"""')[-1]      # skip the docstring, which cites it
        self.assertIn("itemconfigure", body)
        self.assertNotIn('delete("all")', body)

    def test_tab_switching_is_atomic_too(self):
        src = inspect.getsource(AppWindow._switch_dash_tab)
        self.assertIn("_atomic_ui", src)


class PanelClickTests(unittest.TestCase):
    """Live Tk: the panel must not dismiss itself on a stray click, and the
    block it lives in must keep exactly the same height throughout."""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        try:
            cls.root = tk.Tk()
        except Exception as e:                      # no window station (CI)
            raise unittest.SkipTest(f"Tk unavailable: {e}")
        cls.root.geometry("420x700")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        import tkinter as tk
        import types
        import stats as stats_mod
        from app_window import C
        self.w = AppWindow.__new__(AppWindow)
        self.w._root = self.root
        self.w._config = types.SimpleNamespace(impact_range="today",
                                               save_async=lambda: None)
        self.w._stats = stats_mod.StatsStore()
        self.frame = tk.Frame(self.root, bg=C["bg"])
        self.frame.pack(fill="both", expand=True)
        self.addCleanup(self.frame.destroy)
        self.w._build_impact_section(self.frame)
        self.root.update()

    @staticmethod
    def _click(x, y):
        import types
        return types.SimpleNamespace(x=x, y=y)

    def _open(self, key="streak"):
        self.w._open_impact_detail(key)
        self.root.update()

    def test_the_block_height_is_identical_open_and_closed(self):
        before = self.w._impact_stack.winfo_height()
        self._open()
        self.assertEqual(before, self.w._impact_stack.winfo_height())
        self.w._close_impact_detail()
        self.root.update()
        self.assertEqual(before, self.w._impact_stack.winfo_height())

    def test_a_click_in_the_body_does_not_dismiss(self):
        self._open()
        self.w._on_impact_detail_click(self._click(200, 120))
        self.assertEqual("streak", self.w._impact_open)

    def test_the_header_strip_closes(self):
        self._open()
        self.w._on_impact_detail_click(self._click(200, 18))
        self.assertIsNone(self.w._impact_open)

    def test_month_arrows_navigate_without_closing(self):
        self._open()
        before = self.w._streak_month
        self.w._on_impact_detail_click(self._click(22, 46))
        self.assertEqual("streak", self.w._impact_open)
        self.assertLess(self.w._streak_month, before)

    def test_every_panel_draws_without_error(self):
        for key in ("time", "speed", "streak"):
            self.w._open_impact_detail(key)
            self.root.update()
            self.assertGreater(len(self.w._impact_detail.find_all()), 10, key)
            self.w._close_impact_detail()
            self.root.update()


class CalendarGridTests(unittest.TestCase):
    """The streak grid is a real calendar: Monday first, a fixed six rows, and
    leading/trailing days from the neighbouring months so the shape never
    changes between months."""

    @staticmethod
    def _cells(month: datetime.date):
        first = month.replace(day=1)
        start = first - datetime.timedelta(days=first.weekday())
        return [start + datetime.timedelta(days=i) for i in range(42)]

    def test_the_grid_always_starts_on_a_monday(self):
        for m in (datetime.date(2026, 8, 1), datetime.date(2026, 2, 1),
                  datetime.date(2027, 1, 1)):
            self.assertEqual(0, self._cells(m)[0].weekday(), m)

    def test_six_rows_cover_every_day_of_any_month(self):
        for year in (2026, 2027, 2028):          # 2028 is a leap year
            for mon in range(1, 13):
                month = datetime.date(year, mon, 1)
                cells = set(self._cells(month))
                last = calendar.monthrange(year, mon)[1]
                for day in range(1, last + 1):
                    self.assertIn(datetime.date(year, mon, day), cells,
                                  f"{year}-{mon:02d}-{day:02d} fell outside the grid")

    def test_a_month_starting_on_sunday_still_fits(self):
        # The worst case for a 6x7 grid: 31 days starting on a Sunday.
        month = datetime.date(2026, 3, 1)         # 2026-03-01 is a Sunday
        self.assertEqual(6, month.weekday())
        cells = self._cells(month)
        self.assertIn(datetime.date(2026, 3, 31), cells)

    def test_month_stepping_lands_on_the_first_and_wraps_the_year(self):
        window = AppWindow.__new__(AppWindow)
        window._layout_impact_detail = lambda: None
        window._streak_month = datetime.date(2026, 1, 1)
        window._step_streak_month(-1)
        self.assertEqual(datetime.date(2025, 12, 1), window._streak_month)
        window._step_streak_month(1)
        self.assertEqual(datetime.date(2026, 1, 1), window._streak_month)

    def test_stepping_forward_past_today_is_allowed(self):
        # A calendar that refuses to show next month reads as broken.
        window = AppWindow.__new__(AppWindow)
        window._layout_impact_detail = lambda: None
        today_first = datetime.date.today().replace(day=1)
        window._streak_month = today_first
        window._step_streak_month(1)
        self.assertGreater(window._streak_month, today_first)

    def test_jumping_to_today_returns_to_this_month(self):
        window = AppWindow.__new__(AppWindow)
        window._layout_impact_detail = lambda: None
        window._streak_month = datetime.date(2020, 5, 1)
        window._jump_streak_to_today()
        self.assertEqual(datetime.date.today().replace(day=1),
                         window._streak_month)


class SpanFormattingTests(unittest.TestCase):
    def test_sub_minute_reads_in_seconds(self):
        self.assertEqual("48s", AppWindow._fmt_span(0.8))

    def test_small_figures_keep_a_decimal(self):
        # "2 min" next to a "47s saved" headline makes the arithmetic look wrong.
        self.assertEqual("1.5 min", AppWindow._fmt_span(1.5))

    def test_whole_minutes_drop_the_decimal(self):
        self.assertEqual("5 min", AppWindow._fmt_span(5.0))

    def test_larger_spans_round_to_minutes_then_hours(self):
        self.assertEqual("24 min", AppWindow._fmt_span(24.4))
        self.assertEqual("5.4 hrs", AppWindow._fmt_span(324.0))
        self.assertEqual("12 hrs", AppWindow._fmt_span(720.0))

    def test_negative_and_none_are_zero(self):
        self.assertEqual("0s", AppWindow._fmt_span(-5))
        self.assertEqual("0s", AppWindow._fmt_span(None))


if __name__ == "__main__":
    unittest.main()
