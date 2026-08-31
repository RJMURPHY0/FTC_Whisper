"""
Stutter / false-start collapse — the disfluency an ASR engine transcribes
faithfully *because it really was said*.

This is a different failure from the degenerate loop in `hallucination.py`. A
loop is the decoder inventing text nobody spoke; a stutter is the decoder
correctly transcribing a real disfluency: the speaker began a word, cut it off,
and said it again ("push all most **rec** recent changes"), or doubled a
function word ("I **I** think"). The engine is right — the fragment was uttered
— but the user wants the finished sentence, not the false start.

Runs inside both engines' `_post_process`, the one point every path (batch,
committed streaming chunk, live caption, upgrade pass) flows through — the same
placement as `hallucination.clean`, for the same reason.

Because this deletes words the user genuinely said, it is deliberately timid.
Two guards, and a pair only collapses when it clears one of two narrow shapes:

  * FALSE START — a short fragment immediately followed by the fuller word it
    was an aborted attempt at: `_key(prev)` is a strict prefix of `_key(cur)`
    and `cur` is not merely `prev` plus an inflection. "rec" -> "recent",
    "prob" -> "probably", "config" -> "configuration". The fragment is dropped.

  * FUNCTION-WORD DOUBLE — an exact adjacent repeat of a function word that is
    never validly said twice ("the the", "I I", "to to"). The first is dropped.

Everything else is left exactly as spoken. In particular:

  * A fragment that is itself an ordinary word is never dropped ("the theory",
    "he helps", "in industry", "car cars") — the common-word list and the
    inflection guard between them cover the base/inflected and function-word
    cases that dominate the risk.
  * An emphatic or grammatical double is never collapsed ("very very", "no no",
    "had had", "that that") — none is in the collapse set.
  * A sentence boundary is never crossed ("...the cat. Cat food...").

The safe direction here is to UNDER-correct: a missed stutter leaves the text
one word longer, a wrong deletion changes what the user said. The tests pin both
directions, and the false-positive corpus (real dictation) is the one that
matters most.
"""

import re
from typing import Callable, List, Optional

# A fragment must be at least this long. A single stray letter before a word
# ("r recent") is as likely a mis-split as a false start, and clipping a real
# one-letter token ("a", "I") is not worth the rare catch.
_MIN_FRAGMENT = 2

# Edge punctuation ignored when comparing two tokens.
_STRIP = " \t\"'.,!?;:()[]{}-—–…"

# Sentence-final marks. A pair straddling one belongs to two sentences, so the
# apparent duplicate/prefix is a coincidence, never a stutter.
_SENT_END = ".!?"

# Inflectional tails. When the fuller word is exactly the fragment plus one of
# these, the two are a real base/inflected or singular/plural pair ("car cars",
# "read reading", "help helped"), NOT a false start — a genuine fragment adds
# arbitrary letters ("rec" -> "recent"), never a clean grammatical suffix.
_INFLECTIONS = frozenset((
    "s", "es", "ed", "d", "ing", "ings", "er", "ers", "est", "ly",
    "ion", "ions", "y", "ies", "n", "en", "ness", "ment", "ments",
))

# Exact adjacent duplicates collapse ONLY for these. Every one is a function
# word that is never validly said twice in a row. Words with a grammatical
# double ("had had", "that had"), an emphatic double ("very very", "no no",
# "so so", "really really") or a name reading ("Will will") are deliberately
# absent, so a real repeat always survives.
_DUP_COLLAPSE = frozenset("""
the a an i we he she it they you
to of in on at for with from by
and or but
is was are am be been will would can could should do does did has have
this these those your my our
""".split())

# Ordinary words that may look like a fragment of the next word but are complete
# words in their own right, and so must never be dropped ("the theory", "he
# helps", "in industry", "part party"). Not a dictionary — the inflection guard
# already protects base/inflected pairs — but every function word plus the
# common short words most likely to sit in front of a longer word they prefix.
_COMMON_WORDS = frozenset("""
a about above across act add after again age ago air all also am an and any
apple are area arm army art as ask at away baby back bad bag ball ban band
bank bar base bat be bear beat bed been beer bell best bet big bill bind bird
bit black blue boat body book born both box boy bug bus but buy by cab call
came camp can cap car card care case cast cat catch cause cell chat check
child city class clay clean clear cloud club coal coat cod code cold
come cool cop copy cord core corn cost could cow crew cry cup cut dad dam
dark data date day dead deal dear deep deny desk did die diet dig dim dip
do dock doctor does dog done door dot down draw dream drink drop drug dry due
dust each ear earn ease east easy eat edge egg eight else end even ever every
eye face fact fail fair fall fan far farm fast fat fear feed feel fell few
field file fill film find fine fire firm fish fist fit five fix flag flat
flow fly fold food foot for force form fort four free from fuel full fun fund
fur gain game gap gas gate gave gear get gift girl give glad go goal god gold
gone good got grab gray great grew grid grow gun guy had hair half hall hand
hang happy hard has hat hate have he head heal hear heat held hell help her
here hero hey hi hide high hill him his hit hold hole home hope host hot hour
how huge hunt hurt ice idea if ill in inch info ink into iron is it item its
jam job join joke joy jump just keep kept key kick kid kill kind king kiss kit
know lab lack lady laid lake lamp land lane last late lawn lay lazy lead leaf
lean leap learn led left leg lend less let lie life lift like line link lip
list live load loan lock log long look loop lord lose loss lost lot loud love
low luck mad made mail main make male man many map mark mask mass mat match
math may me meal mean meat meet men menu mess met mid mild mile milk mind mine
mint miss mix mob mode mood moon more most move much mud must my nail name near
neat neck need net new news next nice night nine no node none nor nose not note
now null nut oak obey odd off oil old on once one only onto open oral or oral
our out over own pace pack page paid pain pair pale palm pan park part pass
past path pay peak peer pen per pet pick pie pig pile pin pink pipe pit plan
play plot plug plus poem poet point pole poll pond pool poor pop port pose post
pour pray prep prey pro push put quiz race rack rage rail rain rank rare
rate raw read real rear red rely rent rest rice rich ride ring riot rise
risk road rob rock rod role roll roof room root rope rose row rule run rush
sad safe said sail sale salt same sand save saw say sea seat see seed seek seem
seen self sell send sent set sew shall ship shoe shop shot show shut sick side
sign silk sing sink sir sit site six size skin sky slip slow snap snow so soap
sock soft soil sold sole solid some son song soon sort soul soup sour spa spin
spot spy stab star stay stem step still stir stop such suit sum sun sure swim
tab tail take tale talk tall tank tap tape task tax tea team tear tech tell ten
tend tent term test text than that the them then there these they thin thing
this those thus tick tide tie till time tin tiny tip tire to today toe told toll
tone too took tool top torn toss tour town toy trap tray tree trim trip true try
tube tune turn twin two type ugly unit up upon urge us use user vary vast very
vet via vice view vote wage wait wake walk wall want war ward warm warn wash
wave way we weak wear web week well went were west wet what when where which
while whip who whom why wide wife wild will win wind wine wing wipe wire wise
wish with wolf won wood wool word wore work worm worn wrap yard yeah year yes
yet you your zero zone zoo
""".split())

# Optional fleet telemetry: app.py wires this to SupabaseLogger.log_error_event
# so a stutter collapse that fires in the wild is visible. Never raises into the
# transcription path.
_reporter: Optional[Callable[[str, dict], None]] = None


def set_reporter(fn: Optional[Callable[[str, dict], None]]) -> None:
    """Install the telemetry sink. fn(event_type, detail_dict)."""
    global _reporter
    _reporter = fn


def _report(detail: dict) -> None:
    if _reporter is None:
        return
    try:
        _reporter("transcribe_stutter", detail)
    except Exception:
        pass


def _key(tok: str) -> str:
    """Comparison key: edge punctuation and case carry no stutter information."""
    return tok.strip(_STRIP).lower()


def _ends_sentence(tok: str) -> bool:
    """True when the token's trailing punctuation includes a sentence mark."""
    for ch in reversed(tok):
        if ch.isalnum():
            return False
        if ch in _SENT_END:
            return True
    return False


def _is_false_start(prev_key: str, cur_key: str) -> bool:
    """`prev` is an aborted attempt at `cur`: a strict, non-inflectional prefix
    that is not itself an ordinary word."""
    if len(prev_key) < _MIN_FRAGMENT or len(prev_key) >= len(cur_key):
        return False
    if not prev_key.isalpha() or not cur_key.isalpha():
        return False
    if not cur_key.startswith(prev_key):
        return False
    if prev_key in _COMMON_WORDS:
        return False
    # A clean inflectional tail means these are two real words, not a stutter.
    if cur_key[len(prev_key):] in _INFLECTIONS:
        return False
    return True


def _is_function_double(prev_key: str, cur_key: str) -> bool:
    """`prev` and `cur` are the same never-validly-doubled function word."""
    return prev_key == cur_key and prev_key in _DUP_COLLAPSE


def _carry_capital(dropped: str, kept: str) -> str:
    """Move a leading capital from the dropped false-start onto the kept word.

    The dropped token held the utterance/sentence-initial position, so
    "Rec recent" -> "Recent" and "The the file" -> "The file". Only fires when
    the dropped word was capitalised and the kept one is lower case, so a
    mid-sentence collapse never invents a capital."""
    lead = len(kept) - len(kept.lstrip(_STRIP))
    d = dropped.lstrip(_STRIP)
    k = kept[lead:]
    if d and k and d[0].isupper() and k[:1].islower():
        return kept[:lead] + k[0].upper() + k[1:]
    return kept


def destutter(text: str, source: str = "") -> str:
    """Collapse false-start and function-word-double stutters.

    Only the dropped fragment and the single space beside it are removed — every
    other byte, including paragraph breaks, is preserved verbatim. Text with no
    qualifying pair is returned unchanged, so the ordinary dictation pays only
    one tokenise-and-scan.
    """
    if not text:
        return text
    spans = list(re.finditer(r"\S+", text))
    if len(spans) < 2:
        return text
    toks = [m.group(0) for m in spans]

    # ── Phase 1: decide which tokens to drop, comparing each against the last
    # token still standing (so cascaded stutters — "for for for" — resolve). ──
    final = list(toks)              # mutated only to carry a leading capital
    dropped = set()
    last = None                     # index of the last kept token
    for i, tok in enumerate(toks):
        if last is not None and not _ends_sentence(final[last]):
            pk, ck = _key(final[last]), _key(tok)
            if pk and ck and (_is_false_start(pk, ck)
                              or _is_function_double(pk, ck)):
                # The last kept token is the false start / first double: drop it
                # and keep this one, carrying any sentence-leading capital across.
                final[i] = _carry_capital(final[last], tok)
                dropped.add(last)
                last = i
                continue
        last = i

    if not dropped:
        return text

    # ── Phase 2: rebuild, preserving the original whitespace everywhere except
    # the gap beside a dropped token, which collapses to a single space. ──
    out: List[str] = [text[: spans[0].start()]]
    started = False
    gap_dirty = False
    prev_end = 0
    for i, span in enumerate(spans):
        if i in dropped:
            gap_dirty = True
            continue
        if not started:
            out.append(final[i])
            started = True
        else:
            out.append(" " if gap_dirty else text[prev_end: span.start()])
            out.append(final[i])
        prev_end = span.end()
        gap_dirty = False
    out.append(text[spans[-1].end():])

    cleaned = "".join(out)
    print(f"[Disfluency] Collapsed {len(dropped)} stutter(s)"
          f"{f' ({source})' if source else ''}: "
          f"'{text[:60]}' -> '{cleaned[:60]}'")
    _report({"source": source, "dropped": len(dropped),
             "words_before": len(toks), "words_after": len(toks) - len(dropped),
             "sample": text[:200]})
    return cleaned
