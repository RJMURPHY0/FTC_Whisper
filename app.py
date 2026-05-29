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

from config import Config
from recorder import Recorder
from transcriber import Transcriber
from injector import Injector
from hotkey_manager import HotkeyManager, TriggerHotkeyManager, AppState
from feedback import Feedback
from tray import TrayApp
from popup import FloatingPopup
from ai_refiner import AIRefiner
from supabase_client import SupabaseLogger
from auth import AuthManager
from app_window import AppWindow

APP_VERSION = "1.3.3"


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
        self.transcriber = Transcriber(
            model_size=config.whisper_model,
            language=config.language,
        )
        # Fast model: injects text immediately; accurate model refines in background
        self.fast_transcriber = Transcriber(model_size="base.en")
        self.recorder = Recorder(
            sample_rate=config.sample_rate,
            input_device=getattr(config, "input_device", ""),
        )
        self.injector = Injector(method=config.inject_method)

        # ── AI + logging ───────────────────────────────────────────────
        self.ai_refiner = AIRefiner(api_key=config.anthropic_api_key)
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

        self._recording_hwnd: int = 0
        self._mic_loop_running = threading.Event()
        self._mic_level_smooth = 0.0

        # Rolling context: last ~30 words from recent transcriptions fed into Whisper initial_prompt
        self._context_deque: deque = deque(maxlen=150)
        self._context_lock = threading.Lock()

        # Streaming transcription state
        self._stream_last_text: str = ""
        self._stream_inject_count: int = 0
        self._stream_lock = threading.Lock()
        self._stream_stop_event = threading.Event()
        self._stream_thread = None

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

        # ── Pre-load both models immediately in background ────────────
        threading.Thread(
            target=self.transcriber.load_model, daemon=True, name="model-preload"
        ).start()
        threading.Thread(
            target=self.fast_transcriber.load_model, daemon=True, name="fast-model-preload"
        ).start()

        # ── Local HTTP server so the web app can detect + surface this window ──
        _start_local_server(self.app_window, APP_VERSION)

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

        # If no local API key, try to fetch from Supabase app_settings
        if not self.ai_refiner.is_available and self.db.is_enabled:
            key = self.db.fetch_app_setting("anthropic_api_key")
            if key:
                self.ai_refiner.update_api_key(key)
                self.popup.set_ai_refiner(self.ai_refiner)
                print("[App] Loaded Anthropic API key from Supabase.")

        if self.ai_refiner.is_available:
            print("[App] AI refinement enabled.")
        else:
            print("[App] AI refinement disabled — set anthropic_api_key in config or add to Supabase app_settings.")

        if self.db.is_enabled:
            print(f"[App] Supabase logging enabled.")
        else:
            print("[App] Supabase logging disabled — set supabase_url/key in config.")

        # Pre-load model
        self.transcriber.load_model()
        print("[App] Ready! Hold the hotkey and start speaking.")

        # Register global hotkeys
        self.hotkey_manager.register()
        self.refine_hotkey_manager.register()
        print(f"[App] Hotkey: '{self.config.hotkey}' | Mode: {self.config.mode}")
        print(f"[App] Refine hotkey: '{self.config.refine_hotkey}'")

        # Start tray in daemon thread (safe on Windows)
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()

        # Check for updates in the background; show banner in Settings if found
        self._start_update_check()

    def _start_update_check(self) -> None:
        from updater import check_for_update

        def _on_update_found(version: str, url: str) -> None:
            print(f"[App] Update available: {version}")
            self.app_window._root.after(
                0, lambda: self.app_window.show_update_banner(version, url)
            )

        check_for_update(APP_VERSION, _on_update_found)

    def _on_hotkey_change(self, new_hotkey: str) -> None:
        """Called when the user saves a new hotkey in the dashboard."""
        print(f"[App] Updating hotkey to: {new_hotkey}")
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
        elif key == "input_device":
            self.recorder.input_device = value.strip() if value else ""
        elif key == "sound_feedback":
            self.feedback.sound_enabled = bool(value)

    # ------------------------------------------------------------------
    # Recording pipeline
    # ------------------------------------------------------------------

    def _on_start_recording(self) -> None:
        try:
            try:
                self._recording_hwnd = ctypes.windll.user32.GetForegroundWindow()
                print(f"[App] Recording started, target hwnd={self._recording_hwnd:#x}")
            except Exception:
                self._recording_hwnd = 0
            # Capture mouse position now — user is hovering near the target text field
            try:
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                self._rec_cursor_x, self._rec_cursor_y = pt.x, pt.y
            except Exception:
                self._rec_cursor_x, self._rec_cursor_y = 0, 0
            self.feedback.recording_started()
            self.recorder.start()
            # Streaming mid-recording injection is disabled — text is inserted only after release
            self._stream_stop_event.clear()
            self._stream_last_text = ""
            self._stream_inject_count = 0
            self._stream_thread = None
        except Exception as e:
            print(f"[App] Failed to start recording: {e}")
            self.feedback.error_occurred(str(e))
            self.hotkey_manager.set_idle()

    def _on_stop_recording(self) -> None:
        # Stop streaming loop and capture its final state before anything else
        self._stream_stop_event.set()
        _st = self._stream_thread
        if _st is not None and _st.is_alive():
            _st.join(timeout=0.5)
        with self._stream_lock:
            _stream_last = self._stream_last_text
            _stream_count = self._stream_inject_count

        transcribed_text: str = ""
        hwnd = self._recording_hwnd
        upgrading = False
        final_audio = None
        capture_rate = 1
        _inject_text = ""
        _total_inject_count = 0

        try:
            audio = self.recorder.stop()
            self.feedback.recording_stopped()
            capture_rate = max(1, self.recorder.active_sample_rate)

            if audio is None or len(audio) < capture_rate * 0.3:
                print("[App] Recording too short, ignoring.")
                self.hotkey_manager.set_idle()
                self.feedback.error_occurred("Recording too short")
                return

            # When streaming was active, match its 30 s window so _stream_extension
            # can find the extension point; otherwise allow up to 5 min.
            _window_secs = 30.0 if _stream_count > 0 else 300.0
            max_samples = int(capture_rate * _window_secs)
            final_audio = audio[-max_samples:] if len(audio) > max_samples else audio
            print(
                f"[App] Transcribing {len(final_audio) / capture_rate:.1f}s of audio at {capture_rate} Hz..."
            )
            # Fast pass — inject only the delta beyond what streaming already inserted
            _ctx = self._get_context_words()
            _hw  = self._get_hotwords()
            fast_text = self.fast_transcriber.transcribe(
                final_audio, capture_rate, context_words=_ctx, hotwords_str=_hw).strip()
            if fast_text:
                transcribed_text = fast_text
                upgrading = True
                print(f"[App] Fast transcription: '{fast_text}'")
                if _stream_count > 0 and _stream_last:
                    delta = self._stream_extension(_stream_last, fast_text)
                    if delta:
                        _inject_text = delta
                        _total_inject_count = _stream_count + 1
                    else:
                        # Streaming covered it all (or Whisper revised — avoid double-inject)
                        _inject_text = ""
                        _total_inject_count = _stream_count
                else:
                    _inject_text = fast_text
                    _total_inject_count = 1
            else:
                # Fast model found nothing — wait for accurate model
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
                if _stream_count > 0 and _stream_last:
                    delta = self._stream_extension(_stream_last, text)
                    if delta:
                        _inject_text = delta
                        _total_inject_count = _stream_count + 1
                    else:
                        _inject_text = ""
                        _total_inject_count = _stream_count
                else:
                    _inject_text = text
                    _total_inject_count = 1

        except Exception as e:
            print(f"[App] Transcription pipeline error: {e}")
            import traceback

            traceback.print_exc()
            self.feedback.error_occurred(str(e))
            self.hotkey_manager.set_idle()
            return

        # ── Injection — isolated so a failure never prevents the popup ──────────
        self._focus_window(hwnd)

        # Browser windows (ChatGPT, Gmail, Outlook web, etc.) — Win32
        # SetForegroundWindow restores the Chrome/Firefox Win32 focus but does
        # NOT restore the JS/DOM focus of the contenteditable or ProseMirror
        # input. Simulate a click at the recording-start cursor position to
        # re-establish the browser's internal focus before Ctrl+V.
        _BROWSER_PREFIXES = ("Chrome_WidgetWin_", "Mozilla", "CEF-")
        _BROWSER_EXACT = {
            "Chrome_WidgetWin_1",
            "MozillaWindowClass",
            "MozillaDialogClass",
            "Chrome_RenderWidgetHostHWND",
        }
        # Only click to restore DOM focus when streaming hasn't already injected text.
        # If streaming injected words the cursor is already positioned correctly —
        # clicking at the recording-start position would move it backward and cause
        # the final delta to land mid-sentence.
        if _stream_count == 0:
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

        result = False
        try:
            if _inject_text:
                print(f"[App] Injecting delta: {len(_inject_text)} chars")
                result = self.injector.inject(_inject_text)
                print(f"[App] Inject result: {result}")
            elif _stream_count > 0:
                result = True  # streaming already injected successfully
        except Exception as e:
            print(f"[App] Injection error (popup will still appear): {e}")

        self.feedback.transcription_complete(transcribed_text)
        self.hotkey_manager.set_idle()
        threading.Thread(target=self.db.log_transcription, args=(transcribed_text,), daemon=True).start()

        # ── Popup always shown — works as manual-insert fallback if inject failed ─
        _undo_n = max(1, _total_inject_count)
        self.popup.show_cursor_icon(
            transcribed_text,
            on_insert=lambda t=transcribed_text, h=hwnd: self._insert_text(t, h),
            on_replace=lambda new_text, t=transcribed_text, h=hwnd, uc=_undo_n: self._replace_text(new_text, h, t, undo_count=uc),
            inserted=result,
            hwnd=hwnd,
            cursor_x=0,
            cursor_y=0,
            upgrading=upgrading,
        )

        # If fast model was used, run accurate transcription + LLM context-fix in background
        if upgrading and final_audio is not None:
            _fast = transcribed_text
            _upg_ctx = _ctx
            _upg_hw  = _hw
            def _upgrade(_audio=final_audio, _rate=capture_rate, _ft=_fast,
                         _ctx=_upg_ctx, _hw=_upg_hw):
                accurate = self.transcriber.transcribe(
                    _audio, _rate, context_words=_ctx, hotwords_str=_hw).strip()
                if not accurate:
                    return
                final = accurate
                if self.ai_refiner.is_available:
                    try:
                        fixed = self.ai_refiner.context_fix(accurate)
                        if fixed and fixed != accurate:
                            print(f"[App] LLM context-fix: '{accurate}' -> '{fixed}'")
                            final = fixed
                    except Exception as e:
                        print(f"[App] LLM context-fix error, using Whisper result: {e}")
                self._update_context(final)
                if final != _ft:
                    print(f"[App] Accurate transcription: '{final}'")
                    self.popup.set_upgrade_result(final)
            threading.Thread(target=_upgrade, daemon=True, name="accurate-transcription").start()

    def _on_cancel_recording(self) -> None:
        self._stream_stop_event.set()
        try:
            if self.recorder.is_recording:
                self.recorder.stop()
            _st = self._stream_thread
            if _st is not None and _st.is_alive():
                _st.join(timeout=1.0)
            with self._stream_lock:
                _stream_count = self._stream_inject_count
            self.feedback.recording_stopped()
            print("[App] Recording cancelled (short tap).")
            if _stream_count > 0:
                hwnd = self._recording_hwnd
                self._focus_window(hwnd, short=True)
                import keyboard as kb
                for _ in range(_stream_count):
                    kb.send("ctrl+z")
                    time.sleep(0.04)
                print(f"[App] Undid {_stream_count} streaming injection(s)")
        except Exception as e:
            print(f"[App] Error cancelling recording: {e}")
        finally:
            self.hotkey_manager.set_idle()

    def _get_context_words(self) -> str:
        with self._context_lock:
            return " ".join(self._context_deque)

    def _update_context(self, text: str) -> None:
        with self._context_lock:
            self._context_deque.extend(text.split()[-30:])

    def _get_hotwords(self) -> str:
        return (getattr(self.config, "custom_vocabulary", "") or "").strip()

    def _streaming_loop(self, stop_event: threading.Event) -> None:
        """Append-only live transcription: inject new words every ~900 ms while recording."""
        TICK_INTERVAL = 0.9
        MIN_AUDIO_SECS = 0.6

        last_text = ""
        inject_count = 0

        for _ in range(25):
            if self.recorder.is_recording:
                break
            time.sleep(0.04)

        while not stop_event.is_set() and self.recorder.is_recording:
            tick_start = time.time()
            try:
                audio = self.recorder.get_current_audio(max_seconds=30.0)
                rate = self.recorder.active_sample_rate
                if audio is not None and len(audio) >= rate * MIN_AUDIO_SECS:
                    ctx = self._get_context_words()
                    hw = self._get_hotwords()
                    result = self.fast_transcriber.transcribe(
                        audio, rate,
                        context_words=ctx,
                        hotwords_str=hw,
                        blocking=False,
                    ).strip()
                    if result:
                        new_words = self._stream_extension(last_text, result)
                        if new_words:
                            # Strip trailing punctuation — mid-sentence chunks shouldn't end with a period
                            suffix = new_words.rstrip(".,!?;:") + " "
                            self.injector.inject_immediate(suffix)
                            inject_count += 1
                            last_text = result
                            print(f"[Stream] +'{new_words}'")
            except Exception as e:
                print(f"[Stream] Tick error: {e}")

            elapsed = time.time() - tick_start
            remaining = TICK_INTERVAL - elapsed
            if remaining > 0:
                stop_event.wait(remaining)

        with self._stream_lock:
            self._stream_last_text = last_text
            self._stream_inject_count = inject_count
        print(f"[Stream] Ended — {inject_count} injection(s), last='{last_text[:60]}'")

    @staticmethod
    def _stream_extension(prev: str, curr: str) -> str:
        """Return the new words if curr cleanly extends prev; empty string if not."""
        prev_words = prev.split()
        curr_words = curr.split()
        if len(curr_words) <= len(prev_words):
            return ""
        prev_norm = [w.rstrip(".,!?;:") for w in prev_words]
        curr_norm = [w.rstrip(".,!?;:") for w in curr_words]
        if curr_norm[: len(prev_words)] == prev_norm:
            return " ".join(curr_words[len(prev_words) :])
        return ""

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
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                cx, cy = pt.x, pt.y
                self._rec_cursor_x, self._rec_cursor_y = cx, cy
            except Exception:
                cx = getattr(self, "_rec_cursor_x", 0)
                cy = getattr(self, "_rec_cursor_y", 0)
            self.popup.show_status(
                "Recording",
                hwnd=hwnd,
                recording=True,
                cursor_x=cx,
                cursor_y=cy,
            )
            # Feed mic levels to the popup waveform while recording
            if not self._mic_loop_running.is_set():
                self._mic_loop_running.set()
                threading.Thread(target=self._mic_level_loop, daemon=True).start()
        elif state == AppState.PROCESSING:
            self.popup.show_status(
                "Transcribing…",
                hwnd=self._recording_hwnd,
                recording=False,
                cursor_x=getattr(self, "_rec_cursor_x", 0),
                cursor_y=getattr(self, "_rec_cursor_y", 0),
            )
        elif state == AppState.IDLE:
            self._mic_loop_running.clear()
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
            # Move cursor to target, then click at current position (dx/dy = 0
            # in non-absolute mode means "at wherever the cursor now is").
            u32.SetCursorPos(x, y)
            time.sleep(0.05)
            u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.03)
            u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.12)  # let Chrome process click and re-focus the element
            print(f"[App] Browser DOM focus click at ({x}, {y})")
        except Exception as e:
            print(f"[App] Click to restore focus failed: {e}")

    def _focus_window(self, hwnd: int, short: bool = False) -> bool:
        """Bring hwnd to the foreground so injected keystrokes land there.
        Retries once if focus doesn't land on the first attempt.
        Returns True if the window is confirmed foreground after the call."""
        if not hwnd:
            return False
        for attempt in range(2):
            try:
                u32 = ctypes.windll.user32
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

    def _insert_text(self, text: str, hwnd: int) -> None:
        """Manual insert — called when user clicks Insert in the popup.
        Waits for the popup to fully close before focusing the target window,
        otherwise the popup steals focus back and the injection lands nowhere."""
        time.sleep(0.25)  # let popup withdraw and OS settle focus
        self._focus_window(hwnd)
        self.injector.inject(text)
        print(f"[App] Manual insert: {len(text)} chars")

    def _replace_text(self, new_text: str, hwnd: int, original_text: str = "", undo_count: int = 1) -> None:
        import keyboard as kb

        self._focus_window(hwnd)
        for _ in range(max(1, undo_count)):
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
        if not self.ai_refiner.is_available and self.db.is_enabled:
            key = self.db.fetch_app_setting("anthropic_api_key")
            if key:
                self.ai_refiner.update_api_key(key)
                self.popup.set_ai_refiner(self.ai_refiner)
                print("[App] Loaded Anthropic API key from Supabase.")

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


def _start_local_server(app_window, version: str) -> None:
    """
    Tiny localhost-only HTTP server so the FTC web app can detect whether
    FTC Whisper is running and surface its window without needing a custom
    URL protocol registration.  Binds to 127.0.0.1 only — not reachable
    from the network.

    Endpoints:
      GET /ping  → {"ok": true, "version": "x.y.z"}
      GET /show  → brings the window to the front, returns {"ok": true}
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
                from updater import get_latest_release, is_newer, current_exe_path
                info = get_latest_release()
                if info and is_newer(info["version"], version) and current_exe_path():
                    ver, url = info["version"], info["download_url"]
                    if app_window._root:
                        app_window._root.after(
                            0, lambda v=ver, u=url: app_window.show_update_banner(v, u)
                        )
                    app_window.show()
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true,"action":"updating"}')
                else:
                    # Already up to date or running from source — tell web app to download directly
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":false,"action":"download"}')
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
        sys.exit(0)

    # We are the first instance — hold the mutex for the process lifetime.
    _SINGLETON_MUTEX = mutex


def _ensure_startup_task() -> None:
    """
    Register FTC Whisper as a Task Scheduler logon task.

    Task Scheduler is preferred over the HKCU Run registry key because:
      - Tasks run earlier in the login sequence (before most tray apps)
      - Not blocked by Windows Installer Detection (no UAC elevation prompt)
      - We can set above-normal priority so Whisper loads before other startup apps
      - More reliable — the scheduler retries on failure

    Falls back to the registry key if schtasks is unavailable.
    """
    import subprocess

    TASK_NAME = "FTC Whisper"

    if getattr(sys, "frozen", False):
        exe_cmd = f'"{sys.executable}"'
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        script  = os.path.abspath(__file__)
        exe_cmd = f'"{pythonw}" "{script}"'

    # Check if the task already exists and points to the right exe
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and sys.executable.lower() in result.stdout.lower():
            return  # already registered correctly
    except Exception:
        pass

    # Create / overwrite the task — ONLOGON, current user only, no elevation needed
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{os.environ.get("USERNAME", "")}</UserId>
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
    <Priority>6</Priority>
  </Settings>
  <Actions>
    <Exec>
      <Command>{sys.executable if getattr(sys, "frozen", False) else os.path.join(os.path.dirname(sys.executable), "pythonw.exe")}</Command>
      {"" if getattr(sys, "frozen", False) else f"<Arguments>{os.path.abspath(__file__)}</Arguments>"}
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
            capture_output=True, text=True
        )
        os.unlink(xml_path)

        if result.returncode == 0:
            print(f"[App] Startup task registered (Task Scheduler): {exe_cmd}")
        else:
            raise RuntimeError(result.stderr.strip())

    except Exception as e:
        print(f"[App] Task Scheduler registration failed ({e}), falling back to registry")
        _ensure_startup_registry_fallback()


def _register_url_protocol() -> None:
    """
    Register ftcwhisper:// as a Windows URL protocol so browsers can launch the app.
    Runs each startup so the path stays current after an update or move.
    """
    import winreg

    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}" "%1"'
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        script  = os.path.abspath(__file__)
        cmd     = f'"{pythonw}" "{script}" "%1"'

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

    if getattr(sys, "frozen", False):
        exe = sys.executable
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        script  = os.path.abspath(__file__)
        exe = f'"{pythonw}" "{script}"'

    cmd = f'"{exe}"' if not exe.startswith('"') else exe

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


def main() -> None:
    if sys.platform == "win32":
        _ensure_single_instance()
        # Run in background — schtasks can be slow on first launch and there's
        # no reason to block the UI thread waiting for a Task Scheduler write.
        threading.Thread(
            target=_ensure_startup_task, daemon=True, name="startup-task"
        ).start()
        _register_url_protocol()
        # Boost process to above-normal priority so Whisper model loads
        # faster and hotkey response isn't delayed by competing startup apps.
        try:
            ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ABOVE_NORMAL_PRIORITY_CLASS,
            )
        except Exception:
            pass
        try:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print(
                    "[App] Note: running without admin — some hotkeys may not work "
                    "in elevated windows."
                )
        except Exception:
            pass

    try:
        import pyi_splash  # type: ignore
        pyi_splash.close()
    except ImportError:
        pass

    config = Config.load()
    auth = AuthManager(config.supabase_url, config.supabase_key)

    auth_enabled = bool(config.supabase_url and config.supabase_key)

    if not auth_enabled:
        auth.sign_in_offline()  # No Supabase configured — skip login
    else:
        auth.try_restore_session()  # Restore saved session so user stays logged in

    app = WhisperFlowApp(auth, config)
    app.run()


if __name__ == "__main__":
    main()
