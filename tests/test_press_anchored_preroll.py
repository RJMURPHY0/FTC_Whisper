"""Recording starts at the press — never before it.

The warm mic keeps a 1.5s pre-roll ring so a press never waits for a stream to
open. Seeding a recording with a FIXED slice of that ring (0.8s) meant every
dictation opened with whatever the room contained just before the hotkey: the
tail of a sentence said to someone else, a half-abandoned thought, a colleague
talking. Users reported it as "it was recording before I even pressed Alt+V".

The seed is now anchored to the physical press instant, stamped on the hotkey
thread before dispatch, so:
  * audio captured after the press is kept (dispatch lag costs nothing),
  * audio captured before it is dropped, sample-accurately,
  * the only slack is _PRESS_LEAD_SECONDS, which covers hook/callback clock
    skew and is far too short to hold a word.
"""
import inspect
import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder
from recorder import Recorder

RATE = 16000
CHUNK = 320          # 20ms — a typical WASAPI blocksize


def _ring_recorder(now: float, seconds: float = 1.5) -> Recorder:
    """A recorder whose pre-roll holds `seconds` of 20ms chunks ending at
    `now`. Every sample equals its own absolute capture time in seconds, so a
    seeded array says exactly which moments survived."""
    rec = Recorder()
    rec._warm_enabled = True
    rec._active_sample_rate = RATE
    n = int(seconds * RATE / CHUNK)
    for i in range(n):
        end = now - (n - 1 - i) * (CHUNK / RATE)
        start = end - CHUNK / RATE
        stamps = start + np.arange(CHUNK, dtype=np.float64) / RATE
        rec._preroll.append((end, stamps.astype(np.float64)))
        rec._preroll_samples += CHUNK
    return rec


def _seeded_times(rec, press_ts):
    picked = rec._seed_chunks_since(press_ts)
    if not picked:
        return np.array([], dtype=np.float64)
    return np.concatenate(picked)


class PressAnchoredSeedTests(unittest.TestCase):
    def test_nothing_from_before_the_press_is_seeded(self):
        now = 10_000.0
        rec = _ring_recorder(now)
        press = now - 0.30          # pressed 300ms ago; start() runs now
        times = _seeded_times(rec, press)
        self.assertTrue(times.size, "dispatch-lag audio must still be seeded")
        oldest = float(times.min())
        # Nothing older than the press bar the documented tolerance.
        self.assertGreaterEqual(
            oldest, press - recorder._PRESS_LEAD_SECONDS - CHUNK / RATE)
        self.assertLess(press - oldest, 0.1,
                        "pre-press audio leaked into the recording")

    def test_everything_since_the_press_is_kept(self):
        now = 10_000.0
        rec = _ring_recorder(now)
        press = now - 0.30
        times = _seeded_times(rec, press)
        # ~300ms of dispatch lag, all of it recovered (plus the lead).
        self.assertGreater(times.size, 0.28 * RATE)
        self.assertAlmostEqual(float(times.max()), now, delta=CHUNK / RATE)
        # Contiguous: no gap where a chunk was skipped.
        gaps = np.diff(times)
        self.assertLess(float(gaps.max()), 2.0 / RATE)

    def test_straddling_chunk_is_sliced_not_taken_whole(self):
        now = 10_000.0
        # One coarse 400ms chunk covering the press — taking it whole would
        # smuggle in ~200ms of pre-press room.
        rec = Recorder()
        rec._warm_enabled = True
        rec._active_sample_rate = RATE
        n = int(0.4 * RATE)
        stamps = (now - 0.4) + np.arange(n, dtype=np.float64) / RATE
        rec._preroll.append((now, stamps))
        rec._preroll_samples = n
        press = now - 0.2
        times = _seeded_times(rec, press)
        self.assertTrue(times.size)
        self.assertGreaterEqual(
            float(times.min()), press - recorder._PRESS_LEAD_SECONDS - 1e-6)
        self.assertLess(times.size, n)

    def test_no_press_stamp_seeds_essentially_nothing(self):
        """A programmatic start() (no hotkey) anchors on now, so the ring
        contributes at most the tolerance — never the old 0.8s window."""
        now = 10_000.0
        rec = _ring_recorder(now)
        times = _seeded_times(rec, now)
        self.assertLessEqual(
            times.size, int(recorder._PRESS_LEAD_SECONDS * RATE))

    def test_jittered_stamps_cannot_widen_the_window(self):
        """PortAudio delivers blocks in bursts, so consecutive stamps can sit
        closer together than the audio they carry. Per-chunk arithmetic alone
        then hands back more than elapsed — the wall-clock cap stops it."""
        now = 10_000.0
        rec = Recorder()
        rec._warm_enabled = True
        rec._active_sample_rate = RATE
        # Ten 20ms blocks, but stamped as if they all arrived within 20ms.
        for i in range(10):
            rec._preroll.append(
                (now - 0.02 + i * 0.002,
                 np.zeros(CHUNK, dtype=np.float64)))
            rec._preroll_samples += CHUNK
        press = now - 0.05
        picked = rec._seed_chunks_since(press)
        total = sum(c.shape[0] for c in picked)
        self.assertLessEqual(
            total, int((0.05 + recorder._PRESS_LEAD_SECONDS) * RATE))

    def test_seed_is_capped_when_dispatch_stalls(self):
        """A press that reaches start() seconds late must not drag the whole
        ring in — the cap bounds it."""
        now = 10_000.0
        rec = _ring_recorder(now)
        times = _seeded_times(rec, now - 5.0)
        self.assertLessEqual(
            times.size,
            int(recorder._PREROLL_SEED_SECONDS * RATE) + CHUNK)

    def test_lead_tolerance_cannot_hold_a_word(self):
        # A syllable is ~150ms. The tolerance exists for clock skew only.
        self.assertLessEqual(recorder._PRESS_LEAD_SECONDS, 0.08)

    def test_ring_trim_handles_stamped_entries(self):
        """The idle ring trims by sample count on (ts, chunk) pairs — an
        unpacking slip here would raise inside the audio callback."""
        rec = Recorder()
        rec._warm_enabled = True
        rec._active_sample_rate = RATE
        for _ in range(int(3.0 * RATE / CHUNK)):
            rec._audio_callback(
                np.zeros((CHUNK, 1), dtype=np.float32), CHUNK, None, None)
        self.assertLessEqual(
            rec._preroll_samples,
            int(RATE * recorder._PREROLL_KEEP_SECONDS) + CHUNK)
        self.assertTrue(all(isinstance(e, tuple) and len(e) == 2
                            for e in rec._preroll))


class PressStampWiringTests(unittest.TestCase):
    """The stamp is only useful if it survives the trip from the key to the
    recorder. Each hop is source-checked: a signature drifting back to a
    no-argument call would silently restore the old fixed-window behaviour."""

    def test_recorder_start_accepts_a_press_stamp(self):
        sig = inspect.signature(Recorder.start)
        self.assertIn("press_ts", sig.parameters)

    def test_hotkey_manager_stamps_the_press_and_passes_it(self):
        import hotkey_manager
        src = inspect.getsource(hotkey_manager.HotkeyManager._on_key_down)
        self.assertIn("press_mono = time.monotonic()", src)
        self.assertEqual(src.count("args=(press_mono,)"), 2,
                         "both hold and toggle starts must carry the stamp")

    def test_app_forwards_the_stamp_to_the_recorder(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("def _on_start_recording(self, press_ts", src)
        self.assertIn("self.recorder.start(press_ts)", src)
        self.assertNotIn("self.recorder.start()", src)


if __name__ == "__main__":
    unittest.main()
