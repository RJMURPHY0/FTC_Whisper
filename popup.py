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


def _bar_geom(index: int, height: float) -> tuple[float, float]:
    """(centre_x, half_length) for the line segment that draws bar `index`.

    Bars are round-capped lines rather than rectangles, so the cap adds BAR_W/2
    past each endpoint. The segment is shortened by BAR_W so the drawn height
    still equals `height` and the tallest bar keeps fitting inside CANVAS_H.
    Floored just above zero so the shortest bar renders as a dot — a
    zero-length line draws nothing.
    """
    cx = index * (BAR_W + BAR_GAP) + BAR_W / 2
    return cx, max(0.5, (height - BAR_W) / 2)


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

# Z-order upkeep. SetWindowPos flags for "raise to the front of the topmost band
# without touching position, size or focus".
_HWND_TOP          = 0
_HWND_TOPMOST      = -1
_GWL_EXSTYLE       = -20
_WS_EX_TOPMOST     = 0x0008
_SWP_NOSIZE        = 0x0001
_SWP_NOMOVE        = 0x0002
_SWP_NOACTIVATE    = 0x0010
_SWP_NOOWNERZORDER = 0x0200
# How often to re-assert while the popup is on screen. Fast enough that being
# covered is never something the user sees for long, slow enough to be free.
_ONTOP_INTERVAL_MS = 500

# The expanded refinement panel dismisses itself after this long with no
# interaction. Typing in the Ask box, a running AI call, a voice prompt, an
# in-flight upgrade, or the pointer resting over the panel all count as
# activity and restart the countdown.
PANEL_IDLE_DISMISS_SECS = 45.0


def _apply_popup_corners(hwnd: int) -> bool:
    """Apply Win11 DWM rounded corners — no GDI clipping, no black artifacts.

    `hwnd` may be a Tk *child* handle: on Windows `Toplevel.winfo_id()` returns
    the inner Tk window, not the frame DWM actually composes. Passing that
    straight to DwmSetWindowAttribute fails with E_HANDLE (0x80070006) and the
    corners stay square — silently, because ctypes does not raise on a bad
    HRESULT. Resolve to the real top-level via GA_ROOT first, and return whether
    the call actually succeeded so callers can tell a no-op from a success.
    """
    try:
        GA_ROOT = 2
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        top = ctypes.windll.user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        pref = ctypes.c_int(DWMWCP_ROUND)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            top, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref),
        )
        return hr == 0
    except Exception:
        return False


class _RoundedField(tk.Canvas):
    """Hosts a square-cornered tk widget on a rounded-rect background.

    tk.Text has no corner radius and paints an opaque rectangle, so the only way
    to round one is to inset it far enough that its own corners never reach the
    curve and paint the rounding underneath. `fill` matches the child's own bg,
    so the seam between the two is invisible.

    The child keeps driving the layout: callers still size it in character units
    and `sync()` re-reads its requested size, so the popup geometry that hangs
    off that sizing (caption growth, Ask-box autosize) behaves exactly as before.
    """

    def __init__(self, parent, *, fill, radius=6, pad=4, on_resize=None, **kw):
        super().__init__(parent, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, **kw)
        self._fill = fill
        self._radius = radius
        self._pad = pad
        # Fired when the usable inner width changes. A child that measures its
        # own wrapped height (the Ask box) must re-measure afterwards: until the
        # canvas has laid out, the child still reports its natural width and
        # would count every line as fitting on one row.
        self._on_resize = on_resize
        self._last_inner_w = None
        self._shape = None
        self._child = None
        self._win = None
        self._stretch = False
        self.bind("<Configure>", lambda _e: self._paint())

    def host(self, child, *, stretch: bool = False):
        """Embed `child`. stretch=True fills the canvas width (for pack expand),
        else the canvas takes its width from the child's own requested size."""
        self._child = child
        self._stretch = stretch
        self._win = self.create_window(self._pad, self._pad, anchor="nw",
                                       window=child)
        self.sync()
        return child

    def sync(self) -> None:
        """Re-read the child's requested size and resize to wrap it."""
        if self._child is None:
            return
        self.configure(height=self._child.winfo_reqheight() + self._pad * 2)
        if not self._stretch:
            self.configure(width=self._child.winfo_reqwidth() + self._pad * 2)
        self._paint()

    def _paint(self) -> None:
        if self._child is None:
            return
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        from app_window import _rr
        if self._shape is not None:
            self.delete(self._shape)
        self._shape = _rr(self, 0, 0, w - 1, h - 1, self._radius, fill=self._fill)
        self.tag_lower(self._shape)
        inner_w = (w - self._pad * 2) if self._stretch else self._child.winfo_reqwidth()
        self.itemconfigure(self._win, width=max(1, inner_w),
                           height=max(1, h - self._pad * 2))
        if inner_w != self._last_inner_w:
            self._last_inner_w = inner_w
            if self._on_resize is not None:
                # after_idle so the child has actually re-wrapped at the new
                # width before anything measures it.
                self.after_idle(self._on_resize)


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
        # Refine measurement — the raw numbers behind the "Time saved" card's
        # AI-refine line. Elapsed is wall-clock from opening the panel to the
        # user applying a result, so the saving is computed against what the
        # refine ACTUALLY cost, never an assumed figure. See stats.refine_saved_seconds.
        self._refine_started_at: Optional[float] = None
        self._refine_prompt_words: int = 0
        self._refine_prompt_spoken: bool = False
        self._popup_hwnd: int = 0
        self._popup_height: str = "low"  # "low" | "medium" | "high" — vertical placement of the fixed popup
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
        # Z-order upkeep: -topmost only puts the window in the topmost band, it
        # does not keep it at the front of it (see _assert_topmost).
        self._popup_top_hwnd: int = 0
        self._ontop_tick = None
        self._last_geometry: str = ""

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_ai_refiner(self, refiner) -> None:
        self._ai_refiner = refiner

    def set_popup_height(self, value: str) -> None:
        """Vertical placement of the fixed popup: "low" (near the taskbar,
        default), "medium" (mid-screen) or "high" (near the top). Applies to the
        next _reposition — no restart needed."""
        self._popup_height = value if value in ("low", "medium", "high") else "low"

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

    def flash_status(
        self,
        text: str,
        hwnd: int = 0,
        cursor_x: int = 0,
        cursor_y: int = 0,
        ms: int = 1800,
    ) -> None:
        """Show a transient status label, then auto-hide it. Used for one-off
        notices like 'No text selected'. Stamp-guarded so a newer popup (a real
        dictation / refine that starts within the window) is never hidden by this
        timer."""
        self.show_status(text, hwnd=hwnd, recording=False,
                         cursor_x=cursor_x, cursor_y=cursor_y)
        if not self.root:
            return

        def _schedule() -> None:
            stamp = self._status_entered

            def _maybe_hide(s=stamp) -> None:
                if self._mode == "status" and self._status_entered == s:
                    self._do_hide()

            self.root.after(ms, _maybe_hide)

        # _status_entered is stamped inside _enter_status_mode (queued by
        # show_status via after(0)); read it there, after that has run.
        self.root.after(0, _schedule)

    def set_status_text(self, text: str) -> None:
        """Update the status pill label in place, no re-layout. Used to flip
        the honest 'Starting…' cue to 'Recording' once the mic stream is
        proven live. No-op outside status mode, and in caption mode, where
        the label is deliberately empty (the caption bar is the cue there)."""
        if not self.root:
            return

        def _apply() -> None:
            if self._mode != "status":
                return
            try:
                if self._status_label.cget("text"):
                    self._status_label.configure(text=text)
            except tk.TclError:
                pass

        self.root.after(0, _apply)

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
        self._refine_started_at = None
        self._refine_prompt_words = 0
        self._refine_prompt_spoken = False
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
        # Distinct title (never rendered — the window is borderless): a second
        # window titled "FTC Whisper" made every FindWindowW-by-title lookup a
        # coin flip between the dashboard and this popup.
        self.root.title("FTC Whisper Overlay")
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

    def _top_hwnd(self) -> int:
        """The REAL top-level window handle.

        On Windows tkinter's winfo_id() hands back a CHILD window whose parent is
        the wrapper Tk actually manages (verified: the child carries exstyle
        0x04, the parent carries TOPMOST|TOOLWINDOW). Z-order, topmost and
        repaint all belong to the parent, so calling them on winfo_id() silently
        does nothing at all."""
        if not self._popup_top_hwnd:
            try:
                GA_ROOT = 2
                self._popup_top_hwnd = (
                    ctypes.windll.user32.GetAncestor(self._popup_hwnd, GA_ROOT)
                    or self._popup_hwnd)
            except Exception:
                self._popup_top_hwnd = self._popup_hwnd
        return self._popup_top_hwnd

    def _assert_topmost(self) -> None:
        """Push the popup back to the FRONT of the topmost band.

        `-topmost` only puts a window in that band; it does not keep it at the
        front of it. The taskbar is topmost too, and so is every other app's
        always-on-top window — whichever was raised last wins, which is how the
        recording pill ended up behind the taskbar after an app switch. Cheap
        (one SetWindowPos), and SWP_NOACTIVATE means re-asserting never takes
        focus off whatever the user is typing into.

        The raise MUST be HWND_TOP, not HWND_TOPMOST: SetWindowPos with
        HWND_TOPMOST on a window that is ALREADY topmost fails outright — it
        returns 0 and the Z-order does not move (measured, both ways round).
        HWND_TOP raises within whichever band the window is in, so it is
        HWND_TOPMOST that establishes the band and HWND_TOP that wins it."""
        h = self._top_hwnd()
        if not h:
            return
        try:
            u32 = ctypes.windll.user32
            flags = (_SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
                     | _SWP_NOOWNERZORDER)
            if not (u32.GetWindowLongW(h, _GWL_EXSTYLE) & _WS_EX_TOPMOST):
                u32.SetWindowPos(h, _HWND_TOPMOST, 0, 0, 0, 0, flags)
            u32.SetWindowPos(h, _HWND_TOP, 0, 0, 0, 0, flags)
        except Exception:
            pass

    def _keep_on_top(self) -> None:
        """Re-assert Z-order for as long as anything is on screen. One pass at
        show time is not enough: the pill is up for the whole dictation and the
        user switches apps, clicks the taskbar and opens other always-on-top
        windows while it is."""
        self._ontop_tick = None
        if not self.root or self._mode is None:
            return
        self._assert_topmost()
        try:
            self._ontop_tick = self.root.after(_ONTOP_INTERVAL_MS, self._keep_on_top)
        except tk.TclError:
            self._ontop_tick = None

    def _start_keep_on_top(self) -> None:
        if self._ontop_tick is None:
            self._keep_on_top()

    def _stop_keep_on_top(self) -> None:
        if self._ontop_tick is not None:
            try:
                self.root.after_cancel(self._ontop_tick)
            except Exception:
                pass
            self._ontop_tick = None

    def _repaint_popup(self) -> None:
        """Force the whole frame to redraw where it now is.

        Moving or resizing a mapped overrideredirect window lets Windows blit the
        old pixels into the new position — the half-drawn, wrong-content pill.
        Refuses hwnd 0: RedrawWindow(NULL, …) repaints the DESKTOP."""
        h = self._top_hwnd()
        if not h:
            return
        try:
            RDW_INVALIDATE, RDW_ERASE = 0x0001, 0x0004
            RDW_ALLCHILDREN, RDW_UPDATENOW = 0x0080, 0x0100
            ctypes.windll.user32.RedrawWindow(
                h, None, None,
                RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW)
        except Exception:
            pass

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
        # AFTER handing focus back, not before: activating another window raises
        # it, and the taskbar shares our band.
        self._assert_topmost()
        self._repaint_popup()
        self._start_keep_on_top()

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
        self._caption_box = _RoundedField(
            self._caption_wrap, fill=CP["bg_light"], radius=6, pad=4
        )
        self._caption_text = tk.Text(
            self._caption_box,
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
        self._caption_box.host(self._caption_text)
        self._caption_box.pack(side="left", fill="both", expand=True)
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
        self._bars_active = False
        mid = CANVAS_H // 2
        for i in range(NUM_BARS):
            cx, half = _bar_geom(i, BAR_MIN_H)
            bid = self._wave_canvas.create_line(
                cx,
                mid - half,
                cx,
                mid + half,
                fill=CP["bar_idle"],
                width=BAR_W,
                capstyle="round",
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
            # Colour is a one-off, not a per-frame job: the bars are orange for
            # the whole recording. Re-sending the same fill for every bar at
            # 25fps was ~500 no-op Tcl round-trips a second on the UI thread.
            if not getattr(self, "_bars_active", False):
                for bid in self._bar_ids:
                    self._wave_canvas.itemconfigure(bid, fill=CP["bar_active"])
                self._bars_active = True
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

                cx, half = _bar_geom(i, h)
                self._wave_canvas.coords(bid, cx, mid - half, cx, mid + half)

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
        self._ask_box = _RoundedField(ask_row, fill=CP["btn_bg"], radius=6, pad=4,
                                      on_resize=lambda: self._autosize_ask())
        self._ask_entry = tk.Text(
            self._ask_box,
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
        self._ask_box.host(self._ask_entry, stretch=True)
        self._ask_box.pack(side="left", fill="x", expand=True, padx=(0, 4))
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
        self._ask_entry.bind(
            "<KeyRelease>",
            lambda _e: (self._touch_panel_activity(), self._autosize_ask()))

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
        # Space/Enter dismiss belongs to the post-insert badge ALONE. Starting a
        # new dictation while the previous badge is still up left its hook live,
        # so a space typed mid-recording hid the pill (and the badge's own
        # auto-dismiss timer bails on a mode change, so nothing ever took it
        # down). Every entry into status mode clears it.
        self._unregister_space_dismiss()
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
        self._caption_box.sync()

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

            # Width is fixed for the whole recording (set in
            # _reset_caption_widget) — the bar never widens or slides sideways
            # as text arrives, it wraps and grows downward. Re-applying the same
            # width here forced a Tk rewrap on every tick, right before the
            # displaylines measurement below had to wait for it.

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
            self._caption_box.sync()

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
        self._bars_active = False   # next run re-applies the active colour once
        # Reset bars to idle height
        if self._bar_ids:
            mid = CANVAS_H // 2
            for i, bid in enumerate(self._bar_ids):
                cx, half = _bar_geom(i, BAR_MIN_H)
                self._wave_canvas.coords(bid, cx, mid - half, cx, mid + half)
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
        if self._mode == "icon":
            if self._icon_entered == stamp:
                self._do_hide()
            return          # a NEWER badge owns the hook — never unhook that one
        # The badge this timer belonged to is gone (a new dictation started, or
        # the panel opened). Its keyboard hook must go with it: returning here
        # without unhooking is what let a space-dismiss outlive its badge and
        # then hide a live recording.
        self._unregister_space_dismiss()

    def _touch_panel_activity(self) -> None:
        self._panel_activity = time.time()

    def _idle_check_panel(self, stamp: float) -> None:
        if self._mode != "refinement" or getattr(self, "_panel_entered", 0) != stamp:
            return
        active = self._ai_busy or self._mic_recording or self._upgrading
        if not active:
            try:
                px, py = self.root.winfo_pointerx(), self.root.winfo_pointery()
                rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
                active = (rx <= px <= rx + self.root.winfo_width()
                          and ry <= py <= ry + self.root.winfo_height())
            except tk.TclError:
                pass
        if active:
            self._touch_panel_activity()
        remaining = PANEL_IDLE_DISMISS_SECS - (time.time() - self._panel_activity)
        if remaining <= 0:
            self._do_hide()
            return
        self.root.after(int(max(0.5, remaining) * 1000),
                        lambda s=stamp: self._idle_check_panel(s))

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
        if self._refine_started_at is None:
            self._refine_started_at = time.monotonic()
        self._last_shown = time.time()
        # Idle auto-dismiss, stamp-guarded like the icon badge so a stale
        # timer from an earlier panel session can never kill a newer one.
        self._panel_entered = self._last_shown
        self._panel_activity = self._last_shown
        self.root.after(int(PANEL_IDLE_DISMISS_SECS * 1000),
                        lambda s=self._panel_entered: self._idle_check_panel(s))
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
        # The panel has to stay on top for the same reason the pill does; the
        # re-assert never touches focus (SWP_NOACTIVATE), so the Ask box keeps it.
        self._assert_topmost()
        self._repaint_popup()
        self._start_keep_on_top()

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
        self._stop_keep_on_top()
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

    def refine_stats(self) -> Optional[dict]:
        """Measurements for the refine that is being applied right now, or None
        if no refinement ran this session. Read by app.py at the moment the
        result is actually inserted/replaced — a refine the user looked at and
        discarded saved them nothing and must not be counted."""
        if self._refine_started_at is None:
            return None
        return {
            "elapsed_seconds": max(0.0, time.monotonic() - self._refine_started_at),
            "prompt_words": int(self._refine_prompt_words),
            "prompt_spoken": bool(self._refine_prompt_spoken),
            "result_chars": len(self._current_result or ""),
            "source_words": len((self._original_text or "").split()),
        }

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
                # The instruction was SPOKEN, not typed — doing this by hand in
                # a chat window would have meant typing it (see stats).
                self._refine_prompt_spoken = True
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
        self._ask_box.sync()
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
        if not (self._original_text or "").strip():
            # No source text (e.g. the selection capture came back empty). Never
            # call the model with nothing — it replies "please provide the text".
            self._ai_status.configure(text="⚠  No text to refine. Select text, then press your refine hotkey")
            self._ai_status.pack(anchor="w")
            return
        if not self._ai_refiner or not self._ai_refiner.is_available:
            self._ai_status.configure(text="⚠  Set anthropic_api_key in config to enable AI")
            self._ai_status.pack(anchor="w")
            return
        if self._ai_busy:
            return
        if self._refine_started_at is None:
            self._refine_started_at = time.monotonic()
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
        if not (self._original_text or "").strip():
            # No source text captured — refining nothing makes the model reply
            # "please provide the text". Tell the user to select text instead.
            self._ai_status.configure(text="⚠  No text to refine. Select text, then press your refine hotkey")
            self._ai_status.pack(anchor="w")
            return
        if not self._ai_refiner or not self._ai_refiner.is_available:
            self._ai_status.configure(text="⚠  Set anthropic_api_key in config to enable AI")
            self._ai_status.pack(anchor="w")
            return
        if self._ai_busy:
            return
        if self._refine_started_at is None:
            self._refine_started_at = time.monotonic()
        self._refine_prompt_words = len(instruction.split())
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
        # Restart the idle countdown so the user gets the full window to read
        # the result even if the AI call took a while.
        self._touch_panel_activity()
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
            # Prefer the explicit interaction anchor supplied by app.py (caret
            # or focused-window point, not blindly the mouse). Fall back to the
            # target HWND only when no usable anchor was supplied.
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

        # Pick the monitor (multi-monitor aware — follows the focused text/
        # window anchor) and convert its Win32 work-area to tkinter logical
        # pixels. Keep the raw *physical* bounds too: the second-order guard
        # below re-checks the real landing spot against them.
        try:
            left_p, top_p, right_p, bottom_p = self._get_monitor_workarea(cx, cy)
            sx, sy = self._dpi_scale()
            left   = round(left_p   * sx)
            top    = round(top_p    * sy)
            right  = round(right_p  * sx)
            bottom = round(bottom_p * sy)
        except Exception:
            left_p, top_p = 0, 0
            right_p  = self.root.winfo_screenwidth()
            bottom_p = self.root.winfo_screenheight()
            left, top, sx, sy = 0, 0, 1.0, 1.0
            right, bottom = right_p, bottom_p

        if near_cursor and cx > 0 and cy > 0:
            gap = 28
            x   = round(cx * sx) - w // 2
            y_b = round(cy * sy) + gap
            y   = y_b if y_b + h <= bottom else round(cy * sy) - h - gap
        else:
            # Fixed, horizontally centred on the anchored monitor.
            # Vertical placement follows the user's popup_height preference so it
            # can sit out of the way of a chatbot's input box (low, default) or
            # up above the chat window (high).
            x = left + (right - left - w) // 2
            if self._popup_height == "high":
                y = top + 90
            elif self._popup_height == "medium":
                y = top + (bottom - top - h) // 2
            else:  # "low" (default) — hug the taskbar, clear of chat inputs
                y = bottom - h - 6

        # First-order guard: keep the whole popup inside the work-area in Tk
        # coordinates, so a stale width or an odd monitor origin can never push
        # the fixed popup off an edge (the cut-off refine panel).
        x = max(left, min(x, right - w))
        y = max(top, min(y, bottom - h))

        # Size counts as a move: the caption bar grows downward while the pill is
        # bottom-anchored, so a taller pill at the same y is still a blit.
        moved = f"{w}x{h}+{x}+{y}" != self._last_geometry
        self._last_geometry = f"{w}x{h}+{x}+{y}"
        self.root.geometry(f"+{x}+{y}")
        # Flush the position to the window BEFORE the caller deiconifies it, so it
        # never maps at the old/0,0 spot for a frame (top-left black-box flash).
        self.root.update_idletasks()

        # Second-order guard: on a mixed-DPI multi-monitor rig tkinter's logical
        # geometry can render the window off the monitor we targeted — so a popup
        # that is fully on-screen in Tk maths still lands cut off physically
        # (the user's "wrong place / cut off" bug, which the logical numbers
        # alone never showed). Measure where it PHYSICALLY landed and, if it
        # spills past the target monitor's physical work-area, nudge it back by
        # the measured overflow. Empirical, so it corrects any coordinate-space
        # mismatch without having to model the DPI topology.
        try:
            GA_ROOT = 2
            u32 = ctypes.windll.user32
            top_hwnd = u32.GetAncestor(self._popup_hwnd, GA_ROOT) or self._popup_hwnd
            pr = _RECT()
            if u32.GetWindowRect(top_hwnd, ctypes.byref(pr)):
                pw, ph = pr.right - pr.left, pr.bottom - pr.top
                nx = max(left_p, min(pr.left, right_p - pw))
                ny = max(top_p, min(pr.top, bottom_p - ph))
                ddx, ddy = nx - pr.left, ny - pr.top
                if ddx or ddy:
                    self.root.geometry(
                        f"+{x + round(ddx * sx)}+{y + round(ddy * sy)}")
                    self.root.update_idletasks()
                    self._log_pos_anomaly(near_cursor, w, h, x, y,
                                          (left_p, top_p, right_p, bottom_p),
                                          (pr.left, pr.top), (ddx, ddy))
                    moved = True
                    self._last_geometry = ""
        except Exception:
            pass

        # Moving or resizing a MAPPED overrideredirect window lets Windows blit
        # the old pixels into the new spot, which is the ghosted/half-drawn pill.
        # RDW_UPDATENOW only validates Tk's update region — Tk turns WM_PAINT into
        # a queued Expose and paints on a later mainloop spin — so the after(0)
        # twin is the one that actually drains the queue.
        if moved:
            try:
                mapped = bool(self.root.winfo_ismapped())
            except Exception:
                mapped = False
            if mapped:
                self._repaint_popup()
                try:
                    self.root.after(0, self._repaint_popup)
                except tk.TclError:
                    pass

    def _log_pos_anomaly(self, near_cursor, w, h, x, y, wa_phys, landed, delta):
        """Record only the anomalous placements — the ones the physical guard had
        to nudge back on-screen. Rare by construction, so this stays silent on
        healthy single-DPI setups and pinpoints a real coordinate mismatch (send
        me %TEMP%\\ftc_pos_debug.log if a popup ever still lands wrong)."""
        try:
            import os as _os, time as _t
            _dbg = _os.path.join(_os.environ.get("TEMP", "."), "ftc_pos_debug.log")
            with open(_dbg, "a", encoding="utf-8") as _f:
                _f.write(f"{_t.strftime('%H:%M:%S')} CORRECTED mode={self._mode} "
                         f"near={near_cursor} wh=({w},{h}) asked=({x},{y}) "
                         f"wa_phys={wa_phys} landed={landed} nudge={delta} "
                         f"screen=({self.root.winfo_screenwidth()},"
                         f"{self.root.winfo_screenheight()})\n")
        except Exception:
            pass
