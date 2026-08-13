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
        # _window_monitor_centre runs on the PRIVATE typed user32 (immune to
        # shared-windll poisoning), so the fake has to stand in for that too.
        real_mon_u32 = app_mod._monitor_user32
        app_mod._monitor_user32 = lambda: _FakeU32
        try:
            return app_mod.WhisperFlowApp._popup_anchor(self.whisper, hwnd)
        finally:
            app_mod._capture_focus_target = real_capture
            app_mod.ctypes.windll = real_windll
            app_mod._monitor_user32 = real_mon_u32

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


class SharedUser32HygieneTests(unittest.TestCase):
    """Root cause of the whole wrong-screen saga (fixed v1.6.65).

    ctypes.windll is CACHED: windll.user32 is ONE object shared by every
    module in the process. app_window pinned GetMonitorInfoW.argtypes to its
    own _MONITORINFO class on that shared object (v1.6.51), so popup.py's
    call with ITS own struct class raised ctypes.ArgumentError, the except
    swallowed it, and every popup placement fell back to the PRIMARY
    monitor — no matter what the anchor said. Four releases of anchor-policy
    fixes (v1.6.53→v1.6.64) fought the wrong layer; the repro harnesses drove
    the popup WITHOUT the dashboard, so the poisoning never showed in tests.
    These tests exercise the modules TOGETHER, in the poisoning order.
    """

    @staticmethod
    def _fake_widget():
        return types.SimpleNamespace(winfo_id=lambda: 0,
                                     winfo_screenwidth=lambda: 1600,
                                     winfo_screenheight=lambda: 900)

    @staticmethod
    def _win32_monitor_available() -> bool:
        """A real monitor lookup must work in this session at all (a bare CI
        window station may have none) — otherwise the cross-module tests
        can't distinguish poisoning from a headless environment."""
        import ctypes

        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        try:
            u32 = ctypes.WinDLL("user32")
            u32.MonitorFromPoint.restype = ctypes.c_void_p
            u32.MonitorFromPoint.argtypes = [_PT, ctypes.c_ulong]
            return bool(u32.MonitorFromPoint(_PT(5, 5), 2))
        except Exception:
            return False

    def test_app_window_leaves_the_shared_user32_untyped(self):
        import ctypes
        _monitor_work_area(self._fake_widget())  # runs the typing path
        shared = ctypes.windll.user32
        self.assertIsNone(shared.GetMonitorInfoW.argtypes,
                          "app_window typed the SHARED user32 — that breaks "
                          "every other module's GetMonitorInfoW call")
        self.assertIsNone(shared.MonitorFromWindow.argtypes)

    def test_popup_monitor_lookup_survives_app_window_running_first(self):
        # The exact shipped sequence: dashboard resolves its work area first
        # (poisoning point in the old code), then the popup places itself.
        if not self._win32_monitor_available():
            raise unittest.SkipTest("no monitor in this window station")
        import popup
        _monitor_work_area(self._fake_widget())
        sentinel = types.SimpleNamespace(
            root=types.SimpleNamespace(winfo_screenwidth=lambda: -1,
                                       winfo_screenheight=lambda: -1),
            _target_hwnd=0)
        wa = popup.FloatingPopup._get_monitor_workarea(sentinel, 5, 5)
        # (5,5) is on the primary monitor by definition; a REAL work area
        # must come back — -1 marks the Tk-numbers fallback the poisoned
        # build collapsed to on every single placement.
        self.assertNotIn(-1, wa,
                         "popup fell back to primary-screen Tk numbers")
        self.assertGreater(wa[2], wa[0])
        self.assertGreater(wa[3], wa[1])

    def test_window_monitor_centre_survives_app_window_running_first(self):
        if not self._win32_monitor_available():
            raise unittest.SkipTest("no monitor in this window station")
        import ctypes
        import app as app_mod
        _monitor_work_area(self._fake_widget())
        hwnd = ctypes.windll.user32.GetDesktopWindow()
        (_, why) = app_mod.WhisperFlowApp._window_monitor_centre(
            hwnd, (0, 0, 100, 100))
        self.assertEqual("window-monitor", why,
                         "the anchor silently degraded to window-centre — "
                         "the poisoned-GetMonitorInfoW failure mode")

    def test_no_module_types_the_shared_windll(self):
        # Static: the direct form is forbidden outright in every root module.
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for name in os.listdir(root):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as f:
                src = f.read()
            for m in re.finditer(
                    r"windll\.\w+\.\w+\.(argtypes|restype)\s*=", src):
                offenders.append(f"{name}: {m.group(0)}")
        self.assertEqual([], offenders)
        # Runtime: import every module that types Win32 signatures, run the
        # one that types at call time, then confirm the SHARED cache is still
        # pristine on every function any of them touch. (The aliased form
        # can't be caught reliably by regex — app.py legitimately binds `u32`
        # to the shared object in untyped functions.)
        import ctypes
        import app  # noqa: F401 — imported for its module-level ctypes work
        import app_icons  # noqa: F401
        import injector  # noqa: F401
        import popup as popup_mod
        _monitor_work_area(self._fake_widget())
        popup_mod._monitor_user32()
        for dll, fn in (("user32", "GetMonitorInfoW"),
                        ("user32", "MonitorFromWindow"),
                        ("user32", "MonitorFromPoint"),
                        ("user32", "GetForegroundWindow"),
                        ("user32", "GetAncestor"),
                        ("user32", "PrivateExtractIconsW"),
                        ("shell32", "ExtractIconExW")):
            func = getattr(getattr(ctypes.windll, dll), fn)
            self.assertIsNone(
                func.argtypes,
                f"shared windll {dll}.{fn}.argtypes was set by a module")
            self.assertIs(
                func.restype, ctypes.c_int,
                f"shared windll {dll}.{fn}.restype was changed by a module")


if __name__ == "__main__":
    unittest.main()
