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

    def test_dead_stream_window_is_small(self):
        # A press landing on a dead warm stream loses first words outright, so
        # the watchdog must notice a dead stream fast. The check itself is two
        # timestamp reads — a short cadence costs nothing.
        self.assertLessEqual(recorder._WATCHDOG_INTERVAL, 2.5)

    def test_default_follow_stays_infrequent(self):
        # The default-follow bounce tears the stream down (~0.1-0.3s gap) — a
        # shorter watchdog tick must never make the bounce itself frequent.
        self.assertGreaterEqual(
            recorder._WATCHDOG_INTERVAL * recorder._DEVICE_REFRESH_TICKS, 60.0)


class _CallbackFeeder:
    """Feeds _audio_callback like a live stream. gate_on_recording=True holds
    fire until recording flips on, so a test can present a STALE heartbeat to
    start() and still satisfy its callback-verify loop afterwards."""

    def __init__(self, rec, gate_on_recording=False):
        self._rec = rec
        self._gate = gate_on_recording
        self._stop = False
        import threading
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop = True
        self._thread.join(timeout=1.0)

    def _run(self):
        block = np.zeros((1024, 1), dtype=np.float32)
        while not self._stop:
            if not self._gate or self._rec._recording:
                self._rec._audio_callback(block, 1024, None, None)
            time.sleep(0.01)


class PressRecoveryTests(unittest.TestCase):
    def test_instant_press_reports_no_recovery(self):
        rec = _warm_recorder("Mic A")
        self.assertTrue(rec._ensure_warm_stream())
        with _CallbackFeeder(rec):
            time.sleep(0.05)  # heartbeat fresh = live stream
            rec.start()
            self.assertIsNone(rec.last_start_recovery)
            rec.stop()

    def test_stale_press_skips_portaudio_refresh_and_reports(self):
        # Warm stream dead at press: the fast reopen must NOT pay the
        # PortAudio re-init (that is spoken audio lost), and the press must be
        # reported as a recovery so the fleet log can count it.
        rec = _warm_recorder("Mic A")
        self.assertTrue(rec._ensure_warm_stream())
        refreshed = []
        rec._refresh_portaudio = lambda: refreshed.append(True)
        rec._last_callback_ts = time.monotonic() - 5.0  # heartbeat stale
        with _CallbackFeeder(rec, gate_on_recording=True):
            rec.start()
            self.assertEqual(refreshed, [])
            self.assertIsNotNone(rec.last_start_recovery)
            self.assertEqual(rec.last_start_recovery["mode"], "warm_reopen")
            rec.stop()


if __name__ == "__main__":
    unittest.main()


class WarmCaptureLiveTests(unittest.TestCase):
    """`warm_capture_live` is read on the HOTKEY thread to decide whether the
    start cue can fire the instant the key goes down.

    It answers one question only: is the mic already recording the room, so
    that the pre-roll ring holds the moment of the press? If it lies in the
    optimistic direction the user is cued to speak into a stream that is not up
    yet — the exact way first words get lost — so every failure mode here must
    fall to False.
    """

    def _recorder(self):
        r = Recorder.__new__(Recorder)
        r._warm_enabled = True
        return r

    def test_live_warm_stream_reads_as_live(self):
        r = self._recorder()
        r._stream_looks_alive = lambda: True
        self.assertTrue(r.warm_capture_live)

    def test_a_dead_stream_reads_as_not_live(self):
        r = self._recorder()
        r._stream_looks_alive = lambda: False
        self.assertFalse(
            r.warm_capture_live,
            "a stream that has to be reopened has no pre-roll for the press")

    def test_warm_mic_disabled_reads_as_not_live(self):
        r = self._recorder()
        r._warm_enabled = False
        r._stream_looks_alive = lambda: True
        self.assertFalse(r.warm_capture_live)

    def test_it_fails_closed_on_any_error(self):
        r = self._recorder()

        def _boom():
            raise RuntimeError("portaudio hiccup")

        r._stream_looks_alive = _boom
        self.assertFalse(r.warm_capture_live,
                         "an unreadable stream must never be reported as live")

    def test_it_never_blocks_the_press_path(self):
        r = self._recorder()
        r._stream_looks_alive = lambda: True
        start = time.monotonic()
        for _ in range(1000):
            r.warm_capture_live
        self.assertLess(time.monotonic() - start, 0.25,
                        "this runs on the hotkey thread — it cannot be slow")


class StartCueTimingTests(unittest.TestCase):
    """The cue moved from after recorder.start() to the moment of the press.

    start() waits for the next audio callback before returning (measured
    30-95 ms on a warm stream), and the cue used to sit behind that wait, which
    is what made the press feel like it lagged. The SOUND is unchanged — only
    when it fires.
    """

    def _source(self):
        import inspect

        import app

        return (inspect.getsource(app.WhisperFlowApp._on_state_change),
                inspect.getsource(app.WhisperFlowApp._on_start_recording))

    def test_the_cue_fires_on_the_hotkey_thread_when_the_mic_is_already_live(self):
        state_src, _ = self._source()
        self.assertIn("warm_capture_live", state_src)
        self.assertIn("recording_started", state_src)

    def test_the_cold_path_still_waits_for_proven_audio(self):
        _, start_src = self._source()
        self.assertIn("_start_cue_played", start_src,
                      "a stream that had to be opened must still cue only "
                      "after start() proves audio is flowing")

    def test_the_cue_is_never_played_twice_for_one_press(self):
        state_src, start_src = self._source()
        self.assertIn("self._start_cue_played = False", state_src)
        self.assertIn("if not getattr(self, \"_start_cue_played\", False):",
                      start_src)
