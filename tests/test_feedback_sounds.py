"""The start/stop cues must stay the Glaido-matched two-tap figure.

This sound has been changed four times and got it wrong three times (too high,
too bright, then the Windows "ding", which is the sound of an error in every
other app). The shape is now measured, not guessed: Glaido embeds its cues as
24-bit/48kHz WAVs inside glaido-core.exe, and these are the numbers that came
back. Pin them.
"""
import io
import os
import struct
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedback


def _samples(name: str):
    """(float samples, sample rate) for one cue."""
    w = wave.open(io.BytesIO(feedback._get_sound(name)))
    raw = w.readframes(w.getnframes())
    n = len(raw) // 2
    return [s / 32768.0 for s in struct.unpack(f"<{n}h", raw)], w.getframerate()


def _freq(samples, sr, start_ms, end_ms) -> float:
    """Fundamental of a window, by zero crossings. Good enough for a pure sine
    and it keeps this test free of numpy."""
    a, b = int(sr * start_ms / 1000), int(sr * end_ms / 1000)
    seg = samples[a:b]
    crossings = sum(1 for i in range(1, len(seg))
                    if (seg[i - 1] < 0) != (seg[i] < 0))
    return crossings / 2.0 / ((b - a) / sr)


def _peak(samples, sr, start_ms, end_ms) -> float:
    a, b = int(sr * start_ms / 1000), int(sr * end_ms / 1000)
    return max(abs(s) for s in samples[a:b])


class GlaidoFigureTests(unittest.TestCase):
    def setUp(self):
        feedback._SOUND_CACHE.clear()

    def test_start_is_a_rising_fifth(self):
        x, sr = _samples("start")
        first = _freq(x, sr, 2, 26)
        second = _freq(x, sr, 36, 60)
        self.assertAlmostEqual(296.6, first, delta=12)
        self.assertAlmostEqual(442.4, second, delta=15)
        self.assertGreater(second, first, "start must RISE — it means 'listening'")

    def test_stop_is_the_falling_mirror(self):
        x, sr = _samples("stop")
        first = _freq(x, sr, 2, 26)
        second = _freq(x, sr, 52, 76)
        self.assertAlmostEqual(440.9, first, delta=15)
        self.assertAlmostEqual(287.8, second, delta=12)
        self.assertLess(second, first, "stop must FALL — it means 'got it'")

    def test_both_cues_are_two_taps_with_a_gap_between_them(self):
        # A gap that closes turns the figure into one long tone, which is the
        # buzzer this sound exists to not be.
        for name, gap in (("start", (26, 32)), ("stop", (30, 46))):
            x, sr = _samples(name)
            quiet = _peak(x, sr, *gap)
            loud = max(abs(s) for s in x)
            self.assertLess(quiet, loud * 0.12,
                            f"{name}: taps have run together")

    def test_the_second_tap_is_the_louder_one(self):
        for name, one, two in (("start", (0, 30), (33, 70)),
                               ("stop", (0, 30), (48, 90))):
            x, sr = _samples(name)
            self.assertGreater(_peak(x, sr, *two), _peak(x, sr, *one), name)

    def test_nothing_clips(self):
        for name in ("start", "stop", "done", "error"):
            x, _sr = _samples(name)
            self.assertLess(max(abs(s) for s in x), 0.95, name)

    def test_the_cues_are_short_enough_to_stay_out_of_the_way(self):
        # Longer than ~200ms and the stop cue overlaps the text landing.
        for name, ms in (("start", 79.0), ("stop", 94.5), ("done", 34.0)):
            x, sr = _samples(name)
            self.assertAlmostEqual(ms, len(x) / sr * 1000, delta=2.0, msg=name)

    def test_start_and_stop_are_generated_not_the_windows_ding(self):
        # MessageBeep is the OS error chime; users silence it system-wide and
        # then wonder why the app went quiet.
        import inspect
        src = inspect.getsource(feedback._play_sound)
        code = "\n".join(l.split("#")[0] for l in src.splitlines())
        self.assertNotIn("MessageBeep", code)
        self.assertIn("SND_MEMORY", code)

    def test_every_cue_builds_without_a_sound_device(self):
        # _get_sound runs on daemon threads and must never raise; playback is
        # what can fail on a machine with no audio, and that is caught already.
        for name in ("start", "stop", "done", "error"):
            self.assertGreater(len(feedback._get_sound(name)), 44, name)


if __name__ == "__main__":
    unittest.main()
