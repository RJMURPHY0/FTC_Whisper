"""
FTC Whisper — main entry point.

Architecture
------------
Main thread  : AppWindow (tkinter) — login screen → dashboard
Daemon thread: pystray tray icon (safe on Windows)
Daemon thread: Whisper model pre-load
Daemon thread: per-transcription processing pipeline
"""

import os
import sys
import threading
import time
from collections import deque

# Fix Windows console encoding
# Removed stdout wrapping for clear logging

import ctypes

# Boost to above-normal immediately — before heavy project imports (tkinter, PIL, faster-whisper)
# so the entire loading phase runs at elevated priority, not just post-import code.
if sys.platform == "win32":
    try:
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00008000  # ABOVE_NORMAL_PRIORITY_CLASS
        )
    except Exception:
        pass

from config import Config
from recorder import Recorder
from transcriber import Transcriber
from asr_engine import ParakeetTranscriber, model_files_present, download_model
from stream_session import StreamingSession
from injector import Injector, _release_modifiers
from hotkey_manager import HotkeyManager, TriggerHotkeyManager, AppState
from feedback import Feedback
from tray import TrayApp
from popup import FloatingPopup
from ai_refiner import AIRefiner
from supabase_client import SupabaseLogger
from auth import AuthManager
from app_window import AppWindow

APP_VERSION = "1.6.21"


class WhisperFlowApp:
    """
    Main application controller.
    Created once authentication is confirmed; wires all components together.
    """

    def __init__(self, auth: AuthManager, config: Config):
        print("=" * 50)
        print("  FTC Whisper — Voice-to-Text Desktop App")
        print("=" * 50)

        self._auth = auth
        self.config = config
        self._started = False
        self._restart_for_reauth = False

        # ── Core pipeline ──────────────────────────────────────────────
        _ap = getattr(config, "auto_punctuate", True)
        self.transcriber = Transcriber(
            model_size=config.whisper_model,
            language=config.language,
            num_workers=2,
            auto_punctuate=_ap,
        )
        # Fast model: injects text immediately; accurate model refines in background
        # beam_size=1 = greedy decode (2-4x faster than beam search, negligible quality loss for preview)
        # vad_speech_pad_ms=30 + min_silence_duration_ms=100 = tighter VAD for lower latency
        # cpu_threads=4 = avoids thread-spawn overhead on short clips vs. using all cores
        self.fast_transcriber = Transcriber(
            model_size="base.en", beam_size=1,
            vad_speech_pad_ms=30, min_silence_duration_ms=100,
            cpu_threads=4, auto_punctuate=_ap,
        )
        # Primary engine: Parakeet TDT 0.6b v2 (int8 ONNX) — better accuracy
        # than whisper-large-v3 at ~20x realtime on CPU with punctuation built
        # in. English only; whisper pipeline remains the fallback (other
        # languages, or model not yet downloaded).
        self.parakeet = ParakeetTranscriber(auto_punctuate=_ap)
        self._recording_timer: threading.Timer | None = None
        self.recorder = Recorder(
            sample_rate=config.sample_rate,
            input_device=getattr(config, "input_device", ""),
        )
        self.injector = Injector(method=config.inject_method)

        # ── AI + logging ───────────────────────────────────────────────
        self.ai_refiner = AIRefiner(
            api_key=config.anthropic_api_key,
            openrouter_api_key=config.openrouter_api_key,
            openrouter_model=(getattr(config, "openrouter_model", "") or "").strip()
            or AIRefiner.DEFAULT_OPENROUTER_MODEL,
        )
        self.db = SupabaseLogger(url=config.supabase_url, key=config.supabase_key)

        # ── UI components ──────────────────────────────────────────────
        self.app_window = AppWindow(
            auth=auth,
            on_authenticated=self._on_authenticated,
            on_sign_out=self._sign_out,
            on_sign_in=self._on_sign_in,
            on_open_config=self._open_config,
            on_quit=self._shutdown,
            on_hotkey_change=self._on_hotkey_change,
            on_refine_hotkey_change=self._on_refine_hotkey_change,
            on_settings_change=self._on_settings_change,
            db=self.db,
            hotkey=config.hotkey,
            refine_hotkey=config.refine_hotkey,
            config=config,
            get_input_devices=self.recorder.get_input_devices,
            recorder=self.recorder,
            transcriber=self.transcriber,
            version=APP_VERSION,
        )

        self.tray = TrayApp(
            on_quit=self._shutdown_and_destroy,
            on_open_config=self._open_config,
            on_sign_out=self._sign_out,
            on_open=self.app_window.show,
        )

        self.feedback = Feedback(
            sound_enabled=config.sound_feedback,
            on_icon_change=self.tray.update_icon,
        )

        self.popup = FloatingPopup()
        self.popup.set_ai_refiner(self.ai_refiner)
        self.popup.set_voice_prompt_callback(
            lambda audio, rate, blocking=True: self._fast_engine().transcribe(audio, rate, blocking=blocking)
        )
        self.popup.set_voice_capture_fns(
            start=self.recorder.start_aux_capture,
            read=lambda: (self.recorder.read_aux_audio(),
                          self.recorder.active_sample_rate),
            stop=lambda: (self.recorder.stop_aux_capture(),
                          self.recorder.active_sample_rate),
        )

        self._recording_hwnd: int = 0
        self._mic_loop_running = threading.Event()
        self._mic_level_smooth = 0.0

        # ── Live captions (whisper-fallback path only) ─────────────────
        # In Parakeet mode captions come from the StreamingSession worker.
        # _caption_loop_running gates whether the loop should keep going;
        # _caption_stop_event is CLEAR while running and set to stop — the
        # pacing wait uses the stop event (waiting on the running event, which
        # is set, returned immediately and busy-spun the loop).
        self._caption_loop_running = threading.Event()
        self._caption_stop_event = threading.Event()
        self._caption_thread: threading.Thread | None = None

        # Rolling context: last ~30 words from recent transcriptions fed into Whisper initial_prompt
        self._context_deque: deque = deque(maxlen=150)
        self._context_lock = threading.Lock()

        # Streaming transcription session (Parakeet mode) — one per recording
        self._session: StreamingSession | None = None
        # Live Typing (opt-in) per-dictation state
        self._live_inject_active = False   # live-inject engaged for the current dictation
        self._live_focus_lost = False      # foreground left the target while streaming
        # Monotonic id per dictation: stale upgrade results from a previous
        # dictation are discarded instead of contaminating the current popup.
        self._dictation_seq = 0
        # Model change requested while recording — applied when idle again
        self._pending_model_change: str | None = None
        # Auto-update: restart only after a stretch of inactivity. Seeded with
        # "now" so a just-launched app never restarts under the user's feet.
        self._last_dictation_ts = time.time()
        self._auto_update_versions: set = set()

        self.hotkey_manager = HotkeyManager(
            hotkey=config.hotkey,
            mode=config.mode,
            on_start_recording=self._on_start_recording,
            on_stop_recording=self._on_stop_recording,
            on_cancel_recording=self._on_cancel_recording,
            on_state_change=self._on_state_change,
        )

        self.refine_hotkey_manager = TriggerHotkeyManager(
            hotkey=config.refine_hotkey,
            on_trigger=self._on_refine_selection,
        )

        # ── Pre-load all models immediately in background ─────────────
        threading.Thread(
            target=self.transcriber.load_model, daemon=True, name="model-preload"
        ).start()
        threading.Thread(
            target=self.fast_transcriber.load_model, daemon=True, name="fast-model-preload"
        ).start()
        threading.Thread(
            target=self._init_parakeet, daemon=True, name="parakeet-preload"
        ).start()

        # Warm mic: keep a persistent input stream with a ~1.5s pre-roll ring
        # buffer so recording starts instantly and the first syllable — even
        # speech that began ON the go-beep — is captured.
        if getattr(config, "warm_mic", True):
            threading.Thread(
                target=lambda: self.recorder.set_warm(True),
                daemon=True, name="warm-mic",
            ).start()

        # ── Local HTTP server so the web app can detect + surface this window ──
        _start_local_server(self.app_window, APP_VERSION)

    def _init_parakeet(self) -> None:
        """Download (first run only) and load the Parakeet engine. Any failure
        leaves the whisper pipeline in charge — strictly additive."""
        try:
            if not getattr(self.config, "use_parakeet", True):
                print("[App] Parakeet disabled in config — whisper pipeline only.")
                return
            if not model_files_present():
                print("[App] Parakeet model not found — downloading (~660 MB, one-time)…")
                last_pct = [-10]

                def _progress(frac: float, msg: str) -> None:
                    pct = int(frac * 100)
                    if pct - last_pct[0] >= 10:
                        last_pct[0] = pct
                        print(f"[App] {msg} ({pct}%)")

                if not download_model(progress=_progress):
                    return
            self.parakeet.load_model()
            if self.parakeet.is_loaded:
                print("[App] Parakeet engine active — near-instant transcription enabled.")
        except Exception as e:
            print(f"[App] Parakeet init failed ({e}) — using Whisper pipeline.")

    def _use_parakeet(self) -> bool:
        """Parakeet handles English; anything else stays on the whisper path."""
        if not getattr(self.config, "use_parakeet", True):
            return False
        lang = (getattr(self.config, "language", "en") or "en").lower()
        return lang in ("", "en", "english") and self.parakeet.is_loaded

    def _fast_engine(self):
        """Engine used for immediate/injection transcription."""
        return self.parakeet if self._use_parakeet() else self.fast_transcriber

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Start the application.
        If already authenticated (restored session), kick off background
        services immediately. Then block on the tkinter mainloop.
        """
        if self._auth.is_authenticated:
            # Session was restored — start services; AppWindow will fire
            # _on_authenticated via after() once mainloop is running.
            pass

        self.app_window.run()  # blocks on main thread

    def _on_authenticated(self, auth: AuthManager) -> None:
        """
        Called (in a daemon thread) after login or session restore.
        Starts pystray, hotkeys, and Whisper pre-load.
        """
        if self._started:
            return
        self._started = True

        # Share the authenticated Supabase client with the logger so RLS passes
        if auth._client:
            self.db.set_client(auth._client)
        self.db.set_user(auth.user_id)
        self.tray.set_user_email(auth.user_email or "")

        print(f"[App] Authenticated as {auth.user_email}")

        # If no local API key, fetch from Supabase app_settings — in a daemon
        # thread: these are two sequential network calls and used to gate
        # hotkey registration, delaying time-to-first-dictation by seconds.
        if self.db.is_enabled:
            threading.Thread(
                target=self._fetch_remote_api_keys, daemon=True, name="api-key-fetch"
            ).start()

        if self.ai_refiner.is_available:
            print("[App] AI refinement enabled.")
        else:
            print("[App] AI refinement disabled — set anthropic_api_key or openrouter_api_key in config or Supabase app_settings.")

        if self.db.is_enabled:
            print(f"[App] Supabase logging enabled.")
        else:
            print("[App] Supabase logging disabled — set supabase_url/key in config.")

        print("[App] Ready! Hold the hotkey and start speaking.")

        # Initialize popup Toplevel on the main thread (avoids dual-Tk deadlock)
        root = self.app_window._root
        if root:
            root.after(0, lambda: self.popup.initialize(root))

        # Register global hotkeys
        self.hotkey_manager.register()
        self.refine_hotkey_manager.register()
        print(f"[App] Hotkey: '{self.config.hotkey}' | Mode: {self.config.mode}")
        print(f"[App] Refine hotkey: '{self.config.refine_hotkey}'")

        # Start tray in daemon thread (safe on Windows)
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()

        # Check for updates in the background; show banner in Settings if found
        self._start_update_check()

        # If this launch is the first run of a new version, toast it
        self._announce_update_if_any()

    def _fetch_remote_api_keys(self) -> None:
        try:
            if not self.ai_refiner.openrouter_api_key:
                or_key = self.db.fetch_app_setting("openrouter_api_key")
                if or_key:
                    self.ai_refiner.update_openrouter_key(or_key)
                    self.popup.set_ai_refiner(self.ai_refiner)
                    print("[App] Loaded OpenRouter API key from Supabase.")
            if not self.ai_refiner.api_key:
                key = self.db.fetch_app_setting("anthropic_api_key")
                if key:
                    self.ai_refiner.update_api_key(key)
                    self.popup.set_ai_refiner(self.ai_refiner)
                    print("[App] Loaded Anthropic API key from Supabase.")
            if self.ai_refiner.is_available:
                print("[App] AI refinement enabled.")
        except Exception as e:
            print(f"[App] Remote API key fetch failed: {e}")

    def _start_update_check(self) -> None:
        from updater import check_for_update

        def _on_update_found(version: str, url: str) -> None:
            print(f"[App] Update available: {version}")
            auto = getattr(self.config, "auto_update", True)
            self.app_window._ui_after(
                0, lambda: self.app_window.show_update_banner(version, url, auto=auto)
            )
            if auto:
                self._start_auto_update(version, url)

        # Re-check periodically, not just at launch: the app runs for days via
        # the logon task, so a startup-only check leaves users on a stale build
        # until they happen to restart. show_update_banner is idempotent.
        def _check_loop():
            while True:
                check_for_update(APP_VERSION, _on_update_found)
                time.sleep(6 * 3600)

        threading.Thread(
            target=_check_loop, daemon=True, name="update-check-loop"
        ).start()

    def _start_auto_update(self, version: str, url: str) -> None:
        """Download + install *version* in the background, restarting only once
        the app has been idle for a while. One attempt per version per process —
        the periodic check loop must not stack duplicate workers."""
        from updater import current_exe_path, run_auto_update

        exe_path = current_exe_path()
        if not exe_path:
            print("[App] Auto-update skipped — running from source.")
            return
        if version in self._auto_update_versions:
            return
        self._auto_update_versions.add(version)

        def _log_event(stage, ok, detail):
            try:
                self.db.log_update_event(
                    stage, from_version=APP_VERSION, to_version=version,
                    ok=ok, detail=detail)
            except Exception:
                pass

        def _worker():
            try:
                run_auto_update(
                    version, url, exe_path,
                    is_idle=self._safe_to_restart,
                    on_status=self.app_window.set_update_status,
                    on_event=_log_event,
                )
                # run_auto_update only returns on download failure — allow the
                # next 6-hour check to retry this version from scratch.
                self._auto_update_versions.discard(version)
            except Exception as exc:
                self._auto_update_versions.discard(version)
                from error_reporter import report_error
                report_error(
                    f"Auto-update failed: {exc}",
                    context={"version": version, "url": url},
                    user_email=getattr(self._auth, "user_email", None),
                )

        threading.Thread(target=_worker, daemon=True, name="auto-update").start()

    def _safe_to_restart(self) -> bool:
        """True when an update-restart won't interrupt the user: nothing is
        recording/processing and the last dictation was a while ago (which also
        keeps the post-dictation popup with its Insert/Undo buttons usable)."""
        if self.hotkey_manager.state != AppState.IDLE:
            return False
        return (time.time() - self._last_dictation_ts) > 120

    def _announce_update_if_any(self) -> None:
        """First run after an update: show a transient 'Updated to vX' toast."""
        from updater import is_newer, read_last_run_version, write_last_run_version

        try:
            prev = read_last_run_version()
            write_last_run_version(APP_VERSION)
            if prev and is_newer(APP_VERSION, prev):
                print(f"[App] Updated {prev} → {APP_VERSION}")

                def _announce():
                    # Prefer a native tray notification: FTC Whisper runs hidden
                    # in the tray, so the bottom-right corner toast would float
                    # over whatever app is in front (e.g. FTC Contacts) and look
                    # like it belongs to that app. A tray notification is owned by
                    # the FTC Whisper icon — clearly attributed to FTC Whisper and
                    # never overlaying another window. Fall back to the toast only
                    # if the tray can't notify (icon not ready / no support).
                    if not self.tray.notify(
                        f"Updated to v{APP_VERSION} ✓", "FTC Whisper"
                    ):
                        self.app_window.show_toast(
                            f"FTC Whisper updated to v{APP_VERSION} ✓", 6000
                        )

                self.app_window._ui_after(1500, _announce)
        except Exception as e:
            print(f"[App] Update announce failed (non-fatal): {e}")

    def _on_hotkey_change(self, new_hotkey: str) -> None:
        """Called when the user saves a new hotkey in the dashboard."""
        print(f"[App] Updating hotkey to: {new_hotkey}")
        # Re-registering mid-recording kills the release-poll thread without
        # firing key-up, wedging the state machine at RECORDING — cancel first.
        if self.hotkey_manager.state == AppState.RECORDING:
            self._on_cancel_recording()
        self.config.hotkey = new_hotkey
        self.config.save()
        self.hotkey_manager.update_hotkey(new_hotkey)

    def _on_refine_hotkey_change(self, new_hotkey: str) -> None:
        """Called when the user saves a new refine hotkey in the dashboard."""
        print(f"[App] Updating refine hotkey to: {new_hotkey}")
        self.config.refine_hotkey = new_hotkey
        self.config.save()
        self.refine_hotkey_manager.update_hotkey(new_hotkey)

    def _on_settings_change(self, key: str, value) -> None:
        """Called when the user saves a setting in the Settings panel."""
        print(f"[App] Setting changed: {key} = {value!r}")
        setattr(self.config, key, value)
        self.config.save()
        if key == "anthropic_api_key":
            self.ai_refiner.update_api_key(value)
            self.popup.set_ai_refiner(self.ai_refiner)
        elif key == "openrouter_api_key":
            self.ai_refiner.update_openrouter_key(value)
            self.popup.set_ai_refiner(self.ai_refiner)
        elif key == "input_device":
            self.recorder.input_device = value.strip() if value else ""
            # Clear cached device index so next recording re-enumerates with the new choice
            self.recorder._active_device_index = None
            self.recorder._active_device_name = ""
            # Warm stream is bound to the old device — reopen on the new one
            threading.Thread(
                target=self.recorder.restart_warm, daemon=True, name="warm-restart"
            ).start()
        elif key == "warm_mic":
            threading.Thread(
                target=lambda: self.recorder.set_warm(bool(value)),
                daemon=True, name="warm-toggle",
            ).start()
        elif key == "whisper_model":
            self._apply_model_change(value)
        elif key == "sound_feedback":
            self.feedback.sound_enabled = bool(value)
        elif key == "openrouter_model":
            self.ai_refiner.openrouter_model = (value or "").strip() or self.ai_refiner.openrouter_model
        elif key == "auto_punctuate":
            self.transcriber.auto_punctuate = bool(value)
            self.fast_transcriber.auto_punctuate = bool(value)
            self.parakeet.auto_punctuate = bool(value)
        elif key == "live_captions":
            # Live update, no restart. Captions reuse the already-loaded fast
            # model, so there's nothing to build — the next recording picks it up.
            self.config.live_captions = bool(value)
        elif key == "inject_method":
            # Injector is built once; update its strategy in place (whitelist-guarded).
            self.injector.method = value if value in {"clipboard", "keystrokes", "auto"} else "clipboard"
        elif key == "mode":
            if self.hotkey_manager.state == AppState.RECORDING:
                self._on_cancel_recording()
            self.hotkey_manager.mode = value

    def _apply_model_change(self, value: str) -> None:
        """Swap the accurate whisper model. No-op when unchanged (every Save
        used to discard and reload the model). Deferred while recording."""
        if value == self.transcriber.model_size:
            return
        if self.hotkey_manager.state == AppState.RECORDING:
            self._pending_model_change = value
            print(f"[App] Model change to '{value}' queued until recording ends.")
            return
        self._pending_model_change = None
        _ap = getattr(self.config, "auto_punctuate", True)
        new_t = Transcriber(
            model_size=value,
            language=self.config.language,
            auto_punctuate=_ap,
        )
        self.transcriber = new_t
        threading.Thread(
            target=new_t.load_model, daemon=True, name="model-reload"
        ).start()
        print(f"[App] Transcriber reloading model '{value}' in background…")

    # ------------------------------------------------------------------
    # Recording pipeline
    # ------------------------------------------------------------------

    def _on_stream_inject(self, chunk: str) -> bool:
        """Type a locked chunk into the target app live (called from the
        StreamingSession worker thread — Win32 only, no tkinter).

        Injects ONLY while the original recording target is still the foreground
        window. A focus change mid-dictation flips _live_focus_lost and returns
        False, which freezes the session's streaming — so we never type into, nor
        later backspace-reconcile, the wrong window."""
        try:
            fg = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            fg = 0
        if not self._recording_hwnd or fg != self._recording_hwnd:
            self._live_focus_lost = True
            return False
        try:
            return self.injector.inject_stream(chunk)
        except Exception as e:
            print(f"[App] Live stream-inject error: {e}")
            return False

    def _reconcile_live(self, streamed: str, target: str, can_backspace: bool) -> tuple[bool, int]:
        """Converge the live-streamed text already in the document to the final
        `target` text. Returns (ok, our_char_count_in_doc).

        The ONLY place a backspace is ever sent for live-inject — and it deletes
        strictly OUR OWN streamed characters (bounded by len(streamed)), never the
        user's content. When backspacing isn't safe (focus left the field / stream
        froze), append the missing tail words with ZERO deletions instead."""
        if streamed == target:
            return True, len(streamed)

        if can_backspace:
            common = os.path.commonprefix([streamed, target])
            # Back off to the last word boundary so we never re-type a word fragment
            # (e.g. "ice"→"ice cream" must not leave "ic" + "e cream").
            if common and common[-1] != " " and (len(common) < len(streamed)
                                                  and len(common) < len(target)):
                cut = common.rfind(" ")
                common = common[: cut + 1] if cut >= 0 else ""
            n_del = len(streamed) - len(common)
            tail = target[len(common):]
            # delete_stream, NOT send_backspaces: deletions must ride the same
            # transport as the streamed text (WM_CHAR queue for native apps) or
            # trailing backspaces can interleave AFTER the tail and eat it.
            ok_del = self.injector.delete_stream(n_del) if n_del > 0 else True
            ok_type = self.injector.inject_stream(tail) if tail else True
            return (ok_del and ok_type), len(target)

        # Append-only fallback: never delete. Add only words beyond what we streamed.
        sw = streamed.split()
        tw = target.split()
        remainder = tw[len(sw):]
        if not remainder:
            return True, len(streamed)
        add = (" " if streamed else "") + " ".join(remainder)
        if getattr(self.config, "trailing_space", False):
            add += " "
        ok = self.injector.inject_stream(add)
        return ok, len(streamed) + len(add)

    def _on_start_recording(self) -> None:
        self._last_dictation_ts = time.time()
        try:
            try:
                self._recording_hwnd = ctypes.windll.user32.GetForegroundWindow()
                print(f"[App] Recording started, target hwnd={self._recording_hwnd:#x}")
            except Exception:
                self._recording_hwnd = 0
            # Capture the target app identity NOW — browser tab titles change
            # constantly, so this can't be resolved later at log time.
            try:
                from app_icons import capture_app_info
                self._recording_app = capture_app_info(self._recording_hwnd)
            except Exception:
                self._recording_app = {"app_name": "", "app_exe": ""}
            # Capture mouse position now — user is hovering near the target text field
            try:
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                self._rec_cursor_x, self._rec_cursor_y = pt.x, pt.y
            except Exception:
                self._rec_cursor_x, self._rec_cursor_y = 0, 0
            # Start capture FIRST, beep second — the beep is the user's cue to
            # speak, so audio must already be flowing when they hear it. (With
            # the warm mic this also captures ~0.35s of pre-roll.)
            self.recorder.start()
            self.feedback.recording_started()

            # Auto-stop timer: toggle_timeout (toggle mode only) and max_recording_duration (all modes)
            _timeouts = []
            if self.config.mode == "toggle" and getattr(self.config, "toggle_timeout", 0) > 0:
                _timeouts.append(self.config.toggle_timeout)
            if getattr(self.config, "max_recording_duration", 0) > 0:
                _timeouts.append(self.config.max_recording_duration)
            if _timeouts:
                self._recording_timer = threading.Timer(min(_timeouts), self._auto_stop_recording)
                self._recording_timer.daemon = True
                self._recording_timer.start()

            # Parakeet mode: incremental committed-prefix transcription. Long
            # dictations are transcribed while you speak, so the tail left at
            # hotkey-release is always small — stop latency stays constant.
            if self._use_parakeet():
                # Live Typing (opt-in): words are injected into the target app AS
                # you speak. Only in Parakeet mode (needs the stable incremental
                # stream). Reset per-dictation focus state; suppress the caption
                # bar since the app itself now shows the text.
                live = bool(getattr(self.config, "live_inject", False))
                self._live_inject_active = live
                self._live_focus_lost = False
                session = StreamingSession(
                    self.recorder,
                    self.parakeet,
                    context_words=self._get_context_words(),
                    hotwords=self._get_hotwords(),
                    on_caption=self.popup.update_caption,
                    captions_enabled=bool(getattr(self.config, "live_captions", False)) and not live,
                    on_inject=self._on_stream_inject if live else None,
                    live_inject=live,
                )
                self._session = session
                session.start()
            else:
                self._live_inject_active = False
        except Exception as e:
            print(f"[App] Failed to start recording: {e}")
            self.feedback.error_occurred(str(e))
            self.hotkey_manager.set_idle()

    def _on_stop_recording(self) -> None:
        self._last_dictation_ts = time.time()
        self._cancel_recording_timer()
        session = self._session
        self._session = None

        # Stop the whisper-fallback caption loop (Parakeet captions live inside
        # the streaming session) and wait for any in-flight tick, so no caption
        # tick still holds the fast model's lock when the final pass needs it.
        self._caption_stop_event.set()
        self._caption_loop_running.clear()
        _ct = self._caption_thread
        if _ct is not None and _ct.is_alive():
            _ct.join(timeout=0.5)

        self._dictation_seq += 1
        seq = self._dictation_seq

        transcribed_text: str = ""
        hwnd = self._recording_hwnd
        upgrading = False
        final_audio = None
        capture_rate = max(1, self.recorder.active_sample_rate)

        try:
            # A quick-tap can race the async start thread: this handler may run
            # before recorder.start() executed. Wait briefly so we stop the real
            # stream instead of leaving an orphaned always-on recording.
            if not self.recorder.is_recording:
                for _ in range(25):
                    if self.recorder.is_recording:
                        break
                    time.sleep(0.02)

            total_samples = self.recorder.total_recorded_samples
            self.feedback.recording_stopped()

            _ctx = self._get_context_words()
            _hw = self._get_hotwords()

            if session is not None:
                # ── Parakeet path: committed prefix + tail-only final pass ──
                text, tail_audio, capture_rate = session.finalize()
                final_audio = tail_audio
                _streamed = session.injected_text if self._live_inject_active else ""
                # Near-silence gate: when nothing was committed while speaking
                # and the whole result came from an essentially silent tail,
                # it's a hallucination (dead/muted mic), not dictation. Skip the
                # gate if live-inject already typed real words on screen.
                if text and not session.committed_text and not _streamed:
                    import numpy as _np
                    _peak = (float(_np.max(_np.abs(tail_audio)))
                             if tail_audio is not None and len(tail_audio) else 0.0)
                    if _peak < 0.004:
                        print(f"[App] Discarding near-silence result '{text}' "
                              f"(peak={_peak:.4f}) — mic delivered no speech.")
                        text = ""
                if not text:
                    if _streamed:
                        # Final pass came back empty but we already typed live from
                        # real hypotheses — keep what's on screen rather than
                        # backspacing it out. Reconcile below becomes a no-op.
                        text = _streamed
                        print(f"[App] Empty final pass; keeping streamed text '{_streamed}'.")
                    else:
                        if total_samples < capture_rate * 0.3:
                            print("[App] Recording too short, ignoring.")
                            self.feedback.error_occurred("Recording too short")
                        else:
                            print("[App] Empty transcription result.")
                            self.feedback.error_occurred("No speech detected")
                        self.hotkey_manager.set_idle()
                        return
                transcribed_text = text
                # Upgrade = LLM context-fix only. Parakeet already beats the
                # local whisper models on English accuracy, so a whisper
                # re-pass would usually be a downgrade — skip it.
                upgrading = self.ai_refiner.is_available and len(text.split()) >= 4
                print(f"[App] Transcription (parakeet): '{text}'")
            else:
                # ── Whisper fallback path ──
                audio = self.recorder.stop()

                if audio is None or len(audio) < capture_rate * 0.3:
                    print("[App] Recording too short, ignoring.")
                    self.hotkey_manager.set_idle()
                    self.feedback.error_occurred("Recording too short")
                    return

                final_audio = audio
                # Silence gate BEFORE the fast pass — near-silent clips must
                # never reach the model at all: whisper invents fluent text on
                # silence/noise, and anything it returns here gets injected.
                # (The old gate only ran when the fast pass came back empty.)
                import numpy as _np
                _clip_peak = float(_np.max(_np.abs(final_audio))) if len(final_audio) else 0.0
                if _clip_peak < 0.002:
                    print(f"[App] Near-silent clip (peak={_clip_peak:.4f}) — "
                          "skipping transcription entirely.")
                    self.hotkey_manager.set_idle()
                    self.feedback.error_occurred("No speech detected")
                    return
                print(
                    f"[App] Transcribing {len(final_audio) / capture_rate:.1f}s of audio at {capture_rate} Hz..."
                )
                fast_text = self.fast_transcriber.transcribe(
                    final_audio, capture_rate, context_words=_ctx, hotwords_str=_hw).strip()
                if fast_text:
                    transcribed_text = fast_text
                    upgrading = True
                    print(f"[App] Fast transcription: '{fast_text}'")
                else:
                    # Fast model found nothing. If the clip is essentially
                    # silence, report immediately — the old behaviour queued a
                    # synchronous accurate pass behind any in-flight upgrade on
                    # the same lock, freezing "Transcribing…" for 10-30s.
                    import numpy as _np
                    peak = float(_np.max(_np.abs(final_audio))) if len(final_audio) else 0.0
                    if peak < 0.002:
                        print("[App] Silence detected — skipping accurate fallback.")
                        self.hotkey_manager.set_idle()
                        self.feedback.error_occurred("No speech detected")
                        return
                    text = self.transcriber.transcribe(
                        final_audio, capture_rate, context_words=_ctx, hotwords_str=_hw).strip()
                    if not text:
                        print("[App] Empty transcription result.")
                        self.hotkey_manager.set_idle()
                        self.feedback.error_occurred("No speech detected")
                        return
                    transcribed_text = text
                    self._update_context(text)
                    print(f"[App] Transcription: '{text}'")

        except Exception as e:
            print(f"[App] Transcription pipeline error: {e}")
            import traceback

            traceback.print_exc()
            self.feedback.error_occurred(str(e))
            self.hotkey_manager.set_idle()
            return

        # ── Injection — isolated so a failure never prevents the popup ──────────
        # Whole block in try/finally: even if focus/release/inject/feedback throw,
        # set_idle() ALWAYS fires, so the "Transcribing…" pill is never orphaned
        # on screen (it hides via the is_user_facing guard in _on_state_change).
        result = False
        _live_del = 0   # chars we put in the doc via live-inject (for popup Replace)
        try:
            # Fast path: in the overwhelmingly common case the target window
            # never lost focus during recording (the popup is WS_EX_NOACTIVATE),
            # so both the SetForegroundWindow dance AND the browser DOM-focus
            # click are unnecessary — skipping them saves ~0.5-0.7s per dictation
            # and avoids the synthetic click pressing arbitrary page UI.
            fg_now = 0
            try:
                fg_now = ctypes.windll.user32.GetForegroundWindow()
            except Exception:
                pass
            # In hold mode the user's hand is on the hotkey — they can't have
            # clicked elsewhere. In toggle mode, require the mouse to be where
            # it was at recording start: a click inside the same browser window
            # would have moved DOM focus without changing the Win32 foreground.
            same_cursor = True
            if self.config.mode != "hold":
                try:
                    pt = ctypes.wintypes.POINT()
                    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                    same_cursor = (
                        abs(pt.x - getattr(self, "_rec_cursor_x", 0))
                        + abs(pt.y - getattr(self, "_rec_cursor_y", 0))
                    ) < 4
                except Exception:
                    same_cursor = False
            focus_unchanged = bool(hwnd) and fg_now == hwnd and same_cursor

            if not focus_unchanged:
                self._focus_window(hwnd)

            # Release modifier keys BEFORE the browser focus click so Chrome never
            # receives a spurious Alt key-up that would activate its menu bar and
            # steal focus away from the search/input element we're about to click.
            _release_modifiers()

            # Browser windows (ChatGPT, Gmail, Outlook web, etc.) — Win32
            # SetForegroundWindow restores the Chrome/Firefox Win32 focus but does
            # NOT restore the JS/DOM focus of the contenteditable or ProseMirror
            # input. Simulate a click at the recording-start cursor position to
            # re-establish the browser's internal focus before Ctrl+V. Only needed
            # when focus was actually lost — if the browser stayed foreground the
            # DOM focus is still intact.
            _BROWSER_PREFIXES = ("Chrome_WidgetWin_", "Mozilla", "CEF-")
            _BROWSER_EXACT = {
                "Chrome_WidgetWin_1",
                "MozillaWindowClass",
                "MozillaDialogClass",
                "Chrome_RenderWidgetHostHWND",
            }
            if not focus_unchanged:
                try:
                    cls = self._get_window_class(hwnd)
                    if cls and (
                        cls in _BROWSER_EXACT
                        or any(cls.startswith(p) for p in _BROWSER_PREFIXES)
                    ):
                        self._click_to_restore_focus(
                            self._rec_cursor_x, self._rec_cursor_y, hwnd
                        )
                except Exception as e:
                    print(f"[App] Browser focus click error: {e}")

            try:
                _text_to_inject = transcribed_text
                if getattr(self.config, "trailing_space", False):
                    _text_to_inject += " "
                _streamed_live = (session.injected_text
                                  if (session is not None and self._live_inject_active)
                                  else "")
                if _streamed_live:
                    # Live Typing: text is already in the app as the user spoke.
                    # Converge it to the final result — backspacing ONLY our own
                    # streamed characters, and only when the target field is
                    # provably still focused (else append the tail, no deletes).
                    can_bs = (not self._live_focus_lost
                              and not session.stream_frozen
                              and focus_unchanged)
                    print(f"[App] Live reconcile: streamed={len(_streamed_live)} "
                          f"target={len(_text_to_inject)} backspace={can_bs}")
                    result, _live_del = self._reconcile_live(
                        _streamed_live, _text_to_inject, can_bs)
                    print(f"[App] Live reconcile result={result} del={_live_del}")
                else:
                    print(f"[App] Injecting: {len(_text_to_inject)} chars")
                    # Modifiers already released above — skip the second release
                    result = self.injector.inject(_text_to_inject, release_mods=False)
                    print(f"[App] Inject result: {result}")
            except Exception as e:
                print(f"[App] Injection error (popup will still appear): {e}")

            if result and getattr(self.config, "auto_enter", False):
                import keyboard as kb
                time.sleep(0.05)
                kb.send("enter")

            self.feedback.transcription_complete(transcribed_text)
            _app = getattr(self, "_recording_app", None) or {}
            threading.Thread(
                target=self.db.log_transcription,
                args=(transcribed_text,),
                kwargs={"app_name": _app.get("app_name", ""),
                        "app_exe": _app.get("app_exe", "")},
                daemon=True,
            ).start()
        except Exception as e:
            print(f"[App] Finalize error (popup will still appear): {e}")
        finally:
            self.hotkey_manager.set_idle()
            if self._pending_model_change:
                self._apply_model_change(self._pending_model_change)

        # ── Popup always shown — works as manual-insert fallback if inject failed ─
        # undo_count=0 when injection failed: Replace must NOT Ctrl+Z the user's
        # own prior edits when there is nothing of ours to undo.
        _undo_n = 1 if result else 0
        # Live-injected dictations were typed as many keystroke bursts, so Replace
        # must delete exactly the chars we put in (backspace) rather than one Ctrl+Z.
        _live_dc = _live_del if (result and _live_del) else 0
        self.popup.show_cursor_icon(
            transcribed_text,
            on_insert=lambda t=transcribed_text, h=hwnd: self._insert_text(t, h),
            on_replace=lambda new_text, t=transcribed_text, h=hwnd, uc=_undo_n, dc=_live_dc: self._replace_text(new_text, h, t, undo_count=uc, del_chars=dc),
            on_insert_result=lambda new_text, h=hwnd: self._insert_text(new_text, h),
            inserted=result,
            hwnd=hwnd,
            cursor_x=0,
            cursor_y=0,
            upgrading=upgrading,
            session=seq,
        )

        # Background upgrade — session-stamped so a slow upgrade from dictation
        # N can never attach its text to dictation N+1's popup.
        if upgrading:
            if session is not None:
                def _upgrade_llm(_text=transcribed_text, _seq=seq):
                    final = _text
                    try:
                        fixed = self.ai_refiner.context_fix(_text)
                        if fixed and fixed != _text:
                            print(f"[App] LLM context-fix: '{_text}' -> '{fixed}'")
                            final = fixed
                            self.popup.set_upgrade_result(final, session=_seq)
                        else:
                            self.popup.clear_upgrading(session=_seq)
                    except Exception as e:
                        print(f"[App] LLM context-fix error: {e}")
                        self.popup.clear_upgrading(session=_seq)
                    self._update_context(final)
                threading.Thread(target=_upgrade_llm, daemon=True, name="context-fix").start()
            elif final_audio is not None:
                _fast = transcribed_text
                _upg_ctx = _ctx
                _upg_hw = _hw
                def _upgrade(_audio=final_audio, _rate=capture_rate, _ft=_fast,
                             _ctx=_upg_ctx, _hw=_upg_hw, _seq=seq):
                    accurate = self.transcriber.transcribe(
                        _audio, _rate, context_words=_ctx, hotwords_str=_hw).strip()
                    if not accurate:
                        self.popup.clear_upgrading(session=_seq)
                        return
                    offered = False
                    # Show Whisper result immediately so the upgrade button appears fast
                    if accurate != _ft:
                        print(f"[App] Accurate transcription: '{accurate}'")
                        self.popup.set_upgrade_result(accurate, session=_seq)
                        offered = True
                    final = accurate
                    if self.ai_refiner.is_available:
                        try:
                            fixed = self.ai_refiner.context_fix(accurate)
                            if fixed and fixed != accurate:
                                print(f"[App] LLM context-fix: '{accurate}' -> '{fixed}'")
                                final = fixed
                                self.popup.set_upgrade_result(final, session=_seq)
                                offered = True
                        except Exception as e:
                            print(f"[App] LLM context-fix error, using Whisper result: {e}")
                    if not offered:
                        self.popup.clear_upgrading(session=_seq)
                    self._update_context(final)
                threading.Thread(target=_upgrade, daemon=True, name="accurate-transcription").start()
        else:
            # No upgrade pass — the injected text is the best we'll have; feed
            # it to the rolling context now.
            if session is not None:
                self._update_context(transcribed_text)

    def _on_cancel_recording(self) -> None:
        session = self._session
        self._session = None
        self._caption_stop_event.set()
        self._caption_loop_running.clear()
        try:
            if session is not None:
                session.abort()
            # Wait briefly for an in-flight async start before deciding there is
            # nothing to stop — otherwise a quick tap leaves an orphaned stream
            # recording forever (and prepends stale audio to the next dictation).
            if not self.recorder.is_recording:
                for _ in range(25):
                    if self.recorder.is_recording:
                        break
                    time.sleep(0.02)
            if self.recorder.is_recording:
                self.recorder.stop()
            self.feedback.recording_stopped()
            print("[App] Recording cancelled (short tap).")
        except Exception as e:
            print(f"[App] Error cancelling recording: {e}")
        finally:
            self._cancel_recording_timer()
            self.hotkey_manager.set_idle()
            if self._pending_model_change:
                self._apply_model_change(self._pending_model_change)

    def _cancel_recording_timer(self) -> None:
        t = self._recording_timer
        if t is not None:
            t.cancel()
            self._recording_timer = None

    def _auto_stop_recording(self) -> None:
        """Fired by the auto-stop timer — behaves like the user releasing the hotkey."""
        if self.hotkey_manager.state == AppState.RECORDING:
            print("[App] Auto-stopping recording (timeout reached)")
            if self.hotkey_manager.mode == "toggle":
                self.hotkey_manager._on_key_down()
            else:
                self.hotkey_manager._on_key_up()

    def _get_context_words(self) -> str:
        with self._context_lock:
            return " ".join(self._context_deque)

    def _update_context(self, text: str) -> None:
        with self._context_lock:
            self._context_deque.extend(text.split()[-30:])

    def _get_hotwords(self) -> str:
        return (getattr(self.config, "custom_vocabulary", "") or "").strip()

    def _on_state_change(self, state: AppState) -> None:
        self.app_window.update_status(
            state.value
        )  # "idle" / "recording" / "processing"
        if state == AppState.RECORDING:
            # Capture cursor and foreground window RIGHT NOW, synchronously on the
            # hotkey thread. _on_start_recording runs in a daemon thread and has
            # NOT fired yet — reading _rec_cursor_x/y or _recording_hwnd here
            # would return stale values from the previous recording session.
            try:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                self._recording_hwnd = hwnd
            except Exception:
                hwnd = self._recording_hwnd
            try:
                from app_icons import capture_app_info
                self._recording_app = capture_app_info(hwnd)
            except Exception:
                self._recording_app = {"app_name": "", "app_exe": ""}
            try:
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                cx, cy = pt.x, pt.y
                self._rec_cursor_x, self._rec_cursor_y = cx, cy
            except Exception:
                cx = getattr(self, "_rec_cursor_x", 0)
                cy = getattr(self, "_rec_cursor_y", 0)
            _captions_on = self._captions_active()
            self.popup.set_captions_enabled(_captions_on)
            self.popup.show_status(
                "Recording",
                hwnd=hwnd,
                recording=True,
                cursor_x=cx,
                cursor_y=cy,
            )
            # Always feed mic levels to the popup waveform while recording — the
            # waveform now animates in caption mode too (captions render in a bar
            # beneath it, no longer replacing it).
            if not self._mic_loop_running.is_set():
                self._mic_loop_running.set()
                threading.Thread(target=self._mic_level_loop, daemon=True).start()
            if _captions_on:
                # Live captions render beneath the waveform. In Parakeet mode
                # captions are pushed by the StreamingSession worker; the whisper
                # fallback runs its own caption loop.
                if not self._use_parakeet() and not self._caption_loop_running.is_set():
                    self._caption_stop_event.clear()
                    self._caption_loop_running.set()
                    self._caption_thread = threading.Thread(
                        target=self._caption_loop, daemon=True, name="caption-loop")
                    self._caption_thread.start()
        elif state == AppState.PROCESSING:
            # Stop captions immediately so no caption tick holds the fast model
            # while the final injection pass needs it.
            self._caption_stop_event.set()
            self._caption_loop_running.clear()
            self.popup.show_status(
                "Transcribing…",
                hwnd=self._recording_hwnd,
                recording=False,
                cursor_x=getattr(self, "_rec_cursor_x", 0),
                cursor_y=getattr(self, "_rec_cursor_y", 0),
            )
        elif state == AppState.IDLE:
            self._mic_loop_running.clear()
            self._caption_stop_event.set()
            self._caption_loop_running.clear()
            if not self.popup.is_user_facing:
                self.popup.hide()

    def _mic_level_loop(self) -> None:
        """Sample the recorder's audio buffer for RMS level and push to popup."""
        # recorder.start() runs in a separate thread; wait for it to actually begin
        # before entering the poll loop (up to 1 s) — otherwise is_recording is
        # still False on the first check and the loop exits immediately.
        for _ in range(25):
            if self.recorder.is_recording:
                break
            time.sleep(0.04)

        while self.recorder.is_recording and self._mic_loop_running.is_set():
            try:
                rms, peak = self.recorder.get_live_levels()
                # High gain — typical Windows mic RMS is 0.001-0.02 at default
                # gain settings; multiply aggressively so bars are always visible.
                # No floor: even very quiet audio moves the bars.
                raw = max(rms * 80.0, peak * 25.0)
                level = min(1.0, raw)
                # Fast attack (75 % new) so bars snap up immediately on speech
                self._mic_level_smooth = (self._mic_level_smooth * 0.25) + (
                    level * 0.75
                )
                self.popup.update_mic_level(self._mic_level_smooth)
            except Exception:
                pass
            time.sleep(0.04)
        self._mic_loop_running.clear()
        self._mic_level_smooth = 0.0
        self.popup.update_mic_level(0.0)

    def _captions_active(self) -> bool:
        """True only when live captions are enabled AND a caption-capable engine
        is loaded. If no model is ready yet we fall back to the waveform for
        that recording so the user always gets visual feedback (never a blank bar)."""
        return bool(getattr(self.config, "live_captions", False)) \
            and (self._use_parakeet() or self.fast_transcriber.is_loaded)

    def _caption_loop(self) -> None:
        """Show live captions of what the user is saying while recording.

        Mirrors _mic_level_loop: a daemon gated by _caption_loop_running. Each
        tick re-transcribes the recent tail of the audio buffer with the already
        -loaded fast model (blocking=False, so it never queues ahead of the final
        injection pass) and pushes the text to the popup. It NEVER injects, NEVER
        calls _update_context, and NEVER touches stream state — the post-stop
        injection pipeline is completely unaffected.
        """
        TICK_INTERVAL = 0.7
        TAIL_SECONDS = 8.0
        MIN_AUDIO_SECS = 0.4

        # Wait for recording to actually begin (recorder.start runs in its own thread)
        for _ in range(25):
            if self.recorder.is_recording:
                break
            time.sleep(0.04)

        produced_any = False
        while (
            self.recorder.is_recording
            and self._caption_loop_running.is_set()
            and not self._caption_stop_event.is_set()
        ):
            tick_start = time.time()
            try:
                audio = self.recorder.get_current_audio(max_seconds=TAIL_SECONDS)
                rate = self.recorder.active_sample_rate
                if audio is not None and len(audio) >= rate * MIN_AUDIO_SECS:
                    text = self.fast_transcriber.transcribe(
                        audio, rate,
                        context_words=self._get_context_words(),
                        hotwords_str=self._get_hotwords(),
                        blocking=False,  # never queue — skip the tick if model busy
                    ).strip()
                    if text:
                        produced_any = True
                        self.popup.update_caption(text)
            except Exception as e:
                print(f"[Caption] Tick error: {e}")

            elapsed = time.time() - tick_start
            remaining = TICK_INTERVAL - elapsed
            if remaining > 0:
                # Wait on the STOP event (clear while running) — waiting on the
                # running event returned immediately and busy-spun the loop,
                # pegging cores and starving the final pass of the model lock.
                self._caption_stop_event.wait(remaining)

        self._caption_loop_running.clear()
        if not produced_any:
            print("[Caption] loop ended with no caption text produced")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_window_class(hwnd: int) -> str:
        """Return the Win32 class name of the given window (empty string on failure)."""
        if not hwnd:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(128)
            ctypes.windll.user32.GetClassNameW(hwnd, buf, 128)
            return buf.value
        except Exception:
            return ""

    def _click_to_restore_focus(self, x: int, y: int, hwnd: int = 0) -> None:
        """
        Simulate a left-click at (x, y) to restore DOM focus inside browser
        contenteditable / ProseMirror elements (ChatGPT, Gmail, etc.).

        Only fires if (x, y) is actually within the target window rect — this
        prevents accidentally clicking links, buttons, or empty page areas when
        the cursor was outside the input box when recording started.
        """
        if not x and not y:
            return
        try:
            u32 = ctypes.windll.user32

            # Safety check: only click if the point is inside the target window.
            # If the recording-start cursor was outside Chrome (e.g. on another
            # monitor or on the taskbar), skip the click entirely.
            if hwnd:
                rect = ctypes.wintypes.RECT()
                if u32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    if not (
                        rect.left <= x <= rect.right and rect.top <= y <= rect.bottom
                    ):
                        print(
                            f"[App] Click pos ({x},{y}) outside window rect — skipping"
                        )
                        return

            MOUSEEVENTF_LEFTDOWN = 0x0002
            MOUSEEVENTF_LEFTUP = 0x0004
            # Save current cursor so we can restore it — prevents visible cursor
            # jump from wherever the user moved their mouse during transcription.
            saved_pt = ctypes.wintypes.POINT()
            u32.GetCursorPos(ctypes.byref(saved_pt))
            u32.SetCursorPos(x, y)
            time.sleep(0.03)
            u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.15)  # let Chrome process click and fire JS focus events
            u32.SetCursorPos(saved_pt.x, saved_pt.y)
            print(f"[App] Browser DOM focus click at ({x}, {y})")
        except Exception as e:
            print(f"[App] Click to restore focus failed: {e}")

    def _focus_window(self, hwnd: int, short: bool = False) -> bool:
        """Bring hwnd to the foreground so injected keystrokes land there.
        Retries once if focus doesn't land on the first attempt.
        Returns True if the window is confirmed foreground after the call."""
        if not hwnd:
            return False

        u32 = ctypes.windll.user32

        # Already foreground — nothing to do (saves the full dance + sleeps).
        try:
            if u32.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        # Detect fullscreen-exclusive windows (games, presentations) —
        # they already own the display; attempting SetForegroundWindow can
        # cause a jarring mode-switch. If the window is TOPMOST and has no
        # title bar (WS_CAPTION absent), treat it as fullscreen-exclusive and
        # skip the focus dance entirely.
        try:
            WS_CAPTION = 0x00C00000
            WS_EX_TOPMOST = 0x00000008
            style = u32.GetWindowLongW(hwnd, -16)    # GWL_STYLE
            ex_style = u32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            if (ex_style & WS_EX_TOPMOST) and not (style & WS_CAPTION):
                print(f"[App] Window {hwnd:#x} appears fullscreen-exclusive — skipping SetForegroundWindow")
                return True
        except Exception:
            pass

        for attempt in range(2):
            try:
                kernel32 = ctypes.windll.kernel32

                # Only restore if minimised — avoids un-maximise flicker.
                WS_MINIMIZE = 0x20000000
                style = u32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
                if style & WS_MINIMIZE:
                    u32.ShowWindow(hwnd, 9)  # SW_RESTORE

                # AllowSetForegroundWindow(-1) unlocks the focus lock globally
                u32.AllowSetForegroundWindow(-1)

                # AttachThreadInput bypasses Windows focus-steal restrictions.
                fg_hwnd = u32.GetForegroundWindow()
                fg_tid = u32.GetWindowThreadProcessId(fg_hwnd, None)
                our_tid = kernel32.GetCurrentThreadId()

                attached = bool(fg_tid and fg_tid != our_tid)
                if attached:
                    u32.AttachThreadInput(our_tid, fg_tid, True)

                u32.SetForegroundWindow(hwnd)
                u32.BringWindowToTop(hwnd)
                u32.SetFocus(hwnd)

                if attached:
                    u32.AttachThreadInput(our_tid, fg_tid, False)

                time.sleep(0.05 if short else 0.12)

                actual = u32.GetForegroundWindow()
                if actual == hwnd:
                    return True
                print(
                    f"[App] Focus attempt {attempt + 1}: expected {hwnd:#x}, got {actual:#x}"
                )
            except Exception as e:
                print(f"[App] Focus error (attempt {attempt + 1}): {e}")

        # Retries exhausted. If the target hwnd is still a valid window, the
        # foreground app may be a fullscreen/exclusive window that owns the
        # display (and already has focus). Proceed so injection still fires.
        actual = u32.GetForegroundWindow()
        if actual and u32.IsWindow(hwnd):
            print(
                f"[App] Using current foreground {actual:#x} — target {hwnd:#x} may be fullscreen/exclusive"
            )
            return True

        print(f"[App] Focus failed after retries — injecting anyway")
        return False

    def _get_caret_screen_pos(self, hwnd: int) -> tuple[int, int]:
        """Return caret location in screen coordinates for the target UI thread."""
        try:

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            class _GUITHREADINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("flags", ctypes.c_uint),
                    ("hwndActive", ctypes.c_void_p),
                    ("hwndFocus", ctypes.c_void_p),
                    ("hwndCapture", ctypes.c_void_p),
                    ("hwndMenuOwner", ctypes.c_void_p),
                    ("hwndMoveSize", ctypes.c_void_p),
                    ("hwndCaret", ctypes.c_void_p),
                    ("rcCaret", _RECT),
                ]

            u32 = ctypes.windll.user32
            target = hwnd or u32.GetForegroundWindow()
            if not target:
                return self._get_cursor_pos_fallback()

            tid = u32.GetWindowThreadProcessId(target, None)
            if not tid:
                return self._get_cursor_pos_fallback()

            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if not u32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                return self._get_cursor_pos_fallback()

            caret_hwnd = int(info.hwndCaret or info.hwndFocus or target)
            pt = ctypes.wintypes.POINT(info.rcCaret.right, info.rcCaret.bottom)
            if caret_hwnd:
                u32.ClientToScreen(caret_hwnd, ctypes.byref(pt))

            if pt.x or pt.y:
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        return self._get_cursor_pos_fallback()

    @staticmethod
    def _get_cursor_pos_fallback() -> tuple[int, int]:
        try:
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return int(pt.x), int(pt.y)
        except Exception:
            return 0, 0

    def _restore_target_focus(self, hwnd: int) -> None:
        """Popup Insert/Replace paths: wait for the popup to withdraw, focus the
        target, and (for browsers) click to re-establish DOM focus — the popup's
        refinement panel takes real focus, which clears contenteditable focus."""
        time.sleep(0.25)  # let popup withdraw and OS settle focus
        self._focus_window(hwnd)
        try:
            cls = self._get_window_class(hwnd)
            if cls and (
                cls in ("Chrome_WidgetWin_1", "MozillaWindowClass",
                        "MozillaDialogClass", "Chrome_RenderWidgetHostHWND")
                or cls.startswith(("Chrome_WidgetWin_", "Mozilla", "CEF-"))
            ):
                self._click_to_restore_focus(
                    getattr(self, "_rec_cursor_x", 0),
                    getattr(self, "_rec_cursor_y", 0),
                    hwnd,
                )
        except Exception as e:
            print(f"[App] Focus-restore click error: {e}")

    def _insert_text(self, text: str, hwnd: int) -> None:
        """Manual insert — called when user clicks Insert in the popup."""
        self._restore_target_focus(hwnd)
        self.injector.inject(text)
        print(f"[App] Manual insert: {len(text)} chars")

    def _replace_text(self, new_text: str, hwnd: int, original_text: str = "",
                      undo_count: int = 1, del_chars: int = 0) -> None:
        import keyboard as kb

        self._restore_target_focus(hwnd)
        if del_chars > 0:
            # Live-injected dictation: our text landed as many keystroke bursts, so
            # a single Ctrl+Z won't cleanly remove it. Delete exactly the characters
            # we put in (they end at the caret), then inject the replacement.
            # delete_stream = same-transport deletion (see injector docstring).
            self.injector.delete_stream(del_chars)
            time.sleep(0.10)  # let the deletions process before the paste lands
        else:
            # undo_count=0 means the original injection failed — there is nothing of
            # ours to undo, and Ctrl+Z would destroy the user's own last edit.
            for _ in range(max(0, undo_count)):
                kb.send("ctrl+z")
                time.sleep(0.05)
        self.injector.inject(new_text)
        print(f"[App] Replaced with refined text: '{new_text}'")
        if original_text:
            self.db.log_refinement(original_text, new_text, "replace")

    def _on_refine_selection(self) -> None:
        """Fires when the refine-selection hotkey is pressed."""
        try:
            if self.hotkey_manager.state != AppState.IDLE:
                return
            self._last_dictation_ts = time.time()

            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd and hwnd == self.popup._popup_hwnd:
                return

            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            cx, cy = pt.x, pt.y

            self._focus_window(hwnd)
            time.sleep(0.1)

            from injector import _Input, _KbdInput, _INPUT_KEYBOARD, _KEYEVENTF_KEYUP, _u32, _get_focused_child
            u32 = ctypes.windll.user32

            if _u32.OpenClipboard(None):
                _u32.EmptyClipboard()
                _u32.CloseClipboard()

            WM_COPY = 0x0301
            child = _get_focused_child(hwnd)
            u32.SendMessageW(child, WM_COPY, 0, 0)
            time.sleep(0.15)
            text = self._read_clipboard().strip()

            if not text:
                VK_CTRL, VK_C = 0x11, 0x43
                ctrl_dn = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_CTRL))
                c_dn    = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_C))
                c_up    = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_C, dwFlags=_KEYEVENTF_KEYUP))
                ctrl_up = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_CTRL, dwFlags=_KEYEVENTF_KEYUP))
                u32.SendInput(4, (_Input * 4)(ctrl_dn, c_dn, c_up, ctrl_up), ctypes.sizeof(_Input))
                for _ in range(10):
                    time.sleep(0.05)
                    text = self._read_clipboard().strip()
                    if text:
                        break

            if not text:
                return

            def _do_replace(new_text: str, _hwnd: int = hwnd) -> None:
                time.sleep(0.25)
                self._focus_window(_hwnd)
                time.sleep(0.1)
                self.injector.inject(new_text)

            self.popup.show_cursor_icon(
                text,
                on_insert=lambda t=text, h=hwnd: self._insert_text(t, h),
                on_replace=_do_replace,
                inserted=True,
                hwnd=hwnd,
                cursor_x=cx,
                cursor_y=cy,
            )

        except Exception as e:
            print(f"[App] Refine selection error: {e}")

    def _read_clipboard(self) -> str:
        """Read text from clipboard using properly typed ctypes (64-bit safe)."""
        from injector import _u32, _k32
        CF_UNICODETEXT = 13
        if not _u32.OpenClipboard(None):
            return ""
        try:
            h = _u32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            ptr = _k32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                _k32.GlobalUnlock(h)
        except Exception:
            return ""
        finally:
            _u32.CloseClipboard()

    def _open_config(self) -> None:
        config_path = self.config._config_path
        if os.path.exists(config_path):
            os.startfile(config_path)
            print(f"[App] Opened config: {config_path}")

    def _on_sign_in(self, auth) -> None:
        """Called (on a daemon thread) after the user signs in via the overlay."""
        if auth._client:
            self.db.set_client(auth._client)
        self.db.set_user(auth.user_id)
        self.tray.set_user_email(auth.user_email or "")
        print(f"[App] Signed in as {auth.user_email}")
        if self.db.is_enabled:
            threading.Thread(
                target=self._fetch_remote_api_keys, daemon=True, name="api-key-fetch"
            ).start()

    def _sign_out(self) -> None:
        print("[App] Signing out...")
        self._auth.sign_out()
        self._auth.sign_in_offline()   # back to offline state immediately
        self.db.set_user(None)
        self.tray.set_user_email("")
        if self.app_window._root:
            self.app_window._root.after(0, self.app_window._apply_auth_ui)
        print("[App] Signed out — running in offline mode.")

    def _shutdown(self) -> None:
        print("[App] Shutting down...")
        self.hotkey_manager.unregister()
        self.refine_hotkey_manager.unregister()
        if self.recorder.is_recording:
            self.recorder.stop()

    def _shutdown_and_destroy(self) -> None:
        """Called from tray Quit — shuts down and ends the tkinter mainloop."""
        self._shutdown()
        if self.app_window._root:
            self.app_window._root.after(0, self.app_window._root.destroy)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


_SINGLETON_MUTEX = None  # kept alive at module level so the handle isn't GC'd
_LOCAL_PORT = 47832
_INSTALL_COPY_LOCK = threading.Lock()  # serialises _ensure_installed_copy across startup threads


def _start_local_server(app_window, version: str) -> None:
    """
    Tiny localhost-only HTTP server so the FTC web app can detect whether
    FTC Whisper is running and surface its window without needing a custom
    URL protocol registration.  Binds to 127.0.0.1 only — not reachable
    from the network.

    Endpoints:
      GET /ping    → {"ok": true, "version": "x.y.z"}
      GET /show    → brings the window to the front, returns {"ok": true}
      GET /update  → checks GitHub; if newer opens browser download, returns {"action":"updating"} or {"action":"up_to_date"}
    """
    import http.server
    import json
    import socketserver

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # silence access log spam

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/show"):
                app_window.show()
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            elif self.path.startswith("/ping"):
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"ok": True, "version": version}).encode()
                )
            elif self.path.startswith("/update"):
                from updater import cached_release, is_newer, download_update, apply_update, current_exe_path, verify_exe
                import tempfile
                info = cached_release()
                if info and is_newer(info["version"], version):
                    # Respond immediately so FTC Contacts never times out
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true,"action":"updating"}')
                    exe_path = current_exe_path()
                    if exe_path:
                        def _download_and_apply(url=info["download_url"], dst=exe_path):
                            tmp = os.path.join(tempfile.gettempdir(), "FTC-Whisper-update.exe")
                            try:
                                download_update(url, tmp, lambda *_: None)
                                verify_exe(tmp)
                                apply_update(tmp, dst)
                            except Exception:
                                pass
                        threading.Thread(target=_download_and_apply, daemon=True, name="in-app-update").start()
                    else:
                        import webbrowser
                        webbrowser.open(info["download_url"])
                else:
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true,"action":"up_to_date"}')
            else:
                self.send_response(404)
                self.end_headers()

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        server = _Server(("127.0.0.1", _LOCAL_PORT), _Handler)
        threading.Thread(
            target=server.serve_forever, daemon=True, name="local-server"
        ).start()
        print(f"[App] Local server listening on http://127.0.0.1:{_LOCAL_PORT}")
    except OSError as e:
        print(f"[App] Local server unavailable (port {_LOCAL_PORT} in use?): {e}")


def _ensure_single_instance() -> None:
    """
    Use a named Windows mutex to enforce one running instance.
    If another instance already owns the mutex, bring its window to the
    foreground and exit immediately instead of launching a second copy.
    """
    global _SINGLETON_MUTEX
    ERROR_ALREADY_EXISTS = 183
    MUTEX_NAME = "Global\\FTC_Whisper_SingleInstance"

    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    err = kernel32.GetLastError()

    if err == ERROR_ALREADY_EXISTS:
        # Another instance is running — ask it to show itself via the local HTTP
        # server (/show calls app_window.show() → deiconify() + Win32 focus).
        # Using the HTTP endpoint is essential: raw Win32 ShowWindow bypasses
        # tkinter's state machine and produces a blank/tiny window when the
        # existing instance hid itself via withdraw().
        import urllib.request
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{_LOCAL_PORT}/show", timeout=2
            )
        except Exception:
            pass
        # os._exit, not sys.exit: a duplicate instance owns nothing and must die
        # immediately. sys.exit only raises SystemExit, which a non-daemon thread
        # or a wedged DLL-init (e.g. an OOM during model preload) can swallow,
        # leaving a 1-thread zombie that keeps the installed exe's image locked —
        # which then makes the auto-update swap's Copy-Item fail every retry.
        os._exit(0)

    # We are the first instance — hold the mutex for the process lifetime.
    _SINGLETON_MUTEX = mutex


TASK_NAME = "FTC Whisper"


def _app_data_dir() -> str:
    """Stable per-user data dir (logs + the canonical installed exe copy)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    d = os.path.join(base, "FTC Whisper")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _startup_log_path() -> str:
    return os.path.join(_app_data_dir(), "startup-error.log")


def _stable_exe_path() -> str:
    return os.path.join(_app_data_dir(), "FTC Whisper.exe")


def _ensure_installed_copy() -> str:
    """
    Frozen builds only: keep a canonical copy of the exe at a STABLE path
    (%LOCALAPPDATA%\\FTC Whisper) and return that path.

    Auto-launch must never point at the volatile location the user happened to
    double-click from (Downloads, a temp dir, a USB stick). If the running exe
    lives elsewhere, copy it to the stable location (refreshing it when the
    running exe is newer, e.g. right after an update). Returns the current exe
    path if anything goes wrong, so registration still works.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable

    import shutil

    current = sys.executable
    target = _stable_exe_path()
    # Serialised: the startup-task and url-protocol threads both call this at
    # launch. Two concurrent 130MB copy2 calls made one thread hit the half-
    # written/locked file, fall into the exception path, and register the
    # VOLATILE current path (Downloads, dist, ...) as the boot target.
    with _INSTALL_COPY_LOCK:
        try:
            if os.path.normcase(os.path.abspath(current)) == os.path.normcase(target):
                return target  # already running from the stable copy
            needs_copy = (not os.path.exists(target)) or (
                os.path.getmtime(current) > os.path.getmtime(target)
            )
            if needs_copy:
                # Copy to a temp name then atomically replace — a crash mid-copy
                # can never leave a truncated exe at the stable path.
                tmp = target + ".staging"
                shutil.copy2(current, tmp)
                os.replace(tmp, target)
                print(f"[App] Installed canonical copy at {target}")
            return target
        except Exception as e:
            print(f"[App] Could not stage stable exe copy ({e}); using current path.")
            return current


def _startup_target() -> str:
    """The path (frozen) used by every auto-launch mechanism — always stable."""
    if getattr(sys, "frozen", False):
        return _ensure_installed_copy()
    return os.path.join(os.path.dirname(sys.executable), "pythonw.exe")


def _file_version_tuple(path: str) -> tuple:
    """Read the FileVersion resource of a Windows exe. (0,0,0,0) on failure."""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return (0, 0, 0, 0)
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return (0, 0, 0, 0)
        ptr = ctypes.c_void_p()
        plen = ctypes.c_uint()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(plen)):
            return (0, 0, 0, 0)

        class _FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.c_uint32),
                ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32),
                ("dwFileVersionLS", ctypes.c_uint32),
            ]

        ffi = ctypes.cast(ptr, ctypes.POINTER(_FIXEDFILEINFO)).contents
        ms, ls = ffi.dwFileVersionMS, ffi.dwFileVersionLS
        return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return (0, 0, 0, 0)


def _handoff_to_canonical_if_newer() -> None:
    """If this frozen exe is an OLD copy (Downloads, a stale shortcut target)
    and the canonical installed exe is a newer version — because auto-update
    refreshed it — launch the canonical exe and exit. Double-clicking any old
    link therefore always ends up running the updated version."""
    if not getattr(sys, "frozen", False):
        return
    current = os.path.abspath(sys.executable)
    target = _stable_exe_path()
    if os.path.normcase(current) == os.path.normcase(target):
        return
    if not os.path.exists(target):
        return
    cur_v = _file_version_tuple(current)
    tgt_v = _file_version_tuple(target)
    # Only defer to a strictly newer install: when we're same-or-newer the
    # normal path runs (and _ensure_installed_copy refreshes the canonical
    # copy from us). Strict comparison also makes handoff ping-pong impossible.
    if tgt_v <= cur_v or tgt_v == (0, 0, 0, 0):
        return
    try:
        import subprocess
        subprocess.Popen(
            [target] + sys.argv[1:],
            cwd=os.path.dirname(target),
            close_fds=True,
        )
        print(f"[App] This copy is v{'.'.join(map(str, cur_v))}; handing off to "
              f"installed v{'.'.join(map(str, tgt_v))} at {target}.")
        os._exit(0)
    except Exception as e:
        print(f"[App] Handoff to installed version failed ({e}) — continuing.")


def _ensure_startup_task() -> None:
    """
    Register FTC Whisper as a Task Scheduler logon task pointing at the STABLE
    exe path, then reconcile away every legacy/duplicate launcher so exactly one
    auto-start mechanism exists (no boot-time double-launch race).

    Falls back to the registry Run key only if schtasks is unavailable.
    """
    import subprocess

    target = _startup_target()
    script = "" if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    exe_cmd = f'"{target}"' + (f' "{script}"' if script else "")

    _NO_WIN = subprocess.CREATE_NO_WINDOW

    # Re-register if the task is missing OR no longer points at the stable path.
    # (The old check accepted any task that merely mentioned the exe substring,
    # so it never repaired a task pointing at a stale/Downloads path.)
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST", "/v"],
            capture_output=True, text=True, creationflags=_NO_WIN,
        )
        if result.returncode == 0 and os.path.normcase(target) in os.path.normcase(result.stdout):
            # Re-register if the task was created with the old below-normal priority (6).
            # Priority 4 = above-normal in the Task Scheduler scale (0=highest, 10=lowest).
            if "<Priority>4</Priority>" in result.stdout or "Priority: 4" in result.stdout:
                _reconcile_legacy_launchers(task_ok=True)
                return  # already correct
            # Fall through to re-register with the correct priority.
    except Exception:
        pass

    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    user_id = f"{domain}\\{user}" if domain and user else user

    arguments = "" if getattr(sys, "frozen", False) else f"<Arguments>{script}</Arguments>"

    # Create / overwrite the task — ONLOGON, current user, no elevation needed
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user_id}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Priority>4</Priority>
  </Settings>
  <Actions>
    <Exec>
      <Command>{target}</Command>
      {arguments}
    </Exec>
  </Actions>
</Task>"""

    try:
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False, encoding="utf-16"
        ) as tf:
            tf.write(xml)
            xml_path = tf.name

        result = subprocess.run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/xml", xml_path, "/f"],
            capture_output=True, text=True, creationflags=_NO_WIN,
        )
        os.unlink(xml_path)

        if result.returncode == 0:
            print(f"[App] Startup task registered (Task Scheduler): {exe_cmd}")
            _reconcile_legacy_launchers(task_ok=True)
        else:
            raise RuntimeError(result.stderr.strip())

    except Exception as e:
        print(f"[App] Task Scheduler registration failed ({e}), falling back to registry")
        _ensure_startup_registry_fallback()

    _repair_desktop_shortcut(target)


def _repair_desktop_shortcut(target: str) -> None:
    """If a 'FTC Whisper' desktop shortcut exists but points anywhere other
    than the canonical installed exe (an old Downloads copy, a moved file),
    retarget it — so the user's original link always opens the version that
    auto-update maintains. Never creates a shortcut that isn't there."""
    if not getattr(sys, "frozen", False):
        return
    import subprocess
    t = target.replace("'", "''")
    ps = (
        "$sh = New-Object -ComObject WScript.Shell; "
        "$p = Join-Path $sh.SpecialFolders('Desktop') 'FTC Whisper.lnk'; "
        "if (Test-Path $p) { $lnk = $sh.CreateShortcut($p); "
        f"if ($lnk.TargetPath -ne '{t}') {{ $lnk.TargetPath = '{t}'; "
        f"$lnk.WorkingDirectory = '{os.path.dirname(target).replace(chr(39), chr(39)*2)}'; "
        "$lnk.Arguments = ''; $lnk.Save(); Write-Output 'retargeted' } }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if "retargeted" in (r.stdout or ""):
            print(f"[App] Desktop shortcut retargeted to {target}")
    except Exception as e:
        print(f"[App] Desktop shortcut repair skipped ({e})")


def _reconcile_legacy_launchers(task_ok: bool) -> None:
    """
    Remove every competing/legacy auto-start entry so there is ONE source of
    truth. When the Task Scheduler task is healthy this deletes the HKCU\\Run
    fallback value and any stale Startup-folder shortcuts (current + historical
    names) that older versions created — these were racing the task at boot and
    one pointed at a path that no longer exists.
    """
    if not task_ok:
        return

    # 1. HKCU Run fallback value
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        ) as k:
            try:
                winreg.DeleteValue(k, "FTC Whisper")
                print("[App] Removed duplicate HKCU\\Run launcher.")
            except FileNotFoundError:
                pass
    except Exception as e:
        print(f"[App] Could not clean Run key: {e}")

    # 2. Stale Startup-folder shortcuts (current + historical names)
    try:
        startup_dir = os.path.join(
            os.environ.get("APPDATA", ""),
            r"Microsoft\Windows\Start Menu\Programs\Startup",
        )
        for name in ("FTC Whisper.lnk", "FTC Transcribe.lnk"):
            lnk = os.path.join(startup_dir, name)
            if os.path.exists(lnk):
                os.remove(lnk)
                print(f"[App] Removed stale Startup shortcut: {name}")
    except Exception as e:
        print(f"[App] Could not clean Startup folder: {e}")


def _register_url_protocol() -> None:
    """
    Register ftcwhisper:// as a Windows URL protocol so browsers can launch the app.
    Runs each startup so the path stays current after an update or move.
    """
    import winreg

    target = _startup_target()
    if getattr(sys, "frozen", False):
        cmd = f'"{target}" "%1"'
    else:
        script = os.path.abspath(__file__)
        cmd    = f'"{target}" "{script}" "%1"'

    try:
        base = r"Software\Classes\ftcwhisper"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "",             0, winreg.REG_SZ, "URL:FTC Whisper")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
        print("[App] Registered ftcwhisper:// URL protocol handler.")
    except Exception as e:
        print(f"[App] Could not register URL protocol: {e}")


def _ensure_startup_registry_fallback() -> None:
    """Registry Run key fallback — used only if Task Scheduler is unavailable."""
    import winreg

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE   = "FTC Whisper"

    target = _startup_target()
    if getattr(sys, "frozen", False):
        cmd = f'"{target}"'
    else:
        script = os.path.abspath(__file__)
        cmd = f'"{target}" "{script}"'

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as k:
            try:
                current, _ = winreg.QueryValueEx(k, VALUE)
                if current == cmd:
                    return
            except FileNotFoundError:
                pass
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, cmd)
            print(f"[App] Startup registry key set: {cmd}")
    except Exception as e:
        print(f"[App] Could not set startup registry: {e}")


def _log_startup_error(exc: BaseException) -> None:
    """Append an uncaught startup exception to a log file next to the app data.

    The frozen build is console=False, so a launch-then-crash (e.g. when the
    Task Scheduler logon task fires at boot) is otherwise completely invisible
    and reports only a non-zero task result. This makes 'launched but died'
    diagnosable.
    """
    import traceback
    try:
        with open(_startup_log_path(), "a", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write(f"FTC Whisper {APP_VERSION} startup crash\n")
            f.write(f"argv={sys.argv} exe={sys.executable}\n")
            f.write(traceback.format_exc() if exc else "")
            f.write("\n")
    except Exception:
        pass


def _thread_excepthook(args) -> None:
    """Log uncaught exceptions from ANY daemon thread.

    Pipeline work (the _upgrade() upgrade pass, _mic_level_loop, caption loop,
    Supabase logging) all run in daemon threads. In the frozen console=False
    build an exception that escapes their inner try-blocks otherwise vanishes
    with no trace, leaving the app silently degraded. Route them to the same
    log file so failures are diagnosable.
    """
    import traceback
    if args.exc_type is SystemExit:
        return
    try:
        with open(_startup_log_path(), "a", encoding="utf-8") as f:
            f.write("-" * 60 + "\n")
            f.write(f"Uncaught exception in thread {args.thread.name if args.thread else '?'}\n")
            f.write("".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback)))
            f.write("\n")
    except Exception:
        pass
    # Still print so it shows in a console/source run.
    print(f"[App] Uncaught thread exception in {args.thread.name if args.thread else '?'}: {args.exc_value}")


def main() -> None:
    try:
        threading.excepthook = _thread_excepthook
    except Exception:
        pass
    try:
        _main()
    except SystemExit:
        raise
    except BaseException as e:
        _log_startup_error(e)
        raise


def _main() -> None:
    if sys.platform == "win32":
        # BEFORE the mutex: an old copy that defers to the installed version
        # must not be holding the single-instance mutex when the new exe starts.
        _handoff_to_canonical_if_newer()
        _ensure_single_instance()
        # Run in background — schtasks can be slow on first launch and there's
        # no reason to block the UI thread waiting for a Task Scheduler write.
        threading.Thread(
            target=_ensure_startup_task, daemon=True, name="startup-task"
        ).start()
        threading.Thread(target=_register_url_protocol, daemon=True, name="url-protocol").start()
        # Priority already boosted to ABOVE_NORMAL at module import time (top of file).
        try:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print(
                    "[App] Note: running without admin — some hotkeys may not work "
                    "in elevated windows."
                )
        except Exception:
            pass

    config = Config.load()
    auth = AuthManager(config.supabase_url, config.supabase_key)

    auth_enabled = bool(config.supabase_url and config.supabase_key)

    if not auth_enabled:
        auth.sign_in_offline()  # No Supabase configured — skip login
    # else: a saved session is restored ASYNCHRONOUSLY by AppWindow's
    # session-restore loop (first attempt fires immediately). set_session()
    # refreshes the usually-expired access token over the network — doing it
    # here blocked the first paint for seconds (or the full network timeout
    # when launched at boot before Wi-Fi is up).

    app = WhisperFlowApp(auth, config)
    app.run()


if __name__ == "__main__":
    main()
