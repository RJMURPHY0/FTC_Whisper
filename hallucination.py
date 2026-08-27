"""
Degenerate-repetition guard — the one hallucination class both ASR engines can
produce out of thin air.

Parakeet is an RNN-T *transducer*: at each step it may emit a token and stay on
the same audio frame. On degenerate input (a dead/muted mic, heavy steady noise,
a clipped fragment, an unusual accent hitting a low-confidence region) the
decoder can lock into a cycle and emit the same token until the frame budget
runs out — "no, no, no, no, no, no, …" for hundreds of words. Whisper does the
same thing through a different mechanism (autoregressive looping), which is why
it ships `repetition_penalty`; a penalty lowers the odds but does not remove
them.

Nothing upstream catches this. The silence floor and the Silero VAD gate in
asr_engine only decide whether the model runs at all; once it runs, its output
was taken verbatim. The result is the worst possible failure for a dictation
app: a confident wall of text the user never said, injected straight into their
document.

The guard is deliberately text-level rather than a decoder parameter. Decoder
knobs (`no_repeat_ngram_size`, a harder `repetition_penalty`) suppress
legitimately repeated words too and cannot be tested without a GPU-hours
sweep; a text rule is exact, cheap, reversible and unit-testable.

Two rules, both conservative because this rewrites what the user actually said:

  * COLLAPSE — a unit of 1-4 words repeated `_MIN_RUN`+ times in a row is cut
    back to `_KEEP` occurrences. Emphatic real speech tops out around three
    ("no, no, no"), so keeping three never destroys meaning while a 40x loop
    still dies. The last occurrence of the run is kept alongside the first
    ones, so the run's terminal punctuation survives ("no, no, no.").

  * SUPPRESS — an utterance that is *nothing but* one repeated unit, repeated
    `_PURE_RUN`+ times, returns empty. Six or more identical adjacent units
    with no other content is diagnostic of decoder collapse, not dictation,
    and reporting nothing is honest where injecting "No, no, no." is not.
    The app already has a no-speech path for the empty result.

Anything below those thresholds is left exactly as spoken.
"""

import re
from typing import Callable, List, Optional

# A unit must repeat at least this many times consecutively before we touch it.
# Three is reachable in real emphatic speech ("no, no, no"), four essentially
# is not — and a decoder loop overshoots it by an order of magnitude.
_MIN_RUN = 4
# What a collapsed run is cut back to. Three preserves the emphatic reading of
# a genuine repeat and still reads as intentional after a 40x loop is killed.
_KEEP = 3
# A pure-loop utterance (no other content) is suppressed entirely at this many
# repeats. Higher than _MIN_RUN: deleting everything needs more evidence than
# trimming does.
_PURE_RUN = 6
# Longest repeated unit we look for. Covers phrase loops ("I don't know. I
# don't know. …") without matching the natural cadence of real sentences.
_MAX_UNIT = 4

_STRIP = " \t\"'.,!?;:()[]-—–"

# Optional fleet telemetry: app.py wires this to SupabaseLogger.log_error_event
# so a loop that fires in the wild is visible instead of dying in a console
# nobody reads. Never raises into the transcription path.
_reporter: Optional[Callable[[str, dict], None]] = None


def set_reporter(fn: Optional[Callable[[str, dict], None]]) -> None:
    """Install the telemetry sink. fn(event_type, detail_dict)."""
    global _reporter
    _reporter = fn


def _report(detail: dict) -> None:
    if _reporter is None:
        return
    try:
        _reporter("transcribe_repetition", detail)
    except Exception:
        pass


def report(event_type: str, detail: dict) -> None:
    """Send an arbitrary ASR-quality event to the same sink.

    Repetition loops are only one way an engine can return something the user
    did not say; whisper prompt echo is another, and it is detected in
    transcriber.py rather than here. Both belong in one fleet log, and the sink
    app.py installs is already generic, so this exposes it instead of adding a
    second wiring point that could be left unwired. Never raises."""
    if _reporter is None:
        return
    try:
        _reporter(event_type, detail)
    except Exception:
        pass


def _norm(token: str) -> str:
    """Comparison key for a token: punctuation and case carry no information
    about whether the decoder is looping ("no," == "no." == "No")."""
    return token.strip(_STRIP).lower()


def _units_equal(tokens: List[str], a: int, b: int, n: int) -> bool:
    for k in range(n):
        if _norm(tokens[a + k]) != _norm(tokens[b + k]):
            return False
    return True


def _run_length(tokens: List[str], start: int, n: int) -> int:
    """How many times the n-word unit at `start` repeats consecutively."""
    reps = 1
    j = start + n
    while j + n <= len(tokens) and _units_equal(tokens, start, j, n):
        reps += 1
        j += n
    return reps


def is_pure_loop(text: str) -> bool:
    """True when the whole utterance is one unit repeated _PURE_RUN+ times and
    nothing else — decoder collapse, never dictation."""
    tokens = (text or "").split()
    if len(tokens) < _PURE_RUN:
        return False
    # Ignore a unit that is itself empty after normalisation (pure punctuation).
    for n in range(1, _MAX_UNIT + 1):
        if len(tokens) < n * _PURE_RUN or len(tokens) % n:
            continue
        if not any(_norm(t) for t in tokens[:n]):
            continue
        reps = _run_length(tokens, 0, n)
        if reps * n == len(tokens) and reps >= _PURE_RUN:
            return True
    return False


def _period_length(tokens: List[str], i: int, n: int) -> int:
    """Length in tokens of the periodic region starting at `i` with period `n`.

    Extends token-by-token (`t[k] == t[k + n]`), NOT unit-by-unit. Comparing
    whole units only matches when the scan happens to start on a unit boundary:
    on "Honestly I don't know. I don't know. …" a unit-aligned scan locked onto
    the shifted phrase "know. I don't" and collapsed to that phase, stranding a
    dangling "I don't" in front of the next sentence. Extending per token finds
    the region wherever the repetition actually begins.
    """
    L = 0
    while i + n + L < len(tokens) and _norm(tokens[i + L]) == _norm(tokens[i + n + L]):
        L += 1
    return n + L if L else 0


def _collapse(tokens: List[str]) -> tuple:
    """Single left-to-right sweep collapsing every periodic run, trying the
    shortest period first. Returns (tokens, longest_run_seen)."""
    out: List[str] = []
    worst = 0
    i = 0
    while i < len(tokens):
        span = 0
        period = 0
        reps = 0
        if any(_norm(t) for t in tokens[i:i + _MAX_UNIT]):
            for n in range(1, _MAX_UNIT + 1):
                if i + n >= len(tokens):
                    break
                length = _period_length(tokens, i, n)
                if length and length // n >= _MIN_RUN:
                    span, period, reps = length, n, length // n
                    break
        if not span:
            out.append(tokens[i])
            i += 1
            continue
        worst = max(worst, reps)
        full = period * reps
        # First (_KEEP - 1) units, then the FINAL full unit of the run: the last
        # occurrence carries the sentence-terminal punctuation the decoder
        # attached, so "no, no, no, …, no." collapses to "no, no, no." rather
        # than losing the stop.
        out.extend(tokens[i:i + period * (_KEEP - 1)])
        out.extend(tokens[i + full - period:i + full])
        # A trailing partial unit is emitted verbatim — it is never more than
        # _MAX_UNIT - 1 tokens, and this code only ever deletes what it proved
        # was a full repeat.
        out.extend(tokens[i + full:i + span])
        i += span
    return out, worst


def clean(text: str, source: str = "") -> str:
    """Strip degenerate repetition from an ASR result.

    Returns "" when the utterance is nothing but a loop. Text with no
    qualifying run is returned byte-identical, so the ordinary path pays only
    one tokenise-and-scan.
    """
    if not text:
        return text
    tokens = text.split()
    if len(tokens) < _MIN_RUN:
        return text

    if is_pure_loop(text):
        print(f"[Hallucination] Pure repetition loop suppressed"
              f"{f' ({source})' if source else ''}: '{text[:80]}…'"
              f" [{len(tokens)} words]")
        _report({"action": "suppressed", "source": source,
                 "words": len(tokens), "sample": text[:200]})
        return ""

    tokens, worst = _collapse(tokens)
    if not worst:
        return text

    cleaned = " ".join(tokens)
    # A collapse can leave doubled separators behind ("no, , and").
    cleaned = re.sub(r",\s*,", ",", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    print(f"[Hallucination] Collapsed a {worst}x repetition"
          f"{f' ({source})' if source else ''}: "
          f"'{text[:60]}…' -> '{cleaned[:60]}'")
    _report({"action": "collapsed", "source": source, "repeats": worst,
             "words_before": len(text.split()), "words_after": len(tokens),
             "sample": text[:200]})
    return cleaned
