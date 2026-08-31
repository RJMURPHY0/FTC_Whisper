"""Stutter / false-start collapse.

The feature was asked for with a real example: "push all most rec recent
changes" should inject "push all most recent changes" — the aborted fragment
"rec" is dropped, the completed word "recent" kept.

These tests pin BOTH directions. The false-negative cases (a stutter survives)
are the reported want; the false-positive cases (real speech gets rewritten)
are the worse regression, because this code silently deletes words the user
genuinely said. The corpus test at the end fails if the rules ever rewrite real
dictation into a shape none of the two collapse rules explains.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import disfluency


class FalseStartTests(unittest.TestCase):
    """A short fragment immediately followed by the fuller word it aborted."""

    def test_the_reported_example(self):
        self.assertEqual(
            disfluency.destutter("push all most rec recent changes"),
            "push all most recent changes",
        )

    def test_reported_example_in_a_sentence(self):
        self.assertEqual(
            disfluency.destutter("Do this and then push almost rec recent changes to main."),
            "Do this and then push almost recent changes to main.",
        )

    def test_common_fragments(self):
        for src, want in [
            ("prob probably not", "probably not"),
            ("def definitely yes", "definitely yes"),
            ("the config configuration file", "the configuration file"),
            ("trans transcription is slow", "transcription is slow"),
            ("be able to re resize stuff", "be able to resize stuff"),
        ]:
            self.assertEqual(disfluency.destutter(src), want, src)

    def test_leading_capital_carries_to_the_kept_word(self):
        self.assertEqual(disfluency.destutter("Rec recent changes"), "Recent changes")

    def test_fragment_across_a_comma(self):
        self.assertEqual(disfluency.destutter("push rec, recent changes"),
                         "push recent changes")


class FunctionDoubleTests(unittest.TestCase):
    """An exact repeat of a never-validly-doubled function word."""

    def test_common_doubles(self):
        for src, want in [
            ("I I think we should go", "I think we should go"),
            ("the the file is here", "the file is here"),
            ("optimal usage for for this task", "optimal usage for this task"),
            ("right but it it then just", "right but it then just"),
            ("they actually do do something", "they actually do something"),
        ]:
            self.assertEqual(disfluency.destutter(src), want, src)

    def test_triple_collapses_to_one(self):
        self.assertEqual(disfluency.destutter("turn it on on on or off"),
                         "turn it on or off")

    def test_capital_second_is_kept_and_stays_capital(self):
        # "restricted and And if" — the second, capitalised copy is kept.
        self.assertEqual(disfluency.destutter("ones you restricted and And if I"),
                         "ones you restricted And if I")


class NeverTouchTests(unittest.TestCase):
    """Real speech that merely resembles a stutter must be returned unchanged."""

    def test_a_word_that_prefixes_the_next_but_is_a_real_word(self):
        for s in ["the theory of everything", "does he help me",
                  "in industry today", "we website today", "part party time",
                  "so sophisticated"]:
            self.assertEqual(disfluency.destutter(s), s, s)

    def test_base_then_inflected_is_two_words(self):
        for s in ["I bought a car cars are expensive", "read reading is fun",
                  "book books on the shelf", "help helped him", "form former self"]:
            self.assertEqual(disfluency.destutter(s), s, s)

    def test_emphatic_and_grammatical_doubles_survive(self):
        for s in ["it was very very important", "no no that is fine",
                  "he had had enough", "I know that that report is late",
                  "really really good", "so so tired"]:
            self.assertEqual(disfluency.destutter(s), s, s)

    def test_sentence_boundary_is_never_crossed(self):
        self.assertEqual(disfluency.destutter("the cat. Cat food is here."),
                         "the cat. Cat food is here.")

    def test_content_word_double_is_not_collapsed(self):
        # Only function words are collapsed on an exact double; a repeated content
        # word may well be emphasis and is left alone.
        self.assertEqual(disfluency.destutter("recent recent thing"),
                         "recent recent thing")

    def test_short_and_empty_inputs(self):
        for s in ["", "hello", "  ", "one two three"]:
            self.assertEqual(disfluency.destutter(s), s, repr(s))


class WhitespaceTests(unittest.TestCase):
    """Only the fragment and the space beside it are removed — paragraph breaks
    and every other byte survive."""

    def test_paragraph_break_survives_a_collapse(self):
        src = "para one has for for this.\n\nPara two is clean."
        self.assertEqual(disfluency.destutter(src),
                         "para one has for this.\n\nPara two is clean.")

    def test_no_qualifying_pair_returns_byte_identical(self):
        src = "line one\n\nline two\twith  odd   spacing"
        self.assertIs(disfluency.destutter(src), src)


class CorpusRegressionTests(unittest.TestCase):
    """Run every real stored transcript through the collapser and prove each
    change it makes is explained by one of the two rules. A change that fits no
    rule means the guards have drifted and real dictation is being corrupted."""

    def _load(self):
        path = os.path.join(os.environ.get("APPDATA", ""), "FTC Whisper",
                            "history.json")
        if not os.path.exists(path):
            self.skipTest("no local history.json corpus on this machine")
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        texts = [r.get("transcribed_text", "") for r in rows]
        return [t for t in texts if t and t.strip()]

    def _explained(self, before: str, after: str) -> bool:
        """The collapser only ever DELETES tokens, never invents or reorders
        them, and every deleted token is either a collapsible function word or a
        false-start fragment of some word in the utterance. Compared on token
        KEYS so a carried capital ("Rec" -> "Recent") is not read as a new word.
        """
        from collections import Counter
        a = [disfluency._key(t) for t in before.split()]
        b = [disfluency._key(t) for t in after.split()]
        if len(b) >= len(a):            # a real change only ever shortens
            return False
        if Counter(b) - Counter(a):     # no key may appear that wasn't there
            return False
        for w in (Counter(a) - Counter(b)).elements():
            if w in disfluency._DUP_COLLAPSE:
                continue
            if any(disfluency._is_false_start(w, f) for f in a):
                continue
            return False
        return True

    def test_every_corpus_change_is_a_known_stutter(self):
        texts = self._load()
        for t in texts:
            out = disfluency.destutter(t)
            if out == t:
                continue
            # No paragraph break may be lost.
            self.assertEqual(t.count("\n"), out.count("\n"),
                             f"newline lost in: {t[:80]!r}")
            self.assertTrue(self._explained(t, out),
                            f"unexplained rewrite:\n  {t!r}\n  {out!r}")

    def test_the_two_known_stutters_are_actually_fixed(self):
        texts = self._load()
        hits = [disfluency.destutter(t) for t in texts if "rec recent" in t]
        self.assertTrue(hits, "corpus no longer contains the known stutter")
        for h in hits:
            self.assertNotIn("rec recent", h)


if __name__ == "__main__":
    unittest.main()
