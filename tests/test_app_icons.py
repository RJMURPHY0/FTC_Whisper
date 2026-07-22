import unittest
from unittest import mock

import app_icons


class AppIconTests(unittest.TestCase):
    def test_packaged_whatsapp_executable_uses_brand_name(self):
        with mock.patch.object(
            app_icons, "_exe_path_for_hwnd",
            return_value=r"C:\Program Files\WindowsApps\WhatsApp.Root.exe",
        ), mock.patch.object(app_icons, "_window_title", return_value="WhatsApp"):
            info = app_icons.capture_app_info(123)

        self.assertEqual("WhatsApp", info["app_name"])

    def tearDown(self):
        app_icons._raw_icon_cache.clear()
        app_icons._icon_cache.clear()

    def test_browser_titles_resolve_known_services_across_separators(self):
        cases = {
            "New chat - Claude - Google Chrome": "Claude",
            "Ask Jack AI — #1 AI Auto - Google Chrome": "Ask Jack AI",
            "Inbox – Gmail — Microsoft Edge": "Gmail",
            "Feature request | GitHub | Zen Browser": "GitHub",
            "Claude — Project — Zen Browser": "Claude",
            "Some Project — Settings — Zen Browser": "Settings",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(expected, app_icons._browser_service_label(title))

    def test_generic_browser_titles_fall_back_to_browser_name(self):
        for title in (
            "New Tab - Google Chrome",
            "about:blank — Microsoft Edge",
            "Google Chrome",
            "Start Page | Zen Browser",
        ):
            with self.subTest(title=title):
                self.assertEqual("", app_icons._browser_service_label(title))

        with (mock.patch.object(app_icons, "_exe_path_for_hwnd",
                                return_value=r"C:\Apps\zen.exe"),
              mock.patch.object(app_icons, "_window_title",
                                return_value="New Tab — Zen Browser")):
            self.assertEqual(
                {"app_name": "Zen", "app_exe": r"C:\Apps\zen.exe"},
                app_icons.capture_app_info(123),
            )

    def test_raw_extraction_retries_failures_then_reuses_success(self):
        sentinel = object()
        path = r"C:\Missing\Example.exe"
        with mock.patch.object(
                app_icons, "_extract_exe_icon",
                side_effect=[None, sentinel]) as extract:
            self.assertIsNone(app_icons._get_raw_exe_icon(path))
            self.assertIs(sentinel, app_icons._get_raw_exe_icon(path))
            self.assertIs(sentinel, app_icons._get_raw_exe_icon(path))
        self.assertEqual(2, extract.call_count)

    def test_failed_render_does_not_poison_photo_cache(self):
        path = r"C:\Missing\Example.exe"
        with mock.patch.object(app_icons, "_get_raw_exe_icon", return_value=None):
            self.assertIsNone(app_icons.get_app_icon(path, "#101010"))
        self.assertEqual({}, app_icons._icon_cache)

    def test_monogram_cache_key_uses_full_normalized_name(self):
        self.assertNotEqual(
            app_icons._monogram_cache_key("Alpha", "#000000"),
            app_icons._monogram_cache_key("Ask Jack AI", "#000000"),
        )
        self.assertEqual(
            app_icons._monogram_cache_key("  Claude ", "#111111"),
            app_icons._monogram_cache_key("claude", "#111111"),
        )

    def test_zen_is_recognized_as_a_browser(self):
        self.assertIn("zen", app_icons._BROWSERS)


if __name__ == "__main__":
    unittest.main()
