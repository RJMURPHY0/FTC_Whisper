import unittest
from unittest.mock import patch

import hotkey_manager as hm


class SimultaneousChordTests(unittest.TestCase):
    def test_base_first_chord_inside_grace_window_is_ready(self):
        with patch.object(hm, "_vk_is_down", return_value=True):
            self.assertTrue(hm._simultaneous_chord_ready(
                hm._vk_code("v"), ["alt"], 10.0, now=10.08,
                assume_modifier_down="alt",
            ))

    def test_base_first_chord_outside_grace_window_is_rejected(self):
        with patch.object(hm, "_vk_is_down", return_value=True):
            self.assertFalse(hm._simultaneous_chord_ready(
                hm._vk_code("v"), ["alt"], 10.0, now=10.13,
                assume_modifier_down="alt",
            ))

    def test_chord_requires_base_key_to_still_be_down(self):
        with patch.object(hm, "_vk_is_down", return_value=False):
            self.assertFalse(hm._simultaneous_chord_ready(
                hm._vk_code("c"), ["alt"], 10.0, now=10.05,
                assume_modifier_down="alt",
            ))

    def test_key_repeat_does_not_refresh_simultaneous_timestamp(self):
        manager = hm.HotkeyManager(hotkey="alt+v", mode="hold")
        with (
            patch.object(hm.time, "monotonic", side_effect=[10.0, 20.0]),
            patch.object(hm, "_modifiers_are_down", return_value=False),
        ):
            manager._observe_combo_base_press(source="main")
            manager._observe_combo_base_press(source="main")
        self.assertEqual(10.0, manager._combo_base_pressed_at["main"])
        manager._observe_combo_base_release(source="main")
        self.assertEqual(0.0, manager._combo_base_pressed_at["main"])

    def test_main_and_ptt_use_same_simultaneous_tolerance(self):
        manager = hm.HotkeyManager(
            hotkey="alt+v", ptt_hotkey="alt+c", mode="hold")
        manager._combo_base_pressed_at["main"] = 20.0
        manager._combo_base_pressed_at["ptt"] = 20.0
        activated = []
        manager._activate_win32_combo = activated.append
        with (
            patch.object(hm.time, "monotonic", return_value=20.05),
            patch.object(hm, "_vk_is_down", return_value=True),
        ):
            manager._observe_combo_modifier_press(
                source="main", modifier="alt")
            manager._observe_combo_modifier_press(
                source="ptt", modifier="alt")
        self.assertEqual(["main", "ptt"], activated)

    def test_refine_uses_same_simultaneous_tolerance(self):
        manager = hm.TriggerHotkeyManager(hotkey="alt+r")
        manager._combo_base_pressed_at = 30.0
        fired = []
        manager._activate_combo = lambda: fired.append(True)
        with (
            patch.object(hm.time, "monotonic", return_value=30.05),
            patch.object(hm, "_vk_is_down", return_value=True),
        ):
            manager._observe_combo_modifier_press(modifier="alt")
        self.assertEqual([True], fired)

    def test_trigger_activation_is_latched_against_duplicate_sources(self):
        manager = hm.TriggerHotkeyManager(
            hotkey="alt+r", on_trigger=lambda: None)
        manager._registered = True
        manager._fire = lambda: None
        with patch.object(hm.threading, "Thread") as thread:
            manager._activate_combo()
            manager._activate_combo()
        self.assertEqual(1, thread.call_count)

    def test_hold_chord_stops_when_modifier_is_released_first(self):
        manager = hm.HotkeyManager(hotkey="alt+v", mode="hold")
        manager._pollers["main"] = True
        manager._combo_active["main"] = True
        released = []
        manager._on_key_up = lambda source="main": released.append(source)
        with (
            patch.object(hm.time, "sleep"),
            patch.object(hm, "_vk_is_down", return_value=True),
            patch.object(hm, "_modifiers_are_down", return_value=False),
        ):
            manager._poll_release(hm._vk_code("v"), "main")
        self.assertEqual(["main"], released)
        self.assertFalse(manager._combo_active["main"])

    def test_toggle_chord_release_only_rearms_next_press(self):
        manager = hm.HotkeyManager(hotkey="alt+v", mode="toggle")
        manager._pollers["main"] = True
        manager._combo_active["main"] = True
        released = []
        manager._on_key_up = lambda source="main": released.append(source)
        with (
            patch.object(hm.time, "sleep"),
            patch.object(hm, "_vk_is_down", return_value=False),
        ):
            manager._poll_release(hm._vk_code("v"), "main")
        self.assertEqual([], released)
        self.assertFalse(manager._combo_active["main"])


if __name__ == "__main__":
    unittest.main()
