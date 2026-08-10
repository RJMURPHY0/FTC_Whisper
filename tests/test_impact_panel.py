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


_SHARED_ROOT = None


def tearDownModule():
    """Hand the process back with no root and no images bound to it — a later
    module creating its own tk.Tk() otherwise trips over ImageTk objects still
    pointing at the dead interpreter."""
    global _SHARED_ROOT
    try:
        import ui_render
        ui_render.clear_cache()
    except Exception:
        pass
    try:
        import app_icons
        app_icons._icon_cache.clear()
        app_icons._raw_icon_cache.clear()
    except Exception:
        pass
    if _SHARED_ROOT is not None:
        try:
            _SHARED_ROOT.destroy()
        except Exception:
            pass
        _SHARED_ROOT = None


def _shared_root():
    """One Tk root for the whole module, kept alive to the end of the process.

    Destroying it and making another breaks the next one: ui_render's photo
    cache holds ImageTk objects bound to the dead interpreter, and their
    teardown wrecks the fresh one ("invalid command name tcl_findLibrary")."""
    global _SHARED_ROOT
    import tkinter as tk
    if _SHARED_ROOT is not None and _SHARED_ROOT.winfo_exists():
        return _SHARED_ROOT
    try:
        _SHARED_ROOT = tk.Tk()
    except Exception as e:                          # no window station (CI)
        raise unittest.SkipTest(f"Tk unavailable: {e}")
    _SHARED_ROOT.geometry("420x700")
    return _SHARED_ROOT


class PanelClickTests(unittest.TestCase):
    """Live Tk: the panel must not dismiss itself on the click that opened it,
    and the block it lives in must keep exactly the same height throughout."""

    @classmethod
    def setUpClass(cls):
        cls.root = _shared_root()

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

    def _open(self, key="streak", settled=True):
        self.w._open_impact_detail(key)
        self.root.update()
        if settled:
            # Age the panel past the anti-bounce guard: these tests are about
            # a deliberate click, not the second half of a double-click.
            self.w._impact_opened_at = 0.0

    def test_the_block_height_is_identical_open_and_closed(self):
        before = self.w._impact_stack.winfo_height()
        self._open()
        self.assertEqual(before, self.w._impact_stack.winfo_height())
        self.w._close_impact_detail()
        self.root.update()
        self.assertEqual(before, self.w._impact_stack.winfo_height())

    def test_a_click_in_the_body_dismisses(self):
        # The panel is a detail view, not a mode: any click that isn't a
        # control puts the cards back, so it never needs a hunt for the way out.
        self._open()
        self.w._on_impact_detail_click(self._click(200, 120))
        self.assertIsNone(self.w._impact_open)

    def test_a_click_elsewhere_on_the_page_dismisses(self):
        self._open()
        self.w._on_click_outside_impact()
        self.assertIsNone(self.w._impact_open)

    def test_the_opening_click_cannot_immediately_close_it(self):
        # A bounced or double click lands on the panel the first click just
        # opened; closing on it reads as a flicker, not as a toggle.
        self._open(settled=False)
        self.w._on_impact_detail_click(self._click(200, 120))
        self.w._on_click_outside_impact()
        self.assertEqual("streak", self.w._impact_open)

    def test_the_header_strip_closes(self):
        self._open()
        self.w._on_impact_detail_click(self._click(200, 18))
        self.assertIsNone(self.w._impact_open)

    def test_month_arrows_navigate_without_closing(self):
        # The calendar's month picker is the one control that has to survive
        # dismiss-on-any-click, guard elapsed or not.
        self._open()
        before = self.w._streak_month
        self.w._on_impact_detail_click(self._click(22, 50))
        self.assertEqual("streak", self.w._impact_open)
        self.assertLess(self.w._streak_month, before)

    def test_hovering_a_card_re_renders_its_icon_against_the_hover_surface(self):
        # ui_render bakes the card colour INTO the icon image (that is how it
        # anti-aliases). Swapping only the card background left every icon
        # carrying the base colour — a darker box around the glyph on hover.
        from app_window import C
        cv = self.w._impact_cards["time"]["cv"]
        base = self.w._impact_icon_photo(cv, "time", C["surface"])
        hover = self.w._impact_icon_photo(cv, "time", C["surface_hover"])
        if base is None:
            self.skipTest("PIL unavailable — icons fall back to primitives")
        self.assertIsNot(base, hover,
                         "the hover icon must be its own image, not the base one")
        item = self.w._impact_cards["time"].get("icon_item")
        self.assertIsNotNone(item, "the icon must be addressable for the swap")
        self.w._hover_impact_card("time", True)
        self.assertEqual(str(hover), cv.itemcget(item, "image"))
        self.w._hover_impact_card("time", False)
        self.assertEqual(str(base), cv.itemcget(item, "image"))

    def test_every_panel_draws_without_error(self):
        for key in ("time", "speed", "streak"):
            self.w._open_impact_detail(key)
            self.root.update()
            self.assertGreater(len(self.w._impact_detail.find_all()), 10, key)
            self.w._close_impact_detail()
            self.root.update()


class TimePanelTests(unittest.TestCase):
    """Time saved shows what the app COST as well as what it saved, and it does
    it in the height it already had — the block never grows (see _panel_h)."""

    @classmethod
    def setUpClass(cls):
        cls.root = _shared_root()

    def _panel(self, **snap):
        import tkinter as tk
        import types
        from app_window import C
        import stats as stats_mod
        base = dict(stats_mod.StatsStore().snapshot())    # every key the cards read
        base.update({"dictation_saved_minutes": 462.0, "refine_saved_minutes": 0.35,
                     "saved_minutes": 462.35, "refine_count": 1,
                     "refine_seconds": 6.0, "refine_prompt_words": 8,
                     "total_words": 27326, "total_audio_seconds": 11880.0})
        base.update(snap)
        w = AppWindow.__new__(AppWindow)
        w._root = self.root
        w._config = types.SimpleNamespace(impact_range="today",
                                          save_async=lambda: None)
        w._stats = types.SimpleNamespace(snapshot=lambda _s=base: dict(_s))
        frame = tk.Frame(self.root, bg=C["bg"])
        frame.pack(fill="both", expand=True)
        self.addCleanup(frame.destroy)
        w._build_impact_section(frame)
        self.root.update()
        w._open_impact_detail("time")
        self.root.update()
        return w, w._impact_detail

    @staticmethod
    def _texts(cv):
        return [cv.itemcget(i, "text") for i in cv.find_all()
                if cv.type(i) == "text"]

    def test_it_reports_the_time_actually_spent_using_the_app(self):
        w, cv = self._panel()
        texts = self._texts(cv)
        self.assertIn("Time using it", texts)
        # 11880s of recording + 6s in the refine panel = 3.3 hrs, measured.
        self.assertIn(w._fmt_span(11886.0 / 60.0), texts)

    def test_the_spent_row_is_not_added_into_the_headline(self):
        # The saving already has the speaking and refining time netted off, so
        # a panel that summed all three rows would be double counting.
        w, cv = self._panel()
        self.assertIn(w._fmt_span(462.0 + 0.35), self._texts(cv))

    def test_the_typing_by_hand_row_is_shown(self):
        # The fourth row surfaces the counterfactual: what typing it all would
        # have cost. 27,326 words / 40 wpm = 11.4 hrs.
        import stats as stats_mod
        w, cv = self._panel()
        texts = self._texts(cv)
        self.assertIn("Typing by hand", texts)
        self.assertIn(w._fmt_span(27326 / float(stats_mod.TYPING_WPM)), texts)

    def test_the_headline_rides_the_header_not_a_body_row(self):
        # Moving the figure into the header is what freed the room for the
        # fourth row — "saved so far" must be present and near the top.
        w, cv = self._panel()
        subs = [i for i in cv.find_all()
                if cv.type(i) == "text" and cv.itemcget(i, "text") == "saved so far"]
        self.assertTrue(subs, "the headline caption is gone")
        self.assertLess(cv.bbox(subs[0])[1], 40, "it should ride the header row")

    def test_each_row_carries_an_icon(self):
        # Ryan asked for a little icon per row; they are canvas images.
        w, cv = self._panel()
        images = [i for i in cv.find_all() if cv.type(i) == "image"]
        self.assertGreaterEqual(len(images), 4, "one glyph per row")

    def test_all_four_rows_fit_without_the_panel_growing(self):
        w, cv = self._panel()
        h = w._panel_h()
        for item in cv.find_all():
            bbox = cv.bbox(item)
            self.assertLessEqual(bbox[3], h,
                                 f"{cv.type(item)} {cv.bbox(item)} overflows {h}")

    def test_every_row_note_stays_on_one_line(self):
        # Four rows only fit because each explanation is a single short line;
        # a wrapped note pushes the row below it out of the panel.
        w, cv = self._panel()
        for item in cv.find_all():
            if cv.type(item) != "text" or int(cv.itemcget(item, "width") or 0) == 0:
                continue
            x0, y0, x1, y1 = cv.bbox(item)
            self.assertLess(y1 - y0, 18,
                            f"wrapped to two lines: {cv.itemcget(item, 'text')!r}")

    def test_a_fresh_account_still_draws_all_four_rows(self):
        w, cv = self._panel(dictation_saved_minutes=0.0, refine_saved_minutes=0.0,
                            refine_count=0, refine_seconds=0.0,
                            refine_prompt_words=0, total_words=0,
                            total_audio_seconds=0.0)
        texts = self._texts(cv)
        for label in ("Dictation", "AI refine", "Time using it", "Typing by hand"):
            self.assertIn(label, texts)


class HotkeyLinkTests(unittest.TestCase):
    """The Home hint rows (ALT+V / ALT+C / ALT+R) open the Hotkey tab. A
    shortcut you can't change from where you read it is a dead end."""

    def test_every_widget_in_a_hint_row_is_wired_and_shows_a_hand(self):
        import tkinter as tk
        root = tk.Toplevel(_shared_root())
        self.addCleanup(root.destroy)

        window = AppWindow.__new__(AppWindow)
        window._switch_dash_tab = lambda name: switched.append(name)
        switched = []
        frame = tk.Frame(root)
        label = tk.Label(frame, text="ALT+V")
        window._link_to_hotkeys(frame, label)

        for wdg in (frame, label):
            self.assertEqual("hand2", str(wdg.cget("cursor")))
            self.assertTrue(wdg.bind("<Button-1>"),
                            "the row must carry a click binding")

    def test_the_click_switches_to_the_hotkey_tab_and_stops_there(self):
        import tkinter as tk
        root = tk.Toplevel(_shared_root())
        self.addCleanup(root.destroy)
        switched = []
        window = AppWindow.__new__(AppWindow)
        window._switch_dash_tab = lambda name: switched.append(name)
        label = tk.Label(root, text="ALT+R")
        label.pack()
        window._link_to_hotkeys(label)
        root.update()
        # Through Tk, so the real binding runs rather than a Python reference.
        label.event_generate("<Button-1>", x=2, y=2, when="now")
        self.assertEqual(["hotkey"], switched)


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
