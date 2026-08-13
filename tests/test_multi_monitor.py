"""Popups and window placement must follow the monitor the app is ON.

winfo_screenwidth()/winfo_screenheight() report the PRIMARY display only. Every
clamp built on them silently assumed one screen, so with the window on a second
monitor the dropdown list was yanked back onto the main display (and a resize
recentred the whole window there). The fix is the Win32 work-area of the monitor
the widget actually sits on.
"""
import inspect
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_window import AppWindow, Dropdown, _monitor_work_area, show_toast


class WorkAreaTests(unittest.TestCase):
    def test_falls_back_to_the_tk_screen_when_win32_is_unavailable(self):
        # Anything without a real HWND (test doubles, a torn-down widget) must
        # degrade to the old behaviour rather than raise into a paint path.
        fake = types.SimpleNamespace(winfo_screenwidth=lambda: 1600,
                                     winfo_screenheight=lambda: 900)
        self.assertEqual((0, 0, 1600, 900), _monitor_work_area(fake))

    def test_a_totally_dead_widget_still_returns_a_usable_rect(self):
        class _Dead:
            def __getattr__(self, _name):
                raise RuntimeError("widget is gone")

        left, top, right, bottom = _monitor_work_area(_Dead())
        self.assertGreater(right, left)
        self.assertGreater(bottom, top)

    def test_a_real_widget_reports_a_work_area_containing_it(self):
        import tkinter as tk
        # Reuse the interpreter's root if another test already made one: a
        # SECOND tk.Tk() gives ui_render's photo cache two masters, and a
        # cached image drawn on the wrong one fails.
        existing = getattr(tk, "_default_root", None)
        if existing is not None:
            root = tk.Toplevel(existing)
            self.addCleanup(root.destroy)
        else:
            try:
                root = tk.Tk()
            except Exception as e:                   # no window station (CI)
                raise unittest.SkipTest(f"Tk unavailable: {e}")
            self.addCleanup(root.destroy)
        root.geometry("300x200+120+80")
        root.update_idletasks()
        left, top, right, bottom = _monitor_work_area(root)
        self.assertGreater(right, left)
        self.assertGreater(bottom, top)
        self.assertLessEqual(left, root.winfo_rootx())
        self.assertLessEqual(top, root.winfo_rooty())


class ClampSourceTests(unittest.TestCase):
    """Source-level invariants: these are the exact call sites that regressed,
    and a live two-monitor rig can't be assumed in a test run."""

    def test_the_dropdown_clamps_against_its_own_monitor(self):
        src = inspect.getsource(Dropdown.open)
        self.assertIn("_monitor_work_area", src)
        # The clamp itself must use the monitor's edges, not a 0-based screen.
        self.assertIn("mon_l", src)
        self.assertIn("mon_b", src)

    @staticmethod
    def _code(fn) -> str:
        """Source with comments dropped — they cite the old call by name."""
        return "\n".join(line.split("#")[0]
                         for line in inspect.getsource(fn).splitlines())

    def test_resizing_recentres_on_the_current_monitor(self):
        src = self._code(AppWindow._resize)
        self.assertIn("_monitor_work_area", src)
        self.assertNotIn("winfo_screenwidth", src)

    def test_the_toast_lands_on_the_apps_monitor(self):
        src = self._code(show_toast)
        self.assertIn("_monitor_work_area", src)
        self.assertNotIn("winfo_screenwidth", src)


class PopupAnchorTests(unittest.TestCase):
    """The pill and the refine panel open on the monitor the user is working
    on, derived from the WINDOW receiving the text.

    Priority (each step earned by a shipped failure): the mouse when it is
    INSIDE the target window (the only signal that disambiguates a window
    straddling two monitors — the v1.6.63 window-centre anchor tie-broke
    those wrong); otherwise the window's own monitor via MonitorFromWindow
    (the parked-mouse case the raw-cursor anchor failed); otherwise the
    cursor. Never the caret (unreadable in Chromium/Electron — the v1.6.53
    failure).
    """

    def setUp(self):
        import app as app_mod
        self.app_mod = app_mod
        self.whisper = app_mod.WhisperFlowApp.__new__(app_mod.WhisperFlowApp)

    def _anchor(self, hwnd, *, mouse, rect=None, iconic=False, work=None):
        import app as app_mod

        class _FakeU32:
            @staticmethod
            def GetCursorPos(ref):
                ref._obj.x, ref._obj.y = mouse
                return 1

            @staticmethod
            def IsIconic(_h):
                return 1 if iconic else 0

            @staticmethod
            def GetWindowRect(_h, ref):
                if rect is None:
                    return 0
                (ref._obj.left, ref._obj.top,
                 ref._obj.right, ref._obj.bottom) = rect
                return 1

            @staticmethod
            def MonitorFromWindow(_h, _flag):
                return 42 if work is not None else 0

            @staticmethod
            def GetMonitorInfoW(_hm, ref):
                if work is None:
                    return 0
                mi = ref._obj
                (mi.rcWork.left, mi.rcWork.top,
                 mi.rcWork.right, mi.rcWork.bottom) = work
                return 1

        class _FakeWindll:
            user32 = _FakeU32

        # The caret must never come back as an anchor source — consulting it
        # explodes so a regression is loud.
        real_capture = app_mod._capture_focus_target
        def _boom(_h):
            raise AssertionError("the popup anchor must not read the caret")
        app_mod._capture_focus_target = _boom
        real_windll = app_mod.ctypes.windll
        app_mod.ctypes.windll = _FakeWindll
        try:
            return app_mod.WhisperFlowApp._popup_anchor(self.whisper, hwnd)
        finally:
            app_mod._capture_focus_target = real_capture
            app_mod.ctypes.windll = real_windll

    def test_the_window_monitor_wins_over_a_mouse_parked_elsewhere(self):
        # The original reported bug: typing into an app on the right monitor,
        # mouse parked on another screen. The pill follows the window's
        # monitor (work-area centre).
        self.assertEqual(
            (2880, 516),
            self._anchor(0x1234, mouse=(-800, 400),
                         rect=(2000, 200, 3200, 1200),
                         work=(1920, 0, 3840, 1032)))

    def test_the_mouse_wins_inside_a_straddling_window(self):
        # The second reported bug: a wide window hanging across the shared
        # edge of two monitors. Every window-derived point can land on the
        # half the user is NOT working in — but the mouse sits on the text
        # they just selected, INSIDE the window, and that says which side.
        self.assertEqual(
            (2600, 700),
            self._anchor(0x1234, mouse=(2600, 700),
                         rect=(500, 100, 3300, 1000),
                         work=(0, 0, 1920, 1032)))

    def test_the_mouse_inside_the_window_short_circuits(self):
        # Mouse inside a non-straddling window: same monitor either way,
        # and the mouse is simply used as the point.
        self.assertEqual(
            (2500, 600),
            self._anchor(0x1234, mouse=(2500, 600),
                         rect=(2000, 200, 3200, 1200),
                         work=(1920, 0, 3840, 1032)))

    def test_a_window_on_a_negative_coordinate_monitor_wins_too(self):
        # Virtual-screen coords go negative on a left/upper display.
        self.assertEqual(
            (-1280, -540),
            self._anchor(0x1234, mouse=(900, 500),
                         rect=(-2000, -900, -800, -100),
                         work=(-2560, -1080, 0, 0)))

    def test_monitor_info_failure_falls_back_to_the_window_centre(self):
        self.assertEqual(
            (2600, 700),
            self._anchor(0x1234, mouse=(-800, 400),
                         rect=(2000, 200, 3200, 1200), work=None))

    def test_no_target_window_falls_back_to_the_cursor(self):
        self.assertEqual((900, 500), self._anchor(0, mouse=(900, 500)))

    def test_an_unreadable_window_rect_falls_back_to_the_cursor(self):
        self.assertEqual((900, 500),
                         self._anchor(0x1234, mouse=(900, 500), rect=None))

    def test_a_minimised_window_falls_back_to_the_cursor(self):
        # A minimised window's rect is the meaningless -32000 parking spot.
        self.assertEqual(
            (900, 500),
            self._anchor(0x1234, mouse=(900, 500),
                         rect=(-32000, -32000, -31840, -31972), iconic=True))

    def test_an_empty_rect_falls_back_to_the_cursor(self):
        self.assertEqual((900, 500),
                         self._anchor(0x1234, mouse=(900, 500),
                                      rect=(100, 100, 100, 100)))

    def test_source_has_no_caret_preference(self):
        # Guard against a future refactor quietly reintroducing the caret
        # anchor that caused the first wrong-monitor regression.
        src = inspect.getsource(self.app_mod.WhisperFlowApp._popup_anchor)
        self.assertNotIn("_capture_focus_target", src)

    def test_the_recording_pill_uses_the_window_anchor(self):
        # The dictation pill's show_status must be handed the window anchor,
        # never the raw mouse capture (which injection still needs for its
        # same-cursor check — the two must stay separate).
        src = inspect.getsource(self.app_mod.WhisperFlowApp._on_state_change)
        self.assertIn("_popup_anchor", src)
        self.assertIn("cursor_x=ax", src)
        self.assertIn("_rec_anchor_x", src)

    def test_refine_uses_the_window_anchor(self):
        src = inspect.getsource(self.app_mod.WhisperFlowApp._on_refine_selection)
        self.assertIn("_popup_anchor", src)


if __name__ == "__main__":
    unittest.main()
