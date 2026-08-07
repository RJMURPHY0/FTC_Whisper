"""Silent-stream liveness guard, and the pre-press capture window.

2026-08-07: the Windows Audio service hung under a live warm stream, was killed
by Windows, and restarted 60s later. PortAudio kept calling back the whole time,
just with zero-filled buffers — so `stream.active` stayed True, the heartbeat
stayed fresh, and `_stream_looks_alive()` (which knows only about those two)
called the corpse healthy. The watchdog therefore never fired, and every press
for the rest of the session captured nothing and buzzed.

These tests pin the second, content-based liveness signal that fixes it, and the
invariant that keeps it off the hotkey press path: a merely-silent mic must never
fail `_stream_looks_alive()`, because that runs while the user is already
speaking and a reopen there costs them their first words.

The second class pins how much pre-hotkey audio can ever be captured. No real
audio device is opened anywhere in this file.
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
    rec._refresh_portaudio = lambda: None          # never touch real PortAudio
    rec.opened = 0

    def _open():
        rec._active_device_name = name
        rec._active_sample_rate = rate
        rec._last_callback_ts = time.monotonic()
        rec.opened += 1
        return _FakeStream()

    rec._open_best_input_stream = _open
    return rec


def _feed(rec, value=0.0, chunks=1, samples=512):
    """Push chunks through the real audio callback. value=0.0 is the bit-exact
    digital silence a stream delivers once it has lost its device."""
    for _ in range(chunks):
        rec._audio_callback(
            np.full((samples, 1), value, dtype=np.float32), samples, None, None)


class SilentStreamTests(unittest.TestCase):
    def test_exact_silence_starts_a_run_and_signal_clears_it(self):
        rec = _warm_recorder()
        _feed(rec, 0.0, chunks=3)
        self.assertGreater(rec._silent_run_started_ts, 0.0)
        # Any real sample ends the run outright.
        _feed(rec, 0.01)
        self.assertEqual(rec._silent_run_started_ts, 0.0)
        self.assertEqual(rec._silent_run_seconds(), 0.0)

    def test_noise_floor_is_not_silence(self):
        # A live mic in a silent room still carries a floor. Only bit-exact
        # zeros mean the stream stopped carrying a device — this is the whole
        # discriminator, so it gets its own test.
        rec = _warm_recorder()
        _feed(rec, 1e-7, chunks=5)
        self.assertEqual(rec._silent_run_started_ts, 0.0)
        self.assertFalse(rec._stream_is_silently_dead())

    def test_empty_buffer_does_not_count_as_silence(self):
        rec = _warm_recorder()
        rec._audio_callback(np.zeros((0, 1), dtype=np.float32), 0, None, None)
        self.assertEqual(rec._silent_run_started_ts, 0.0)

    def test_dead_only_after_the_threshold(self):
        rec = _warm_recorder()
        _feed(rec, 0.0)
        self.assertFalse(rec._stream_is_silently_dead())
        rec._silent_run_started_ts = (
            time.monotonic() - recorder._SILENT_STREAM_SECONDS - 1.0)
        self.assertTrue(rec._stream_is_silently_dead())

    def test_press_path_liveness_ignores_silence(self):
        # THE guard on this change: _stream_looks_alive() is consulted by
        # start() → _ensure_warm_stream while the user is already speaking.
        # A muted mic reads as exact zeros too, and failing liveness there
        # would cold-open the stream mid-sentence and eat the first words.
        rec = _warm_recorder()
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        _feed(rec, 0.0, chunks=4)
        rec._silent_run_started_ts = (
            time.monotonic() - recorder._SILENT_STREAM_SECONDS - 30.0)
        self.assertTrue(rec._stream_is_silently_dead())
        self.assertTrue(rec._stream_looks_alive())

    def test_recovery_reopens_and_reports(self):
        rec = _warm_recorder()
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        opened_before = rec.opened
        rec._silent_run_started_ts = (
            time.monotonic() - recorder._SILENT_STREAM_SECONDS - 5.0)
        rec._recover_silent_stream()
        self.assertEqual(rec.opened, opened_before + 1)
        self.assertIsNotNone(rec.last_silence_recovery)
        self.assertEqual(rec.last_silence_recovery["device"], "Mic A")
        self.assertGreaterEqual(rec.last_silence_recovery["silent_seconds"],
                                recorder._SILENT_STREAM_SECONDS)
        # Run is cleared so the fresh stream is judged on its own output.
        self.assertEqual(rec._silent_run_started_ts, 0.0)

    def test_recovery_deferred_around_a_dictation(self):
        # Same rule as the periodic default-follow: the PortAudio re-init gap
        # must never coincide with a press.
        rec = _warm_recorder()
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        opened_before = rec.opened
        rec._last_record_activity_ts = time.monotonic()   # just dictated
        rec._silent_run_started_ts = (
            time.monotonic() - recorder._SILENT_STREAM_SECONDS - 5.0)
        rec._recover_silent_stream()
        self.assertEqual(rec.opened, opened_before)
        self.assertIsNone(rec.last_silence_recovery)

    def test_backoff_prevents_a_reopen_loop(self):
        # A hardware-muted mic delivers exact zeros forever. It must settle into
        # an occasional retry, not a reopen every _SILENT_STREAM_SECONDS.
        rec = _warm_recorder()
        self.assertTrue(rec._ensure_warm_stream(force_fresh=True))
        rec._silent_run_started_ts = (
            time.monotonic() - recorder._SILENT_STREAM_SECONDS - 5.0)
        rec._recover_silent_stream()
        self.assertEqual(rec._silence_recovery_count, 1)
        # Still silent straight afterwards → not eligible again yet.
        rec._silent_run_started_ts = (
            time.monotonic() - recorder._SILENT_STREAM_SECONDS - 5.0)
        self.assertFalse(rec._stream_is_silently_dead())
        # ...until the backoff expires.
        rec._silence_retry_after = time.monotonic() - 0.01
        self.assertTrue(rec._stream_is_silently_dead())

    def test_real_audio_resets_the_backoff(self):
        rec = _warm_recorder()
        rec._silence_recovery_count = 3
        rec._silence_retry_after = time.monotonic() + 600.0
        _feed(rec, 0.02)
        self.assertEqual(rec._silence_recovery_count, 0)
        self.assertEqual(rec._silence_retry_after, 0.0)


class PrePressCaptureWindowTests(unittest.TestCase):
    """How much audio from BEFORE the hotkey press can ever reach a transcript.

    Ryan's explicit constraint (2026-08-07): the app must not transcribe what he
    said before pressing. Two bounds govern it — the idle ring length and the
    slice of it seeded into a recording. Both are pinned here so no future
    change widens the window silently. The seed is deliberately tuned: 0.35s and
    0.6s both clipped first words under load (see the comment on the constant),
    so this pins the current value rather than lowering it.
    """

    def test_seed_and_ring_widths_are_pinned(self):
        self.assertEqual(recorder._PREROLL_SEED_SECONDS, 0.8)
        self.assertEqual(recorder._PREROLL_KEEP_SECONDS, 1.5)
        self.assertLessEqual(recorder._PREROLL_SEED_SECONDS,
                             recorder._PREROLL_KEEP_SECONDS)

    def test_idle_ring_never_grows_past_its_keep_window(self):
        rate, chunk = 16000, 1600
        rec = _warm_recorder(rate=rate)
        rec._active_sample_rate = rate
        # Ten seconds of idle room audio through the warm stream.
        _feed(rec, 0.01, chunks=100, samples=chunk)
        max_keep = int(rate * recorder._PREROLL_KEEP_SECONDS)
        # Trim leaves at most one chunk of overshoot.
        self.assertLessEqual(rec._preroll_samples, max_keep + chunk)


if __name__ == "__main__":
    unittest.main()
