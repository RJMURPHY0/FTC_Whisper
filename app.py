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
            self.recorder.start()
            self.feedback.recording_started()
            threading.Thread(target=self._streaming_loop, daemon=True).start()
        except Exception as e:
            print(f"[App] Failed to start recording: {e}")
            self.feedback.error_occurred(str(e))
            self.hotkey_manager.set_idle()

    def _streaming_loop(self) -> None:
        """Show a live preview while recording. Uses non-blocking transcribe so it
        never delays the final transcription when the hotkey is released."""
        INTERVAL = 2.0
        MIN_SAMPLES = int(self.config.sample_rate * 1.5)
        while self.recorder.is_recording:
            time.sleep(INTERVAL)
            if not self.recorder.is_recording:
                break
            audio = self.recorder.get_current_audio()
            if audio is None or len(audio) < MIN_SAMPLES:
                continue
            try:
                # blocking=False — skips immediately if final transcription has the lock
                partial = self.transcriber.transcribe(
                    audio, self.config.sample_rate, blocking=False)
                if partial.strip():
                    preview = partial.strip()[:80]
                    self.popup.show_status(f"💬 {preview}", hwnd=self._recording_hwnd)
            except Exception:
                pass

    def _on_stop_recording(self) -> None:
        try:
            audio = self.recorder.stop()
            self.feedback.recording_stopped()

            if audio is None or len(audio) < self.config.sample_rate * 0.3:
                print("[App] Recording too short, ignoring.")
                self.hotkey_manager.set_idle()
                self.feedback.error_occurred("Recording too short")
                return

            print(f"[App] Transcribing {len(audio)} samples...")
            text = self.transcriber.transcribe(audio, self.config.sample_rate)
            print(f"[App] Transcription: '{text}'")

            if not text.strip():
                print("[App] Empty transcription result.")
                self.hotkey_manager.set_idle()
                self.feedback.error_occurred("No speech detected")
                return

            hwnd = self._recording_hwnd
            print(f"[App] Focusing hwnd={hwnd:#x} then injecting...")
            self._focus_window(hwnd)
            result = self.injector.inject(text)
            print(f"[App] Inject result: {result}")
            self.feedback.transcription_complete(text)
            self.hotkey_manager.set_idle()
            self.db.log_transcription(text)

            self.popup.show_cursor_icon(
                text,
                on_replace=lambda new_text, t=text: self._replace_text(new_text, hwnd, t),
                hwnd=hwnd,
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
            self.popup.show_status("🎙️ Recording...", hwnd=self._recording_hwnd)
        elif state == AppState.PROCESSING:
            self.popup.show_status("⚙️ Transcribing...", hwnd=self._recording_hwnd)
        elif state == AppState.IDLE:
            if not self.popup.is_user_facing:
                self.popup.hide()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _focus_window(self, hwnd: int) -> None:
        if not hwnd:
            return
        try:
            u32      = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # Only restore if the window is actually minimised — avoids
            # the un-maximise / flicker that SW_RESTORE causes on normal windows.
            SW_SHOWNOACTIVATE = 4
            WS_MINIMIZE = 0x20000000
            style = u32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
            if style & WS_MINIMIZE:
                u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

            # AttachThreadInput bypasses Windows focus-steal restrictions.
            fg_hwnd = u32.GetForegroundWindow()
            fg_tid  = u32.GetWindowThreadProcessId(fg_hwnd, None)
            our_tid = kernel32.GetCurrentThreadId()

            attached = bool(fg_tid and fg_tid != our_tid)
            if attached:
                u32.AttachThreadInput(our_tid, fg_tid, True)

            u32.SetForegroundWindow(hwnd)
            u32.BringWindowToTop(hwnd)

            if attached:
                u32.AttachThreadInput(our_tid, fg_tid, False)

            time.sleep(0.15)
        except Exception as e:
            print(f"[App] Focus failed: {e}")

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


def main() -> None:
    if sys.platform == "win32":
        _ensure_single_instance()
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
