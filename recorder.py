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

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        input_device: str = "",
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.input_device = (input_device or "").strip()
        self._chunks: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._recording = False
        self._active_device_index: Optional[int] = None
        self._active_device_name: str = ""

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _audio_callback(
        self, indata: np.ndarray, _frames: int, _time_info, status
    ) -> None:
        """Called by sounddevice for each audio chunk."""
        if status:
            print(f"[Recorder] Stream status: {status}")
        with self._lock:
            if self._recording:
                self._chunks.append(indata.copy())

    def start(self) -> None:
        """Start recording with resilient input-device selection/fallback."""
        if self._recording:
            print("[Recorder] Already recording.")
            return

        with self._lock:
            self._chunks = []
            self._recording = True

        try:
            self._stream = self._open_best_input_stream()
            self._stream.start()
            where = self._active_device_name or "default input"
            print(f"[Recorder] Recording started ({where}).")
        except Exception as e:
            self._recording = False
            self._stream = None
            print(f"[Recorder] Failed to start recording: {e}")
            raise

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
                input_devices.append(
                    {
                        "index": i,
                        "name": dev["name"],
                        "channels": dev["max_input_channels"],
                        "sample_rate": dev["default_samplerate"],
                    }
                )
        return input_devices

    def _open_best_input_stream(self) -> sd.InputStream:
        candidates = self._candidate_device_indices()
        if not candidates:
            return self._open_stream_with_rates(None)

        last_err: Optional[Exception] = None
        for dev_index in candidates:
            try:
                stream = self._open_stream_with_rates(dev_index)
                info = sd.query_devices(dev_index)
                self._active_device_index = int(dev_index)
                self._active_device_name = str(info.get("name", f"device {dev_index}"))
                if self.input_device:
                    print(
                        f"[Recorder] Using input device '{self._active_device_name}' (#{dev_index})"
                    )
                return stream
            except Exception as e:
                last_err = e
                print(f"[Recorder] Device #{dev_index} unavailable ({e}); trying next.")

        self._active_device_index = None
        self._active_device_name = ""
        if last_err:
            raise RuntimeError(
                f"No working microphone device found: {last_err}"
            ) from last_err
        raise RuntimeError("No working microphone device found")

    def _open_stream_with_rates(self, dev_index: Optional[int]) -> sd.InputStream:
        rates = [int(self.sample_rate)]
        try:
            dev_info = (
                sd.query_devices(dev_index)
                if dev_index is not None
                else sd.query_devices(kind="input")
            )
            default_rate = int(
                float(dev_info.get("default_samplerate", self.sample_rate))
            )
            if default_rate > 0 and default_rate not in rates:
                rates.append(default_rate)
        except Exception:
            pass

        last_err: Optional[Exception] = None
        for rate in rates:
            try:
                return sd.InputStream(
                    samplerate=rate,
                    channels=self.channels,
                    dtype="float32",
                    callback=self._audio_callback,
                    blocksize=1024,
                    device=dev_index,
                )
            except Exception as e:
                last_err = e
                print(
                    f"[Recorder] Stream open failed (device={dev_index}, rate={rate}): {e}"
                )

        if last_err:
            raise last_err
        raise RuntimeError("Could not open audio stream")

    def _candidate_device_indices(self) -> list[int]:
        devices = self.get_input_devices()
        if not devices:
            return []

        candidates: list[int] = []
        preferred = self._resolve_preferred_input(devices)
        if preferred is not None:
            candidates.append(preferred)

        default_idx = self._get_default_input_index()
        if default_idx is not None:
            candidates.append(default_idx)

        candidates.extend(int(d["index"]) for d in devices)

        seen = set()
        ordered: list[int] = []
        for idx in candidates:
            if idx in seen:
                continue
            seen.add(idx)
            ordered.append(idx)
        return ordered

    def _resolve_preferred_input(self, devices: list[dict]) -> Optional[int]:
        if not self.input_device:
            return None

        token = self.input_device.strip()
        if token.isdigit():
            idx = int(token)
            if any(int(d["index"]) == idx for d in devices):
                return idx
            print(f"[Recorder] Config input_device #{idx} not found; using fallback.")
            return None

        lowered = token.lower()
        for d in devices:
            if d["name"].lower() == lowered:
                return int(d["index"])
        for d in devices:
            if lowered in d["name"].lower():
                return int(d["index"])

        print(f"[Recorder] Config input_device '{token}' not found; using fallback.")
        return None

    @staticmethod
    def _get_default_input_index() -> Optional[int]:
        try:
            pair = sd.default.device
            if pair is None:
                return None
            if isinstance(pair, (list, tuple)) and len(pair) >= 1:
                idx = pair[0]
            else:
                idx = pair
            idx = int(idx)
            if idx >= 0:
                return idx
        except Exception:
            pass
        try:
            info = sd.query_devices(kind="input")
            name = str(info.get("name", "")).lower()
            for idx, dev in enumerate(sd.query_devices()):
                if (
                    dev.get("max_input_channels", 0) > 0
                    and str(dev.get("name", "")).lower() == name
                ):
                    return int(idx)
        except Exception:
            pass
        return None
