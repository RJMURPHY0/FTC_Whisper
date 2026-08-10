"""A dropdown list must not orphan on top of another window.

The house Dropdown posts a -topmost overrideredirect Toplevel for its list. It
already dismisses on Escape and on a click elsewhere INSIDE the app, but nothing
closed it when the app itself lost the foreground — so clicking away to another
window left the list floating on top of it while the rest of the app went behind.
A foreground watch (`_watch_foreground`) closes the list the moment the app stops
being the OS foreground window.
"""
import inspect
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_window import Dropdown


class SourceInvariantTests(unittest.TestCase):
    """The wiring that makes deactivation dismiss the list — checked at the
    source level so a live rig with real window focus isn't required."""

    def test_open_starts_the_foreground_watch(self):
        self.assertIn("_watch_foreground", inspect.getsource(Dropdown.open))

    def test_close_cancels_the_foreground_watch(self):
        src = inspect.getsource(Dropdown.close)
        self.assertIn("_fg_watch", src)
        self.assertIn("after_cancel", src)

    def test_watch_consults_the_os_foreground_window(self):
        self.assertIn("GetForegroundWindow",
                      inspect.getsource(Dropdown._foreground_is_ours))

    def test_watch_reschedules_only_while_open(self):
        # It must stop looping once the list is gone, not spin forever.
        src = inspect.getsource(Dropdown._watch_foreground)
        self.assertIn("self._menu is None", src)

    def test_foreground_check_fails_open(self):
        # A Win32 hiccup must never close a list the user is reading: the guard
        # returns True (ours) on any exception rather than dismissing.
        src = inspect.getsource(Dropdown._foreground_is_ours)
        self.assertIn("return True", src)


class _StubMenu:
    """Stand-in for the open list Toplevel: destroy() is all close() needs."""
    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class WatchBehaviourTests(unittest.TestCase):
    """Behavioural checks that don't need a real Tk display: drive
    `_watch_foreground` against a stubbed menu and a stubbed foreground verdict.
    """

    def _dropdown(self):
        d = Dropdown.__new__(Dropdown)
        d._menu = _StubMenu()
        d._menu_cv = object()
        d._fg_watch = "after#1"
        d._painted = []
        d._cancelled = []
        d._scheduled = []
        d.after = lambda ms, fn: (d._scheduled.append((ms, fn)), "after#next")[1]
        d.after_cancel = lambda tok: d._cancelled.append(tok)
        d._paint = lambda: d._painted.append(True)
        return d

    def test_losing_foreground_closes_the_list(self):
        d = self._dropdown()
        d._foreground_is_ours = lambda: False
        d._watch_foreground()
        self.assertIsNone(d._menu)               # list dismissed
        self.assertTrue(d._painted)              # closed control repainted
        self.assertEqual([], d._scheduled)       # no further polling

    def test_keeping_foreground_reschedules_the_watch(self):
        d = self._dropdown()
        d._foreground_is_ours = lambda: True
        d._watch_foreground()
        self.assertIsNotNone(d._menu)            # list still open
        self.assertEqual(1, len(d._scheduled))   # polled again
        self.assertEqual("after#next", d._fg_watch)

    def test_watch_is_inert_after_close(self):
        d = self._dropdown()
        d._menu = None                           # already closed
        d._foreground_is_ours = lambda: False
        d._watch_foreground()
        self.assertEqual([], d._scheduled)       # does not reschedule

    def test_close_cancels_a_pending_watch(self):
        d = self._dropdown()
        Dropdown.close(d)
        self.assertIsNone(d._menu)
        self.assertIsNone(d._fg_watch)
        self.assertIn("after#1", d._cancelled)   # timer torn down


class ForegroundVerdictTests(unittest.TestCase):
    """`_foreground_is_ours` degrades safely without a real HWND / Win32."""

    def test_no_win32_fails_open(self):
        d = Dropdown.__new__(Dropdown)
        d._menu = None
        # No winfo_toplevel / ctypes usable here → the guard must not raise and
        # must fail open (treat the app as foreground) rather than close.
        d.winfo_toplevel = lambda: (_ for _ in ()).throw(RuntimeError("no tk"))
        self.assertTrue(Dropdown._foreground_is_ours(d))


if __name__ == "__main__":
    unittest.main()
