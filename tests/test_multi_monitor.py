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


class RefineAnchorTests(unittest.TestCase):
    """The refine panel and the dictation pill open on the CURSOR's monitor.

    The popup picks its monitor from the coordinates it is handed. The rule is
    the mouse cursor, always: on a multi-monitor desk the caret (the text being
    edited) is routinely on a different screen from the cursor, and anchoring to
    it opened the panel on the wrong monitor. `_refine_anchor` therefore returns
    the raw cursor position and never consults the caret or the window rect.
    """

    def setUp(self):
        import app as app_mod
        self.app_mod = app_mod
        self.whisper = app_mod.WhisperFlowApp.__new__(app_mod.WhisperFlowApp)

    def _anchor(self, hwnd, *, mouse):
        import app as app_mod

        class _FakeU32:
            @staticmethod
            def GetCursorPos(ref):
                ref._obj.x, ref._obj.y = mouse
                return 1

        class _FakeWindll:
            user32 = _FakeU32

        # A caret on a different monitor must NOT be able to pull the anchor
        # off the cursor's screen — so make consulting it explode if it ever runs.
        real_capture = app_mod._capture_focus_target
        def _boom(_h):
            raise AssertionError("refine must not read the caret")
        app_mod._capture_focus_target = _boom
        real_windll = app_mod.ctypes.windll
        app_mod.ctypes.windll = _FakeWindll
        try:
            return app_mod.WhisperFlowApp._refine_anchor(self.whisper, hwnd)
        finally:
            app_mod._capture_focus_target = real_capture
            app_mod.ctypes.windll = real_windll

    def test_the_cursor_wins_even_with_a_target_window(self):
        # Mouse on the right monitor — the panel goes to the right monitor.
        self.assertEqual((2600, 700), self._anchor(0x1234, mouse=(2600, 700)))

    def test_the_cursor_wins_on_the_left_monitor(self):
        # Virtual-screen coords go negative on a left-hand display.
        self.assertEqual((-1400, 500), self._anchor(0x1234, mouse=(-1400, 500)))

    def test_no_target_window_still_returns_the_cursor(self):
        self.assertEqual((900, 500), self._anchor(0, mouse=(900, 500)))

    def test_source_has_no_caret_preference(self):
        # Guard against a future refactor quietly reintroducing the caret anchor
        # that caused the wrong-monitor regression.
        src = inspect.getsource(self.app_mod.WhisperFlowApp._refine_anchor)
        self.assertNotIn("_capture_focus_target", src)


if __name__ == "__main__":
    unittest.main()
