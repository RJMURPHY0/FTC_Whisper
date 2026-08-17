"""AIRefiner.suggest_mishearings — the vocab editor's AI helper.

Parsing/validation only; the LLM call is mocked, so these never touch the network.
The method is best-effort and off the dictation path, so the contract is: clean up
whatever the model returns, and never crash.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_refiner import AIRefiner  # noqa: E402


def refiner_with(reply: str) -> AIRefiner:
    r = AIRefiner(api_key="test")           # is_available True
    r._raw_chat = lambda system, user, max_tokens=256: reply
    return r


class SuggestMishearingsTests(unittest.TestCase):

    def test_strips_formatting_lowercases_and_dedupes(self):
        r = refiner_with('1. The Cell\n2. "ver cell"\n- vercell\nVersel\nthe cell')
        self.assertEqual(["the cell", "ver cell", "vercell", "versel"],
                         r.suggest_mishearings("Vercel"))

    def test_drops_the_term_itself(self):
        r = refiner_with("Vercel\nvercel\nvercell")
        self.assertEqual(["vercell"], r.suggest_mishearings("Vercel"))

    def test_drops_too_short_and_overlong_lines(self):
        r = refiner_with("x\nab\nvercell\n"
                         "this is clearly a whole sentence not a mishearing at all")
        self.assertEqual(["vercell"], r.suggest_mishearings("Vercel"))

    def test_respects_the_limit(self):
        r = refiner_with("\n".join(f"cand{i}word" for i in range(20)))
        self.assertEqual(3, len(r.suggest_mishearings("Vercel", limit=3)))

    def test_empty_reply_is_empty_list(self):
        self.assertEqual([], refiner_with("").suggest_mishearings("Vercel"))
        self.assertEqual([], refiner_with("   \n  \n").suggest_mishearings("Vercel"))

    def test_no_key_returns_empty_without_calling_the_model(self):
        r = AIRefiner(api_key="test")
        r.api_key = ""
        r.openrouter_api_key = ""            # force is_available False
        called = []
        r._raw_chat = lambda *a, **k: called.append(1) or "vercell"
        self.assertEqual([], r.suggest_mishearings("Vercel"))
        self.assertEqual([], called, "the model must not be called without a key")

    def test_blank_or_tiny_term_returns_empty(self):
        r = refiner_with("vercell")
        self.assertEqual([], r.suggest_mishearings(""))
        self.assertEqual([], r.suggest_mishearings("a"))


if __name__ == "__main__":
    unittest.main()
