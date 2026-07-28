import unittest
from types import SimpleNamespace

from app_window import AppWindow, DASH_H, MIN_H, MIN_W, WINDOW_W


class _FakeRoot:
    def __init__(self, w=500, h=700, state="normal",
                 screen_w=1920, screen_h=1080):
        self._w, self._h = w, h
        self._state = state
        self._screen_w, self._screen_h = screen_w, screen_h
        self.jobs = []
        self.cancelled = set()
        self.geometry_calls = []

    def winfo_width(self):
        return self._w

    def winfo_height(self):
        return self._h

    def winfo_screenwidth(self):
        return self._screen_w

    def winfo_screenheight(self):
        return self._screen_h

    def state(self):
        return self._state

    def geometry(self, spec):
        self.geometry_calls.append(spec)

    def after(self, delay, callback, *args):
        job = len(self.jobs) + 1
        self.jobs.append((job, delay, callback, args))
        return job

    def after_cancel(self, job):
        self.cancelled.add(job)

    def run_jobs(self):
        while self.jobs:
            job, _delay, callback, args = self.jobs.pop(0)
            if job not in self.cancelled:
                callback(*args)


class _FakeConfig(SimpleNamespace):
    def __init__(self, **kw):
        super().__init__(window_sizes={}, **kw)
        self.saves = 0

    def save_async(self):
        self.saves += 1


class _FakeDb:
    def __init__(self):
        self.pushed = []

    def set_app_setting(self, key, value):
        self.pushed.append((key, value))

    def fetch_app_setting(self, _key):
        return ""


def _window(email="user@example.com", config=None, root=None, db=None):
    window = object.__new__(AppWindow)
    window._auth = SimpleNamespace(user_email=email)
    window._config = config if config is not None else _FakeConfig()
    window._root = root if root is not None else _FakeRoot()
    window._db = db
    window._applied_size = None
    window._dash_visible = True
    window._win_save_job = None
    window._install_default_checked = False
    return window


class ParseSizeTests(unittest.TestCase):
    def test_valid_and_invalid_inputs(self):
        self.assertEqual((420, 640), AppWindow._parse_size("420x640"))
        self.assertEqual((500, 900), AppWindow._parse_size("500X900"))
        self.assertIsNone(AppWindow._parse_size(""))
        self.assertIsNone(AppWindow._parse_size(None))
        self.assertIsNone(AppWindow._parse_size("wide"))
        self.assertIsNone(AppWindow._parse_size("420x"))
        self.assertIsNone(AppWindow._parse_size("420x640x2"))

    def test_out_of_range_sizes_are_rejected(self):
        self.assertIsNone(AppWindow._parse_size("10x10"))
        self.assertIsNone(AppWindow._parse_size("9000x9000"))
        self.assertIsNone(AppWindow._parse_size(f"{MIN_W - 1}x{MIN_H}"))


class SavedDashSizeTests(unittest.TestCase):
    def test_account_size_beats_install_default_beats_builtin(self):
        config = _FakeConfig()
        window = _window(config=config)

        self.assertEqual((WINDOW_W, DASH_H), window._saved_dash_size())

        config.window_sizes = {"_default": "460x700"}
        self.assertEqual((460, 700), window._saved_dash_size())

        config.window_sizes["user@example.com"] = "520x800"
        self.assertEqual((520, 800), window._saved_dash_size())

    def test_saved_size_is_clamped_to_screen(self):
        config = _FakeConfig()
        config.window_sizes = {"user@example.com": "3000x2000"}
        window = _window(config=config,
                         root=_FakeRoot(screen_w=1280, screen_h=720))
        self.assertEqual((1280, 720 - 40), window._saved_dash_size())

    def test_signed_out_uses_default_bucket(self):
        config = _FakeConfig()
        config.window_sizes = {"_default": "460x700"}
        window = _window(email="", config=config)
        self.assertEqual("_default", window._account_size_key())
        self.assertEqual((460, 700), window._saved_dash_size())


class PersistWindowSizeTests(unittest.TestCase):
    def test_persists_current_size_for_the_account(self):
        config = _FakeConfig()
        window = _window(config=config, root=_FakeRoot(w=510, h=730))

        window._persist_window_size()

        self.assertEqual("510x730", config.window_sizes["user@example.com"])
        self.assertEqual(1, config.saves)
        self.assertEqual((510, 730), window._applied_size)

    def test_maximised_state_is_never_persisted(self):
        config = _FakeConfig()
        window = _window(config=config,
                         root=_FakeRoot(w=1920, h=1040, state="zoomed"))

        window._persist_window_size()

        self.assertEqual({}, config.window_sizes)
        self.assertEqual(0, config.saves)

    def test_unchanged_size_does_not_rewrite(self):
        config = _FakeConfig()
        config.window_sizes = {"user@example.com": "510x730"}
        window = _window(config=config, root=_FakeRoot(w=510, h=730))

        window._persist_window_size()

        self.assertEqual(0, config.saves)

    def test_super_admin_resize_pushes_the_install_default(self):
        config = _FakeConfig()
        db = _FakeDb()
        window = _window(email="Ryan.Murphy@ftc-ss.com", config=config,
                         root=_FakeRoot(w=444, h=666), db=db)

        window._persist_window_size()

        self.assertEqual("444x666", config.window_sizes["ryan.murphy@ftc-ss.com"])
        self.assertEqual([("default_window_size", "444x666")], db.pushed)

    def test_normal_account_never_pushes_the_install_default(self):
        db = _FakeDb()
        window = _window(config=_FakeConfig(),
                         root=_FakeRoot(w=444, h=666), db=db)

        window._persist_window_size()

        self.assertEqual([], db.pushed)


class RootConfigureTests(unittest.TestCase):
    def test_programmatic_resize_echo_is_ignored(self):
        root = _FakeRoot()
        window = _window(root=root)
        window._applied_size = (500, 700)

        window._on_root_configure(
            SimpleNamespace(widget=root, width=500, height=700))

        # The echo must never arm the size-SAVE debounce. A repaint-heal job
        # is allowed: _on_root_configure deliberately schedules a debounced
        # _repaint_all on every real size change (login→dashboard jump ghost).
        self.assertIsNone(window._win_save_job)
        self.assertNotIn(window._persist_window_size,
                         [job[2] for job in root.jobs])

    def test_child_configure_events_are_ignored(self):
        root = _FakeRoot()
        window = _window(root=root)

        window._on_root_configure(
            SimpleNamespace(widget=object(), width=100, height=100))

        self.assertIsNone(window._win_save_job)

    def test_user_resize_debounces_to_one_save_job(self):
        root = _FakeRoot()
        window = _window(root=root)
        window._applied_size = (420, 640)

        window._on_root_configure(
            SimpleNamespace(widget=root, width=430, height=640))
        first = window._win_save_job
        window._on_root_configure(
            SimpleNamespace(widget=root, width=440, height=640))

        self.assertIn(first, root.cancelled)
        self.assertIsNotNone(window._win_save_job)

    def test_login_screen_resizes_are_not_persisted(self):
        root = _FakeRoot()
        window = _window(root=root)
        window._dash_visible = False

        window._on_root_configure(
            SimpleNamespace(widget=root, width=430, height=640))

        self.assertIsNone(window._win_save_job)


class InstallDefaultTests(unittest.TestCase):
    def test_adopt_stores_default_only_when_no_size_exists(self):
        config = _FakeConfig()
        root = _FakeRoot()
        window = _window(config=config, root=root)
        window._applied_size = (WINDOW_W, DASH_H)

        window._adopt_install_default((480, 720))

        self.assertEqual({"_default": "480x720"}, config.window_sizes)
        # The freshly-adopted default is applied to the live window.
        self.assertIn("480x720", root.geometry_calls[-1])

    def test_adopt_never_overwrites_an_existing_size(self):
        config = _FakeConfig()
        config.window_sizes = {"user@example.com": "510x730"}
        window = _window(config=config)

        window._adopt_install_default((480, 720))

        self.assertEqual({"user@example.com": "510x730"}, config.window_sizes)

    def test_fetch_is_skipped_once_any_size_exists(self):
        config = _FakeConfig()
        config.window_sizes = {"_default": "480x720"}
        db = _FakeDb()
        window = _window(config=config, db=db)

        window._maybe_fetch_install_default()

        self.assertTrue(window._install_default_checked)


if __name__ == "__main__":
    unittest.main()
