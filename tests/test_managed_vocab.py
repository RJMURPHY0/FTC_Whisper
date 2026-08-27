"""Managed (CRM-derived) vocabulary: the strict tier, and the prompt firewall.

Two separate things are pinned here, and they fail in opposite directions.

The CORRECTOR must not put words in anyone's mouth. Managed terms are synced
from a CRM, so unlike the user's own vocabulary nobody reviewed them and there
can be hundreds; a term like "Shell" or "Michaels" sits one phonetic slip from
ordinary speech. A missed correction is a typo. A false one is the app changing
what someone said.

The PROMPT firewall is the other direction, and it is the more serious of the
two. faster-whisper encodes `hotwords` into the decoder prompt under `sot_prev`,
the same token slot as previous-transcript context, so whisper cannot tell a
hint from something that was just spoken and on unclear audio will emit prompt
content as if dictated. That is how CRM company names reached transcripts as
fluent sentences about businesses the user had never mentioned. Managed terms
must never reach a decoder prompt. They are safe in Parakeet's `hotwords_str`,
which only ever re-cases a whole word the model already produced.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from text_expansion import (  # noqa: E402
    MANAGED_JW,
    MANAGED_MIN_TERM_LEN,
    apply_vocabulary_fuzzy,
    managed_variants,
)


def managed(*terms):
    return [{"term": t, "sounds_like": [], "managed": True} for t in terms]


def owned(*terms):
    return [{"term": t, "sounds_like": []} for t in terms]


class ManagedVariantsTests(unittest.TestCase):
    """A CRM holds the registered name; people say the trading name."""

    def test_strips_legal_suffix_and_keeps_the_full_name(self):
        v = managed_variants("Fosseway Freight Ltd")
        self.assertIn("Fosseway Freight Ltd", v)
        self.assertIn("Fosseway Freight", v)

    def test_strips_a_trailing_tagline_after_a_colon(self):
        # A real row from the estate CRM. Nobody dictates the tagline.
        v = managed_variants(
            "Boast International Ltd: Uk & International Freight Forwarder")
        self.assertIn("Boast International", v)

    def test_stacked_suffixes_unwind(self):
        self.assertIn("Kinetics", managed_variants("Kinetics Group"))

    def test_a_suffix_never_becomes_a_term_on_its_own(self):
        for v in managed_variants("Fosseway Freight Ltd"):
            self.assertNotEqual(v.lower(), "ltd")

    def test_short_junk_rows_produce_nothing(self):
        # "Ltl" is a real row in the cache and is pure noise.
        self.assertEqual([], managed_variants("Ltl"))
        self.assertEqual([], managed_variants("   "))

    def test_longest_form_comes_first(self):
        v = managed_variants("Alban Safety Limited")
        self.assertEqual("Alban Safety Limited", v[0])


class ManagedCorrectionTests(unittest.TestCase):

    def test_corrects_a_misheard_company_name(self):
        self.assertEqual(
            "chase up Wincanton about the order",
            apply_vocabulary_fuzzy("chase up win canton about the order",
                                   managed("Wincanton")))

    def test_multi_word_names_are_corrected_at_the_normal_gate(self):
        # A multi-word term is constrained by having to match across
        # consecutive words, so it does not take the strict tier. This is the
        # mis-SPLIT case, which is what the phonetic net is for: same phonemes,
        # wrong word boundary.
        # Composed the way app._managed_entries does it: variants first, so the
        # trading name is a term in its own right. Correcting to the REGISTERED
        # name here would be wrong — it would append "Ltd" to words the user
        # never said.
        entries = managed(*managed_variants("Fosseway Freight Ltd"))
        self.assertEqual(
            "the Fosseway Freight job",
            apply_vocabulary_fuzzy("the fosse way freight job", entries))

    def test_the_metaphone_gate_stays_the_primary_firewall(self):
        # "lightning" and "lighting" are different phonemes, so this is NOT a
        # near-miss the net should chase — matching it would mean loosening the
        # metaphone gate, which is the one thing keeping the whole pass safe.
        # Pinned so a future "improvement" has to argue with this comment.
        text = "the storm lightning job"
        self.assertEqual(text, apply_vocabulary_fuzzy(text,
                                                      managed("Storm Lighting")))

    def test_already_correct_text_is_left_for_the_casing_pass(self):
        # Exact matches are Parakeet's job (asr_engine._post_process); the
        # corrector deliberately skips them rather than doing it twice.
        text = "chase up Wincanton about the order"
        self.assertEqual(text, apply_vocabulary_fuzzy(text, managed("Wincanton")))


class StrictTierTests(unittest.TestCase):
    """Single-word managed terms are where the whole risk lives."""

    def test_single_word_managed_term_that_is_ordinary_english_is_refused(self):
        # jw("shall","shell") ~= 0.88, which clears even the strict floor, and
        # "shall" is not itself a common-listed word so the span check cannot
        # save us. The term has to be refused at ingest instead.
        text = "we shall look at it tomorrow"
        self.assertEqual(text, apply_vocabulary_fuzzy(text, managed("Shell")))

    def test_the_same_term_typed_by_hand_is_allowed(self):
        # The user chose it, so they meant it. This asymmetry is the point of
        # the tier: it is about who reviewed the term, not about the word.
        entries = owned("Shell")
        self.assertNotEqual(
            "", apply_vocabulary_fuzzy("we shall look at it", entries))

    def test_short_single_word_managed_terms_are_dropped(self):
        short = "x" * (MANAGED_MIN_TERM_LEN - 1)
        text = f"the {short}y thing"
        self.assertEqual(text, apply_vocabulary_fuzzy(text, managed(short)))

    def test_strict_floor_is_higher_than_the_user_floor(self):
        from text_expansion import FUZZY_JW
        self.assertGreater(MANAGED_JW, FUZZY_JW)

    def test_ordinary_speech_survives_a_realistic_crm(self):
        crm = managed("Michaels", "Storm", "Next", "Shell", "Wincanton",
                      "No Letting Go", "Call Centre Ltd")
        for text in (
            "michael said he would come",
            "a warm welcome to everyone",
            "sell the next one to them",
            "there is no letting go of that",
            "i will take the call",
            "we shall look at it tomorrow",
        ):
            self.assertEqual(text, apply_vocabulary_fuzzy(text, crm),
                             f"managed vocabulary rewrote ordinary speech: {text!r}")


class PrecedenceTests(unittest.TestCase):

    def test_user_entry_wins_when_it_collides_with_a_managed_one(self):
        # The precompute dedups case-insensitively and keeps the first spelling
        # it sees, so callers must pass user entries first. app.py does.
        entries = owned("BrightLink") + managed("Brightlink Ltd")
        self.assertEqual(
            "the BrightLink rollout",
            apply_vocabulary_fuzzy("the bright link rollout", entries))

    def test_managed_entries_never_use_the_exact_sounds_like_path(self):
        # They carry no mishearings by design: the phonetic net derives them,
        # which is why the CRM side never has to supply pairs.
        for e in managed("Wincanton", "Fosseway Freight Ltd"):
            self.assertEqual([], e["sounds_like"])


class PromptFirewallTests(unittest.TestCase):
    """Managed terms must never reach a whisper decoder prompt."""

    def _app(self, own="Pipedrive", estate=("Prashad Indian Vegetarian Restaurant",)):
        import app as app_mod

        obj = app_mod.WhisperFlowApp.__new__(app_mod.WhisperFlowApp)
        obj._estate_terms = list(estate)
        obj._estate_vocab = ", ".join(estate)
        obj.config = type("C", (), {"custom_vocabulary": own,
                                    "managed_vocab": True})()
        obj._user_entries = lambda kind: []
        return obj

    def test_whisper_hotwords_exclude_managed_terms(self):
        obj = self._app()
        prompt_hw = obj._get_prompt_hotwords()
        self.assertIn("Pipedrive", prompt_hw)
        self.assertNotIn("Prashad", prompt_hw)

    def test_parakeet_hotwords_include_managed_terms(self):
        # Safe there: asr_engine only re-cases a word already produced.
        obj = self._app()
        self.assertIn("Prashad", obj._get_hotwords())

    def test_kill_switch_removes_managed_terms_everywhere(self):
        obj = self._app()
        obj.config.managed_vocab = False
        self.assertEqual([], obj._managed_entries())
        self.assertNotIn("Prashad", obj._get_hotwords())

    def test_managed_entries_survive_a_crm_name_containing_a_comma(self):
        # Real row: "Forklifts Group West, Manteca". The list is the store
        # precisely so a comma in a name cannot split it into invented terms.
        obj = self._app(estate=("Forklifts Group West, Manteca",))
        terms = [e["term"] for e in obj._managed_entries()]
        self.assertIn("Forklifts Group West, Manteca", terms)


class EchoGuardTests(unittest.TestCase):
    """The guard has to cover hotwords, not just the rolling context."""

    def test_guard_reads_both_prompt_sources(self):
        import inspect

        import transcriber

        src = inspect.getsource(transcriber.Transcriber._run)
        self.assertIn("prompt_sources", src)
        # Both halves of the prompt must feed the check. Reverting either one
        # re-opens the CRM-name-echo bug.
        self.assertRegex(
            src, r"prompt_sources\s*=\s*.*context_words.*hotwords_str")
        self.assertIn("norm(text) in norm(prompt_sources)", src)

    def test_a_suppressed_echo_is_reported(self):
        import hallucination
        import transcriber

        seen = []
        hallucination.set_reporter(lambda et, d: seen.append((et, d)))
        try:
            transcriber._report_echo("a b c d e f g h")
        finally:
            hallucination.set_reporter(None)
        self.assertEqual(1, len(seen))
        self.assertEqual("transcribe_prompt_echo", seen[0][0])

    def test_reporting_never_raises_into_the_transcription_path(self):
        import hallucination
        import transcriber

        def boom(_et, _d):
            raise RuntimeError("sink down")

        hallucination.set_reporter(boom)
        try:
            transcriber._report_echo("a b c")  # must not raise
        finally:
            hallucination.set_reporter(None)


class CorpusRegressionTests(unittest.TestCase):
    """The real bar: 150 real CRM terms against 200 real dictations.

    Rewriting genuine speech is a worse regression than missing a correction,
    so this fails if the managed tier touches ANY real transcript. If a future
    threshold change trips this, the change is wrong, not the test.
    """

    def _load(self):
        import json

        base = os.path.join(os.environ.get("APPDATA", ""), "FTC Whisper")
        hist = os.path.join(base, "history.json")
        crm = os.path.join(base, "estate-vocab.json")
        if not (os.path.exists(hist) and os.path.exists(crm)):
            self.skipTest("no local history/CRM cache on this machine")
        with open(hist, encoding="utf-8") as f:
            rows = json.load(f)
        with open(crm, encoding="utf-8") as f:
            terms = json.load(f)
        return rows, terms

    def test_managed_terms_do_not_rewrite_real_dictation(self):
        rows, terms = self._load()
        entries, seen = [], set()
        for raw in terms:
            for v in managed_variants(raw):
                if v.lower() not in seen:
                    seen.add(v.lower())
                    entries.append({"term": v, "sounds_like": [],
                                    "managed": True})
        offenders = []
        for r in rows:
            text = (r.get("transcribed_text") or "").strip()
            if not text:
                continue
            out = apply_vocabulary_fuzzy(text, entries)
            if out != text:
                offenders.append((text, out))
        self.assertEqual(
            [], offenders,
            f"{len(offenders)} real transcript(s) rewritten, e.g. {offenders[:2]}")


if __name__ == "__main__":
    unittest.main()
