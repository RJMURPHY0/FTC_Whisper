"""Session restore after a reboot, and the page-swap repaint.

Two shipped bugs are pinned here.

1. A cold boot starts the app before the network is up, so the restore overruns
   the wait. The old code abandoned the worker, returned False, started a SECOND
   restore against the same on-disk refresh token, and Supabase — which rotates
   refresh tokens — answered "Refresh Token Not Found". That text matched the
   auth-failure heuristic, the session file was deleted, and the user had to
   sign in again. Meanwhile the retry loop saw is_authenticated and returned
   WITHOUT promoting, leaving the login form up over a signed-in app.

2. The login->dashboard swap moves and resizes the window AFTER the atomic
   present, and RDW_UPDATENOW only validates Tk's update region (Tk draws on a
   later mainloop spin). Without a heal after the geometry change, the old
   page's pixels get blitted into the new position and stay there.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
from auth import AuthManager, _is_definitive_auth_error


class ErrorClassifierTests(unittest.TestCase):
    def test_dead_tokens_are_auth_errors(self):
        for msg in ("Invalid Refresh Token: Refresh Token Not Found",
                    "invalid_grant", "JWT expired", "Unauthorized"):
            self.assertTrue(_is_definitive_auth_error(msg), msg)

    def test_network_failures_are_never_auth_errors(self):
        # These carry words like "not found" / "invalid" but mean the network
        # is down, not that the credentials are dead.
        for msg in ("[Errno 11001] getaddrinfo failed",
                    "ConnectError: All connection attempts failed",
                    "Read timed out",
                    "SSLError: certificate verify failed",
                    "Name or service not known",
                    "Max retries exceeded with url"):
            self.assertFalse(_is_definitive_auth_error(msg), msg)

    def test_empty_message_is_not_an_auth_error(self):
        self.assertFalse(_is_definitive_auth_error(""))
        self.assertFalse(_is_definitive_auth_error(None))


class _StubAuth(AuthManager):
    """AuthManager with the network and disk replaced."""

    def __init__(self, error=None, tokens=("at", "rt")):
        super().__init__("https://example.invalid", "key")
        self._error = error
        self._tokens = tokens
        self.cleared = False

    def _clear_session(self):
        self.cleared = True
        self._user = None


class RestoreDoesNotDeleteGoodSessionsTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(
            os.environ.get("TEMP", "."), "ftc_whisper_test_session.bin")
        # Plain JSON — the legacy on-disk shape, which _restore_once parses via
        # its non-DPAPI fallback, so the test reaches the network step.
        with open(self.path, "wb") as f:
            f.write(b'{"access_token": "at", "refresh_token": "rt"}')
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _restore_raising(self, mgr, exc):
        def _boom(_raw):
            raise exc
        # Force the failure at the decrypt step, which is inside the try block.
        original = auth._dpapi_decrypt
        auth._dpapi_decrypt = _boom
        self.addCleanup(setattr, auth, "_dpapi_decrypt", original)
        return mgr._restore_once(self.path)

    def test_network_failure_keeps_the_session_file(self):
        mgr = _StubAuth()
        # A decrypt/parse failure has its own guard and must never clear.
        self.assertFalse(self._restore_raising(mgr, RuntimeError("getaddrinfo failed")))
        self.assertFalse(mgr.cleared)

    def test_auth_error_is_ignored_when_already_signed_in(self):
        """A late-landing attempt already signed us in — a loser's rotation
        error must not delete the session it just refreshed."""
        mgr = _StubAuth()
        mgr._user = types.SimpleNamespace(id="u", email="a@b.c")
        mgr._get_client = lambda: (_ for _ in ()).throw(
            RuntimeError("Invalid Refresh Token: Refresh Token Not Found"))
        self.assertFalse(mgr._restore_once(self.path))
        self.assertFalse(mgr.cleared)

    def test_auth_error_is_ignored_when_the_file_was_refreshed_meanwhile(self):
        mgr = _StubAuth()

        def _rotated():
            with open(self.path, "wb") as f:
                f.write(b"fresher-bytes")
            raise RuntimeError("Refresh Token Not Found")

        mgr._get_client = lambda: _rotated()
        self.assertFalse(mgr._restore_once(self.path))
        self.assertFalse(mgr.cleared)

    def test_a_genuine_auth_error_still_clears(self):
        mgr = _StubAuth()
        mgr._get_client = lambda: (_ for _ in ()).throw(
            RuntimeError("Invalid Refresh Token: Refresh Token Not Found"))
        self.assertFalse(mgr._restore_once(self.path))
        self.assertTrue(mgr.cleared)


class LateRestorePromotesTests(unittest.TestCase):
    def setUp(self):
        # _session_path is a module global; restore it or later tests in
        # the suite read this test's temp file.
        self.addCleanup(setattr, auth, "_session_path", auth._session_path)

    def test_a_restore_that_lands_after_the_wait_fires_the_listener(self):
        import threading
        released = threading.Event()
        mgr = _StubAuth()
        fired = []
        mgr.set_restore_listener(lambda: fired.append(True))

        def _slow(_path):
            released.wait(5.0)
            mgr._user = types.SimpleNamespace(id="u", email="a@b.c")
            return True

        mgr._restore_once = _slow
        path = os.path.join(os.environ.get("TEMP", "."),
                            "ftc_whisper_test_session2.bin")
        with open(path, "wb") as f:
            f.write(b"x")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        auth._session_path = lambda: path

        self.assertFalse(mgr._load_saved_session(wait_seconds=0.05))
        self.assertEqual([], fired)          # nothing yet — still in flight
        self.assertTrue(mgr.restore_in_flight)
        released.set()
        mgr._restore_thread.join(5.0)
        self.assertEqual([True], fired)      # promoted late, not dropped

    def test_a_second_call_waits_instead_of_starting_a_rival_restore(self):
        import threading
        started = []
        gate = threading.Event()
        mgr = _StubAuth()

        def _slow(_path):
            started.append(1)
            gate.wait(5.0)
            mgr._user = types.SimpleNamespace(id="u", email="a@b.c")
            return True

        mgr._restore_once = _slow
        path = os.path.join(os.environ.get("TEMP", "."),
                            "ftc_whisper_test_session3.bin")
        with open(path, "wb") as f:
            f.write(b"x")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        auth._session_path = lambda: path

        mgr._load_saved_session(wait_seconds=0.05)
        # The retry loop fires again while the first attempt is STILL talking to
        # the server. Replaying the same (already-rotated) refresh token is what
        # produced "Refresh Token Not Found" and deleted a good session, so this
        # call must join the attempt in flight, not start a rival one.
        second = threading.Thread(
            target=lambda: mgr._load_saved_session(wait_seconds=5.0))
        second.start()
        threading.Event().wait(0.2)
        self.assertEqual(1, len(started))
        gate.set()
        second.join(5.0)
        mgr._restore_thread.join(5.0)
        self.assertEqual(1, len(started))
        self.assertTrue(mgr.is_authenticated)


class _FakeRoot:
    def __init__(self):
        self.after_calls = []
        self.geometry_calls = []

    def after(self, ms, fn=None, *a):
        self.after_calls.append((ms, fn))
        return f"job{len(self.after_calls)}"

    def after_cancel(self, _job):
        pass

    def winfo_screenheight(self):
        return 1080

    def winfo_screenwidth(self):
        return 1920


class PageSwapHealTests(unittest.TestCase):
    """The one-frame present must repaint AFTER it applies the deferred
    geometry, and the Configure heal must fire on a MOVE as well as a resize (a
    page swap recentres the window, so two same-height pages only ever move).

    The mechanics live in ui_atomic (shared with the embedded login page), so
    these assert against it plus AppWindow's delegation to it."""

    def test_atomic_ui_repaints_after_the_deferred_geometry(self):
        import inspect
        import ui_atomic
        src = inspect.getsource(ui_atomic.atomic)
        geo_at = src.index("root.geometry(geo)")
        heal_at = src.index("repaint(hwnd, erase=moved)")
        self.assertLess(geo_at, heal_at,
                        "the heal must come after the geometry change")

    def test_atomic_ui_defers_the_resize_out_of_the_freeze(self):
        """AppWindow still owns _in_atomic/_pending_geometry: _resize() checks
        the flag, and geometry() must run after painting is back on."""
        import inspect
        from app_window import AppWindow
        src = inspect.getsource(AppWindow._atomic_ui)
        self.assertIn("self._in_atomic = True", src)
        self.assertIn("geometry=_geometry", src)
        self.assertIn("_pending_geometry", src)

    def test_erase_is_reserved_for_swaps_that_moved_the_window(self):
        """Erasing a same-size swap flashes the whole window background for a
        frame. Only a move/resize exposes pixels no widget repaints over."""
        import inspect
        import ui_atomic
        src = inspect.getsource(ui_atomic.atomic)
        self.assertIn("moved = True", src)
        self.assertNotIn("repaint(hwnd, erase=True)", src)

    def test_nested_atomic_never_presents_early(self):
        """WM_SETREDRAW is a flag, not a counter: an inner unfreeze would
        present the outer change half-done."""
        import inspect
        import ui_atomic
        src = inspect.getsource(ui_atomic.atomic)
        self.assertIn("hwnd in _frozen", src)

    def test_configure_heal_is_gated_on_geometry_not_size(self):
        import inspect
        from app_window import AppWindow
        src = inspect.getsource(AppWindow._on_root_configure)
        self.assertIn("_last_repaint_geom", src)
        self.assertIn("erase=True", src)

    def test_repaint_all_never_targets_the_desktop(self):
        import inspect
        import ui_atomic
        src = inspect.getsource(ui_atomic.repaint)
        # RedrawWindow(NULL) repaints the whole screen; top_hwnd returns 0 on
        # failure, so the guard has to be there.
        self.assertIn("if not hwnd:", src)


class SplashRevealTests(unittest.TestCase):
    """The splash must not drop to the login form while a restore is running,
    and must promote (not just return) once the session is valid."""

    def _window(self, authenticated=False, saved=True, in_flight=False):
        from app_window import AppWindow
        w = AppWindow.__new__(AppWindow)
        w._root = _FakeRoot()
        w._auth = types.SimpleNamespace(
            is_authenticated=authenticated,
            has_saved_session=lambda: saved,
            restore_in_flight=in_flight,
        )
        w.calls = []
        w._promote_restored_session = lambda: w.calls.append("promote")
        w._reveal_login_now = lambda: w.calls.append("reveal")
        return w

    def test_in_flight_restore_reschedules_instead_of_revealing(self):
        w = self._window(in_flight=True)
        w._reveal_login_if_pending()
        self.assertEqual([], w.calls)
        self.assertTrue(w._root.after_calls)

    def test_idle_restore_reveals_the_form(self):
        w = self._window(in_flight=False)
        w._reveal_login_if_pending()
        self.assertEqual(["reveal"], w.calls)

    def test_authenticated_promotes_rather_than_returning(self):
        w = self._window(authenticated=True)
        w._reveal_login_if_pending()
        self.assertEqual(["promote"], w.calls)

    def test_cleared_session_reveals_immediately(self):
        w = self._window(saved=False, in_flight=True)
        w._reveal_login_if_pending()
        self.assertEqual(["reveal"], w.calls)

    def test_retry_loop_promotes_when_already_authenticated(self):
        import inspect
        from app_window import AppWindow
        src = inspect.getsource(AppWindow._session_restore_retry_loop)
        head = src[:src.index("if not self._auth.has_saved_session()")]
        self.assertIn("_promote_restored_session", head,
                      "the is_authenticated early-return must promote first")


if __name__ == "__main__":
    unittest.main()
