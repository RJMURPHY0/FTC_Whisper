"""
AI text refinement using the Claude API.
Rewrites transcribed text in various styles (email, formal, casual, punctuation fix).
Requires ANTHROPIC_API_KEY environment variable or api_key in config.
"""

import os
from typing import Optional


REFINE_PROMPTS = {
    "punctuation": (
        "Fix the punctuation, capitalisation, and spacing in this transcribed speech. "
        "Correct obvious errors but keep the original wording and meaning intact. "
        "Return only the corrected text, nothing else."
    ),
    "email": (
        "Rewrite this transcribed speech as a clear, professional email body. "
        "Add proper punctuation, structure sentences properly, and use a polished tone. "
        "Return only the rewritten text, nothing else."
    ),
    "formal": (
        "Rewrite this transcribed speech in a formal, professional tone. "
        "Fix grammar and punctuation. Return only the rewritten text, nothing else."
    ),
    "casual": (
        "Rewrite this transcribed speech in a friendly, conversational tone. "
        "Keep it natural and fix any obvious transcription errors. "
        "Return only the rewritten text, nothing else."
    ),
    "concise": (
        "Rewrite this transcribed speech as concisely as possible while preserving the full meaning. "
        "Fix punctuation. Return only the rewritten text, nothing else."
    ),
    "prompt_optimiser": (
        "You are Lyra, a master-level AI prompt optimization specialist. "
        "Your mission: transform the user input below into a precision-crafted prompt that unlocks AI's full potential.\n\n"
        "Apply the 4-D methodology:\n"
        "1. DECONSTRUCT — extract core intent, key entities, context, output requirements\n"
        "2. DIAGNOSE — identify clarity gaps, ambiguity, missing specificity\n"
        "3. DEVELOP — select the optimal technique:\n"
        "   • Creative → multi-perspective + tone emphasis\n"
        "   • Technical → constraint-based + precision focus\n"
        "   • Educational → few-shot examples + clear structure\n"
        "   • Complex → chain-of-thought + systematic frameworks\n"
        "4. DELIVER — output only the final optimized prompt, ready to paste directly into any AI.\n\n"
        "Return only the optimized prompt text, nothing else. No explanation, no preamble."
    ),
}


class AIRefiner:
    """
    Refines transcribed text using the Claude API.

    Usage:
        refiner = AIRefiner(api_key="sk-ant-...")
        polished = refiner.refine("hello how are you doing today", mode="email")
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def refine(self, text: str, mode: str = "punctuation", custom_prompt: Optional[str] = None) -> str:
        """
        Refine text using Claude.

        Args:
            text: The raw transcribed text to refine
            mode: One of "punctuation", "email", "formal", "casual", "concise"
            custom_prompt: Override the system prompt entirely

        Returns:
            Refined text, or original text if API call fails
        """
        if not text.strip():
            return text

        if not self.is_available:
            print("[AIRefiner] No API key set — returning original text.")
            return text

        prompt = custom_prompt or REFINE_PROMPTS.get(mode, REFINE_PROMPTS["punctuation"])

        try:
            client = self._get_client()
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": f"{prompt}\n\n{text}",
                    }
                ],
            )
            result = message.content[0].text.strip()
            print(f"[AIRefiner] Refined ({mode}): '{result}'")
            return result

        except Exception as e:
            print(f"[AIRefiner] Error during refinement: {e}")
            return text
