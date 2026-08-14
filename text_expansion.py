"""Custom vocabulary corrections and snippet expansion.

Two user-owned libraries that rewrite a finished transcript before it is
injected:

  Vocabulary — a term the user cares about ("Pipedrive"), plus the ways the
    recogniser tends to mishear it ("pipe drive"). The term itself is fed to
    the engines as a hotword so it is recognised correctly in the first place;
    the mishearings are a safety net for when it still slips.

  Snippets — a short spoken trigger ("my email") that expands into a block of
    text (the address itself).

Both run at the single post-processing point in app.py, alongside the spoken
symbol commands, so injection, the popup, history logging and the upgrade
passes all see identical text.

Design rules, because this rewrites words the user actually said:

  * Whole word / whole phrase only. A substring match would turn "scrapes"
    into "sCRMpes" the first time somebody adds "cra" as a mishearing.
  * One single pass over the text, built from an alternation of every pattern.
    Replacing in a loop lets an inserted replacement be re-matched by a later
    rule, so "my email" -> an address containing "at" -> expanded again. A
    single pass structurally cannot cascade.
  * Longest pattern first, so "my work email" wins over "my email".
  * Case-insensitive matching, and the replacement carries the casing the user
    typed — they wrote "Pipedrive", they get "Pipedrive".

Pure functions over plain dicts: no Tk, no config object, no network.
"""

import re

# A mishearing shorter than this is refused. Two-character patterns ("in",
# "to", "us") appear inside ordinary speech constantly, and a whole-word match
# does not save you: "us" is a real word, so a rule mapping it to "US" would
# rewrite half of every dictation.
MIN_PATTERN_LEN = 3

# Refused as snippet triggers outright. Everything here is a word somebody
# says by accident within seconds of turning the feature on.
_RESERVED_TRIGGERS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "have", "he", "her", "his", "i", "if", "in", "is", "it", "its", "me",
    "my", "no", "not", "of", "on", "or", "our", "she", "so", "that", "the",
    "their", "them", "then", "there", "they", "this", "to", "up", "was",
    "we", "were", "what", "when", "which", "who", "will", "with", "you",
    "your", "yes", "ok", "okay", "please", "thanks", "thank you",
}


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _pattern_for(phrase: str) -> str:
    """Regex matching `phrase` as a whole word/phrase, whitespace-tolerant.

    `\\b` is wrong at both ends here: a pattern may legitimately start or end
    with punctuation ("e.g.", "@ftc"), and `\\b` before a non-word character
    asserts the opposite of what is wanted. Lookarounds conditioned on the
    pattern's own edges are correct in every case.
    """
    words = phrase.split()
    core = r"\s+".join(re.escape(w) for w in words)
    lead = r"(?<!\w)" if words and _is_word_char(phrase[0]) else ""
    trail = r"(?!\w)" if words and _is_word_char(phrase[-1]) else ""
    return f"{lead}{core}{trail}"


def _live(entries) -> list:
    """Entries that are present, not soft-deleted, and shaped like dicts."""
    out = []
    for e in entries or []:
        if isinstance(e, dict) and not e.get("deleted"):
            out.append(e)
    return out


def _compile(pairs):
    """One case-insensitive alternation over (pattern, replacement) pairs.

    Longest pattern first so a longer phrase is never shadowed by a shorter one
    it contains. Returns (regex, {group_name: replacement}) or (None, {}).
    """
    pairs = [(p, r) for p, r in pairs if p]
    if not pairs:
        return None, {}
    pairs.sort(key=lambda pr: len(pr[0]), reverse=True)
    parts, table = [], {}
    for i, (phrase, replacement) in enumerate(pairs):
        name = f"m{i}"
        table[name] = replacement
        parts.append(f"(?P<{name}>{_pattern_for(phrase)})")
    try:
        return re.compile("|".join(parts), re.IGNORECASE), table
    except re.error:
        return None, {}


def _starts_sentence(text: str, pos: int) -> bool:
    """True when the match at `pos` opens the text or follows . ! ? or a newline."""
    i = pos - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    if i < 0:
        return True
    return text[i] in ".!?\n"


def _cased(replacement: str, text: str, start: int) -> str:
    """The replacement, capitalised only when it is entirely lower case AND it
    opens a sentence. Anything the user typed with deliberate casing
    ("iPhone", "FTC", "Pipedrive") is left exactly as they typed it — that
    casing is the whole reason the entry exists."""
    if not replacement or not replacement[0].isalpha():
        return replacement
    if replacement != replacement.lower():
        return replacement
    if _starts_sentence(text, start):
        return replacement[0].upper() + replacement[1:]
    return replacement


def _substitute(text: str, pairs, recase: bool = True) -> str:
    """Single-pass replacement. `recase` capitalises an all-lower-case
    replacement that opens a sentence — right for a vocabulary term, wrong for
    a snippet body, which is a verbatim block the user typed out. Recasing a
    body would turn a snippet holding an email address into "Ryan@ftc.co.uk"
    whenever it happened to land at the start of a sentence."""
    rx, table = _compile(pairs)
    if rx is None or not text:
        return text

    def _repl(m):
        replacement = table.get(m.lastgroup, m.group(0))
        if not recase:
            return replacement
        return _cased(replacement, text, m.start())

    return rx.sub(_repl, text)


# ── Vocabulary ───────────────────────────────────────────────────────────────

def vocabulary_hotwords(entries) -> str:
    """Comma-joined terms, for the recogniser's hotword list and the LLM prompt.

    Only the TERMS — never the mishearings. Feeding "pipe drive" to the engine
    as a hotword would bias it towards producing the very output the entry
    exists to correct.
    """
    terms = []
    seen = set()
    for e in _live(entries):
        term = (e.get("term") or "").strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return ", ".join(terms)


def apply_vocabulary(text: str, entries) -> str:
    """Replace each entry's known mishearings with its term."""
    if not text:
        return text
    pairs = []
    for e in _live(entries):
        term = (e.get("term") or "").strip()
        if not term:
            continue
        for variant in e.get("sounds_like") or []:
            variant = " ".join((variant or "").split())
            # A variant identical to the term is a no-op that only costs a
            # regex branch; one shorter than the floor is unsafe.
            if len(variant) < MIN_PATTERN_LEN or variant.lower() == term.lower():
                continue
            pairs.append((variant, term))
    return _substitute(text, pairs)


# ── Snippets ─────────────────────────────────────────────────────────────────

def apply_snippets(text: str, entries) -> str:
    """Expand each entry's trigger phrase into its body."""
    if not text:
        return text
    pairs = []
    for e in _live(entries):
        trigger = " ".join((e.get("trigger") or "").split())
        body = e.get("body") or ""
        if len(trigger) < MIN_PATTERN_LEN or not body:
            continue
        pairs.append((trigger, body))
    return _substitute(text, pairs, recase=False)


# ── Validation, shared by the editors ────────────────────────────────────────

def validate_sounds_like(variant: str, term: str = "") -> tuple:
    """(ok, reason). The reason is shown to the user — never a silent drop."""
    variant = " ".join((variant or "").split())
    if not variant:
        return False, "Enter what it sounds like."
    if len(variant) < MIN_PATTERN_LEN:
        return False, (f"Too short — needs {MIN_PATTERN_LEN} characters or more, "
                       "or it will match ordinary speech.")
    if term and variant.lower() == term.strip().lower():
        return False, "That's the same as the word itself."
    return True, ""


def validate_trigger(trigger: str) -> tuple:
    """(ok, reason). Rejects triggers that would fire during normal dictation."""
    trigger = " ".join((trigger or "").split())
    if not trigger:
        return False, "Enter a trigger phrase."
    if len(trigger) < MIN_PATTERN_LEN:
        return False, (f"Too short — needs {MIN_PATTERN_LEN} characters or more, "
                       "or it will fire while you talk.")
    if trigger.lower() in _RESERVED_TRIGGERS:
        return False, (f"“{trigger}” is too common — it would expand "
                       "every time you said it. Try a longer phrase.")
    return True, ""
