"""Recording a new shortcut must capture the keys the user actually pressed.

Two measured defects made "Change Shortcut" record the wrong combo:

  1. The global binds stayed LIVE during capture. RegisterHotKey swallows its
     combo before any window sees it, so pressing a current shortcut delivered
     nothing to the recorder — and it FIRED the action instead. The refine
     trigger copies the selection with a synthetic Ctrl+C, and that Ctrl+C is
     what the recorder captured: pressing ALT+R recorded CTRL+C. Reproduced
     live against the running app (the capture window saw VK 0xFF from
     _mask_menu_tap, then the injected Ctrl+C).

  2. The combo was read off tkinter's keysym and event.state. Measured on
     Windows: Alt+; arrives as keysym "semicolon" (which _vk_code() cannot
     resolve, so the shortcut saves and never registers) and the same physical
     key reports "R" under Alt but "r" under Shift. The virtual-key code is the
     only stable identifier.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hotkey_manager as hm_mod
from app_window import AppWindow
from hotkey_manager import HotkeyManager, TriggerHotkeyManager


def _event(keycode, keysym="", state=0):
    return types.SimpleNamespace(keycode=keycode, keysym=keysym, state=state)


def _window(**attrs):
    """An AppWindow with only the fields the capture path touches."""
    win = AppWindow.__new__(AppWindow)
    win._hotkey = "ALT+V"
    win._ptt_hotkey = "ALT+C"
    win._refine_hotkey = "ALT+R"
    win._hotkeys_suspended = False
    win._on_hotkey_capture = None
    for k, v in attrs.items():
        setattr(win, k, v)
    return win


class ComboFromEventTests(unittest.TestCase):
    def setUp(self):
        self.win = _window()
        # Physical modifier state is a Win32 read; drive it explicitly here.
        self.held = []
        self.win._physical_modifiers = lambda: list(self.held)

    def combo(self, keycode, keysym="", state=0):
        return AppWindow._combo_from_event(self.win, _event(keycode, keysym, state))

    def test_alt_plus_letter_captures_the_letter_not_the_shifted_glyph(self):
        # Measured: Alt+R arrives as keysym 'R', Shift+R as 'r'. Same key.
        self.held = ["alt"]
        self.assertEqual("alt+r", self.combo(0x52, "R", 0x2000A))
        self.held = ["shift"]
        self.assertEqual("shift+r", self.combo(0x52, "r", 0x0000B))

    def test_punctuation_resolves_to_a_name_the_registrar_understands(self):
        self.held = ["alt"]
        self.assertEqual("alt+;", self.combo(0xBA, "semicolon", 0x2000A))

    def test_a_modifier_press_on_its_own_is_not_a_combo(self):
        for keysym in ("Alt_L", "Control_R", "Shift_L", "Super_L"):
            self.assertIsNone(self.combo(0x12, keysym, 0x8))

    def test_physical_state_fills_in_what_tk_leaves_out(self):
        # Windows folds Ctrl+Alt into AltGr and Tk reports only its Ctrl bit,
        # so state alone recorded CTRL+R for a Ctrl+Alt+R press.
        self.held = ["ctrl", "alt"]
        self.assertEqual("ctrl+alt+r", self.combo(0x52, "R", 0x0000E))

    def test_tk_state_fills_in_what_the_physical_read_leaves_out(self):
        # Union, not replacement — a modifier may be missed, never invented.
        self.held = []
        self.assertEqual("ctrl+shift+r", self.combo(0x52, "r", 0x0000F))

    def test_modifier_order_is_stable(self):
        self.held = ["shift", "alt", "ctrl"]
        self.assertEqual("ctrl+alt+shift+r", self.combo(0x52, "R", 0))

    def test_an_unmapped_key_falls_back_to_the_keysym(self):
        self.held = []
        self.assertEqual("f13", self.combo(0x7C, "F13", 0))


class RegistrableKeyTests(unittest.TestCase):
    """Every key the recorder can emit must be one the registrar can bind.

    A name _vk_code() returns 0 for means RegisterHotKey is never called: the
    card says "Shortcut updated" and the key does nothing for ever after.
    """

    def test_every_captured_base_key_resolves_to_a_virtual_key(self):
        for vk, name in AppWindow._VK_KEY_NAMES.items():
            if name == "caps lock":
                continue        # its own path in HotkeyManager (_suppress_caps)
            self.assertEqual(
                vk, hm_mod._vk_code(name),
                f"captured key {name!r} does not round-trip to VK {vk:#x}")


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.win = _window()

    def test_a_bare_character_key_is_refused(self):
        # It would fire every time the user typed that letter.
        self.assertIn("Ctrl", AppWindow._combo_conflict(self.win, "r", "refine"))
        self.assertIn("Ctrl", AppWindow._combo_conflict(self.win, "1", "main"))

    def test_a_bare_function_key_is_allowed(self):
        self.assertEqual("", AppWindow._combo_conflict(self.win, "f9", "refine"))
        self.assertEqual("", AppWindow._combo_conflict(self.win, "insert", "ptt"))

    def test_each_bind_refuses_the_other_two(self):
        self.assertIn("hands-free",
                      AppWindow._combo_conflict(self.win, "alt+v", "refine"))
        self.assertIn("push-to-talk",
                      AppWindow._combo_conflict(self.win, "alt+c", "refine"))
        self.assertIn("refine",
                      AppWindow._combo_conflict(self.win, "alt+r", "main"))
        self.assertIn("refine",
                      AppWindow._combo_conflict(self.win, "alt+r", "ptt"))

    def test_an_unset_push_to_talk_bind_blocks_nothing(self):
        self.win._ptt_hotkey = ""
        self.assertEqual("", AppWindow._combo_conflict(self.win, "alt+q", "refine"))

    def test_a_free_combo_passes(self):
        self.assertEqual("", AppWindow._combo_conflict(self.win, "ctrl+shift+9", "refine"))


class SuspensionTests(unittest.TestCase):
    """The binds must come down for the capture and back up afterwards."""

    def setUp(self):
        self.calls = []
        self.win = _window(_on_hotkey_capture=self.calls.append)

    def test_suspend_then_resume(self):
        AppWindow._suspend_global_hotkeys(self.win)
        self.assertEqual([True], self.calls)
        AppWindow._resume_global_hotkeys(self.win)
        self.assertEqual([True, False], self.calls)

    def test_suspending_twice_only_takes_them_down_once(self):
        AppWindow._suspend_global_hotkeys(self.win)
        AppWindow._suspend_global_hotkeys(self.win)
        self.assertEqual([True], self.calls)

    def test_a_resume_without_a_suspend_does_nothing(self):
        AppWindow._resume_global_hotkeys(self.win)
        self.assertEqual([], self.calls)

    def test_the_binds_come_back_even_if_the_suspend_callback_threw(self):
        def _boom(_active):
            raise RuntimeError("no")
        win = _window(_on_hotkey_capture=_boom)
        AppWindow._suspend_global_hotkeys(win)      # must not propagate
        AppWindow._resume_global_hotkeys(win)
        self.assertFalse(win._hotkeys_suspended)

    def test_cancel_resumes_even_with_no_recorder_running(self):
        win = _window(_on_hotkey_capture=self.calls.append,
                      _recording_hotkey=False, _recording_ptt_hotkey=False,
                      _recording_refine_hotkey=False)
        AppWindow._suspend_global_hotkeys(win)
        AppWindow._cancel_hotkey_capture(win)
        self.assertEqual([True, False], self.calls)
        self.assertFalse(win._hotkeys_suspended)


class ManagerSuspendTests(unittest.TestCase):
    """suspend() has to really unregister — a flag that merely ignores the
    callback still leaves RegisterHotKey eating the keystroke."""

    def _trigger(self):
        tm = TriggerHotkeyManager.__new__(TriggerHotkeyManager)
        tm.hotkey = "alt+r"
        tm._registered = True
        tm._suspended = False
        tm._win32_ok = True
        tm._is_combo = True
        tm.unregistered = 0
        tm.registered = 0

        def _unreg():
            tm.unregistered += 1
            tm._registered = False
        def _reg():
            tm.registered += 1
            tm._registered = True
            return True
        tm.unregister = _unreg
        tm.register = _reg
        return tm

    def test_suspend_unregisters_and_resume_registers(self):
        tm = self._trigger()
        TriggerHotkeyManager.suspend(tm)
        self.assertEqual(1, tm.unregistered)
        self.assertTrue(tm._suspended)
        TriggerHotkeyManager.resume(tm)
        self.assertEqual(1, tm.registered)
        self.assertFalse(tm._suspended)

    def test_suspend_is_idempotent(self):
        tm = self._trigger()
        TriggerHotkeyManager.suspend(tm)
        TriggerHotkeyManager.suspend(tm)
        self.assertEqual(1, tm.unregistered)

    def test_saving_while_suspended_registers_immediately(self):
        # The save arrives before the recorder's resume; without clearing the
        # flag the new shortcut would sit dead until the next capture.
        tm = self._trigger()
        TriggerHotkeyManager.suspend(tm)
        tm._parse_hotkey = lambda _hk: None
        tm.bind_status = lambda: {"ok": True, "os_level": True,
                                  "combo": tm.hotkey, "detail": ""}
        TriggerHotkeyManager.update_hotkey(tm, "alt+g")
        self.assertFalse(tm._suspended)
        self.assertEqual(1, tm.registered)

    def test_bind_status_flags_a_hook_only_registration(self):
        hm = HotkeyManager.__new__(HotkeyManager)
        hm.hotkey = "alt+v"
        hm._registered = True
        hm._win32_ok = False          # RegisterHotKey lost it to another app
        hm._is_combo = True
        hm._suppress_caps = False
        status = HotkeyManager.bind_status(hm, "main")
        self.assertTrue(status["ok"])
        self.assertFalse(status["os_level"])


class StatusMessageTests(unittest.TestCase):
    def setUp(self):
        self.win = _window()

    def test_a_clean_registration_says_nothing_extra(self):
        msg, _c = AppWindow._describe_bind_status(
            self.win, {"ok": True, "os_level": True, "combo": "alt+r"})
        self.assertEqual("", msg)

    def test_a_failed_registration_is_reported(self):
        msg, _c = AppWindow._describe_bind_status(
            self.win, {"ok": False, "os_level": False, "combo": "alt+r"})
        self.assertIn("ALT+R", msg)
        self.assertIn("another app", msg)

    def test_a_shared_combo_is_reported(self):
        msg, _c = AppWindow._describe_bind_status(
            self.win, {"ok": True, "os_level": False, "combo": "alt+r"})
        self.assertIn("shared", msg)


if __name__ == "__main__":
    unittest.main()
