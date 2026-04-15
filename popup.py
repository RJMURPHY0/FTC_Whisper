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

POPUP_RADIUS = 16  # window-level corner radius

# Waveform config
NUM_BARS = 16
BAR_W = 3
BAR_GAP = 2
BAR_MAX_H = 30
BAR_MIN_H = 3
CANVAS_W = NUM_BARS * (BAR_W + BAR_GAP) - BAR_GAP
CANVAS_H = BAR_MAX_H + 4


def _apply_popup_corners(hwnd: int, w: int, h: int, r: int = POPUP_RADIUS) -> None:
    """Clip the popup window to a rounded rectangle using GDI SetWindowRgn."""
    try:
        hRgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r * 2, r * 2)
        ctypes.windll.user32.SetWindowRgn(hwnd, hRgn, True)
    except Exception:
        pass


class FloatingPopup:
    def __init__(self):
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        self._ready.wait()

        self._mode: Optional[str] = None
        self._on_insert: Optional[Callable] = None
        self._on_replace: Optional[Callable] = None
        self._target_hwnd: int = 0
        self._cursor_x: int = 0
        self._cursor_y: int = 0
        self._ai_refiner = None
        self._original_text: str = ""
        self._current_result: Optional[str] = None
        self._inserted_ok: bool = True
        self._ai_busy: bool = False
        self._popup_hwnd: int = 0

        # Waveform state
        self._mic_level: float = 0.0  # 0.0–1.0, updated by audio thread
        self._waveform_running: bool = False
        self._bar_phases = [i * (2 * math.pi / NUM_BARS) for i in range(NUM_BARS)]

        # Cursor position at last show_status call — used for monitor selection
        self._status_cx: int = 0
        self._status_cy: int = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_ai_refiner(self, refiner) -> None:
        self._ai_refiner = refiner

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
    ) -> None:
        self._on_insert = on_insert
        self._on_replace = on_replace
        self._inserted_ok = inserted
        self._target_hwnd = hwnd
        self._original_text = text
        self._current_result = None
        # Use pre-captured position (from recording start) if provided,
        # otherwise fall back to current mouse position
        if cursor_x or cursor_y:
            self._cursor_x, self._cursor_y = cursor_x, cursor_y
        else:
            self._cursor_x, self._cursor_y = self._get_cursor_pos()
        if self.root:
            self.root.after(0, self._enter_icon_mode)

    def update_mic_level(self, level: float) -> None:
        """Called from the audio thread with RMS level (0.0–1.0)."""
        self._mic_level = max(0.0, min(1.0, level))

    def hide(self) -> None:
        if self.root:
            self.root.after(0, self._do_hide)

    @property
    def is_user_facing(self) -> bool:
        return self._mode in ("icon", "refinement")

    # ── Tkinter setup ──────────────────────────────────────────────────────────

    def _run_tk(self) -> None:
        self.root = tk.Tk()
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

        self._build_status_frame()
        self._build_icon_frame()
        self._build_refinement_frame()

        self.root.bind("<Configure>", self._on_popup_configure)

        self._ready.set()
        self.root.mainloop()

    def _on_popup_configure(self, event) -> None:
        if event.widget is self.root:
            self.root.update_idletasks()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            if w > 4 and h > 4 and self._popup_hwnd:
                _apply_popup_corners(self._popup_hwnd, w, h)

    # ── Status frame (recording / transcribing pill) ───────────────────────────

    def _build_status_frame(self) -> None:
        f = tk.Frame(self.root, bg=CP["bg"], padx=14, pady=10)
        self._status_frame = f

        # Timer label  e.g. "01:16.2"
        self._timer_var = tk.StringVar(value="")
        self._timer_lbl = tk.Label(
            f,
            textvariable=self._timer_var,
            fg=CP["text"],
            bg=CP["bg"],
            font=("Consolas", 13, "bold"),
        )
        # NOT packed here — packed on demand in _enter_status_mode

        # Waveform canvas — bars animated by microphone level
        self._wave_canvas = tk.Canvas(
            f,
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
            f,
            text="",
            fg=CP["subtext"],
            bg=CP["bg"],
            font=("Segoe UI", 11, "bold"),
        )
        # Packed at build time — always visible in the status frame
        self._status_label.pack(side="left")

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
        """Called every 40 ms while recording — updates bar heights."""
        if not self._waveform_running:
            return

        level = self._mic_level
        t = time.time()
        mid = CANVAS_H // 2

        for i, bid in enumerate(self._bar_ids):
            phase = self._bar_phases[i]

            # Ambient oscillation — always present, gives a gentle idle wave
            ambient = (math.sin(t * 3.5 + phase) * 0.5 + 0.5) * 0.22  # 0 → 0.22

            # Speech component — scales with mic level, faster oscillation
            speech = level * (math.sin(t * 9.0 + phase * 1.4) * 0.35 + 0.65)

            normalized = min(1.0, ambient + speech)
            h = BAR_MIN_H + (BAR_MAX_H - BAR_MIN_H) * normalized

            x1 = i * (BAR_W + BAR_GAP)
            x2 = x1 + BAR_W
            color = CP["bar_active"] if level > 0.005 else CP["bar_idle"]
            self._wave_canvas.coords(bid, x1, mid - h / 2, x2, mid + h / 2)
            self._wave_canvas.itemconfigure(bid, fill=color)

        # Update timer
        if self._rec_start is not None:
            elapsed = time.time() - self._rec_start
            mins = int(elapsed) // 60
            secs = elapsed % 60
            self._timer_var.set(f"{mins:02d}:{secs:04.1f}")

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

        self._space_hook = None

    # ── Refinement frame ───────────────────────────────────────────────────────

    def _build_refinement_frame(self) -> None:
        f = tk.Frame(self.root, bg=CP["bg"], padx=16, pady=14)
        self._refine_frame = f

        # Top row: inserted badge + Insert button + AI buttons + close
        top = tk.Frame(f, bg=CP["bg"])
        top.pack(fill="x", pady=(0, 10))

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
        insert_btn = tk.Label(
            top,
            text="  ↓ Insert  ",
            fg=CP["text"],
            bg=CP["btn_bg"],
            font=("Segoe UI", 9, "bold"),
            padx=6,
            pady=3,
            cursor="hand2",
        )
        insert_btn.pack(side="left", padx=(0, 10))
        insert_btn.bind("<Button-1>", lambda _e: self._do_insert())
        insert_btn.bind(
            "<Enter>", lambda _e: insert_btn.configure(bg=CP["accent"], fg=CP["bg"])
        )
        insert_btn.bind(
            "<Leave>", lambda _e: insert_btn.configure(bg=CP["btn_bg"], fg=CP["text"])
        )

        for label, mode in [
            ("✉ Email", "email"),
            ("🎩 Formal", "formal"),
            ("💬 Casual", "casual"),
            ("✨ Fix", "punctuation"),
            ("✂ Short", "concise"),
            ("⚡ Optimise", "prompt_optimiser"),
        ]:
            self._btn(top, label, lambda m=mode: self._run_ai(m)).pack(
                side="left", padx=(0, 4)
            )

        close = tk.Label(
            top,
            text="✕",
            fg=CP["subtext"],
            bg=CP["bg"],
            font=("Segoe UI", 13),
            cursor="hand2",
            padx=8,
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._do_hide())
        close.bind("<Enter>", lambda _e: close.configure(fg=CP["accent"]))
        close.bind("<Leave>", lambda _e: close.configure(fg=CP["subtext"]))

        self._ai_status = tk.Label(
            f,
            text="",
            fg=CP["accent"],
            bg=CP["bg"],
            font=("Segoe UI", 10, "italic"),
        )
        self._ai_status.pack(anchor="w")

        # Result area
        self._result_frame = tk.Frame(f, bg=CP["bg"])

        tk.Frame(self._result_frame, bg=CP["divider"], height=1).pack(
            fill="x", pady=(4, 8)
        )

        self._result_text = tk.Label(
            self._result_frame,
            text="",
            fg=CP["subtext"],
            bg=CP["bg"],
            font=("Segoe UI", 12),
            wraplength=520,
            justify="left",
        )
        self._result_text.pack(anchor="w")

        btn_row = tk.Frame(self._result_frame, bg=CP["bg"])
        btn_row.pack(fill="x", pady=(8, 0))

        replace = tk.Label(
            btn_row,
            text="  ↩  Replace  ",
            fg=CP["bg"],
            bg=CP["accent"],
            font=("Segoe UI", 10, "bold"),
            padx=10,
            pady=6,
            cursor="hand2",
        )
        replace.pack(side="left")
        replace.bind("<Button-1>", lambda _e: self._do_replace())
        replace.bind("<Enter>", lambda _e: replace.configure(bg=CP["accent_hover"]))
        replace.bind("<Leave>", lambda _e: replace.configure(bg=CP["accent"]))

        self.root.bind("<Escape>", lambda _e: self._do_hide())

    def _btn(self, parent: tk.Frame, text: str, command: Callable) -> tk.Label:
        b = tk.Label(
            parent,
            text=text,
            fg=CP["subtext"],
            bg=CP["btn_bg"],
            font=("Segoe UI", 10),
            padx=8,
            pady=5,
            cursor="hand2",
        )
        b.bind("<Button-1>", lambda _e: command())
        b.bind("<Enter>", lambda _e: b.configure(fg=CP["accent"], bg=CP["btn_hover"]))
        b.bind("<Leave>", lambda _e: b.configure(fg=CP["subtext"], bg=CP["btn_bg"]))
        return b

    # ── Mode transitions ───────────────────────────────────────────────────────

    def _hide_all_frames(self) -> None:
        for frame in (self._status_frame, self._icon_frame, self._refine_frame):
            frame.pack_forget()

    def _enter_status_mode(self, text: str, recording: bool = False) -> None:
        self._stop_waveform()
        self._hide_all_frames()

        # Always remove timer/canvas from pack order first so we control placement
        self._timer_lbl.pack_forget()
        self._wave_canvas.pack_forget()

        if recording:
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
        self.root.deiconify()
        self.root.lift()

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
        self._stop_waveform()
        self._hide_all_frames()
        self._result_frame.pack_forget()
        self._ai_status.configure(text="")
        self._icon_frame.pack()
        self._mode = "icon"
        # Use the same fixed bottom-centre position as the recording status pill
        # so the badge always appears in a predictable, consistent location.
        self._reposition(self._status_cx, self._status_cy)
        self.root.deiconify()
        self.root.lift()
        if self._inserted_ok:
            self._register_space_dismiss()
        else:
            self._unregister_space_dismiss()

    def _expand_to_panel(self) -> None:
        self._hide_all_frames()
        self._result_frame.pack_forget()
        self._ai_status.configure(text="")
        self._refresh_insert_status()
        self._refine_frame.pack()
        self._mode = "refinement"
        self._reposition(self._status_cx, self._status_cy)
        self.root.lift()

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
        try:
            import keyboard as kb

            self._unregister_space_dismiss()

            def _on_space(_e):
                self._unregister_space_dismiss()
                if self.root:
                    self.root.after(0, self._do_hide)

            self._space_hook = kb.on_press_key("space", _on_space, suppress=False)
        except Exception:
            pass

    def _unregister_space_dismiss(self) -> None:
        if self._space_hook is not None:
            try:
                import keyboard as kb

                kb.unhook(self._space_hook)
            except Exception:
                pass
            self._space_hook = None

    def _do_hide(self) -> None:
        self._stop_waveform()
        self._unregister_space_dismiss()
        self._mode = None
        self._ai_busy = False
        self._hide_all_frames()
        self.root.withdraw()

    # ── Actions ────────────────────────────────────────────────────────────────

    def _do_insert(self) -> None:
        """Manual insert — re-injects the original transcribed text at cursor."""
        if self._on_insert:
            cb = self._on_insert
            self._do_hide()
            threading.Thread(target=cb, daemon=True).start()

    def _do_replace(self) -> None:
        if self._current_result and self._on_replace:
            result = self._current_result
            self._do_hide()
            threading.Thread(
                target=self._on_replace, args=(result,), daemon=True
            ).start()

    # ── AI refinement ──────────────────────────────────────────────────────────

    def _run_ai(self, mode: str) -> None:
        if not self._ai_refiner or not self._ai_refiner.is_available:
            self._ai_status.configure(
                text="⚠  Set ANTHROPIC_API_KEY to enable AI refinement"
            )
            return
        if self._ai_busy:
            return
        self._ai_busy = True
        self._ai_status.configure(text=f"✦  Refining ({mode})…")
        self._result_frame.pack_forget()
        text = self._original_text

        def _worker():
            result = self._ai_refiner.refine(text, mode)
            self.root.after(0, self._show_ai_result, result)

        threading.Thread(target=_worker, daemon=True).start()

    def _show_ai_result(self, text: str) -> None:
        self._ai_busy = False
        self._current_result = text
        self._ai_status.configure(text="")
        display = text if len(text) <= 140 else text[:137] + "…"
        self._result_text.configure(text=display)
        self._result_frame.pack(fill="x")
        self._reposition(self._cursor_x, self._cursor_y)

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

    def _reposition(self, cx: int = 0, cy: int = 0, near_cursor: bool = False) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        left, top, right, bottom = self._get_monitor_workarea(cx, cy)
        if near_cursor and cx > 0 and cy > 0:
            # Position the popup below the caret with a comfortable gap.
            # If there isn't enough room below, flip it above.
            gap = 28
            x = max(left, min(cx - w // 2, right - w))
            y_below = cy + gap
            if y_below + h <= bottom:
                y = y_below
            else:
                y = max(top, cy - h - gap)
        else:
            x = left + (right - left - w) // 2
            y = bottom - h - 130
        self.root.geometry(f"+{x}+{y}")
