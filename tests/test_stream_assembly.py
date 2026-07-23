"""Unit tests for pause/paragraph assembly and final polish.

Guards the two shipped punctuation artefacts:
  - "would actually be. Inserted." — Parakeet ends every pause with a period,
    and the joiner used to keep it (and even break a paragraph there) when the
    speaker was merely thinking mid-sentence.
  - "And then,." — a period stacked onto a trailing comma by the assembly's
    invented period and by polish()'s terminal-punctuation append.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_session import StreamingSession
from asr_engine import ParakeetTranscriber


def assemble(entries):
    return StreamingSession._assemble_paragraphs(entries)


class TestAssembleParagraphs(unittest.TestCase):
    def test_confident_paragraph_break(self):
        out = assemble([("The first point is done.", False),
                        ("Now a new topic starts.", True)])
        self.assertEqual(out, "The first point is done.\n\nNow a new topic starts.")

    def test_thinking_pause_mid_sentence_drops_period_and_break(self):
        # Long pause mid-sentence: model ended the chunk with a period, but the
        # lowercase continuation proves the sentence carries on.
        out = assemble([("so it would actually be.", False),
                        ("inserted at the end.", True)])
        self.assertEqual(out, "so it would actually be inserted at the end.")

    def test_commit_boundary_period_lowercase_continuation(self):
        # Ordinary ~300ms commit boundary (no paragraph flag) with a stray period.
        out = assemble([("we should fix the.", False),
                        ("grammar as well.", False)])
        self.assertEqual(out, "we should fix the grammar as well.")

    def test_unpunctuated_break_never_invents_period(self):
        # The old code turned this into "and then,.\n\nYeah." — the shipped bug.
        out = assemble([("and then,", False), ("Yeah.", True)])
        self.assertNotIn(",.", out)
        self.assertNotIn("\n\n", out)
        self.assertEqual(out, "and then, Yeah.")

    def test_break_needs_capital_next(self):
        out = assemble([("sentence is finished.", False),
                        ("but this continues lowercase.", True)])
        self.assertNotIn("\n\n", out)

    def test_exclamation_never_stripped(self):
        out = assemble([("stop!", False), ("really.", True)])
        self.assertEqual(out, "stop! really.")

    def test_ellipsis_not_stripped(self):
        out = assemble([("well...", False), ("maybe not.", False)])
        self.assertEqual(out, "well... maybe not.")

    def test_flags_off_reduces_to_space_join(self):
        out = assemble([("First bit.", False), ("Second bit.", False)])
        self.assertEqual(out, "First bit. Second bit.")

    def test_empty_entries_skipped(self):
        out = assemble([("", False), ("Hello there.", False), ("", True)])
        self.assertEqual(out, "Hello there.")


class TestPolish(unittest.TestCase):
    def _engine(self, auto_punctuate=True):
        # polish() only reads self.auto_punctuate — no model load needed.
        eng = object.__new__(ParakeetTranscriber)
        eng.auto_punctuate = auto_punctuate
        return eng

    def test_trailing_comma_replaced_not_stacked(self):
        self.assertEqual(self._engine().polish("and then,"), "And then.")

    def test_trailing_colon_replaced(self):
        self.assertEqual(self._engine().polish("here is the list:"),
                         "Here is the list.")

    def test_normal_sentence_untouched(self):
        self.assertEqual(self._engine().polish("All done."), "All done.")

    def test_terminal_period_added(self):
        self.assertEqual(self._engine().polish("small it"), "Small it.")

    def test_auto_punctuate_off_leaves_trailing_comma(self):
        self.assertEqual(self._engine(False).polish("and then,"), "And then,")


if __name__ == "__main__":
    unittest.main()
