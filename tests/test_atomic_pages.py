"""No page may reflow live — every page, not just the ones that were reported.

Tk paints asynchronously, so mapping or unmapping a widget while the window is
visible is drawn over several frames: the user sees rows tear, duplicate, or a
strip of the previous layout. The fix that landed for the dashboard's impact
cards is the general one — freeze, mutate, present once (ui_atomic.atomic) —
and this pins it across the whole app so a new pack() somewhere quiet cannot
reintroduce the glitch:

  * Home        — the push-to-talk hint row
  * Hotkey /
    Settings /
    History     — tab swaps, the mic-test meter, the history re-render
  * Sign-in     — the status banner, login↔sign-up, the resend link
  * Splash      — the "Sign in manually" escape link
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ui_atomic

APP_WINDOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app_window.py")
LOGIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "login_window.py")


def _source(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def _method(src, name):
    """Body of `def name(` up to the next same-indent def."""
    start = src.index(f"    def {name}(")
    rest = src[start + 10:]
    end = rest.find("\n    def ")
    return rest if end < 0 else rest[:end]


class DashboardPageTests(unittest.TestCase):
    def setUp(self):
        self.src = _source(APP_WINDOW)

    def test_tab_swap_is_one_frame(self):
        body = _method(self.src, "_switch_dash_tab")
        self.assertIn("self._atomic_ui(_swap)", body)
        # The map/unmap must sit INSIDE the frozen closure. Done live and
        # chased with repaints afterwards (what shipped before), each repaint
        # is a race against Tk's own painting and losing one is the flicker.
        self.assertLess(body.index("def _swap():"), body.index("grid_remove()"))
        self.assertLess(body.index("grid_remove()"),
                        body.index("self._atomic_ui(_swap)"))

    def test_home_ptt_row_is_one_frame(self):
        body = _method(self.src, "_update_home_ptt_row")
        self.assertIn("self._atomic_ui(", body)

    def test_history_render_is_one_frame(self):
        body = _method(self.src, "_render_history")
        self.assertIn("self._atomic_ui(self._render_history_body", body)

    def test_in_page_changes_do_not_erase_the_whole_window(self):
        """Freeze/present makes them one frame; an erase on top of that would
        flash the entire window for a change the user expects to be local."""
        for call in ("self._atomic_ui(_hide_meter",
                     "self._atomic_ui(_show_meter",
                     "self._atomic_ui(self._render_history_body",
                     "self._atomic_ui(lambda: lbl.pack"):
            with self.subTest(change=call):
                at = self.src.index(call)
                self.assertIn("erase=False", self.src[at:at + 120])

    def test_mic_test_meter_is_one_frame(self):
        # Both directions: showing the meter grows the mic card, hiding it
        # shrinks it, and each reflows every card below.
        self.assertIn("self._atomic_ui(_hide_meter", self.src)
        self.assertIn("self._atomic_ui(_show_meter", self.src)

    def test_splash_escape_link_is_one_frame(self):
        body = _method(self.src, "_show_signin_escape")
        self.assertIn("self._atomic_ui(", body)

    def test_page_swaps_are_one_frame(self):
        for name in ("_switch_to_login", "_switch_to_dashboard",
                     "_switch_to_signing_in"):
            with self.subTest(page=name):
                self.assertIn("self._atomic_ui(_swap)", _method(self.src, name))

    def test_nested_atomic_ui_defers_to_the_outer_frame(self):
        """The login page's reset() presents, and calls _switch() which
        presents too. The inner call must not clear _in_atomic underneath the
        outer one — that would let the rest of the outer change resize while
        the window is frozen (the unmapped-frame white strip)."""
        body = _method(self.src, "_atomic_ui")
        head = body[:body.index("self._pending_geometry = None")]
        self.assertIn('getattr(self, "_in_atomic", False)', head)
        self.assertIn("fn()", head)

    def test_no_page_swap_resizes_inside_the_freeze(self):
        """_resize must stay the only geometry path: it defers while frozen."""
        body = _method(self.src, "_resize")
        self.assertIn("_in_atomic", body)
        self.assertIn("self._pending_geometry = geo", body)


class SignInPageTests(unittest.TestCase):
    def setUp(self):
        self.src = _source(LOGIN)

    def test_status_banner_is_one_frame(self):
        self.assertIn("self._atomic(_apply)", _method(self.src, "_set_status"))

    def test_mode_switch_is_one_frame(self):
        self.assertIn("self._atomic(_apply)", _method(self.src, "_switch"))

    def test_reset_is_one_frame(self):
        self.assertIn("self._atomic(_apply)", _method(self.src, "reset"))

    def test_resend_link_is_one_frame(self):
        self.assertIn("self._atomic(_show_resend)",
                      _method(self.src, "_handle_result"))

    def test_embedded_login_freezes_the_hosting_window(self):
        """Embedded, the login packs into the MAIN window — freezing its own
        widget's toplevel would be the same HWND, but the hook keeps the two
        classes from disagreeing about which window owns the frame."""
        self.assertIn("atomic=self._atomic_ui", _source(APP_WINDOW))
        body = _method(self.src, "_atomic")
        self.assertIn("self._atomic_hook", body)
        self.assertIn("ui_atomic.atomic(root, fn, erase=False)", body)


class PrimitiveTests(unittest.TestCase):
    def test_atomic_runs_the_mutation_without_a_window(self):
        """Headless (no HWND) it must still run the change, not swallow it."""
        ran = []
        ui_atomic.atomic(_HeadlessRoot(), lambda: ran.append(1))
        self.assertEqual([1], ran)

    def test_a_failing_mutation_still_unfreezes(self):
        root = _HeadlessRoot()
        with self.assertRaises(ValueError):
            ui_atomic.atomic(root, _boom)
        self.assertNotIn(0, ui_atomic._frozen)
        # And the window is left paintable for the next change.
        ran = []
        ui_atomic.atomic(root, lambda: ran.append(1))
        self.assertEqual([1], ran)


def _boom():
    raise ValueError("layout blew up")


class _HeadlessRoot:
    def winfo_id(self):
        raise RuntimeError("no window")

    def winfo_ismapped(self):
        return True

    def update_idletasks(self):
        pass

    def after(self, _ms, _cb):
        return "job"


if __name__ == "__main__":
    unittest.main()
