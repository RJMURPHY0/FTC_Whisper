"""Copy to Clipboard OFF must actually mean off.

A clipboard-paste injection writes the dictation to the clipboard and schedules
a restore of what was there before. When there was NOTHING to restore (an empty
clipboard, or non-text content we could not back up) the old code simply
returned — so the dictation stayed on the clipboard, which is exactly what the
Copy to Clipboard setting is supposed to opt IN to. With the setting off the
clipboard is emptied instead.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import injector as I


class _NoDelay:
    """Collapse the 1.5s paste-settling sleep so the tests stay instant."""

    def __enter__(self):
        self._real = I.time.sleep
        I.time.sleep = lambda _s: None
        return self

    def __exit__(self, *_a):
        I.time.sleep = self._real


class ClipboardRestoreTests(unittest.TestCase):
    def setUp(self):
        self.cleared = 0
        self.restored = []
        self._real_clear = I.Injector._clipboard_clear
        self._real_set = I.Injector._clipboard_set
        self._keep = I.Injector.keep_clipboard

        def _fake_clear():
            self.cleared += 1
            return True

        def _fake_set(text, bump=True):
            self.restored.append(text)
            return True, ""

        I.Injector._clipboard_clear = staticmethod(_fake_clear)
        I.Injector._clipboard_set = staticmethod(_fake_set)

    def tearDown(self):
        I.Injector._clipboard_clear = self._real_clear
        I.Injector._clipboard_set = self._real_set
        I.Injector.keep_clipboard = self._keep

    @staticmethod
    def _settle():
        # The restore/clear runs on a daemon thread.
        for _ in range(200):
            time.sleep(0.005)
            if I.threading.active_count() <= 1:
                break
        time.sleep(0.05)

    def test_nothing_to_restore_clears_the_clipboard_when_the_setting_is_off(self):
        I.Injector.keep_clipboard = False
        with _NoDelay():
            I.Injector._clipboard_restore("", I._clip_gen)
            self._settle()
        self.assertEqual(1, self.cleared)
        self.assertEqual([], self.restored)

    def test_nothing_to_restore_leaves_the_dictation_when_the_setting_is_on(self):
        I.Injector.keep_clipboard = True
        with _NoDelay():
            I.Injector._clipboard_restore("", I._clip_gen)
            self._settle()
        self.assertEqual(0, self.cleared)

    def test_a_newer_paste_supersedes_the_clear(self):
        # The gen guard must cover the clear exactly as it covers a restore, or
        # we would empty a clipboard the NEXT dictation had just written to.
        I.Injector.keep_clipboard = False
        stale_gen = I._clip_gen - 1
        with _NoDelay():
            I.Injector._clipboard_restore("", stale_gen)
            self._settle()
        self.assertEqual(0, self.cleared)

    def test_real_content_is_still_restored_not_cleared(self):
        I.Injector.keep_clipboard = False
        with _NoDelay():
            I.Injector._clipboard_restore("the user's own copy", I._clip_gen)
            self._settle()
        self.assertEqual(0, self.cleared)
        self.assertEqual(["the user's own copy"], self.restored)


class SettingWiringTests(unittest.TestCase):
    """The flag has to be mirrored onto the Injector, or the helpers (which are
    static and never see the config) fall back to the default."""

    def test_app_mirrors_the_setting_at_startup_and_on_change(self):
        import inspect
        import app as app_mod
        src = inspect.getsource(app_mod.WhisperFlowApp)
        self.assertIn("Injector.keep_clipboard", src)
        applied = inspect.getsource(app_mod.WhisperFlowApp._apply_runtime_setting)
        self.assertIn("copy_to_clipboard", applied,
                      "toggling the setting must apply live, not on restart")


if __name__ == "__main__":
    unittest.main()
