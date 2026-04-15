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

# Fix Windows console encoding
# Removed stdout wrapping for clear logging

import ctypes

from config import Config
from recorder import Recorder
from transcriber import Transcriber
from injector import Injector
from hotkey_manager import HotkeyManager, AppState
from feedback import Feedback
from tray import TrayApp
from popup import FloatingPopup
from ai_refiner import AIRefiner
from supabase_client import SupabaseLogger
from auth import AuthManager
from app_window import AppWindow


class WhisperFlowApp:
    """
    Main application controller.
    Created once authentication is confirmed; wires all components together.
    """

    def __init__(self, auth: AuthManager, config: Config):
        print("=" * 50)
        print("  FTC Whisper — Voice-to-Text Desktop App")
        print("=" * 50)

        self._auth   = auth
        self.config  = config
        self._started = False

        # ── Core pipeline ──────────────────────────────────────────────
        self.transcriber = Transcriber(
            model_size=config.whisper_model,
            language=config.language,
        )
        self.recorder  = Recorder(sample_rate=config.sample_rate)
        self.injector  = Injector(method=config.inject_method)

        # ── AI + logging ───────────────────────────────────────────────
        self.ai_refiner = AIRefiner(api_key=config.anthropic_api_key)
        self.db = SupabaseLogger(url=config.supabase_url, key=config.supabase_key)

        # ── UI components ──────────────────────────────────────────────
        self.app_window = AppWindow(
            auth=auth,
            on_authenticated=self._on_authenticated,
            on_sign_out=self._sign_out,
            on_open_config=self._open_config,
            on_quit=self._shutdown,
            on_hotkey_change=self._on_hotkey_change,
            db=self.db,
            hotkey=config.hotkey,
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

        self.hotkey_manager = HotkeyManager(
            hotkey=config.hotkey,
            mode=config.mode,
            on_start_recording=self._on_start_recording,
            on_stop_recording=self._on_stop_recording,
            on_cancel_recording=self._on_cancel_recording,
            on_state_change=self._on_state_change,
        )

        # ── Pre-load Whisper model immediately in background ───────────
        # Auth and model loading now run in parallel — model will be
        # ready (or close to it) by the time the user first presses the hotkey.
        threading.Thread(target=self.transcriber.load_model,
                         daemon=True, name="model-preload").start()

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

        self.app_window.run()   # blocks on main thread

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

        if self.ai_refiner.is_available:
            print("[App] AI refinement enabled.")
        else:
            print("[App] AI refinement disabled — set anthropic_api_key in config.")

        if self.db.is_enabled:
            print(f"[App] Supabase logging enabled.")
        else:
            print("[App] Supabase logging disabled — set supabase_url/key in config.")

        # Pre-load model
        self.transcriber.load_model()
        print("[App] Ready! Hold the hotkey and start speaking.")

        # Register global hotkey
        self.hotkey_manager.register()
        print(f"[App] Hotkey: '{self.config.hotkey}' | Mode: {self.config.mode}")

        # Start tray in daemon thread (safe on Windows)
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()

    def _on_hotkey_change(self, new_hotkey: str) -> None:
        """Called when the user saves a new hotkey in the dashboard."""
        print(f"[App] Updating hotkey to: {new_hotkey}")
        self.config.hotkey = new_hotkey
        self.config.save()
        self.hotkey_manager.update_hotkey(new_hotkey)

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
            # Streaming injection state — reset each recording session
            self._injected_text = ""          # text already typed into cursor during streaming
            self._streaming_lock = threading.Lock()
            self.recorder.start()
            self.feedback.recording_started()
            threading.Thread(target=self._streaming_loop, daemon=True).start()
        except Exception as e:
            print(f"[App] Failed to start recording: {e}")
            self.feedback.error_occurred(str(e))
            self.hotkey_manager.set_idle()

    def _streaming_loop(self) -> None:
        """Inject partial transcription into the cursor every ~1 s while recording.
        Uses non-blocking transcribe so it never delays the final pass.
        No focus call needed — RegisterHotKey leaves the target window focused."""
        INTERVAL = 1.0
        MIN_SAMPLES = int(self.config.sample_rate * 1.0)
        while self.recorder.is_recording:
            time.sleep(INTERVAL)
            if not self.recorder.is_recording:
                break
            # Short rolling window — faster Whisper inference, less drift
            audio = self.recorder.get_current_audio(max_seconds=4.0)
            if audio is None or len(audio) < MIN_SAMPLES:
                continue
            try:
                partial = self.transcriber.transcribe(
                    audio, self.config.sample_rate, blocking=False)
                if not partial or not partial.strip():
                    continue
                partial = partial.strip() + " "   # trailing space before next word

                with self._streaming_lock:
                    if not self.recorder.is_recording:
                        break   # final transcription is now running — let it handle the rest
                    already = self._injected_text
                    if partial.startswith(already) and len(partial) > len(already):
                        new_part = partial[len(already):]
                        # Target window stays focused during hold-to-talk; no focus
                        # switch needed here — avoids the 80ms settle delay per word
                        ok = self.injector.inject_immediate(new_part)
                        if ok:
                            self._injected_text = partial
                            print(f"[App] Streamed +{len(new_part)} chars")
                    # If Whisper changed earlier words we skip the stream update
                    # and let the final transcription do a clean overwrite.

            except Exception as e:
                print(f"[App] Streaming error (non-fatal): {e}")

    def _on_stop_recording(self) -> None:
        try:
            audio = self.recorder.stop()
            self.feedback.recording_stopped()

            if audio is None or len(audio) < self.config.sample_rate * 0.3:
                print("[App] Recording too short, ignoring.")
                self.hotkey_manager.set_idle()
                self.feedback.error_occurred("Recording too short")
                return

            # Cap to last 30 s — streaming handles earlier text; this keeps
            # the final Whisper pass fast even for minutes-long recordings.
            MAX_FINAL_SECS = 30.0
            max_samples = int(self.config.sample_rate * MAX_FINAL_SECS)
            final_audio = audio[-max_samples:] if len(audio) > max_samples else audio
            print(f"[App] Transcribing {len(final_audio)} samples "
                  f"({len(final_audio)/self.config.sample_rate:.1f}s)...")
            text = self.transcriber.transcribe(final_audio, self.config.sample_rate)
            print(f"[App] Transcription: '{text}'")

            if not text.strip():
                print("[App] Empty transcription result.")
                self.hotkey_manager.set_idle()
                self.feedback.error_occurred("No speech detected")
                return

            hwnd = self._recording_hwnd
            final = text.strip()

            # Read and clear the streaming state (streaming loop is done by now)
            with self._streaming_lock:
                injected = self._injected_text
                self._injected_text = ""

            self._focus_window(hwnd)

            if not injected:
                # Nothing was streamed — inject the whole transcription
                print(f"[App] Injecting full text ({len(final)} chars)")
                result = self.injector.inject(final)
            elif final.startswith(injected.rstrip()):
                # Whisper's final agrees with what was streamed; inject just the tail
                suffix = final[len(injected.rstrip()):]
                if suffix:
                    print(f"[App] Injecting suffix ({len(suffix)} chars)")
                    result = self.injector.inject(suffix)
                else:
                    print("[App] Streaming already complete — nothing to append")
                    result = True
            else:
                # Whisper changed earlier words — erase streamed text and re-inject
                print(f"[App] Correcting stream: erasing {len(injected)} chars, re-injecting")
                import keyboard as kb
                for _ in range(len(injected)):
                    kb.send("backspace")
                time.sleep(0.05)
                result = self.injector.inject(final)

            print(f"[App] Inject result: {result}")
            self.feedback.transcription_complete(text)
            self.hotkey_manager.set_idle()
            self.db.log_transcription(text)

            self.popup.show_cursor_icon(
                text,
                on_insert=lambda t=text, h=hwnd: self._insert_text(t, h),
                on_replace=lambda new_text, t=text: self._replace_text(new_text, hwnd, t),
                hwnd=hwnd,
                cursor_x=getattr(self, "_rec_cursor_x", 0),
                cursor_y=getattr(self, "_rec_cursor_y", 0),
            )

        except Exception as e:
            print(f"[App] Pipeline error: {e}")
            import traceback; traceback.print_exc()
            self.feedback.error_occurred(str(e))
            self.hotkey_manager.set_idle()

    def _on_cancel_recording(self) -> None:
        try:
            if self.recorder.is_recording:
                self.recorder.stop()
            self.feedback.recording_stopped()
            print("[App] Recording cancelled (short tap).")
        except Exception as e:
            print(f"[App] Error cancelling recording: {e}")
        finally:
            self.hotkey_manager.set_idle()

    def _on_state_change(self, state: AppState) -> None:
        self.app_window.update_status(state.value)  # "idle" / "recording" / "processing"
        if state == AppState.RECORDING:
            self.popup.show_status("Recording", hwnd=self._recording_hwnd, recording=True)
            # Feed mic levels to the popup waveform while recording
            threading.Thread(target=self._mic_level_loop, daemon=True).start()
        elif state == AppState.PROCESSING:
            self.popup.show_status("Transcribing…", hwnd=self._recording_hwnd, recording=False)
        elif state == AppState.IDLE:
            if not self.popup.is_user_facing:
                self.popup.hide()

    def _mic_level_loop(self) -> None:
        """Sample the recorder's audio buffer for RMS level and push to popup."""
        import numpy as np
        while self.recorder.is_recording:
            try:
                audio = self.recorder.get_current_audio(max_seconds=0.1)
                if audio is not None and len(audio) > 0:
                    rms = float(np.sqrt(np.mean(audio ** 2)))
                    # Scale: typical speech RMS ~0.02–0.15 → normalise to 0–1
                    level = min(1.0, rms / 0.08)
                    self.popup.update_mic_level(level)
            except Exception:
                pass
            time.sleep(0.05)
        self.popup.update_mic_level(0.0)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _focus_window(self, hwnd: int, short: bool = False) -> bool:
        """Bring hwnd to the foreground so injected keystrokes land there.
        Retries once if focus doesn't land on the first attempt.
        Returns True if the window is confirmed foreground after the call."""
        if not hwnd:
            return False
        for attempt in range(2):
            try:
                u32      = ctypes.windll.user32
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
                fg_tid  = u32.GetWindowThreadProcessId(fg_hwnd, None)
                our_tid = kernel32.GetCurrentThreadId()

                attached = bool(fg_tid and fg_tid != our_tid)
                if attached:
                    u32.AttachThreadInput(our_tid, fg_tid, True)

                u32.SetForegroundWindow(hwnd)
                u32.BringWindowToTop(hwnd)
                u32.SetFocus(hwnd)

                if attached:
                    u32.AttachThreadInput(our_tid, fg_tid, False)

                time.sleep(0.08 if short else 0.20)

                actual = u32.GetForegroundWindow()
                if actual == hwnd:
                    return True
                print(f"[App] Focus attempt {attempt+1}: expected {hwnd:#x}, got {actual:#x}")
            except Exception as e:
                print(f"[App] Focus error (attempt {attempt+1}): {e}")

        print(f"[App] Focus failed after retries — injecting anyway")
        return False

    def _insert_text(self, text: str, hwnd: int) -> None:
        """Manual insert fallback — focuses the target window and injects the full text."""
        self._focus_window(hwnd)
        self.injector.inject(text)
        print(f"[App] Manual insert: {len(text)} chars")

    def _replace_text(self, new_text: str, hwnd: int, original_text: str = "") -> None:
        import keyboard as kb
        import pyperclip
        self._focus_window(hwnd)
        kb.send("ctrl+z")
        time.sleep(0.05)
        pyperclip.copy(new_text)
        time.sleep(0.05)
        kb.send("ctrl+v")
        print(f"[App] Replaced with refined text: '{new_text}'")
        if original_text:
            self.db.log_refinement(original_text, new_text, "replace")

    def _open_config(self) -> None:
        config_path = self.config._config_path
        if os.path.exists(config_path):
            os.startfile(config_path)
            print(f"[App] Opened config: {config_path}")

    def _sign_out(self) -> None:
        print("[App] Signing out...")
        self._auth.sign_out()
        self.hotkey_manager.unregister()
        if self.recorder.is_recording:
            self.recorder.stop()
        self.tray.stop()
        # Restart fresh so the login window appears
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _shutdown(self) -> None:
        print("[App] Shutting down...")
        self.hotkey_manager.unregister()
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

def _ensure_single_instance() -> None:
    """Kill any previous instance using a PID file, then record our own PID."""
    pid_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ftc_pid")
    kernel32 = ctypes.windll.kernel32

    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            if old_pid and old_pid != os.getpid():
                PROCESS_TERMINATE = 0x0001
                handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, old_pid)
                if handle:
                    kernel32.TerminateProcess(handle, 0)
                    kernel32.CloseHandle(handle)
                    print(f"[App] Killed previous instance (PID {old_pid})")
                    time.sleep(0.6)  # let Win32 release the RegisterHotKey
        except Exception as e:
            print(f"[App] Single-instance cleanup: {e}")

    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _ensure_startup_registry() -> None:
    """
    Add FTC Whisper to Windows startup (HKCU Run) so it launches when the user logs in.
    Safe to call on every launch — only writes if the value is missing or stale.
    Works for both the PyInstaller exe and the source (pythonw.exe app.py) installs.
    """
    import winreg
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE   = "FTC Whisper"

    # Determine our own executable path
    if getattr(sys, "frozen", False):
        # PyInstaller bundle — use the exe itself
        exe = sys.executable
    else:
        # Source install — pythonw.exe + this script
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        script  = os.path.abspath(__file__)
        exe     = f'"{pythonw}" "{script}"'

    cmd = f'"{exe}"' if not exe.startswith('"') else exe

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            try:
                current, _ = winreg.QueryValueEx(k, VALUE)
                if current == cmd:
                    return   # already set correctly
            except FileNotFoundError:
                pass   # value doesn't exist yet
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, cmd)
            print(f"[App] Startup registry key set: {cmd}")
    except Exception as e:
        print(f"[App] Could not set startup registry: {e}")


def main() -> None:
    if sys.platform == "win32":
        _ensure_single_instance()
        _ensure_startup_registry()
        try:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print("[App] Note: running without admin — some hotkeys may not work "
                      "in elevated windows.")
        except Exception:
            pass

    config = Config.load()
    auth   = AuthManager(config.supabase_url, config.supabase_key)

    if not auth.try_restore_session():
        # Try silent background sign-in if credentials are in config
        if config.supabase_email and config.supabase_password:
            ok, msg = auth.sign_in(config.supabase_email, config.supabase_password)
            if ok:
                print(f"[App] Silent sign-in OK ({config.supabase_email})")
            else:
                print(f"[App] Silent sign-in failed: {msg} — running offline")
                auth.sign_in_offline()
        else:
            auth.sign_in_offline()

    app = WhisperFlowApp(auth, config)
    app.run()


if __name__ == "__main__":
    main()
