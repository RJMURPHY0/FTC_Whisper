"""
AI text refinement using the Claude API.
Rewrites transcribed text in various styles (email, formal, casual, punctuation fix).
Requires ANTHROPIC_API_KEY environment variable or api_key in config.
"""

import os
import re
from typing import Optional

# A bracketed sign-off placeholder the model sometimes emits despite being told
# not to (e.g. "[Sender's Name]", "[Your Name]", "[Name]", "[Signature]"). Post-
# processing swaps it for the real name, or strips it when the name is unknown.
_NAME_PLACEHOLDER = re.compile(
    r"\[[^\]\n]*?(?:name|sender|sign[\s-]?off|signature)[^\]\n]*?\]", re.I)


# Added to every prompt — prevents AI from adding bullet points, dashes, or markdown
_NO_FORMAT = (
    " Write in plain prose only. "
    "Do not use bullet points, numbered lists, dashes, hyphens, asterisks, headers, "
    "or any markdown formatting. Output flowing sentences and paragraphs."
)

REFINE_PROMPTS = {
    # "Fix All" in the refine popup. A full grammar-checker pass in the Grammarly
    # mould: every mechanical error is in scope, but the writer's words and voice
    # are not. The explicit checklist beats a vague "proofread this" — the model
    # reliably fixes what it is named and skips what it is not.
    "punctuation": (
        "Act as a meticulous grammar and spelling checker, in the mould of "
        "Grammarly, for text that was DICTATED rather than typed. Correct every "
        "mechanical error you find:\n"
        "- Spelling, including misspelt names of well-known products and companies\n"
        "- Confused and misheard homophones: their/there/they're, your/you're, "
        "its/it's, to/too, of/have, affect/effect, then/than\n"
        "- Punctuation: missing or wrong full stops, commas, question marks, "
        "apostrophes (especially possessives and contractions), quotation marks, "
        "hyphens, colons and semicolons\n"
        "- Stray dictation artefacts such as ',.' or a doubled full stop\n"
        "- Sentence boundaries: a full stop dropped mid-sentence where the speaker "
        "paused to think, a needless capital left after it, two sentences run "
        "together with no punctuation, and sentence fragments\n"
        "- Capitalisation: sentence starts, proper nouns, the pronoun 'I'\n"
        "- Grammar: subject-verb agreement, verb tense consistency, plurals, "
        "missing articles and wrong prepositions\n"
        "- Immediate duplicated words from dictation stutters ('that that')\n\n"
        "STRICT LIMITS - this is a correction pass, not a rewrite:\n"
        "- Keep every word the speaker said, including casual phrasing and "
        "fillers like 'so yeah'. Do not tighten, shorten or improve the style\n"
        "- Do not reorder sentences, merge paragraphs, or add any new content\n"
        "- The only words you may delete are immediate stutter duplicates; the "
        "only words you may change are those grammar or spelling force you to\n"
        "- Preserve British spelling and the writer's regional usage\n"
        "- If a passage is already correct, leave it exactly as it is\n\n"
        "Return only the corrected text, nothing else." + _NO_FORMAT
    ),
    "email": (
        "Restructure this dictated speech into a well-written email. "
        "Keep the sender's natural tone and voice. Reorganise the content into short, "
        "clear paragraphs the way a good email reads: one topic per paragraph with a "
        "blank line between paragraphs. Fix all grammar, punctuation, and "
        "capitalisation. Remove false starts, repeated words, and filler. "
        "Keep every point the sender made. Do not invent content, pleasantries, or "
        "placeholders. A name at the very end of the speech is the sender signing "
        "off: keep it as the sign-off with its dictated phrase (for example "
        "'Cheers, Ryan'), never turn it into the greeting. Add a greeting line only "
        "if the sender addressed the recipient in the speech. "
        "Return only the email body, nothing else." + _NO_FORMAT
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
        "You are a transcription corrector for dictated speech. Fix ONLY these two "
        "kinds of error:\n"
        "1. Misheard words: homophones, similar-sounding words, garbled words that "
        "make no sense in context.\n"
        "2. Pause artefacts in punctuation and capitalisation: a full stop or comma "
        "inserted mid-sentence where the speaker merely paused to think "
        "(\"would actually be. Inserted\" should read \"would actually be inserted\"), "
        "a full stop followed by a needlessly capitalised word that continues the "
        "same sentence (\"near their first. Name. That can\" should read \"near "
        "their first name that can\" - lower-case the word and drop the stop), "
        "stray sequences like \",.\", two sentences run together with no punctuation "
        "between them, a wrong capital left after a removed full stop, and a "
        "paragraph break that clearly falls in the middle of a sentence.\n\n"
        "STRICT RULES:\n"
        "- Do NOT change any word that could be correct as-is\n"
        "- Do NOT add, remove, or reorder any words\n"
        "- Do NOT paraphrase, restyle, or improve wording\n"
        "- Keep paragraph breaks unless one clearly interrupts a sentence\n"
        "- When uncertain, leave the text exactly as-is\n\n"
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

    # Verified live on OpenRouter (July 2026). gemini-2.0-flash-001 was
    # DELISTED — requests to it fail, which silently killed the whole
    # OpenRouter path. Fallbacks are tried in-request by OpenRouter itself.
    DEFAULT_OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"
    OPENROUTER_FALLBACK_MODELS = [
        "meta-llama/llama-4-scout",
        "anthropic/claude-haiku-4.5",
    ]

    def __init__(
        self,
        api_key: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
        openrouter_model: str = DEFAULT_OPENROUTER_MODEL,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.openrouter_api_key = openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.openrouter_model = openrouter_model or self.DEFAULT_OPENROUTER_MODEL
        self._client = None
        self._openrouter_client = None

    def update_api_key(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        self._client = None  # force re-init with new key

    def update_openrouter_key(self, key: str) -> None:
        self.openrouter_api_key = key.strip()
        self._client = None  # force re-init with new key
        self._openrouter_client = None

    @property
    def is_available(self) -> bool:
        return bool(self.openrouter_api_key or self.api_key)

    _STYLE_SYSTEM_PROMPT = (
        "You are a text refinement assistant. Write in plain prose only. "
        "No bullet points, numbered lists, dashes at line starts, asterisks, markdown, or headers. "
        "Use clear, simple language. Keep sentences short and direct. Use active voice. "
        "Avoid em dashes, semicolons, filler words, and cliches. "
        "Write as a human would: natural, direct, easy to read. "
        "Return only the refined text, nothing else."
    )
    # context_fix must NOT get the style prompt — "keep sentences short",
    # "avoid semicolons" etc. directly contradict "change nothing but misheard
    # words" and push the model to rewrite.
    _CORRECTOR_SYSTEM_PROMPT = (
        "You are a transcription corrector. You fix misheard words and the "
        "punctuation artefacts dictation pauses leave behind. "
        "You never rewrite, rephrase, or restyle. "
        "Return only the corrected text, nothing else."
    )

    @staticmethod
    def _max_tokens_for(text: str) -> int:
        # 1024 silently truncated long dictations (~750+ words). Scale with
        # input; generous ceiling since output ≈ input length for our modes.
        return min(8192, max(1024, len(text.split()) * 4))

    def _system_prompt_for(self, mode: str) -> str:
        return self._CORRECTOR_SYSTEM_PROMPT if mode == "context_fix" else self._STYLE_SYSTEM_PROMPT

    def _refine_via_openrouter(self, text: str, prompt: str, mode: str) -> str:
        """Call OpenRouter using the openai-compatible SDK. Lazy import so missing package doesn't crash."""
        try:
            import openai  # type: ignore
        except ImportError:
            print("[AIRefiner] openai package not installed — falling back to Anthropic")
            return ""

        if self._openrouter_client is None:
            self._openrouter_client = openai.OpenAI(
                api_key=self.openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
                timeout=20.0,
                max_retries=1,
            )
        response = self._openrouter_client.chat.completions.create(
            model=self.openrouter_model,
            max_tokens=self._max_tokens_for(text),
            temperature=0,
            messages=[
                {"role": "system", "content": self._system_prompt_for(mode)},
                {"role": "user", "content": f"{prompt}\n\n{text}"},
            ],
            # OpenRouter-native fallback: tries these in order if the primary
            # model errors or is rate-limited — one request, no extra latency.
            extra_body={"models": self.OPENROUTER_FALLBACK_MODELS},
        )
        return response.choices[0].message.content.strip()

    def _get_anthropic_client(self):
        if self._client is None:
            import anthropic
            # SDK defaults are 600s timeout + 2 retries ≈ minutes of hang on a
            # bad connection; a transcript fix is worthless after ~20s.
            self._client = anthropic.Anthropic(
                api_key=self.api_key, timeout=20.0, max_retries=1
            )
        return self._client

    def refine(self, text: str, mode: str = "punctuation", custom_prompt: Optional[str] = None,
               sender_name: Optional[str] = None) -> str:
        """
        Refine text using Claude or OpenRouter.

        Args:
            text: The raw transcribed text to refine
            mode: One of "punctuation", "email", "formal", "casual", "concise"
            custom_prompt: Override the system prompt entirely
            sender_name: The sender's real name (email mode) — signs the email off
                with it instead of a "[Sender's Name]" placeholder.

        Returns:
            Refined text, or original text if API call fails
        """
        if not text.strip():
            return text

        if not self.is_available:
            print("[AIRefiner] No API key set — returning original text.")
            return text

        prompt = custom_prompt or REFINE_PROMPTS.get(mode, REFINE_PROMPTS["punctuation"])

        # Email sign-off: hand the model the sender's real name so a bare
        # sign-off ("Thanks,") gets a name and no placeholder is invented.
        name = (sender_name or "").strip()
        if mode == "email" and not custom_prompt and name:
            prompt += (
                f" The sender's name is '{name}'. If the speech ends with a sign-off "
                f"phrase that has no name after it (for example 'Thanks,' or 'Cheers,'), "
                f"put '{name}' on the next line as the signature. Never output a "
                f"placeholder such as [Your Name] or [Sender's Name] — use the real name."
            )

        # OpenRouter takes priority over Anthropic direct
        if self.openrouter_api_key:
            try:
                result = self._refine_via_openrouter(text, prompt, mode)
                if result:
                    result = self._apply_sender_name(result, mode, name)
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
                max_tokens=self._max_tokens_for(text),
                system=self._system_prompt_for(mode),
                messages=[
                    {
                        "role": "user",
                        "content": f"{prompt}\n\n{text}",
                    }
                ],
            )
            result = message.content[0].text.strip()
            result = self._apply_sender_name(result, mode, name)
            print(f"[AIRefiner] Refined via Anthropic ({mode}): '{result}'")
            return result

        except Exception as e:
            print(f"[AIRefiner] Error during refinement: {e}")
            return text

    def _raw_chat(self, system: str, user: str, max_tokens: int = 256) -> str:
        """A plain system+user chat turn, OpenRouter first then Anthropic, same
        timeouts/fallbacks as refine(). Returns the reply text, or "" on no key or
        error. Used off the dictation hot path only."""
        if self.openrouter_api_key:
            try:
                import openai  # type: ignore
                if self._openrouter_client is None:
                    self._openrouter_client = openai.OpenAI(
                        api_key=self.openrouter_api_key,
                        base_url="https://openrouter.ai/api/v1",
                        timeout=20.0, max_retries=1)
                resp = self._openrouter_client.chat.completions.create(
                    model=self.openrouter_model, max_tokens=max_tokens,
                    temperature=0.2,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    extra_body={"models": self.OPENROUTER_FALLBACK_MODELS})
                out = (resp.choices[0].message.content or "").strip()
                if out:
                    return out
            except Exception as e:
                print(f"[AIRefiner] OpenRouter chat failed: {e}")
                if not self.api_key:
                    return ""
        if not self.api_key:
            return ""
        try:
            client = self._get_anthropic_client()
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=max_tokens,
                system=system, messages=[{"role": "user", "content": user}])
            return (msg.content[0].text or "").strip()
        except Exception as e:
            print(f"[AIRefiner] Anthropic chat failed: {e}")
            return ""

    def suggest_mishearings(self, term: str, limit: int = 8) -> list:
        """How a speech-to-text system is likely to mangle `term`, most likely
        first, lowercased. Best-effort: [] with no key, on error, or when nothing
        usable comes back. This is how a term picks up its harder mishearings (a
        brand like "Vercel" heard as "the cell") that a phonetic rule can't safely
        guess — the user reviews the list before it is saved, so an odd suggestion
        costs nothing. Never called on the dictation path."""
        term = (term or "").strip()
        if len(term) < 2 or not self.is_available:
            return []
        system = ("You list likely speech-to-text mishearings of a given word or "
                  "phrase. Output only the mishearings, one per line, all lowercase, "
                  "no numbering, quotes, or explanation.")
        user = (f'An English speech-to-text system keeps mishearing the term '
                f'"{term}". List up to {limit} ways it is most likely to be '
                f'transcribed wrongly, most likely first. Include plausible splits '
                f'into ordinary words (for example the brand "Vercel" often comes '
                f'out as "the cell"). One per line, lowercase. Do not include the '
                f'correct term "{term}" itself.')
        raw = self._raw_chat(system, user, max_tokens=256)
        if not raw:
            return []
        out, seen = [], set()
        low_term = term.lower()
        for line in raw.splitlines():
            # Strip any bullet/number/quote formatting the model added anyway.
            cand = re.sub(r'^[\s\-\*•\d\.\)\"\'`]+', "", line).strip().strip('"\'')
            cand = " ".join(cand.split()).lower()
            # Reject the term itself, blanks, overlong lines (a stray sentence),
            # and near-duplicates. Length/validity is enforced again on save.
            if (not cand or cand == low_term or cand in seen
                    or len(cand) < 3 or len(cand.split()) > 5):
                continue
            seen.add(cand)
            out.append(cand)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _apply_sender_name(text: str, mode: str, name: str) -> str:
        """Email post-process: swap any '[Sender's Name]'-style placeholder for the
        real name, or strip it when the name is unknown so the literal bracket text
        never reaches the user. No-op for every other mode."""
        if mode != "email" or not _NAME_PLACEHOLDER.search(text):
            return text
        out = _NAME_PLACEHOLDER.sub(name, text)
        if not name:
            # Removing the placeholder can leave a trailing space or an empty
            # signature line ("Thanks, " / "Thanks,\n\n"); tidy both.
            out = re.sub(r"[ \t]+(\n|$)", r"\1", out)
            out = re.sub(r"\n{3,}", "\n\n", out).rstrip()
        return out

    def context_fix(self, text: str) -> str:
        """Fix misheard words using sentence context. Rejects the result if word
        count changes beyond tolerance OR too many words were substituted —
        a count-only guard silently accepted same-length rewrites."""
        if len(text.split()) < 4:
            return text
        result = self.refine(text, mode="context_fix")
        if result == text:
            return text
        # Guard against the LLM adding/removing content, but tolerate small
        # count shifts from legitimate corrections (contractions like
        # "do not"->"don't", hyphenation, split/merged compounds). A strict
        # equality check silently rejected valid fixes.
        orig_words = text.split()
        new_words = result.split()
        orig_n = len(orig_words)
        new_n = len(new_words)
        tolerance = max(1, round(orig_n * 0.1))
        if abs(new_n - orig_n) > tolerance:
            print(f"[AIRefiner] context_fix rejected: word count changed {orig_n} -> {new_n} (tolerance {tolerance}), using original")
            return text
        # Substitution guard: the count check alone lets an equal-length rewrite
        # through untouched. context_fix should touch a few misheard words, never
        # rework the sentence — reject when more than ~1/3 of words changed.
        if new_n == orig_n:
            def _bare(w: str) -> str:
                return w.strip(".,!?;:\"'").lower()
            changed = sum(1 for a, b in zip(orig_words, new_words) if _bare(a) != _bare(b))
            max_changed = max(1, orig_n // 3)
            if changed > max_changed:
                print(f"[AIRefiner] context_fix rejected: {changed}/{orig_n} words substituted (max {max_changed}), using original")
                return text
        return result
