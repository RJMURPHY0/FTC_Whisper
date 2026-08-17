"""Pause-punctuation artefacts that survived the v1.6.x rules.

Reported live with a real example: "my Claude OS is set up 100%. Correctly."
Two gaps caused it:

1. `_STOP_BEFORE_LOWER` required `[a-z]{2,}` before the stop, so a stop after a
   NUMBER or percentage ("100%. correctly") could never match.
2. Nothing handled a stop followed by a CAPITALISED word, because in general
   that is a real sentence boundary. The one shape that never is: a "sentence"
   consisting of a single -ly adverb and nothing else.

The second rule is deliberately narrow. Sentences legitimately open with
"Obviously, ..." and "Basically, ...", so only the standalone adverb merges.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asr_engine import fix_pause_punctuation as fix


class TheReportedFailure(unittest.TestCase):
    def test_the_exact_reported_text(self):
        got = fix("My Claude OS is set up 100%. Correctly. Everything works.")
        self.assertEqual("My Claude OS is set up 100% correctly. Everything works.", got)

    def test_stop_after_a_number_then_lowercase(self):
        self.assertEqual("It is set up 100 correctly.",
                         fix("It is set up 100. correctly."))

    def test_stop_after_a_percentage_then_lowercase(self):
        self.assertEqual("It is set up 100% correctly.",
                         fix("It is set up 100%. correctly."))

    def test_lone_adverb_at_the_very_end(self):
        self.assertEqual("The whole thing is working properly.",
                         fix("The whole thing is working. Properly."))


class RealSentencesSurvive(unittest.TestCase):
    """The regression that would be worse than the bug."""

    def test_adverb_opener_with_a_comma_is_untouched(self):
        t = "That is done. Obviously, we should still check it."
        self.assertEqual(t, fix(t))

    def test_adverb_starting_a_longer_sentence_is_untouched(self):
        t = "It failed. Basically the whole pipeline needs a rewrite."
        self.assertEqual(t, fix(t))

    def test_ordinary_sentence_boundary_is_untouched(self):
        t = "The scaffold is up. Inspection is booked for Tuesday."
        self.assertEqual(t, fix(t))

    def test_a_real_number_sentence_end_is_untouched(self):
        # A capital after a number is a genuine boundary; only a lone -ly
        # adverb or a lowercase continuation is treated as an artefact.
        t = "The total came to 100. Then we went home."
        self.assertEqual(t, fix(t))

    def test_abbreviations_are_untouched(self):
        t = "Meet at 9 a.m. tomorrow."
        self.assertEqual(t, fix(t))

    def test_a_noun_sentence_of_one_word_is_untouched(self):
        # Only -ly adverbs qualify; a one-word noun sentence can be intentional.
        t = "I asked him twice. Nothing."
        self.assertEqual(t, fix(t))


class NoInteractionWithTheExistingRules(unittest.TestCase):
    def test_conjunction_rule_still_applies(self):
        self.assertEqual("I did it, and then I left.",
                         fix("I did it. And then I left."))

    def test_lowercase_continuation_rule_still_applies(self):
        self.assertEqual("would actually be inserted",
                         fix("would actually be. inserted"))

    def test_both_artefacts_in_one_utterance(self):
        got = fix("It is set up 100%. Correctly. And then we shipped it.")
        self.assertEqual("It is set up 100% correctly, and then we shipped it.", got)

    def test_empty_and_none_safe(self):
        self.assertEqual("", fix(""))
        self.assertIsNone(fix(None))


class RealCorpusRegression(unittest.TestCase):
    """Run the rules over Ryan's actual stored transcripts. Any change must be
    one of the artefact shapes above — the rules must not quietly rewrite the
    hundreds of legitimate sentence boundaries in real dictation."""

    def _corpus(self):
        import json
        p = os.path.join(os.environ.get("APPDATA", ""), "FTC Whisper", "history.json")
        if not os.path.isfile(p):
            self.skipTest("no local history corpus on this machine")
        with open(p, encoding="utf-8") as f:
            return [(r.get("transcribed_text") or "") for r in json.load(f)]

    def test_changes_are_rare_and_all_are_artefacts(self):
        import re
        texts = self._corpus()
        changed = [(t, fix(t)) for t in texts if fix(t) != t]
        # Every change must be explainable by one of the three rules.
        lone_adverb = re.compile(r"\.\s+[A-Z][a-z]+ly\s*[.!?]")
        stop_lower = re.compile(r"(?:[a-z]{2,}|\d+%?)\.\s+[a-z]")
        conj = re.compile(r"\.\s+(?:And|But|So|Or|Nor|Yet)\b")
        for before, _after in changed:
            self.assertTrue(
                lone_adverb.search(before) or stop_lower.search(before)
                or conj.search(before),
                f"unexplained rewrite of real dictation: {before[:120]!r}")

    def test_the_new_lone_adverb_rule_fires_on_real_data_but_rarely(self):
        import re
        texts = self._corpus()
        lone = re.compile(r"\.\s+[A-Z][a-z]+ly\s*[.!?]")
        hits = [t for t in texts if lone.search(t)]
        # It should be a rare shape — if it ever matched a large share of real
        # transcripts, the rule would be too broad to trust.
        self.assertLess(len(hits), max(5, len(texts) * 0.05),
                        "lone-adverb rule is matching far too much real speech")


if __name__ == "__main__":
    unittest.main()
