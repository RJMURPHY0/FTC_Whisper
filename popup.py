"""
Floating UI popup — light grey pill with animated waveform during recording.

Three modes:
  status     — pill shown during recording / transcribing
  icon       — small FTC badge near cursor after injection
  refinement — full AI refinement panel
"""

import ctypes
import ctypes.wintypes
import math
import threading
import time
import tkinter as tk
from typing import Callable, Optional


# ctypes structs
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


# ── Popup palette (light grey floating pill) ──────────────────────────────────
# The main app window stays dark; the small floating popups use a softer look.
CP = {
    "bg": "#2b2b2b",  # dark-ish grey pill background
    "bg_light": "#3a3a3a",  # slightly lighter for icon/refinement
    "text": "#ffffff",
    "subtext": "#aaaaaa",
    "accent": "#f39200",  # FTC orange
    "accent_hover": "#e08200",
    "divider": "#4a4a4a",
    "btn_bg": "#444444",
    "btn_hover": "#555555",
    "bar_idle": "#666666",  # waveform bar — not speaking
    "bar_active": "#f39200",  # waveform bar — speaking (FTC orange)
    "error": "#ff5555",
    "success": "#4ade80",
}

# Keep the dark-theme names aliased so refinement frame code is consistent
C = CP

# Waveform config
NUM_BARS = 16
BAR_W    = 4
BAR_GAP  = 2
BAR_MAX_H = 30
BAR_MIN_H = 5
CANVAS_W = NUM_BARS * (BAR_W + BAR_GAP) - BAR_GAP
CANVAS_H = BAR_MAX_H + 6

# Live-caption bar (shown beneath the waveform when captions are on).
# The bar is a FIXED-WIDTH paragraph block (CAPTION_MAX_CHARS wide) from the very
# first word: text wraps and grows vertically up to CAPTION_MAX_LINES, then
# scrolls. Fixed width means the bar never widens or slides sideways as you speak
# — it reads as a stable left-aligned paragraph the whole time.
# The full transcript-so-far stays in the widget — the view tails the newest
# words, and the user can wheel-scroll back through everything said.
CAPTION_MIN_CHARS = 16   # legacy floor (bar now renders at CAPTION_MAX_CHARS)
CAPTION_MAX_CHARS = 60
CAPTION_MAX_LINES = 5
CAPTION_SCROLL_HOLDOFF = 4.0  # secs after a manual scroll before auto-tail resumes

# The Ask-AI instruction box grows downward as the typed text wraps past the
# right edge, up to this many visible lines, then scrolls internally.
ASK_MAX_LINES = 6
ASK_PLACEHOLDER = "Ask AI — e.g. 'change language to French' or 'make this shorter'"

# How long the post-transcription icon badge stays before auto-dismissing, so it
# never lingers on screen indefinitely.
ICON_AUTO_DISMISS_SECS = 30.0


def _apply_popup_corners(hwnd: int) -> None:
    """Apply Win11 DWM rounded corners — no GDI clipping, no black artifacts."""
    try:
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref),
        )
    except Exception:
        pass


class FloatingPopup:
    def __init__(self):
        self.root: Optional[tk.Toplevel] = None

        self._mode: Optional[str] = None
        self._on_insert: Optional[Callable] = None
        self._on_replace: Optional[Callable] = None
        self._on_insert_result: Optional[Callable] = None
        self._session_token: int = 0
        self._target_hwnd: int = 0
        self._cursor_x: int = 0
        self._cursor_y: int = 0
        self._ai_refiner = None
        self._voice_prompt_fn = None
        self._voice_capture_start = None
        self._voice_capture_read = None
        self._voice_capture_stop = None
        self._mic_recording: bool = False
        self._mic_pulse_on: bool = True
        self._original_text: str = ""
        self._current_result: Optional[str] = None
        self._inserted_ok: bool = True
        self._ai_busy: bool = False
        self._popup_hwnd: int = 0
        self._upgrading: bool = False
        self._upgrade_result: Optional[str] = None

        # Waveform state
        self._mic_level: float = 0.0  # 0.0–1.0, updated by audio thread
        self._waveform_running: bool = False
        self._bar_phases = [i * (2 * math.pi / NUM_BARS) for i in range(NUM_BARS)]

        # Cursor position at last show_status call — used for monitor selection
        self._status_cx: int = 0
        self._status_cy: int = 0

        # Watchdog: guarantees the popup is never left visible-but-empty/stuck.
        # _last_activity is refreshed by the recording animation / caption ticks;
        # _status_entered marks when the current status pill appeared.
        self._last_activity: float = 0.0
        self._status_entered: float = 0.0
        self._icon_entered: float = 0.0
        self._last_shown: float = 0.0  # when any mode last became visible (grace window)
        self._watchdog_started: bool = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_ai_refiner(self, refiner) -> None:
        self._ai_refiner = refiner

    def set_voice_prompt_callback(self, fn) -> None:
        self._voice_prompt_fn = fn

    def set_voice_capture_fns(self, start, read, stop) -> None:
        """Recorder-backed audio capture for the voice-prompt mic.
        start() -> bool; read() -> (audio|None, rate); stop() -> (audio|None, rate).
        Replaces the popup opening its own sounddevice stream, which recorded
        the Windows default mic (not the configured device) and could be killed
        mid-read by the recorder watchdog's PortAudio re-init."""
        self._voice_capture_start = start
        self._voice_capture_read = read
        self._voice_capture_stop = stop

    def show_status(
        self,
        text: str,
        hwnd: int = 0,
        recording: bool = False,
        cursor_x: int = 0,
        cursor_y: int = 0,
    ) -> None:
        self._target_hwnd = hwnd
        # Store cursor position so popup appears on the correct monitor
        if cursor_x or cursor_y:
            self._status_cx, self._status_cy = cursor_x, cursor_y
        else:
            self._status_cx, self._status_cy = self._get_cursor_pos()
        if self.root:
            # Use lambda — avoids tkinter after() quirks with boolean positional args
            self.root.after(0, lambda: self._enter_status_mode(text, recording))

    def show_cursor_icon(
        self,
        text: str,
        on_insert: Callable = None,
        on_replace: Callable[[str], None] = None,
        inserted: bool = True,
        hwnd: int = 0,
        cursor_x: int = 0,
        cursor_y: int = 0,
        upgrading: bool = False,
        session: int = 0,
        on_insert_result: Callable[[str], None] = None,
    ) -> None:
        self._on_insert = on_insert
        self._on_replace = on_replace
        self._on_insert_result = on_insert_result
        self._inserted_ok = inserted
        self._target_hwnd = hwnd
        self._original_text = text
        self._current_result = None
        self._upgrading = upgrading
        self._upgrade_result = None
        # Session token: a slow upgrade thread from a PREVIOUS dictation must
        # not attach its text to this popup (Replace would then undo the new
        # dictation and inject the old one).
        self._session_token = session
        # If explicit cursor coords are provided (e.g. refine-selection flow where
        # show_status was never called), also update _status_cx/cy so _enter_icon_mode
        # and _expand_to_panel position the popup on the correct monitor.
        if cursor_x or cursor_y:
            self._cursor_x, self._cursor_y = cursor_x, cursor_y
            self._status_cx, self._status_cy = cursor_x, cursor_y
        else:
            self._cursor_x, self._cursor_y = self._get_cursor_pos()
        if self.root:
            self.root.after(0, self._enter_icon_mode)

    def update_mic_level(self, level: float) -> None:
        """Called from the audio thread with RMS level (0.0–1.0)."""
        self._mic_level = max(0.0, min(1.0, level))
        self._last_activity = time.time()

    def set_captions_enabled(self, enabled: bool) -> None:
        """Toggle live-caption display mode (replaces the waveform when on).
        Call before show_status() so the next recording renders the right bar."""
        self._captions_enabled = bool(enabled)

    def update_caption(self, text: str) -> None:
        """Called from the caption loop (background thread) with live partial text.
        The full transcript-so-far is kept in the bar: the view tails the newest
        words, and the user can scroll back through everything said. (Text size
        is trivial even for long dictations — no need to truncate.)"""
        if not text:
            return
        self._last_activity = time.time()
        if self.root:
            self.root.after(0, lambda: self._update_caption_widget(text))

    def hide(self) -> None:
        if self.root:
            self.root.after(0, self._do_hide)

    @property
    def is_user_facing(self) -> bool:
        return self._mode in ("icon", "refinement")

    # ── Tkinter setup ──────────────────────────────────────────────────────────

    def initialize(self, main_root: tk.Tk) -> None:
        """Create the popup Toplevel on the main thread. Call once after main Tk is running."""
        if self.root is not None:
            return
        self.root = tk.Toplevel(main_root)
        # Hide immediately and park far off-screen BEFORE anything else, so the
        # freshly-created dark Toplevel can never flash as a little black box at
        # the top-left (0,0) for a frame before it's positioned/withdrawn.
        self.root.withdraw()
        self.root.geometry("+-4000+-4000")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)
        self.root.attributes("-toolwindow", True)
        self.root.configure(bg=CP["bg"])
        self.root.withdraw()

        self.root.update_idletasks()
        self._popup_hwnd = self.root.winfo_id()

        # WS_EX_NOACTIVATE: popup can never steal focus from the foreground app.
        # This is the critical fix for ChatGPT / browser inputs — without it the
        # recording pill steals focus from Chrome, which clears the ProseMirror /
        # contenteditable focus state so Ctrl+V lands nowhere.
        # Mouse clicks on the popup's own buttons still work normally.
        try:
            GWL_EXSTYLE      = -20
            WS_EX_NOACTIVATE = 0x08000000
            u32 = ctypes.windll.user32
            style = u32.GetWindowLongW(self._popup_hwnd, GWL_EXSTYLE)
            u32.SetWindowLongW(self._popup_hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
        except Exception:
            pass

        # Apply Win11 DWM rounded corners once — no GDI region, no black artifacts.
        _apply_popup_corners(self._popup_hwnd)

        self._build_status_frame()
        self._build_icon_frame()
        self._build_refinement_frame()

        self.root.bind("<Configure>", self._on_popup_configure)

        self._start_watchdog()

    def _start_watchdog(self) -> None:
        if self._watchdog_started or not self.root:
            return
        self._watchdog_started = True
        self.root.after(2000, self._watchdog)

    def _watchdog(self) -> None:
        """Safety net: never leave an empty/stuck box on screen.

        A real recording animates the waveform (or ticks the caption timer)
        every <100 ms, so _last_activity stays fresh; this only fires when the
        animation has stopped but the pill is still up (a stuck/blank box).
        Never touches the user-facing icon/refinement badges.
        """
        try:
            now = time.time()

            # Content-truth check FIRST: if the window is on screen but NO frame
            # is packed, it's a contentless box no matter what _mode claims → hide.
            # winfo_ismapped reflects this Toplevel's own shown/withdrawn state.
            try:
                viewable = bool(self.root.winfo_ismapped())
            except Exception:
                viewable = False
            # Grace period: never act on a pill that JUST appeared — its frame
            # may be packed but not yet mapped for one event-loop cycle.
            shown_recently = (now - self._last_shown) < 4.0
            if viewable and not shown_recently:
                try:
                    any_frame = any(
                        f.winfo_ismapped()
                        for f in (self._status_frame, self._icon_frame, self._refine_frame)
                    )
                except Exception:
                    any_frame = True  # fail-safe: never hide on inspection error
                if not any_frame:
                    print("[Popup] Watchdog: visible window with no packed frame -> hiding")
                    self._do_hide()
                    if self.root:
                        self.root.after(2000, self._watchdog)
                    return

            if self._mode == "status":
                stuck_recording = (
                    self._rec_start is not None and (now - self._last_activity) > 12.0
                )
                hung_transcribing = (
                    self._rec_start is None and (now - self._status_entered) > 60.0
                )
                if stuck_recording or hung_transcribing:
                    print(
                        f"[Popup] Watchdog hiding stuck status pill "
                        f"(recording={stuck_recording}, transcribing={hung_transcribing})"
                    )
                    self._do_hide()
            elif self._mode == "icon":
                # The post-transcription badge is a brief affordance — auto-dismiss
                # it so it never sits on screen indefinitely (the user's complaint).
                if self._icon_entered and (now - self._icon_entered) > ICON_AUTO_DISMISS_SECS:
                    print("[Popup] Watchdog auto-dismissing lingering icon badge")
                    self._do_hide()
            elif self._mode is None:
                # Mode says hidden but the window is somehow still on screen → re-hide.
                try:
                    if self.root.winfo_viewable():
                        self.root.withdraw()
                except Exception:
                    pass
        except Exception:
            pass
        if self.root:
            self.root.after(2000, self._watchdog)

    def _set_no_activate(self, enabled: bool) -> None:
        """Enable or disable WS_EX_NOACTIVATE on the popup window."""
        try:
            GWL_EXSTYLE      = -20
            WS_EX_NOACTIVATE = 0x08000000
            u32 = ctypes.windll.user32
            style = u32.GetWindowLongW(self._popup_hwnd, GWL_EXSTYLE)
            if enabled:
                u32.SetWindowLongW(self._popup_hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
            else:
                u32.SetWindowLongW(self._popup_hwnd, GWL_EXSTYLE, style & ~WS_EX_NOACTIVATE)
        except Exception:
            pass

    def _show_no_activate(self) -> None:
        """
        Show the popup window WITHOUT stealing keyboard focus from the active app.

        tkinter's deiconify() calls ShowWindow(SW_SHOW=5) which ACTIVATES the
        window and steals focus — even when WS_EX_NOACTIVATE is set (that flag
        only prevents activation on mouse click, not on ShowWindow).

        Strategy:
          1. Remember who currently has focus.
          2. Call deiconify() + lift() as normal (lets tkinter track internal state).
          3. Immediately give focus back to whoever had it before.

        This keeps tkinter's internal window-state consistent while ensuring the
        foreground app (e.g. Chrome with ChatGPT open) never loses keyboard focus.
        """
        u32 = ctypes.windll.user32
        prev_fg = u32.GetForegroundWindow()
        self.root.deiconify()
        self.root.lift()
        # update_idletasks() ensures tkinter finishes the ShowWindow call so our
        # process is registered as foreground before we hand focus back.
        self.root.update_idletasks()
        if prev_fg and prev_fg != self._popup_hwnd:
            try:
                # AllowSetForegroundWindow(-1) clears the foreground lock —
                # without it SetForegroundWindow can silently fail even when
                # we are the current foreground process.
                u32.AllowSetForegroundWindow(-1)
                u32.SetForegroundWindow(prev_fg)
            except Exception:
                pass

    def _on_popup_configure(self, event) -> None:
        pass  # corners handled once at initialize via DWM

    # ── Status frame (recording / transcribing pill) ───────────────────────────

    def _build_status_frame(self) -> None:
        f = tk.Frame(self.root, bg=CP["bg"], padx=14, pady=10)
        self._status_frame = f

        # Top row: timer | waveform | status label — packed left-to-right.
        # The live-caption bar (below) stacks underneath this row.
        row = tk.Frame(f, bg=CP["bg"])
        self._status_row = row
        row.pack(side="top", fill="x")

        # Timer label  e.g. "01:16.2"
        self._timer_var = tk.StringVar(value="")
        self._timer_lbl = tk.Label(
            row,
            textvariable=self._timer_var,
            fg=CP["text"],
            bg=CP["bg"],
            font=("Consolas", 13, "bold"),
        )
        # NOT packed here — packed on demand in _enter_status_mode

        # Waveform canvas — bars animated by microphone level
        self._wave_canvas = tk.Canvas(
            row,
            width=CANVAS_W,
            height=CANVAS_H,
            bg=CP["bg"],
            highlightthickness=0,
        )
        # NOT packed here — packed on demand in _enter_status_mode
        self._bar_ids: list[int] = []
        self._draw_bars_initial()

        # Status text label  "Recording…" / "Transcribing…"
        self._status_label = tk.Label(
            row,
            text="",
            fg=CP["subtext"],
            bg=CP["bg"],
            font=("Segoe UI", 11, "bold"),
        )
        # Packed at build time — always visible in the status row
        self._status_label.pack(side="left")

        # ── Live-caption bar ────────────────────────────────────────────────
        # When live captions are on, the waveform stays on top and this bar is
        # shown beneath it. It grows horizontally with the text until it hits
        # the max width, then wraps and grows vertically up to CAPTION_MAX_LINES,
        # then scrolls (tailing the latest words). Not packed here — packed on
        # demand in _enter_status_mode.
        self._captions_enabled = False
        self._caption_wrap = tk.Frame(f, bg=CP["bg"])
        self._caption_text = tk.Text(
            self._caption_wrap,
            fg=CP["text"],
            bg=CP["bg_light"],
            font=("Segoe UI", 11),
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            width=CAPTION_MAX_CHARS,   # fixed paragraph width from the first word
            height=1,
            padx=10,
            pady=6,
            state="disabled",
            cursor="arrow",
        )
        self._caption_scroll = tk.Scrollbar(
            self._caption_wrap,
            orient="vertical",
            command=self._caption_text.yview,
            bg="#3a3a3a",
            troughcolor=CP["bg"],
            activebackground="#505050",
            relief="flat",
            borderwidth=0,
            elementborderwidth=0,
            highlightthickness=0,
            bd=0,
            width=10,
        )
        self._caption_text.configure(yscrollcommand=self._caption_scroll.set)
        self._caption_text.pack(side="left", fill="both", expand=True)
        self._caption_lines = 0  # last rendered visible-line count (reposition debounce)
        # Manual scrollback: Windows routes wheel input to the hovered window
        # even though the popup never activates (WS_EX_NOACTIVATE), so a wheel
        # binding is enough. A manual scroll pauses auto-tailing briefly so the
        # view doesn't snap back to the end while the user is reading.
        self._caption_user_scroll_ts = 0.0
        self._caption_text.bind("<MouseWheel>", self._on_caption_wheel)
        self._caption_scroll.bind(
            "<ButtonPress-1>",
            lambda _e: setattr(self, "_caption_user_scroll_ts", time.time()),
            add="+",
        )

        # Recording start time (for timer)
        self._rec_start: Optional[float] = None

    def _draw_bars_initial(self) -> None:
        self._wave_canvas.delete("all")
        self._bar_ids = []
        for i in range(NUM_BARS):
            x1 = i * (BAR_W + BAR_GAP)
            x2 = x1 + BAR_W
            mid = CANVAS_H // 2
            y1 = mid - BAR_MIN_H // 2
            y2 = mid + BAR_MIN_H // 2
            bid = self._wave_canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=CP["bar_idle"],
                outline="",
                width=0,
            )
            self._bar_ids.append(bid)

    def _animate_waveform(self) -> None:
        """Called every 40 ms while recording — updates bar heights.

        IMPORTANT: the try/except around the body is intentional and must stay.
        In the PyInstaller frozen build (console=False) any unhandled exception
        inside a tkinter after() callback is silently swallowed, which breaks
        the after(40, ...) chain and freezes the bars permanently. Wrapping the
        body ensures we always reschedule even if a single frame fails.
        """
        if not self._waveform_running:
            return

        try:
            level = self._mic_level
            t     = time.time()
            self._last_activity = t
            mid   = CANVAS_H // 2

            # Idle ceiling: bars oscillate up to 30 % of full height even in silence.
            # Voice ceiling: bars can reach 100 % when speaking.
            idle_max = BAR_MIN_H + (BAR_MAX_H - BAR_MIN_H) * 0.30

            for i, bid in enumerate(self._bar_ids):
                phase = self._bar_phases[i]

                # Gentle idle wave — always visible even with no microphone input
                idle_osc = math.sin(t * 4.0 + phase) * 0.5 + 0.5        # 0 → 1
                h_idle   = BAR_MIN_H + (idle_max - BAR_MIN_H) * idle_osc

                # Voice spike — grows with mic level, faster oscillation per bar
                voice_osc = math.sin(t * 10.0 + phase * 1.6) * 0.4 + 0.6  # 0.2 → 1.0
                h_voice   = (BAR_MAX_H - idle_max) * level * voice_osc

                h = max(BAR_MIN_H, min(BAR_MAX_H, h_idle + h_voice))

                x1 = i * (BAR_W + BAR_GAP)
                x2 = x1 + BAR_W
                # Always orange while recording so the waveform is always visible
                self._wave_canvas.coords(bid, x1, mid - h / 2, x2, mid + h / 2)
                self._wave_canvas.itemconfigure(bid, fill=CP["bar_active"])

            # Update timer
            if self._rec_start is not None:
                elapsed = time.time() - self._rec_start
                mins = int(elapsed) // 60
                secs = elapsed % 60
                self._timer_var.set(f"{mins:02d}:{secs:04.1f}")

        except Exception:
            pass  # never let a single-frame error kill the animation loop

        self.root.after(40, self._animate_waveform)

    # ── Icon frame ─────────────────────────────────────────────────────────────

    def _build_icon_frame(self) -> None:
        self._icon_frame = tk.Frame(self.root, bg=CP["bg"], padx=8, pady=6)

        from logo_cache import get_logo_photo

        self._icon_photo = get_logo_photo(self.root, CP["bg"], max_w=68, max_h=26)

        if self._icon_photo:
            lbl = tk.Label(
                self._icon_frame, image=self._icon_photo, bg=CP["bg"], cursor="hand2"
            )
        else:
            lbl = tk.Label(
                self._icon_frame,
                text="FTC",
                fg=CP["accent"],
                bg=CP["bg"],
                font=("Segoe UI", 9, "bold"),
                cursor="hand2",
            )
        lbl.pack(side="left", padx=(0, 2))

        tk.Frame(self._icon_frame, bg=CP["divider"], width=1).pack(
            side="left", fill="y", padx=(2, 4)
        )

        close = tk.Label(
            self._icon_frame,
            text="✕",
            fg=CP["subtext"],
            bg=CP["bg"],
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            padx=3,
        )
        close.pack(side="left")

        def _close_click(_e):
            self.root.after(0, self._do_hide)
            return "break"

        close.bind("<Button-1>", _close_click)
        close.bind("<Enter>", lambda _e: close.configure(fg=CP["accent"]))
        close.bind("<Leave>", lambda _e: close.configure(fg=CP["subtext"]))

        for w in (self._icon_frame, lbl):
            w.bind("<Button-1>", lambda _e: self.root.after(0, self._expand_to_panel))
            w.bind("<Enter>", lambda _e: self._icon_frame.configure(bg=CP["btn_bg"]))
            w.bind("<Leave>", lambda _e: self._icon_frame.configure(bg=CP["bg"]))

        self._dismiss_hooks = []

    # ── Refinement frame ───────────────────────────────────────────────────────

    def _build_refinement_frame(self) -> None:
        from app_window import RoundedButton
        f = tk.Frame(self.root, bg=CP["bg"], padx=16, pady=14)
        self._refine_frame = f

        # ── Row 1: status badge + Insert + AI preset buttons + close ──────────
        top = tk.Frame(f, bg=CP["bg"])
        top.pack(fill="x", pady=(0, 6))

        self._inserted_badge = tk.Label(
            top,
            text="  ✓ Inserted  ",
            fg=CP["bg"],
            bg=CP["accent"],
            font=("Segoe UI", 9, "bold"),
            padx=4,
            pady=3,
        )
        self._inserted_badge.pack(side="left", padx=(0, 6))

        # Insert button — manual fallback if auto-inject missed
        insert_btn = RoundedButton(
            top, text="↓ Insert", command=self._do_insert,
            fg=CP["text"], fill=CP["btn_bg"],
            font=("Segoe UI", 9, "bold"), padx=10, pady=4,
        )
        insert_btn.pack(side="left", padx=(0, 10))
        insert_btn.bind("<Enter>", lambda _e: insert_btn.configure(bg=CP["accent"], fg=CP["bg"]))
        insert_btn.bind("<Leave>", lambda _e: insert_btn.configure(bg=CP["btn_bg"], fg=CP["text"]))

        for label, mode in [
            ("✉ Email", "email"),
            ("🎩 Formal", "formal"),
            ("💬 Casual", "casual"),
            ("✨ Fix All", "punctuation"),
            ("✂ Short", "concise"),
            ("⚡ Optimise", "prompt_optimiser"),
        ]:
            self._btn(top, label, lambda m=mode: self._run_ai(m)).pack(
                side="left", padx=(0, 4)
            )

        close = tk.Label(
            top, text="✕", fg=CP["subtext"], bg=CP["bg"],
            font=("Segoe UI", 13), cursor="hand2", padx=8,
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._do_hide())
        close.bind("<Enter>", lambda _e: close.configure(fg=CP["accent"]))
        close.bind("<Leave>", lambda _e: close.configure(fg=CP["subtext"]))

        # ── Row 2: Ask AI custom instruction input ────────────────────────────
        ask_row = tk.Frame(f, bg=CP["bg"])
        ask_row.pack(fill="x", pady=(0, 6))

        # Multi-line, word-wrapping instruction box: starts one line high and
        # grows downward (up to ASK_MAX_LINES) as the text wraps past the right
        # edge — Text, not Entry, because Entry is single-line and scrolls
        # horizontally instead of expanding.
        self._ask_lines = 1
        self._ask_showing_placeholder = True
        self._ask_entry = tk.Text(
            ask_row,
            height=1,
            wrap="word",
            bg=CP["btn_bg"],
            fg=CP["subtext"],
            insertbackground=CP["text"],
            relief="flat",
            font=("Segoe UI", 10),
            bd=0,
            padx=6,
            pady=4,
            highlightthickness=0,
        )
        self._ask_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._ask_entry.insert("1.0", ASK_PLACEHOLDER)

        def _clear_placeholder(_e=None):
            if self._ask_showing_placeholder:
                self._ask_entry.delete("1.0", "end")
                self._ask_entry.configure(fg=CP["text"])
                self._ask_showing_placeholder = False
                self._autosize_ask()

        def _restore_placeholder(_e=None):
            if not self._ask_entry.get("1.0", "end-1c").strip():
                self._ask_entry.delete("1.0", "end")
                self._ask_entry.insert("1.0", ASK_PLACEHOLDER)
                self._ask_entry.configure(fg=CP["subtext"])
                self._ask_showing_placeholder = True
                self._autosize_ask()

        def _on_return(_e):
            self._run_ai_custom()
            return "break"  # submit on Enter — don't insert a newline

        self._ask_entry.bind("<FocusIn>", _clear_placeholder)
        self._ask_entry.bind("<FocusOut>", _restore_placeholder)
        self._ask_entry.bind("<Return>", _on_return)
        self._ask_entry.bind("<Shift-Return>", lambda _e: self._autosize_ask())
        self._ask_entry.bind("<KeyRelease>", lambda _e: self._autosize_ask())

        self._mic_btn = tk.Label(
            ask_row,
            text="🎙",
            fg=CP["subtext"],
            bg=CP["btn_bg"],
            font=("Segoe UI", 11),
            padx=6,
            pady=4,
            cursor="hand2",
        )
        self._mic_btn.pack(side="left", padx=(0, 6))
        self._mic_btn.bind("<Button-1>", lambda _e: self._on_mic_click())
        self._mic_btn.bind("<Enter>", lambda _e: self._mic_btn.configure(fg=CP["text"]) if not self._mic_recording else None)
        self._mic_btn.bind("<Leave>", lambda _e: self._mic_btn.configure(fg=CP["subtext"]) if not self._mic_recording else None)

        ask_btn = RoundedButton(
            ask_row, text="✦ Ask", command=self._run_ai_custom,
            fg=CP["bg"], fill=CP["accent"],
            font=("Segoe UI", 10, "bold"), padx=12, pady=6,
        )
        ask_btn.pack(side="left")
        ask_btn.bind("<Enter>", lambda _e: ask_btn.configure(bg=CP["accent_hover"]))
        ask_btn.bind("<Leave>", lambda _e: ask_btn.configure(bg=CP["accent"]))

        # ── Status / spinner ──────────────────────────────────────────────────
        self._ai_status = tk.Label(
            f, text="", fg=CP["accent"], bg=CP["bg"],
            font=("Segoe UI", 10, "italic"),
        )
        # Not packed initially — shown only when there is content to display

        # ── Result area ───────────────────────────────────────────────────────
        self._result_frame = tk.Frame(f, bg=CP["bg"])

        tk.Frame(self._result_frame, bg=CP["divider"], height=1).pack(
            fill="x", pady=(4, 8)
        )

        # Scrollable text area — shows full result no matter how long,
        # capped at 8 lines tall then scrolls.
        txt_frame = tk.Frame(self._result_frame, bg=CP["bg"])
        txt_frame.pack(fill="x", anchor="w")

        self._result_text = tk.Text(
            txt_frame,
            fg=CP["subtext"],
            bg=CP["bg"],
            font=("Segoe UI", 12),
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            width=52,
            height=4,       # initial height — grows up to 8 lines
            state="disabled",
            cursor="arrow",
        )
        self._result_scrollbar = tk.Scrollbar(
            txt_frame, orient="vertical",
            command=self._result_text.yview,
            bg="#3a3a3a", troughcolor=CP["bg"],
            activebackground="#505050",
            relief="flat", borderwidth=0,
            elementborderwidth=0,
            highlightthickness=0, bd=0, width=10,
        )
        self._result_text.configure(yscrollcommand=self._result_scrollbar.set)
        self._result_text.pack(side="left", fill="x", expand=True)

        btn_row = tk.Frame(self._result_frame, bg=CP["bg"])
        btn_row.pack(fill="x", pady=(8, 0))

        replace = RoundedButton(
            btn_row, text="↩  Replace & Close", command=self._do_replace,
            fg=CP["bg"], fill=CP["accent"],
            font=("Segoe UI", 10, "bold"), padx=12, pady=6,
        )
        replace.pack(side="left")
        replace.bind("<Enter>", lambda _e: replace.configure(bg=CP["accent_hover"]))
        replace.bind("<Leave>", lambda _e: replace.configure(bg=CP["accent"]))

        insert_result_btn = RoundedButton(
            btn_row, text="↓ Insert Result", command=self._do_insert_result,
            fg=CP["text"], fill=CP["btn_bg"],
            font=("Segoe UI", 10, "bold"), padx=12, pady=6,
        )
        insert_result_btn.pack(side="left", padx=(8, 0))
        insert_result_btn.bind("<Enter>", lambda _e: insert_result_btn.configure(bg=CP["accent"], fg=CP["bg"]))
        insert_result_btn.bind("<Leave>", lambda _e: insert_result_btn.configure(bg=CP["btn_bg"], fg=CP["text"]))

        self.root.bind("<Escape>", lambda _e: self._do_hide())

    def _btn(self, parent: tk.Frame, text: str, command: Callable) -> "RoundedButton":
        from app_window import RoundedButton
        b = RoundedButton(
            parent, text=text, command=command,
            fg=CP["subtext"], fill=CP["btn_bg"],
            font=("Segoe UI", 10), padx=11, pady=5,
        )
        b.bind("<Enter>", lambda _e: b.configure(fg=CP["accent"], bg=CP["btn_hover"]))
        b.bind("<Leave>", lambda _e: b.configure(fg=CP["subtext"], bg=CP["btn_bg"]))
        return b

    # ── Mode transitions ───────────────────────────────────────────────────────

    def _hide_all_frames(self) -> None:
        for frame in (self._status_frame, self._icon_frame, self._refine_frame):
            frame.pack_forget()

    def _enter_status_mode(self, text: str, recording: bool = False) -> None:
        self._set_no_activate(True)
        self._status_entered = time.time()
        self._last_activity = time.time()
        self._last_shown = time.time()
        self._stop_waveform()
        self._hide_all_frames()

        # Always remove timer/canvas/caption from pack order first so we control placement
        self._timer_lbl.pack_forget()
        self._wave_canvas.pack_forget()
        self._caption_wrap.pack_forget()

        if recording and self._captions_enabled:
            # Live-caption mode: waveform centred at the top (timer at the far
            # left), caption paragraph beneath — text starts at the left edge
            # and reads naturally rightward, wrapping down to CAPTION_MAX_LINES
            # before scrolling.
            self._draw_bars_initial()  # fresh bars every session
            self._timer_var.set("00:00.0")
            self._rec_start = time.time()
            self._timer_lbl.pack(side="left", before=self._status_label, padx=(0, 12))
            # expand=True centres the waveform in the remaining row width so it
            # sits top-middle above the caption text.
            self._wave_canvas.pack(side="left", before=self._status_label,
                                   expand=True)
            self._status_label.configure(text="")
            self._reset_caption_widget("Listening…")
            self._caption_wrap.pack(side="top", anchor="w", pady=(10, 0))
            # The waveform loop drives the timer and keeps _last_activity fresh.
            self._start_waveform()
        elif recording:
            # Correct order: timer | waveform | label
            self._draw_bars_initial()  # fresh bars every session
            self._timer_var.set("00:00.0")
            self._rec_start = time.time()
            self._timer_lbl.pack(side="left", before=self._status_label, padx=(0, 12))
            self._wave_canvas.pack(side="left", before=self._status_label, padx=(0, 12))
            self._status_label.configure(text=text)
            self._start_waveform()
        else:
            # Transcribing — just the label, no timer or waveform
            self._rec_start = None
            self._status_label.configure(text=text)

        self._status_frame.pack()
        self._mode = "status"
        self.root.update_idletasks()  # force canvas render before animation
        # Always position on the monitor where the cursor is
        self._reposition(self._status_cx, self._status_cy)
        self._show_no_activate()

    def _reset_caption_widget(self, text: str = "") -> None:
        """Reset the caption bar to a single-line-tall paragraph block (start of a
        recording). Width stays fixed at CAPTION_MAX_CHARS — only height resets."""
        self._caption_lines = 0
        self._caption_user_scroll_ts = 0.0
        self._caption_scroll.pack_forget()
        self._caption_text.configure(state="normal")
        self._caption_text.delete("1.0", "end")
        self._caption_text.insert("1.0", text)
        self._caption_text.configure(
            state="disabled", width=CAPTION_MAX_CHARS, height=1
        )

    def _update_caption_widget(self, text: str) -> None:
        """Render live caption text into the bar: fixed paragraph width, growing
        vertically up to CAPTION_MAX_LINES, then scroll (tailing the latest words).
        Runs on the UI thread via root.after()."""
        if self._mode != "status" or not self._captions_enabled:
            return
        try:
            self._caption_text.configure(state="normal")
            self._caption_text.delete("1.0", "end")
            self._caption_text.insert("1.0", text)

            # Fixed paragraph width from the first word — the bar never widens or
            # slides sideways as text arrives; it just wraps and grows downward.
            self._caption_text.configure(width=CAPTION_MAX_CHARS)

            # Height: measure wrapped display lines at this width.
            self._caption_text.update_idletasks()
            try:
                lines = int(
                    self._caption_text.count("1.0", "end-1c", "displaylines")[0]
                )
            except Exception:
                lines = int(self._caption_text.index("end-1c").split(".")[0])
            lines = max(1, lines)
            shown = min(lines, CAPTION_MAX_LINES)
            self._caption_text.configure(height=shown, state="disabled")

            if lines > shown:
                self._caption_scroll.pack(side="right", fill="y")
            else:
                self._caption_scroll.pack_forget()

            # ALWAYS tail the newest words (not just once the bar is full):
            # if the height measurement ever lags a tick, the words being
            # spoken right now would otherwise sit clipped below the visible
            # area — the "text disappears at the end of the first line" bug.
            # Suspended briefly after a manual scroll so read-back doesn't snap.
            if time.time() - self._caption_user_scroll_ts > CAPTION_SCROLL_HOLDOFF:
                self._caption_text.see("end")

            # Keep the pill bottom-anchored as it grows: reposition only when the
            # visible line count changes (avoids per-tick geometry flicker).
            if shown != self._caption_lines:
                self._caption_lines = shown
                self._reposition(self._status_cx, self._status_cy)
        except Exception:
            pass

    def _on_caption_wheel(self, event) -> str:
        """Wheel over the caption bar — scroll back through the transcript."""
        try:
            step = -(int(event.delta) // 120) * 2 or (-2 if event.delta > 0 else 2)
            self._caption_text.yview_scroll(step, "units")
            # Scrolling back pauses auto-tailing; returning to the bottom
            # (or wheeling past it) resumes it immediately.
            if self._caption_text.yview()[1] >= 0.999:
                self._caption_user_scroll_ts = 0.0
            else:
                self._caption_user_scroll_ts = time.time()
        except Exception:
            pass
        return "break"

    def _start_waveform(self) -> None:
        self._waveform_running = True
        # Small delay so canvas is fully rendered before first animation tick
        self.root.after(30, self._animate_waveform)

    def _stop_waveform(self) -> None:
        self._waveform_running = False
        self._mic_level = 0.0
        # Reset bars to idle height
        if self._bar_ids:
            for i, bid in enumerate(self._bar_ids):
                x1 = i * (BAR_W + BAR_GAP)
                x2 = x1 + BAR_W
                mid = CANVAS_H // 2
                self._wave_canvas.coords(
                    bid, x1, mid - BAR_MIN_H // 2, x2, mid + BAR_MIN_H // 2
                )
                self._wave_canvas.itemconfigure(bid, fill=CP["bar_idle"])

    def _enter_icon_mode(self) -> None:
        self._set_no_activate(True)
        self._stop_waveform()
        self._hide_all_frames()
        self._result_frame.pack_forget()
        self._ai_status.configure(text="")
        self._ai_status.pack_forget()
        self._icon_frame.pack()
        self._mode = "icon"
        self._icon_entered = time.time()
        self._last_shown = time.time()
        self._reposition(self._status_cx, self._status_cy)
        self._show_no_activate()
        if self._inserted_ok:
            self._register_space_dismiss()
        else:
            self._unregister_space_dismiss()
        # Auto-dismiss the badge after a while so it never lingers on screen.
        # Stamp-guarded so a newer popup isn't killed by an old timer.
        self.root.after(
            int(ICON_AUTO_DISMISS_SECS * 1000),
            lambda s=self._icon_entered: self._auto_dismiss_icon(s),
        )

    def _auto_dismiss_icon(self, stamp: float) -> None:
        if self._mode == "icon" and self._icon_entered == stamp:
            self._do_hide()

    def _expand_to_panel(self) -> None:
        # Space should dismiss the small badge, but NOT the full refinement panel
        # where the user may be typing in the Ask AI field.
        self._unregister_space_dismiss()
        # Remove WS_EX_NOACTIVATE so the Entry widget can receive keyboard focus
        self._set_no_activate(False)
        # Pack the panel FIRST (and refresh its status after), so a failure in
        # _refresh_insert_status can never strand a hidden-frames empty window.
        self._hide_all_frames()
        self._result_frame.pack_forget()
        self._refine_frame.pack()
        self._mode = "refinement"
        self._last_shown = time.time()
        try:
            self._refresh_insert_status()
        except Exception as e:
            print(f"[Popup] _refresh_insert_status failed: {e}")

        # Show upgrade state if accurate model is still running or already done
        if self._upgrade_result:
            self._ai_status.configure(text="")
            self._show_ai_result(self._upgrade_result)
        elif self._upgrading:
            self._ai_status.configure(text="✦  Improving accuracy…")
            self._ai_status.pack(anchor="w")
        else:
            self._ai_status.configure(text="")

        self._reposition(self._status_cx, self._status_cy)
        # Activate the window so keyboard input works
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _refresh_insert_status(self) -> None:
        if self._inserted_ok:
            self._inserted_badge.configure(
                text="  ✓ Inserted  ", fg=CP["bg"], bg=CP["accent"]
            )
        else:
            self._inserted_badge.configure(
                text="  ⚠ Not inserted  ", fg=CP["text"], bg=CP["error"]
            )

    def _register_space_dismiss(self) -> None:
        # In the post-insert icon badge, Space OR Enter dismisses it — the user
        # carries on typing (space) or sends/newlines (enter) in the target app
        # and the badge gets out of the way. suppress=False so the keypress still
        # reaches the app. NOT active in the refinement panel, where Enter submits
        # the Ask box (_expand_to_panel unhooks these first).
        try:
            import keyboard as kb

            self._unregister_space_dismiss()

            def _on_dismiss_key(_e):
                self._unregister_space_dismiss()
                if self.root:
                    self.root.after(0, self._do_hide)

            self._dismiss_hooks = [
                kb.on_press_key("space", _on_dismiss_key, suppress=False),
                kb.on_press_key("enter", _on_dismiss_key, suppress=False),
            ]
        except Exception:
            pass

    def _unregister_space_dismiss(self) -> None:
        if self._dismiss_hooks:
            try:
                import keyboard as kb

                for hook in self._dismiss_hooks:
                    if hook is not None:
                        kb.unhook(hook)
            except Exception:
                pass
            self._dismiss_hooks = []

    def _do_hide(self) -> None:
        self._set_no_activate(True)
        self._stop_waveform()
        self._unregister_space_dismiss()
        self._mode = None
        self._ai_busy = False
        # Stop an in-flight voice-prompt recording — closing the panel must not
        # leave the mic stream open with a late transcription going nowhere.
        self._mic_recording = False
        self._upgrading = False
        self._upgrade_result = None
        self._ai_status.configure(text="")
        self._ai_status.pack_forget()
        self._result_frame.pack_forget()
        self._hide_all_frames()
        self.root.withdraw()

    def clear_upgrading(self, session: int = 0) -> None:
        """Upgrade finished with nothing better to offer — stop showing
        'Improving accuracy…' (it previously stayed forever in that case)."""
        if session and session != getattr(self, "_session_token", 0):
            return
        if self.root:
            def _clear():
                if session and session != getattr(self, "_session_token", 0):
                    return
                self._upgrading = False
                if self._mode == "refinement" and not self._ai_busy and self._current_result is None:
                    self._ai_status.configure(text="")
                    self._ai_status.pack_forget()
            self.root.after(0, _clear)

    def set_upgrade_result(self, accurate_text: str, session: int = 0) -> None:
        """Called from background thread when the accurate model finishes.
        session must match the token passed to show_cursor_icon — stale results
        from an earlier dictation are dropped."""
        if session and session != getattr(self, "_session_token", 0):
            print(f"[Popup] Discarding stale upgrade result (session {session})")
            return
        if self.root:
            self.root.after(0, lambda: self._on_upgrade_ready(accurate_text, session))

    def _on_upgrade_ready(self, accurate_text: str, session: int = 0) -> None:
        # Re-check on the UI thread — a new dictation may have arrived between
        # the after() call and now.
        if session and session != getattr(self, "_session_token", 0):
            return
        self._upgrading = False
        self._upgrade_result = accurate_text
        if self._mode == "refinement":
            if self._ai_busy:
                # A user-triggered refine is in flight — don't overwrite it.
                # The result is stored in _upgrade_result and shown when the
                # panel is next opened.
                return
            self._ai_status.configure(text="")
            self._ai_status.pack_forget()
            self._show_ai_result(accurate_text)
        # In icon mode: result is stored and shown automatically when user expands

    # ── Actions ────────────────────────────────────────────────────────────────

    def _do_insert(self) -> None:
        """Manual insert — re-injects the original transcribed text at cursor."""
        if self._on_insert:
            cb = self._on_insert
            self._do_hide()
            threading.Thread(target=cb, daemon=True).start()

    def _do_insert_result(self) -> None:
        """Insert the AI result at the caret WITHOUT undoing anything — for when
        the original injection failed or the user wants the result appended.
        (Replace & Close is the undo-and-swap action.)"""
        if self._current_result and self._on_insert_result:
            result = self._current_result
            self._do_hide()
            threading.Thread(target=self._on_insert_result, args=(result,), daemon=True).start()
        # No fallback to _on_replace here: Replace UNDOES the original
        # injection first, which is the opposite of this button's contract
        # ("insert WITHOUT undoing anything").

    def _do_replace(self) -> None:
        """Undo original injection and insert AI result instead."""
        if self._current_result and self._on_replace:
            result = self._current_result
            self._do_hide()
            threading.Thread(
                target=self._on_replace, args=(result,), daemon=True
            ).start()

    # ── Voice prompt ────────────────────────────────────────────────────────────

    def _on_mic_click(self) -> None:
        if self._mic_recording:
            # User clicked stop — recording loop will exit on next iteration
            self._mic_recording = False
            return

        if not self._voice_prompt_fn:
            self._ai_status.configure(text="⚠  Voice prompt not configured")
            self._ai_status.pack(anchor="w")
            return

        self._mic_recording = True
        self._mic_btn.configure(text="⏹", fg=CP["bg"], bg=CP["accent"])
        self._ai_status.configure(text="🔴  Recording…  click ⏹ to stop")
        self._ai_status.pack(anchor="w")
        self._start_mic_pulse()

        def _run():
            if not self._voice_capture_start:
                self.root.after(0, lambda: self._ai_status.configure(
                    text="⚠  Voice capture not configured"))
                self.root.after(2000, self._finish_mic)
                return
            try:
                if not self._voice_capture_start():
                    self.root.after(0, lambda: self._ai_status.configure(
                        text="⚠  Mic unavailable"))
                    self.root.after(2000, self._finish_mic)
                    return
                start = time.time()
                last_preview = 0.0
                while self._mic_recording and (time.time() - start) < 120.0:
                    time.sleep(0.15)
                    if time.time() - last_preview >= 1.6:
                        last_preview = time.time()
                        snap, rate = self._voice_capture_read()
                        if snap is not None and len(snap) > rate // 2:
                            # non-blocking: skip if the model is busy from last preview
                            preview = self._voice_prompt_fn(snap, rate, blocking=False)
                            if preview and preview.strip():
                                p = preview.strip()
                                self.root.after(0, lambda p=p: self._set_ask_entry(p))
            except Exception as exc:
                err = str(exc)
                try:
                    self._voice_capture_stop()
                except Exception:
                    pass
                self.root.after(0, lambda: self._ai_status.configure(text=f"⚠  Mic error: {err}"))
                self.root.after(2000, self._finish_mic)
                return

            audio, rate = self._voice_capture_stop()
            if audio is None or len(audio) < rate // 4:
                self.root.after(0, lambda: self._ai_status.configure(text="⚠  No speech detected"))
                self.root.after(2000, self._finish_mic)
                return

            # Final blocking pass on the full buffer — gives the cleanest result
            self.root.after(0, lambda: self._ai_status.configure(text="✦  Finalising…"))
            try:
                text = self._voice_prompt_fn(audio, rate, blocking=True)
            except Exception as exc:
                err = str(exc)
                self.root.after(0, lambda: self._ai_status.configure(text=f"⚠  Transcription error: {err}"))
                self.root.after(2000, self._finish_mic)
                return

            if text and text.strip():
                t = text.strip()
                self.root.after(0, lambda t=t: self._set_ask_entry(t))
            else:
                self.root.after(0, lambda: self._ai_status.configure(text="⚠  No speech detected"))
                self.root.after(2000, self._finish_mic)
                return

            self.root.after(0, self._finish_mic)

        threading.Thread(target=_run, daemon=True).start()

    def _start_mic_pulse(self) -> None:
        self._mic_pulse_on = True
        self._mic_pulse_tick()

    def _mic_pulse_tick(self) -> None:
        if not self._mic_recording:
            return
        if self._mic_pulse_on:
            self._mic_btn.configure(fg=CP["bg"], bg=CP["accent"])
        else:
            self._mic_btn.configure(fg=CP["accent"], bg=CP["btn_bg"])
        self._mic_pulse_on = not self._mic_pulse_on
        self.root.after(500, self._mic_pulse_tick)

    def _set_ask_entry(self, text: str) -> None:
        self._ask_entry.delete("1.0", "end")
        self._ask_entry.insert("1.0", text)
        self._ask_entry.configure(fg=CP["text"])
        self._ask_showing_placeholder = False
        self._autosize_ask()

    def _autosize_ask(self) -> None:
        """Grow/shrink the Ask box to fit its wrapped text (1..ASK_MAX_LINES lines).

        Uses displaylines so word-wrapped text counts each visual row, not just
        newline-delimited lines. Repositions the panel when the height changes so
        it stays anchored to the bottom of the screen as it grows upward.
        """
        try:
            self._ask_entry.update_idletasks()
            res = self._ask_entry.count("1.0", "end-1c", "displaylines")
            if isinstance(res, (tuple, list)):
                n = res[0] if res else 1
            elif res is None:
                n = 1
            else:
                n = int(res)
            n = max(1, min(int(n), ASK_MAX_LINES))
        except Exception:
            n = 1
        if n == self._ask_lines:
            return
        self._ask_lines = n
        self._ask_entry.configure(height=n)
        # Panel is anchored bottom-centre (y = bottom - height - 60), so a taller
        # box must be re-placed or it would grow down into the taskbar.
        if self._mode == "refinement":
            try:
                self._reposition(self._status_cx, self._status_cy)
            except Exception:
                pass

    def _finish_mic(self) -> None:
        self._mic_recording = False
        self._mic_btn.configure(text="🎙", fg=CP["subtext"], bg=CP["btn_bg"])
        self._ai_status.configure(text="")
        self._ai_status.pack_forget()

    def _reset_mic_btn(self) -> None:
        self._mic_recording = False
        self._mic_btn.configure(text="🎙", fg=CP["subtext"], bg=CP["btn_bg"])

    # ── AI refinement ──────────────────────────────────────────────────────────

    def _run_ai(self, mode: str) -> None:
        if not self._ai_refiner or not self._ai_refiner.is_available:
            self._ai_status.configure(text="⚠  Set anthropic_api_key in config to enable AI")
            self._ai_status.pack(anchor="w")
            return
        if self._ai_busy:
            return
        self._ai_busy = True
        self._ai_status.configure(text=f"✦  Refining ({mode})…")
        self._ai_status.pack(anchor="w")
        self._result_frame.pack_forget()
        text = self._original_text

        token = self._session_token

        def _worker():
            result = self._ai_refiner.refine(text, mode)
            self.root.after(0, self._show_ai_result, result, token)

        threading.Thread(target=_worker, daemon=True).start()

    def _run_ai_custom(self) -> None:
        """Run AI refinement with a custom instruction typed by the user."""
        instruction = "" if self._ask_showing_placeholder else self._ask_entry.get("1.0", "end-1c").strip()
        if not instruction:
            self._ai_status.configure(text="⚠  Type an instruction first")
            self._ai_status.pack(anchor="w")
            return
        if not self._ai_refiner or not self._ai_refiner.is_available:
            self._ai_status.configure(text="⚠  Set anthropic_api_key in config to enable AI")
            self._ai_status.pack(anchor="w")
            return
        if self._ai_busy:
            return
        self._ai_busy = True
        self._ai_status.configure(text=f"✦  Asking AI…")
        self._ai_status.pack(anchor="w")
        self._result_frame.pack_forget()
        text = self._original_text
        custom_prompt = (
            f"{instruction}. "
            "Return only the rewritten text, nothing else."
        )

        token = self._session_token

        def _worker():
            result = self._ai_refiner.refine(text, custom_prompt=custom_prompt)
            self.root.after(0, self._show_ai_result, result, token)

        threading.Thread(target=_worker, daemon=True).start()

    def _show_ai_result(self, text: str, session: int = 0) -> None:
        self._ai_busy = False
        if self._mode is None:
            # Popup was closed while the worker was running; discard silently.
            return
        if session and session != self._session_token:
            # Result belongs to an EARLIER dictation — a new popup session
            # started while the worker ran. Displaying it would let "Replace"
            # inject the old dictation's text over the new one.
            return
        self._current_result = text
        self._ai_status.configure(text="")
        self._ai_status.pack_forget()  # hide spinner row — no blank gap

        # Write full text into the scrollable widget
        self._result_text.configure(state="normal")
        self._result_text.delete("1.0", "end")
        self._result_text.insert("1.0", text)
        self._result_text.configure(state="disabled")

        # Auto-size height: count DISPLAY lines (wrapped), not logical newlines —
        # a single-paragraph result (the normal case) wraps to many display
        # lines but has line_count == 1, which clamped the box to 2 rows.
        self._result_text.update_idletasks()
        try:
            line_count = int(
                self._result_text.count("1.0", "end-1c", "displaylines")[0]
            )
        except Exception:
            line_count = int(self._result_text.index("end-1c").split(".")[0])
        self._result_text.configure(height=min(max(line_count, 2), 8))

        # Show scrollbar only when text exceeds visible area
        if line_count > 8:
            self._result_scrollbar.pack(side="right", fill="y")
        else:
            self._result_scrollbar.pack_forget()

        self._result_frame.pack(fill="x")
        self._reposition(self._status_cx, self._status_cy)

    # ── Positioning ────────────────────────────────────────────────────────────

    @staticmethod
    def _get_cursor_pos() -> tuple[int, int]:
        try:
            pt = _POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return pt.x, pt.y
        except Exception:
            return 0, 0

    def _get_monitor_workarea(
        self, x: int = 0, y: int = 0
    ) -> tuple[int, int, int, int]:
        try:
            MONITOR_DEFAULTTONEAREST = 2
            u32 = ctypes.windll.user32
            # Prefer explicit cursor coords — always puts popup on the user's active screen.
            # Fall back to target hwnd only when no cursor coords given.
            if x or y:
                pt = _POINT()
                pt.x, pt.y = x, y
                hmon = u32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
            elif self._target_hwnd:
                hmon = u32.MonitorFromWindow(
                    self._target_hwnd, MONITOR_DEFAULTTONEAREST
                )
            else:
                # Last resort: monitor containing the current cursor
                pt = _POINT()
                u32.GetCursorPos(ctypes.byref(pt))
                hmon = u32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            u32.GetMonitorInfoW(hmon, ctypes.byref(info))
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
        except Exception:
            return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _dpi_scale(self) -> tuple[float, float]:
        """
        Scale factors (sx, sy) from Win32 physical pixels → tkinter logical pixels.

        Win32 GetMonitorInfoW / GetCursorPos return physical pixels for DPI-aware
        processes and logical pixels for non-DPI-aware ones.  tkinter geometry()
        always uses logical pixels.  Comparing SM_CXSCREEN (Win32 physical) with
        winfo_screenwidth() (tkinter logical) gives the scale factor; if the
        process is non-DPI-aware the two match and (1.0, 1.0) is returned.
        """
        try:
            phys_w = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN physical
            phys_h = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN physical
            tk_w   = self.root.winfo_screenwidth()
            tk_h   = self.root.winfo_screenheight()
            if phys_w > 0 and phys_h > 0:
                return tk_w / phys_w, tk_h / phys_h
        except Exception:
            pass
        return 1.0, 1.0

    def _reposition(self, cx: int = 0, cy: int = 0, near_cursor: bool = False) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()

        # Use Win32 MonitorFromPoint to pick the correct monitor (multi-monitor
        # aware — popup follows whichever screen the cursor is on).  Then scale
        # the work-area from Win32 physical pixels to tkinter logical pixels so
        # geometry() lands in the right place on any DPI configuration.
        try:
            left_p, top_p, right_p, bottom_p = self._get_monitor_workarea(cx, cy)
            sx, sy = self._dpi_scale()
            left   = round(left_p   * sx)
            top    = round(top_p    * sy)
            right  = round(right_p  * sx)
            bottom = round(bottom_p * sy)
        except Exception:
            left, top, sx, sy = 0, 0, 1.0, 1.0
            right  = self.root.winfo_screenwidth()
            bottom = self.root.winfo_screenheight()

        if near_cursor and cx > 0 and cy > 0:
            gap = 28
            x   = max(left, min(round(cx * sx) - w // 2, right - w))
            y_b = round(cy * sy) + gap
            y   = y_b if y_b + h <= bottom else max(top, round(cy * sy) - h - gap)
        else:
            # Fixed bottom-centre of whichever monitor the cursor is on.
            x = left + (right - left - w) // 2
            y = bottom - h - 60   # 60 px above the taskbar
        self.root.geometry(f"+{x}+{y}")
        # Flush the position to the window BEFORE the caller deiconifies it, so it
        # never maps at the old/0,0 spot for a frame (top-left black-box flash).
        self.root.update_idletasks()
