"""Structured injection-failure detail + read-back verification guard.

  1. _build_failure_detail carries every fact the remote error log needs to
     answer "why didn't the text land" (elevated target, moved foreground,
     partial WM_CHAR, exception), with the exception capped.
  2. verify_text_landed answers True/False ONLY for classic Edit controls it
     can honestly read (WM_GETTEXT); everything it cannot verify — browsers,
     password fields, hung or huge targets — must return None, because a None
     is 'unknown' and a False is logged as a false success.

Pure stubs — no real HWNDs, safe anywhere.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import injector as I


class BuildFailureDetailTests(unittest.TestCase):
    def test_basic_fields(self):
        d = I._build_failure_detail(
            "chrome.exe", "Chrome_WidgetWin_1", "clipboard", 42,
            partial=False, elevated=False)
        self.assertEqual(d["fg_exe"], "chrome.exe")
        self.assertEqual(d["fg_class"], "Chrome_WidgetWin_1")
        self.assertEqual(d["method"], "clipboard")
        self.assertEqual(d["chars"], 42)
        self.assertFalse(d["partial_direct"])
        self.assertFalse(d["elevated_target"])
        self.assertNotIn("foreground_moved", d)
        self.assertNotIn("exception", d)

    def test_unknown_exe_placeholder(self):
        d = I._build_failure_detail("", "", "auto", 1,
                                    partial=True, elevated=True)
        self.assertEqual(d["fg_exe"], "?")
        self.assertTrue(d["partial_direct"])
        self.assertTrue(d["elevated_target"])

    def test_foreground_moved_flag(self):
        d = I._build_failure_detail(
            "app.exe", "Edit", "clipboard", 5, partial=False, elevated=False,
            target_hwnd=0x1111, fg_hwnd=0x9999)
        self.assertTrue(d["foreground_moved"])
        same = I._build_failure_detail(
            "app.exe", "Edit", "clipboard", 5, partial=False, elevated=False,
            target_hwnd=0x1111, fg_hwnd=0x1111)
        self.assertNotIn("foreground_moved", same)

    def test_exception_capped(self):
        d = I._build_failure_detail(
            "a.exe", "c", "auto", 1, partial=False, elevated=False,
            exception="x" * 1000)
        self.assertEqual(len(d["exception"]), 300)


class _FakeU32:
    def __init__(self, is_window=True):
        self._is_window = is_window

    def IsWindow(self, h):
        return self._is_window


class VerifyTextLandedTests(unittest.TestCase):
    HWND = 0x2222

    def setUp(self):
        self._saved = {
            "_u32": I._u32,
            "_control_class": I._control_class,
            "_control_style": I._control_style,
            "_send_message_timeout": I._send_message_timeout,
            "_read_control_text": I._read_control_text,
        }
        I._u32 = _FakeU32()
        I._control_class = lambda h: "Edit"
        I._control_style = lambda h: 0
        self._set_control_text("Dear team, please find attached the report.")

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(I, name, value)

    def _set_control_text(self, text):
        I._send_message_timeout = (
            lambda h, m, w=0, l=0, timeout_ms=500:
            len(text) if m == I._WM_GETTEXTLENGTH else len(text))
        I._read_control_text = lambda h, n: text

    def test_text_present_is_true(self):
        self.assertIs(
            I.verify_text_landed(self.HWND, "please find attached the report."),
            True)

    def test_text_absent_is_false(self):
        self.assertIs(
            I.verify_text_landed(self.HWND, "completely different words"),
            False)

    def test_whitespace_normalised_match(self):
        # WM_CHAR turned \n into \r and the control renders \r\n — the
        # comparison must not care.
        self._set_control_text("line one\r\nline two")
        self.assertIs(I.verify_text_landed(self.HWND, "line one\nline two"),
                      True)

    def test_empty_control_is_none_not_false(self):
        # Chat inputs clear themselves on Enter — an empty control is
        # ambiguous, never a logged false success.
        I._send_message_timeout = lambda h, m, w=0, l=0, timeout_ms=500: 0
        self.assertIsNone(I.verify_text_landed(self.HWND, "hello there"))

    def test_non_edit_class_is_none(self):
        I._control_class = lambda h: "Chrome_RenderWidgetHostHWND"
        self.assertIsNone(I.verify_text_landed(self.HWND, "hello"))

    def test_password_field_is_none(self):
        I._control_style = lambda h: 0x0020  # ES_PASSWORD
        self.assertIsNone(I.verify_text_landed(self.HWND, "hello"))

    def test_hung_target_is_none(self):
        I._send_message_timeout = lambda h, m, w=0, l=0, timeout_ms=500: None
        self.assertIsNone(I.verify_text_landed(self.HWND, "hello"))

    def test_huge_document_is_none(self):
        I._send_message_timeout = (
            lambda h, m, w=0, l=0, timeout_ms=500: 500_000)
        self.assertIsNone(I.verify_text_landed(self.HWND, "hello"))

    def test_invalid_window_is_none(self):
        I._u32 = _FakeU32(is_window=False)
        self.assertIsNone(I.verify_text_landed(self.HWND, "hello"))

    def test_blank_text_is_none(self):
        self.assertIsNone(I.verify_text_landed(self.HWND, "   "))
        self.assertIsNone(I.verify_text_landed(0, "hello"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
