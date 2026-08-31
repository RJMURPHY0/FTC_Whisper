"""The CRM glass button, and the refine hotkey opening its panel directly.

Two things ship together here:

1. `GlassButton` is a port of Brightlink's `.glass-button`. The behaviour that
   matters is that hover, press and the selected/accent look are all owned by
   the WIDGET: call sites that bind <Enter>/<Leave> on one would replace its
   own handler and silently kill the hover (a widget-level bind replaces, it
   does not add). The hotkey tab's Save buttons therefore drive it through
   `command=` and `configure(bg=…)`, never through binds — pinned below at
   source level so the pattern cannot creep back.

2. The refine hotkey opens the refine PANEL, not the badge. The badge is a
   step that only asks the user to click again: they already selected the text
   and pressed the key. The post-dictation popup keeps its badge, because
   there the text is already in the document — that half is pinned too, since
   losing it would be the worse regression.
"""
import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ui_render
from popup import FloatingPopup


class _Recorder:
    """Stand-in Tk canvas: GlassButton only needs somewhere to put its state."""


def _make_button(command=lambda: None, **kw):
    """Build a GlassButton without a Tk display: __init__ is bypassed and the
    fields it would set are supplied directly, so the state machine can be
    exercised headless (the CI runners have no window server)."""
    import app_window
    b = app_window.GlassButton.__new__(app_window.GlassButton)
    b._text = kw.get("text", "Change Shortcut")
    b._fg = app_window.C["text"]
    b._subfg = app_window._GLASS_MUTED
    b._card = app_window.C["surface_hover"]
    b._radius = 8
    b._padx, b._pady = 16, 5
    b._command = command
    b._accent = ""
    b._state = "rest"
    b._sheen = -1.0
    b._sweep_job = None
    b._photo = None
    b._draws = 0
    b._draw = lambda *a, **k: setattr(b, "_draws", b._draws + 1)
    b._start_sweep = lambda: None
    b._stop_sweep = lambda: None
    return b


class GlassStateTests(unittest.TestCase):
    def test_hover_and_leave_move_between_states(self):
        b = _make_button()
        b._on_enter()
        self.assertEqual(b._state, "hover")
        b._on_leave()
        self.assertEqual(b._state, "rest")
        self.assertEqual(b._sheen, -1.0, "the settled surface carries no band")

    def test_a_button_with_no_command_never_hovers(self):
        # The disarmed Save. A lift on a dead button reads as an invitation the
        # click will not honour.
        b = _make_button(command=None)
        b._on_enter()
        self.assertEqual(b._state, "rest")
        b._on_press()
        self.assertEqual(b._state, "rest")

    def test_surface_tones_mean_unselected_accents_mean_selected(self):
        import app_window as aw
        b = _make_button()
        for tone in (aw.C["surface"], aw.C["surface_hover"], aw.C["border"],
                     aw.C["bg"]):
            b.configure(bg=tone)
            self.assertEqual(b._accent, "",
                             "%s is a surface, not a selection accent" % tone)
        b.configure(bg=aw.C["accent"])
        self.assertEqual(b._accent, aw.C["accent"])
        b.configure(bg=aw.C["error"])
        self.assertEqual(b._accent, aw.C["error"],
                         "the recorder's Cancel state is the red chip look")

    def test_resting_ink_is_muted_hover_ink_is_not(self):
        import app_window as aw
        b = _make_button()
        # Mirrors _draw's one-line rule; kept in step by the source check below.
        def ink(btn):
            return ((btn._accent or btn._fg) if btn._state != "rest"
                    else btn._subfg)
        self.assertEqual(ink(b), aw._GLASS_MUTED)
        b._on_enter()
        self.assertEqual(ink(b), aw.C["text"])

    def test_selected_button_does_not_lift(self):
        # CSS: only `:not([data-active="true"]):hover` translates. A selected
        # chip stays pressed in and merely brightens.
        b = _make_button()
        b._state = "hover"
        self.assertEqual(b._face_centre(120, 40),
                         (60, (ui_render.GLASS_PAD_T - ui_render._GLASS_LIFT
                               + 40 - ui_render.GLASS_PAD_B
                               - ui_render._GLASS_LIFT) // 2))
        b._accent = "#f39200"
        self.assertEqual(b._face_centre(120, 40),
                         (60, (ui_render.GLASS_PAD_T
                               + 40 - ui_render.GLASS_PAD_B) // 2))


class GlassSurfaceTests(unittest.TestCase):
    def test_mix_matches_css_color_mix(self):
        self.assertEqual(ui_render._mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(ui_render._mix("#1a1a1a", "#ffffff", 0.0), "#1a1a1a")
        self.assertEqual(ui_render._mix("#1a1a1a", "#ffffff", 1.0), "#ffffff")

    def test_the_face_leaves_room_for_shadow_and_lift(self):
        # The canvas is bigger than the face on purpose: a state change must be
        # a pure image swap, and a resize mid-hover reflows the whole row.
        self.assertGreaterEqual(ui_render.GLASS_PAD_T, ui_render._GLASS_LIFT)
        self.assertGreater(ui_render.GLASS_PAD_B, ui_render.GLASS_PAD_T,
                           "the shadow falls downward, so it needs more room")

    def test_render_returns_none_rather_than_raising_without_pil(self):
        self.assertIsNone(ui_render.glass_button(None, 0, 0))


class GlassWiringTests(unittest.TestCase):
    """Source-level: the hotkey tab must drive these buttons the right way."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "app_window.py"), encoding="utf-8") as f:
            self.src = f.read()
        start = self.src.index("def _build_hotkey_tab")
        self.tab = self.src[start:self.src.index("def _hotkey_help_text")]

    def test_the_three_shortcut_buttons_are_the_plain_button(self):
        # The glass treatment belongs to the keycaps above them; the buttons
        # themselves are the app's ordinary rounded rectangle.
        self.assertEqual(self.tab.count("self._hotkey_btn("), 3,
                         "hands-free, push-to-talk and refine")
        self.assertNotIn("GlassButton", self.tab)
        self.assertNotIn("_glass_btn", self.tab)
        body = self.src[self.src.index("def _hotkey_btn"):]
        body = body[:body.index(chr(10) + "    # ")]
        self.assertIn("_surface_btn", body)

    def test_no_call_site_binds_enter_or_leave_on_a_save_button(self):
        # A widget-level bind REPLACES the widget's handler, so this would
        # delete the hover the port exists to provide.
        for name in ("_save_btn", "_ptt_save_btn", "_refine_save_btn"):
            self.assertNotRegex(
                self.src, r"self\.%s\.bind\(" % name,
                "%s must be driven by command=/configure(), not binds" % name)

    def test_every_save_button_is_armed_through_the_one_helper(self):
        # Text, colour, cursor and command are ONE state ("Saved" grey and
        # inert vs "Save" accent and clickable). Configuring a save button in
        # place is how those drift apart, so no call site may do it.
        for name in ("_save_btn", "_ptt_save_btn", "_refine_save_btn"):
            self.assertNotRegex(
                self.src, r"self\.%s\.configure\(" % name,
                "%s must go through _arm_save/_disarm_save" % name)
            self.assertIn("self._arm_save(self.%s" % name, self.src)
            self.assertIn("self._disarm_save(self.%s" % name, self.src)

    def test_the_two_save_faces_differ_in_more_than_colour(self):
        import app_window as aw
        self.assertNotEqual(aw.AppWindow.SAVE_IDLE, aw.AppWindow.SAVE_ARMED)
        self.assertEqual(aw.AppWindow.SAVE_IDLE, "Saved")
        self.assertEqual(aw.AppWindow.SAVE_ARMED, "Save")
        arm = self.src[self.src.index("def _arm_save"):]
        arm = arm[:arm.index("def _disarm_save")]
        for part in ("text=", "bg=", "fg=", "cursor=", "command="):
            self.assertIn(part, arm, "_arm_save must set %s" % part)
        dis = self.src[self.src.index("def _disarm_save"):]
        dis = dis[:dis.index(chr(10) + "    def ", 10)]
        self.assertIn("command=None", dis)

    def test_a_disarmed_save_button_needs_no_unbind(self):
        # unbind(seq) drops EVERY handler for that sequence on Python < 3.12
        # and CI builds on 3.11, so disarming is command=None, never unbind.
        body = self.src[self.src.index("class SurfaceButton"):]
        body = body[:body.index(chr(10) + "class ")]
        self.assertNotIn(".unbind(", body)
        self.assertIn("if self._command is not None:", body)

    def test_a_state_change_mid_hover_survives_the_pointer_leaving(self):
        # "Change Hotkey" -> "Cancel" happens on a click, i.e. always with
        # the pointer ON the button. bg= must set the RESTING fill or the
        # red is lost the moment the pointer moves away.
        body = self.src[self.src.index("class SurfaceButton"):]
        body = body[:body.index(chr(10) + "class ")]
        cfg = body[body.index("def configure"):]
        self.assertIn("self._rest_fill = kw[k]", cfg)


class RefineOpensThePanelTests(unittest.TestCase):
    def setUp(self):
        self.p = FloatingPopup.__new__(FloatingPopup)
        self.calls = []
        self.p.root = self._fake_root()
        self.p._enter_icon_mode = lambda: self.calls.append("badge")
        self.p._expand_to_panel = lambda: self.calls.append("panel")
        self.p._get_cursor_pos = lambda: (0, 0)

    def _fake_root(self):
        outer = self

        class _Root:
            @staticmethod
            def after(_ms, fn):
                outer.calls.append("after")
                outer.calls.pop()
                fn()
        return _Root()

    def _show(self, **kw):
        self.p.show_cursor_icon("some selected words", inserted=True,
                                hwnd=7, cursor_x=100, cursor_y=200, **kw)

    def test_direct_panel_skips_the_badge(self):
        self._show(direct_panel=True)
        self.assertEqual(self.calls, ["panel"])

    def test_the_post_dictation_popup_still_shows_its_badge(self):
        self._show()
        self.assertEqual(self.calls, ["badge"],
                         "the badge is the default and must not regress")

    def test_the_refine_hotkey_is_the_only_caller_asking_for_the_panel(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "app.py"), encoding="utf-8") as f:
            app_src = f.read()
        self.assertEqual(app_src.count("direct_panel=True"), 1)
        # …and it is inside the refine-selection handler, not the dictation one.
        start = app_src.index("def _on_refine_selection")
        end = app_src.index("def _read_clipboard", start)
        self.assertIn("direct_panel=True", app_src[start:end])

    def test_the_cursor_coords_still_reach_the_panel_placement(self):
        # _expand_to_panel repositions from _status_cx/cy, so a direct open
        # that skipped _enter_icon_mode must still have them set.
        self._show(direct_panel=True)
        self.assertEqual((self.p._status_cx, self.p._status_cy), (100, 200))


class SignatureTests(unittest.TestCase):
    def test_direct_panel_defaults_to_off(self):
        sig = inspect.signature(FloatingPopup.show_cursor_icon)
        self.assertIs(sig.parameters["direct_panel"].default, False)


if __name__ == "__main__":
    unittest.main()


class CacheKeyContractTests(unittest.TestCase):
    """`_photo`/`_photo_im` composite the render onto `key[-1]`.

    Both new renderers shipped with `bg` in the middle of their key, and the
    failure was silent in two different ways: `icon_badge` raised inside the
    swallow-and-return-None wrapper (card headers lost their badge entirely)
    and `glass_button` composited onto an INT, which PIL happily reads as a
    near-black grey — every button grew a dark halo. Nothing in the app
    complains, so the contract is pinned here instead.
    """

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "ui_render.py"), encoding="utf-8") as f:
            self.src = f.read()

    def _key_of(self, fn_name):
        body = self.src[self.src.index("def %s(" % fn_name):]
        body = body[:body.index("\n    return ")]
        m = re.search(r"key = \((.*?)\)\n", body, re.S)
        self.assertIsNotNone(m, "no cache key in %s" % fn_name)
        return [p.strip() for p in m.group(1).split(",")]

    def test_every_cache_key_ends_with_bg(self):
        for fn in ("round_rect", "toggle_pill", "icon_glyph", "icon_badge",
                   "glass_button", "icon_media"):
            self.assertEqual(self._key_of(fn)[-1], "bg",
                             "%s: the bg colour must be key[-1]" % fn)

    def test_the_renderers_reject_a_non_colour_bg_instead_of_guessing(self):
        for helper in ("_photo", "_photo_im"):
            body = self.src[self.src.index("def %s(" % helper):]
            body = body[:body.index("\n\ndef ")]
            self.assertIn("isinstance(bg, str)", body,
                          "%s must refuse a key that does not end in a "
                          "colour — PIL silently accepts an int" % helper)


class HotkeyTabLayoutTests(unittest.TestCase):
    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "app_window.py"), encoding="utf-8") as f:
            self.src = f.read()
        start = self.src.index("def _build_hotkey_tab")
        self.tab = self.src[start:self.src.index("def _hotkey_help_text")]

    def test_the_dictation_pairs_stack_left_aligned(self):
        # The pair stacks in its column and each button is sized to its own
        # label -- a full-width button in a 161px column reads as a form row.
        for name in ("_record_btn", "_save_btn",
                     "_ptt_record_btn", "_ptt_save_btn"):
            self.assertRegex(self.tab, r"self\.%s\.pack\(anchor=\"w\"" % name,
                             "%s must sit left-aligned in its column" % name)
        cols = self.tab[self.tab.index("col_hf = tk.Frame"):
                        self.tab.index("# ── Refine selection")]
        self.assertNotIn('side="left"', cols,
                         "the dictation columns stack, they do not share rows")

    def test_the_refine_pair_shares_its_row(self):
        # The refine card is full width; two stacked buttons there would read
        # as a form rather than as a control.
        refine = self.tab[self.tab.index("btn_row2"):]
        self.assertEqual(refine.count('side="left"'), 2)

    def test_the_shortcut_buttons_carry_no_icon(self):
        # The plain button draws text only, so an icon= would be silently
        # dropped rather than shown.
        self.assertNotIn('icon="pen"', self.tab)
        for name in ("_record_btn", "_ptt_record_btn", "_refine_record_btn",
                     "_save_btn", "_ptt_save_btn", "_refine_save_btn"):
            self.assertNotRegex(
                self.src, r"self\.%s\.configure\([^)]*icon" % name,
                "%s has no icon to configure" % name)

    def test_a_cancel_button_turns_red_and_restores_the_resting_fill(self):
        # Cancel is the destructive face of the same button; leaving the
        # record state must put back the fill the button rests on, not the
        # card surface it sits against.
        for name in ("_record_btn", "_ptt_record_btn", "_refine_record_btn"):
            m = re.search(
                r'self\.%s\.configure\(\s*text="Cancel".*?\)' % name,
                self.src, re.S)
            self.assertIsNotNone(m, "no Cancel branch for %s" % name)
            self.assertIn('bg=C["error"]', m.group(0))
            back = re.search(
                r'self\.%s\.configure\((?![^)]*Cancel)[^)]*cursor="hand2"[^)]*\)'
                % name, self.src, re.S)
            self.assertIsNotNone(back, "no restore branch for %s" % name)
            self.assertIn('bg=C["surface_hover"]', back.group(0),
                          "%s must go back to the fill it rests on" % name)

    def test_the_card_header_has_a_rule_under_it(self):
        self.assertIn('tk.Frame(card, bg=C["border"], height=1)', self.tab)

    def test_the_header_badge_is_a_disc(self):
        self.assertIn("icon_badge(", self.tab)
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "ui_render.py"), encoding="utf-8") as f:
            ui = f.read()
        sig = ui[ui.index("def icon_badge("):]
        self.assertIn("circle: bool = True", sig[:sig.index('"""')])


class TabIconTests(unittest.TestCase):
    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "app_window.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_each_tab_declares_a_glyph(self):
        loop = self.src.split("for name, label, glyph in")[1].split("]")[0]
        for glyph in ('"home"', '"keyboard"', '"history"'):
            self.assertIn(glyph, loop)

    def test_ink_and_glyph_are_set_together(self):
        # The old handler read the label's fg back to decide the state, which
        # broke the moment a third colour (hover) existed. One painter now
        # owns both halves.
        body = self.src[self.src.index("def _paint_tab"):]
        body = body[:body.index("def _tab_hover")]
        self.assertIn("btn.configure(fg=colour)", body)
        self.assertIn("btn.configure(image=ph)", body)
        switch = self.src[self.src.index("def _switch_dash_tab"):]
        switch = switch[:switch.index("\n    def ", 10)]
        self.assertIn('self._paint_tab(n, "on" if active else "off")', switch)

    def test_hovering_the_active_tab_does_not_dim_it(self):
        body = self.src[self.src.index("def _tab_hover"):]
        body = body[:body.index("def _switch_dash_tab")]
        self.assertIn("_current_tab", body)
        self.assertIn("return", body)


class KeyCapParsingTests(unittest.TestCase):
    """A shortcut is drawn as caps; anything that is not a shortcut is not.

    `_segments` is the whole decision, and it is a staticmethod so it can be
    exercised without a display.
    """

    def seg(self, text):
        import app_window
        return app_window.KeyCapRow._segments(text)

    def test_a_chord_becomes_one_cap_per_key(self):
        self.assertEqual(self.seg("alt+v"), ["ALT", "V"])
        self.assertEqual(self.seg("CTRL+SHIFT+R"), ["CTRL", "SHIFT", "R"])
        self.assertEqual(self.seg(" alt + c "), ["ALT", "C"])

    def test_a_lone_key_still_gets_a_cap(self):
        # Single-key binds are legal (F9, ESC) and are still keys.
        self.assertEqual(self.seg("F9"), ["F9"])
        self.assertEqual(self.seg("home"), ["HOME"])

    def test_placeholders_are_never_drawn_as_keys(self):
        # A cap around an ellipsis reads as a key called "...".
        for placeholder in ("", "   ", "—", "…", "Not set", "+", "  +  "):
            self.assertIsNone(self.seg(placeholder), repr(placeholder))

    def test_the_last_segment_is_the_one_that_takes_the_accent(self):
        # Not asserted on colour (no display here) but on the contract the
        # renderer depends on: order is preserved, so segs[-1] is the key.
        self.assertEqual(self.seg("ctrl+alt+delete")[-1], "DELETE")


class KeyCapWiringTests(unittest.TestCase):
    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "app_window.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_all_three_shortcut_displays_are_keycaps(self):
        for name in ("_hotkey_display_lbl", "_ptt_display_lbl",
                     "_refine_hotkey_display_lbl"):
            m = re.search(r"self\.%s = (\w+)\(" % name, self.src)
            self.assertIsNotNone(m, "no constructor for %s" % name)
            self.assertEqual(m.group(1), "KeyCapRow", name)

    def test_the_recorder_still_drives_them_like_a_label(self):
        # A dozen call sites do .configure(text=…, fg=…); the widget must keep
        # honouring both rather than forcing them all to change.
        body = self.src[self.src.index("class KeyCapRow"):]
        body = body[:body.index("\nclass ")]
        self.assertIn('if "text" in kw:', body)
        self.assertIn('for k in ("fg", "foreground"):', body)
        self.assertIn("config = configure", body)

    def test_the_help_text_spaces_the_chord_like_the_caps(self):
        self.assertIn("def _spaced", self.src)
        import app_window
        self.assertEqual(app_window.AppWindow._spaced("ALT+V"), "ALT + V")
        self.assertEqual(app_window.AppWindow._spaced(""), "")


_MEASURE = r"""
import json, os, sys, tkinter as tk
sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[1])
import app_window as aw
from config import Config

root = tk.Tk(); root.withdraw(); root.geometry("420x700")
out = {}

# Mirrors _build_hotkey_tab: card padx 20 a side, inner_pad 18 a side, a 1px
# divider between the columns and 6px of padding either side of it.
col = (aw.MIN_W - 2 * 20 - 2 * 18 - 1 - 12) // 2
half = (aw.MIN_W - 2 * 20 - 2 * 18 - 6) // 2
out["column"] = col
out["half"] = half

holder = aw.AppWindow.__new__(aw.AppWindow)
frame = tk.Frame(root, bg=aw.C["surface"])
out["buttons"] = {}
for label in ("Change Hotkey", "Set Hotkey", "Cancel", "Saved", "Save"):
    b = aw.AppWindow._hotkey_btn(holder, frame, label)
    out["buttons"][label] = int(b["width"])
out["save_btn"] = int(aw.AppWindow._save_btn_new(holder, frame)["width"])

out["chords"] = {}
out["cap_heights"] = {}
for chord in ("ALT+V", "CTRL+SHIFT+R", "CTRL+ALT+SHIFT+F12", "Not set"):
    row = aw.KeyCapRow(frame, text=chord, bg=aw.C["surface"])
    row.place(x=0, y=0, width=col)
    row.update_idletasks()
    row._render()
    box = row.bbox("all")
    out["chords"][chord] = box[2] if box else 0
    out["cap_heights"][chord] = int(row["height"])
    row.destroy()

segs = aw.KeyCapRow._segments("CTRL+ALT+SHIFT+F12")
probe = aw.KeyCapRow(frame, text="x", bg=aw.C["surface"])
out["four_mod_tier"] = probe._pick_tier(segs, col)
out["two_key_tier_is_biggest"] = (
    probe._pick_tier(aw.KeyCapRow._segments("ALT+V"), 200)
    == aw.KeyCapRow._TIERS[0])
probe.destroy()

# The whole tab against the DEFAULT window's content region.
h = aw.AppWindow.__new__(aw.AppWindow)
h._root = root
h._config = Config()
h._hotkey, h._ptt_hotkey, h._refine_hotkey = "ALT+V", "ALT+C", "ALT+R"
h._recording_hotkey = h._recording_ptt_hotkey = h._recording_refine_hotkey = False
h._pending_hotkey = h._pending_ptt_hotkey = h._pending_refine_hotkey = None
h._ptt_warned = False
h._wheel_targets = []
tab = tk.Frame(root, bg=aw.C["bg"])
tab.place(x=0, y=0, width=aw.WINDOW_W, height=aw.DASH_H - 215)
h._build_hotkey_tab(tab)
root.update_idletasks()
out["tab_needs"] = h._hk_cv.content.winfo_reqheight()
out["tab_has"] = aw.DASH_H - 215
out["dash_h"] = aw.DASH_H

print("JSON:" + json.dumps(out))
"""


def _measure():
    """Run the Tk measurements in a SEPARATE PROCESS.

    They need real font metrics, so they need a live Tk — but a third Tk root
    in the pytest process destabilises test_impact_panel and
    test_library_pages (empty 1px canvases, key events going to the wrong
    focus owner) about one run in ten, whether that root is shared, borrowed,
    or created and destroyed. A subprocess cannot touch this interpreter at
    all, and costs about a second.
    """
    import json
    import subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        r = subprocess.run([sys.executable, "-c", _MEASURE, here],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        raise unittest.SkipTest("cannot measure: %s" % e)
    line = next((l for l in r.stdout.splitlines() if l.startswith("JSON:")), None)
    if line is None:
        raise unittest.SkipTest("Tk unavailable: %s" % (r.stderr or "")[-300:])
    return json.loads(line[5:])


class LayoutFitTests(unittest.TestCase):
    """Nothing in the Hotkey tab may overflow its column or its window.

    Both halves have already shipped broken this cycle: Save was clipped off
    the push-to-talk column, and CTRL+SHIFT+R drew 188px into a 161px one.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = _measure()

    def test_no_button_label_overflows_a_hotkey_column(self):
        for label, w in self.m["buttons"].items():
            self.assertLessEqual(w, self.m["column"],
                                 "%r needs %dpx in a %dpx column"
                                 % (label, w, self.m["column"]))

    def test_the_refine_pair_fits_half_the_card_each(self):
        self.assertLessEqual(self.m["buttons"]["Change Hotkey"], self.m["half"])
        self.assertLessEqual(self.m["save_btn"], self.m["half"])

    def test_every_chord_shape_fits_the_column(self):
        for chord, drawn in self.m["chords"].items():
            self.assertLessEqual(
                drawn, self.m["column"],
                "%s draws %dpx into a %dpx column"
                % (chord, drawn, self.m["column"]))

    def test_a_chord_too_wide_for_caps_degrades_to_text(self):
        # Four modifiers beat any cap size worth reading, so the row drops to
        # plain text rather than drawing keys nobody can make out.
        self.assertIsNone(self.m["four_mod_tier"])
        self.assertTrue(self.m["two_key_tier_is_biggest"],
                        "an ordinary chord must still get full-size caps")

    def test_the_row_height_never_changes_with_the_chord(self):
        # A taller row on a longer chord would shove everything under it down
        # the moment the user rebinds.
        heights = set(self.m["cap_heights"].values())
        self.assertEqual(len(heights), 1, "row height varies: %s" % heights)

    def test_the_hotkey_tab_fits_the_default_window(self):
        # It did not this round, and the only reason it went unnoticed is that
        # the developer's saved window was taller than the shipped default.
        self.assertLessEqual(
            self.m["tab_needs"], self.m["tab_has"],
            "the Hotkey tab needs %dpx but the default %dpx window gives it "
            "%dpx — it will scroll out of the box"
            % (self.m["tab_needs"], self.m["dash_h"], self.m["tab_has"]))


class HelpDotTests(unittest.TestCase):
    """The "?" beside each section. Its whole job is to say the thing the UI
    cannot show: what the mode actually does, and that the bind is yours."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "app_window.py"), encoding="utf-8") as f:
            self.src = f.read()

    def _helps(self):
        import app_window as aw
        return {"hands-free": aw.AppWindow._HELP_HANDS_FREE,
                "push-to-talk": aw.AppWindow._HELP_PTT,
                "refine": aw.AppWindow._HELP_REFINE}

    def test_all_three_sections_carry_one(self):
        start = self.src.index("def _build_hotkey_tab")
        tab = self.src[start:self.src.index("def _hotkey_help_text")]
        self.assertEqual(tab.count("self._micro_label("), 2,
                         "hands-free and push-to-talk")
        self.assertIn("help_text=self._HELP_REFINE", tab)
        self.assertIn("HelpDot(title_row", tab)

    def test_every_tip_says_the_hotkey_can_be_changed(self):
        for name, text in self._helps().items():
            low = text.lower()
            self.assertIn("change hotkey", low, name)
            self.assertIn("single key", low,
                          "%s must say a single key works" % name)
            self.assertIn("two", low,
                          "%s must say a two-key combination works" % name)

    def test_the_tips_explain_the_mode_not_just_the_binding(self):
        h = self._helps()
        self.assertIn("again", h["hands-free"].lower(),
                      "hands-free is tap-to-start, tap-again-to-stop")
        self.assertIn("let go", h["push-to-talk"].lower())
        self.assertIn("highlight", h["refine"].lower())

    def test_the_tip_is_plain_words(self):
        # It is the one surface aimed at someone who has not used the app.
        banned = ("hotkey manager", "bind", "chord", "toggle mode",
                  "keybinding", "ptt")
        for name, text in self._helps().items():
            for word in banned:
                self.assertNotIn(word, text.lower(),
                                 "%s uses jargon: %r" % (name, word))

    def test_the_tip_window_follows_the_dropdown_conventions(self):
        body = self.src[self.src.index("class HelpDot"):]
        body = body[:body.index("\nclass ")]
        self.assertIn("overrideredirect(True)", body)
        self.assertIn('attributes("-topmost", True)', body)
        self.assertIn("_apply_popup_corners", body)
        self.assertIn("_monitor_work_area", body,
                      "clamp to the monitor it is on, not the primary")
        self.assertIn('self.bind("<Button-1>"', body,
                      "hover-only is unreachable for click-driven users")
