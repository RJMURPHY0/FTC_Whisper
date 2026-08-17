"""Degenerate-repetition guard.

The failure this encodes was reported live: a user's dictation came back as
"no, no, no, no, no, …" repeated dozens of times — an RNN-T decoder cycling on
one audio frame. Nothing in the pipeline caught it, so it was injected verbatim.

These tests pin BOTH directions. The false-negative cases (a loop survives) are
the reported bug; the false-positive cases (real speech gets rewritten) are the
worse regression, because this code silently edits what the user actually said.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hallucination


class PureLoopSuppressionTests(unittest.TestCase):
    """An utterance that is nothing but a loop must return empty, not a
    trimmed-but-still-wrong sentence."""

    def test_the_reported_failure(self):
        text = "No, " + "no, " * 38 + "no."
        self.assertEqual(hallucination.clean(text), "")

    def test_pure_loop_of_a_phrase(self):
        text = " ".join(["I don't know."] * 8)
        self.assertEqual(hallucination.clean(text), "")

    def test_pure_loop_needs_six_repeats(self):
        # Five is trimmed but NOT wiped: deleting everything takes more
        # evidence than trimming does.
        self.assertNotEqual(hallucination.clean("No, no, no, no, no."), "")

    def test_is_pure_loop_ignores_punctuation_and_case(self):
        self.assertTrue(hallucination.is_pure_loop("No, no. NO no, no, no, no"))

    def test_punctuation_only_input_is_not_a_loop(self):
        # Normalises to empty units — must not be treated as a repeated word.
        self.assertFalse(hallucination.is_pure_loop(". . . . . . . ."))


class CollapseTests(unittest.TestCase):
    """A loop embedded in real speech is cut back, never deleted wholesale."""

    def test_embedded_loop_is_collapsed_and_speech_survives(self):
        text = "I said " + "no, " * 20 + "and then I left."
        out = hallucination.clean(text)
        self.assertIn("I said", out)
        self.assertIn("and then I left.", out)
        self.assertEqual(out.lower().count("no"), 3)

    def test_collapse_keeps_terminal_punctuation_of_the_run(self):
        # The LAST occurrence carries the stop the decoder attached; losing it
        # would run the loop into the next sentence.
        out = hallucination.clean("Well " + "no, " * 9 + "no. Then we left.")
        self.assertIn("no.", out)
        self.assertIn("Then we left.", out)

    def test_phrase_loop_collapsed_within_real_speech(self):
        text = "Honestly " + "I don't know. " * 9 + "But we should ask."
        out = hallucination.clean(text)
        self.assertIn("Honestly", out)
        self.assertIn("But we should ask.", out)
        self.assertEqual(out.count("I don't know"), 3)

    def test_leading_capital_is_preserved(self):
        out = hallucination.clean("No, " + "no, " * 10 + "yes it is.")
        self.assertTrue(out.startswith("No,"))


class RealSpeechUntouchedTests(unittest.TestCase):
    """The regression that would be worse than the bug."""

    def test_ordinary_sentence_is_byte_identical(self):
        text = ("The site inspection is booked for Tuesday and I need the "
                "risk assessment signed off before anyone goes on site.")
        self.assertIs(hallucination.clean(text), text)

    def test_emphatic_triple_is_left_alone(self):
        # Real emphatic speech tops out around three — this must survive.
        text = "No, no, no, that is not what I meant."
        self.assertIs(hallucination.clean(text), text)

    def test_legitimately_repeated_words_survive(self):
        text = "It is very very good and that that clause needs redrafting."
        self.assertIs(hallucination.clean(text), text)

    def test_repeated_word_not_adjacent_is_untouched(self):
        text = "No I said no and he said no and then she said no as well."
        self.assertIs(hallucination.clean(text), text)

    def test_short_input_is_untouched(self):
        self.assertIs(hallucination.clean("No, no."), "No, no.")

    def test_empty_and_none_safe(self):
        self.assertEqual(hallucination.clean(""), "")
        self.assertIsNone(hallucination.clean(None))


class ReporterTests(unittest.TestCase):
    """Telemetry must fire for fleet visibility, and must never be able to
    break the transcription path."""

    def tearDown(self):
        hallucination.set_reporter(None)

    def test_suppression_is_reported(self):
        seen = []
        hallucination.set_reporter(lambda e, d: seen.append((e, d)))
        hallucination.clean("No, " + "no, " * 38 + "no.", source="parakeet")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "transcribe_repetition")
        self.assertEqual(seen[0][1]["action"], "suppressed")
        self.assertEqual(seen[0][1]["source"], "parakeet")

    def test_collapse_is_reported_with_the_run_length(self):
        seen = []
        hallucination.set_reporter(lambda e, d: seen.append((e, d)))
        hallucination.clean("I said " + "no, " * 20 + "and left.")
        self.assertEqual(seen[0][1]["action"], "collapsed")
        self.assertGreaterEqual(seen[0][1]["repeats"], 4)

    def test_clean_speech_reports_nothing(self):
        seen = []
        hallucination.set_reporter(lambda e, d: seen.append((e, d)))
        hallucination.clean("A perfectly ordinary sentence about scaffolding.")
        self.assertEqual(seen, [])

    def test_a_throwing_reporter_cannot_break_transcription(self):
        def _boom(_e, _d):
            raise RuntimeError("supabase down")
        hallucination.set_reporter(_boom)
        self.assertEqual(hallucination.clean("No, " + "no, " * 38 + "no."), "")


class EngineIntegrationTests(unittest.TestCase):
    """The guard has to sit INSIDE each engine's post-processing, not at some
    call site a future path could bypass."""

    def test_parakeet_post_process_suppresses_a_loop(self):
        import asr_engine
        t = asr_engine.ParakeetTranscriber()
        self.assertEqual(t._post_process("No, " + "no, " * 38 + "no."), "")

    def test_parakeet_post_process_keeps_real_speech(self):
        import asr_engine
        t = asr_engine.ParakeetTranscriber()
        out = t._post_process("The scaffold inspection is due on Friday.")
        self.assertIn("scaffold inspection", out)

    def test_whisper_post_process_suppresses_a_loop(self):
        import transcriber
        t = transcriber.Transcriber(model_size="base.en")
        self.assertEqual(t._post_process("No, " + "no, " * 38 + "no."), "")


if __name__ == "__main__":
    unittest.main()
