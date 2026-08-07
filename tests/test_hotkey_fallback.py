"""The keyboard-hook fallback (Win32 RegisterHotKey lost the combo to another
app) must not leak the base key, and must never eat ordinary typing.

The old code called _install_base_key_suppressor() on this path, but that helper
returns early unless `_win32_ok` — which is False by definition here — so the
suppressor was never installed and every dictation sent Alt+V on to the focused
app. Splitting it back into "non-blocking detector + blocking suppressor" is not
an option either: the keyboard library returns from direct_callback the moment a
blocking hook suppresses, BEFORE it queues the event for non-blocking handlers,
so the detector would stop firing. One hook has to do both.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hotkey_manager as hm_mod
from hotkey_manager import AppState, HotkeyManager


class FallbackHookTests(unittest.TestCase):
    def setUp(self):
        self.hm = HotkeyManager.__new__(HotkeyManager)
        self.hm._base_key = "v"
        self.hm._modifiers = ["alt"]
        self.hm._kb_hooks = []
        self.hm._is_combo = True
        self.hm._win32_ok = False
        self.hm.mode = "toggle"
        self.registered = []

        def _on_press(key, cb, suppress=False):
            self.registered.append(("press", key, cb, suppress))
            return cb

        def _on_release(key, cb, suppress=False):
            self.registered.append(("release", key, cb, suppress))
            return cb

        patcher = mock.patch.multiple(hm_mod.kb, on_press_key=_on_press,
                                      on_release_key=_on_release)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _press_hook(self):
        self.hm._install_fallback_combo_hooks()
        return next(cb for kind, _k, cb, _s in self.registered if kind == "press")

    def test_the_press_hook_is_registered_as_blocking(self):
        self.hm._install_fallback_combo_hooks()
        press = [r for r in self.registered if r[0] == "press"]
        self.assertEqual(1, len(press))
        self.assertTrue(press[0][3], "the press hook must suppress, or the key leaks")

    def test_only_one_press_hook_exists(self):
        # Two hooks (detector + suppressor) cannot both fire: see the module
        # docstring. One hook does both jobs.
        self.hm._install_fallback_combo_hooks()
        self.assertEqual(1, sum(1 for r in self.registered if r[0] == "press"))

    def test_typing_the_bare_letter_is_never_swallowed(self):
        # The nightmare regression: a blocking hook on "v" that forgets to
        # check the modifiers stops the user typing the letter v at all.
        hook = self._press_hook()
        with mock.patch.object(self.hm, "_combo_modifiers_held", return_value=False):
            with mock.patch.object(self.hm, "_kb_combo_down") as down:
                self.assertTrue(hook(object()), "bare 'v' must pass through")
                down.assert_not_called()

    def test_the_combo_is_swallowed_and_starts_a_recording(self):
        hook = self._press_hook()
        fired = []
        with mock.patch.object(self.hm, "_combo_modifiers_held", return_value=True):
            with mock.patch.object(self.hm, "_kb_combo_down",
                                   side_effect=lambda *_a: fired.append(1)):
                self.assertFalse(hook(object()), "the combo press must be suppressed")
                for _ in range(100):
                    if fired:
                        break
                    time.sleep(0.01)
        self.assertEqual([1], fired, "the combo must still be detected")

    def test_detection_runs_off_the_input_thread(self):
        # A blocking hook runs inline in the global keyboard path; anything
        # slow in there stalls typing system-wide.
        hook = self._press_hook()
        started = []
        with mock.patch.object(self.hm, "_combo_modifiers_held", return_value=True):
            with mock.patch.object(self.hm, "_kb_combo_down",
                                   side_effect=lambda *_a: (time.sleep(0.4),
                                                            started.append(1))):
                t0 = time.perf_counter()
                hook(object())
                elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.1, "the hook blocked the keyboard thread")

    def test_a_broken_hook_never_blocks_the_key(self):
        hook = self._press_hook()
        with mock.patch.object(self.hm, "_combo_modifiers_held",
                               side_effect=RuntimeError("boom")):
            self.assertTrue(hook(object()),
                            "an error must fail open, not eat the keystroke")

    def test_the_release_hook_stays_non_blocking(self):
        self.hm._install_fallback_combo_hooks()
        rel = [r for r in self.registered if r[0] == "release"]
        self.assertEqual(1, len(rel))
        self.assertFalse(rel[0][3])


class AutoStopSourceTests(unittest.TestCase):
    """The auto-stop timer is the only thing that recovers a recording whose
    key-up was never seen. Both handlers ignore a source that doesn't own the
    recording, so it has to name the right one."""

    def _app(self, mode, rec_source):
        import app as app_mod
        a = app_mod.WhisperFlowApp.__new__(app_mod.WhisperFlowApp)
        calls = []

        class _HM:
            state = AppState.RECORDING

            def __init__(self):
                self.mode = mode
                self._rec_source = rec_source

            def _on_key_down(self, _event=None, source="main"):
                calls.append(("down", source))

            def _on_key_up(self, _event=None, source="main"):
                calls.append(("up", source))

        a.hotkey_manager = _HM()
        return a, calls

    def test_a_ptt_recording_is_stopped_by_its_own_source(self):
        for mode in ("toggle", "hold"):
            app, calls = self._app(mode, "ptt")
            app._auto_stop_recording()
            self.assertEqual([("up", "ptt")], calls, mode)

    def test_the_main_bind_in_toggle_mode_stops_with_a_second_press(self):
        app, calls = self._app("toggle", "main")
        app._auto_stop_recording()
        self.assertEqual([("down", "main")], calls)

    def test_the_main_bind_in_hold_mode_stops_with_a_release(self):
        app, calls = self._app("hold", "main")
        app._auto_stop_recording()
        self.assertEqual([("up", "main")], calls)

    def test_nothing_happens_when_no_recording_is_live(self):
        app, calls = self._app("toggle", "main")
        app.hotkey_manager.state = AppState.IDLE
        app._auto_stop_recording()
        self.assertEqual([], calls)


class LiveReconcileTests(unittest.TestCase):
    """A failed delete must not be followed by a retype — that appends the
    corrected text after the characters we failed to remove."""

    def _app(self, del_ok):
        import app as app_mod
        a = app_mod.WhisperFlowApp.__new__(app_mod.WhisperFlowApp)
        self.typed = []

        class _Inj:
            def delete_stream(_s, n):
                return del_ok

            def inject_stream(_s, text):
                self.typed.append(text)
                return True

        a.injector = _Inj()
        a.config = mock.Mock(trailing_space=False)
        return a

    def test_a_failed_delete_leaves_the_streamed_text_alone(self):
        app = self._app(del_ok=False)
        ok, n = app._reconcile_live("hello wurld", "hello world", True)
        self.assertFalse(ok)
        self.assertEqual([], self.typed, "retyped on top of an un-deleted tail")
        self.assertEqual(len("hello wurld"), n)

    def test_a_good_delete_still_retypes_the_corrected_tail(self):
        app = self._app(del_ok=True)
        ok, n = app._reconcile_live("hello wurld", "hello world", True)
        self.assertTrue(ok)
        self.assertEqual(["world"], self.typed)
        self.assertEqual(len("hello world"), n)

    def test_identical_text_touches_nothing(self):
        app = self._app(del_ok=False)
        ok, n = app._reconcile_live("same text", "same text", True)
        self.assertTrue(ok)
        self.assertEqual([], self.typed)


if __name__ == "__main__":
    unittest.main()
