"""Guards for the blit-free ScrollPane and the ghost-proof tab switch.

The duplicated-card / torn-page ghost was minted by tk.Canvas blit-scroll
copying child-HWND pixels (settings/hotkey cards are real child windows) and
by hidden tabs staying mapped underneath a z-order raise. The structural fix
is: those tabs scroll on ScrollPane (place()-moved content, nothing ever
bit-copies pixels) and inactive tabs are UNMAPPED. These tests pin both.
"""

import inspect
import unittest

import app_window
from app_window import AppWindow, ScrollPane


class BlitFreeInvariantTests(unittest.TestCase):
    def test_settings_and_hotkey_tabs_scroll_without_canvas_blit(self):
        # Rebuilding either tab on a Canvas + create_window scroller brings
        # the duplicated-card ghost back. Do not "simplify" back to it.
        for builder in (AppWindow._build_settings_tab,
                        AppWindow._build_hotkey_tab):
            src = inspect.getsource(builder)
            self.assertIn("ScrollPane(", src,
                          f"{builder.__name__} must scroll via ScrollPane")
            self.assertNotIn(
                "create_window", src,
                f"{builder.__name__} must not embed content in a canvas")

    def test_inactive_tabs_are_unmapped_not_just_lowered(self):
        # An unmapped window has no pixels on screen, so a hidden tab can
        # never bleed through a missed repaint. tkraise alone left the old
        # tab's HWNDs mapped underneath — the "settings over History" ghost.
        src = inspect.getsource(AppWindow._switch_dash_tab)
        self.assertIn("grid_remove", src)


def _make_pane(content_h=1000, viewport_h=200, width=300):
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # headless runner
        raise unittest.SkipTest(f"Tk unavailable: {exc}")
    # Off-screen but mapped: winfo_height() must report real geometry.
    root.geometry(f"{width}x{viewport_h}+20000+20000")
    pane = ScrollPane(root, bg="#000000")
    pane.place(x=0, y=0, width=width, height=viewport_h)
    filler = tk.Frame(pane.content, width=width, height=content_h,
                      bg="#000000")
    filler.pack()
    root.update()
    return root, pane


class ScrollPaneTests(unittest.TestCase):
    def test_metrics_move_and_clamp(self):
        root, pane = _make_pane()
        try:
            content_h, max_top, top = AppWindow._scroll_metrics(pane)
            self.assertEqual(1000.0, content_h)
            self.assertEqual(800.0, max_top)
            self.assertEqual(0.0, top)

            # place_configure applies at idle — flush before reading geometry
            # (the app's animation cadence reaches idle every frame).
            pane.yview_moveto(300 / content_h)
            root.update_idletasks()
            self.assertEqual(-300, pane.content.winfo_y())
            self.assertAlmostEqual(300 / 1000, pane.yview()[0])

            # Over-scroll clamps to the bottom, never past the content.
            pane.yview_moveto(5.0)
            root.update_idletasks()
            self.assertEqual(-800, pane.content.winfo_y())
            # And back past the top clamps to zero.
            pane.yview_moveto(-1.0)
            root.update_idletasks()
            self.assertEqual(0, pane.content.winfo_y())
        finally:
            root.destroy()

    def test_scrollbar_command_path_drives_the_pane(self):
        root, pane = _make_pane()
        try:
            window = object.__new__(AppWindow)
            window._scroll_states = {}
            window._hist_cv = None
            window._scrollbar_command(pane, "moveto", 0.5)
            root.update_idletasks()
            self.assertEqual(-500, pane.content.winfo_y())
        finally:
            root.destroy()

    def test_content_shrink_reclamps_offset(self):
        root, pane = _make_pane()
        try:
            pane.yview_moveto(0.8)  # 800px, the bottom
            for child in pane.content.winfo_children():
                child.configure(height=400)  # content now 400px tall
            root.update()
            # max_top is now 200 — the stored 800 offset must have clamped,
            # not left a blank void below the content.
            self.assertGreaterEqual(pane.content.winfo_y(), -200)
        finally:
            root.destroy()

    def test_scrollbar_feed_reports_fractions(self):
        root, pane = _make_pane()
        try:
            seen = []
            pane.configure(yscrollcommand=lambda a, b: seen.append((a, b)))
            pane.yview_moveto(0.2)
            self.assertTrue(seen)
            first, last = seen[-1]
            self.assertAlmostEqual(0.2, first)
            self.assertAlmostEqual(0.4, last)  # 200px viewport / 1000px content
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
