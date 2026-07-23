"""
Streaming transcription session — incremental committed-prefix transcription.

While the user is recording, a worker thread watches the audio buffer. Once
the uncommitted audio grows past ~10s it finds a silence boundary (a natural
pause), transcribes everything up to it ONCE, appends the text to a committed
list, and releases that audio back to the recorder. The tail that still needs
transcribing at hotkey-release is therefore always bounded (~10-18s max, and
with Parakeet that transcribes in ~0.3-0.8s) — a 5-minute dictation lands just
as fast as a 5-second one.

The same worker feeds live captions: when captions are on, each tick also
transcribes the current uncommitted window (non-blocking — skipped if the
engine is busy) and reports committed + hypothesis text via on_caption.

One engine lock serialises everything, so the finalize() pass can never race
a caption tick.
"""

import threading
import time
from typing import Callable, Optional

import numpy as np


class StreamingSession:
    TICK_INTERVAL = 0.3        # seconds between worker checks
    COMMIT_AFTER = 10.0        # start looking for a boundary once uncommitted > this
    FORCE_COMMIT_AT = 18.0     # commit at the quietest point even without clear silence
    KEEP_TAIL = 1.2            # never commit audio closer than this to "now"
    MIN_HEAD = 2.0             # never commit a boundary earlier than this
    SILENCE_WIN = 0.1          # RMS window for silence search
    SILENCE_RUN = 3            # consecutive quiet windows (=300ms) that count as a pause
    CAPTION_TAIL_TRIM = 0.25   # drop the trailing partial word from the caption window
    LOCK_LAG = 1               # keep the freshest N agreed words un-injected (churn buffer)
    PAUSE_FLUSH_QUIET = 0.55   # trailing silence (s) that counts as "speaker paused"
    PAUSE_FLUSH_RMS = 0.006    # quiet floor for the pause flush (matches the commit floor)
    PARAGRAPH_PAUSE = 2.0      # continuous quiet (s) that starts a new paragraph
    PARAGRAPH_RMS = 0.006      # absolute quiet floor for paragraph detection

    def __init__(
        self,
        recorder,
        engine,
        context_words: str = "",
        hotwords: str = "",
        on_caption: Optional[Callable[[str], None]] = None,
        captions_enabled: bool = False,
        on_inject: Optional[Callable[[str], bool]] = None,
        live_inject: bool = False,
        audio_sink=None,
        auto_paragraphs: bool = False,
    ):
        self._recorder = recorder
        self._engine = engine
        # Committed audio is dropped from the recorder ring to keep memory
        # flat, so the sink is the only place the full clip survives: each
        # chunk is handed over at the exact moment its samples are committed.
        self._audio_sink = audio_sink
        self._context = context_words
        self._hotwords = hotwords
        self._on_caption = on_caption
        self._captions = captions_enabled
        # Live-injection: emit locked words into the target app AS the user speaks.
        # on_inject returns True only if the chunk actually landed; append-only —
        # never a backspace here (the app's finalize reconcile owns correction).
        self._on_inject = on_inject
        self._live_inject = live_inject
        self._injected_words: list[str] = []   # words handed to on_inject (that landed)
        self._injected_text = ""               # exact string sent — drives finalize reconcile
        self._stream_frozen = False            # transport emit failed — stop emitting
        # Terminal punctuation stripped from a pause-flushed word: the model
        # ends EVERY pause with a period, even mid-sentence. It's held here and
        # restored on the next emit only if the model still ends the sentence
        # there (next word capitalised).
        self._pending_punct = ""
        # Consecutive ticks held by frontier disagreement — a commit that
        # rewords already-typed text must stall emission briefly, not forever.
        self._hold_ticks = 0

        # Auto-paragraphs: a clear dictation pause (PARAGRAPH_PAUSE of true
        # silence) after a finished sentence becomes a paragraph break in the
        # final text. Never under live injection: the live stream is append-only
        # plain words, and a newline emitted as a keypress could submit a chat
        # box, so the reconcile must never see paragraph breaks it didn't type.
        self._auto_paragraphs = bool(auto_paragraphs) and not live_inject
        # Break-BEFORE flag per _committed_texts entry (parallel list; only
        # finalize() reads it, so captions/context joins stay space-separated).
        self._para_flags: list[bool] = []
        # Trailing quiet seconds of the previous committed chunk, so a pause
        # that spans a commit boundary still counts in full.
        self._carry_quiet = 0.0

        self._committed_texts: list[str] = []
        self._committed_sample = 0     # absolute sample position of the commit frontier
        self._prev_hyp_words: list[str] = []  # last caption hypothesis (for agreement)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._finalized = False
        # Guards commit-state mutation vs finalize's tail computation: if the
        # 3s worker join times out mid-commit, the straggler tick must discard
        # its result instead of committing AFTER the tail range was computed
        # (which would duplicate that audio's text).
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="stream-session"
        )
        self._thread.start()

    def _worker(self) -> None:
        # Wait for recording to actually begin (start may run in another thread)
        for _ in range(25):
            if self._recorder.is_recording or self._stop_event.is_set():
                break
            time.sleep(0.04)

        while not self._stop_event.is_set() and self._recorder.is_recording:
            tick_start = time.time()
            try:
                self._tick()
            except Exception as e:
                print(f"[Stream] Tick error: {e}")
            remaining = self.TICK_INTERVAL - (time.time() - tick_start)
            if remaining > 0:
                self._stop_event.wait(remaining)

    def _tick(self) -> None:
        rate = max(1, self._recorder.active_sample_rate)
        audio = self._recorder.get_audio_range(self._committed_sample)
        if audio is None or len(audio) < rate * 0.6:
            return
        uncommitted_secs = len(audio) / rate

        if uncommitted_secs >= self.COMMIT_AFTER:
            boundary = self._find_commit_point(audio, rate, uncommitted_secs)
            if boundary is not None:
                chunk = audio[:boundary]
                # finalize_text=False: forced capitals/periods per chunk corrupt
                # the joined text at mid-sentence commit boundaries — polish is
                # applied once on the full utterance in finalize().
                results = self._transcribe_paragraph_parts(chunk, rate)
                preview = " ".join(t for t, _ in results if t)
                with self._state_lock:
                    if self._finalized or self._stop_event.is_set():
                        return  # finalize owns the tail now — discard, tail re-covers it
                    for text, brk in results:
                        if text:
                            self._committed_texts.append(text)
                            self._para_flags.append(brk)
                    if preview:
                        print(f"[Stream] Committed {boundary / rate:.1f}s: '{preview[:60]}…'")
                    # Advance even when the chunk transcribed empty (pure noise) so
                    # a noisy environment can't make the window grow unbounded.
                    # Sink write sits inside the same locked commit: a tick that
                    # loses the finalize race returns above and never writes, so
                    # the tail (which re-covers its audio) can't be duplicated.
                    if self._audio_sink is not None:
                        self._audio_sink.write(chunk, rate)
                    self._committed_sample += boundary
                    self._recorder.drop_audio_before(self._committed_sample)
                    # The caption window's start just moved — the previous
                    # hypothesis no longer aligns with it.
                    self._prev_hyp_words = []
                return

        # The stable-hypothesis pass feeds BOTH live captions and live injection.
        # Run it whenever either consumer is active.
        want_captions = bool(self._captions and self._on_caption)
        want_inject = bool(self._live_inject and self._on_inject and not self._stream_frozen)
        if want_captions or want_inject:
            # Use the SAME context as finalize() so the live hypothesis tracks the
            # text that will actually be injected — a context-free hypothesis
            # reads noticeably rougher than the final pass. Display-only for
            # captions; for injection only the LOCK_LAG-lagged prefix is emitted.
            #
            # Two guards keep us from surfacing words the user never said:
            #  1. Trim the trailing ~0.25s — that's the half-spoken word the
            #     model would otherwise have to guess at.
            #  2. LocalAgreement: only the word-prefix that two consecutive
            #     hypotheses agree on. A guess that changes next tick is withheld.
            trim = int(self.CAPTION_TAIL_TRIM * rate)
            cap_audio = audio[:-trim] if len(audio) > trim + int(rate * 0.6) else audio
            hyp = self._engine.transcribe(
                cap_audio, rate,
                context_words=self._context_with_committed(),
                hotwords_str=self._hotwords,
                blocking=False,   # skip the tick if the engine is busy
                finalize_text=False,
            ).strip()
            if not hyp:
                # Engine busy (skipped tick) or nothing spoken since the commit
                # frontier. Leave the caption showing its last text — updating
                # it with committed-only would visibly shrink then regrow.
                return
            stable: list[str] = []
            words = hyp.split()
            if self._prev_hyp_words:
                n = 0
                for a, b in zip(self._prev_hyp_words, words):
                    # Punctuation/caps on a word often settle one tick
                    # later than the word itself — compare bare words.
                    if a.strip(".,!?;:").lower() != b.strip(".,!?;:").lower():
                        break
                    n += 1
                stable = words[:n]
            else:
                # First hypothesis: nothing to agree with yet — withhold
                # only the in-flight final word instead of showing nothing.
                stable = words[:-1]
            self._prev_hyp_words = words
            # ── Captions ──
            if want_captions:
                shown_hyp = " ".join(stable)
                if shown_hyp or self._committed_texts:
                    # Full transcript-so-far, not a trailing window: the popup tails
                    # the newest words itself and lets the user scroll back through
                    # everything said. Strings this size are trivial to rejoin.
                    shown = " ".join(
                        self._committed_texts + ([shown_hyp] if shown_hyp else [])
                    )
                    self._on_caption(shown)
            # ── Live injection (append-only) ──
            if want_inject:
                committed_words = " ".join(self._committed_texts).split()
                pause = bool(stable and self._tail_is_quiet(audio, rate))
                if pause:
                    # Speaker paused: no upcoming words will push the tail past
                    # LOCK_LAG, so the withheld words would sit untyped until
                    # the next commit or hotkey release. The pause itself is
                    # the stability signal — flush the whole agreed hypothesis.
                    locked_hyp = stable
                else:
                    locked_hyp = stable[:-self.LOCK_LAG] if len(stable) > self.LOCK_LAG else []
                self._emit_locked(committed_words + locked_hyp, pause_flush=pause)

    @staticmethod
    def _norm_word(w: str) -> str:
        return w.strip(".,!?;:\"'").lower()

    def _quiet_profile(
        self, audio: np.ndarray, rate: int
    ) -> tuple[list[int], float, float]:
        """Scan for paragraph-length quiet runs. Returns (split_points,
        lead_secs, trail_secs): sample indices at the middle of each INTERNAL
        quiet run at least PARAGRAPH_PAUSE long, plus the quiet durations at
        the chunk's edges. Edge runs are never split points here — the carry
        logic in _transcribe_paragraph_parts joins them across boundaries."""
        win = max(1, int(self.SILENCE_WIN * rate))
        n_wins = len(audio) // win
        dur = len(audio) / rate
        if n_wins < 1:
            return [], dur, dur
        rms = np.sqrt(
            np.mean(
                audio[: n_wins * win].reshape(n_wins, win).astype(np.float64) ** 2,
                axis=1,
            )
        )
        quiet = rms < self.PARAGRAPH_RMS
        need = max(1, int(round(self.PARAGRAPH_PAUSE / self.SILENCE_WIN)))
        lead = 0
        for q in quiet:
            if not q:
                break
            lead += 1
        trail = 0
        for q in quiet[::-1]:
            if not q:
                break
            trail += 1
        splits: list[int] = []
        run = 0
        for i, q in enumerate(quiet):
            if q:
                run += 1
            else:
                if run >= need and run < i:  # internal run only, never leading
                    splits.append((i - run // 2) * win)
                run = 0
        return splits, lead * self.SILENCE_WIN, trail * self.SILENCE_WIN

    def _transcribe_paragraph_parts(
        self, chunk: np.ndarray, rate: int
    ) -> list[tuple[str, bool]]:
        """Transcribe a chunk, split at paragraph-length pauses when
        auto-paragraphs is on. Returns (text, break_before) pairs and updates
        the boundary-spanning quiet carry. Context chains across the parts so
        a split never costs recognition accuracy."""
        base_ctx = self._context_with_committed()
        if not self._auto_paragraphs:
            text = self._engine.transcribe(
                chunk, rate,
                context_words=base_ctx,
                hotwords_str=self._hotwords,
                finalize_text=False,
            ).strip()
            return [(text, False)]
        splits, lead_q, trail_q = self._quiet_profile(chunk, rate)
        first_break = (self._carry_quiet + lead_q) >= self.PARAGRAPH_PAUSE
        if lead_q >= (len(chunk) / rate) - 1e-6:
            # Whole chunk is silence — keep accumulating across chunks so a
            # very long pause spanning several commits still counts in full.
            self._carry_quiet += len(chunk) / rate
        else:
            self._carry_quiet = trail_q
        parts: list[np.ndarray] = []
        prev = 0
        for s in splits:
            parts.append(chunk[prev:s])
            prev = s
        parts.append(chunk[prev:])
        out: list[tuple[str, bool]] = []
        ctx = base_ctx
        for part in parts:
            if len(part) < int(rate * 0.25):
                continue
            text = self._engine.transcribe(
                part, rate,
                context_words=ctx,
                hotwords_str=self._hotwords,
                finalize_text=False,
            ).strip()
            if not text:
                continue
            # Parts after the first surviving one were separated from it by a
            # paragraph-length pause by construction.
            out.append((text, first_break if not out else True))
            ctx = f"{ctx} {text}".strip()
        if not out:
            return [("", False)]
        return out

    def _tail_is_quiet(self, audio: np.ndarray, rate: int) -> bool:
        """True when the trailing PAUSE_FLUSH_QUIET seconds of the uncommitted
        window are silence — the speaker has paused. Uses an absolute floor, so
        a noisy room simply never flushes early (fail-safe: behaves as before)."""
        n = int(self.PAUSE_FLUSH_QUIET * rate)
        if n <= 0 or len(audio) < n:
            return False
        tail = audio[-n:].astype(np.float64)
        return float(np.sqrt(np.mean(tail * tail))) < self.PAUSE_FLUSH_RMS

    def _emit_locked(self, locked: list[str], pause_flush: bool = False) -> None:
        """Append-only live injection. `locked` is the confident word list so far
        (committed words + lagged agreed hypothesis). Emit whatever extends what
        we already typed. Never retracts — the app's finalize reconcile owns
        correction. on_inject's return contract: True = landed, None = target
        not focused right now (skip, retry next tick), False = transport failure
        (freeze for the rest of the dictation)."""
        # Once finalize has begun (stop_event set) it owns the document — a
        # straggler tick must NOT inject after the reconcile has started, or it
        # would append past the corrected text and desync the char count.
        if self._stop_event.is_set() or self._finalized:
            return
        n_have = len(self._injected_words)
        if n_have > len(locked):
            return  # locked shrank this tick — wait for it to regrow
        # Agreement check on the recent frontier ONLY (last 6 words). A commit
        # re-transcription can reword text far behind the frontier; requiring
        # full-prefix agreement made emission stall permanently ("live typing
        # just stops"). Old-text correctness belongs to the finalize reconcile.
        start = max(0, n_have - 6)
        diverged = any(
            self._norm_word(a) != self._norm_word(b)
            for a, b in zip(self._injected_words[start:n_have],
                            locked[start:n_have])
        )
        if diverged:
            self._hold_ticks += 1
            if self._hold_ticks <= 4:
                return  # hypothesis churn — usually settles within a tick or two
            # Persistent rewording at the frontier (a commit changed words we
            # already typed): keep the stream flowing anyway — finalize repairs
            # the wording. A frozen stream reads far worse than a small drift.
        self._hold_ticks = 0
        new_words = list(locked[n_have:])
        if not new_words:
            return
        if n_have == 0 and new_words[0] and new_words[0][0].islower():
            # Mirror polish()'s leading capital NOW: finalize() capitalises the
            # first word, and a byte-exact reconcile prefix-compare means a
            # lowercase live first char would force a FULL delete+retype of the
            # whole dictation (the exact "typed twice / truncated" corruption
            # when a transport drops keys). Capitalising here collapses the
            # reconcile to appending the terminal period. Agreement compares
            # via _norm_word (case-folded), so this never breaks the prefix check.
            new_words = [new_words[0][0].upper() + new_words[0][1:], *new_words[1:]]
        # Restore a previously withheld pause period only if the model STILL
        # ends the sentence there (next word starts a capital). If it re-read
        # the pause as mid-sentence, the period silently disappears — which is
        # exactly what the document needs.
        lead = ""
        if self._injected_text:
            lead = " "
            if self._pending_punct and 0 < n_have <= len(locked):
                prev = locked[n_have - 1]
                if prev and prev[-1] in ".!?" and new_words[0][:1].isupper():
                    lead = prev[-1] + " "
        # A pause flush carries the model's utterance-final period even when the
        # user merely paused mid-sentence. Hold that punctuation back; the next
        # chunk decides whether it was real (see above).
        new_pending = ""
        if pause_flush and new_words[-1] and len(new_words[-1]) > 1 \
                and new_words[-1][-1] in ".!?":
            new_pending = new_words[-1][-1]
            new_words = [*new_words[:-1], new_words[-1][:-1]]
        chunk = lead + " ".join(new_words)
        ok = None
        try:
            ok = self._on_inject(chunk)
        except Exception as e:
            print(f"[Stream] live-inject emit error: {e}")
            ok = False
        if ok is None:
            # Target not foreground right now (toast, brief alt-tab). Nothing
            # was typed and no state changed — emission resumes automatically
            # when focus returns. This must NOT freeze the stream: a transient
            # focus flick used to kill live typing for the whole dictation.
            return
        if ok:
            self._injected_words.extend(new_words)
            self._injected_text += chunk
            self._pending_punct = new_pending
        else:
            # Transport genuinely failed — stop emitting so we can't leave a
            # gap in the document; the finalize reconcile appends the rest.
            self._stream_frozen = True
            print("[Stream] live-inject frozen (emit transport failed)")

    def _find_commit_point(
        self, audio: np.ndarray, rate: int, uncommitted_secs: float
    ) -> Optional[int]:
        """Latest natural pause in the eligible region, or (past FORCE_COMMIT_AT)
        the quietest window. Returns a sample index into `audio` or None."""
        win = max(1, int(self.SILENCE_WIN * rate))
        lo = int(self.MIN_HEAD * rate)
        hi = len(audio) - int(self.KEEP_TAIL * rate)
        if hi - lo < win * self.SILENCE_RUN:
            return None

        seg = audio[lo:hi]
        n_wins = len(seg) // win
        if n_wins < self.SILENCE_RUN:
            return None
        rms = np.sqrt(
            np.mean(
                seg[: n_wins * win].reshape(n_wins, win).astype(np.float64) ** 2,
                axis=1,
            )
        )
        thr = max(0.006, float(np.percentile(rms, 20)) * 1.5)

        run = 0
        best_end = None
        for i, v in enumerate(rms):
            if v < thr:
                run += 1
                if run >= self.SILENCE_RUN:
                    best_end = i  # latest qualifying run wins
            else:
                run = 0
        if best_end is not None:
            # Middle of the quiet run — clean cut inside the pause
            mid_win = best_end - self.SILENCE_RUN // 2
            return lo + mid_win * win

        if uncommitted_secs >= self.FORCE_COMMIT_AT:
            return lo + int(np.argmin(rms)) * win + win // 2
        return None

    def _context_with_committed(self) -> str:
        tail = " ".join(" ".join(self._committed_texts).split()[-30:])
        return f"{self._context} {tail}".strip()

    # ------------------------------------------------------------------

    def finalize(self) -> tuple[str, Optional[np.ndarray], int]:
        """Stop the worker, stop the recorder, transcribe the remaining tail and
        return (full_text, tail_audio, sample_rate). Call exactly once, from the
        stop-recording path. tail_audio is the audio the final text's last part
        came from (None if nothing was captured)."""
        with self._state_lock:
            if self._finalized:
                return "", None, self._recorder.active_sample_rate
            self._finalized = True

        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

        rate = max(1, self._recorder.active_sample_rate)
        offset_before = self._recorder.dropped_samples
        audio = self._recorder.stop()

        with self._state_lock:
            committed_sample = self._committed_sample
            committed_texts = list(self._committed_texts)
            para_flags = list(self._para_flags)

        tail: Optional[np.ndarray] = None
        if audio is not None and len(audio) > 0:
            rel = committed_sample - offset_before
            tail = audio[rel:] if 0 < rel < len(audio) else (None if rel >= len(audio) else audio)

        tail_entries: list[tuple[str, bool]] = []
        if tail is not None and len(tail) >= rate * 0.25:
            tail_entries = self._transcribe_paragraph_parts(tail, rate)

        entries = list(zip(committed_texts, para_flags))
        entries += [(t, b) for t, b in tail_entries if t]
        full = self._assemble_paragraphs(entries)
        if full and hasattr(self._engine, "polish"):
            full = self._engine.polish(full)
        return full, tail, rate

    @staticmethod
    def _assemble_paragraphs(entries: list[tuple[str, bool]]) -> str:
        """Join (text, break_before) entries into paragraph text. A break
        lands after a finished sentence. When the pause said "break" but the
        model left the previous part unpunctuated (common when the segment
        cut fell inside the silence), the next part starting with a capital
        letter confirms the sentence ended — supply the period and break. A
        lowercase continuation joins as one paragraph: that pause was
        mid-sentence thinking. With auto-paragraphs off every flag is False
        and this reduces to the old single space-joined string."""
        paras: list[list[str]] = [[]]
        for text, brk in entries:
            if not text:
                continue
            if brk and paras[-1]:
                prev = paras[-1][-1].rstrip()
                nxt = text.lstrip()
                if prev and prev[-1] in ".!?":
                    paras.append([])
                elif prev and nxt[:1].isupper():
                    paras[-1][-1] = prev + "."
                    paras.append([])
            paras[-1].append(text)
        para_strs = [" ".join(" ".join(p).split()) for p in paras]
        return "\n\n".join(p for p in para_strs if p)

    def abort(self) -> None:
        """Cancel path — just stop the worker; caller handles the recorder."""
        with self._state_lock:
            self._finalized = True
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)

    @property
    def committed_text(self) -> str:
        return " ".join(self._committed_texts)

    @property
    def injected_text(self) -> str:
        """The exact string live-injected into the target while speaking (drives
        the finalize reconcile). Empty if live-inject was off or nothing landed."""
        return self._injected_text

    @property
    def stream_frozen(self) -> bool:
        """True if live-injection stopped early (an emit failed / focus was lost) —
        the caller must NOT backspace-reconcile; append the remainder instead."""
        return self._stream_frozen
