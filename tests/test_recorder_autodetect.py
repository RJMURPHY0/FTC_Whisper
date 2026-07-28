import unittest
from unittest.mock import patch

from recorder import Recorder


def _rec(input_device="auto"):
    rec = object.__new__(Recorder)
    rec.input_device = input_device
    return rec


DEVICES = [
    {"index": 3, "name": "Stereo Mix (Realtek)", "channels": 2, "sample_rate": 48000},
    {"index": 5, "name": "Headset (Buds Pro Hands-Free AG Audio)", "channels": 1, "sample_rate": 16000},
    {"index": 7, "name": "Microphone Array (Intel Smart Sound)", "channels": 2, "sample_rate": 48000},
    {"index": 9, "name": "Yeti Stereo Microphone", "channels": 2, "sample_rate": 48000},
]


class AutoRankTests(unittest.TestCase):
    def test_wired_mics_beat_bluetooth_beat_virtual(self):
        rank = Recorder._auto_rank
        self.assertLess(rank("Microphone Array (Intel)"), rank("Headset (Hands-Free AG Audio)"))
        self.assertLess(rank("Headset (Hands-Free AG Audio)"), rank("Stereo Mix (Realtek)"))
        self.assertLess(rank("Yeti Stereo Microphone"), rank("CABLE Output (VB-Audio)"))

    def test_external_mics_beat_the_builtin_array(self):
        # Plugging in a dedicated mic must win over the laptop array even
        # while Windows keeps the array as its default input.
        rank = Recorder._auto_rank
        self.assertLess(rank("Yeti Stereo Microphone"),
                        rank("Microphone Array (Intel Smart Sound)"))
        self.assertLess(rank("USB Audio Device"),
                        rank("Microphone (Realtek High Definition)"))

    def test_unknown_names_sit_between_mics_and_bluetooth(self):
        rank = Recorder._auto_rank
        self.assertLess(rank("USB Audio Device"), rank("Mystery Device 3000"))
        self.assertLess(rank("Mystery Device 3000"), rank("Bluetooth Audio"))


class PickBestInputTests(unittest.TestCase):
    def test_picks_first_best_rank_when_no_windows_default(self):
        # The Yeti is the only dedicated external mic — it outranks the
        # built-in array (see test_external_mics_beat_the_builtin_array).
        rec = _rec()
        with patch.object(Recorder, "_get_default_input_index",
                          staticmethod(lambda: None)):
            self.assertEqual(9, rec._pick_best_input(DEVICES))

    def test_first_enumerated_wins_within_the_best_rank(self):
        rec = _rec()
        two_externals = DEVICES + [
            {"index": 11, "name": "Blue Snowball", "channels": 1,
             "sample_rate": 48000}]
        with patch.object(Recorder, "_get_default_input_index",
                          staticmethod(lambda: None)):
            self.assertEqual(9, rec._pick_best_input(two_externals))

    def test_windows_default_wins_within_the_best_rank(self):
        rec = _rec()
        with patch.object(Recorder, "_get_default_input_index",
                          staticmethod(lambda: 9)), \
             patch("recorder.sd.query_devices",
                   lambda idx=None, **kw: {"name": "Yeti Stereo Microphone"}):
            self.assertEqual(9, rec._pick_best_input(DEVICES))

    def test_windows_default_cannot_drag_in_a_worse_rank(self):
        # Windows default set to the Bluetooth hands-free mic: auto-detect must
        # still prefer a real wired mic (the Yeti is the best-ranked one).
        rec = _rec()
        with patch.object(Recorder, "_get_default_input_index",
                          staticmethod(lambda: 5)), \
             patch("recorder.sd.query_devices",
                   lambda idx=None, **kw: {"name": "Headset (Buds Pro Hands-Free AG Audio)"}):
            self.assertEqual(9, rec._pick_best_input(DEVICES))

    def test_empty_device_list_returns_none(self):
        self.assertIsNone(_rec()._pick_best_input([]))

    def test_best_input_name_matches_pick(self):
        rec = _rec()
        with patch.object(Recorder, "_get_default_input_index",
                          staticmethod(lambda: None)):
            self.assertEqual("Yeti Stereo Microphone",
                             rec.pick_best_input_name(DEVICES))


class ResolvePreferredTests(unittest.TestCase):
    def test_auto_and_empty_both_resolve_via_ranking(self):
        for token in ("auto", "", "  AUTO "):
            rec = _rec(token)
            with patch.object(Recorder, "_get_default_input_index",
                              staticmethod(lambda: None)):
                self.assertEqual(9, rec._resolve_preferred_input(DEVICES),
                                 f"token {token!r}")

    def test_explicit_name_still_pins_that_device(self):
        rec = _rec("Yeti")
        self.assertEqual(9, rec._resolve_preferred_input(DEVICES))

    def test_is_auto_device(self):
        self.assertTrue(Recorder.is_auto_device(""))
        self.assertTrue(Recorder.is_auto_device("auto"))
        self.assertTrue(Recorder.is_auto_device(" Auto "))
        self.assertFalse(Recorder.is_auto_device("Yeti"))


if __name__ == "__main__":
    unittest.main()
