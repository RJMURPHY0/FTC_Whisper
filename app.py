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
from login_window import LoginWindow


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
        self._mic_loop_running = threading.Event()
        self._mic_level_smooth = 0.0

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
        threading.Thread(
            target=self.transcriber.load_model, daemon=True, name="model-preload"
        ).start()

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
            self.recorder.start()
            self.feedback.recording_started()
        except Exception as e:
            print(f"[App] Failed to start recording: {e}")
            self.feedback.error_occurred(str(e))
            self.hotkey_manager.set_idle()

    def _on_stop_recording(self) -> None:
        transcribed_text: str = ""
        hwnd = self._recording_hwnd

        try:
            audio = self.recorder.stop()
            self.feedback.recording_stopped()
            capture_rate = max(1, self.recorder.active_sample_rate)

            if audio is None or len(audio) < capture_rate * 0.3:
                print("[App] Recording too short, ignoring.")
                self.hotkey_manager.set_idle()
                self.feedback.error_occurred("Recording too short")
                return

            # Cap to 60 s — enough for any reasonable dictation
            MAX_SECS = 60.0
            max_samples = int(capture_rate * MAX_SECS)
            final_audio = audio[-max_samples:] if len(audio) > max_samples else audio
            print(
                f"[App] Transcribing {len(final_audio) / capture_rate:.1f}s of audio at {capture_rate} Hz..."
            )
            text = self.transcriber.transcribe(final_audio, capture_rate)
            print(f"[App] Transcription: '{text}'")

            if not text.strip():
                print("[App] Empty transcription result.")
                self.hotkey_manager.set_idle()
                self.feedback.error_occurred("No speech detected")
                return

            transcribed_text = text.strip()

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
            print(f"[App] Injecting {len(transcribed_text)} chars")
            result = self.injector.inject(transcribed_text)
            print(f"[App] Inject result: {result}")
        except Exception as e:
            print(f"[App] Injection error (popup will still appear): {e}")

        self.feedback.transcription_complete(transcribed_text)
        self.hotkey_manager.set_idle()
        self.db.log_transcription(transcribed_text)

        # ── Popup always shown — works as manual-insert fallback if inject failed ─
        self.popup.show_cursor_icon(
            transcribed_text,
            on_insert=lambda t=transcribed_text, h=hwnd: self._insert_text(t, h),
            on_replace=lambda new_text, t=transcribed_text: self._replace_text(
                new_text, hwnd, t
            ),
            inserted=result,
            hwnd=hwnd,
            cursor_x=0,
            cursor_y=0,
        )

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
        self.app_window.update_status(
            state.value
        )  # "idle" / "recording" / "processing"
        cx = getattr(self, "_rec_cursor_x", 0)
        cy = getattr(self, "_rec_cursor_y", 0)
        if state == AppState.RECORDING:
            self.popup.show_status(
                "Recording",
                hwnd=self._recording_hwnd,
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
                cursor_x=cx,
                cursor_y=cy,
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

                time.sleep(0.08 if short else 0.20)

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
        """Manual insert fallback — focuses the target window and injects the full text."""
        self._focus_window(hwnd)
        self.injector.inject(text)
        print(f"[App] Manual insert: {len(text)} chars")

    def _replace_text(self, new_text: str, hwnd: int, original_text: str = "") -> None:
        import keyboard as kb

        self._focus_window(hwnd)
        kb.send("ctrl+z")
        time.sleep(0.05)
        # Use the injector's native clipboard method (no pyperclip dependency)
        self.injector.inject(new_text)
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
        self._restart_for_reauth = True
        self.hotkey_manager.unregister()
        if self.recorder.is_recording:
            self.recorder.stop()
        self.db.set_user(None)
        self.tray.set_user_email("")
        self.tray.stop()
        if self.app_window._root:
            self.app_window._root.after(0, self.app_window._root.destroy)

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
    VALUE = "FTC Whisper"

    # Determine our own executable path
    if getattr(sys, "frozen", False):
        # PyInstaller bundle — use the exe itself
        exe = sys.executable
    else:
        # Source install — pythonw.exe + this script
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        script = os.path.abspath(__file__)
        exe = f'"{pythonw}" "{script}"'

    cmd = f'"{exe}"' if not exe.startswith('"') else exe

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as k:
            try:
                current, _ = winreg.QueryValueEx(k, VALUE)
                if current == cmd:
                    return  # already set correctly
            except FileNotFoundError:
                pass  # value doesn't exist yet
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
                print(
                    "[App] Note: running without admin — some hotkeys may not work "
                    "in elevated windows."
                )
        except Exception:
            pass

    config = Config.load()
    auth = AuthManager(config.supabase_url, config.supabase_key)

    auth_enabled = bool(config.supabase_url and config.supabase_key)

    while True:
        if auth_enabled:
            if not auth.try_restore_session():
                # Optional silent sign-in (owner/admin convenience)
                if config.supabase_email and config.supabase_password:
                    ok, msg = auth.sign_in(
                        config.supabase_email, config.supabase_password
                    )
                    if ok:
                        print(f"[App] Silent sign-in OK ({config.supabase_email})")
                    else:
                        print(f"[App] Silent sign-in failed: {msg}")

                # If still not authenticated, require explicit login/signup
                if not auth.is_authenticated:
                    print("[App] Showing login window...")
                    LoginWindow(auth, on_success=lambda _auth: None).run()

                if not auth.is_authenticated:
                    print("[App] Authentication was cancelled. Exiting.")
                    return
        else:
            # Supabase auth disabled in config; run in local-only mode.
            auth.sign_in_offline()

        app = WhisperFlowApp(auth, config)
        app.run()

        if not app._restart_for_reauth:
            break

        print("[App] Signed out. Returning to login screen...")


if __name__ == "__main__":
    main()
