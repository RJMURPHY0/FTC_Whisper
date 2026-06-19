"""
AI text refinement using the Claude API.
Rewrites transcribed text in various styles (email, formal, casual, punctuation fix).
Requires ANTHROPIC_API_KEY environment variable or api_key in config.
"""

import os
from typing import Optional


# Added to every prompt — prevents AI from adding bullet points, dashes, or markdown
_NO_FORMAT = (
    " Write in plain prose only. "
    "Do not use bullet points, numbered lists, dashes, hyphens, asterisks, headers, "
    "or any markdown formatting. Output flowing sentences and paragraphs."
)

REFINE_PROMPTS = {
    "punctuation": (
        "Fix the punctuation, capitalisation, spacing, grammar, and spelling in this transcribed speech. "
        "Correct all errors but keep the original wording and meaning intact. "
        "Return only the corrected text, nothing else." + _NO_FORMAT
    ),
    "email": (
        "Rewrite this transcribed speech as a clear, professional email body. "
        "Add proper punctuation, structure sentences properly, and use a polished tone. "
        "Return only the rewritten email body, nothing else." + _NO_FORMAT
    ),
    "formal": (
        "Rewrite this transcribed speech in a formal, professional tone. "
        "Fix grammar and punctuation. Return only the rewritten text, nothing else." + _NO_FORMAT
    ),
    "casual": (
        "Rewrite this transcribed speech in a friendly, conversational tone. "
        "Keep it natural and fix any obvious transcription errors. "
        "Return only the rewritten text, nothing else." + _NO_FORMAT
    ),
    "concise": (
        "Rewrite this transcribed speech using short, simple sentences. "
        "Keep every point and every piece of information from the original. Do not remove anything useful. "
        "Cut filler words and repetition only. Fix punctuation. "
        "Make it easy to read and easy to understand at a glance. "
        "Return only the rewritten text, nothing else." + _NO_FORMAT
    ),
    "prompt_optimiser": (
        "You are a prompt optimisation specialist. "
        "Transform the text below into a clear, precise prompt ready to paste into any AI. "
        "Preserve the intent exactly. Return only the optimised prompt text, nothing else." + _NO_FORMAT
    ),
    "context_fix": (
        "You are a transcription corrector. Fix ONLY words that were misheard or garbled "
        "by speech recognition — homophones, similar-sounding words, garbled words that "
        "make no sense in context.\n\n"
        "STRICT RULES:\n"
        "- Do NOT change any word that could be correct as-is\n"
        "- Do NOT add, remove, or reorder any words\n"
        "- Do NOT change punctuation, grammar, or sentence structure\n"
        "- Do NOT paraphrase or improve wording\n"
        "- When uncertain, leave the word exactly as-is\n\n"
        "Return ONLY the corrected text. If nothing needs fixing, return it unchanged."
    ),
}


class AIRefiner:
    """
    Refines transcribed text using the Claude API or OpenRouter.

    Priority: OpenRouter (if key set) > Anthropic direct (if key set) > unavailable.

    Usage:
        refiner = AIRefiner(api_key="sk-ant-...", openrouter_api_key="sk-or-...")
        polished = refiner.refine("hello how are you doing today", mode="email")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
        openrouter_model: str = "google/gemini-2.0-flash-001",
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.openrouter_api_key = openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.openrouter_model = openrouter_model
        self._client = None

    def update_api_key(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        self._client = None  # force re-init with new key

    def update_openrouter_key(self, key: str) -> None:
        self.openrouter_api_key = key.strip()
        self._client = None  # force re-init with new key

    @property
    def is_available(self) -> bool:
        return bool(self.openrouter_api_key or self.api_key)

    def _refine_via_openrouter(self, text: str, prompt: str) -> str:
        """Call OpenRouter using the openai-compatible SDK. Lazy import so missing package doesn't crash."""
        try:
            import openai  # type: ignore
        except ImportError:
            print("[AIRefiner] openai package not installed — falling back to Anthropic")
            return ""

        client = openai.OpenAI(
            api_key=self.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        response = client.chat.completions.create(
            model=self.openrouter_model,
            max_tokens=1024,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a text refinement assistant. Write in plain prose only. "
                        "No bullet points, numbered lists, dashes at line starts, asterisks, markdown, or headers. "
                        "Use clear, simple language. Keep sentences short and direct. Use active voice. "
                        "Avoid em dashes, semicolons, filler words, and cliches. "
                        "Write as a human would: natural, direct, easy to read. "
                        "Return only the refined text, nothing else."
                    ),
                },
                {"role": "user", "content": f"{prompt}\n\n{text}"},
            ],
        )
        return response.choices[0].message.content.strip()

    def _get_anthropic_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def refine(self, text: str, mode: str = "punctuation", custom_prompt: Optional[str] = None) -> str:
        """
        Refine text using Claude or OpenRouter.

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

        # OpenRouter takes priority over Anthropic direct
        if self.openrouter_api_key:
            try:
                result = self._refine_via_openrouter(text, prompt)
                if result:
                    print(f"[AIRefiner] Refined via OpenRouter ({mode}): '{result}'")
                    return result
                # Empty result (e.g. openai not installed) — fall through to Anthropic
            except Exception as e:
                print(f"[AIRefiner] OpenRouter error during refinement: {e}")
                # Fall through to Anthropic if key is also set
                if not self.api_key:
                    return text

        # Anthropic direct path
        if not self.api_key:
            return text

        try:
            client = self._get_anthropic_client()
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=(
                    "You are a text refinement assistant. "
                    "Write in plain prose only. No bullet points, numbered lists, dashes at line starts, asterisks, markdown, or headers. "
                    "Use clear, simple language. Keep sentences short and direct. Use active voice. "
                    "Avoid em dashes, semicolons, filler words, and cliches. "
                    "Write as a human would: natural, direct, easy to read. "
                    "Return only the refined text, nothing else."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": f"{prompt}\n\n{text}",
                    }
                ],
            )
            result = message.content[0].text.strip()
            print(f"[AIRefiner] Refined via Anthropic ({mode}): '{result}'")
            return result

        except Exception as e:
            print(f"[AIRefiner] Error during refinement: {e}")
            return text

    def context_fix(self, text: str) -> str:
        """Fix misheard words using sentence context. Rejects result if word count changes."""
        if len(text.split()) < 4:
            return text
        result = self.refine(text, mode="context_fix")
        if result == text:
            return text
        if len(result.split()) != len(text.split()):
            print(f"[AIRefiner] context_fix rejected: word count changed {len(text.split())} -> {len(result.split())}, using original")
            return text
        return result
