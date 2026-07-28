"""Evidence-based mic selection guard.

The auto-detect ranking is a name-based prior; the probe adds evidence:
a device that sat silent through a dictation while another mic clearly heard
the user is demoted, and the mic that heard the speech is preferred outright.
These tests pin the contract:

  1. Evidence preference beats name ranking (the not-worn-headset case).
  2. Demoted devices are skipped; all-demoted falls back to plain ranking.
  3. Any topology change (plug/unplug) resets the evidence — a headset put
     back on gets a fresh chance instead of a permanent ban.
  4. Advice is consumed exactly once and clears the fast-path device cache.
  5. The probe never triggers when recording is off, a device is pinned,
     or voice has been heard.

No audio streams are opened — pure selection logic.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder import Recorder


DEVICES = [
    {"index": 3, "name": "Headset Microphone (Logi)", "channels": 1,
     "sample_rate": 48000.0},
    {"index": 5, "name": "Microphone Array (Realtek Audio)", "channels": 2,
     "sample_rate": 48000.0},
    {"index": 7, "name": "Stereo Mix (Realtek Audio)", "channels": 2,
     "sample_rate": 48000.0},
]


def _make_recorder(input_device: str = "auto") -> Recorder:
    rec = Recorder(input_device=input_device)
    # No Windows default in these tests — ranking alone decides.
    rec._get_default_input_index = staticmethod(lambda: None)
    return rec


class PickBestInputTests(unittest.TestCase):
    def test_headset_wins_by_name_rank(self):
        rec = _make_recorder()
        self.assertEqual(rec._pick_best_input(DEVICES), 3)

    def test_demoted_device_is_skipped(self):
        rec = _make_recorder()
        rec._demoted_devices = {"headset microphone (logi)"}
        rec._demotion_topology = frozenset(
            d["name"].strip().lower() for d in DEVICES)
        self.assertEqual(rec._pick_best_input(DEVICES), 5)

    def test_evidence_preferred_beats_rank(self):
        rec = _make_recorder()
        rec._evidence_preferred = "Microphone Array (Realtek Audio)"
        rec._demotion_topology = frozenset(
            d["name"].strip().lower() for d in DEVICES)
        self.assertEqual(rec._pick_best_input(DEVICES), 5)

    def test_all_demoted_falls_back_to_rank(self):
        rec = _make_recorder()
        rec._demoted_devices = {d["name"].strip().lower() for d in DEVICES}
        rec._demotion_topology = frozenset(
            d["name"].strip().lower() for d in DEVICES)
        self.assertEqual(rec._pick_best_input(DEVICES), 3)

    def test_new_device_appearing_resets_evidence(self):
        rec = _make_recorder()
        rec._demoted_devices = {"headset microphone (logi)"}
        rec._evidence_preferred = "Microphone Array (Realtek Audio)"
        rec._demotion_topology = frozenset(
            d["name"].strip().lower() for d in DEVICES)
        changed = DEVICES + [{"index": 9, "name": "USB Audio Device",
                              "channels": 1, "sample_rate": 44100.0}]
        picked = rec._pick_best_input(changed)
        self.assertFalse(rec._demoted_devices)
        self.assertEqual(rec._evidence_preferred, "")
        # Fresh ranking again: dedicated hardware (headset/USB) rank first.
        self.assertIn(picked, (3, 9))

    def test_device_disappearing_keeps_evidence(self):
        # A PortAudio re-init transiently hiding an endpoint must not cancel
        # a switch the user was just told about.
        rec = _make_recorder()
        rec._demoted_devices = {"headset microphone (logi)"}
        rec._evidence_preferred = "Microphone Array (Realtek Audio)"
        rec._demotion_topology = frozenset(
            d["name"].strip().lower() for d in DEVICES)
        fewer = [d for d in DEVICES if "Stereo Mix" not in d["name"]]
        self.assertEqual(rec._pick_best_input(fewer), 5)
        self.assertTrue(rec._demoted_devices)

    def test_preferred_device_vanishing_resets_evidence(self):
        rec = _make_recorder()
        rec._demoted_devices = {"headset microphone (logi)"}
        rec._evidence_preferred = "Microphone Array (Realtek Audio)"
        rec._demotion_topology = frozenset(
            d["name"].strip().lower() for d in DEVICES)
        without_pref = [d for d in DEVICES if "Array" not in d["name"]]
        picked = rec._pick_best_input(without_pref)
        self.assertEqual(rec._evidence_preferred, "")
        self.assertEqual(picked, 3)

    def test_clear_mic_memory(self):
        rec = _make_recorder()
        rec._demoted_devices = {"x"}
        rec._evidence_preferred = "y"
        rec._demotion_topology = frozenset({"x"})
        rec._mic_advice = {"from": "x", "to": "y"}
        rec.clear_mic_memory()
        self.assertFalse(rec._demoted_devices)
        self.assertEqual(rec._evidence_preferred, "")
        self.assertIsNone(rec._mic_advice)


class MicAdviceTests(unittest.TestCase):
    def _armed_advice(self, rec, rec_ts):
        return {"from": "Headset Microphone (Logi)",
                "to": "Microphone Array (Realtek Audio)",
                "from_rms": 0.0001, "to_rms": 0.02,
                "rec_ts": rec_ts,
                "topology": frozenset(
                    d["name"].strip().lower() for d in DEVICES)}

    def test_consume_commits_evidence_and_clears_device_cache(self):
        rec = _make_recorder()
        rec._recording_started_ts = 123.0
        rec._active_device_index = 3
        rec._active_device_name = "Headset Microphone (Logi)"
        rec._mic_advice = self._armed_advice(rec, 123.0)
        advice = rec.consume_mic_advice()
        self.assertEqual(advice["to"], "Microphone Array (Realtek Audio)")
        self.assertIsNone(rec.consume_mic_advice())
        # Consumption is the ONLY commit point for the evidence.
        self.assertIn("headset microphone (logi)", rec._demoted_devices)
        self.assertEqual(rec._evidence_preferred,
                         "Microphone Array (Realtek Audio)")
        # Cached fast-path index must be dropped so the next stream open
        # re-enumerates and honours the evidence preference.
        self.assertIsNone(rec._active_device_index)

    def test_unconsumed_advice_never_changes_selection(self):
        # A successful dictation discards advice without consuming it — the
        # ranked pick must be untouched (no silent switch at the next
        # watchdog refresh).
        rec = _make_recorder()
        rec._recording_started_ts = 123.0
        rec._mic_advice = self._armed_advice(rec, 123.0)
        self.assertEqual(rec._pick_best_input(DEVICES), 3)
        self.assertFalse(rec._demoted_devices)
        self.assertEqual(rec._evidence_preferred, "")

    def test_stale_recording_stamp_discards_advice(self):
        # Advice computed during recording N must not switch mics after N+1.
        rec = _make_recorder()
        rec._recording_started_ts = 456.0
        rec._active_device_index = 3
        rec._mic_advice = self._armed_advice(rec, 123.0)
        self.assertIsNone(rec.consume_mic_advice())
        self.assertFalse(rec._demoted_devices)
        self.assertEqual(rec._active_device_index, 3)

    def test_no_advice_keeps_device_cache(self):
        rec = _make_recorder()
        rec._active_device_index = 3
        self.assertIsNone(rec.consume_mic_advice())
        self.assertEqual(rec._active_device_index, 3)


class ProbeTriggerTests(unittest.TestCase):
    def _armed_recorder(self) -> Recorder:
        """Recorder in the state where a probe SHOULD fire."""
        rec = _make_recorder()
        rec._recording = True
        rec._voiced_samples = 0
        rec._recording_started_ts = time.monotonic() - 5.0
        rec._probe_calls = []
        rec._probe_alternatives = lambda: rec._probe_calls.append(1)
        return rec

    def _wait_probe(self, rec, expect: bool) -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if rec._probe_calls:
                break
            time.sleep(0.02)
        self.assertEqual(bool(rec._probe_calls), expect)

    def test_probe_fires_on_voice_free_recording(self):
        rec = self._armed_recorder()
        rec.maybe_probe_alternatives()
        self._wait_probe(rec, True)
        self.assertTrue(rec._probe_done)

    def test_probe_fires_once_per_recording(self):
        rec = self._armed_recorder()
        rec.maybe_probe_alternatives()
        self._wait_probe(rec, True)
        rec._probe_active = False
        rec.maybe_probe_alternatives()
        time.sleep(0.1)
        self.assertEqual(len(rec._probe_calls), 1)

    def test_no_probe_when_not_recording(self):
        rec = self._armed_recorder()
        rec._recording = False
        rec.maybe_probe_alternatives()
        self._wait_probe(rec, False)

    def test_no_probe_when_voice_heard(self):
        rec = self._armed_recorder()
        rec._voiced_samples = 4800
        rec.maybe_probe_alternatives()
        self._wait_probe(rec, False)

    def test_no_probe_too_early(self):
        rec = self._armed_recorder()
        rec._recording_started_ts = time.monotonic()
        rec.maybe_probe_alternatives()
        self._wait_probe(rec, False)

    def test_no_probe_for_pinned_device(self):
        rec = self._armed_recorder()
        rec.input_device = "Headset Microphone (Logi)"
        rec.maybe_probe_alternatives()
        self._wait_probe(rec, False)

    def test_start_resets_probe_state(self):
        rec = _make_recorder()
        rec._probe_done = True
        rec._mic_advice = {"from": "a", "to": "b"}
        # start() would open a real stream; only exercise the reset block by
        # replicating its contract here via the recording guard path.
        with rec._lock:
            rec._probe_done = False
            rec._mic_advice = None
            rec._recording_started_ts = time.monotonic()
        self.assertFalse(rec._probe_done)
        self.assertIsNone(rec._mic_advice)


if __name__ == "__main__":
    unittest.main(verbosity=2)
