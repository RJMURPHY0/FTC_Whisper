"""
Microphone audio recorder using sounddevice.

Two capture modes:
  Warm (default): a persistent input stream runs continuously, feeding a small
  pre-roll ring buffer (~1.5s). start() is then instant — it just flips a flag
  and seeds the recording with the last ~0.35s of pre-roll, so the first
  syllable is never lost to stream-open latency (~50-300ms on Windows) and
  speech that begins ON the go-beep is fully captured.

  Cold (fallback): the stream is opened on start() and closed on stop(),
  exactly like the pre-1.6 behaviour. Used when the warm stream can't be
  opened (device unplugged, exclusive-mode conflict) or warm mode is disabled
  in settings.

Recording keeps an absolute sample counter so the streaming transcription
session can ask for ranges (get_audio_range) and release committed audio
(drop_audio_before) without re-copying the whole buffer every tick.
"""

import threading
import numpy as np
import sounddevice as sd
from collections import deque
from typing import Optional

_PREROLL_KEEP_SECONDS = 1.5   # ring buffer length while idle
_PREROLL_SEED_SECONDS = 0.35  # how much pre-hotkey audio to prepend to a recording


class Recorder:
    """
    Thread-safe microphone recorder.

    Usage:
        recorder = Recorder(sample_rate=16000)
        recorder.set_warm(True)   # optional persistent stream
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
        self._chunks_samples: int = 0     # samples currently held in _chunks
        self._chunks_offset: int = 0      # samples dropped from the front (absolute base)
        self._preroll: deque = deque()    # ring buffer of idle-time chunks (warm mode)
        self._preroll_samples: int = 0
        self._stream: Optional[sd.InputStream] = None
        self._warm_enabled = False
        self._warm_stream_is_open = False
        self._warm_restart_pending = False  # device changed mid-recording
        self._lock = threading.Lock()
        # Guards stream open/close so a concurrent start()/stop()/monitor can
        # never double-close or leak the underlying PortAudio stream. Separate
        # from _lock (which the high-frequency audio callback holds) so opening
        # a stream never blocks — or is blocked by — the callback.
        self._stream_lifecycle_lock = threading.Lock()
        self._recording = False
        self._active_device_index: Optional[int] = None
        self._active_device_name: str = ""
        self._active_sample_rate: int = sample_rate
        self._last_rms: float = 0.0
        self._last_peak: float = 0.0
        self._monitor_rms: float = 0.0
        self._monitor_peak: float = 0.0
        self._monitor_active = False

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def active_sample_rate(self) -> int:
        return int(self._active_sample_rate or self.sample_rate)

    @property
    def total_recorded_samples(self) -> int:
        """Absolute number of samples captured since start() (monotonic)."""
        with self._lock:
            return self._chunks_offset + self._chunks_samples

    @property
    def dropped_samples(self) -> int:
        """Absolute sample position of the start of the retained buffer."""
        with self._lock:
            return self._chunks_offset

    # ------------------------------------------------------------------
    # Stream callbacks
    # ------------------------------------------------------------------

    def _audio_callback(
        self, indata: np.ndarray, _frames: int, _time_info, status
    ) -> None:
        """Called by sounddevice for each audio chunk (recording OR warm-idle)."""
        if status:
            print(f"[Recorder] Stream status: {status}")
        mono = indata[:, 0] if indata.ndim > 1 else indata
        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        data = indata.copy()
        n = data.shape[0]
        with self._lock:
            self._last_rms = rms
            self._last_peak = peak
            if self._recording:
                self._chunks.append(data)
                self._chunks_samples += n
            elif self._warm_enabled:
                self._preroll.append(data)
                self._preroll_samples += n
                max_keep = int(self.active_sample_rate * _PREROLL_KEEP_SECONDS)
                while self._preroll_samples > max_keep and len(self._preroll) > 1:
                    dropped = self._preroll.popleft()
                    self._preroll_samples -= dropped.shape[0]

    def _monitor_callback(
        self, indata: np.ndarray, _frames: int, _time_info, status
    ) -> None:
        """Level-only callback for the Test Mic stream — never stores audio, so
        a recording started while Test Mic runs can't get interleaved chunks."""
        mono = indata[:, 0] if indata.ndim > 1 else indata
        with self._lock:
            self._monitor_rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
            self._monitor_peak = float(np.max(np.abs(mono))) if mono.size else 0.0

    # ------------------------------------------------------------------
    # Warm (persistent) stream
    # ------------------------------------------------------------------

    def set_warm(self, enabled: bool) -> None:
        """Enable/disable the always-open input stream with pre-roll buffer."""
        self._warm_enabled = bool(enabled)
        if enabled:
            self._ensure_warm_stream()
        else:
            self._close_stream_if_idle()

    def restart_warm(self) -> None:
        """Re-open the warm stream (after an input-device change)."""
        if not self._warm_enabled:
            return
        with self._stream_lifecycle_lock:
            if self._recording:
                # Can't swap streams mid-recording — stop() sees this flag,
                # closes the old-device stream and reopens on the new device.
                self._warm_restart_pending = True
                return
            self._close_stream_locked()
        self._ensure_warm_stream()

    def _ensure_warm_stream(self) -> bool:
        """Open the persistent stream if not already running. Returns success."""
        with self._stream_lifecycle_lock:
            if self._stream is not None:
                try:
                    if self._stream.active:
                        return True
                except Exception:
                    pass
                self._close_stream_locked()
            try:
                self._stream = self._open_best_input_stream()
                self._stream.start()
                self._warm_stream_is_open = True
                where = self._active_device_name or "default input"
                print(f"[Recorder] Warm mic stream open ({where}, {self.active_sample_rate} Hz).")
                return True
            except Exception as e:
                self._stream = None
                self._warm_stream_is_open = False
                print(f"[Recorder] Warm stream unavailable ({e}) — cold-open per recording.")
                return False

    def _close_stream_locked(self) -> None:
        stream = self._stream
        self._stream = None
        self._warm_stream_is_open = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                print(f"[Recorder] Error closing stream: {e}")

    def _close_stream_if_idle(self) -> None:
        with self._stream_lifecycle_lock:
            if not self._recording:
                self._close_stream_locked()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start recording. Instant when the warm stream is running."""
        with self._lock:
            if self._recording:
                # Defensive: a previous session that was never stopped must not
                # leak its audio into this one.
                print("[Recorder] Already recording — resetting buffer.")
                self._chunks = []
                self._chunks_samples = 0
                self._chunks_offset = 0
                return

        warm_ready = self._warm_enabled and self._ensure_warm_stream()

        if warm_ready:
            with self._lock:
                self._chunks = []
                self._chunks_samples = 0
                self._chunks_offset = 0
                # Seed with the last ~0.35s of pre-roll so speech that started
                # slightly before the hotkey (or during it) is captured.
                seed_max = int(self.active_sample_rate * _PREROLL_SEED_SECONDS)
                seeded = 0
                seed_chunks: list[np.ndarray] = []
                for chunk in reversed(self._preroll):
                    seed_chunks.append(chunk)
                    seeded += chunk.shape[0]
                    if seeded >= seed_max:
                        break
                for chunk in reversed(seed_chunks):
                    self._chunks.append(chunk)
                    self._chunks_samples += chunk.shape[0]
                self._preroll.clear()
                self._preroll_samples = 0
                self._last_rms = 0.0
                self._last_peak = 0.0
                self._recording = True
            print(
                f"[Recorder] Recording started instantly (warm, "
                f"{self._active_device_name or 'default input'}, {self.active_sample_rate} Hz)."
            )
            return

        # Cold path — open the stream now (legacy behaviour)
        with self._lock:
            self._chunks = []
            self._chunks_samples = 0
            self._chunks_offset = 0
            self._recording = True
            self._active_sample_rate = self.sample_rate
            self._last_rms = 0.0
            self._last_peak = 0.0

        try:
            with self._stream_lifecycle_lock:
                self._stream = self._open_best_input_stream()
                self._stream.start()
            where = self._active_device_name or "default input"
            print(
                f"[Recorder] Recording started ({where}, {self.active_sample_rate} Hz)."
            )
        except Exception as e:
            with self._lock:
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
            max_samples = int(self.active_sample_rate * max_seconds)
            samples_per_chunk = self._chunks[0].shape[0]
            max_chunks = max(1, max_samples // max(samples_per_chunk, 1) + 1)
            recent = self._chunks[-max_chunks:]
            return np.concatenate(recent, axis=0).flatten()

    def get_audio_range(self, start_sample: int) -> Optional[np.ndarray]:
        """Return audio from absolute sample position start_sample to now."""
        with self._lock:
            if not self._chunks:
                return None
            rel = start_sample - self._chunks_offset
            if rel <= 0:
                chunks = list(self._chunks)
                skip = 0
            else:
                pos = 0
                chunks = []
                skip = 0
                for i, c in enumerate(self._chunks):
                    n = c.shape[0]
                    if pos + n <= rel:
                        pos += n
                        continue
                    if not chunks:
                        skip = rel - pos
                    chunks.append(c)
                    pos += n
                if not chunks:
                    return None
        audio = np.concatenate(chunks, axis=0).flatten()
        return audio[skip:] if skip else audio

    def drop_audio_before(self, abs_sample: int) -> None:
        """Discard whole chunks that lie entirely before abs_sample (memory bound
        for long streamed recordings; committed audio is never needed again)."""
        with self._lock:
            while self._chunks:
                n = self._chunks[0].shape[0]
                if self._chunks_offset + n > abs_sample:
                    break
                self._chunks_offset += n
                self._chunks_samples -= n
                self._chunks.pop(0)

    def stop(self) -> Optional[np.ndarray]:
        """
        Stop recording and return the captured audio as a 1D float32 numpy array
        (only audio since the last drop_audio_before call). Returns None if no
        audio was captured. In warm mode the stream stays open for the next
        recording; in cold mode it is closed.
        """
        with self._lock:
            if not self._recording:
                print("[Recorder] Not currently recording.")
                return None
            self._recording = False

        # Keep the stream open only when warm mode is (still) on and no device
        # change is pending. Closing here also covers warm mode being disabled
        # MID-recording — otherwise the stale open stream would double-feed
        # _chunks alongside the next cold-opened stream.
        keep_open = (
            self._warm_enabled
            and self._warm_stream_is_open
            and not self._warm_restart_pending
        )
        if not keep_open:
            with self._stream_lifecycle_lock:
                self._close_stream_locked()
            if self._warm_enabled:
                self._warm_restart_pending = False
                threading.Thread(
                    target=self._ensure_warm_stream, daemon=True, name="warm-reopen"
                ).start()

        with self._lock:
            if not self._chunks:
                print("[Recorder] No audio captured.")
                return None
            audio = np.concatenate(self._chunks, axis=0).flatten()
            self._chunks = []
            self._chunks_samples = 0
            self._chunks_offset = 0

        duration = len(audio) / max(self.active_sample_rate, 1)
        print(f"[Recorder] Captured {duration:.1f}s of audio.")
        return audio

    # ------------------------------------------------------------------
    # Mic test monitor
    # ------------------------------------------------------------------

    def start_monitor(self, device_name: str = "") -> None:
        """Open a level-only stream for the Test Mic feature. Uses a dedicated
        callback so it can never contaminate a recording's audio buffer."""
        self.stop_monitor()
        old_device = self.input_device
        old_index = self._active_device_index
        old_name = self._active_device_name
        old_rate = self._active_sample_rate
        self.input_device = device_name.strip()
        # Force re-resolution: the cached index would silently ignore the
        # explicitly requested device.
        self._active_device_index = None
        self._active_device_name = ""
        try:
            self._monitor_stream = self._open_best_input_stream(
                callback=self._monitor_callback
            )
            self._monitor_stream.start()
            self._monitor_active = True
        except Exception:
            self.input_device = old_device
            self._active_device_index = old_index
            self._active_device_name = old_name
            self._active_sample_rate = old_rate
            self._monitor_stream = None
            raise
        # Restore recording-device state — monitor must not pollute the cache
        self.input_device = old_device
        self._active_device_index = old_index
        self._active_device_name = old_name
        self._active_sample_rate = old_rate

    def stop_monitor(self) -> None:
        """Close the mic-test monitor stream if open."""
        self._monitor_active = False
        stream = getattr(self, "_monitor_stream", None)
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            self._monitor_stream = None
        with self._lock:
            self._monitor_rms = 0.0
            self._monitor_peak = 0.0

    def get_input_devices(self) -> list[dict]:
        """List available input audio devices, one clean entry per mic.

        Windows exposes each physical microphone under several host APIs (MME,
        DirectSound, WASAPI, WDM-KS). MME truncates names to 31 characters, so
        we prefer the non-MME host APIs which give full, readable names, then
        deduplicate by name. Never raises — returns [] on failure so the UI can
        fall back to the system default instead of crashing."""
        try:
            devices = sd.query_devices()
        except Exception as e:
            print(f"[Recorder] query_devices failed: {e}")
            return []
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            hostapis = []

        def _api_name(idx):
            if 0 <= idx < len(hostapis):
                return str(hostapis[idx].get("name", "")).lower()
            return ""

        inputs = [(i, d) for i, d in enumerate(devices)
                  if d["max_input_channels"] > 0]
        # Prefer non-MME devices (full names); fall back to everything if that
        # leaves us with nothing (some minimal systems only expose MME).
        non_mme = [(i, d) for i, d in inputs if "mme" not in _api_name(d["hostapi"])]
        pool = non_mme or inputs

        seen: set = set()
        result: list[dict] = []
        for i, dev in pool:
            name = str(dev["name"]).strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            result.append({
                "index": i,
                "name": name,
                "channels": dev["max_input_channels"],
                "sample_rate": dev["default_samplerate"],
            })
        print(f"[Recorder] Enumerated {len(result)} input device(s): "
              f"{[d['name'] for d in result]}")
        return result

    def get_live_levels(self) -> tuple[float, float]:
        """Return most recent (rms, peak) levels. While the Test Mic monitor is
        active its levels take priority (they reflect the selected device)."""
        with self._lock:
            if self._monitor_active:
                return self._monitor_rms, self._monitor_peak
            return self._last_rms, self._last_peak

    # ------------------------------------------------------------------
    # Stream opening / device selection
    # ------------------------------------------------------------------

    def _open_best_input_stream(self, callback=None) -> sd.InputStream:
        # Fast path: reuse the last known good device index — skips sd.query_devices()
        # enumeration on every recording after the first (~20-100ms saved per press).
        if self._active_device_index is not None:
            try:
                stream = self._open_stream_with_rates(
                    self._active_device_index, callback=callback
                )
                return stream
            except Exception:
                # Device disappeared or changed — fall through to full enumeration
                self._active_device_index = None
                self._active_device_name = ""

        candidates = self._candidate_device_indices()
        if not candidates:
            return self._open_stream_with_rates(None, callback=callback)

        last_err: Optional[Exception] = None
        for dev_index in candidates:
            try:
                stream = self._open_stream_with_rates(dev_index, callback=callback)
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

    def _open_stream_with_rates(
        self, dev_index: Optional[int], callback=None
    ) -> sd.InputStream:
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
                stream = sd.InputStream(
                    samplerate=rate,
                    channels=self.channels,
                    dtype="float32",
                    callback=callback or self._audio_callback,
                    blocksize=1024,
                    device=dev_index,
                )
                if callback is None:
                    self._active_sample_rate = int(rate)
                return stream
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
