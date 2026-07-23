"""
Visual and audio feedback for the application.
Plays sounds on recording start/stop and manages tray icon state changes.

Sounds are soft sine blips (generated once in memory, played async via
winsound.PlaySound) rather than winsound.Beep: Beep is a raw square wave at
full volume, which reads as a long, harsh, high-pitched alarm. The blips are
quiet, faded in/out, and short — unobtrusive but clearly audible.
"""

import io
import math
import struct
import sys
import threading
import wave
from typing import Callable, Optional
# error_reporter pulls in urllib/email (~100ms) — imported on first error
# instead of at startup (this module is on the first-paint path).

_SR = 22050  # sample rate for generated blips — plenty for simple sines


def _tone(frequency: float, ms: float, volume: float = 0.22,
          fade_ms: float = 12.0) -> list:
    """One soft sine tone with attack/release fades (no clicks, no harshness)."""
    n = int(_SR * ms / 1000)
    fade = max(1, int(_SR * fade_ms / 1000))
    out = []
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = max(0.0, (n - i) / fade)
        out.append(math.sin(2 * math.pi * frequency * i / _SR) * volume * env)
    return out


def _glide(f0: float, f1: float, ms: float, volume: float = 0.14,
           peak_frac: float = 0.42, glide_frac: float = 0.38) -> list:
    """One short pitch glide with a raised-cosine envelope peaking at
    peak_frac of the duration. The pitch sweeps over the first glide_frac
    then holds f1 — matching the measured Glaido shape (quick move, settle).
    Phase-accumulated so the sweep is clickless; a quiet second harmonic
    rounds the timbre out of pure-sine territory."""
    n = int(_SR * ms / 1000)
    peak = max(1, int(n * peak_frac))
    sweep = max(1, int(n * glide_frac))
    out = []
    phase = 0.0
    for i in range(n):
        f = f0 + (f1 - f0) * min(1.0, i / sweep)
        phase += 2 * math.pi * f / _SR
        if i < peak:
            env = 0.5 - 0.5 * math.cos(math.pi * i / peak)
        else:
            env = 0.5 + 0.5 * math.cos(math.pi * (i - peak) / (n - peak))
        s = math.sin(phase) + 0.30 * math.sin(2 * phase)
        out.append(s * volume * env)
    return out


def _silence(ms: float) -> list:
    return [0.0] * int(_SR * ms / 1000)


def _wav_bytes(samples: list) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
            for s in samples))
    return buf.getvalue()


_SOUND_CACHE: dict = {}


def _get_sound(name: str) -> bytes:
    """Lazily build and cache one notification blip. Built off the UI thread
    (callers are daemon threads), ~1ms of pure-python sine maths."""
    if name not in _SOUND_CACHE:
        recipes = {
            # Matched to the Glaido chime pair (measured from its assets:
            # ~80ms rise 304→456 Hz / ~95ms fall 445→286 Hz, peak ≈0.15).
            # start: one soft low rising glide — "listening"
            "start": _glide(304, 456, 80),
            # stop: the falling mirror — "got it"
            "stop": _glide(445, 286, 95, peak_frac=0.55),
            # done: a single very soft tap in the same register
            "done": _glide(440, 452, 55, volume=0.09),
            # error: one low, quiet buzz — noticeable, not alarming
            "error": _tone(233, 220, volume=0.20),
        }
        _SOUND_CACHE[name] = _wav_bytes(recipes[name])
    return _SOUND_CACHE[name]


def _play_sound(name: str) -> None:
    """Play a named blip asynchronously (Windows only, never raises)."""
    try:
        import winsound
        winsound.PlaySound(_get_sound(name),
                           winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        _play_beep(700, 90)  # last-resort fallback rather than going silent


def _play_beep(frequency: int = 800, duration_ms: int = 150) -> None:
    """Play a short beep sound (Windows only). Fallback path only."""
    try:
        import winsound
        winsound.Beep(frequency, duration_ms)
    except Exception:
        pass  # Silently fail on non-Windows or if audio unavailable


class Feedback:
    """
    Provides audio and visual feedback for app state changes.

    Audio (soft notification blips, not raw beeps):
        - Rising two-note blip on recording start
        - Falling two-note blip on recording stop
        - Quiet double blip on transcription complete

    Visual:
        - Delegates icon updates to a tray callback
    """

    def __init__(
        self,
        sound_enabled: bool = True,
        on_icon_change: Optional[Callable[[str], None]] = None,
        on_error_notify: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            sound_enabled: Whether to play sound feedback
            on_icon_change: Callback to change tray icon. Called with state name:
                            "idle", "recording", "processing"
            on_error_notify: Optional callback invoked with the error message so
                             the app can log it and show a visible notification —
                             additive on top of the buzz, never replaces it
        """
        self.sound_enabled = sound_enabled
        self.on_icon_change = on_icon_change
        self.on_error_notify = on_error_notify

    def recording_started(self) -> None:
        """Called when recording begins."""
        if self.on_icon_change:
            self.on_icon_change("recording")

        if self.sound_enabled:
            threading.Thread(
                target=_play_sound, args=("start",), daemon=True
            ).start()

    def recording_stopped(self) -> None:
        """Called when recording ends and processing begins."""
        if self.on_icon_change:
            self.on_icon_change("processing")

        if self.sound_enabled:
            threading.Thread(
                target=_play_sound, args=("stop",), daemon=True
            ).start()

    def transcription_complete(self, text: str) -> None:
        """Called when transcription is done and text has been injected."""
        if self.on_icon_change:
            self.on_icon_change("idle")

        if self.sound_enabled:
            # The gap between the two blips is baked into the wav itself, so
            # one async play does the whole figure without a sleeping thread.
            threading.Thread(
                target=_play_sound, args=("done",), daemon=True
            ).start()

    def error_occurred(self, error: str) -> None:
        """Called when an error occurs during recording/transcription."""
        if self.on_icon_change:
            self.on_icon_change("idle")

        if self.sound_enabled:
            # Low buzz for error
            threading.Thread(
                target=_play_sound, args=("error",), daemon=True
            ).start()

        print(f"[Feedback] Error: {error}")
        try:
            from error_reporter import report_error
            report_error(error)
        except Exception:
            pass

        # Additive visibility: the buzz alone is easy to miss (and silent when
        # sound is off), print vanishes in console=False builds, and
        # report_error no-ops when unconfigured — let the app log the error
        # and show a visible notification too.
        if self.on_error_notify:
            try:
                self.on_error_notify(error)
            except Exception as e:
                print(f"[Feedback] error notify failed (non-fatal): {e}")
