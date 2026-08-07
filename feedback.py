"""
Visual and audio feedback for the application.
Plays sounds on recording start/stop and manages tray icon state changes.

The start/stop cues are a two-tap figure modelled on Glaido's, which is the
sound Ryan actually wanted. They are not guessed: Glaido ships its cues as two
24-bit/48kHz WAVs embedded in `%LOCALAPPDATA%\\Glaido\\glaido-core.exe`, and
those were extracted and measured (see _TAP_* below for exactly what came back).
Everything is a soft sine tap generated once in memory and played synchronously
via winsound.PlaySound — never winsound.Beep, which is a raw square wave at
full volume that reads as a long, harsh, high-pitched alarm.
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

_SR = 44100  # generated-blip sample rate: the taps have a ~6ms attack, and
             # 22050 smeared that transient enough to soften the "tick"


# ── Glaido-matched taps ───────────────────────────────────────────────────────
# Measured from the two WAVs embedded in glaido-core.exe (24-bit, 48 kHz, one
# at offset 19033401, the other at 19056203). Both are the SAME shape: two
# mallet-style sine taps, essentially pure (2nd harmonic is 0.4–1.8% of the
# fundamental, 3rd is 0.2%), each with a ~6ms attack and a fast exponential
# decay, the second tap louder than the first.
#
#   rise (79.0 ms):  296.6 Hz @ 0.100 peak, then 442.4 Hz @ 0.153, 33ms apart
#   fall (94.5 ms):  440.9 Hz @ 0.106 peak, then 287.8 Hz @ 0.147, 48ms apart
#
# So it is a perfect fifth (D4↔A4) played up for one cue and down for the
# other. Reproduced rather than shipped verbatim: their asset is theirs.
_TAP_ATTACK_MS = 6.0     # measured 5.5–6.5 across all four taps
_TAP_TAU_MS = 6.3        # decay constant: 10% of peak at ~13ms
_TAP_MS = 34.0           # audible length of one tap before it is inaudible
# Glaido's own cues peak at 0.15 (-16 dBFS), which is quiet on laptop speakers.
# Doubled here — same figure, same character, actually audible across a room.
_TAP_GAIN = 2.0


def _tap(freq: float, peak: float, ms: float = _TAP_MS) -> list:
    """One sine tap: raised-cosine attack, exponential decay. Pure sine, no
    harmonics added, because that is what the measurement showed."""
    n = int(_SR * ms / 1000)
    atk = max(1, int(_SR * _TAP_ATTACK_MS / 1000))
    tau = _SR * _TAP_TAU_MS / 1000
    out = []
    for i in range(n):
        if i < atk:
            env = 0.5 - 0.5 * math.cos(math.pi * i / atk)
        else:
            env = math.exp(-(i - atk) / tau)
        out.append(math.sin(2 * math.pi * freq * i / _SR) * peak * env)
    return out


def _two_tap(f1: float, p1: float, f2: float, p2: float,
             gap_ms: float, total_ms: float) -> list:
    """The Glaido figure: tap, short gap, second tap. Taps are summed rather
    than concatenated so the first one's tail rings under the second, exactly
    as it does in the original."""
    n = int(_SR * total_ms / 1000)
    buf = [0.0] * n
    for start_ms, freq, peak in ((0.0, f1, p1), (gap_ms, f2, p2)):
        off = int(_SR * start_ms / 1000)
        for i, s in enumerate(_tap(freq, peak * _TAP_GAIN)):
            if off + i < n:
                buf[off + i] += s
    return buf


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


def _recipe(name: str) -> list:
    """Sample list for one cue. One family, so the set sounds like one product:
    every cue is the same mallet tap, and only the pitches and count change."""
    if name == "start":
        # Rising fifth, D4 → A4 — "listening". Glaido's own figure.
        return _two_tap(296.6, 0.100, 442.4, 0.153, 33.0, 79.0)
    if name == "stop":
        # The mirror, A4 → D4 — "got it".
        return _two_tap(440.9, 0.106, 287.8, 0.147, 48.0, 94.5)
    if name == "done":
        # Kept for callers that want an explicit "landed" tap, but the normal
        # dictation path no longer fires it — see transcription_complete.
        return _tap(442.4, 0.085 * _TAP_GAIN)
    # error: two low taps a semitone apart — reads as "no" without alarming.
    return _two_tap(233.1, 0.130, 220.0, 0.130, 90.0, 190.0)


def _get_sound(name: str) -> bytes:
    """Lazily build and cache one notification blip. Built off the UI thread
    (callers are daemon threads), a few ms of pure-python sine maths."""
    if name not in _SOUND_CACHE:
        _SOUND_CACHE[name] = _wav_bytes(_recipe(name))
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
        # NB: start/stop used to call MessageBeep(MB_OK) — the Windows "ding".
        # It is loud and unmistakable, but it is also the sound of an error in
        # every other app, and users silence it system-wide. The Glaido-matched
        # taps replace it; do not reintroduce MessageBeep here.
        winsound.PlaySound(_get_sound(name), winsound.SND_MEMORY)
    except Exception:
        # Never fall back to winsound.Beep here — a raw square-wave beep is the
        # exact harsh sound these tones replace. Silent-fail instead; the visual
        # popup still signals state.
        pass


class Feedback:
    """
    Provides audio and visual feedback for app state changes.

    Audio (soft mallet taps, not raw beeps) — two cues per dictation:
        - Rising two-tap figure (D4→A4) on recording start
        - Falling two-tap figure (A4→D4) on recording stop
        - Nothing on transcription complete; see transcription_complete()

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
        """Called when transcription is done and text has been injected.

        Deliberately SILENT. Two cues per dictation, not three: the falling
        stop cue already says "got it", and with Parakeet's sub-second latency
        a third tap lands under half a second after it, which reads as clutter
        rather than confirmation. The text appearing is its own confirmation,
        and the popup covers the case where it did not."""
        if self.on_icon_change:
            self.on_icon_change("idle")

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
