"""Instant-start / first-words-not-dropped guard.

The warm mic keeps a pre-roll ring so start() is instant and the first syllable
survives stream-open latency. The ~2-minute periodic "default-follow" bounce used
to tear the stream down AND clear that ring unconditionally — a keypress landing
in the ~seed-length window right after found an empty ring and dropped the first
word or two.

The fix: a periodic follow that lands back on the SAME device keeps the ring
(contiguous, trustworthy audio); a real default-device change (or a dead-stream
recovery) still clears it (stale / wrong-device audio). These tests pin that
contract without opening any real audio stream.
"""
import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder
from recorder import Recorder


class _FakeStream:
    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass

    @property
    def active(self):
        return True


def _warm_recorder(name="Mic A", rate=16000):
    rec = Recorder()
    rec._warm_enabled = True
    # Never re-init real PortAudio in a unit test.
    rec._refresh_portaudio = lambda: None

    def _open(dev_name=name, r=rate):
        rec._active_device_name = dev_name
        rec._active_sample_rate = r
        rec._last_callback_ts = time.monotonic()
        return _FakeStream()

    rec._open_best_input_stream = _open
    return rec


def _seed_ring(rec, samples=1600):
    rec._preroll.append(np.zeros(samples, dtype=np.float32))
    rec._preroll_samples = samples


class PrerollPreserveTests(unittest.TestCase):
    def test_periodic_follow_same_device_keeps_preroll(self):
        rec = _warm_recorder("Mic A")
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        _seed_ring(rec, 1600)
        # Periodic default-follow re-opens the SAME device → ring survives, so a
        # keypress immediately afterwards still has a seed.
        self.assertTrue(
            rec._ensure_warm_stream(force_fresh=True, preserve_preroll=True))
        self.assertEqual(rec._preroll_samples, 1600)
        self.assertEqual(len(rec._preroll), 1)

    def test_default_device_change_clears_preroll(self):
        rec = _warm_recorder("Mic A")
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        _seed_ring(rec, 1600)

        def _open_b():
            rec._active_device_name = "Mic B"
            rec._active_sample_rate = 16000
            rec._last_callback_ts = time.monotonic()
            return _FakeStream()

        rec._open_best_input_stream = _open_b
        # A real default change → the old ring is wrong-device audio, must go.
        self.assertTrue(
            rec._ensure_warm_stream(force_fresh=True, preserve_preroll=True))
        self.assertEqual(rec._preroll_samples, 0)
        self.assertEqual(len(rec._preroll), 0)

    def test_sample_rate_change_clears_preroll(self):
        rec = _warm_recorder("Mic A", rate=16000)
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        _seed_ring(rec, 1600)

        def _open_slow():
            rec._active_device_name = "Mic A"
            rec._active_sample_rate = 48000  # same name, different rate
            rec._last_callback_ts = time.monotonic()
            return _FakeStream()

        rec._open_best_input_stream = _open_slow
        # Same name but a different rate means the old samples no longer line up.
        self.assertTrue(
            rec._ensure_warm_stream(force_fresh=True, preserve_preroll=True))
        self.assertEqual(rec._preroll_samples, 0)

    def test_recovery_reopen_clears_preroll(self):
        # The dead-stream recovery path does NOT pass preserve_preroll, so the
        # (untrustworthy) ring is always cleared there.
        rec = _warm_recorder("Mic A")
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        _seed_ring(rec, 1600)
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        self.assertEqual(rec._preroll_samples, 0)


class TuningTests(unittest.TestCase):
    def test_seed_covers_dispatch_latency(self):
        # The seed must stay comfortably above the old 0.6 that still clipped.
        self.assertGreaterEqual(recorder._PREROLL_SEED_SECONDS, 0.75)
        # And never exceed what the idle ring actually keeps.
        self.assertLessEqual(
            recorder._PREROLL_SEED_SECONDS, recorder._PREROLL_KEEP_SECONDS)

    def test_periodic_follow_is_deferred_around_dictation(self):
        self.assertGreater(recorder._DEVICE_REFRESH_QUIET_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
