"""
Microphone audio recorder using sounddevice.
Records audio in real-time chunks and returns a numpy array when stopped.
"""

import threading
import numpy as np
import sounddevice as sd
from typing import Optional


class Recorder:
    """
    Thread-safe microphone recorder.

    Usage:
        recorder = Recorder(sample_rate=16000)
        recorder.start()
        # ... user speaks ...
        audio = recorder.stop()  # returns numpy array of float32 samples
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._chunks: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _audio_callback(self, indata: np.ndarray, _frames: int, _time_info, status) -> None:
        """Called by sounddevice for each audio chunk."""
        if status:
            print(f"[Recorder] Stream status: {status}")
        with self._lock:
            if self._recording:
                self._chunks.append(indata.copy())

    def start(self) -> None:
        """Start recording from the default microphone."""
        if self._recording:
            print("[Recorder] Already recording.")
            return

        with self._lock:
            self._chunks = []
            self._recording = True

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            callback=self._audio_callback,
            blocksize=1024,
        )
        self._stream.start()
        print("[Recorder] Recording started.")

    def get_current_audio(self, max_seconds: float = 10.0) -> Optional[np.ndarray]:
        """Return a snapshot of recent audio without stopping the stream.
        Only the last max_seconds are returned to avoid O(n) growth on long recordings."""
        with self._lock:
            if not self._chunks:
                return None
            # Work from the tail so concatenation cost stays bounded regardless
            # of how long the user has been recording.
            max_samples = int(self.sample_rate * max_seconds)
            samples_per_chunk = self._chunks[0].shape[0]
            max_chunks = max(1, max_samples // max(samples_per_chunk, 1) + 1)
            recent = self._chunks[-max_chunks:]
            return np.concatenate(recent, axis=0).flatten()

    def stop(self) -> Optional[np.ndarray]:
        """
        Stop recording and return the captured audio as a 1D float32 numpy array.
        Returns None if no audio was captured.
        """
        if not self._recording:
            print("[Recorder] Not currently recording.")
            return None

        self._recording = False

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                print(f"[Recorder] Error closing stream: {e}")
            finally:
                self._stream = None

        with self._lock:
            if not self._chunks:
                print("[Recorder] No audio captured.")
                return None
            audio = np.concatenate(self._chunks, axis=0).flatten()
            self._chunks = []

        duration = len(audio) / self.sample_rate
        print(f"[Recorder] Captured {duration:.1f}s of audio.")
        return audio

    def get_input_devices(self) -> list[dict]:
        """List available input audio devices."""
        devices = sd.query_devices()
        input_devices = []
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                input_devices.append({
                    "index": i,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "sample_rate": dev["default_samplerate"],
                })
        return input_devices
