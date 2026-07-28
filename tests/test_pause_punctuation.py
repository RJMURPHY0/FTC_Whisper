"""Pause-punctuation cleanup.

Parakeet stamps a full stop (and capitalises the next word) wherever the speaker
pauses, even mid-sentence, so dictation comes out as "near their first. Name.
That can" instead of one flowing sentence. fix_pause_punctuation() undoes the two
UNAMBIGUOUS cases in the raw output; the risky capitalised-noun case is left to
the LLM correction pass. The contract here: real sentence boundaries are never
merged, and no words are ever added or removed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asr_engine import fix_pause_punctuation, ParakeetTranscriber


class FixPausePunctuationTests(unittest.TestCase):
    def test_stop_before_conjunction_becomes_comma(self):
        self.assertEqual(
            fix_pause_punctuation("I did it. And then we left"),
            "I did it, and then we left",
        )
        self.assertEqual(
            fix_pause_punctuation("It works. But slowly"),
            "It works, but slowly",
        )
        self.assertEqual(
            fix_pause_punctuation("Turn it on. Or off"),
            "Turn it on, or off",
        )

    def test_stop_before_lowercase_is_dropped(self):
        self.assertEqual(
            fix_pause_punctuation("would actually be. inserted here"),
            "would actually be inserted here",
        )

    def test_real_sentence_boundary_is_kept(self):
        # Capitalised non-conjunction: could be a real new sentence, so leave it.
        s = "I went home. She stayed behind."
        self.assertEqual(fix_pause_punctuation(s), s)

    def test_because_is_not_merged(self):
        # "Because" legitimately opens a sentence — excluded from the conj. list.
        s = "I left. Because it was late"
        self.assertEqual(fix_pause_punctuation(s), s)

    def test_single_letter_abbreviations_are_safe(self):
        # e.g. / i.e. / a.m. have a single letter before the stop — untouched.
        self.assertEqual(
            fix_pause_punctuation("meet at 9 a.m. and leave"),
            "meet at 9 a.m. and leave",
        )

    def test_ellipsis_is_untouched(self):
        s = "wait... okay then"
        self.assertEqual(fix_pause_punctuation(s), s)

    def test_no_words_are_added_or_removed(self):
        src = "I want a crown. And it can be turned on. Or off. that is the idea"
        out = fix_pause_punctuation(src)

        def words(t):
            return [w.strip(".,").lower() for w in t.split()]

        self.assertEqual(words(src), words(out))

    def test_empty_and_none_safe(self):
        self.assertEqual(fix_pause_punctuation(""), "")
        self.assertEqual(fix_pause_punctuation(None), None)


class PolishIntegrationTests(unittest.TestCase):
    """polish() runs the de-pause pass, then the leading-capital / terminal-stop
    rules — the combination a real dictation goes through."""

    def _engine(self):
        # __init__ only sets flags; the ONNX model is lazy, so no download here.
        return ParakeetTranscriber(auto_punctuate=True)

    def test_polish_fixes_pause_then_capitalises(self):
        e = self._engine()
        self.assertEqual(
            e.polish("would actually be. inserted here"),
            "Would actually be inserted here.",
        )

    def test_polish_fixes_conjunction(self):
        e = self._engine()
        self.assertEqual(
            e.polish("i did it. And then we left"),
            "I did it, and then we left.",
        )

    def test_polish_off_when_auto_punctuate_disabled(self):
        e = ParakeetTranscriber(auto_punctuate=False)
        # Leading capital is always applied (pre-existing behaviour), but the
        # de-pause pass and terminal-stop are skipped: ". And" survives and no
        # full stop is appended.
        self.assertEqual(
            e.polish("i did it. And then"),
            "I did it. And then",
        )


if __name__ == "__main__":
    unittest.main()
