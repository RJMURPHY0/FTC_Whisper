"""
Visual and audio feedback for the application.
Plays sounds on recording start/stop and manages tray icon state changes.

The recording start/stop cues play the Windows system "ding"
(winsound.MessageBeep) so they match the sound Claude emits on Ctrl+Alt+Enter
and are easy to hear. The remaining cues (done/error) are soft sine blips
generated once in memory and played synchronously via winsound.PlaySound —
never winsound.Beep, which is a raw square wave at full volume that reads as a
long, harsh, high-pitched alarm.
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
           peak_frac: float = 0.42, glide_frac: float = 0.38,
           harmonic: float = 0.30) -> list:
    """One short pitch glide with a raised-cosine envelope peaking at
    peak_frac of the duration. The pitch sweeps over the first glide_frac
    then holds f1 — matching the measured Glaido shape (quick move, settle).
    Phase-accumulated so the sweep is clickless; `harmonic` sets how much
    second harmonic rounds the timbre — keep it small (≤0.12) for a soft,
    dark chime, larger for a brighter one."""
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
        s = math.sin(phase) + harmonic * math.sin(2 * phase)
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


# Cues that should play the actual Windows system "ding" instead of a generated
# sine. This is the sound Windows emits for a rejected/unhandled key combo (e.g.
# Ctrl+Alt+Enter in a terminal) — the exact chime the user hears in Claude.
# Playing the OS sound guarantees a byte-for-byte match and, on equal-loudness
# grounds, is far easier to hear than a soft low tone. Reverses the earlier
# low/soft-sine preference at Ryan's explicit request (2026-07-30).
_SYSTEM_BEEP = {"start", "stop"}


_SOUND_CACHE: dict = {}


def _get_sound(name: str) -> bytes:
    """Lazily build and cache one notification blip. Built off the UI thread
    (callers are daemon threads), ~1ms of pure-python sine maths."""
    if name not in _SOUND_CACHE:
        recipes = {
            # Warm, low Glaido-style chime pair — soft two-note glide, kept in
            # the low-mid register (C4–E4, ~247–330 Hz) so it stays calm but is
            # actually audible on laptop speakers, which roll off badly below
            # ~250 Hz. Volume is up at 0.26 because low tones need more
            # amplitude to be heard for the same loudness (equal-loudness
            # contours); a small second harmonic (0.12) gives warmth without
            # brightness. Psychoacoustics: annoyance climbs steeply with pitch
            # and the harsh band is 2–5 kHz, so this register reads as gentle.
            # Start rises softly, stop mirrors it down (settle).
            # start: soft low Glaido-style rise — "listening"
            "start": _glide(262.0, 330.0, 95, volume=0.26,
                            peak_frac=0.45, harmonic=0.12),
            # stop: the falling mirror, a touch lower — "got it"
            "stop": _glide(330.0, 247.0, 105, volume=0.26,
                           peak_frac=0.55, harmonic=0.12),
            # done: a single soft, short tap in the same register
            "done": _glide(294.0, 300.0, 55, volume=0.16, harmonic=0.10),
            # error: one low, quiet buzz — noticeable, not alarming
            "error": _tone(220.0, 220, volume=0.18),
        }
        _SOUND_CACHE[name] = _wav_bytes(recipes[name])
    return _SOUND_CACHE[name]


def _play_sound(name: str) -> None:
    """Play a named blip (Windows only, never raises).

    Plays SYNCHRONOUSLY from memory. This is deliberate and required: winsound
    CANNOT play SND_MEMORY asynchronously — `SND_MEMORY | SND_ASYNC` raises
    "Cannot play asynchronously from memory". The old async flag therefore threw
    on EVERY cue and silently dropped to a raw 700 Hz winsound.Beep fallback, so
    none of the soft generated tones were ever heard (they looked fine in a
    stand-alone SND_MEMORY test, which masked the bug). Every caller already
    runs this on its own daemon thread, so a ~100 ms synchronous play blocks
    nothing that matters."""
    try:
        import winsound
        if name in _SYSTEM_BEEP:
            # MessageBeep(MB_OK) plays the "Default Beep" system sound — exactly
            # the ding Windows emits on an unhandled Ctrl+Alt+Enter. It is async
            # by nature (never blocks) and needs no in-memory wav, so the old
            # SND_MEMORY|SND_ASYNC crash-to-harsh-Beep bug cannot apply here.
            winsound.MessageBeep(winsound.MB_OK)
            return
        winsound.PlaySound(_get_sound(name), winsound.SND_MEMORY)
    except Exception:
        # Never fall back to winsound.Beep here — a raw square-wave beep is the
        # exact harsh sound these tones replace. Silent-fail instead; the visual
        # popup still signals state.
        pass


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
