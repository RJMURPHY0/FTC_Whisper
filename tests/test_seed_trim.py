"""Pre-hotkey speech must never be injected.

The warm mic listens continuously and start() seeds the recording from a
pre-roll ring, so the first syllable survives hotkey-dispatch latency. Measured
2026-08-29: that seed also carried whatever the user had said in the 0.8s BEFORE
the press. Real capture, real transcript — "Hello, my name is Ryan" spoken to
the room, then ALT+V, then "Whisperflow" — was injected as
"Hello, my name is Ryan Whisperflow."

_seed_cut_index() is the guard. These tests pin BOTH directions, because the
regression in either is worse than the bug: dropping a first word the user did
say is the failure v1.6.71 spent a release chasing.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder
from recorder import Recorder, _seed_cut_index

RATE = 16000


def _silence(seconds: float, level: float = 2e-4) -> np.ndarray:
    n = int(RATE * seconds)
    rng = np.random.default_rng(7)
    return (rng.standard_normal(n) * level).astype(np.float32)


def _speech(seconds: float, level: float = 0.05) -> np.ndarray:
    """Voiced-looking audio: a modulated tone well above the room floor."""
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    carrier = np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 7 * t))
    return (carrier * level).astype(np.float32)


def _kept(seed: np.ndarray, floor: float = 2e-4) -> float:
    """Seconds of seed surviving the trim. `floor` is the room level the warm
    stream has been tracking, which is what the threshold is built from."""
    return (seed.shape[0] - _seed_cut_index(seed, RATE, floor)) / RATE


class SeedTrimTests(unittest.TestCase):
    def test_all_silence_keeps_the_whole_window(self):
        """The ordinary press: nothing was said, so nothing can leak. Keeping
        the silence is what protects a quiet onset the level test would miss."""
        self.assertAlmostEqual(_kept(_silence(0.8)), 0.8, places=2)

    def test_speech_before_a_pause_is_dropped(self):
        """The reported bug: talk, pause, press. The talking is not dictation."""
        seed = np.concatenate([_speech(0.5), _silence(0.3)])
        self.assertLessEqual(_kept(seed), 0.32)

    def test_word_begun_on_the_press_survives(self):
        """Speech starting a beat before the key registers is the entire reason
        the seed exists. Silence, then a short onset running into the press."""
        seed = np.concatenate([_silence(0.55), _speech(0.25)])
        self.assertGreaterEqual(_kept(seed), 0.24)

    def test_a_long_run_into_the_press_is_dropped_not_shortened(self):
        seed = np.concatenate([_silence(0.1), _speech(0.7)])
        self.assertLessEqual(_kept(seed), 0.02)

    def test_gapless_speech_into_the_press_is_dropped_whole(self):
        """Pressed mid-flow with no pause anywhere: the user was already
        talking, so none of it was dictated."""
        self.assertLessEqual(_kept(_speech(0.8)), 0.02)

    def test_a_run_is_never_cut_part_way_through(self):
        """Keeping half a word is worse than keeping none: measured, it turned
        "Host all recent changes" into "Post all recent changes"."""
        for run in (0.20, 0.30, 0.40, 0.55, 0.70):
            seed = np.concatenate([_silence(0.8 - run), _speech(run)])
            kept = _kept(seed)
            self.assertTrue(kept <= 0.02 or kept >= run - 0.03,
                            "run %.2fs was cut mid-word (kept %.2fs)"
                            % (run, kept))

    def test_short_dip_inside_speech_is_not_a_boundary(self):
        """A ~60ms gap between two words of the SAME onset must not cut it —
        that is how a first word loses its first phoneme."""
        seed = np.concatenate([_silence(0.45), _speech(0.12),
                               _silence(0.06), _speech(0.17)])
        self.assertGreaterEqual(_kept(seed), 0.34)

    def test_quiet_far_field_speech_is_still_detected(self):
        """The floor is adaptive: the same speech sits ~20 dB lower on a
        far-field mic and a fixed threshold would call the whole window silent
        and leak all of it."""
        seed = np.concatenate([_speech(0.5, level=0.004),
                               _silence(0.3, level=6e-5)])
        self.assertLessEqual(_kept(seed, floor=6e-5), 0.32)

    def test_degenerate_windows_never_raise(self):
        for seed in (np.zeros(0, dtype=np.float32),
                     np.zeros(3, dtype=np.float32),
                     _silence(0.01)):
            self.assertGreaterEqual(_seed_cut_index(seed, RATE, 2e-4), 0)


class SeedTrimWiringTests(unittest.TestCase):
    """_trim_seed is the only caller; it must fail open, never raise into the
    press path, and must not touch a seed that holds no speech."""

    def setUp(self):
        self.rec = Recorder.__new__(Recorder)
        self.rec._active_sample_rate = RATE
        self.rec.sample_rate = RATE
        self.rec._noise_floor = 2e-4

    def test_silent_seed_passes_through_untouched(self):
        chunks = [_silence(0.4).reshape(-1, 1), _silence(0.4).reshape(-1, 1)]
        out = self.rec._trim_seed(chunks, int(RATE * 0.8))
        self.assertIs(out, chunks)

    def test_leading_speech_is_replaced_not_deleted(self):
        """The clip keeps its length — deleting the region moved the first real
        word and changed how its opening phoneme decoded."""
        chunks = [_speech(0.5).reshape(-1, 1), _silence(0.3).reshape(-1, 1)]
        out = self.rec._trim_seed(chunks, int(RATE * 0.8))
        flat = np.concatenate(out, axis=0)
        self.assertEqual(flat.shape[0], int(RATE * 0.8))
        head = flat[:int(RATE * 0.45)]
        self.assertLess(float(np.abs(head).max()), 0.005)

    def test_replaced_region_is_room_tone_not_digital_zero(self):
        chunks = [_speech(0.5).reshape(-1, 1), _silence(0.3).reshape(-1, 1)]
        flat = np.concatenate(self.rec._trim_seed(chunks, int(RATE * 0.8)), axis=0)
        self.assertTrue(bool(np.any(flat[:int(RATE * 0.4)] != 0.0)))

    def test_failure_falls_back_to_the_untrimmed_seed(self):
        """A trim that blows up must cost the user nothing worse than the old
        behaviour — never a lost dictation."""
        chunks = [_speech(0.5).reshape(-1, 1)]
        original = recorder._seed_cut_index
        recorder._seed_cut_index = lambda *a, **k: 1 / 0
        try:
            self.assertIs(self.rec._trim_seed(chunks, int(RATE * 0.8)), chunks)
        finally:
            recorder._seed_cut_index = original


if __name__ == "__main__":
    unittest.main()
