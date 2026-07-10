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

    def __init__(
        self,
        recorder,
        engine,
        context_words: str = "",
        hotwords: str = "",
        on_caption: Optional[Callable[[str], None]] = None,
        captions_enabled: bool = False,
    ):
        self._recorder = recorder
        self._engine = engine
        self._context = context_words
        self._hotwords = hotwords
        self._on_caption = on_caption
        self._captions = captions_enabled

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

        if self._captions and self._on_caption:
            # Use the SAME context as finalize() so the live caption tracks the
            # text that will actually be injected — a context-free hypothesis
            # reads noticeably rougher than the final pass. Display-only: this
            # never feeds the commit path or the injected result.
            #
            # Two guards keep the caption from showing words the user never
            # said (the final pass was always right; the LIVE text wasn't):
            #  1. Trim the trailing ~0.25s — that's the half-spoken word the
            #     model would otherwise have to guess at.
            #  2. LocalAgreement: only display the word-prefix that two
            #     consecutive hypotheses agree on. A guess that changes on the
            #     next tick never reaches the screen.
            trim = int(self.CAPTION_TAIL_TRIM * rate)
            cap_audio = audio[:-trim] if len(audio) > trim + int(rate * 0.6) else audio
            hyp = self._engine.transcribe(
                cap_audio, rate,
                context_words=self._context_with_committed(),
                hotwords_str=self._hotwords,
                blocking=False,   # skip the tick if the engine is busy
                finalize_text=False,
            ).strip()
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
                shown_hyp = " ".join(stable)
            else:
                shown_hyp = ""  # engine busy or silence — keep the last caption
            if shown_hyp or self._committed_texts:
                shown = " ".join(
                    self._committed_texts[-2:] + ([shown_hyp] if shown_hyp else [])
                )
                self._on_caption(shown)

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
