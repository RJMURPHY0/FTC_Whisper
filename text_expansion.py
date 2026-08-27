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


# ── Fuzzy vocabulary (phonetic safety net) ────────────────────────────────────
#
# The exact `sounds_like` list above only catches mishearings the user has typed
# out. This pass catches the CLOSE ones they haven't — "vercell", "ver cell",
# "versel", "versaille" -> "Vercel" — automatically, without the user listing
# every variant.
#
# It deliberately does NOT try to catch common-word mishearings like "the cell":
# nothing in the TEXT distinguishes "click the cell" (keep) from a mis-heard
# "Vercel" (fix), so an automatic rule loose enough to catch it would also rewrite
# ordinary English. Those stay the job of an explicit `sounds_like` entry (which
# the vocab editor can suggest). This is the same division of labour Wispr Flow /
# Superwhisper use: automatic hints for the close cases, explicit rules for the rest.
#
# Speed: additive and bounded. Skips entirely when the account has no vocabulary,
# computes each term's phonetic code once, and each transcript word position is
# encoded at most three times — a fraction of a millisecond on a real dictation
# (guarded by a timing test). It runs on the already-final transcript, never on
# the transcription or injection path, so stop-to-text latency is untouched.
#
# Safety gate (all must hold, so it cannot put words in the user's mouth):
#   * Double Metaphone code of the span matches the term's — the primary firewall
#     (this is what rejects "excel"=AKSL and "the cell"=0SL against "Vercel"=FRSL).
#   * Jaro-Winkler similarity >= FUZZY_JW.
#   * Length ratio within FUZZY_LEN_LO..FUZZY_LEN_HI.
#   * The span is NOT made up entirely of ordinary English words — a second
#     firewall for the rare case a term's phonetic code collides with a stock phrase.
#   * Term is at least FUZZY_MIN_TERM_LEN chars (shorter terms are collision-prone
#     and stay with the exact path only).

FUZZY_MIN_TERM_LEN = 4      # shorter terms rely on the exact sounds_like path
FUZZY_JW = 0.70             # Jaro-Winkler floor (secondary to the metaphone gate)
FUZZY_LEN_LO = 0.5          # span/term length ratio bounds
FUZZY_LEN_HI = 1.7
FUZZY_MAX_WINDOW = 3        # a mishearing spans at most this many words

# ── Managed (CRM-derived) terms: a stricter tier ─────────────────────────────
#
# An entry the user typed is one they opted into: they accepted it would rewrite
# near-misses, and they can delete it when it misbehaves. A term synced from a
# CRM has been reviewed by nobody, and there can be hundreds of them, so it has
# to clear a higher bar before it may change what someone said.
#
# The bar applies to SINGLE-WORD managed terms only, because that is where the
# whole risk lives: real CRM data holds names like "Michaels", "Wincanton" and
# "Storm" that sit one phonetic slip away from ordinary speech. A multi-word
# managed term ("Bright Link Solutions") is already heavily constrained — the
# span has to match across two or three consecutive words — so restricting the
# window for those would only break the names that are safest to correct.
MANAGED_MIN_TERM_LEN = 5    # single-word managed terms; shorter ones are dropped
MANAGED_JW = 0.86           # vs FUZZY_JW 0.70 — near-identical or nothing
MANAGED_LEN_LO = 0.7        # tighter than FUZZY_LEN_* so a short word cannot
MANAGED_LEN_HI = 1.4        # reach a long company name

# Ordinary English words. A span made entirely of these is never rewritten, even
# on a phonetic-code collision. Not exhaustive by design — the metaphone gate is
# the primary firewall; this is the belt-and-braces list of words a fuzzy rule is
# most likely to trip over (function words plus the short, common nouns/verbs that
# collide most: "cell", "bell", "sell", "sale", "shares"…).
_COMMON_WORDS = frozenset("""
a about after all also am an and any are as at back be because been before being
but by call came can come could day did do does doing done down each even every
few first for from get give go going good got had has have he her here him his how
i if in into is it its just keep know last less let like little long look made make
man many may me might more most much must my never new next no not now of off on once
one only or other our out over own put ran said same say see she should side so some
such take than that the their them then there these they thing think this those
through time to too two up us use very was way we well went were what when where which
while who why will with would year you your
cell cells bell bells sell sells sale sales share shares mail mails male sell seller
tell well fell dwell shell smell spell swell dell hell fella
""".split())


def _jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    l1, l2 = len(s1), len(s2)
    if l1 == 0 or l2 == 0:
        return 0.0
    reach = max(l1, l2) // 2 - 1
    if reach < 0:
        reach = 0
    m1 = [False] * l1
    m2 = [False] * l2
    matches = 0
    for i in range(l1):
        lo = max(0, i - reach)
        hi = min(i + reach + 1, l2)
        for j in range(lo, hi):
            if m2[j] or s1[i] != s2[j]:
                continue
            m1[i] = m2[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    t = 0
    k = 0
    for i in range(l1):
        if not m1[i]:
            continue
        while not m2[k]:
            k += 1
        if s1[i] != s2[k]:
            t += 1
        k += 1
    t /= 2
    return (matches / l1 + matches / l2 + (matches - t) / matches) / 3.0


def _jaro_winkler(s1: str, s2: str, scale: float = 0.1) -> float:
    j = _jaro(s1, s2)
    prefix = 0
    for a, b in zip(s1, s2):
        if a != b:
            break
        prefix += 1
        if prefix == 4:
            break
    return j + prefix * scale * (1.0 - j)


_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def _dm():
    """The Double Metaphone encoder, or None if the (optional) package is absent.

    A missing encoder makes the fuzzy pass a clean no-op — the exact path and every
    other feature are unaffected — so this can never break a build or a dictation."""
    try:
        from metaphone import doublemetaphone
        return doublemetaphone
    except Exception:
        return None


def _phonetic_match(term_codes: tuple, cand_codes: tuple) -> bool:
    """True when the term's primary code lines up with either candidate code, or
    the candidate's primary matches the term's secondary. Empty codes never match."""
    tp, ts = term_codes
    cp, cs = cand_codes
    if not tp:
        return False
    return tp == cp or tp == cs or (bool(cp) and cp == ts)


# Legal and trading suffixes. A CRM holds the REGISTERED name ("Fosseway Freight
# Ltd"); people say the trading name ("Fosseway Freight"). Without a stripped
# variant the spoken form matches nothing, because the suffix changes the
# phonetic code — and matching it to the full term would be worse, since it
# would append a suffix to words the user never said.
_LEGAL_SUFFIXES = (
    "ltd", "limited", "llp", "llc", "plc", "inc", "incorporated", "corp",
    "corporation", "co", "company", "gmbh", "bv", "nv", "sa", "ag", "pty",
    "group", "holdings", "international",
)


def managed_variants(raw: str) -> list:
    """The forms of a CRM name worth correcting towards, longest first.

    Always the name as stored. Plus the name with trailing legal suffixes
    removed, when that leaves something substantial enough to be worth matching
    (2+ words, or one word of real length) — "Fosseway Freight Ltd" also yields
    "Fosseway Freight", but "Ltd" alone never becomes a term.

    A trailing descriptor after a colon is dropped too: real rows look like
    "Boast International Ltd: Uk & International Freight Forwarder", and nobody
    dictates the tagline.
    """
    name = " ".join((raw or "").split())
    if not name:
        return []
    out = []
    seen = set()

    def _add(v: str) -> None:
        v = " ".join(v.split()).strip(" ,;:-&")
        low = v.lower()
        if not v or low in seen:
            return
        # One word must be long enough to be distinctive; two or more words are
        # constrained enough by the span match.
        if " " not in v and len(v) < MANAGED_MIN_TERM_LEN:
            return
        seen.add(low)
        out.append(v)

    _add(name)
    head = name.split(":", 1)[0]
    _add(head)
    words = head.split()
    while len(words) > 1 and words[-1].lower().strip(".,") in _LEGAL_SUFFIXES:
        words = words[:-1]
        _add(" ".join(words))
    return out


def apply_vocabulary_fuzzy(text: str, entries) -> str:
    """Phonetic safety net: rewrite spans that SOUND like a vocabulary term but
    were not listed as an explicit mishearing. Conservative by construction (see
    the gate above). No-op without a metaphone encoder, without vocabulary, or on
    empty text."""
    if not text:
        return text
    dm = _dm()
    if dm is None:
        return text

    # Precompute each term's phonetic code once. Dedup case-insensitively; keep
    # the user's casing for the replacement.
    terms = []
    seen = set()
    for e in _live(entries):
        term = (e.get("term") or "").strip()
        low = term.lower()
        # `managed` marks a term synced from a CRM rather than typed by the
        # user. Only a SINGLE-WORD managed term takes the strict tier — see the
        # constants above for why multi-word ones do not need it.
        strict = bool(e.get("managed")) and " " not in term
        min_len = MANAGED_MIN_TERM_LEN if strict else FUZZY_MIN_TERM_LEN
        if len(term) < min_len or low in seen:
            continue
        # A single-word managed term that IS ordinary English is refused
        # outright. The span check further down only asks whether the text being
        # replaced is common, which does not help here: a real CRM holds
        # companies called "Shell" and "Next", and "shall" is not itself a
        # common-listed word, so without this it would clear the strict floor
        # (jw("shall","shell") ~= 0.88) and rewrite ordinary speech. A term the
        # user typed by hand is exempt: they chose it, so they meant it.
        if strict and low in _COMMON_WORDS:
            continue
        # Callers put the user's own entries first, so a hand-typed term always
        # claims the name before a managed one with the same spelling can.
        seen.add(low)
        try:
            codes = dm(term)
        except Exception:
            continue
        if codes and codes[0]:
            terms.append((term, low, codes, strict))
    if not terms:
        return text

    tokens = list(_WORD_RE.finditer(text))
    if not tokens:
        return text

    # The costly part is Double Metaphone, so pay it once per word and prefilter
    # cheaply. Every term's primary phoneme starts with one of these characters;
    # a window whose first word does not can never match, so it skips the encode.
    term_first = {c[0] for _t, _l, codes, _s in terms for c in codes if c}
    word_dm = []
    for t in tokens:
        try:
            word_dm.append(dm(t.group(0)))
        except Exception:
            word_dm.append(("", ""))
    word_low = [t.group(0).lower() for t in tokens]

    # Collect non-overlapping replacements, longest window first so a 3-word
    # mishearing wins over a 1-word sub-match, and left-to-right within a size.
    replacements = []          # (start, end, replacement)
    consumed = [False] * len(tokens)
    for size in range(FUZZY_MAX_WINDOW, 0, -1):
        for i in range(0, len(tokens) - size + 1):
            # Cheap prefilter before any per-window work: the first word's
            # phoneme must be able to begin a term.
            wp = word_dm[i][0]
            if not wp or wp[0] not in term_first:
                continue
            if any(consumed[i:i + size]):
                continue
            span_tokens = tokens[i:i + size]
            # Words must be separated by whitespace only — never merge across
            # punctuation ("ver. cell" / "ver, cell" are two clauses, not a word).
            gaps_ok = all(
                text[span_tokens[k].end():span_tokens[k + 1].start()].strip() == ""
                for k in range(size - 1)
            )
            if not gaps_ok:
                continue
            # Never rewrite a span that is entirely ordinary English.
            if all(word_low[k] in _COMMON_WORDS for k in range(i, i + size)):
                continue
            words = [t.group(0) for t in span_tokens]
            cand = " ".join(words)
            cand_low = cand.lower()
            # Reuse the per-word code for a 1-word span; encode only multi-word
            # spans that cleared the prefilter (a small minority).
            if size == 1:
                cand_codes = word_dm[i]
            else:
                try:
                    cand_codes = dm(cand)
                except Exception:
                    continue
            # Compare the collapsed letter sequences: metaphone already ignores
            # word breaks, so "ver cell" should score against "vercel" exactly as
            # "vercell" does — not be penalised for the space it was mis-split on.
            cand_key = cand_low.replace(" ", "")
            best = None
            best_jw = 0.0
            for term, low, codes, strict in terms:
                if cand_low == low:          # already correct — leave casing to others
                    best = None
                    break
                if not _phonetic_match(codes, cand_codes):
                    continue
                term_key = low.replace(" ", "")
                lr = len(cand_key) / float(len(term_key) or 1)
                len_lo, len_hi = ((MANAGED_LEN_LO, MANAGED_LEN_HI) if strict
                                  else (FUZZY_LEN_LO, FUZZY_LEN_HI))
                if not (len_lo <= lr <= len_hi):
                    continue
                jw = _jaro_winkler(cand_key, term_key)
                floor = MANAGED_JW if strict else FUZZY_JW
                if jw >= floor and jw > best_jw:
                    best, best_jw = term, jw
            if best is not None:
                start, end = span_tokens[0].start(), span_tokens[-1].end()
                replacements.append((start, end, _cased(best, text, start)))
                for k in range(i, i + size):
                    consumed[k] = True

    if not replacements:
        return text
    replacements.sort()
    out = []
    pos = 0
    for start, end, rep in replacements:
        out.append(text[pos:start])
        out.append(rep)
        pos = end
    out.append(text[pos:])
    return "".join(out)


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
