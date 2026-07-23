"""Injection target-capture guard.

Simulates the 'clicked away mid-dictation' scenario without a real window and
asserts the reliability contract that keeps text landing in the ORIGINAL box:

  1. _post_wm_char posts to the STORED child control when given one (needs no
     foreground), so a native app still receives the text after a focus change.
  2. _clipboard_paste REFUSES Ctrl+V when the live foreground is not the capture
     target — never pasting into whatever window the user clicked instead.
  3. inject() clears its per-call target state afterwards (no leak into the next,
     unrelated, injection or a streaming emit).

Pure-Win32 stubs, no real HWNDs — safe to run in CI on any machine.
"""
import ctypes as C
import unittest

import injector as I


class InjectTargetTests(unittest.TestCase):
    TARGET_HWND = 0x1111
    TARGET_CHILD = 0x2222
    OTHER_HWND = 0x9999  # foreground after a mid-speech click elsewhere

    def setUp(self):
        self.posted_to = []
        outer = self

        class FakeU32:
            def GetForegroundWindow(self):
                return outer.OTHER_HWND

            def IsWindow(self, h):
                return h in (outer.TARGET_HWND, outer.TARGET_CHILD,
                             outer.OTHER_HWND)

            def PostMessageW(self, hwnd, msg, wparam, lparam):
                outer.posted_to.append(int(hwnd))
                return 1

            def GetWindowThreadProcessId(self, hwnd, p):
                return 123

            def GetFocus(self):
                return outer.OTHER_HWND

            def AttachThreadInput(self, a, b, c):
                return 1

            def GetAsyncKeyState(self, vk):
                return 0

            def SendInput(self, n, arr, sz):
                return n

            def keybd_event(self, *a):
                return 0

            def MapVirtualKeyW(self, *a):
                return 0

        self.fake = FakeU32()
        self._saved = {
            "_u32": I._u32,
            "_get_fg_class": I._get_fg_class,
            "_is_browser_class": I._is_browser_class,
            "_is_game_class": I._is_game_class,
            "_is_terminal_class": I._is_terminal_class,
            "windll": C.windll,
        }
        I._u32 = self.fake
        I._get_fg_class = lambda: "Notepad"
        I._is_browser_class = lambda cls: False
        I._is_game_class = lambda cls: False
        I._is_terminal_class = lambda cls: False

        real_kernel32 = self._saved["windll"].kernel32

        class _FakeWinDLL:
            user32 = self.fake
            kernel32 = real_kernel32

        C.windll = _FakeWinDLL

    def tearDown(self):
        I._u32 = self._saved["_u32"]
        I._get_fg_class = self._saved["_get_fg_class"]
        I._is_browser_class = self._saved["_is_browser_class"]
        I._is_game_class = self._saved["_is_game_class"]
        I._is_terminal_class = self._saved["_is_terminal_class"]
        C.windll = self._saved["windll"]

    def test_wm_char_posts_to_stored_child(self):
        posted, failed = I._post_wm_char("hi", self.TARGET_CHILD)
        self.assertEqual((posted, failed), (2, 0))
        self.assertEqual(set(self.posted_to), {self.TARGET_CHILD})

    def test_wm_char_without_child_uses_foreground(self):
        I._post_wm_char("hi", 0)
        self.assertNotEqual(set(self.posted_to), {self.TARGET_CHILD})

    def test_clipboard_paste_refuses_wrong_foreground(self):
        inj = I.Injector(method="clipboard")
        inj._target_hwnd = self.TARGET_HWND
        inj._target_child = self.TARGET_CHILD
        inj._clipboard_set = lambda text: (True, "")
        inj._clipboard_restore = lambda *a, **k: None
        self.assertFalse(inj._clipboard_paste("some text"))

    def test_inject_clears_target_state(self):
        inj = I.Injector(method="clipboard")
        inj._inject = lambda text, release_mods=True: True
        inj.inject("hello", target_hwnd=self.TARGET_HWND,
                   target_child=self.TARGET_CHILD)
        self.assertEqual(inj._target_hwnd, 0)
        self.assertEqual(inj._target_child, 0)


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    unittest.main(verbosity=2)
