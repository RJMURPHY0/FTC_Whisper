"""Dynamic injection re-targeting guard.

The injection target is captured at record START, but the user may not have
clicked into a text box until DURING dictation (or may have moved from one
field into another). At stop time the app re-reads the live focus and, when it
points at a control that can actually accept text, redirects the dictation
there — while still falling back to the start capture when the live foreground
is somewhere text can't go (a click on empty space, a button).

These are pure decision helpers, safe to run in CI on any machine (no real
HWNDs, no window manager).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _looks_editable, _choose_inject_target


class LooksEditableTests(unittest.TestCase):
    def test_caret_present_is_editable(self):
        # A live caret only exists in an editable text field — strongest signal.
        self.assertTrue(_looks_editable((120, 340), "", ""))
        self.assertTrue(_looks_editable((0, 5), "SomeCustomCanvas", ""))

    def test_no_caret_and_no_hints_is_not_editable(self):
        # A click on a button / static / empty desktop: nothing to type into.
        self.assertFalse(_looks_editable((0, 0), "Button", "Shell_TrayWnd"))
        self.assertFalse(_looks_editable((0, 0), "", ""))

    def test_edit_control_class_is_editable(self):
        for cls in ("Edit", "RichEdit20W", "RICHEDIT50W", "Scintilla", "_WwG"):
            self.assertTrue(_looks_editable((0, 0), cls, ""),
                            f"{cls} should read as editable")

    def test_browser_render_widget_is_editable(self):
        # Browsers host DOM inputs Win32 can't introspect; treat them as text
        # targets whether the hint is on the child or the top-level class.
        self.assertTrue(
            _looks_editable((0, 0), "Chrome_RenderWidgetHostHWND", ""))
        self.assertTrue(_looks_editable((0, 0), "", "Chrome_WidgetWin_1"))
        self.assertTrue(_looks_editable((0, 0), "", "MozillaWindowClass"))


class ChooseInjectTargetTests(unittest.TestCase):
    START_HWND, START_CHILD = 0x1111, 0x2222
    LIVE_HWND, LIVE_CHILD = 0x9999, 0x8888

    def test_clicked_into_a_new_editable_field_retargets(self):
        # The reported bug: nothing insertable at start, user clicks a box
        # mid-speech. The dictation must follow to the live foreground.
        hwnd, child, is_live = _choose_inject_target(
            self.START_HWND, self.START_CHILD,
            self.LIVE_HWND, self.LIVE_CHILD, live_editable=True)
        self.assertEqual((hwnd, child, is_live),
                         (self.LIVE_HWND, self.LIVE_CHILD, True))

    def test_click_on_non_editable_keeps_start_capture(self):
        # Clicked somewhere text can't go — never strand the dictation; fall
        # back to the box that WAS focused at record start.
        hwnd, child, is_live = _choose_inject_target(
            self.START_HWND, self.START_CHILD,
            self.LIVE_HWND, self.LIVE_CHILD, live_editable=False)
        self.assertEqual((hwnd, child, is_live),
                         (self.START_HWND, self.START_CHILD, False))

    def test_no_live_foreground_keeps_start_capture(self):
        hwnd, child, is_live = _choose_inject_target(
            self.START_HWND, self.START_CHILD, 0, 0, live_editable=True)
        self.assertEqual((hwnd, child, is_live),
                         (self.START_HWND, self.START_CHILD, False))

    def test_same_field_still_reports_live_focus(self):
        # Normal case: the box focused at start is still the live focus. It
        # reports is_live_focus=True so the focus-restore dance is skipped
        # (focus is already correct) — the fast path stays fast.
        hwnd, child, is_live = _choose_inject_target(
            self.START_HWND, self.START_CHILD,
            self.START_HWND, self.START_CHILD, live_editable=True)
        self.assertEqual((hwnd, child, is_live),
                         (self.START_HWND, self.START_CHILD, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
