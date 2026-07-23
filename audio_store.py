"""
Local per-dictation audio storage.

Each dictation's captured audio is saved as a 16-bit PCM mono WAV in
%APPDATA%\\FTC Whisper\\audio, named by the digits of the history row's
created_at timestamp — the same value that is the durable local/remote
identity in supabase_client, so a history row maps to its recording with
no schema change and the link survives remote merges.

Audio never leaves the machine: playback/retry work only for dictations
made on this device. Files are pruned oldest-first past a count/size cap.
"""

import os
import re
import threading
import wave
from typing import Optional

import numpy as np

_MAX_FILES = 400
_MAX_BYTES = 500 * 1024 * 1024

_write_lock = threading.Lock()


def audio_dir() -> str:
    app_data = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(app_data, "FTC Whisper", "audio")
    os.makedirs(folder, exist_ok=True)
    return folder


def _key(created_at: str) -> str:
    return re.sub(r"[^0-9]", "", created_at or "")[:20]


def path_for(created_at: str) -> str:
    return os.path.join(audio_dir(), _key(created_at) + ".wav")


def find(created_at: str) -> Optional[str]:
    """Path of the recording for a history row, or None if not on this device."""
    if not created_at:
        return None
    try:
        p = path_for(created_at)
        return p if os.path.isfile(p) else None
    except Exception:
        return None


def delete_for(created_at: str) -> None:
    try:
        p = find(created_at)
        if p:
            os.remove(p)
    except Exception:
        pass


class DictationAudioWriter:
    """Incremental WAV writer for one dictation.

    The Parakeet streaming path releases committed audio from the recorder
    ring to keep memory flat, so the full clip only ever exists on disk:
    each committed chunk is appended here the moment it is committed, and
    the tail is appended at stop. The file opens lazily on the first write
    (never on the hotkey-press path) and is renamed to its history key on
    finish, or deleted on abort.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._wf = None
        self._tmp_path = None
        self._rate = 0
        self._frames = 0
        self._dead = False

    def write(self, audio, rate: int) -> None:
        """Append a float32 mono chunk. Silently drops on any failure —
        audio persistence must never break the dictation itself."""
        if audio is None or len(audio) == 0:
            return
        try:
            with self._lock:
                if self._dead:
                    return
                if self._wf is None:
                    self._tmp_path = os.path.join(
                        audio_dir(), f".rec-{os.getpid()}-{id(self)}.tmp")
                    wf = wave.open(self._tmp_path, "wb")
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(max(1, int(rate)))
                    self._wf = wf
                    self._rate = max(1, int(rate))
                pcm = np.clip(np.asarray(audio, dtype=np.float32),
                              -1.0, 1.0)
                self._wf.writeframes((pcm * 32767.0).astype(np.int16).tobytes())
                self._frames += len(pcm)
        except Exception as e:
            print(f"[AudioStore] write failed (non-fatal): {e}")
            self.abort()

    def finish(self, created_at: str) -> Optional[str]:
        """Close and rename to the history key. Returns the final path."""
        with self._lock:
            self._dead = True
            wf, tmp = self._wf, self._tmp_path
            self._wf = None
        if wf is None:
            return None
        try:
            wf.close()
            final = path_for(created_at)
            os.replace(tmp, final)
            return final
        except Exception as e:
            print(f"[AudioStore] finish failed (non-fatal): {e}")
            try:
                os.remove(tmp)
            except Exception:
                pass
            return None

    def abort(self) -> None:
        with self._lock:
            self._dead = True
            wf, tmp = self._wf, self._tmp_path
            self._wf = None
        if wf is not None:
            try:
                wf.close()
            except Exception:
                pass
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def read(path: str):
    """Load a stored WAV as (float32 mono ndarray, rate)."""
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    return audio, rate


def waveform(path: str, buckets: int = 200):
    """(duration_seconds, per-bucket peak levels 0..1) for the progress bar.

    Peaks track when the user was actually speaking — silence stays near
    zero — so the fill maps playback time onto real speech.
    """
    with wave.open(path, "rb") as wf:
        rate = max(1, wf.getframerate())
        n = wf.getnframes()
        raw = wf.readframes(n)
    duration = n / float(rate)
    samples = np.abs(np.frombuffer(raw, dtype=np.int16).astype(np.float32))
    if len(samples) == 0:
        return duration, [0.0] * buckets
    step = max(1, len(samples) // buckets)
    peaks = [float(samples[i:i + step].max())
             for i in range(0, step * buckets, step)]
    while len(peaks) < buckets:
        peaks.append(0.0)
    top = max(max(peaks), 1.0)
    return duration, [p / top for p in peaks]


def prune(max_files: int = _MAX_FILES, max_bytes: int = _MAX_BYTES) -> None:
    """Delete oldest recordings past the count/size cap, plus stale temps."""
    try:
        folder = audio_dir()
        entries = []
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if name.endswith(".tmp"):
                # Leftover from a crash mid-dictation — never current: current
                # temps are younger than a dictation, pruning runs at startup.
                try:
                    os.remove(p)
                except OSError:
                    pass
                continue
            if name.endswith(".wav"):
                entries.append((st.st_mtime, st.st_size, p))
        entries.sort(reverse=True)  # newest first
        total = 0
        for i, (_m, size, p) in enumerate(entries):
            total += size
            if i >= max_files or total > max_bytes:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception as e:
        print(f"[AudioStore] prune failed (non-fatal): {e}")
