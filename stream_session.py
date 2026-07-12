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
    TICK_INTERVAL = 0.5        # seconds between worker checks
    COMMIT_AFTER = 10.0        # start looking for a boundary once uncommitted > this
    FORCE_COMMIT_AT = 18.0     # commit at the quietest point even without clear silence
    KEEP_TAIL = 1.2            # never commit audio closer than this to "now"
    MIN_HEAD = 2.0             # never commit a boundary earlier than this
    SILENCE_WIN = 0.1          # RMS window for silence search
    SILENCE_RUN = 3            # consecutive quiet windows (=300ms) that count as a pause
    CAPTION_TAIL_TRIM = 0.25   # drop the trailing partial word from the caption window
    LOCK_LAG = 2               # keep the freshest N agreed words un-injected (churn buffer)

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
    ):
        self._recorder = recorder
        self._engine = engine
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
        self._stream_frozen = False            # an emit failed / focus lost — stop emitting

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
                text = self._engine.transcribe(
                    chunk, rate,
                    context_words=self._context_with_committed(),
                    hotwords_str=self._hotwords,
                    finalize_text=False,
                ).strip()
                with self._state_lock:
                    if self._finalized or self._stop_event.is_set():
                        return  # finalize owns the tail now — discard, tail re-covers it
                    if text:
                        self._committed_texts.append(text)
                        print(f"[Stream] Committed {boundary / rate:.1f}s: '{text[:60]}…'")
                    # Advance even when the chunk transcribed empty (pure noise) so
                    # a noisy environment can't make the window grow unbounded.
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
            stable: list[str] = []
            if hyp:
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
                locked_hyp = stable[:-self.LOCK_LAG] if len(stable) > self.LOCK_LAG else []
                self._emit_locked(committed_words + locked_hyp)

    @staticmethod
    def _norm_word(w: str) -> str:
        return w.strip(".,!?;:\"'").lower()

    def _emit_locked(self, locked: list[str]) -> None:
        """Append-only live injection. `locked` is the confident word list so far
        (committed words + lagged agreed hypothesis). Emit whatever extends what we
        already typed, but ONLY if the already-typed prefix still agrees (ignoring
        case/punctuation churn at commit boundaries). Never retracts — genuine
        divergence just holds until the app's finalize reconcile fixes it."""
        # Once finalize has begun (stop_event set) it owns the document — a
        # straggler tick must NOT inject after the reconcile has started, or it
        # would append past the corrected text and desync the char count.
        if self._stop_event.is_set() or self._finalized:
            return
        n_have = len(self._injected_words)
        if n_have > len(locked):
            return  # locked shrank this tick — wait for it to regrow
        # Verify our already-emitted prefix still matches (loose compare).
        for a, b in zip(self._injected_words, locked):
            if self._norm_word(a) != self._norm_word(b):
                return  # divergence — hold; finalize reconcile corrects it
        new_words = locked[n_have:]
        if not new_words:
            return
        chunk = (" " if self._injected_text else "") + " ".join(new_words)
        ok = False
        try:
            ok = bool(self._on_inject(chunk))
        except Exception as e:
            print(f"[Stream] live-inject emit error: {e}")
            ok = False
        if ok:
            self._injected_words.extend(new_words)
            self._injected_text += chunk
        else:
            # Emit failed (or focus left the target). Stop emitting so we can't
            # leave a gap in the document; the finalize reconcile appends the rest.
            self._stream_frozen = True
            print("[Stream] live-inject frozen (emit failed / focus lost)")

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

        tail: Optional[np.ndarray] = None
        if audio is not None and len(audio) > 0:
            rel = committed_sample - offset_before
            tail = audio[rel:] if 0 < rel < len(audio) else (None if rel >= len(audio) else audio)

        tail_text = ""
        if tail is not None and len(tail) >= rate * 0.25:
            tail_text = self._engine.transcribe(
                tail, rate,
                context_words=self._context_with_committed(),
                hotwords_str=self._hotwords,
                finalize_text=False,
            ).strip()

        full = " ".join([*committed_texts, tail_text]).strip()
        full = " ".join(full.split())  # normalise double spaces at joins
        if full and hasattr(self._engine, "polish"):
            full = self._engine.polish(full)
        return full, tail, rate

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
