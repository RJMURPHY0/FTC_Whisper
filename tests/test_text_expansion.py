"""Vocabulary corrections and snippet expansion.

This module rewrites words the user actually said, so the tests here are
mostly about what it must REFUSE to touch. A missed correction is a typo; a
false positive is the app putting words in someone's mouth.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from text_expansion import (  # noqa: E402
    MIN_PATTERN_LEN,
    apply_snippets,
    apply_vocabulary,
    validate_sounds_like,
    validate_trigger,
    vocabulary_hotwords,
)


def vocab(term, *sounds_like, **kw):
    e = {"term": term, "sounds_like": list(sounds_like)}
    e.update(kw)
    return e


def snip(trigger, body, **kw):
    e = {"trigger": trigger, "body": body}
    e.update(kw)
    return e


class VocabularyTests(unittest.TestCase):

    def test_replaces_a_known_mishearing(self):
        entries = [vocab("Pipedrive", "pipe drive")]
        self.assertEqual("We logged it in Pipedrive today.",
                         apply_vocabulary("We logged it in pipe drive today.",
                                          entries))

    def test_match_is_case_insensitive_but_the_term_keeps_its_casing(self):
        entries = [vocab("Pipedrive", "pipe drive")]
        for said in ("pipe drive", "Pipe Drive", "PIPE DRIVE"):
            self.assertEqual(f"Open Pipedrive now.",
                             apply_vocabulary(f"Open {said} now.", entries))

    def test_never_matches_inside_a_word(self):
        # The failure this exists to prevent: a substring match mangling an
        # unrelated word that merely contains the pattern.
        entries = [vocab("CRM", "cra")]
        for text in ("He scrapes the data.", "A crate of parts.", "cracker"):
            self.assertEqual(text, apply_vocabulary(text, entries))

    def test_matches_across_irregular_whitespace(self):
        entries = [vocab("Pipedrive", "pipe drive")]
        self.assertEqual("Use Pipedrive.",
                         apply_vocabulary("Use pipe   drive.", entries))
        self.assertEqual("Use Pipedrive.",
                         apply_vocabulary("Use pipe\ndrive.", entries))

    def test_longest_variant_wins(self):
        entries = [vocab("FTC Safety Solutions", "ftc safety solutions limited"),
                   vocab("FTC", "ftc safety")]
        self.assertEqual(
            "Invoice FTC Safety Solutions please.",
            apply_vocabulary("Invoice ftc safety solutions limited please.",
                             entries))

    def test_a_replacement_is_never_re_matched(self):
        # Single pass: the inserted term must not be eligible for another rule,
        # or corrections chain into nonsense. Mid-sentence so the assertion is
        # about cascading alone, not about sentence-start casing.
        entries = [vocab("beta", "alpha"), vocab("gamma", "beta")]
        self.assertEqual("Say beta now.",
                         apply_vocabulary("Say alpha now.", entries))

    def test_variants_below_the_floor_are_ignored(self):
        entries = [vocab("United States", "us")]
        text = "Give us a call."
        self.assertEqual(text, apply_vocabulary(text, entries))

    def test_a_variant_equal_to_the_term_is_a_no_op(self):
        entries = [vocab("Pipedrive", "Pipedrive")]
        self.assertEqual("Open Pipedrive.",
                         apply_vocabulary("Open Pipedrive.", entries))

    def test_lower_case_term_is_capitalised_only_at_a_sentence_start(self):
        entries = [vocab("e.g.", "for example")]
        self.assertEqual("E.g. the roof survey.",
                         apply_vocabulary("For example the roof survey.",
                                          entries))
        self.assertEqual("Take e.g. the roof survey.",
                         apply_vocabulary("Take for example the roof survey.",
                                          entries))

    def test_deliberate_casing_survives_a_sentence_start(self):
        entries = [vocab("iPhone", "i phone"), vocab("FTC", "f t c")]
        self.assertEqual("iPhone is charged.",
                         apply_vocabulary("I phone is charged.", entries))
        self.assertEqual("FTC won the tender.",
                         apply_vocabulary("F t c won the tender.", entries))

    def test_soft_deleted_entries_do_nothing(self):
        entries = [vocab("Pipedrive", "pipe drive", deleted=True)]
        self.assertEqual("Open pipe drive.",
                         apply_vocabulary("Open pipe drive.", entries))

    def test_empty_and_malformed_input_is_safe(self):
        self.assertEqual("", apply_vocabulary("", [vocab("A", "aaa")]))
        self.assertEqual("hi", apply_vocabulary("hi", None))
        self.assertEqual("hi", apply_vocabulary("hi", [None, "nonsense", {}]))
        self.assertEqual("hi", apply_vocabulary("hi", [vocab("", "aaa")]))
        self.assertEqual("hi", apply_vocabulary("hi", [vocab("X")]))

    def test_regex_metacharacters_in_a_variant_are_literal(self):
        entries = [vocab("C++", "c plus plus"), vocab("query", "que.ry")]
        self.assertEqual("I write C++.",
                         apply_vocabulary("I write c plus plus.", entries))
        # "que.ry" must not match "queXry" — the dot is escaped.
        self.assertEqual("Run queXry now.",
                         apply_vocabulary("Run queXry now.", entries))


class HotwordTests(unittest.TestCase):

    def test_terms_only_never_the_mishearings(self):
        entries = [vocab("Pipedrive", "pipe drive"), vocab("FTC", "f t c")]
        hot = vocabulary_hotwords(entries)
        self.assertIn("Pipedrive", hot)
        self.assertIn("FTC", hot)
        # Feeding the mishearing to the engine would bias it towards producing
        # exactly the output the entry exists to correct.
        self.assertNotIn("pipe drive", hot)
        self.assertNotIn("f t c", hot)

    def test_deduplicates_and_skips_deleted(self):
        entries = [vocab("FTC"), vocab("ftc"), vocab("Gone", deleted=True)]
        self.assertEqual("FTC", vocabulary_hotwords(entries))

    def test_empty(self):
        self.assertEqual("", vocabulary_hotwords([]))
        self.assertEqual("", vocabulary_hotwords(None))


class SnippetTests(unittest.TestCase):

    def test_expands_a_trigger(self):
        entries = [snip("my email", "ryan@ftc.co.uk")]
        self.assertEqual("It's ryan@ftc.co.uk really.",
                         apply_snippets("It's my email really.", entries))

    def test_longest_trigger_wins(self):
        entries = [snip("my email", "personal@x.com"),
                   snip("my work email", "work@ftc.co.uk")]
        self.assertEqual("Send to work@ftc.co.uk.",
                         apply_snippets("Send to my work email.", entries))

    def test_multi_line_body(self):
        entries = [snip("sign off", "Kind regards,\nRyan Murphy\nFTC Safety")]
        self.assertEqual("Kind regards,\nRyan Murphy\nFTC Safety",
                         apply_snippets("Sign off", entries))

    def test_body_is_not_rescanned(self):
        # A body containing another trigger must not expand again.
        entries = [snip("intro", "my email is here"),
                   snip("my email", "ryan@ftc.co.uk")]
        self.assertEqual("Say my email is here now.",
                         apply_snippets("Say intro now.", entries))

    def test_a_body_is_inserted_verbatim_never_recased(self):
        # Vocabulary capitalises an all-lower-case term at a sentence start;
        # a snippet body must NOT get that treatment, or a snippet holding an
        # email address becomes "Ryan@ftc.co.uk" at the top of a sentence.
        entries = [snip("my email", "ryan@ftc.co.uk")]
        self.assertEqual("ryan@ftc.co.uk is the address.",
                         apply_snippets("My email is the address.", entries))

    def test_never_fires_inside_a_word(self):
        entries = [snip("cat", "Concrete Assessment Test")]
        for text in ("The catalogue arrived.", "concatenate"):
            self.assertEqual(text, apply_snippets(text, entries))

    def test_short_trigger_or_empty_body_is_ignored(self):
        self.assertEqual("go to it", apply_snippets("go to it", [snip("to", "X")]))
        self.assertEqual("my email", apply_snippets("my email",
                                                    [snip("my email", "")]))

    def test_soft_deleted_and_malformed_are_safe(self):
        self.assertEqual("my email",
                         apply_snippets("my email",
                                        [snip("my email", "x", deleted=True)]))
        self.assertEqual("hi", apply_snippets("hi", [None, 7, {}]))
        self.assertEqual("", apply_snippets("", [snip("my email", "x")]))


class ValidationTests(unittest.TestCase):

    def test_sounds_like_floor(self):
        ok, reason = validate_sounds_like("us")
        self.assertFalse(ok)
        self.assertIn(str(MIN_PATTERN_LEN), reason)
        self.assertTrue(validate_sounds_like("pipe drive")[0])

    def test_sounds_like_cannot_equal_the_term(self):
        ok, reason = validate_sounds_like("Pipedrive", "pipedrive")
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_sounds_like_rejects_blank(self):
        for value in ("", "   ", None):
            self.assertFalse(validate_sounds_like(value)[0])

    def test_trigger_rejects_common_words(self):
        for word in ("the", "and", "thanks", "Okay"):
            ok, reason = validate_trigger(word)
            self.assertFalse(ok, f"{word!r} should be refused")
            self.assertTrue(reason)

    def test_trigger_accepts_a_real_phrase(self):
        self.assertTrue(validate_trigger("my email")[0])
        self.assertTrue(validate_trigger("standard sign off")[0])

    def test_every_reason_is_user_facing_text(self):
        # A validator that fails without saying why produces a dead Save button.
        for ok, reason in (validate_sounds_like("a"),
                           validate_trigger(""),
                           validate_trigger("the")):
            self.assertFalse(ok)
            self.assertTrue(reason.strip())


class FuzzyVocabularyTests(unittest.TestCase):
    """The phonetic safety net. Split, like the module, into what it MUST fix and
    the far longer list of what it must refuse to touch — a false positive here
    puts a word in the user's mouth."""

    def setUp(self):
        from text_expansion import apply_vocabulary_fuzzy, _dm
        self.fuzzy = apply_vocabulary_fuzzy
        if _dm() is None:
            self.skipTest("metaphone encoder unavailable")

    # ── must correct (close mishearings the user never listed) ────────────────
    def test_catches_close_mishearings_of_a_term(self):
        entries = [vocab("Vercel")]
        for said in ("vercell", "ver cell", "versel", "versaille", "Vercell"):
            self.assertEqual(
                "Deploy to Vercel now.",
                self.fuzzy(f"Deploy to {said} now.", entries),
                f"{said!r} should have been corrected to Vercel")

    def test_catches_a_split_multiword_mishearing(self):
        entries = [vocab("Pipedrive")]
        self.assertEqual("Log it in Pipedrive.",
                         self.fuzzy("Log it in pipe drife.", entries))

    def test_corrected_term_keeps_its_casing(self):
        entries = [vocab("Vercel")]
        self.assertEqual("Use Vercel.", self.fuzzy("Use vercell.", entries))

    def test_lowercase_term_capitalised_only_at_sentence_start(self):
        entries = [vocab("kubectl")]
        self.assertEqual("Kubectl applies it.",
                         self.fuzzy("Kubecuttle applies it.", entries))

    # ── must NOT touch (the false-positive firewall) ──────────────────────────
    def test_leaves_ordinary_english_alone(self):
        entries = [vocab("Vercel")]
        for text in ("Click the cell to edit it.",
                     "Open it in excel please.",
                     "Ring the bell when ready.",
                     "That is the sell side desk.",
                     "My cell rang twice.",
                     "We should sell the shares.",
                     "A crate of parts arrived."):
            self.assertEqual(text, self.fuzzy(text, entries),
                             f"{text!r} should have been left untouched")

    def test_unrelated_words_are_never_pulled_in(self):
        entries = [vocab("Vercel")]
        self.assertEqual("The parcel is at the front desk.",
                         self.fuzzy("The parcel is at the front desk.", entries))

    def test_short_terms_do_not_fuzzy_match(self):
        # Under FUZZY_MIN_TERM_LEN: too collision-prone, exact path only.
        entries = [vocab("AWS")]
        self.assertEqual("It was a test.", self.fuzzy("It was a test.", entries))

    def test_no_metaphone_is_a_clean_noop(self, ):
        import text_expansion as te
        orig = te._dm
        te._dm = lambda: None
        try:
            self.assertEqual("Deploy to vercell now.",
                             te.apply_vocabulary_fuzzy("Deploy to vercell now.",
                                                       [vocab("Vercel")]))
        finally:
            te._dm = orig

    def test_soft_deleted_and_empty_are_safe(self):
        self.assertEqual("say vercell",
                         self.fuzzy("say vercell", [vocab("Vercel", deleted=True)]))
        self.assertEqual("", self.fuzzy("", [vocab("Vercel")]))
        self.assertEqual("hi", self.fuzzy("hi", None))
        self.assertEqual("hi", self.fuzzy("hi", [None, "x", {}]))

    def test_no_cascade_single_pass(self):
        # A span is replaced once; the inserted term is not re-scanned.
        entries = [vocab("Vercel"), vocab("Marcel")]
        # "vercell" -> Vercel, and Vercel must not then be pulled to Marcel.
        self.assertEqual("Ship on Vercel.", self.fuzzy("Ship on vercell.", entries))

    def test_bounded_cost_is_negligible(self):
        # "Won't affect speed": prove the pass is sub-millisecond-ish even with a
        # large vocabulary over a long transcript. Runs on the already-final text,
        # never on the transcription/injection path.
        import time
        entries = [vocab(f"Term{i}word") for i in range(100)]
        entries.append(vocab("Vercel"))
        text = ("Deploy the build to vercell and then check the logs "
                "carefully for any errors before we ship it out today. ") * 12
        t0 = time.perf_counter()
        for _ in range(5):
            out = self.fuzzy(text, entries)
        dt = (time.perf_counter() - t0) / 5.0
        self.assertIn("Vercel", out)
        # Generous ceiling; typical run is a fraction of this. A regression that
        # made this heavy would trip here.
        self.assertLess(dt, 0.05, f"fuzzy pass too slow: {dt*1000:.1f}ms")


class PipelineOrderTests(unittest.TestCase):
    """Vocabulary runs before snippets (app.py applies them in that order), so a
    corrected term can complete a snippet trigger but never the reverse."""

    def test_vocabulary_correction_can_complete_a_snippet_trigger(self):
        v = [vocab("Pipedrive", "pipe drive")]
        s = [snip("Pipedrive link", "https://ftc.pipedrive.com")]
        text = apply_vocabulary("Send the pipe drive link.", v)
        self.assertEqual("Send the https://ftc.pipedrive.com.",
                         apply_snippets(text, s))


if __name__ == "__main__":
    unittest.main()
