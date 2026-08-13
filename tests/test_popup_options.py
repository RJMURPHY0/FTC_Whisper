"""The two popup options shipped in v1.6.64.

1. show_pill_arrows — the ▴▾◂▸ nudge arrows on the recording pill become
   optional. Hiding must never lose the saved position, and re-showing must
   restore the exact negative-offset placement that puts each arrow on the
   pill's box border (place_forget drops the geometry, so the specs are
   captured at build time).
2. hide_popup_in_screenshots — SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)
   on the popup's REAL top-level. winfo_id() is the CHILD window on Windows
   (the _top_hwnd gotcha): the affinity call on the child succeeds yet the
   popup still shows in captures, so the test reads the affinity back from
   the top-level the setter must have targeted.
"""
import ctypes
import ctypes.wintypes
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from popup import FloatingPopup

_WDA_EXCLUDEFROMCAPTURE = 0x11


def _tk_root():
    import tkinter as tk
    existing = getattr(tk, "_default_root", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                return existing
        except Exception:
            pass
    try:
        return tk.Tk()
    except Exception as e:                          # no window station (CI)
        raise unittest.SkipTest(f"Tk unavailable: {e}")


class ConfigDefaultTests(unittest.TestCase):
    def test_arrows_default_on_and_capture_hiding_default_off(self):
        from config import Config
        c = Config()
        self.assertTrue(c.show_pill_arrows)
        self.assertFalse(c.hide_popup_in_screenshots)


class PillArrowToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _tk_root()
        cls.popup = FloatingPopup()
        cls.popup.initialize(cls.root)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.popup.root.destroy()
        except Exception:
            pass

    def _arrows(self):
        p = self.popup
        return (p._pos_up, p._pos_down, p._pos_left, p._pos_right)

    def test_arrows_hide_and_restore_their_exact_placement(self):
        before = [dict(c.place_info()) for c in self._arrows()]
        self.assertTrue(all(before), "arrows should start placed")
        self.popup.set_pill_arrows(False)
        self.root.update()
        for c in self._arrows():
            self.assertFalse(c.place_info(), "arrow still placed while off")
        self.popup.set_pill_arrows(True)
        self.root.update()
        for b, c in zip(before, self._arrows()):
            a = dict(c.place_info())
            for k in ("relx", "rely", "x", "y", "anchor"):
                self.assertEqual(b.get(k), a.get(k),
                                 f"placement key {k} not restored")

    def test_the_flag_is_safe_before_initialize(self):
        p2 = FloatingPopup()
        p2.set_pill_arrows(False)       # no root, no arrows yet — must not raise
        self.assertFalse(p2._arrows_visible)


class CaptureAffinityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _tk_root()
        cls.popup = FloatingPopup()
        # Config is pushed BEFORE the window exists on the real startup path —
        # the flag must survive until initialize applies it.
        cls.popup.set_capture_hidden(True)
        cls.popup.initialize(cls.root)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.popup.root.destroy()
        except Exception:
            pass

    def _affinity(self) -> int:
        aff = ctypes.wintypes.DWORD(0)
        hw = self.popup._top_hwnd()
        ok = ctypes.windll.user32.GetWindowDisplayAffinity(
            hw, ctypes.byref(aff))
        if not ok:
            raise unittest.SkipTest("GetWindowDisplayAffinity unavailable")
        return aff.value

    def test_set_before_initialize_is_applied_on_creation(self):
        self.assertEqual(_WDA_EXCLUDEFROMCAPTURE, self._affinity())

    def test_toggling_updates_the_real_toplevel_live(self):
        self.popup.set_capture_hidden(False)
        self.assertEqual(0, self._affinity())
        self.popup.set_capture_hidden(True)
        self.assertEqual(_WDA_EXCLUDEFROMCAPTURE, self._affinity())


class WiringTests(unittest.TestCase):
    def test_runtime_setting_keys_are_wired(self):
        import app as app_mod
        src = inspect.getsource(app_mod.WhisperFlowApp._apply_runtime_setting)
        self.assertIn('"show_pill_arrows"', src)
        self.assertIn('"hide_popup_in_screenshots"', src)
        self.assertIn("set_pill_arrows", src)
        self.assertIn("set_capture_hidden", src)

    def test_startup_pushes_both_flags_to_the_popup(self):
        import app as app_mod
        src = inspect.getsource(app_mod.WhisperFlowApp.__init__)
        self.assertIn("set_pill_arrows", src)
        self.assertIn("set_capture_hidden", src)

    def test_settings_ui_offers_both_toggles(self):
        import app_window
        with open(app_window.__file__, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('_toggle_card("show_pill_arrows"', text)
        self.assertIn('_toggle_card("hide_popup_in_screenshots"', text)

    def test_the_affinity_targets_the_real_toplevel_not_the_child(self):
        src = inspect.getsource(FloatingPopup._apply_capture_affinity)
        self.assertIn("_top_hwnd", src)
        self.assertNotIn("_popup_hwnd,", src)


if __name__ == "__main__":
    unittest.main()
