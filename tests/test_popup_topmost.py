"""The recording pill must stay on top, redraw where it moved to, and never be
dismissed by a spacebar typed mid-dictation.

Three shipped bugs, one file:

1. `-topmost` only puts a window IN the topmost band — it does not keep it at
   the front OF it. The taskbar is topmost too, so switching apps or clicking
   the taskbar buried the pill behind it.
2. tkinter's winfo_id() is a CHILD window on Windows; Z-order and repaint belong
   to its parent, so every Win32 call aimed at winfo_id() was a silent no-op.
3. Key-dismiss belongs to the post-insert badge alone, but starting a new
   dictation while the badge was up left its keyboard hook live — and the
   badge's own auto-dismiss timer bails on a mode change, so nothing took it
   down. A space typed while recording hid the pill. Now that ANY key
   dismisses, an orphaned hook would kill the recording pill on the first
   character typed, so the unhook-on-mode-change contract matters more.

Plus the badge's dismiss behaviour itself: any key (after a short grace) and
switching to another app both take it down.
"""
import inspect
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import popup as popup_mod
from popup import FloatingPopup


class _FakeUser32:
    """Records the Win32 calls the popup makes, so the Z-order contract can be
    asserted without a window manager."""

    def __init__(self, top_hwnd=4242, exstyle=0x0008):
        self.calls = []
        self._top = top_hwnd
        self._exstyle = exstyle

    def GetAncestor(self, hwnd, flag):
        self.calls.append(("GetAncestor", hwnd, flag))
        return self._top

    def GetWindowLongW(self, hwnd, index):
        return self._exstyle

    def SetWindowPos(self, hwnd, after, x, y, cx, cy, flags):
        self.calls.append(("SetWindowPos", hwnd, after, flags))
        return 1

    def RedrawWindow(self, hwnd, rect, rgn, flags):
        self.calls.append(("RedrawWindow", hwnd, flags))
        return 1

    def __getattr__(self, _name):
        return lambda *a, **k: 0


class _StubPopup(FloatingPopup):
    """A FloatingPopup with no Tk and no Win32 — just the plumbing under test."""

    def __init__(self, u32, hwnd=17):
        self._popup_hwnd = hwnd
        self._popup_top_hwnd = 0
        self._ontop_tick = None
        self._last_geometry = ""
        self._mode = None
        self.root = None
        self._u32 = u32


class _Win32Patch:
    def __init__(self, u32):
        self.u32 = u32

    def __enter__(self):
        self._real = popup_mod.ctypes.windll
        popup_mod.ctypes.windll = types.SimpleNamespace(user32=self.u32)
        return self.u32

    def __exit__(self, *exc):
        popup_mod.ctypes.windll = self._real
        return False


class TopmostTests(unittest.TestCase):
    def test_it_targets_the_real_toplevel_not_the_tk_child(self):
        u32 = _FakeUser32(top_hwnd=9001)
        p = _StubPopup(u32, hwnd=17)
        with _Win32Patch(u32):
            p._assert_topmost()
        pos = [c for c in u32.calls if c[0] == "SetWindowPos"]
        self.assertEqual(1, len(pos))
        self.assertEqual(9001, pos[0][1],
                         "Z-order belongs to the parent; winfo_id() is a child")

    def test_the_raise_uses_hwnd_top_not_hwnd_topmost(self):
        # Measured both ways: SetWindowPos(HWND_TOPMOST) on a window that is
        # ALREADY topmost returns 0 and the Z-order does not move. HWND_TOP is
        # what actually raises it — this is the whole bug.
        u32 = _FakeUser32(exstyle=0x0008)          # already topmost
        p = _StubPopup(u32)
        with _Win32Patch(u32):
            p._assert_topmost()
        pos = [c for c in u32.calls if c[0] == "SetWindowPos"]
        self.assertEqual([0], [c[2] for c in pos], "HWND_TOP")

    def test_a_window_that_lost_the_band_is_put_back_in_it_first(self):
        u32 = _FakeUser32(exstyle=0x0000)          # WS_EX_TOPMOST gone
        p = _StubPopup(u32)
        with _Win32Patch(u32):
            p._assert_topmost()
        self.assertEqual([-1, 0],
                         [c[2] for c in u32.calls if c[0] == "SetWindowPos"],
                         "HWND_TOPMOST establishes the band, HWND_TOP wins it")

    def test_it_never_moves_resizes_or_activates(self):
        u32 = _FakeUser32()
        p = _StubPopup(u32)
        with _Win32Patch(u32):
            p._assert_topmost()
        for _, _, _, flags in [c for c in u32.calls if c[0] == "SetWindowPos"]:
            for flag, name in ((0x0002, "SWP_NOMOVE"), (0x0001, "SWP_NOSIZE"),
                               (0x0010, "SWP_NOACTIVATE")):
                self.assertTrue(flags & flag, name)

    def test_the_toplevel_lookup_is_resolved_once(self):
        u32 = _FakeUser32()
        p = _StubPopup(u32)
        with _Win32Patch(u32):
            for _ in range(5):
                p._assert_topmost()
        self.assertEqual(1, len([c for c in u32.calls if c[0] == "GetAncestor"]),
                         "a 500ms tick must not re-walk the window tree")

    def test_a_repaint_never_targets_hwnd_zero(self):
        # RedrawWindow(NULL, ...) repaints the DESKTOP.
        u32 = _FakeUser32(top_hwnd=0)
        p = _StubPopup(u32, hwnd=0)
        with _Win32Patch(u32):
            p._repaint_popup()
        self.assertEqual([], [c for c in u32.calls if c[0] == "RedrawWindow"])

    def test_showing_the_popup_asserts_z_order_after_handing_focus_back(self):
        # Activating another window raises it, and the taskbar shares our band —
        # asserting before the hand-back would be undone by it.
        src = inspect.getsource(FloatingPopup._show_no_activate)
        self.assertLess(src.index("SetForegroundWindow"), src.index("_assert_topmost"))

    def test_the_recording_pill_raises_once_and_does_not_fight_the_taskbar(self):
        # v1.6.57 revert: the pill is raised ONCE at show so it appears on top,
        # but the 500ms keep-on-top loop is gone. It stays -topmost (above
        # ordinary windows, so you can see you're recording) and yields when the
        # taskbar or another topmost window is raised — instead of jumping back
        # in front every tick and burying the taskbar the user is trying to use.
        src = inspect.getsource(FloatingPopup._show_no_activate)
        self.assertIn("_assert_topmost", src, "still raised once so it appears on top")
        self.assertNotIn("_start_keep_on_top", src,
                         "the continuous re-assert is what buried the taskbar")

    def test_the_keep_on_top_tick_stops_when_the_popup_hides(self):
        self.assertIn("_stop_keep_on_top", inspect.getsource(FloatingPopup._do_hide))

    def test_the_tick_reschedules_only_while_something_is_on_screen(self):
        u32 = _FakeUser32()
        p = _StubPopup(u32)
        p._mode = None
        with _Win32Patch(u32):
            p._keep_on_top()
        self.assertEqual([], [c for c in u32.calls if c[0] == "SetWindowPos"])
        self.assertIsNone(p._ontop_tick)

    def test_the_refinement_panel_stays_on_top_too(self):
        src = inspect.getsource(FloatingPopup._expand_to_panel)
        self.assertIn("_assert_topmost", src)
        self.assertIn("_start_keep_on_top", src)


class PositionNudgeTests(unittest.TestCase):
    """The ▴▾ arrows on the pill lift/drop the fixed popup and the offset sticks."""

    def _popup(self, offset=30, align="centre"):
        p = FloatingPopup.__new__(FloatingPopup)
        p._popup_offset = offset
        p._popup_align = align
        p._status_cx = 0
        p._status_cy = 0
        p._reposition = lambda *a, **k: p.__dict__.setdefault("_repos", []).append(True)
        p._settings_saver = lambda k, v: p.__dict__.setdefault("_saved", []).append((k, v))
        return p

    def test_up_raises_the_offset_by_one_step(self):
        p = self._popup(30)
        FloatingPopup._nudge_position(p, +1)
        self.assertEqual(30 + popup_mod._OFFSET_STEP, p._popup_offset)

    def test_down_lowers_it_and_floors_at_the_baseline(self):
        p = self._popup(popup_mod._OFFSET_STEP // 2)  # less than one step above 0
        FloatingPopup._nudge_position(p, -1)
        self.assertEqual(0, p._popup_offset, "down never drops below the tier baseline")

    def test_up_is_clamped_to_the_max(self):
        p = self._popup(popup_mod._OFFSET_MAX)
        FloatingPopup._nudge_position(p, +1)
        self.assertEqual(popup_mod._OFFSET_MAX, p._popup_offset)

    def test_a_nudge_repositions_live_and_persists(self):
        p = self._popup(30)
        FloatingPopup._nudge_position(p, +1)
        self.assertEqual([True], p._repos, "the pill moves the instant you click")
        self.assertEqual([("popup_offset", 30 + popup_mod._OFFSET_STEP)], p._saved,
                         "and the new offset is saved so it survives a restart")

    def test_a_no_op_nudge_neither_moves_nor_saves(self):
        p = self._popup(popup_mod._OFFSET_MAX)   # already at the ceiling
        FloatingPopup._nudge_position(p, +1)
        self.assertNotIn("_repos", p.__dict__)
        self.assertNotIn("_saved", p.__dict__)

    def test_set_popup_offset_clamps_a_stray_value(self):
        p = FloatingPopup.__new__(FloatingPopup)
        FloatingPopup.set_popup_offset(p, 99999)
        self.assertEqual(popup_mod._OFFSET_MAX, p._popup_offset)
        FloatingPopup.set_popup_offset(p, -5)
        self.assertEqual(0, p._popup_offset)
        FloatingPopup.set_popup_offset(p, "nonsense")
        self.assertEqual(popup_mod._POPUP_OFFSET_DEFAULT, p._popup_offset)

    def test_reposition_lifts_the_fixed_popup_by_the_offset(self):
        # The offset is what clears the pill off the taskbar; it must be applied
        # to the fixed y (and the on-screen clamp below still protects the edge).
        src = inspect.getsource(FloatingPopup._reposition)
        self.assertIn("y -= self._popup_offset", src)

    # ── Horizontal ◂ ▸ arrows ──────────────────────────────────────────────
    def test_left_steps_toward_the_left_and_persists(self):
        p = self._popup(align="centre")
        FloatingPopup._nudge_h_position(p, -1)
        self.assertEqual("left", p._popup_align)
        self.assertEqual([True], p._repos)
        self.assertEqual([("popup_align", "left")], p._saved)

    def test_right_steps_toward_the_right(self):
        p = self._popup(align="centre")
        FloatingPopup._nudge_h_position(p, +1)
        self.assertEqual("right", p._popup_align)

    def test_alignment_stops_at_the_ends(self):
        p = self._popup(align="left")
        FloatingPopup._nudge_h_position(p, -1)          # already leftmost
        self.assertEqual("left", p._popup_align)
        self.assertNotIn("_saved", p.__dict__, "a hard stop saves nothing")

    def test_set_popup_align_rejects_a_stray_value(self):
        p = FloatingPopup.__new__(FloatingPopup)
        FloatingPopup.set_popup_align(p, "sideways")
        self.assertEqual("centre", p._popup_align)
        FloatingPopup.set_popup_align(p, "right")
        self.assertEqual("right", p._popup_align)

    def test_reposition_places_the_popup_by_alignment(self):
        src = inspect.getsource(FloatingPopup._reposition)
        self.assertIn('self._popup_align == "left"', src)
        self.assertIn('self._popup_align == "right"', src)


class RepaintOnMoveTests(unittest.TestCase):
    def test_a_move_forces_a_redraw_now_and_on_the_next_spin(self):
        # RDW_UPDATENOW only validates Tk's update region — Tk turns WM_PAINT
        # into a queued Expose and paints later, so the after(0) twin is the one
        # that drains it.
        src = inspect.getsource(FloatingPopup._reposition)
        self.assertIn("_repaint_popup()", src)
        self.assertIn("after(0, self._repaint_popup)", src)
        self.assertIn("winfo_ismapped", src,
                      "a withdrawn popup has nothing to repaint")

    def test_the_redraw_erases_rather_than_painting_over(self):
        # Painting over cannot clear a strip the window just uncovered.
        src = inspect.getsource(FloatingPopup._repaint_popup)
        self.assertIn("RDW_ERASE", src)
        self.assertIn("RDW_ALLCHILDREN", src)


class KeyDismissTests(unittest.TestCase):
    """ANY key dismisses the post-insert badge — and NOTHING else."""

    def _popup(self):
        p = FloatingPopup.__new__(FloatingPopup)
        p._dismiss_hooks = []
        p._fg_watch_tick = None
        p.unhooked = 0

        def _unreg():
            p.unhooked += 1
            p._dismiss_hooks = []
        p._unregister_key_dismiss = _unreg
        p._stop_foreground_watch = lambda: None
        return p

    def test_starting_a_recording_clears_a_badge_hook(self):
        src = inspect.getsource(FloatingPopup._enter_status_mode)
        self.assertIn("_unregister_key_dismiss", src,
                      "a key typed mid-dictation must not hide the pill")

    def test_the_hook_is_dropped_when_its_badge_is_gone(self):
        p = self._popup()
        p._mode = "status"                 # a new dictation replaced the badge
        p._icon_entered = 100.0
        FloatingPopup._auto_dismiss_icon(p, 50.0)
        self.assertEqual(1, p.unhooked)

    def test_a_stale_timer_never_unhooks_a_newer_badge(self):
        p = self._popup()
        p._mode = "icon"                   # badge #2 is up and owns the hook
        p._icon_entered = 100.0
        p._do_hide = lambda: self.fail("a stale timer must not hide a live badge")
        FloatingPopup._auto_dismiss_icon(p, 50.0)
        self.assertEqual(0, p.unhooked)

    def test_its_own_badge_still_auto_dismisses(self):
        p = self._popup()
        p._mode = "icon"
        p._icon_entered = 50.0
        hidden = []
        p._do_hide = lambda: hidden.append(True)
        FloatingPopup._auto_dismiss_icon(p, 50.0)
        self.assertEqual([True], hidden)

    def test_the_refinement_panel_never_carries_the_hook(self):
        # Enter submits the Ask box there, and the user is typing into it.
        self.assertIn("_unregister_key_dismiss",
                      inspect.getsource(FloatingPopup._expand_to_panel))

    def test_only_the_badge_registers_it(self):
        registers = [name for name, fn in vars(FloatingPopup).items()
                     if callable(fn) and not name.startswith("_register")
                     and "_register_key_dismiss()" in (
                         inspect.getsource(fn) if inspect.isfunction(fn) else "")]
        self.assertEqual(["_enter_icon_mode"], registers)

    # ── any-key dismiss ──────────────────────────────────────────────────

    def test_the_hook_is_global_not_two_named_keys(self):
        # Binding only Space and Enter left the badge sitting there through
        # every other kind of typing — the reported complaint.
        src = inspect.getsource(FloatingPopup._register_key_dismiss)
        self.assertIn("kb.hook(", src)
        self.assertNotIn("on_press_key", src)

    def test_the_keystroke_still_reaches_the_app(self):
        # suppress=True would eat the user's typing to hide a badge.
        self.assertIn("suppress=False",
                      inspect.getsource(FloatingPopup._register_key_dismiss))

    def test_key_up_never_dismisses(self):
        # The hotkey's own key-up lands right after the badge appears.
        src = inspect.getsource(FloatingPopup._register_key_dismiss)
        self.assertIn('!= "down"', src)

    def test_a_grace_window_guards_the_dismiss(self):
        src = inspect.getsource(FloatingPopup._register_key_dismiss)
        self.assertIn("KEY_DISMISS_GRACE_SECS", src)
        self.assertLessEqual(popup_mod.KEY_DISMISS_GRACE_SECS, 2.0,
                             "a long grace makes the badge feel unresponsive")
        self.assertGreaterEqual(popup_mod.KEY_DISMISS_GRACE_SECS, 0.5,
                                "too short and an in-flight keystroke eats it")

    def test_the_grace_is_measured_from_the_badge_this_hook_belongs_to(self):
        # Captured at registration, not read live: a newer badge must not
        # extend an older hook's grace, and vice versa.
        self.assertIn("armed_at = self._icon_entered",
                      inspect.getsource(FloatingPopup._register_key_dismiss))

    # ── switching apps dismisses ─────────────────────────────────────────

    def test_switching_apps_hides_the_badge(self):
        p = self._popup()
        p._mode = "icon"
        p.root = object()
        hidden = []
        p._do_hide = lambda: hidden.append(True)
        p._foreground_is_target = lambda: False
        FloatingPopup._watch_target_foreground(p)
        self.assertEqual([True], hidden)

    def test_staying_in_the_target_app_keeps_the_badge(self):
        p = self._popup()
        p._mode = "icon"
        rearmed = []

        class _Root:
            def after(self, _ms, _fn):
                rearmed.append(True)
                return "tick"
        p.root = _Root()
        p._do_hide = lambda: self.fail("must not hide while still in the app")
        p._foreground_is_target = lambda: True
        FloatingPopup._watch_target_foreground(p)
        self.assertEqual([True], rearmed)

    def test_the_watch_stops_once_the_badge_is_gone(self):
        p = self._popup()
        p._mode = None
        p.root = object()
        p._do_hide = lambda: self.fail("nothing to hide")
        p._foreground_is_target = lambda: False
        FloatingPopup._watch_target_foreground(p)   # must simply return

    def test_foreground_check_fails_open(self):
        # A Win32 hiccup must never yank the badge away mid-read.
        p = FloatingPopup.__new__(FloatingPopup)
        p._target_hwnd = 0
        self.assertTrue(FloatingPopup._foreground_is_target(p),
                        "no captured target -> cannot judge -> keep showing")

    def test_the_popups_own_window_counts_as_ours(self):
        # winfo_id() is a CHILD window on Windows; both handles must be in the
        # 'ours' set or clicking the badge would dismiss it.
        src = inspect.getsource(FloatingPopup._foreground_is_target)
        self.assertIn("self._popup_hwnd", src)
        self.assertIn("self._top_hwnd()", src)

    def test_hiding_stops_the_watch(self):
        self.assertIn("_stop_foreground_watch",
                      inspect.getsource(FloatingPopup._do_hide))

    def test_a_failed_injection_badge_does_not_follow_the_user_away(self):
        # "⚠ Not inserted" is the one badge worth reading after switching apps.
        src = inspect.getsource(FloatingPopup._enter_icon_mode)
        start = src.index("_inserted_ok")
        self.assertIn("_stop_foreground_watch", src[start:])


if __name__ == "__main__":
    unittest.main()
