"""The VAD gate must never trim the START or the END of what the user said.

The reported bug: "it's not getting the first word I say." Measured on Ryan's
own stored dictations, the Silero gate — not the capture — was the thing losing
them. Silero decides where speech BEGINS from its own confidence curve, and a
quiet first word falls under threshold 0.40, so the gate cut it out and the
model never saw it:

    "Can you give me a prompt…"  ->  "Give me a prompt…"
    "It's very very important…"  ->  "Very very important…"
    "I haven't had some scrapes…" -> "Haven't had some scrapes…"

With the gate disabled all three were complete, which is what identified the
gate as the culprit. `speech_pad_ms=200` does not help: it pads a region Silero
has already placed AFTER the missed word.

These tests pin both directions. The head/tail cases are the reported bug; the
noise cases are the worse regression, because the gate is the only thing
stopping a noise-only clip from being decoded into a confident sentence the
user never said.
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asr_engine
from asr_engine import ParakeetTranscriber

SR = asr_engine._MODEL_SAMPLE_RATE


def _clip(seconds: float) -> np.ndarray:
    """Distinctly-valued audio so a returned slice can be traced to its source
    sample — value at index i is i+1, so the first/last kept sample is readable.
    Deliberately never 0.0: the splice inserts runs of zeros as the join gap,
    and a source sample that collided with those would be unreadable."""
    return np.arange(1, int(SR * seconds) + 1, dtype=np.float32)


class _FakeVad:
    """Stand in for Silero so a region can be placed exactly where the failure
    needs it. The real model's onset estimate is the thing under test, so it
    must not also be the thing generating the test input."""

    def __init__(self, regions):
        self.regions = regions

    def install(self, test):
        import faster_whisper.vad as fwvad

        original = fwvad.get_speech_timestamps
        fwvad.get_speech_timestamps = lambda audio, opts=None: [
            {"start": int(a * SR), "end": int(b * SR)} for a, b in self.regions
        ]
        test.addCleanup(setattr, fwvad, "get_speech_timestamps", original)


class HeadPreservationTests(unittest.TestCase):
    """A quiet first word sits BEFORE where Silero thinks speech starts."""

    def setUp(self):
        self.engine = ParakeetTranscriber(vad_gate=True)

    def test_head_before_the_first_region_is_kept(self):
        # Silero puts speech at 1.9s; the user actually started at ~0.2s. That
        # 1.7s gap is the real, measured failure (a clip peaking at 0.030).
        _FakeVad([(1.9, 8.0)]).install(self)
        audio = _clip(10.0)
        out = self.engine._vad_clip(audio)
        self.assertIsNotNone(out)
        self.assertEqual(
            float(out[0]), float(audio[0]),
            "audio before Silero's first region was dropped — that is exactly "
            "where a quiet first word lives")

    def test_head_is_kept_up_to_the_edge_bound(self):
        # A long lead-in is bounded rather than kept whole, so a noisy room can
        # never feed the transducer an unbounded noise-only run.
        _FakeVad([(30.0, 40.0)]).install(self)
        audio = _clip(45.0)
        out = self.engine._vad_clip(audio)
        first_sample = float(out[0])
        expected = (30.0 - ParakeetTranscriber._VAD_EDGE_KEEP) * SR + 1
        self.assertAlmostEqual(first_sample, expected, delta=SR * 0.05)

    def test_edge_keep_covers_the_measured_worst_case(self):
        self.assertGreaterEqual(
            ParakeetTranscriber._VAD_EDGE_KEEP, 2.0,
            "1.0s was measured as too small: Silero placed its first region "
            "1.9s late on a real far-field dictation and the fix still cut "
            "'I haven't had' off the front")


class TailPreservationTests(unittest.TestCase):
    def setUp(self):
        self.engine = ParakeetTranscriber(vad_gate=True)

    def test_tail_after_the_last_region_is_kept(self):
        _FakeVad([(1.0, 5.0)]).install(self)
        audio = _clip(6.0)
        out = self.engine._vad_clip(audio)
        self.assertEqual(
            float(out[-1]), float(audio[-1]),
            "audio after Silero's last region was dropped — a trailing word "
            "fades exactly the same way a leading one does")


class InteriorSplicingStillWorksTests(unittest.TestCase):
    """The gate's real job is untouched: long non-speech BETWEEN utterances is
    still cut out, because that is what stops steady noise being decoded."""

    def setUp(self):
        self.engine = ParakeetTranscriber(vad_gate=True)

    def test_long_interior_gap_is_removed(self):
        _FakeVad([(0.0, 2.0), (30.0, 32.0)]).install(self)
        audio = _clip(32.0)
        out = self.engine._vad_clip(audio)
        self.assertLess(
            len(out), len(audio),
            "a 28s gap between two utterances must still be spliced out")

    def test_no_speech_anywhere_returns_none(self):
        _FakeVad([]).install(self)
        self.assertIsNone(self.engine._vad_clip(_clip(5.0)))

    def test_nearly_all_speech_is_returned_untouched(self):
        _FakeVad([(0.1, 4.9)]).install(self)
        audio = _clip(5.0)
        out = self.engine._vad_clip(audio)
        self.assertIs(out, audio, "no splice needed — natural timing preserved")

    def test_gate_failure_leaves_the_audio_alone(self):
        import faster_whisper.vad as fwvad

        original = fwvad.get_speech_timestamps

        def _boom(audio, opts=None):
            raise RuntimeError("silero unavailable")

        fwvad.get_speech_timestamps = _boom
        self.addCleanup(setattr, fwvad, "get_speech_timestamps", original)
        audio = _clip(3.0)
        self.assertIs(self.engine._vad_clip(audio), audio)


class OrderingTests(unittest.TestCase):
    """Extending the outer edges can make regions touch or overlap; the result
    must stay a sorted, non-overlapping list or the splice re-orders audio."""

    def setUp(self):
        self.engine = ParakeetTranscriber(vad_gate=True)

    def test_overlapping_regions_are_merged_not_duplicated(self):
        # Edge-extending the first region pushes it past the second.
        _FakeVad([(5.0, 6.0), (6.5, 7.0), (20.0, 21.0)]).install(self)
        audio = _clip(40.0)
        out = self.engine._vad_clip(audio)
        # Zeros are the inserted join gap; every other value is a source sample.
        # Source samples increase monotonically, so a region emitted twice or
        # out of order shows up as a non-increasing step.
        src = out[out != 0.0]
        self.assertTrue(
            bool(np.all(np.diff(src) > 0)),
            "source audio was emitted twice or out of order — extending the "
            "outer edges made regions overlap and they were not merged")


if __name__ == "__main__":
    unittest.main()
