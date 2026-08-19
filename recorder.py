"""
Microphone audio recorder using sounddevice.

Two capture modes:
  Warm (default): a persistent input stream runs continuously, feeding a small
  pre-roll ring buffer (~1.5s). start() is then instant — it just flips a flag
  and seeds the recording with the last ~0.8s of pre-roll, so the first
  syllable is never lost to stream-open latency (~50-300ms on Windows) and
  speech that begins ON the hotkey press is fully captured.

  Cold (fallback): the stream is opened on start() and closed on stop(),
  exactly like the pre-1.6 behaviour. Used when the warm stream can't be
  opened (device unplugged, exclusive-mode conflict) or warm mode is disabled
  in settings.

Recording keeps an absolute sample counter so the streaming transcription
session can ask for ranges (get_audio_range) and release committed audio
(drop_audio_before) without re-copying the whole buffer every tick.
"""

import threading
import time
import numpy as np
import sounddevice as sd
from collections import deque
from typing import Optional

_PREROLL_KEEP_SECONDS = 1.5   # ring buffer length while idle
_PREROLL_SEED_SECONDS = 0.8   # pre-hotkey audio prepended to a recording: covers
                              # hotkey-dispatch latency plus speech that starts
                              # on (or a beat before) the press. 0.35 clipped
                              # first words when the press and the first
                              # syllable landed together under load; 0.6 still
                              # occasionally clipped the first word or two, so
                              # the margin is 0.8 (the ring keeps 1.5s).

# A healthy input stream delivers a callback every blocksize/rate seconds
# (~21-64ms). If none arrived for this long the stream is considered dead —
# WASAPI/MME streams silently stop calling back after a device removal,
# default-device switch, audio-engine restart, or sleep/resume, while
# stream.active happily stays True.
_HEARTBEAT_STALE_SECONDS = 1.2
# How long start() waits for the first fresh callback before abandoning the
# warm stream and cold-opening a new one.
_START_VERIFY_SECONDS = 0.45
_WATCHDOG_INTERVAL = 2.0      # idle heartbeat check cadence. Kept short: a
                              # press landing on a dead warm stream loses the
                              # first words outright, so the window in which a
                              # stream can sit dead must be as small as the
                              # (near-free) heartbeat check allows.
_DEVICE_REFRESH_TICKS = 60    # full PortAudio refresh every N watchdog ticks (~120s).
                              # Only needed to follow a *default-device change*; a
                              # dead stream is recovered separately within ~5s and
                              # the evidence probe covers a mic that stops hearing
                              # the user — so a slower cadence just means fewer
                              # teardowns that could clip a keypress's first words.
_DEVICE_REFRESH_QUIET_SECONDS = 20.0  # never run the periodic default-follow within
                              # this long of a dictation (start OR stop): the ~0.1-0.3s
                              # PortAudio re-init gap must never coincide with a press,
                              # which is what dropped the first word or two.

# A live mic ALWAYS carries a noise floor, so bit-exact digital silence means
# the stream has stopped carrying its device. WASAPI leaves a stream callbacking
# with zero-filled buffers when the Windows Audio service dies and is restarted
# under it (observed 2026-08-07: Audiosrv hung, was killed, restarted 60s later,
# and the app sat on the dead stream for hours). The heartbeat stays fresh and
# stream.active stays True through all of that, so _stream_looks_alive() — which
# only knows about those two — calls it healthy forever and the watchdog never
# fires. This is the second, content-based liveness signal.
_SILENT_STREAM_SECONDS = 25.0
# Backoff between silence-triggered reopens. A hardware-muted mic also delivers
# exact zeros, and must settle into an occasional retry rather than a reopen
# every _SILENT_STREAM_SECONDS. Reset as soon as any real signal arrives.
_SILENCE_RETRY_BACKOFF = (60.0, 120.0, 300.0, 600.0)

# Evidence probe: when the chosen mic has been alive but voice-free for this
# long into a recording, briefly sample the other real mics to learn whether a
# different one is actually hearing the user (the not-worn-headset case).
_PROBE_TRIGGER_SECONDS = 1.6  # voiced silence before probing other mics
_PROBE_WINDOW_SECONDS = 0.9   # how long the probe streams listen
_PROBE_MAX_CANDIDATES = 4     # cap simultaneous probe streams
_PROBE_MIN_RMS = 0.006        # absolute speech-evidence floor for a candidate
_PROBE_MARGIN = 4.0           # candidate must beat the primary by this factor
_PROBE_SUSTAIN_SECONDS = 0.3  # candidate must stay above the floor this long —
                              # a single 20ms transient (chair squeak, cough,
                              # AGC hiss) must never count as speech evidence


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
        # Voiced-time meter: samples in the current recording whose chunk RMS
        # clearly cleared the adaptive noise floor — i.e. actual speech, not
        # pauses or room noise. Powers the real words-per-minute stat.
        self._voiced_samples: int = 0
        self._noise_floor: float = 0.0015
        self._monitor_rms: float = 0.0
        self._monitor_peak: float = 0.0
        self._monitor_active = False
        # Aux capture (popup voice-prompt mic): audio is taken from the SAME
        # stream/callback as recordings, so the watchdog's PortAudio re-init
        # can never run underneath it and it always uses the configured device.
        self._aux_chunks: list[np.ndarray] = []
        self._aux_active = False
        self._aux_cold_opened = False
        # Heartbeat of the recording/warm stream: monotonic time of the last
        # audio callback. Seeded on stream open so a just-opened stream counts
        # as healthy before its first callback arrives.
        self._last_callback_ts: float = 0.0
        self._watchdog_started = False
        self._watchdog_lock = threading.Lock()
        # Evidence-based mic selection (auto mode only). A device that sat
        # silent through a dictation while another mic clearly heard the user
        # is demoted for this session; the mic that heard the speech is
        # preferred outright. Cleared when the device topology changes (user
        # plugged/unplugged something) or on a manual device change — so a
        # headset put back on gets a fresh chance instead of a permanent ban.
        self._demoted_devices: set[str] = set()
        self._demotion_topology: frozenset = frozenset()
        self._evidence_preferred: str = ""
        self._probe_active = False        # probe streams open — watchdog must
                                          # not re-init PortAudio underneath them
        self._probe_done = False          # one probe per recording
        self._mic_advice: Optional[dict] = None
        self._recording_started_ts: float = 0.0
        # Monotonic time of the last record start OR stop. The periodic
        # default-follow bounce is deferred while this is recent so a stream
        # teardown can never coincide with an imminent/just-finished dictation
        # and clip its first words.
        self._last_record_activity_ts: float = 0.0
        # Set by _ensure_warm_stream when it actually (re)opened a stream, so
        # start() can tell an instant press (stream was live, pre-roll seeded,
        # zero loss) from one that had to recover — the recovering press is the
        # ONLY path that can drop the first words, and the app reports it to
        # the fleet log so those events are visible instead of anecdotal.
        self._last_warm_reopened = False
        self.last_start_recovery: Optional[dict] = None
        # Start of the current unbroken run of bit-exact-silent callbacks, or
        # 0.0 when the last buffer carried signal. See _SILENT_STREAM_SECONDS.
        self._silent_run_started_ts: float = 0.0
        self._silence_recovery_count: int = 0
        self._silence_retry_after: float = 0.0
        # Set when the watchdog reopened a silently-dead stream. Polled and
        # cleared by the app for the fleet log, same contract as
        # last_start_recovery above.
        self.last_silence_recovery: Optional[dict] = None
        # Wall-clock seconds of the most recent recording, set by stop().
        self.last_recording_seconds: float = 0.0

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def warm_capture_live(self) -> bool:
        """True when the warm stream is open and demonstrably carrying audio
        RIGHT NOW, so the pre-roll ring already holds the moment of a keypress.

        Read on the hotkey thread to decide whether the start cue can be played
        immediately: when this is True the mic is not "about to" start, it is
        already recording the room, so the cue is honest the instant the key
        goes down. When it is False the press has to open or recover a stream
        first, and the cue stays where it was — after start() proves audio is
        flowing — because telling the user to speak into a stream that is not
        up yet is exactly how first words get lost.

        Cheap and non-blocking on purpose: it must never add latency to the
        very path it exists to make instant."""
        try:
            return bool(self._warm_enabled and self._stream_looks_alive())
        except Exception:
            return False

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

    @property
    def voiced_seconds(self) -> float:
        """Seconds of actual speech (energy above the noise floor) captured
        since start(). Pauses and silence are excluded by construction."""
        with self._lock:
            return self._voiced_samples / float(self.active_sample_rate or 1)

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
        now = time.monotonic()
        with self._lock:
            self._last_callback_ts = now
            self._last_rms = rms
            self._last_peak = peak
            # Bit-exact silence run (see _SILENT_STREAM_SECONDS). A real device
            # always carries a noise floor, so an unbroken run of exact zeros
            # means this stream is no longer carrying one — even though it is
            # still calling back. An empty buffer carries no information either
            # way and must not count as silence.
            if mono.size:
                if peak > 0.0:
                    self._silent_run_started_ts = 0.0
                    if self._silence_recovery_count:
                        # Real audio is flowing again — a later failure deserves
                        # a prompt recovery, not the tail of an old backoff.
                        self._silence_recovery_count = 0
                        self._silence_retry_after = 0.0
                elif self._silent_run_started_ts == 0.0:
                    self._silent_run_started_ts = now
            if self._aux_active:
                self._aux_chunks.append(data)
            if self._recording:
                self._chunks.append(data)
                self._chunks_samples += n
                # Adaptive floor: drops fast onto quiet, creeps up slowly, so
                # a noisy room raises the bar instead of counting as speech.
                if rms < self._noise_floor:
                    self._noise_floor = max(
                        1e-5, 0.8 * self._noise_floor + 0.2 * rms)
                else:
                    self._noise_floor = min(self._noise_floor * 1.005, 0.02)
                if rms > max(0.0025, self._noise_floor * 2.5):
                    self._voiced_samples += n
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
            self._start_watchdog()
        else:
            self._close_stream_if_idle()

    def mark_warm_pending(self) -> None:
        """Set the warm flag ahead of an async set_warm(True) call. A start()
        that races the open must see warm enabled and wait on the in-flight
        stream (via _stream_lifecycle_lock inside _ensure_warm_stream) instead
        of falling to the cold path, which has no pre-roll and loses whatever
        the user says while its own synchronous open grinds through device
        enumeration on a fresh machine."""
        self._warm_enabled = True

    def restart_warm(self) -> None:
        """Re-open the warm stream (after an input-device change)."""
        if not self._warm_enabled:
            return
        if self.is_recording:
            # Can't swap streams mid-recording — stop() sees this flag,
            # closes the old-device stream and reopens on the new device.
            self._warm_restart_pending = True
            return
        # force_fresh re-inits PortAudio so a device plugged in after launch
        # is actually visible to the enumeration.
        self._ensure_warm_stream(force_fresh=True)

    def _heartbeat_age(self) -> float:
        """Seconds since the last audio callback (inf if none ever fired)."""
        with self._lock:
            ts = self._last_callback_ts
        return (time.monotonic() - ts) if ts > 0 else float("inf")

    def _stream_looks_alive(self) -> bool:
        """True when the current stream both claims to be active AND has
        delivered a callback recently. A stream whose device vanished (or died
        across sleep/resume) keeps active=True but never calls back again.

        Deliberately says nothing about stream CONTENT — see
        _stream_is_silently_dead(), which is checked only by the idle watchdog.
        This method is on the hotkey press path (start() → _ensure_warm_stream),
        where returning False forces a reopen and risks the first words, so a
        merely-silent mic must never fail it."""
        stream = self._stream
        if stream is None:
            return False
        try:
            if not stream.active:
                return False
        except Exception:
            return False
        return self._heartbeat_age() < _HEARTBEAT_STALE_SECONDS

    def _silent_run_seconds(self) -> float:
        """Seconds of unbroken bit-exact-silent callbacks (0.0 when the stream
        is currently carrying signal)."""
        with self._lock:
            started = self._silent_run_started_ts
        return (time.monotonic() - started) if started > 0.0 else 0.0

    def _stream_is_silently_dead(self) -> bool:
        """True when the stream keeps calling back but has carried nothing but
        bit-exact silence long enough that it is clearly no longer attached to
        a device (the audio-engine-restart case _SILENT_STREAM_SECONDS covers).

        Kept out of _stream_looks_alive() on purpose: that is consulted on the
        press path, and a hardware-muted mic also reads as exact zeros. Failing
        liveness there would cold-open the stream under a user who is already
        speaking and cost them their first words — a worse outcome than the
        silence itself. Only the idle watchdog acts on this."""
        if self._silent_run_seconds() < _SILENT_STREAM_SECONDS:
            return False
        return time.monotonic() >= self._silence_retry_after

    def _recover_silent_stream(self) -> None:
        """Reopen a stream that is callbacking but carrying only silence.

        Held off within _DEVICE_REFRESH_QUIET_SECONDS of a dictation for the
        same reason as the periodic default-follow: the PortAudio re-init gap
        must never coincide with a press. The pre-roll is never carried across
        (it is all silence, and seeding a recording from it is exactly how a
        dead mic used to feed hallucinated fillers into the transcript)."""
        if (time.monotonic() - self._last_record_activity_ts
                <= _DEVICE_REFRESH_QUIET_SECONDS):
            return
        silent_for = self._silent_run_seconds()
        device = self._active_device_name
        print(f"[Recorder] Watchdog: stream silent for {silent_for:.0f}s "
              f"while still callbacking — reopening.")
        # Arm the backoff BEFORE reopening: a genuinely muted mic starts a new
        # silent run the moment the fresh stream opens.
        idx = min(self._silence_recovery_count, len(_SILENCE_RETRY_BACKOFF) - 1)
        self._silence_recovery_count += 1
        self._silence_retry_after = (
            time.monotonic() + _SILENCE_RETRY_BACKOFF[idx])
        with self._lock:
            self._silent_run_started_ts = 0.0
        self._ensure_warm_stream(force_fresh=True)
        self.last_silence_recovery = {
            "device": device,
            "silent_seconds": round(silent_for, 1),
            "attempt": self._silence_recovery_count,
        }

    def _refresh_portaudio(self) -> None:
        """Re-initialise PortAudio so the device list reflects reality.
        PortAudio snapshots the device topology at init — devices added,
        removed, or made default afterwards are invisible (and stale indices
        can 'successfully' reopen a dead endpoint) until a re-init. Must only
        be called with ALL of our streams closed."""
        if self._monitor_active or self._probe_active:
            return
        try:
            sd._terminate()
            time.sleep(0.05)
            sd._initialize()
            print("[Recorder] PortAudio device list refreshed.")
        except Exception as e:
            print(f"[Recorder] PortAudio refresh failed (continuing): {e}")
        # Cached index is meaningless across a re-init.
        self._active_device_index = None
        self._active_device_name = ""

    def _ensure_warm_stream(self, force_fresh: bool = False,
                            preserve_preroll: bool = False,
                            skip_refresh: bool = False) -> bool:
        """Open (or verify) the persistent stream. Returns success.

        force_fresh=True closes any existing stream and re-initialises
        PortAudio first — used by the watchdog to recover dead streams and to
        follow Windows default-device changes.

        preserve_preroll=True keeps the pre-roll ring across the reopen when the
        device and sample rate come back unchanged (a periodic default-follow
        that found the same device). Clearing it there would open a
        seed-length window in which a keypress lands with no pre-roll and the
        first words are lost.

        skip_refresh=True skips the PortAudio re-init when a stale stream has
        to be reopened — the press path uses this because every ~100ms of that
        re-init is spoken audio lost forever. The common stale causes (audio
        engine restart, sleep/resume) leave the device topology unchanged, so
        a direct reopen usually works; the zombie-open case is caught by
        start()'s callback-verify loop, which falls back to a full-refresh
        cold open. The watchdog keeps the full refresh — it has time."""
        with self._stream_lifecycle_lock:
            if self._recording:
                # Never touch the stream under an active recording.
                return self._stream is not None
            if self._stream is not None and not force_fresh:
                if self._stream_looks_alive():
                    return True
                print(f"[Recorder] Warm stream is stale (no audio callback for "
                      f"{self._heartbeat_age():.1f}s) — reopening.")
                force_fresh = True
                # A stale stream means the device is gone/changed — its ring is
                # untrustworthy, so never carry it across this kind of reopen.
                preserve_preroll = False
            old_name = self._active_device_name
            old_rate = self.active_sample_rate
            if force_fresh and self._stream is not None:
                self._close_stream_locked()
            if force_fresh and not skip_refresh:
                self._refresh_portaudio()
            try:
                self._last_warm_reopened = True
                self._stream = self._open_best_input_stream()
                with self._lock:
                    self._last_callback_ts = time.monotonic()
                    # Keep the ring only when the reopen landed on the SAME
                    # device at the SAME rate (contiguous, trustworthy audio);
                    # otherwise it is stale/wrong-device audio and must go.
                    keep_ring = (preserve_preroll
                                 and old_name != ""
                                 and self._active_device_name == old_name
                                 and self.active_sample_rate == old_rate)
                    if not keep_ring:
                        self._preroll.clear()
                        self._preroll_samples = 0
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

    def _start_watchdog(self) -> None:
        """Idle-time keeper of the warm stream: recovers dead streams within
        ~5s and does a full PortAudio refresh every ~60s so the stream follows
        Windows default-device changes instead of clinging to a gone device."""
        with self._watchdog_lock:
            if self._watchdog_started:
                return
            self._watchdog_started = True
        threading.Thread(
            target=self._watchdog_loop, daemon=True, name="mic-watchdog"
        ).start()

    def _watchdog_loop(self) -> None:
        tick = 0
        while True:
            time.sleep(_WATCHDOG_INTERVAL)
            tick += 1
            if not self._warm_enabled:
                continue
            if (self.is_recording or self._monitor_active or self._aux_active
                    or self._probe_active):
                continue
            try:
                if not self._stream_looks_alive():
                    print("[Recorder] Watchdog: warm stream dead — recovering.")
                    self._ensure_warm_stream(force_fresh=True)
                elif self._stream_is_silently_dead():
                    # Callbacks still arriving, but carrying nothing at all —
                    # the stream outlived its device (audio-engine restart).
                    self._recover_silent_stream()
                elif tick % _DEVICE_REFRESH_TICKS == 0:
                    # Periodic refresh: PortAudio can't see default-device
                    # changes without a re-init, so bounce the idle stream to
                    # pick up whatever Windows now routes as the default mic.
                    # Skip it right around a dictation so the teardown can never
                    # coincide with a keypress and clip the first words, and keep
                    # the pre-roll when the device turns out unchanged.
                    quiet = (time.monotonic() - self._last_record_activity_ts
                             > _DEVICE_REFRESH_QUIET_SECONDS)
                    if quiet:
                        self._ensure_warm_stream(force_fresh=True,
                                                 preserve_preroll=True)
            except Exception as e:
                print(f"[Recorder] Watchdog error (non-fatal): {e}")

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

        with self._lock:
            # Fresh probe window per recording: advice from a previous
            # dictation must never switch mics under a new one.
            self._probe_done = False
            self._mic_advice = None
            self._recording_started_ts = time.monotonic()
            self._last_record_activity_ts = self._recording_started_ts

        # Recovery report for this press: stays None on the instant path (live
        # warm stream + pre-roll seed = nothing lost); set whenever the stream
        # had to be (re)opened, i.e. whenever first words were at risk.
        self.last_start_recovery = None
        self._last_warm_reopened = False
        press_ts = time.monotonic()

        # skip_refresh: if the warm stream died, reopen it directly instead of
        # burning ~0.3-0.7s in a PortAudio re-init while the user is speaking —
        # the verify loop below still falls back to a full-refresh cold open.
        warm_ready = self._warm_enabled and self._ensure_warm_stream(
            skip_refresh=True)

        if warm_ready:
            preroll_fresh = self._heartbeat_age() < _HEARTBEAT_STALE_SECONDS
            with self._lock:
                self._chunks = []
                self._chunks_samples = 0
                self._chunks_offset = 0
                # Seed with the last ~0.6s of pre-roll so speech that started
                # slightly before the hotkey (or during it) is captured — but
                # ONLY if the stream is demonstrably live. A stale pre-roll is
                # old room noise: transcribing it alone is how a dead mic used
                # to inject hallucinated fillers ("Mm-hmm.") instead of speech.
                if preroll_fresh:
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
                seed_samples = self._chunks_samples
                self._preroll.clear()
                self._preroll_samples = 0
                self._last_rms = 0.0
                self._last_peak = 0.0
                self._voiced_samples = 0
                self._recording = True
            # Trust but verify: the stream passed the liveness check, yet a
            # device can die at any moment. If no fresh callback lands shortly,
            # abandon the warm stream and cold-open a new one so a recording
            # can never proceed on a silent mic.
            deadline = time.monotonic() + _START_VERIFY_SECONDS
            verified = False
            while time.monotonic() < deadline:
                with self._lock:
                    if self._chunks_samples > seed_samples:
                        verified = True
                        break
                time.sleep(0.015)
            if verified:
                if self._last_warm_reopened:
                    # Stream was dead at press and the fast reopen saved it —
                    # audio flows now, but whatever was said before the reopen
                    # completed is gone. Report it so the fleet log shows how
                    # often first words were at risk.
                    self.last_start_recovery = {
                        "mode": "warm_reopen",
                        "elapsed_ms": int((time.monotonic() - press_ts) * 1000),
                        "device": self._active_device_name,
                        "seed_ms": int(seed_samples * 1000
                                       / max(1, self.active_sample_rate)),
                    }
                print(
                    f"[Recorder] Recording started instantly (warm, "
                    f"{self._active_device_name or 'default input'}, {self.active_sample_rate} Hz)."
                )
                return
            print("[Recorder] Warm stream produced no audio after start — "
                  "falling back to a fresh cold stream.")
            with self._stream_lifecycle_lock:
                self._close_stream_locked()
                self._refresh_portaudio()
                try:
                    self._stream = self._open_best_input_stream()
                    with self._lock:
                        self._last_callback_ts = time.monotonic()
                    self._stream.start()
                    self._warm_stream_is_open = True
                    self.last_start_recovery = {
                        "mode": "cold_recovered",
                        "elapsed_ms": int((time.monotonic() - press_ts) * 1000),
                        "device": self._active_device_name,
                    }
                    print(f"[Recorder] Recording started (recovered, "
                          f"{self._active_device_name or 'default input'}, "
                          f"{self.active_sample_rate} Hz).")
                    return
                except Exception as e:
                    with self._lock:
                        self._recording = False
                    self._stream = None
                    self._warm_stream_is_open = False
                    print(f"[Recorder] Failed to start recording: {e}")
                    raise

        # Cold path — open the stream now (legacy behaviour)
        with self._lock:
            self._chunks = []
            self._chunks_samples = 0
            self._chunks_offset = 0
            self._recording = True
            self._active_sample_rate = self.sample_rate
            self._last_rms = 0.0
            self._last_peak = 0.0
            self._voiced_samples = 0

        try:
            with self._stream_lifecycle_lock:
                self._stream = self._open_best_input_stream()
                self._stream.start()
            if self._warm_enabled:
                # Warm mode couldn't open its stream at all — the press paid a
                # full cold open with no pre-roll. Worth a fleet-log entry.
                self.last_start_recovery = {
                    "mode": "cold_open",
                    "elapsed_ms": int((time.monotonic() - press_ts) * 1000),
                    "device": self._active_device_name,
                }
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
            # Defer the next periodic default-follow so the reopen can't land on
            # a fast follow-up dictation and clip its first words.
            self._last_record_activity_ts = time.monotonic()
            # Wall-clock length of this recording. The app compares it against
            # the audio actually captured: a hotkey held for seconds that
            # yielded no samples is a dead stream, not an accidental tap.
            self.last_recording_seconds = (
                self._last_record_activity_ts - self._recording_started_ts
                if self._recording_started_ts > 0.0 else 0.0)

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
    # Aux capture (popup voice-prompt mic)
    # ------------------------------------------------------------------

    def start_aux_capture(self) -> bool:
        """Begin capturing audio for the popup's voice-prompt mic. Reuses the
        recording/warm stream so it records from the configured input device
        and is protected from the watchdog's PortAudio re-init. Coexists with
        a main recording (the callback feeds both buffers independently)."""
        with self._lock:
            self._aux_chunks = []
            self._aux_active = True
            self._aux_cold_opened = False
        if self._stream_looks_alive():
            return True
        if self._warm_enabled and self._ensure_warm_stream():
            return True
        # Cold mode: open a stream just for this capture.
        try:
            with self._stream_lifecycle_lock:
                if self._stream is None:
                    self._stream = self._open_best_input_stream()
                    with self._lock:
                        self._last_callback_ts = time.monotonic()
                    self._stream.start()
                    with self._lock:
                        self._aux_cold_opened = True
            return True
        except Exception as e:
            with self._lock:
                self._aux_active = False
            self._stream = None
            print(f"[Recorder] Aux capture failed to open stream: {e}")
            return False

    def read_aux_audio(self) -> Optional[np.ndarray]:
        """Snapshot of all aux audio captured so far (None if none yet)."""
        with self._lock:
            if not self._aux_chunks:
                return None
            return np.concatenate(self._aux_chunks, axis=0).flatten()

    def stop_aux_capture(self) -> Optional[np.ndarray]:
        """End aux capture and return the full captured audio (or None)."""
        with self._lock:
            self._aux_active = False
            chunks = self._aux_chunks
            self._aux_chunks = []
            cold = self._aux_cold_opened
            self._aux_cold_opened = False
        if cold:
            with self._stream_lifecycle_lock:
                if not self._recording and not self._warm_stream_is_open:
                    self._close_stream_locked()
        if not chunks:
            return None
        return np.concatenate(chunks, axis=0).flatten()

    # ------------------------------------------------------------------
    # Mic test monitor
    # ------------------------------------------------------------------

    def start_monitor(self, device_name: str = "") -> None:
        """Open a level-only stream for the Test Mic feature. Uses a dedicated
        callback so it can never contaminate a recording's audio buffer.
        Serialised under the lifecycle lock so the watchdog's PortAudio re-init
        can never run while the monitor stream is being opened."""
        self.stop_monitor()
        with self._stream_lifecycle_lock:
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
        with self._stream_lifecycle_lock:
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

    # ------------------------------------------------------------------
    # Evidence probe (silent-mic recovery)
    # ------------------------------------------------------------------

    def maybe_probe_alternatives(self) -> None:
        """Cheap per-tick check (called from the mic-level loop while
        recording): if the chosen mic has been alive but voice-free for
        _PROBE_TRIGGER_SECONDS — the 'headset ranked best but is on the desk'
        case — briefly sample the other real mics once and remember which one
        is actually hearing the user. Costs one flag check on a normal
        dictation. Auto mode only: a manually pinned mic is the user's call.
        Never opens Bluetooth hands-free or loopback endpoints (opening HFP
        degrades the user's audio; virtual devices hear the speakers)."""
        if not self.is_auto_device(self.input_device):
            return
        with self._lock:
            if (not self._recording or self._probe_done or self._probe_active
                    or self._voiced_samples > 0):
                return
            started = self._recording_started_ts
        if not started or time.monotonic() - started < _PROBE_TRIGGER_SECONDS:
            return
        with self._lock:
            if self._probe_active or self._probe_done or not self._recording:
                return
            self._probe_active = True
            self._probe_done = True
        try:
            threading.Thread(target=self._probe_alternatives, daemon=True,
                             name="mic-probe").start()
        except Exception as e:
            # Roll back, or a failed Thread.start() would latch _probe_active
            # forever — silencing the watchdog and PortAudio refresh for the
            # rest of the session.
            with self._lock:
                self._probe_active = False
            print(f"[Recorder] Mic probe spawn failed (non-fatal): {e}")

    def _probe_alternatives(self) -> None:
        """One-shot parallel sample of alternative mics (~0.9s, level-only).
        Runs while a recording is in progress, so the watchdog never touches
        PortAudio underneath (it skips while is_recording); _probe_active
        covers the tail where the recording stops mid-probe.

        The verdict only SETS _mic_advice (stamped with the recording it
        observed) — demotion and preference are committed exclusively in
        consume_mic_advice(), after a confirmed no-speech result. Committing
        here let a successful dictation silently lose its mic to the next
        watchdog refresh."""
        try:
            with self._lock:
                started = self._recording_started_ts
            devices = self.get_input_devices()
            active_name = self._active_device_name
            active = (active_name or "").strip().lower()
            candidates = [
                d for d in devices
                if d["name"].strip().lower() != active
                and self._auto_rank(d["name"]) <= 2
            ][:_PROBE_MAX_CANDIDATES]
            if not candidates:
                return

            # Per-candidate evidence: peak rms AND how long the signal stayed
            # above the floor. A single loud transient must not look like
            # speech — only sustained energy counts.
            levels: dict[str, dict] = {
                d["name"]: {"peak": 0.0, "loud": 0,
                            "rate": int(d.get("sample_rate") or 16000)}
                for d in candidates}
            levels_lock = threading.Lock()

            def _make_cb(name):
                def _cb(indata, _frames, _time_info, _status):
                    mono = indata[:, 0] if indata.ndim > 1 else indata
                    rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
                    with levels_lock:
                        entry = levels[name]
                        if rms > entry["peak"]:
                            entry["peak"] = rms
                        if rms >= _PROBE_MIN_RMS:
                            entry["loud"] += int(mono.shape[0])
                return _cb

            streams = []
            for d in candidates:
                try:
                    s = sd.InputStream(
                        samplerate=int(d.get("sample_rate") or 16000),
                        channels=1,
                        dtype="float32",
                        callback=_make_cb(d["name"]),
                        blocksize=1024,
                        device=int(d["index"]),
                    )
                    s.start()
                    streams.append(s)
                except Exception as e:
                    print(f"[Recorder] Probe skip '{d['name']}': {e}")
            if not streams:
                return

            primary_rms = 0.0
            deadline = time.monotonic() + _PROBE_WINDOW_SECONDS
            while time.monotonic() < deadline:
                with self._lock:
                    if (not self._recording
                            or self._recording_started_ts != started):
                        # Recording ended (or a NEW one began — never mix
                        # evidence across two dictations).
                        break
                    primary_rms = max(primary_rms, self._last_rms)
                time.sleep(0.05)

            for s in streams:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass

            with levels_lock:
                snapshot = {name: dict(e) for name, e in levels.items()}
            for name, entry in snapshot.items():
                if entry["peak"] >= _PROBE_MIN_RMS:
                    # This mic proved it can hear — clear any old demotion.
                    self._demoted_devices.discard(name.strip().lower())
            # Only candidates with SUSTAINED energy qualify as speech evidence.
            sustained = {
                name: entry for name, entry in snapshot.items()
                if entry["loud"] / float(entry["rate"] or 1)
                >= _PROBE_SUSTAIN_SECONDS
            }
            if not sustained:
                print("[Recorder] Mic probe: no sustained signal on any "
                      "alternative mic.")
                return
            best_name, best = max(sustained.items(),
                                  key=lambda kv: kv[1]["peak"])
            best_rms = best["peak"]
            if best_rms >= max(_PROBE_MIN_RMS, primary_rms * _PROBE_MARGIN):
                with self._lock:
                    if self._recording_started_ts != started:
                        return  # a new recording began — verdict is stale
                    self._mic_advice = {
                        "from": active_name,
                        "to": best_name,
                        "from_rms": round(primary_rms, 5),
                        "to_rms": round(best_rms, 5),
                        "rec_ts": started,
                        "topology": frozenset(
                            d["name"].strip().lower() for d in devices),
                    }
                print(f"[Recorder] Mic probe: '{best_name}' hears speech "
                      f"(rms {best_rms:.4f}) while '{active_name}'"
                      f" is silent (rms {primary_rms:.4f}) — advice armed.")
            else:
                print(f"[Recorder] Mic probe: no better mic found "
                      f"(best '{best_name}' rms {best_rms:.4f}).")
        except Exception as e:
            print(f"[Recorder] Mic probe failed (non-fatal): {e}")
        finally:
            self._probe_active = False

    @property
    def probe_in_flight(self) -> bool:
        """True while probe streams are open / the verdict is being computed —
        lets the no-speech path wait briefly instead of consuming None."""
        return self._probe_active

    def consume_mic_advice(self) -> Optional[dict]:
        """Return-and-clear the probe's switch advice ({from,to,from_rms,
        to_rms}) — the app acts on it exactly once, after a no-speech result.

        This is the ONLY place demotion and evidence preference are committed:
        advice that was never consumed (the dictation succeeded, or a new
        recording superseded it) leaves the selection state untouched."""
        with self._lock:
            advice, self._mic_advice = self._mic_advice, None
            if advice and advice.get("rec_ts") != self._recording_started_ts:
                advice = None  # verdict belongs to a different recording
            if advice:
                self._demoted_devices.add(
                    (advice.get("from") or "").strip().lower())
                self._demotion_topology = (advice.get("topology")
                                           or frozenset())
                self._evidence_preferred = advice.get("to", "")
        if advice:
            # The cached fast-path index still points at the silent device —
            # force full enumeration so the next open honours the evidence.
            self._active_device_index = None
            self._active_device_name = ""
        return advice

    def clear_mic_memory(self) -> None:
        """Forget evidence-based demotions/preference. Called on a manual
        device change in Settings — the user's explicit choice outranks
        anything the probe learned."""
        self._demoted_devices.clear()
        self._demotion_topology = frozenset()
        self._evidence_preferred = ""
        with self._lock:
            self._mic_advice = None

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

    # ── Auto-detect (best available) ─────────────────────────────────────
    # Name-based ranking, no audio sampling: instant, deterministic, and safe
    # to run on every stream open. Real wired mics first, Bluetooth hands-free
    # profiles late (they sound terrible), loopback/virtual endpoints last.

    @staticmethod
    def _auto_rank(name: str) -> int:
        """Lower is better. External mics beat the built-in array: plugging in
        a headset must switch to it even when Windows keeps the laptop mic as
        the system default (Windows often does), which is exactly what a user
        who just plugged one in expects."""
        n = (name or "").lower()
        if any(v in n for v in (
                "stereo mix", "sound mapper", "primary sound", "what u hear",
                "wave out", "pc speaker", "loopback", "virtual", "cable",
                # Localised Windows loopback endpoints — the probe must never
                # prefer a device that hears the speakers, in any language.
                "stereomix", "mixage", "mezcla", "missaggio",
                "микшер", "ミキサー")):
            return 4
        if any(b in n for b in (
                "bluetooth", "hands-free", "hfp", " ag audio")):
            return 3
        # Dedicated / plugged-in hardware.
        if any(s in n for s in (
                "headset", "usb audio", "usb mic", "webcam", "logi", "jabra",
                "yeti", "rode", "shure", "blue ", "elgato", "samson",
                "snowball", "at2020", "fifine", "hyperx", "steelseries")):
            return 0
        # Built-in laptop array or a plain "Microphone (…)" endpoint.
        if any(s in n for s in ("microphone", "mic", "array")):
            return 1
        return 2

    def _pick_best_input(self, devices: list[dict]) -> Optional[int]:
        """Best device index by rank. Within the winning rank the Windows
        default input wins (respects the user's OS-level choice among good
        mics); otherwise the first enumerated device of that rank.

        Evidence beats name ranking: a mic that provably heard the user while
        the ranked pick sat silent (probe verdict) wins outright, and the
        silent device is skipped. The evidence resets when a NEW device
        appears (the user plugged something in — including re-plugging the
        demoted headset) or the preferred device vanishes. A device merely
        disappearing does NOT reset — a PortAudio re-init transiently hiding
        an endpoint must not cancel a switch the user was just told about."""
        if not devices:
            return None
        # Keep the ranking helper safe for lightweight callers that construct a
        # Recorder without opening PortAudio (including diagnostics/tests).
        demoted = getattr(self, "_demoted_devices", set())
        preferred = getattr(self, "_evidence_preferred", "")
        topology = getattr(self, "_demotion_topology", frozenset())
        names_now = frozenset(d["name"].strip().lower() for d in devices)
        if ((demoted or preferred) and topology):
            appeared = names_now - topology
            preferred_gone = bool(
                preferred and preferred.strip().lower() not in names_now)
            if appeared or preferred_gone:
                demoted.clear()
                preferred = ""
                topology = frozenset()
                self._demoted_devices = demoted
                self._evidence_preferred = preferred
                self._demotion_topology = topology
        if preferred:
            wanted = preferred.strip().lower()
            for d in devices:
                if d["name"].strip().lower() == wanted:
                    return int(d["index"])
        pool = [d for d in devices
                if d["name"].strip().lower() not in demoted]
        if not pool:
            pool = devices  # every mic demoted — fall back to plain ranking
        best_rank = min(self._auto_rank(d["name"]) for d in pool)
        best = [d for d in pool if self._auto_rank(d["name"]) == best_rank]
        default_idx = self._get_default_input_index()
        if default_idx is not None:
            try:
                default_name = str(
                    sd.query_devices(default_idx).get("name", "")).strip().lower()
            except Exception:
                default_name = ""
            for d in best:
                if d["name"].strip().lower() == default_name:
                    return int(d["index"])
        return int(best[0]["index"])

    def pick_best_input_name(self, devices: list[dict]) -> str:
        """Name auto-detect would choose right now ('' if none) — used by the
        Settings dropdown to show 'Auto-detect (<device>)'."""
        idx = self._pick_best_input(devices)
        if idx is None:
            return ""
        for d in devices:
            if int(d["index"]) == idx:
                return d["name"]
        return ""

    @staticmethod
    def is_auto_device(token: str) -> bool:
        return (token or "").strip().lower() in ("", "auto")

    def _resolve_preferred_input(self, devices: list[dict]) -> Optional[int]:
        token = (self.input_device or "").strip()
        if self.is_auto_device(token):
            picked = self._pick_best_input(devices)
            if picked is not None:
                name = next((d["name"] for d in devices
                             if int(d["index"]) == picked), f"#{picked}")
                print(f"[Recorder] Auto-detect using '{name}' (#{picked})")
            return picked

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
