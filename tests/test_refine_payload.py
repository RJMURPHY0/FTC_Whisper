"""Refine-selection payload contract.

The Alt+R "refine selection" flow captures the user's highlighted text and hands
it to AIRefiner.refine(). Two invariants keep it from producing the "please
provide the text to refine" hallucination that shipped when the capture came
back empty:

  1. When source text IS present it MUST appear in the model payload, so the
     model refines the real selection rather than acting on the instruction
     alone.
  2. Empty / whitespace-only source text MUST short-circuit and never reach the
     API. An empty payload is exactly what makes the model ask for the text.

Pure stubs, no network. Safe to run anywhere.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_refiner import AIRefiner


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        # Record exactly what would have gone to the model.
        self._sink["messages"] = kwargs.get("messages")
        return _FakeResponse("REWRITTEN")


class _FakeChat:
    def __init__(self, sink):
        self.completions = _FakeCompletions(sink)


class _FakeOpenRouterClient:
    def __init__(self, sink):
        self.chat = _FakeChat(sink)


class RefinePayloadTests(unittest.TestCase):
    def _refiner(self, sink):
        r = AIRefiner(openrouter_api_key="sk-or-test")
        r._openrouter_client = _FakeOpenRouterClient(sink)
        return r

    def test_selected_text_is_in_the_payload(self):
        sink = {}
        r = self._refiner(sink)
        selection = "the about section has a fade for each individual colour"
        out = r.refine(selection, custom_prompt="Make this better.")
        self.assertEqual(out, "REWRITTEN")
        user_msg = sink["messages"][-1]["content"]
        self.assertIn(selection, user_msg)
        self.assertIn("Make this better.", user_msg)

    def test_empty_text_never_calls_the_api(self):
        sink = {}
        r = self._refiner(sink)
        out = r.refine("   ", custom_prompt="Make this better.")
        # Short-circuits: returns the (empty) input and no request was built.
        self.assertEqual(out, "   ")
        self.assertNotIn("messages", sink)

    def test_mode_refine_also_carries_text(self):
        sink = {}
        r = self._refiner(sink)
        out = r.refine("fix my punctuation please", mode="punctuation")
        self.assertEqual(out, "REWRITTEN")
        self.assertIn("fix my punctuation please", sink["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
