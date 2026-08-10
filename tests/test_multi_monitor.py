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
    """Recording and refine popups open where the TEXT is, not the mouse.

    The popup picks its monitor from the coordinates it is handed, and refine
    handed it the raw cursor position. Selections are routinely made with the
    keyboard, and on a two-monitor desk the pointer is often parked on the other
    screen — which is how the refine panel kept opening on the wrong monitor.
    """

    def setUp(self):
        import app as app_mod
        self.app_mod = app_mod
        self.whisper = app_mod.WhisperFlowApp.__new__(app_mod.WhisperFlowApp)

    def _anchor(self, hwnd, *, caret, mouse, rect):
        import app as app_mod

        class _FakeU32:
            @staticmethod
            def GetCursorPos(ref):
                ref._obj.x, ref._obj.y = mouse
                return 1

            @staticmethod
            def GetWindowRect(_h, ref):
                if rect is None:
                    return 0
                (ref._obj.left, ref._obj.top,
                 ref._obj.right, ref._obj.bottom) = rect
                return 1

        class _FakeWindll:
            user32 = _FakeU32

        real_windll = app_mod.ctypes.windll
        app_mod.ctypes.windll = _FakeWindll
        try:
            return app_mod.WhisperFlowApp._popup_anchor(
                self.whisper, hwnd, caret)
        finally:
            app_mod.ctypes.windll = real_windll

    def test_the_caret_wins(self):
        # Text on the left monitor, mouse parked on the right one.
        self.assertEqual(
            (400, 300),
            self._anchor(0x1234, caret=(400, 300), mouse=(2600, 700),
                         rect=(0, 0, 1900, 1000)))

    def test_the_mouse_is_used_when_it_is_over_the_target_window(self):
        self.assertEqual(
            (900, 500),
            self._anchor(0x1234, caret=(0, 0), mouse=(900, 500),
                         rect=(0, 0, 1900, 1000)))

    def test_a_mouse_on_another_screen_falls_back_to_the_window(self):
        # Without this the popup follows the pointer to the other display.
        self.assertEqual(
            (950, 500),
            self._anchor(0x1234, caret=(0, 0), mouse=(2600, 700),
                         rect=(0, 0, 1900, 1000)))

    def test_no_target_window_still_returns_a_point(self):
        self.assertEqual(
            (2600, 700),
            self._anchor(0, caret=(0, 0), mouse=(2600, 700), rect=None))

    def test_recording_state_uses_the_focus_anchor_not_raw_mouse(self):
        # Guard the actual Alt+V/PTT call site. Keeping _popup_anchor only in
        # refine would reproduce the reported wrong-monitor recording pill.
        src = inspect.getsource(self.app_mod.WhisperFlowApp._on_state_change)
        self.assertIn("_recording_popup_anchor", src)
        self.assertIn("self._popup_anchor", src)


if __name__ == "__main__":
    unittest.main()
