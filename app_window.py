"""
FTC Whisper — Main application window.

Dashboard: Home / Hotkey / History tabs.
Dark theme with rounded-corner cards via Canvas.
"""

import bisect
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta
from typing import Callable, Optional
import ctypes

try:
    from app_icons import (get_app_icon, get_brand_icon,
                           get_monogram_icon, get_fallback_icon)
except Exception:
    get_app_icon = lambda *a, **k: None        # noqa: E731
    get_brand_icon = lambda *a, **k: None      # noqa: E731
    get_monogram_icon = lambda *a, **k: None   # noqa: E731
    get_fallback_icon = lambda *a, **k: None   # noqa: E731

# ── Dark colour palette ───────────────────────────────────────────────────────
C = {
    "bg":            "#0d0d0d",   # near-black window background
    "surface":       "#1a1a1a",   # card surface
    "surface_hover": "#242424",   # card hover / active
    "input_bg":      "#141414",   # entry fields
    "text":          "#ffffff",   # primary text
    "subtext":       "#777777",   # secondary / hint text
    "accent":        "#f39200",   # FTC orange
    "accent_hover":  "#e08200",   # darker orange
    "accent_dim":    "#3d2600",   # very muted orange (badge bg)
    "error":         "#ff5555",
    "success":       "#4ade80",
    "divider":       "#1f1f1f",   # hairline separator
    "border":        "#2d2d2d",   # card border
    "scrollbar":     "#2d2d2d",
}

WINDOW_W = 420
DASH_H   = 640

# Resizable-window bounds. Content reflows down to MIN_W; anything narrower
# would clip the impact cards and the hotkey pills.
MIN_W = 400
MIN_H = 520

# The account whose resizes become the shipped install default (pushed to the
# app_settings table; every fresh install reads it once at first sign-in).
SUPER_ADMIN_EMAIL = "ryan.murphy@ftc-ss.com"

# Impact card box height — must fit icon, caption, value and sub-label with
# the reference's breathing room; the layout draws against this constant.
_IMPACT_CARD_H = 148


def show_toast(root: tk.Misc, message: str, duration_ms: int = 5000) -> None:
    """Transient bottom-right notification that never takes focus.
    Must be called on the tkinter main thread."""
    toast = tk.Toplevel(root)
    toast.overrideredirect(True)
    toast.attributes("-topmost", True)
    toast.attributes("-alpha", 0.0)
    toast.configure(bg=C["accent"])  # 1px accent border via padding frame

    inner = tk.Frame(toast, bg=C["surface"])
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    tk.Label(
        inner, text=message,
        fg=C["text"], bg=C["surface"],
        font=("Segoe UI", 10), padx=18, pady=12,
    ).pack()

    toast.update_idletasks()
    w, h = toast.winfo_reqwidth(), toast.winfo_reqheight()
    # Bottom-right, above the taskbar
    x = toast.winfo_screenwidth() - w - 16
    y = toast.winfo_screenheight() - h - 60
    toast.geometry(f"{w}x{h}+{x}+{y}")

    # Borderless Toplevels get square corners by default, so the toast needs the
    # same DWM treatment as the floating popup or it reads as a different app.
    # Must run after update_idletasks()/geometry(): before the window is realised
    # GetAncestor(GA_ROOT) resolves to the wrong handle and the call is a silent
    # no-op that still returns S_OK. Imported lazily because popup.py pulls
    # _rr/RoundedButton from this module — a module-level import closes the cycle.
    toast.update_idletasks()
    try:
        from popup import _apply_popup_corners
        _apply_popup_corners(toast.winfo_id())
    except Exception:
        pass

    def _fade(alpha: float, step: float):
        try:
            alpha = max(0.0, min(1.0, alpha + step))
            toast.attributes("-alpha", alpha)
            if step > 0 and alpha < 1.0:
                toast.after(25, _fade, alpha, step)
            elif step < 0:
                if alpha > 0.0:
                    toast.after(25, _fade, alpha, step)
                else:
                    toast.destroy()
        except tk.TclError:
            pass  # root died mid-animation

    _fade(0.0, 0.1)
    toast.after(duration_ms, lambda: _fade(1.0, -0.1))
    toast.bind("<Button-1>", lambda _e: toast.destroy())


# ── Modern scrollbar ──────────────────────────────────────────────────────────

class ModernScrollbar(tk.Canvas):
    """Thin, rounded, light-grey scrollbar that syncs with a Canvas/Text yview.

    Replaces tk.Scrollbar (which renders as a chunky white/native box on
    Windows) with a clean grey thumb like the FTC Contacts scrollbars —
    clearly visible, comfortable width, no arrow buttons or native chrome.
    Drop-in: pass the scrolled widget's ``yview`` as ``command`` and set the
    widget's ``yscrollcommand`` to this scrollbar's ``set``."""

    WIDTH = 12
    THUMB = "#4a4a4a"        # light grey, clearly visible on the near-black bg
    THUMB_HOVER = "#5f5f5f"

    def __init__(self, parent, command, track=None, **kw):
        super().__init__(parent, width=self.WIDTH, bg=track or C["bg"],
                         highlightthickness=0, bd=0, takefocus=0, **kw)
        self._command = command      # the scrolled widget's yview
        self._first = 0.0
        self._last = 1.0
        self._hover = False
        self._drag_dy = 0
        # Keep three persistent canvas items instead of deleting/recreating the
        # thumb on every animated yview update.  At 60+ updates/second the old
        # implementation generated avoidable Tcl object churn.
        self._thumb_top = self.create_oval(0, 0, 0, 0, outline="", state="hidden")
        self._thumb_mid = self.create_rectangle(0, 0, 0, 0, outline="", state="hidden")
        self._thumb_bottom = self.create_oval(0, 0, 0, 0, outline="", state="hidden")
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)

    def set(self, first, last):
        # Called by the scrolled widget via yscrollcommand.
        self._first, self._last = float(first), float(last)
        self._redraw()

    def _set_hover(self, v):
        self._hover = v
        self._redraw()

    def _thumb_bounds(self):
        h = self.winfo_height()
        return self._first * h, self._last * h

    def _redraw(self):
        # Fully visible content → no thumb (nothing to scroll)
        if self._first <= 0.0 and self._last >= 1.0:
            for item in (self._thumb_top, self._thumb_mid, self._thumb_bottom):
                self.itemconfigure(item, state="hidden")
            return
        w = self.winfo_width()
        pad = 2
        y0, y1 = self._thumb_bounds()
        y0 += pad
        y1 -= pad
        if y1 - y0 < 16:                       # keep a grabbable minimum
            y1 = y0 + 16
        r = (w - 2 * pad) / 2
        color = self.THUMB_HOVER if self._hover else self.THUMB
        items = (self._thumb_top, self._thumb_mid, self._thumb_bottom)
        self.coords(self._thumb_top, pad, y0, w - pad, y0 + 2 * r)
        self.coords(self._thumb_mid, pad, y0 + r, w - pad, y1 - r)
        self.coords(self._thumb_bottom, pad, y1 - 2 * r, w - pad, y1)
        for item in items:
            self.itemconfigure(item, fill=color, state="normal")

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        r = max(0, min(r, (y1 - y0) / 2))
        self.create_oval(x0, y0, x1, y0 + 2 * r, outline="", **kw)
        self.create_oval(x0, y1 - 2 * r, x1, y1, outline="", **kw)
        self.create_rectangle(x0, y0 + r, x1, y1 - r, outline="", **kw)

    def _on_press(self, e):
        y0, y1 = self._thumb_bounds()
        if y0 <= e.y <= y1:
            self._drag_dy = e.y - y0            # grab offset within the thumb
        else:
            # Click on the track → center the thumb on the cursor and jump
            h = max(self.winfo_height(), 1)
            span = y1 - y0
            self._command("moveto", max(0.0, min(1.0, (e.y - span / 2) / h)))
            self._drag_dy = span / 2

    def _on_drag(self, e):
        h = max(self.winfo_height(), 1)
        self._command("moveto", max(0.0, min(1.0, (e.y - self._drag_dy) / h)))


# ── Pill toggle widget ────────────────────────────────────────────────────────

class TogglePill(tk.Frame):
    """Capsule-shaped boolean toggle. FTC orange when ON, border-grey when OFF."""
    W, H = 48, 28

    def __init__(self, parent, value: bool = False, command=None, **kw):
        bg = kw.pop("bg", C["surface"])
        super().__init__(parent, bg=bg, width=self.W, height=self.H)
        self._value = bool(value)
        self._cmd   = command
        self._cv    = tk.Canvas(
            self, width=self.W, height=self.H,
            bg=bg, highlightthickness=0, cursor="hand2",
        )
        self._cv.pack()
        self._cv.bind("<Button-1>", lambda _e: self.toggle())
        self.bind("<Button-1>",     lambda _e: self.toggle())
        self._draw()

    def _draw(self):
        self._cv.delete("all")
        track = C["accent"] if self._value else C["border"]
        r = self.H // 2
        # True pill: filled rect + two semicircle caps
        self._cv.create_rectangle(r, 0, self.W - r, self.H, fill=track, outline="")
        self._cv.create_oval(0, 0, self.H, self.H, fill=track, outline="")
        self._cv.create_oval(self.W - self.H, 0, self.W, self.H, fill=track, outline="")
        # Dot
        m = 3
        d = self.H - m * 2
        tx = self.W - m - d if self._value else m
        self._cv.create_oval(tx, m, tx + d, self.H - m, fill=C["text"], outline="")

    def get(self) -> bool:
        return self._value

    def set(self, v: bool):
        self._value = bool(v)
        self._draw()

    def toggle(self):
        self._value = not self._value
        self._draw()
        if self._cmd:
            self._cmd(self._value)


# ── Rounded card helper ───────────────────────────────────────────────────────

def _rr(canvas, x1, y1, x2, y2, r, **kw):
    """Draw a smooth rounded rectangle on a Canvas."""
    pts = (
        x1+r, y1,   x2-r, y1,   x2,   y1,
        x2,   y1+r, x2,   y2-r, x2,   y2,
        x2-r, y2,   x1+r, y2,   x1,   y2,
        x1,   y2-r, x1,   y1+r, x1,   y1,
    )
    return canvas.create_polygon(pts, smooth=True, **kw)


class RoundedButton(tk.Canvas):
    """A flat button with slightly rounded corners.

    tk's Label/Button are square-cornered, so buttons are drawn on a Canvas
    instead. Deliberately a near-drop-in for the old tk.Label buttons: the
    same call sites keep working because .configure(text=/bg=/fg=/cursor=) and
    .bind('<Button-1>'/'<Enter>'/'<Leave>') behave as before — `bg` maps to the
    rounded fill and triggers a redraw. Auto-sizes to its text.
    """

    def __init__(self, parent, text="", command=None, *,
                 fill=None, fg=None, font=("Segoe UI", 9),
                 radius=8, padx=12, pady=6, **kw):
        parent_bg = kw.pop("bg", None) or parent.cget("bg")
        super().__init__(parent, bg=parent_bg, highlightthickness=0, bd=0,
                         cursor=kw.pop("cursor", "hand2"), **kw)
        fam = font[0]
        size = font[1] if len(font) > 1 else 9
        weight = "bold" if (len(font) > 2 and font[2] == "bold") else "normal"
        self._font = tkfont.Font(family=fam, size=size, weight=weight)
        self._text = text
        self._fill = fill if fill is not None else C["surface_hover"]
        self._fg = fg if fg is not None else C["text"]
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self._command = command
        if command is not None:
            self.bind("<Button-1>", lambda _e: self._command())
        self._resize_and_draw()

    def _resize_and_draw(self):
        # Emoji/symbols (✉ 🎩 ✨ …) render taller than the font's nominal line
        # height, so sizing from linespace alone clips them. Measure the ACTUAL
        # rendered glyph box and use whichever is taller — nothing gets cut off,
        # and text stays vertically centred.
        probe = self.create_text(0, 0, text=self._text or " ",
                                  font=self._font, anchor="nw")
        bx1, by1, bx2, by2 = self.bbox(probe)
        self.delete(probe)
        line_h = self._font.metrics("linespace")
        w = max(self._font.measure(self._text), bx2 - bx1) + 2 * self._padx
        h = max(line_h, by2 - by1) + 2 * self._pady
        super().configure(width=w, height=h)
        self._draw(w, h)

    def _draw(self, w=None, h=None):
        if w is None:
            w = int(self["width"])
        if h is None:
            h = int(self["height"])
        self.delete("all")
        _rr(self, 1, 1, w - 1, h - 1, self._radius, fill=self._fill, outline="")
        self.create_text(w // 2, h // 2, text=self._text,
                         fill=self._fg, font=self._font)

    # Intercept the Label-style options the old call sites pass so a plain
    # .configure(bg=…, fg=…, text=…) keeps working (bg == the rounded fill).
    def configure(self, cnf=None, **kw):
        resize = redraw = False
        if "text" in kw:
            self._text = kw.pop("text"); resize = True
        for k in ("bg", "background"):
            if k in kw:
                self._fill = kw.pop(k); redraw = True
        for k in ("fg", "foreground"):
            if k in kw:
                self._fg = kw.pop(k); redraw = True
        if cnf or kw:
            super().configure(cnf, **kw)
        if resize:
            self._resize_and_draw()
        elif redraw:
            self._draw()
    config = configure


class AppWindow:
    _STATUS = {
        "idle":       ("● Ready",         C["success"]),
        "recording":  ("● Recording…",    "#ff5555"),
        "processing": ("⚙ Transcribing…", C["accent"]),
    }

    def __init__(
        self,
        auth,
        on_authenticated: Callable,
        on_sign_out: Callable,
        on_open_config: Callable,
        on_quit: Callable,
        on_hotkey_change: Callable,
        on_refine_hotkey_change: Callable = None,
        on_settings_change: Callable = None,
        on_sign_in: Callable = None,
        db=None,
        stats=None,
        hotkey: str = "alt+v",
        refine_hotkey: str = "alt+r",
        config=None,
        get_input_devices: Callable = None,
        recorder=None,
        transcriber=None,
        version: str = "",
    ):
        self._version                 = version
        self._auth                    = auth
        self._on_authenticated        = on_authenticated
        self._on_sign_out             = on_sign_out
        self._open_config_cb          = on_open_config
        self._on_quit                 = on_quit
        self._on_hotkey_change        = on_hotkey_change
        self._on_refine_hotkey_change = on_refine_hotkey_change
        self._on_settings_change      = on_settings_change
        self._on_sign_in              = on_sign_in
        self._db                      = db
        self._stats                   = stats
        self._config                  = config
        self._get_input_devices       = get_input_devices
        self._recorder                = recorder
        self._transcriber             = transcriber
        self._hotkey                  = hotkey.upper()
        self._refine_hotkey           = refine_hotkey.upper()
        self._root: Optional[tk.Tk] = None

        # One coalesced animation state per scroll canvas. Wheel input updates a
        # pixel target; a single scheduled frame eases toward it.
        self._scroll_states = {}
        self._scrollregion_jobs = {}

        # Per-account window sizing. _applied_size is the last size WE set
        # programmatically, so its Configure echo is never mistaken for a user
        # resize. _dash_visible gates persistence: login-screen resizes are
        # transient and never saved.
        self._applied_size = None
        self._dash_visible = False
        self._win_save_job = None
        self._install_default_checked = False

        # History is cache-first and refreshed without clearing the visible rows.
        self._hist_all = []
        self._history_loading = False
        self._history_last_fetch_started = 0.0
        self._history_dirty = True
        self._history_pending_render = False
        self._history_rendered_once = False
        self._history_fingerprint = None

        # Hotkey recorder state
        self._recording_hotkey        = False
        self._pending_hotkey: Optional[str] = None
        self._recording_refine_hotkey = False
        self._pending_refine_hotkey: Optional[str] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> None:
        # Tell Windows this is its own app (not python.exe) so the taskbar uses
        # OUR icon, not the interpreter's, when running from source.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FTC.Whisper")
        except Exception:
            pass

        self._root = tk.Tk()
        self._root.withdraw()  # hide before Windows has a chance to render the default blank window
        self._root.title("FTC Whisper")
        # Window / taskbar icon — the FTC swirl (logo.ico). Setting it at runtime is
        # what actually changes the visible title-bar + taskbar icon; the exe's
        # embedded icon alone doesn't update a running window.
        try:
            from logo_cache import get_icon_path
            _ico = get_icon_path()
            if _ico:
                self._root.iconbitmap(default=_ico)
        except Exception:
            pass
        self._root.configure(bg=C["bg"])
        # Fully resizable: edges, corners and the maximize box all work. The
        # layout reflows to fill whatever size the user drags to, and the size
        # is remembered per signed-in account (see _on_root_configure).
        self._root.resizable(True, True)
        self._root.minsize(MIN_W, MIN_H)
        self._root.protocol("WM_DELETE_WINDOW", self._hide)
        self._root.bind("<Configure>", self._on_root_configure, add="+")

        self._apply_dark_titlebar()

        try:
            self._build_header()

            self._dash_frame = tk.Frame(self._root, bg=C["bg"])
            self._build_dashboard(self._dash_frame)

            self._login_frame = tk.Frame(self._root, bg=C["bg"])
            self._build_embedded_login()

            # Impact cards follow the stats store: refresh after every
            # dictation/sync (marshalled to the tk thread) and at midnight.
            if self._stats is not None and not getattr(self, "_stats_listener_added", False):
                self._stats.add_listener(
                    lambda: self._ui_after(0, self._refresh_impact))
                self._stats_listener_added = True
            self._refresh_impact()
            self._schedule_midnight_refresh()

            if self._auth.is_authenticated:
                self._switch_to_dashboard()
                self._fire_authenticated()
            else:
                self._switch_to_login()
                # A saved session that exists but didn't restore means the startup
                # attempt failed on network/timeout — common when the logon task
                # launches before Wi-Fi is up after a reboot. Retry in the
                # background and promote to the dashboard once it succeeds, so the
                # user isn't forced to sign in again just because the network wasn't
                # ready yet. (Definitive auth failures delete the file, so the loop
                # stops and correctly leaves the login screen up.)
                if self._auth.has_saved_session():
                    self._start_session_restore_retry()

        except Exception:
            import traceback
            tb = traceback.format_exc()
            print(f"[AppWindow] Startup error:\n{tb}")
            # Show the error in-window instead of leaving a blank window
            self._root.geometry(f"{WINDOW_W}x480+100+100")
            err_frame = tk.Frame(self._root, bg=C["bg"])
            err_frame.pack(fill="both", expand=True, padx=16, pady=16)
            tk.Label(
                err_frame, text="FTC Whisper — Startup Error",
                fg=C["error"], bg=C["bg"],
                font=("Segoe UI", 12, "bold"), anchor="w",
            ).pack(fill="x", pady=(0, 8))
            tk.Label(
                err_frame, text=tb,
                fg=C["subtext"], bg=C["bg"],
                font=("Courier New", 8), anchor="w",
                justify="left", wraplength=388,
            ).pack(fill="both", expand=True)

        # Force all pending geometry/paint then show the fully-built window.
        try:
            self._root.update()
        except Exception:
            pass
        self._root.deiconify()
        # Re-apply after the window is mapped — DWM caption attributes set while
        # the window was withdrawn don't always stick, leaving a white title bar.
        self._apply_dark_titlebar()

        # Pre-draw cached History shortly after first paint so its first click is
        # only a frame raise, not a network request plus widget construction.
        self._root.after(75, self._prime_history)

        self._root.mainloop()
        # Destroy after mainloop exits (quit() was called on sign-out)
        try:
            self._root.destroy()
        except Exception:
            pass
        self._root = None

    def show(self) -> None:
        self._ui_after(0, self._do_show)

    def _do_show(self) -> None:
        self._root.deiconify()
        try:
            u32 = ctypes.windll.user32
            # FindWindowW by title is more reliable than winfo_id() which can
            # return a child HWND rather than the actual top-level window handle.
            hwnd = u32.FindWindowW(None, "FTC Whisper")
            if not hwnd:
                hwnd = u32.GetParent(self._root.winfo_id()) or self._root.winfo_id()
            HWND_TOPMOST   = -1
            HWND_NOTOPMOST = -2
            SWP_NOMOVE     = 0x0002
            SWP_NOSIZE     = 0x0001
            u32.ShowWindow(hwnd, 9)   # SW_RESTORE — un-minimise first
            u32.ShowWindow(hwnd, 5)   # SW_SHOW    — un-hide if withdrawn to tray
            # Flash TOPMOST→NOTOPMOST: bypasses Windows focus-steal prevention.
            # Must happen after ShowWindow so the window is visible when raised.
            u32.SetWindowPos(hwnd, HWND_TOPMOST,   0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            u32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            u32.SetForegroundWindow(hwnd)
        except Exception:
            self._root.lift()
            self._root.focus_force()

    def update_status(self, state: str) -> None:
        if self._root and hasattr(self, "_status_lbl"):
            text, color = self._STATUS.get(state, ("● Ready", C["success"]))
            self._ui_after(0, lambda: self._status_lbl.configure(text=text, fg=color))

    def _ui_after(self, delay_ms: int, callback, *args) -> None:
        """Schedule a UI callback from a background thread. Drops the update
        silently if the mainloop has already exited (RuntimeError) or the root
        was destroyed (tk.TclError) — e.g. app closed while a daemon thread
        was still in flight; otherwise the excepthook logs it as a crash."""
        root = self._root
        if not root:
            return
        try:
            root.after(delay_ms, callback, *args)
        except (tk.TclError, RuntimeError):
            pass

    # ── Windows dark title bar ────────────────────────────────────────────────

    # Title-bar colours (Windows 11). Caption is a grey that matches the app's
    # card surface instead of the default white; text stays white.
    _TITLEBAR_GREY = "#1a1a1a"

    @staticmethod
    def _colorref(hex_color: str) -> int:
        """#RRGGBB → Win32 COLORREF (0x00BBGGRR)."""
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (b << 16) | (g << 8) | r

    def _apply_dark_titlebar(self) -> None:
        try:
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            DWMWA_CAPTION_COLOR = 35   # Windows 11 build 22000+
            DWMWA_TEXT_COLOR = 36
            u32 = ctypes.windll.user32
            # FindWindowW by title returns the real top-level HWND; winfo_id()
            # can be a child HWND that DWM caption attributes don't apply to.
            hwnd = u32.FindWindowW(None, "FTC Whisper")
            if not hwnd:
                hwnd = u32.GetParent(self._root.winfo_id()) or self._root.winfo_id()
            dwm = ctypes.windll.dwmapi

            def _set(attr, value):
                dwm.DwmSetWindowAttribute(
                    hwnd, attr,
                    ctypes.byref(ctypes.c_int(value)), ctypes.sizeof(ctypes.c_int),
                )

            _set(DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
            # Explicit grey caption + white text (falls through harmlessly on
            # Windows 10, which doesn't support these two attributes).
            _set(DWMWA_CAPTION_COLOR, self._colorref(self._TITLEBAR_GREY))
            _set(DWMWA_TEXT_COLOR, self._colorref("#ffffff"))

            # WS_EX_COMPOSITED: Windows double-buffers the whole child-window
            # tree, painting each frame in one atomic pass. This is what stops
            # the scroll/resize tearing where embedded card Frames left ghost
            # copies of themselves (duplicated rows, stray floating labels) on
            # the Settings and Hotkey tabs.
            GWL_EXSTYLE = -20
            WS_EX_COMPOSITED = 0x02000000
            style = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not (style & WS_EX_COMPOSITED):
                u32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_COMPOSITED)
        except Exception:
            pass

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        # Wrap header + divider in a container so it can be hidden during login
        self._header_outer = tk.Frame(self._root, bg=C["bg"])

        header = tk.Frame(self._header_outer, bg=C["bg"], pady=20)
        header.pack(fill="x")

        # Gear icon — top right of header
        self._gear_btn = tk.Label(
            header, text="⚙",
            fg=C["subtext"], bg=C["bg"],
            font=("Segoe UI", 15), cursor="hand2", padx=12,
        )
        self._gear_btn.pack(side="right", anchor="ne")
        self._gear_btn.bind("<Button-1>", lambda _e: self._switch_dash_tab("settings"))
        self._gear_btn.bind("<Enter>",    lambda _e: self._gear_btn.configure(fg=C["text"]))
        self._gear_btn.bind("<Leave>",    lambda _e: self._gear_btn.configure(
            fg=C["accent"] if getattr(self, "_current_tab", "") == "settings" else C["subtext"]))

        from logo_cache import get_logo_photo
        self._logo_photo = get_logo_photo(self._root, C["bg"], max_w=180, max_h=60)

        if self._logo_photo:
            tk.Label(header, image=self._logo_photo, bg=C["bg"]).pack()
        else:
            tk.Label(
                header, text="FTC Whisper",
                fg=C["accent"], bg=C["bg"],
                font=("Segoe UI", 22, "bold"),
            ).pack()

        # Hairline divider — inside container so it hides with the header
        tk.Frame(self._header_outer, bg=C["divider"], height=1).pack(fill="x")

    # ── Dashboard shell ───────────────────────────────────────────────────────

    def _build_dashboard(self, parent: tk.Frame) -> None:
        # Tab bar with underline indicator
        tab_bar = tk.Frame(parent, bg=C["bg"])
        tab_bar.pack(fill="x", padx=20, pady=(14, 0))

        self._dash_tabs = {}
        self._tab_indicators = {}

        for name, label in [("home", "Home"), ("hotkey", "Hotkey"), ("history", "History")]:
            col = tk.Frame(tab_bar, bg=C["bg"])
            col.pack(side="left", expand=True, fill="x")

            btn = tk.Label(
                col, text=label,
                fg=C["subtext"], bg=C["bg"],
                font=("Segoe UI", 10), pady=8, cursor="hand2",
            )
            btn.pack(fill="x")
            btn.bind("<Button-1>", lambda _e, n=name: self._switch_dash_tab(n))
            btn.bind("<Enter>",    lambda _e, b=btn: b.configure(fg=C["text"]) if b.cget("fg") != C["accent"] else None)
            btn.bind("<Leave>",    lambda _e, b=btn, n=name: b.configure(fg=C["accent"] if self._dash_tabs.get(n) and b.cget("fg") == C["text"] else b.cget("fg")))

            ind = tk.Frame(col, bg=C["bg"], height=2)
            ind.pack(fill="x")

            self._dash_tabs[name] = btn
            self._tab_indicators[name] = ind

        tk.Frame(parent, bg=C["divider"], height=1).pack(fill="x", padx=0)

        # Content area — all tab frames stacked in same grid cell, tkraise() to switch
        self._dash_content = tk.Frame(parent, bg=C["bg"])
        self._dash_content.pack(fill="both", expand=True, pady=(10, 0))
        self._dash_content.grid_rowconfigure(0, weight=1)
        self._dash_content.grid_columnconfigure(0, weight=1)

        self._home_frame     = tk.Frame(self._dash_content, bg=C["bg"])
        self._hotkey_frame   = tk.Frame(self._dash_content, bg=C["bg"])
        self._history_frame  = tk.Frame(self._dash_content, bg=C["bg"])
        self._settings_frame = tk.Frame(self._dash_content, bg=C["bg"])

        for f in (self._home_frame, self._hotkey_frame,
                  self._history_frame, self._settings_frame):
            f.grid(row=0, column=0, sticky="nsew")

        self._build_home_tab(self._home_frame)
        self._build_hotkey_tab(self._hotkey_frame)
        self._build_history_tab(self._history_frame)
        self._build_settings_tab(self._settings_frame)

        # Route wheel input exactly once. The previous local + bind_all Hotkey
        # bindings handled the same event twice when the pointer was over Canvas.
        self._root.bind_all("<MouseWheel>", self._route_mousewheel)

        if self._db is not None and hasattr(self._db, "add_history_listener"):
            try:
                self._db.add_history_listener(
                    lambda items: self._ui_after(
                        0, self._on_history_cache_changed, items))
            except Exception:
                pass

        # Footer
        footer = tk.Frame(parent, bg=C["bg"], padx=24, pady=10)
        footer.pack(fill="x", side="bottom")

        email = self._auth.user_email or ""
        self._email_display = tk.Label(
            footer, text=email if email else "Not signed in",
            fg=C["subtext"], bg=C["bg"],
            font=("Segoe UI", 9), anchor="w",
        )
        self._email_display.pack(side="left", fill="x", expand=True)

        self._ghost_btn(footer, "Quit", self._do_quit).pack(side="right", padx=(8, 0))
        self._sign_btn = self._ghost_btn(footer, "Sign Out", self._do_sign_action)
        self._sign_btn.pack(side="right")

        tk.Frame(parent, bg=C["divider"], height=1).pack(fill="x", before=footer)

        self._switch_dash_tab("home")

    def _switch_dash_tab(self, name: str) -> None:
        previous = getattr(self, "_current_tab", None)
        self._current_tab = name

        tab_frames = {
            "home": self._home_frame,
            "hotkey": self._hotkey_frame,
            "history": self._history_frame,
            "settings": self._settings_frame,
        }

        # Raise the active frame — no pack/unpack, so no layout flash
        if name in tab_frames:
            tab_frames[name].tkraise()

        for n in tab_frames:
            if n in self._dash_tabs:
                active = (n == name)
                self._dash_tabs[n].configure(fg=C["accent"] if active else C["subtext"])
                self._tab_indicators[n].configure(bg=C["accent"] if active else C["bg"])

        # Gear icon highlight
        is_settings = (name == "settings")
        self._gear_btn.configure(fg=C["accent"] if is_settings else C["subtext"])

        # History is stale-while-revalidate: its already-drawn cache remains on
        # screen, and a repeat click on the active tab is intentionally a no-op.
        if name == "history":
            if self._history_pending_render:
                self._history_pending_render = False
                self._root.after(16, self._render_history)
            if previous != "history":
                self._load_history()
        elif name == "settings":
            # The session can finish restoring after these widgets were built.
            self._apply_auth_ui()
            if hasattr(self, "_update_check_btn") and hasattr(self, "_do_update_check"):
                # Don't clobber an in-flight check — resetting the label here
                # both swallowed the pending result and re-armed the button for
                # overlapping checks.
                if self._update_check_btn.cget("text") != "Checking...":
                    self._update_check_btn.configure(
                        text="Check for Updates", fg=C["accent"], cursor="hand2")
                    self._update_check_btn.bind("<Button-1>", self._do_update_check)

    def _build_embedded_login(self) -> None:
        from login_window import LoginWindow

        def _on_success(auth):
            self._switch_to_dashboard()
            self._fire_authenticated()
            if self._on_sign_in:
                threading.Thread(target=self._on_sign_in, args=(auth,), daemon=True).start()

        self._login_ui = LoginWindow(self._auth, on_success=_on_success, on_cancel=self._do_quit)
        self._login_ui.embed(self._login_frame)

    def _switch_to_login(self) -> None:
        self._dash_visible = False
        self._header_outer.pack_forget()
        self._dash_frame.pack_forget()
        self._login_frame.pack(fill="both", expand=True)
        self._resize(WINDOW_W, 560)
        if hasattr(self, "_login_ui"):
            self._login_ui.reset()

    def _switch_to_dashboard(self) -> None:
        self._login_frame.pack_forget()
        self._header_outer.pack(fill="x")
        self._show_dashboard()

    def _show_dashboard(self) -> None:
        self._dash_frame.pack(fill="both", expand=True)
        self._resize(*self._saved_dash_size())
        self._dash_visible = True
        # Fresh installs read the super-admin default size once (never again
        # after any size exists locally).
        self._maybe_fetch_install_default()
        # Session restore happens in a background thread, so labels populated
        # while the dashboard was built may contain the pre-restore state.
        self._apply_auth_ui()
        # Cards may be stale after a missed midnight (laptop asleep) or an
        # account switch — recompute whenever the dashboard is shown.
        self._refresh_impact()

    # ── Per-account window sizing ─────────────────────────────────────────────

    def _account_size_key(self) -> str:
        email = (getattr(self._auth, "user_email", "") or "").strip().lower()
        return email or "_default"

    @staticmethod
    def _parse_size(raw) -> Optional[tuple]:
        """'WxH' string to a sane (w, h) tuple, or None."""
        try:
            w_s, h_s = str(raw).lower().split("x")
            w, h = int(w_s), int(h_s)
        except (ValueError, AttributeError):
            return None
        if MIN_W <= w <= 5120 and MIN_H <= h <= 3200:
            return (w, h)
        return None

    def _saved_dash_size(self) -> tuple:
        """Resolve the dashboard size: this account's saved size, else the
        install default, else the built-in default. Clamped to the screen."""
        sizes = getattr(self._config, "window_sizes", None) if self._config else None
        size = None
        if isinstance(sizes, dict):
            size = (self._parse_size(sizes.get(self._account_size_key()))
                    or self._parse_size(sizes.get("_default")))
        w, h = size or (WINDOW_W, DASH_H)
        try:
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            w = min(w, sw)
            h = min(h, sh - 40)
        except tk.TclError:
            pass
        return max(w, MIN_W), max(h, MIN_H)

    def _on_root_configure(self, event) -> None:
        """Debounced per-account size save. The root's name is in every
        descendant's bindtags, so filter to the toplevel's own events."""
        if event.widget is not self._root or not self._dash_visible:
            return
        if (event.width, event.height) == self._applied_size:
            return
        if self._win_save_job is not None:
            try:
                self._root.after_cancel(self._win_save_job)
            except tk.TclError:
                pass
        self._win_save_job = self._root.after(600, self._persist_window_size)

    def _persist_window_size(self) -> None:
        self._win_save_job = None
        if not self._root or not self._dash_visible or not self._config:
            return
        try:
            # A maximised window is a state, not a chosen size: restoring the
            # saved WxH must bring back the pre-maximise geometry.
            if self._root.state() == "zoomed":
                return
            w, h = self._root.winfo_width(), self._root.winfo_height()
        except tk.TclError:
            return
        if w < MIN_W or h < MIN_H:
            return
        key = self._account_size_key()
        sizes = getattr(self._config, "window_sizes", None)
        sizes = dict(sizes) if isinstance(sizes, dict) else {}
        value = f"{w}x{h}"
        if sizes.get(key) == value:
            return
        sizes[key] = value
        self._config.window_sizes = sizes
        try:
            self._config.save_async()
        except Exception as e:
            print(f"[AppWindow] Window size save failed: {e}")
        # Baseline moves to what the user chose, so only further drags re-save.
        self._applied_size = (w, h)
        # The super-admin account's size becomes the fleet-wide install
        # default. Existing installs are untouched (they only read it once,
        # at first sign-in with no local size).
        if key == SUPER_ADMIN_EMAIL and self._db is not None \
                and hasattr(self._db, "set_app_setting"):
            try:
                self._db.set_app_setting("default_window_size", value)
            except Exception as e:
                print(f"[AppWindow] Default size push failed: {e}")

    def _maybe_fetch_install_default(self) -> None:
        """Fresh install only: pull the super-admin's default window size and
        lock it in as this install's starting size. Runs at most once per
        session and never once any local size exists."""
        if self._install_default_checked or not self._config:
            return
        sizes = getattr(self._config, "window_sizes", None)
        if isinstance(sizes, dict) and sizes:
            self._install_default_checked = True
            return
        if self._db is None or not hasattr(self._db, "fetch_app_setting"):
            return
        self._install_default_checked = True

        def _fetch():
            try:
                raw = self._db.fetch_app_setting("default_window_size")
            except Exception:
                raw = ""
            size = self._parse_size(raw)
            if size:
                self._ui_after(0, self._adopt_install_default, size)

        threading.Thread(target=_fetch, daemon=True,
                         name="install-default-size").start()

    def _adopt_install_default(self, size: tuple) -> None:
        """Main thread: store the fetched default; apply it only if the user
        hasn't resized or been given a size in the meantime."""
        if not self._config:
            return
        sizes = getattr(self._config, "window_sizes", None)
        if isinstance(sizes, dict) and sizes:
            return  # a size appeared while fetching — never overwrite it
        self._config.window_sizes = {"_default": f"{size[0]}x{size[1]}"}
        try:
            self._config.save_async()
        except Exception:
            pass
        if (self._dash_visible and self._win_save_job is None
                and self._applied_size == (WINDOW_W, DASH_H)):
            self._resize(*self._saved_dash_size())

    # ── Home tab ──────────────────────────────────────────────────────────────

    def _build_home_tab(self, parent: tk.Frame) -> None:
        # Status card
        sc = self._card(parent, margin=(0, 8))
        self._status_lbl = tk.Label(
            sc, text="● Ready",
            fg=C["success"], bg=C["surface"],
            font=("Segoe UI", 17, "bold"), anchor="w",
        )
        self._status_lbl.pack(fill="x")

        tk.Frame(sc, bg=C["border"], height=1).pack(fill="x", pady=(10, 10))

        hint_row = tk.Frame(sc, bg=C["surface"])
        hint_row.pack(fill="x")

        # Hotkey pill
        pill_bg = tk.Frame(hint_row, bg=C["accent_dim"], padx=8, pady=3)
        pill_bg.pack(side="left")
        hint_text = self._hotkey if self._hotkey else "—"
        self._home_hotkey_lbl = tk.Label(
            pill_bg, text=hint_text,
            fg=C["accent"], bg=C["accent_dim"],
            font=("Segoe UI", 10, "bold"),
        )
        self._home_hotkey_lbl.pack()

        _cur_mode = getattr(self._config, "mode", "hold") if self._config else "hold"
        _hint_text = " hold to dictate" if _cur_mode != "toggle" else " press to start/stop"
        self._home_mode_hint_lbl = tk.Label(
            hint_row, text=_hint_text,
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10),
        )
        self._home_mode_hint_lbl.pack(side="left")

        # Refine hotkey pill
        refine_hint_row = tk.Frame(sc, bg=C["surface"])
        refine_hint_row.pack(fill="x", pady=(4, 0))

        refine_pill_bg = tk.Frame(refine_hint_row, bg=C["accent_dim"], padx=8, pady=3)
        refine_pill_bg.pack(side="left")
        refine_hint_text = self._refine_hotkey if self._refine_hotkey else "—"
        self._home_refine_hotkey_lbl = tk.Label(
            refine_pill_bg, text=refine_hint_text,
            fg=C["accent"], bg=C["accent_dim"],
            font=("Segoe UI", 10, "bold"),
        )
        self._home_refine_hotkey_lbl.pack()

        tk.Label(
            refine_hint_row, text=" refine selection with AI",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10),
        ).pack(side="left")

        # Your impact — live per-account stats (replaces the old instructions
        # card so Home still fits on one page without scrolling)
        self._build_impact_section(parent)

    # ── Your impact section ───────────────────────────────────────────────────

    def _build_impact_section(self, parent: tk.Frame) -> None:
        tk.Label(
            parent, text="Your impact",
            fg=C["text"], bg=C["bg"],
            font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(fill="x", padx=20, pady=(10, 8))

        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x", padx=20)
        for i in range(3):
            row.grid_columnconfigure(i, weight=1, uniform="impact")
        row.grid_rowconfigure(0, minsize=_IMPACT_CARD_H)

        self._impact_font_value = tkfont.Font(family="Segoe UI", size=18, weight="bold")
        self._impact_font_unit  = tkfont.Font(family="Segoe UI", size=10)

        self._impact_cards = {}
        specs = [
            ("time",   "TIME SAVED",      self._draw_icon_clock),
            ("speed",  "DICTATION SPEED", self._draw_icon_bolt),
            ("streak", "DAY STREAK",      self._draw_icon_flame),
        ]
        for i, (key, label, icon_fn) in enumerate(specs):
            cv = tk.Canvas(row, bg=C["bg"], highlightthickness=0, bd=0,
                           height=_IMPACT_CARD_H, width=120)
            cv.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            self._impact_cards[key] = {
                "cv": cv, "icon": icon_fn, "label": label,
                "value": "", "unit": "", "sub": "",
            }
            cv.bind("<Configure>", lambda _e, k=key: self._layout_impact_card(k))

        # The speed card is the product constant, not a measurement.
        self._set_impact_card("speed", "160", "wpm", "4× faster than typing")

        # Today bar
        bar = self._card(parent, inner_pad=(14, 10), margin=(8, 0))
        brow = tk.Frame(bar, bg=C["surface"])
        brow.pack(fill="x")
        icv = tk.Canvas(brow, bg=C["surface"], highlightthickness=0, bd=0,
                        width=18, height=18)
        icv.pack(side="left")
        _rr(icv, 3, 1, 15, 17, 3, fill=C["surface"], outline=C["subtext"], width=1.4)
        icv.create_line(6, 7, 12, 7, fill=C["subtext"], width=1.4)
        icv.create_line(6, 11, 12, 11, fill=C["subtext"], width=1.4)
        tk.Label(
            brow, text="Today", fg=C["text"], bg=C["surface"],
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(9, 0))
        self._impact_today_lbl = tk.Label(
            brow, text="·  0 words dictated",
            fg=C["subtext"], bg=C["surface"], font=("Segoe UI", 10),
        )
        self._impact_today_lbl.pack(side="left", padx=(6, 0))

        self._refresh_impact()

    def _set_impact_card(self, key: str, value: str, unit: str, sub: str) -> None:
        card = self._impact_cards.get(key)
        if not card:
            return
        card["value"], card["unit"], card["sub"] = value, unit, sub
        self._layout_impact_card(key)

    def _layout_impact_card(self, key: str) -> None:
        card = self._impact_cards.get(key)
        if not card:
            return
        cv = card["cv"]
        w = cv.winfo_width()
        h = _IMPACT_CARD_H
        if w < 40:
            return
        cv.delete("all")
        _rr(cv, 0, 0, w - 1, h - 1, 12, fill=C["surface"], outline=C["border"])
        cx = w // 2
        card["icon"](cv, cx, 32)
        # tkinter has no letter-spacing — hair spaces between characters give
        # the reference's tracked-out caption look.
        cv.create_text(cx, 66, text=" ".join(card["label"]),
                       fill=C["subtext"], font=("Segoe UI", 7, "bold"))
        # Value + unit centred as a pair, sharing one text baseline.
        # The figure is the card's focal point: wide gap above it, and it sits
        # tight to the description below.
        vw = self._impact_font_value.measure(card["value"])
        uw = self._impact_font_unit.measure(card["unit"]) if card["unit"] else 0
        gap = 5 if card["unit"] else 0
        x0 = cx - (vw + gap + uw) // 2
        # anchor="sw" pins the bounding-box bottom, and that box includes the
        # font's descender. The 18pt figure descends 6px and the 10pt unit only
        # 3px, so pinning both to the same y left "hrs"/"wpm"/"days" sitting 3px
        # low. Offset each by its own descent so the glyphs share a baseline.
        baseline = 110 - self._impact_font_value.metrics("descent")
        cv.create_text(x0, baseline + self._impact_font_value.metrics("descent"),
                       text=card["value"], fill=C["text"],
                       font=self._impact_font_value, anchor="sw")
        if card["unit"]:
            cv.create_text(x0 + vw + gap,
                           baseline + self._impact_font_unit.metrics("descent"),
                           text=card["unit"], fill=C["subtext"],
                           font=self._impact_font_unit, anchor="sw")
        cv.create_text(cx, 127, text=card["sub"], fill=C["subtext"],
                       font=("Segoe UI", 8))

    @staticmethod
    def _draw_icon_clock(cv, cx, cy):
        r = 11
        cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                       outline=C["accent"], width=2)
        cv.create_line(cx, cy, cx, cy - 6, fill=C["accent"], width=2,
                       capstyle="round")
        cv.create_line(cx, cy, cx + 5, cy + 2.5, fill=C["accent"], width=2,
                       capstyle="round")

    @staticmethod
    def _draw_icon_bolt(cv, cx, cy):
        pts = [(2.5, -12), (-7, 1.5), (-1, 1.5), (-2.5, 12), (7, -1.5), (1, -1.5)]
        cv.create_polygon([(cx + dx, cy + dy) for dx, dy in pts],
                          fill=C["success"], outline="")

    @staticmethod
    def _draw_icon_flame(cv, cx, cy):
        """Filled teardrop flame with a lick curling off the left, matching the
        reference. Filled reads far better than a stroke at this size."""
        outer = [
            (0.5, -12),      # tip
            (4.5, -6.5),
            (7, -1),
            (7.5, 4),
            (4.5, 9.5),
            (0, 11.5),
            (-4.5, 9.5),
            (-7.5, 4.5),
            (-7, -0.5),
            (-4, -4),
            (-2.5, -1),      # inner notch — the flame's curl
            (-1.5, -5.5),
        ]
        cv.create_polygon([(cx + dx, cy + dy) for dx, dy in outer],
                          fill=C["accent"], outline="", smooth=1)
        # Inner cut-out gives the two-tone flame depth without a second colour.
        inner = [(0.5, 1), (3.5, 4.5), (2.5, 8.5), (-0.5, 10),
                 (-3.5, 8), (-3, 4)]
        cv.create_polygon([(cx + dx, cy + dy) for dx, dy in inner],
                          fill=C["surface"], outline="", smooth=1)

    def _refresh_impact(self) -> None:
        """Recompute the impact cards from the stats store. Main thread only —
        background callers must come through _ui_after."""
        if not getattr(self, "_stats", None) or not hasattr(self, "_impact_cards"):
            return
        try:
            snap = self._stats.snapshot()
        except Exception as e:
            print(f"[AppWindow] Impact refresh failed: {e}")
            return

        m = snap["saved_minutes"]
        if m < 1:
            v, u, s = "< 1", "min", "A tiny moment"
        elif m < 10:
            v, u, s = str(int(m)), "min", "Every word counts"
        elif m < 60:
            v, u, s = str(int(m)), "min", "Adding up nicely"
        elif m < 600:
            v, u, s = f"{m / 60:.1f}", "hrs", "Real time back"
        else:
            v, u, s = str(int(round(m / 60))), "hrs", "Real time back"
        self._set_impact_card("time", v, u, s)

        n = snap["streak_days"]
        if n == 0:
            sub = "Dictate today to begin"
        elif snap["streak_active_today"]:
            sub = "Keep it going"
        else:
            sub = "Dictate to keep it"
        self._set_impact_card("streak", str(n), "day" if n == 1 else "days", sub)

        tw = snap["today_words"]
        if hasattr(self, "_impact_today_lbl"):
            self._impact_today_lbl.configure(
                text=f"·  {tw:,} word{'' if tw == 1 else 's'} dictated")

    def _schedule_midnight_refresh(self) -> None:
        """Roll the cards over at local midnight so Today resets and the
        streak flips without a restart."""
        root = self._root
        if not root:
            return
        now = datetime.now()
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5,
                                                microsecond=0)
        ms = max(1000, int((nxt - now).total_seconds() * 1000))

        def _roll():
            self._refresh_impact()
            self._schedule_midnight_refresh()

        try:
            root.after(ms, _roll)
        except Exception:
            pass

    # ── Hotkey tab ────────────────────────────────────────────────────────────

    def _build_hotkey_tab(self, parent: tk.Frame) -> None:
        # Scrollable container
        self._hk_cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0,
                                yscrollincrement=1)
        _hk_cv = self._hk_cv
        _hk_sb = ModernScrollbar(
            parent, command=lambda *a: self._scrollbar_command(_hk_cv, *a))
        _hk_cv.configure(yscrollcommand=_hk_sb.set)
        _hk_sb.pack(side="right", fill="y")
        _hk_cv.pack(side="left", fill="both", expand=True)
        _hk_inner = tk.Frame(_hk_cv, bg=C["bg"])
        _hk_win = _hk_cv.create_window(0, 0, window=_hk_inner, anchor="nw")
        _hk_inner.bind("<Configure>",
                       lambda _e: self._queue_scrollregion_sync(_hk_cv))
        _hk_cv.bind("<Configure>", lambda e: _hk_cv.itemconfigure(
            _hk_win, width=e.width))
        parent = _hk_inner

        # ── Dictation hotkey ─────────────────────────────────────────────────────
        card1 = self._card(parent, margin=(0, 8))

        tk.Label(card1, text="Dictation shortcut",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")

        self._hotkey_display_lbl = tk.Label(
            card1, text=self._hotkey or "ALT+V",
            fg=C["accent"], bg=C["surface"],
            font=("Segoe UI", 18, "bold"), anchor="w",
        )
        self._hotkey_display_lbl.pack(fill="x", pady=(2, 8))

        tk.Frame(card1, bg=C["border"], height=1).pack(fill="x", pady=(0, 10))

        # Mode state must exist before the description below — its wording
        # depends on hold vs toggle.
        _init_mode = getattr(self._config, "mode", "hold") if self._config else "hold"
        self._mode_toggle_on = (_init_mode == "toggle")

        self._hotkey_record_msg = tk.Label(
            card1,
            text=self._hotkey_help_text(),
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340,
        )
        self._hotkey_record_msg.pack(fill="x", pady=(0, 8))
        self._autowrap(self._hotkey_record_msg)

        btn_row = tk.Frame(card1, bg=C["surface"])
        btn_row.pack(fill="x")

        self._record_btn = self._surface_btn(
            btn_row, "Change Shortcut", self._toggle_hotkey_recording)
        self._record_btn.pack(side="left", padx=(0, 8))

        self._save_btn = RoundedButton(
            btn_row, text="Save",
            fg=C["subtext"], fill=C["border"],
            font=("Segoe UI", 10, "bold"), padx=16, pady=8,
        )
        self._save_btn.pack(side="left")

        # Compact toggle switch — inline with the buttons (mode state was
        # initialised above, before the description label).
        _mode_right = tk.Frame(btn_row, bg=C["surface"])
        _mode_right.pack(side="right")

        self._mode_lbl = tk.Label(
            _mode_right, text="Toggle",
            fg=C["text"] if self._mode_toggle_on else C["subtext"],
            bg=C["surface"],
            font=("Segoe UI", 9), cursor="hand2",
        )
        self._mode_lbl.pack(side="right", padx=(6, 0))

        def _click_mode_toggle_logic(new_val: bool):
            self._mode_toggle_on = new_val
            mode = "toggle" if new_val else "hold"
            self._mode_lbl.configure(
                fg=C["text"] if new_val else C["subtext"]
            )
            # Description follows the mode — but never clobber the "press your
            # new key…" prompt while a shortcut recording is in progress.
            if not self._recording_hotkey:
                self._hotkey_record_msg.configure(
                    text=self._hotkey_help_text(), fg=C["subtext"])
            if hasattr(self, "_home_mode_hint_lbl"):
                self._home_mode_hint_lbl.configure(
                    text=" press to start/stop" if new_val else " hold to dictate"
                )
            if self._on_settings_change:
                self._on_settings_change("mode", mode)

        self._mode_pill = TogglePill(
            _mode_right, value=self._mode_toggle_on, bg=C["surface"],
            command=_click_mode_toggle_logic,
        )
        self._mode_pill.pack(side="right")
        self._mode_lbl.bind("<Button-1>", lambda _e: self._mode_pill.toggle())

        # ── Refine selection hotkey ───────────────────────────────────────────────
        card2 = self._card(parent, margin=(0, 8))

        tk.Label(card2, text="Refine selection shortcut",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")

        self._refine_hotkey_display_lbl = tk.Label(
            card2, text=self._refine_hotkey or "ALT+R",
            fg=C["accent"], bg=C["surface"],
            font=("Segoe UI", 18, "bold"), anchor="w",
        )
        self._refine_hotkey_display_lbl.pack(fill="x", pady=(2, 8))

        tk.Frame(card2, bg=C["border"], height=1).pack(fill="x", pady=(0, 10))

        self._refine_record_msg = tk.Label(
            card2,
            text="Select text anywhere, then press this key to refine it with AI.",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340,
        )
        self._refine_record_msg.pack(fill="x", pady=(0, 8))
        self._autowrap(self._refine_record_msg)

        btn_row2 = tk.Frame(card2, bg=C["surface"])
        btn_row2.pack(fill="x")

        self._refine_record_btn = self._surface_btn(
            btn_row2, "Change Shortcut", self._toggle_refine_hotkey_recording)
        self._refine_record_btn.pack(side="left", padx=(0, 8))

        self._refine_save_btn = RoundedButton(
            btn_row2, text="Save",
            fg=C["subtext"], fill=C["border"],
            font=("Segoe UI", 10, "bold"), padx=16, pady=8,
        )
        self._refine_save_btn.pack(side="left")

    def _hotkey_help_text(self) -> str:
        """Description under the shortcut — wording tracks hold vs toggle mode."""
        hk = (self._hotkey or "ALT+V").upper()
        if getattr(self, "_mode_toggle_on", True):
            return (f"Press {hk} once to start recording, again to stop and "
                    "insert. Press  Change Shortcut  to set a new key combo.")
        return (f"Hold {hk} while you speak, release to insert. "
                "Press  Change Shortcut  to set a new key combo.")

    def _toggle_hotkey_recording(self) -> None:
        if self._recording_hotkey:
            self._stop_hotkey_recording(cancelled=True)
        else:
            self._start_hotkey_recording()

    def _start_hotkey_recording(self) -> None:
        # Both recorders share the root <KeyPress> binding — only one can be
        # live, or stopping either would strand the other mid-recording.
        if self._recording_refine_hotkey:
            self._stop_refine_hotkey_recording(cancelled=True)
        self._recording_hotkey = True
        self._pending_hotkey = None
        self._record_btn.configure(text="Cancel", bg=C["error"], fg=C["text"])
        self._hotkey_record_msg.configure(
            text="Press your new key or combination… (Escape to cancel)",
            fg=C["accent"],
        )
        self._hotkey_display_lbl.configure(text="…")
        self._root.focus_force()
        self._root.bind("<KeyPress>",   self._on_hk_keypress)
        self._root.bind("<KeyRelease>", self._on_hk_keyrelease)

    _TK_CTRL  = 0x0004
    _TK_ALT   = 0x20000
    _TK_SHIFT = 0x0001

    def _on_hk_keypress(self, event) -> str:
        keysym = event.keysym.lower()
        if keysym == "escape":
            self._stop_hotkey_recording(cancelled=True)
            return "break"
        if keysym in ("control_l", "control_r", "alt_l", "alt_r",
                      "shift_l", "shift_r", "super_l", "super_r", "meta_l", "meta_r"):
            return "break"
        mods = []
        if event.state & self._TK_CTRL:  mods.append("ctrl")
        if event.state & self._TK_ALT:   mods.append("alt")
        if event.state & self._TK_SHIFT: mods.append("shift")
        base = self._norm_keysym(keysym)
        combo = "+".join(mods + [base]) if mods else base
        self._pending_hotkey = combo
        self._hotkey_display_lbl.configure(text=combo.upper())
        self._root.after(300, lambda: self._stop_hotkey_recording(cancelled=False))
        return "break"

    def _on_hk_keyrelease(self, event) -> None:
        pass

    def _stop_hotkey_recording(self, cancelled: bool) -> None:
        self._recording_hotkey = False
        self._root.unbind("<KeyPress>")
        self._root.unbind("<KeyRelease>")
        self._record_btn.configure(text="Change Shortcut",
                                   bg=C["surface"], fg=C["text"], cursor="hand2")

        if cancelled or not self._pending_hotkey:
            self._hotkey_display_lbl.configure(text=self._hotkey or "—")
            self._hotkey_record_msg.configure(
                text=self._hotkey_help_text(), fg=C["subtext"])
            self._save_btn.configure(bg=C["border"], cursor="", fg=C["subtext"])
        else:
            self._hotkey_display_lbl.configure(text=self._pending_hotkey.upper())
            self._hotkey_record_msg.configure(
                text=f"New shortcut: {self._pending_hotkey.upper()} — Click Save to apply.",
                fg=C["success"],
            )
            self._save_btn.configure(bg=C["accent"], cursor="hand2", fg=C["bg"])
            self._save_btn.bind("<Button-1>", lambda _e: self._save_hotkey())
            self._save_btn.bind("<Enter>",    lambda _e: self._save_btn.configure(bg=C["accent_hover"]))
            self._save_btn.bind("<Leave>",    lambda _e: self._save_btn.configure(bg=C["accent"]))

    def _save_hotkey(self) -> None:
        if not self._pending_hotkey:
            return
        new_hotkey = self._pending_hotkey
        self._hotkey = new_hotkey.upper()
        self._hotkey_display_lbl.configure(text=self._hotkey or "—")
        if hasattr(self, "_home_hotkey_lbl"):
            self._home_hotkey_lbl.configure(text=self._hotkey or "—")
        self._pending_hotkey = None
        self._save_btn.configure(bg=C["border"], cursor="", fg=C["subtext"])
        self._save_btn.unbind("<Button-1>")
        self._hotkey_record_msg.configure(
            text=f"Shortcut updated to {self._hotkey}.",
            fg=C["success"],
        )

        # Let the confirmation flash, then settle back to the mode-aware help.
        def _restore_help():
            try:
                if not self._recording_hotkey:
                    self._hotkey_record_msg.configure(
                        text=self._hotkey_help_text(), fg=C["subtext"])
            except tk.TclError:
                pass
        if self._root:
            self._root.after(4000, _restore_help)

        threading.Thread(
            target=self._on_hotkey_change, args=(new_hotkey,), daemon=True
        ).start()

    # ── Refine hotkey recorder ────────────────────────────────────────────────

    def _toggle_refine_hotkey_recording(self) -> None:
        if self._recording_refine_hotkey:
            self._stop_refine_hotkey_recording(cancelled=True)
        else:
            self._start_refine_hotkey_recording()

    def _start_refine_hotkey_recording(self) -> None:
        if self._recording_hotkey:
            self._stop_hotkey_recording(cancelled=True)
        self._recording_refine_hotkey = True
        self._pending_refine_hotkey = None
        self._refine_record_btn.configure(text="Cancel", bg=C["error"], fg=C["text"])
        self._refine_record_msg.configure(
            text="Press your new key or combination… (Escape to cancel)",
            fg=C["accent"],
        )
        self._refine_hotkey_display_lbl.configure(text="…")
        self._root.focus_force()
        self._root.bind("<KeyPress>",   self._on_refine_hk_keypress)
        self._root.bind("<KeyRelease>", self._on_refine_hk_keyrelease)

    def _on_refine_hk_keypress(self, event) -> str:
        keysym = event.keysym.lower()
        if keysym == "escape":
            self._stop_refine_hotkey_recording(cancelled=True)
            return "break"
        if keysym in ("control_l", "control_r", "alt_l", "alt_r",
                      "shift_l", "shift_r", "super_l", "super_r", "meta_l", "meta_r"):
            return "break"
        mods = []
        if event.state & self._TK_CTRL:  mods.append("ctrl")
        if event.state & self._TK_ALT:   mods.append("alt")
        if event.state & self._TK_SHIFT: mods.append("shift")
        base = self._norm_keysym(keysym)
        combo = "+".join(mods + [base]) if mods else base
        self._pending_refine_hotkey = combo
        self._refine_hotkey_display_lbl.configure(text=combo.upper())
        self._root.after(300, lambda: self._stop_refine_hotkey_recording(cancelled=False))
        return "break"

    def _on_refine_hk_keyrelease(self, event) -> None:
        pass

    def _stop_refine_hotkey_recording(self, cancelled: bool) -> None:
        self._recording_refine_hotkey = False
        self._root.unbind("<KeyPress>")
        self._root.unbind("<KeyRelease>")
        self._refine_record_btn.configure(
            text="Change Shortcut", bg=C["surface"], fg=C["text"], cursor="hand2")

        if cancelled or not self._pending_refine_hotkey:
            self._refine_hotkey_display_lbl.configure(text=self._refine_hotkey or "—")
            self._refine_record_msg.configure(
                text="Select text anywhere, then press this key to refine it with AI.",
                fg=C["subtext"],
            )
            self._refine_save_btn.configure(bg=C["border"], cursor="", fg=C["subtext"])
        else:
            self._refine_hotkey_display_lbl.configure(text=self._pending_refine_hotkey.upper())
            self._refine_record_msg.configure(
                text=f"New shortcut: {self._pending_refine_hotkey.upper()} — Click Save to apply.",
                fg=C["success"],
            )
            self._refine_save_btn.configure(bg=C["accent"], cursor="hand2", fg=C["bg"])
            self._refine_save_btn.bind("<Button-1>", lambda _e: self._save_refine_hotkey())
            self._refine_save_btn.bind("<Enter>",    lambda _e: self._refine_save_btn.configure(bg=C["accent_hover"]))
            self._refine_save_btn.bind("<Leave>",    lambda _e: self._refine_save_btn.configure(bg=C["accent"]))

    def _save_refine_hotkey(self) -> None:
        if not self._pending_refine_hotkey:
            return
        new_hotkey = self._pending_refine_hotkey
        self._refine_hotkey = new_hotkey.upper()
        self._refine_hotkey_display_lbl.configure(text=self._refine_hotkey or "—")
        self._home_refine_hotkey_lbl.configure(text=self._refine_hotkey or "—")
        self._pending_refine_hotkey = None
        self._refine_save_btn.configure(bg=C["border"], cursor="", fg=C["subtext"])
        self._refine_save_btn.unbind("<Button-1>")
        self._refine_record_msg.configure(
            text=f"Shortcut updated to {self._refine_hotkey}.",
            fg=C["success"],
        )
        if self._on_refine_hotkey_change:
            threading.Thread(
                target=self._on_refine_hotkey_change, args=(new_hotkey,), daemon=True
            ).start()

    @staticmethod
    def _norm_keysym(keysym: str) -> str:
        _MAP = {
            "return": "enter", "prior": "pageup", "next": "pagedown",
            "caps_lock": "caps lock", "escape": "esc",
        }
        return _MAP.get(keysym.lower(), keysym.lower())

    # ── History tab ───────────────────────────────────────────────────────────

    def _build_history_tab(self, parent: tk.Frame) -> None:
        top = tk.Frame(parent, bg=C["bg"])
        top.pack(fill="x", padx=20, pady=(0, 8))

        tk.Label(top, text="Recent transcriptions",
                 fg=C["subtext"], bg=C["bg"],
                 font=("Segoe UI", 9)).pack(side="left")

        self._ghost_btn(
            top, "↻ Refresh", lambda: self._load_history(force=True)
        ).pack(side="right")
        self._ghost_btn(top, "✕ Clear",   self._confirm_clear_history).pack(side="right", padx=(0, 8))

        # Search bar — rounded (matches the history card), filters the fetched
        # history client-side as you type.
        self._hist_query = ""
        SEARCH_H = 34
        search_cv = tk.Canvas(parent, height=SEARCH_H, bg=C["bg"],
                              highlightthickness=0, bd=0)
        search_cv.pack(fill="x", padx=20, pady=(0, 10))
        search_inner = tk.Frame(search_cv, bg=C["input_bg"])

        _mag = tk.Canvas(search_inner, width=18, height=18, bg=C["input_bg"],
                         highlightthickness=0, bd=0)
        _mag.create_oval(4, 4, 12, 12, outline=C["subtext"], width=1.5)
        _mag.create_line(11.5, 11.5, 15.5, 15.5, fill=C["subtext"], width=1.5,
                         capstyle="round")
        _mag.pack(side="left", padx=(12, 6))

        _PLACEHOLDER = "Search transcriptions…"
        self._hist_search = tk.Entry(
            search_inner, bg=C["input_bg"], fg=C["subtext"], relief="flat", bd=0,
            highlightthickness=0, insertbackground=C["text"], font=("Segoe UI", 9))
        self._hist_search.insert(0, _PLACEHOLDER)
        self._hist_search.pack(side="left", fill="x", expand=True, padx=(0, 12))

        # Inner content is inset 2px so the Canvas-drawn rounded outline stays visible.
        search_win = search_cv.create_window(2, SEARCH_H // 2, window=search_inner,
                                             anchor="w")
        _search_state = {"focus": False}

        def _draw_search(_e=None):
            w = search_cv.winfo_width()
            if w <= 1:
                return
            search_cv.delete("bg")
            outline = C["accent"] if _search_state["focus"] else C["border"]
            _rr(search_cv, 1, 1, w - 1, SEARCH_H - 1, 8,
                fill=C["input_bg"], outline=outline, width=1, tags="bg")
            search_cv.tag_lower("bg")
            search_cv.itemconfigure(search_win, width=w - 4)

        search_cv.bind("<Configure>", _draw_search)

        def _sf_in(_e):
            if self._hist_search.get() == _PLACEHOLDER:
                self._hist_search.delete(0, "end")
                self._hist_search.configure(fg=C["text"])
            _search_state["focus"] = True
            _draw_search()

        def _sf_out(_e):
            _search_state["focus"] = False
            _draw_search()
            if not self._hist_search.get().strip():
                self._hist_search.delete(0, "end")
                self._hist_search.insert(0, _PLACEHOLDER)
                self._hist_search.configure(fg=C["subtext"])

        def _sf_key(_e):
            v = self._hist_search.get()
            self._hist_query = "" if v == _PLACEHOLDER else v.strip().lower()
            old_job = getattr(self, "_hist_search_job", None)
            if old_job is not None:
                try:
                    self._root.after_cancel(old_job)
                except tk.TclError:
                    pass
            self._hist_search_job = self._root.after(90, self._apply_history_search)

        self._hist_search.bind("<FocusIn>", _sf_in)
        self._hist_search.bind("<FocusOut>", _sf_out)
        self._hist_search.bind("<KeyRelease>", _sf_key)
        search_cv.bind("<Button-1>", lambda _e: self._hist_search.focus_set())

        # Middle row: the rounded card sits on the left and an external
        # scrollbar sits on the far right, OUTSIDE the card border (matching the
        # Settings/Hotkey tabs, where the scrollbar is on the window background).
        # padx=(20, 0): left aligns with the search bar; the scrollbar attaches
        # flush to the window's right edge like the Hotkey/Settings tabs.
        mid = tk.Frame(parent, bg=C["bg"])
        mid.pack(fill="both", expand=True, padx=(20, 0), pady=(0, 8))

        # Rounded card background canvas — packed after the scrollbar (below) so
        # the scrollbar reserves the right edge first, then the card fills the rest.
        card_cv = tk.Canvas(mid, bg=C["bg"], highlightthickness=0, bd=0)

        def _redraw_card(_e=None):
            cw, ch = card_cv.winfo_width(), card_cv.winfo_height()
            if cw < 2 or ch < 2:
                return
            card_cv.delete("bg")
            _rr(card_cv, 0, 0, cw-1, ch-1, 10,
                fill=C["surface"], outline=C["border"], tags="bg")
            card_cv.tag_lower("bg")

        # Frame embedded in card canvas to hold the scrollable list
        card_inner = tk.Frame(card_cv, bg=C["surface"])
        card_win = card_cv.create_window(1, 1, window=card_inner, anchor="nw")

        def _sync_card(_e=None):
            cw, ch = card_cv.winfo_width(), card_cv.winfo_height()
            if cw > 2 and ch > 2:
                card_cv.itemconfigure(card_win, width=cw - 2, height=ch - 2)
            _redraw_card()

        card_cv.bind("<Configure>", _sync_card)

        # Scrollable list canvas — fills the whole card (no scrollbar inside now)
        self._hist_cv = tk.Canvas(card_inner, bg=C["surface"],
                                  highlightthickness=0, bd=0,
                                  yscrollincrement=1)
        self._hist_cv.pack(fill="both", expand=True)

        # External scrollbar on the window background, to the RIGHT of the card.
        # Packed (side=right) before the card is packed (side=left, expand) so it
        # claims the right edge; the card then fills the remaining width.
        self._hist_sb = ModernScrollbar(
            mid, command=lambda *a: self._scrollbar_command(self._hist_cv, *a))
        self._hist_cv.configure(yscrollcommand=self._hist_sb.set)
        self._hist_sb.pack(side="right", fill="y")
        # Right pad 8 so the card's right edge lines up with the search bar (W-20),
        # while the 12px scrollbar sits flush at the window edge just beyond it.
        card_cv.pack(side="left", fill="both", expand=True, padx=(0, 8))

        # History rows are native Canvas items, not a thousand embedded HWND
        # widgets.  One surface eliminates child-window ghosting and keeps fast
        # scroll repaint cost essentially constant as history grows.
        self._hist_layout = []
        self._hist_row_starts = []
        self._hist_drawn_rows = {}
        self._hist_icon_refs = []
        self._hist_hover_index = None
        self._hist_expanded_key = None
        self._hist_confirm_key = None
        self._hist_cv.bind("<Configure>", self._on_history_canvas_configure)
        self._hist_cv.bind("<Motion>", self._on_history_motion)
        self._hist_cv.bind("<Leave>", lambda _e: self._set_history_hover(None))
        self._hist_cv.bind("<Button-1>", self._on_history_click)


    @staticmethod
    def _repaint(widget) -> None:
        """Windows: a tk.Canvas scrolls by blitting its pixels, but embedded row
        Frames are real child windows — fast wheel scrolling leaves torn "ghost"
        copies of rows in the uncovered regions (the expose pass never repaints
        them). Force a clean repaint of the canvas and every child after each
        scroll step. RDW_INVALIDATE|RDW_ALLCHILDREN (0x81) WITHOUT erase:
        repaint-over, so no background flash."""
        try:
            ctypes.windll.user32.RedrawWindow(widget.winfo_id(), None, None, 0x81)
        except Exception:
            pass

    def _route_mousewheel(self, event):
        """Route each Windows wheel event once to the active scroll surface."""
        cv = None
        if self._current_tab == "history":
            cv = getattr(self, "_hist_cv", None)
        elif self._current_tab == "settings":
            cv = getattr(self, "_settings_cv", None)
        elif self._current_tab == "hotkey":
            cv = getattr(self, "_hk_cv", None)
        if cv is not None:
            return self._wheel_scroll(cv, event)
        return None

    @staticmethod
    def _scroll_metrics(cv):
        """Return (content_height, max_top, current_top) in canvas pixels.

        Height comes from the configured scrollregion, not bbox("all"). bbox
        walks every item on the canvas, so on a long history it got slower the
        more rows existed — and it ran on every wheel event AND every animation
        frame. The scrollregion is already maintained by whoever fills the
        canvas and reading it is O(1) regardless of row count.
        """
        try:
            region = str(cv.cget("scrollregion")).split()
            content_h = max(float(region[3]) - float(region[1]), 1.0)
        except (tk.TclError, AttributeError, IndexError, ValueError):
            bbox = cv.bbox("all")
            if not bbox:
                return 1.0, 0.0, 0.0
            content_h = max(float(bbox[3] - bbox[1]), 1.0)
        viewport_h = max(float(cv.winfo_height()), 1.0)
        max_top = max(content_h - viewport_h, 0.0)
        try:
            current = float(cv.yview()[0]) * content_h
        except (tk.TclError, IndexError, TypeError):
            current = 0.0
        return content_h, max_top, max(0.0, min(max_top, current))

    # Scroll feel. The target is where the wheel says you are; the animation
    # only smooths the last hop to it, it never adds momentum — stop turning
    # the wheel and motion stops inside ~50ms rather than coasting.
    _SCROLL_PX_PER_DELTA = 0.75    # one notch (delta=120) = 90px ≈ 1.7 rows
    _SCROLL_EASE = 0.5             # fraction of the remaining gap per frame
    _SCROLL_MAX_STEP = 120.0       # px/frame ceiling for a fast wheel spin
    _SCROLL_SNAP = 1.0             # within 1px, land on the target and stop

    def _wheel_scroll(self, cv, event):
        """Accumulate precise wheel deltas and animate one pixel target.

        A standard Windows notch (delta=120) travels 90px over ~4 frames.
        Precision touchpad deltas are retained instead of truncated.
        """
        try:
            delta = float(event.delta)
            content_h, max_top, actual = self._scroll_metrics(cv)
        except (tk.TclError, TypeError, ValueError):
            return "break"
        if max_top <= 0.0 or delta == 0.0:
            return "break"

        state = self._scroll_states.setdefault(
            cv, {"current": actual, "target": actual, "job": None})
        if state["job"] is None or abs(state["current"] - actual) > 2.0:
            state["current"] = actual
            state["target"] = actual
        state["target"] = max(
            0.0, min(max_top, state["target"] - delta * self._SCROLL_PX_PER_DELTA))

        if cv is getattr(self, "_hist_cv", None):
            self._set_history_hover(None)
        if state["job"] is None:
            state["job"] = cv.after(0, self._animate_scroll, cv)
        return "break"

    def _animate_scroll(self, cv) -> None:
        state = self._scroll_states.get(cv)
        if not state:
            return
        state["job"] = None
        try:
            content_h, max_top, _actual = self._scroll_metrics(cv)
        except tk.TclError:
            self._scroll_states.pop(cv, None)
            return
        state["target"] = max(0.0, min(max_top, state["target"]))
        state["current"] = max(0.0, min(max_top, state["current"]))
        diff = state["target"] - state["current"]
        if abs(diff) <= self._SCROLL_SNAP:
            state["current"] = state["target"]
        else:
            cap = self._SCROLL_MAX_STEP
            step = max(-cap, min(cap, diff * self._SCROLL_EASE))
            # Collapse the tail instead of creeping the last few px — a slow
            # crawl after the wheel stops is what reads as lag.
            if abs(step) < 2.0:
                step = 2.0 if diff > 0 else -2.0
            state["current"] += step
        try:
            cv.yview_moveto(state["current"] / content_h)
            # History is one native canvas and needs no descendant repaint.
            if cv is not getattr(self, "_hist_cv", None):
                self._repaint(cv)
        except tk.TclError:
            self._scroll_states.pop(cv, None)
            return

        if abs(state["target"] - state["current"]) > self._SCROLL_SNAP:
            state["job"] = cv.after(12, self._animate_scroll, cv)
        elif cv is getattr(self, "_hist_cv", None):
            cv.after(16, self._refresh_history_hover)

    def _cancel_smooth_scroll(self, cv) -> None:
        state = self._scroll_states.get(cv)
        if state and state.get("job") is not None:
            try:
                cv.after_cancel(state["job"])
            except tk.TclError:
                pass
        self._scroll_states.pop(cv, None)

    def _scrollbar_command(self, cv, *args) -> None:
        """Thumb/track motion remains direct and one-to-one with the pointer."""
        self._cancel_smooth_scroll(cv)
        try:
            cv.yview(*args)
        except tk.TclError:
            return
        if cv is not getattr(self, "_hist_cv", None):
            self._repaint(cv)

    def _queue_scrollregion_sync(self, cv) -> None:
        """Recompute a scroll canvas's scrollregion once geometry has settled.
        A bbox taken mid-layout (e.g. the moment the update banner packs in)
        under-reports the content height, leaving the page bottom cut off and
        unreachable — after_idle re-measures when the layout is final."""
        old_job = self._scrollregion_jobs.get(cv)
        if old_job is not None:
            try:
                cv.after_cancel(old_job)
            except tk.TclError:
                pass

        def _sync():
            self._scrollregion_jobs.pop(cv, None)
            try:
                cv.configure(scrollregion=cv.bbox("all"))
            except tk.TclError:
                pass  # canvas destroyed mid-shutdown
        try:
            self._scrollregion_jobs[cv] = cv.after_idle(_sync)
        except tk.TclError:
            pass

    @staticmethod
    def _history_item_key(item: dict) -> str:
        return str(item.get("id") or "|".join((
            item.get("created_at") or "",
            item.get("transcribed_text") or "",
            item.get("refined_text") or "",
        )))

    def _apply_history_search(self) -> None:
        self._hist_search_job = None
        if getattr(self, "_current_tab", "") == "history":
            self._render_history()
        else:
            self._history_pending_render = True

    @staticmethod
    def _history_items_fingerprint(items: list) -> tuple:
        return tuple((
            item.get("id"), item.get("created_at"),
            item.get("transcribed_text"), item.get("refined_text"),
            item.get("app_name"), item.get("app_exe"),
        ) for item in items)

    def _prime_history(self) -> None:
        """Draw local/in-memory rows after first paint, then refresh remotely."""
        if not self._root or not hasattr(self, "_hist_cv"):
            return
        try:
            cached = (self._db.get_cached_history(limit=200)
                      if self._db and hasattr(self._db, "get_cached_history")
                      else [])
        except Exception:
            cached = []
        if cached:
            self._populate_history(cached, render=True)
        self._load_history(force=True)

    def _on_history_cache_changed(self, cached=None) -> None:
        """A new local dictation is available; show it without waiting for sync."""
        if not self._db or not hasattr(self._db, "get_cached_history"):
            self._history_dirty = True
            return
        if cached is None:
            try:
                cached = self._db.get_cached_history(limit=200)
            except Exception:
                self._history_dirty = True
                return
        self._history_dirty = True
        self._populate_history(
            cached, render=(getattr(self, "_current_tab", "") == "history"))

    def _load_history(self, force: bool = False) -> None:
        if self._history_loading or not self._db:
            return
        now = time.monotonic()
        if (not force and not self._history_dirty
                and now - self._history_last_fetch_started < 2.0):
            return
        self._history_loading = True
        self._history_last_fetch_started = now
        if not self._hist_all:
            self._hist_set_placeholder("Loading…")

        def _fetch():
            error = None
            try:
                items = self._db.fetch_history(limit=200)
            except Exception as exc:
                print(f"[AppWindow] History fetch failed: {exc}")
                items, error = [], exc
            self._ui_after(0, self._finish_history_fetch, items, error)

        threading.Thread(
            target=_fetch, daemon=True, name="history-fetch").start()

    def _finish_history_fetch(self, items: list, error=None) -> None:
        self._history_loading = False
        if error is not None:
            if not self._hist_all:
                self._hist_set_placeholder("History unavailable offline.")
            return
        self._history_dirty = False
        self._populate_history(items)

    def _hist_set_placeholder(self, msg: str) -> None:
        cv = self._hist_cv
        cv.delete("all")
        cv.create_text(14, 16, text=msg, fill=C["subtext"],
                       font=("Segoe UI", 10, "italic"), anchor="nw")
        cv.configure(scrollregion=(0, 0, max(cv.winfo_width(), 1), 52))
        self._hist_layout = []
        self._hist_row_starts = []
        self._hist_drawn_rows = {}
        self._hist_icon_refs = []
        # Stale hover state against an empty canvas would point at a row that
        # no longer exists.
        self._hist_hover_index = None
        self._hist_bin_hot = False
        try:
            cv.configure(cursor="")
        except tk.TclError:
            pass

    def _populate_history(self, items: list, render=None) -> None:
        items = items or []
        fingerprint = self._history_items_fingerprint(items)
        changed = fingerprint != self._history_fingerprint
        self._hist_all = items
        self._history_fingerprint = fingerprint
        if not changed and self._history_rendered_once:
            return
        if render is None:
            render = (getattr(self, "_current_tab", "") == "history")
        if render or not self._history_rendered_once:
            self._render_history()
        else:
            self._history_pending_render = True

    @staticmethod
    def _hist_date_label(dt_local) -> str:
        today = datetime.now().date()
        d = dt_local.date()
        if d == today:
            return "Today"
        if (today - d).days == 1:
            return "Yesterday"
        label = f"{d.day} {dt_local.strftime('%B')}"
        if d.year != today.year:
            label += f" {d.year}"
        return label

    def _render_history(self) -> None:
        self._hist_search_job = None
        self._hist_clear_confirm = False
        cv = self._hist_cv
        width = cv.winfo_width()
        if width <= 10:
            self._history_pending_render = True
            return
        old_query = getattr(self, "_hist_render_query", None)
        old_top = self._scroll_metrics(cv)[2]
        items = getattr(self, "_hist_all", [])
        q = getattr(self, "_hist_query", "")
        if q:
            items = [it for it in items
                     if q in ((it.get("refined_text") or it.get("transcribed_text")
                               or "").lower())
                     or q in (it.get("app_name") or "").lower()]
        if not items:
            self._hist_set_placeholder(
                "No matches." if q else "No transcriptions yet.")
            self._hist_render_query = q
            self._history_rendered_once = True
            return
        cv.delete("all")
        self._hist_layout = []
        self._hist_row_starts = []      # y0 per row, sorted — see _history_index_at_y
        self._hist_drawn_rows = {}
        self._hist_icon_refs = []
        self._hist_hover_index = None
        last_group = None
        y = 0
        for item in items:
            raw_ts = item.get("created_at") or ""
            try:
                dt = datetime.fromisoformat(
                    raw_ts.replace("Z", "+00:00")).astimezone()
                group = self._hist_date_label(dt)
            except Exception:
                dt, group = None, "Earlier"
            if group != last_group:
                y += 11 if last_group is None else 13
                cv.create_text(14, y, text=group.upper(), fill=C["subtext"],
                               font=("Segoe UI", 8, "bold"), anchor="nw")
                y += 22
                last_group = group
            elif y > 0:
                cv.create_line(10, y, width - 10, y, fill=C["divider"])

            key = self._history_item_key(item)
            expanded = key in (self._hist_expanded_key, self._hist_confirm_key)
            text = item.get("refined_text") or item.get("transcribed_text") or ""
            # Expanded rows drop the preview line, so the full text starts just
            # under the app name instead of below a 52px header. While the
            # Cancel/Delete pair is up it needs to clear the button (which ends
            # at y0+36), so the text starts lower.
            detail_y = 46 if key == self._hist_confirm_key else 34
            row_h = (detail_y + self._history_detail_height(
                        text, max(width - 72, 80))
                     if expanded else 52)
            index = len(self._hist_layout)
            entry = {"index": index, "item": item, "key": key, "dt": dt,
                     "y0": y, "y1": y + row_h, "detail_y": detail_y}
            self._hist_layout.append(entry)
            self._hist_row_starts.append(y)
            self._draw_history_canvas_row(entry, width)
            y += row_h

        total_h = max(y + 8, cv.winfo_height())
        cv.configure(scrollregion=(0, 0, width, total_h))
        if old_query == q and old_top > 0:
            cv.yview_moveto(min(old_top / max(total_h, 1), 1.0))
        else:
            cv.yview_moveto(0.0)
        self._hist_render_query = q
        self._history_rendered_once = True
        self._history_pending_render = False
        # Expand/collapse/delete re-render under a stationary pointer; restore
        # the hover highlight immediately instead of waiting for mouse motion.
        cv.after(16, self._refresh_history_hover)

    def _history_detail_height(self, text: str, width: int) -> int:
        font = getattr(self, "_hist_body_font", None)
        if font is None:
            font = self._hist_body_font = tkfont.Font(family="Segoe UI", size=9)
        # Ask the same Canvas renderer used for the real detail text. This also
        # handles long URLs/tokens that Tk wraps between characters rather than
        # at spaces, which a word-count approximation under-sized badly.
        probe = self._hist_cv.create_text(
            -10000, -10000, text=text or " ", font=font, anchor="nw",
            width=max(width, 1))
        try:
            bbox = self._hist_cv.bbox(probe)
            rendered_h = (bbox[3] - bbox[1]) if bbox else font.metrics("linespace")
        finally:
            self._hist_cv.delete(probe)
        return max(38, rendered_h + 16)

    def _history_elide(self, text: str, width: int, *, size: int) -> tuple[str, object]:
        """Return one measured line that cannot overlap the metadata below it."""
        attr = f"_hist_elide_font_{size}"
        font = getattr(self, attr, None)
        if font is None:
            font = tkfont.Font(family="Segoe UI", size=size)
            setattr(self, attr, font)
        value = " ".join((text or "").split())
        cache = getattr(self, "_hist_elide_cache", None)
        if cache is None:
            cache = self._hist_elide_cache = {}
        cache_key = (value, int(width), size)
        if cache_key in cache:
            return cache[cache_key], font
        full_width = font.measure(value)
        if full_width <= width:
            if len(cache) >= 1000:
                cache.clear()
            cache[cache_key] = value
            return value, font
        suffix = "…"
        suffix_attr = f"_hist_elide_suffix_width_{size}"
        suffix_width = getattr(self, suffix_attr, None)
        if suffix_width is None:
            suffix_width = font.measure(suffix)
            setattr(self, suffix_attr, suffix_width)
        available = max(width - suffix_width, 0)
        # Start from the full-string average width, then shrink only if the
        # prefix happens to contain wider glyphs. This needs one or two Tcl font
        # calls instead of a binary-search call for every history row.
        count = max(0, min(len(value), int(len(value) * available / full_width)))
        while count > 0:
            prefix = value[:count].rstrip()
            measured = font.measure(prefix)
            if measured <= available:
                result = prefix + suffix
                if len(cache) >= 1000:
                    cache.clear()
                cache[cache_key] = result
                return result, font
            scaled = int(count * available / max(measured, 1))
            count = min(count - 1, scaled)
        cache[cache_key] = suffix
        return suffix, font

    def _history_icon(self, item: dict, bg: str):
        app_name = item.get("app_name") or ""
        app_exe = item.get("app_exe") or ""
        return (get_brand_icon(app_name, bg)
                or get_app_icon(app_exe, bg)
                or get_monogram_icon(app_name, bg)
                or get_fallback_icon(bg))

    def _draw_history_canvas_row(self, entry: dict, width: int) -> None:
        cv = self._hist_cv
        item, y0, y1 = entry["item"], entry["y0"], entry["y1"]
        text = item.get("refined_text") or item.get("transcribed_text") or ""
        app_name = item.get("app_name") or "Unknown app"
        time_str = entry["dt"].strftime("%H:%M") if entry["dt"] else ""
        bg_id = cv.create_rectangle(0, y0, width, y1, fill=C["surface"],
                                    outline="")

        icon_n = self._history_icon(item, C["surface"])
        icon_id = cv.create_image(11, y0 + 8, image=icon_n, anchor="nw")
        self._hist_icon_refs.append(icon_n)

        confirming = (entry["key"] == self._hist_confirm_key)
        expanded = entry["key"] in (self._hist_expanded_key,
                                    self._hist_confirm_key)
        # While the Cancel/Delete pair is showing it eats into the header, so the
        # header text has to stop well short of it or it renders underneath.
        text_width = max((width - 227) if confirming else (width - 154), 80)

        if expanded:
            # One block of text per state: the full text is drawn below, so the
            # truncated preview is dropped and the app name takes the top slot.
            app_label, app_font = self._history_elide(
                app_name, text_width, size=8)
            cv.create_text(57, y0 + 12, text=app_label, fill=C["subtext"],
                           font=app_font, anchor="nw")
        else:
            preview, preview_font = self._history_elide(
                text, text_width, size=9)
            app_label, app_font = self._history_elide(
                app_name, text_width, size=8)
            cv.create_text(57, y0 + 8, text=preview, fill=C["text"],
                           font=preview_font, anchor="nw")
            cv.create_text(57, y0 + 28, text=app_label, fill=C["subtext"],
                           font=app_font, anchor="nw")

        time_id = None
        confirm_items = []
        if confirming:
            confirm_items.append(cv.create_text(
                width - 139, y0 + 18, text="Cancel", fill=C["subtext"],
                font=("Segoe UI", 9), anchor="center"))
            confirm_items.append(_rr(
                cv, width - 109, y0 + 8, width - 48, y0 + 36, 7,
                fill=C["error"], outline=""))
            confirm_items.append(cv.create_text(
                width - 78, y0 + 22, text="Delete", fill=C["bg"],
                font=("Segoe UI", 9, "bold"), anchor="center"))
        else:
            time_id = cv.create_text(width - 51, y0 + 20, text=time_str,
                                     fill=C["subtext"], font=("Segoe UI", 8),
                                     anchor="center")

        # Copy glyph is always stable in the far-right action slot.
        copy_items = [
            cv.create_rectangle(width - 22, y0 + 13, width - 12, y0 + 23,
                                outline=C["subtext"], width=1),
            cv.create_rectangle(width - 26, y0 + 17, width - 16, y0 + 27,
                                fill=C["surface"], outline=C["subtext"], width=1),
        ]

        # Line-art trash can (never an emoji glyph — those render inconsistently
        # and clip). It occupies the TIMESTAMP's slot rather than a slot of its
        # own: on hover the time hides and the bin takes its place, so the header
        # never gets wider and nothing to its left can be pushed out or overlapped.
        # Items are drawn once and only toggled visible, so scrolling under the
        # pointer causes no geometry churn.
        bx, by = width - 62, y0 + 9      # top-left of the 22x22 glyph box
        delete_items = [
            # handle
            cv.create_line(bx + 8, by + 5, bx + 14, by + 5, fill=C["subtext"],
                           width=2, capstyle="round", state="hidden"),
            # lid
            cv.create_line(bx + 4, by + 7, bx + 18, by + 7, fill=C["subtext"],
                           width=2, capstyle="round", state="hidden"),
            # bucket body
            cv.create_line(bx + 6, by + 8, bx + 7, by + 17, bx + 15, by + 17,
                           bx + 16, by + 8, fill=C["subtext"], width=2,
                           capstyle="round", joinstyle="round", state="hidden"),
        ] + [
            # inner stripes
            cv.create_line(bx + x, by + 9, bx + x, by + 15, fill=C["subtext"],
                           width=1, capstyle="round", state="hidden")
            for x in (9, 11, 13)
        ]

        if expanded:
            cv.create_text(57, y0 + entry["detail_y"], text=text,
                           fill=C["text"], font=("Segoe UI", 9), anchor="nw",
                           width=max(width - 72, 80))

        self._hist_drawn_rows[entry["index"]] = {
            "bg": bg_id, "icon": icon_id, "icon_n": icon_n,
            "copy": copy_items, "delete": delete_items,
            "time": time_id, "confirm": confirm_items,
        }

    def _history_index_at_y(self, canvas_y: float):
        """Row under a canvas y, in O(log n).

        Rows are laid out top-to-bottom so their y0 values are already sorted;
        a linear scan here ran on every single mouse-motion event and got
        slower the longer the history was.
        """
        starts = self._hist_row_starts
        if not starts:
            return None
        i = bisect.bisect_right(starts, canvas_y) - 1
        if i < 0:
            return None
        entry = self._hist_layout[i]
        return entry["index"] if canvas_y < entry["y1"] else None

    def _set_history_hover(self, index) -> None:
        if index == self._hist_hover_index:
            return
        cv = getattr(self, "_hist_cv", None)
        if cv is None:
            return
        old = self._hist_drawn_rows.get(self._hist_hover_index)
        if old:
            cv.itemconfigure(old["bg"], fill=C["surface"])
            cv.itemconfigure(old["icon"], image=old["icon_n"])
            for part in old["delete"]:
                # Reset the colour too — a bin left red would come back red the
                # next time this row is hovered.
                cv.itemconfigure(part, state="hidden", fill=C["subtext"])
            if old["time"] is not None:      # timestamp comes back
                cv.itemconfigure(old["time"], state="normal")
        self._hist_bin_hot = False
        self._hist_hover_index = index
        row = self._hist_drawn_rows.get(index)
        # Rows are clickable (expand/copy/delete) — show a hand as feedback.
        try:
            cv.configure(cursor="hand2" if row else "")
        except tk.TclError:
            pass
        if not row:
            return
        cv.itemconfigure(row["bg"], fill=C["surface_hover"])
        entry = self._hist_layout[index]
        # Hover icon is fetched once per row and kept on the row dict — the
        # old append-per-hover list grew without bound over a long session.
        icon_h = row.get("icon_h")
        if icon_h is None:
            icon_h = self._history_icon(entry["item"], C["surface_hover"])
            row["icon_h"] = icon_h
        cv.itemconfigure(row["icon"], image=icon_h)
        if entry["key"] != self._hist_confirm_key:
            # Bin REPLACES the time in its slot — hide one before showing the
            # other or the two render on top of each other.
            if row["time"] is not None:
                cv.itemconfigure(row["time"], state="hidden")
            for part in row["delete"]:
                cv.itemconfigure(part, state="normal")

    def _on_history_motion(self, event) -> None:
        cv = self._hist_cv
        index = self._history_index_at_y(cv.canvasy(event.y))
        self._set_history_hover(index)
        # Bin turns red when the pointer is actually on it — the delete
        # affordance the widget version had. Only repaints on a state change.
        width = cv.winfo_width()
        on_bin = (index is not None
                  and width - 66 <= event.x < width - 34)
        if on_bin == getattr(self, "_hist_bin_hot", False):
            return
        self._hist_bin_hot = on_bin
        row = self._hist_drawn_rows.get(index)
        if not row:
            return
        colour = C["error"] if on_bin else C["subtext"]
        for part in row["delete"]:
            try:
                cv.itemconfigure(part, fill=colour)
            except tk.TclError:
                return

    def _refresh_history_hover(self) -> None:
        cv = getattr(self, "_hist_cv", None)
        if not cv:
            return
        try:
            px, py = cv.winfo_pointerxy()
            x, y = px - cv.winfo_rootx(), py - cv.winfo_rooty()
            if 0 <= x < cv.winfo_width() and 0 <= y < cv.winfo_height():
                self._set_history_hover(
                    self._history_index_at_y(cv.canvasy(y)))
        except tk.TclError:
            pass

    def _on_history_canvas_configure(self, event) -> None:
        if not self._history_rendered_once:
            return
        old = getattr(self, "_hist_canvas_width", 0)
        self._hist_canvas_width = event.width
        if abs(event.width - old) <= 2:
            return
        job = getattr(self, "_hist_resize_job", None)
        if job is not None:
            try:
                self._root.after_cancel(job)
            except tk.TclError:
                pass
        self._hist_resize_job = self._root.after(30, self._render_history)

    def _on_history_click(self, event) -> None:
        if getattr(self, "_hist_clear_confirm", False):
            if 14 <= event.x <= 126 and 76 <= event.y <= 108:
                self._clear_history()
            elif 136 <= event.x <= 210 and 76 <= event.y <= 108:
                self._hist_clear_confirm = False
                self._render_history()
            return
        index = self._history_index_at_y(self._hist_cv.canvasy(event.y))
        if index is None:
            return
        entry = self._hist_layout[index]
        key, item = entry["key"], entry["item"]
        width = self._hist_cv.winfo_width()

        if key == self._hist_confirm_key:
            if width - 110 <= event.x <= width - 47:
                self._delete_history_item(item)
            elif width - 164 <= event.x < width - 110:
                self._hist_confirm_key = None
                self._hist_expanded_key = None
                self._render_history()
            return
        if event.x >= width - 34:
            text = item.get("refined_text") or item.get("transcribed_text") or ""
            self._copy_to_clipboard(text)
            return
        # Bin slot = the timestamp slot (width-62 … width-40), padded a little.
        if (width - 66 <= event.x < width - 34
                and self._hist_hover_index == index):
            self._hist_confirm_key = key
            self._hist_expanded_key = key
            self._render_history()
            return
        self._hist_expanded_key = None if self._hist_expanded_key == key else key
        self._hist_confirm_key = None
        self._render_history()

    def _delete_history_item(self, item: dict) -> None:
        def _worker():
            try:
                if self._db:
                    self._db.delete_transcription(item)
            except Exception as exc:
                print(f"[History] Delete failed: {exc}")

        threading.Thread(target=_worker, daemon=True, name="history-delete").start()
        try:
            self._hist_all.remove(item)
        except ValueError:
            pass
        self._history_fingerprint = self._history_items_fingerprint(self._hist_all)
        self._hist_confirm_key = None
        self._hist_expanded_key = None
        self._render_history()

    def _copy_to_clipboard(self, text: str, btn=None) -> None:
        if self._root:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
        if btn:
            btn.configure(text="✓", fg=C["success"])
            self._root.after(1500, lambda: btn.configure(text="⎘", fg=C["subtext"]))

    def _confirm_clear_history(self) -> None:
        self._cancel_smooth_scroll(self._hist_cv)
        self._hist_clear_confirm = True
        cv = self._hist_cv
        cv.delete("all")
        cv.yview_moveto(0)
        cv.configure(scrollregion=(0, 0, max(cv.winfo_width(), 1), 140))
        cv.create_text(14, 14, text="Delete all history?", fill=C["text"],
                       font=("Segoe UI", 10), anchor="nw")
        cv.create_text(
            14, 37,
            text="Disappears from the app now; removed from the cloud after 30 days.",
            fill=C["subtext"], font=("Segoe UI", 8), anchor="nw",
            width=max(cv.winfo_width() - 28, 120))
        _rr(cv, 14, 76, 126, 108, 8, fill=C["error"], outline="")
        cv.create_text(70, 92, text="Yes, delete all", fill=C["bg"],
                       font=("Segoe UI", 9, "bold"), anchor="center")
        _rr(cv, 136, 76, 210, 108, 8, fill=C["surface_hover"], outline="")
        cv.create_text(173, 92, text="Cancel", fill=C["subtext"],
                       font=("Segoe UI", 9), anchor="center")

    def _clear_history(self) -> None:
        self._hist_clear_confirm = False
        self._hist_all = []
        self._history_fingerprint = ()
        self._hist_set_placeholder("No transcriptions yet.")

        def _worker():
            if self._db:
                try:
                    self._db.clear_history()
                except Exception as exc:
                    print(f"[History] Clear failed: {exc}")

        threading.Thread(target=_worker, daemon=True, name="history-clear").start()

    # ── Settings tab ─────────────────────────────────────────────────────────

    def _build_settings_tab(self, parent: tk.Frame) -> None:
        # Scrollable container
        self._settings_cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0,
                                      bd=0, yscrollincrement=1)
        self._settings_sb = ModernScrollbar(
            parent, command=lambda *a: self._scrollbar_command(
                self._settings_cv, *a))
        self._settings_cv.configure(yscrollcommand=self._settings_sb.set)
        self._settings_sb.pack(side="right", fill="y")
        self._settings_cv.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(self._settings_cv, bg=C["bg"])
        _win = self._settings_cv.create_window(0, 0, window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda _e: self._queue_scrollregion_sync(self._settings_cv))
        self._settings_cv.bind("<Configure>", lambda e: self._settings_cv.itemconfigure(
            _win, width=e.width))

        # Shadow parent so all existing code below writes into the scrollable frame
        parent = inner
        cfg = self._config

        # ── Updates — deliberately first in Settings ─────────────────────────
        ver_card = self._card(parent, margin=(0, 4))
        ver_header = tk.Frame(ver_card, bg=C["surface"])
        ver_header.pack(fill="x")
        tk.Label(ver_header, text="Updates",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(
                     side="left", fill="x", expand=True)

        self._update_check_btn = tk.Label(
            ver_header, text="Check for Updates",
            fg=C["accent"], bg=C["surface"],
            font=("Segoe UI", 9), cursor="hand2", anchor="e",
        )
        self._update_check_btn.pack(side="right")

        ver_lbl_text = f"Version {self._version}" if self._version else "FTC Whisper"
        tk.Label(ver_card, text=ver_lbl_text,
                 fg=C["text"], bg=C["surface"],
                 font=("Segoe UI", 10), anchor="w").pack(fill="x", pady=(6, 0))

        # Status gets its own full-width line so even failure messages remain
        # completely readable at the app's narrow fixed width.
        self._update_status_lbl = tk.Label(
            ver_card, text="", fg=C["success"], bg=C["surface"],
            font=("Segoe UI", 9), anchor="w", justify="left", wraplength=320,
        )
        self._autowrap(self._update_status_lbl)

        def _show_update_status(text, colour):
            self._update_status_lbl.configure(text=text, fg=colour)
            self._update_status_lbl.pack(fill="x", pady=(5, 0))

        def _restore_check_btn():
            self._update_check_btn.configure(
                text="Check for Updates", fg=C["accent"], cursor="hand2")
            self._update_check_btn.bind("<Button-1>", _check_now)

        def _check_now(_e=None):
            self._update_check_btn.configure(text="Checking...", fg=C["subtext"], cursor="")
            self._update_check_btn.unbind("<Button-1>")
            self._update_status_lbl.pack_forget()

            def _check_worker():
                from updater import get_latest_release, is_newer
                info = get_latest_release()

                def _apply():
                    if info is None:
                        _show_update_status(
                            "Check failed — no connection? You can try again.", C["subtext"])
                        _restore_check_btn()
                    elif is_newer(info["version"], self._version):
                        _show_update_status(
                            f"Update {info['version']} is available.", C["accent"])
                        _restore_check_btn()
                        self.show_update_banner(info["version"], info["download_url"])
                    else:
                        _show_update_status("You're up to date ✓", C["success"])
                        _restore_check_btn()
                self._ui_after(0, _apply)

            threading.Thread(target=_check_worker, daemon=True,
                             name="manual-update-check").start()

        self._do_update_check = _check_now
        self._update_check_btn.bind("<Button-1>", _check_now)
        self._update_check_btn.bind("<Enter>",
            lambda _e: self._update_check_btn.configure(fg=C["accent_hover"]))
        self._update_check_btn.bind("<Leave>",
            lambda _e: self._update_check_btn.configure(fg=C["accent"]))

        self._ver_update_row = tk.Frame(ver_card, bg=C["surface"])

        # Detailed available-update notice appears immediately below the card.
        self._update_banner_frame = tk.Frame(parent, bg=C["bg"])
        self._update_banner_frame.pack(fill="x")

        # ── Microphone ────────────────────────────────────────────────────────
        mic_card = self._card(parent, margin=(0, 8))
        tk.Label(mic_card, text="Microphone",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")

        # Deduplicate by name, then sort: real mics first, virtual/system last
        def _mic_rank(d):
            n = d["name"].lower()
            if any(x in n for x in ["stereo mix", "sound mapper", "primary sound",
                                     "what u hear", "wave out", "pc speaker"]):
                return 2
            if any(x in n for x in ["microphone", "mic", "headset", "webcam",
                                     "logi", "jabra", "yeti", "rode", "shure",
                                     "usb audio", "array"]):
                return 0
            return 1

        unique_devs = []
        current_mic = (cfg.input_device or "") if cfg else ""
        mic_var = tk.StringVar(value=current_mic if current_mic else "Default")

        mic_menu = tk.OptionMenu(mic_card, mic_var, "Default")
        mic_menu.configure(bg=C["surface_hover"], fg=C["text"], relief="flat",
                           font=("Segoe UI", 9), anchor="w", highlightthickness=0,
                           activebackground=C["accent"], activeforeground=C["bg"])
        mic_menu["menu"].configure(bg=C["surface"], fg=C["text"],
                                   activebackground=C["accent"], activeforeground=C["bg"],
                                   font=("Segoe UI", 9))
        mic_menu.pack(fill="x", pady=(4, 0))

        # The OptionMenu button itself shows the selected device — no separate
        # label below it (that duplicated the same text into two blocks).

        def _populate_mic_menu(devs):
            try:
                seen_names: set = set()
                for d in devs:
                    if d["name"] not in seen_names:
                        seen_names.add(d["name"])
                        unique_devs.append(d)
                unique_devs.sort(key=_mic_rank)
                options = ["Default"] + [d["name"] for d in unique_devs]
                menu = mic_menu["menu"]
                menu.delete(0, "end")
                for opt in options:
                    menu.add_command(label=opt, command=lambda v=opt: mic_var.set(v))

                if current_mic and current_mic in options:
                    mic_var.set(current_mic)
                else:
                    # Empty config = "Default" (follow the Windows default mic).
                    # The old code treated empty as "not chosen yet" and silently
                    # persisted a specific device — which made "Default"
                    # impossible to keep AND pinned the app to a mic that stops
                    # being the right one the moment the user plugs in a headset.
                    mic_var.set("Default")
                print(f"[Settings] Mic menu populated with {len(options)} option(s)")
            except Exception as e:
                import traceback
                print(f"[Settings] _populate_mic_menu FAILED: {e}")
                traceback.print_exc()

        # Enumerate on a daemon thread (avoids a 200-500ms block on machines with
        # many audio/Bluetooth devices), but hand the result back via a MAIN-THREAD
        # poller. Calling .after() directly from the worker races the mainloop
        # startup and raises "main thread is not in main loop", which silently
        # dropped the update — the menu stayed stuck on "Default". Scheduling the
        # poller from the main thread here is always safe.
        _mic_devs: list = []
        _mic_done = [False]

        def _enumerate_worker():
            try:
                devs = self._get_input_devices() if self._get_input_devices else []
            except Exception as e:
                print(f"[Settings] Mic enumeration failed: {e}")
                devs = []
            _mic_devs.extend(devs)
            _mic_done[0] = True

        def _poll_mic(attempt=0):
            if _mic_done[0]:
                if not _mic_devs:
                    print("[Settings] No input devices found — dropdown shows Default only")
                _populate_mic_menu(list(_mic_devs))
            elif attempt < 100:                 # up to ~10s of polling
                self._root.after(100, lambda: _poll_mic(attempt + 1))

        threading.Thread(target=_enumerate_worker, daemon=True, name="mic-enum").start()
        self._root.after(150, _poll_mic)

        # ── Mic test button + level meter ─────────────────────────────────────
        btn_row = tk.Frame(mic_card, bg=C["surface"])
        btn_row.pack(fill="x", pady=(8, 0))

        test_btn = RoundedButton(btn_row, text="Test Mic",
                                 fg=C["text"], fill=C["surface_hover"],
                                 font=("Segoe UI", 9), padx=14, pady=6)
        test_btn.pack(side="left", padx=(0, 6))
        test_btn.bind("<Enter>", lambda _e: test_btn.configure(bg=C["accent"], fg=C["bg"]))
        test_btn.bind("<Leave>", lambda _e: test_btn.configure(bg=C["surface_hover"], fg=C["text"]))

        scan_btn = RoundedButton(btn_row, text="Find Best Mic",
                                 fg=C["text"], fill=C["surface_hover"],
                                 font=("Segoe UI", 9), padx=14, pady=6)
        scan_btn.pack(side="left")

        test_status = tk.Label(mic_card, text="", fg=C["subtext"], bg=C["surface"],
                               font=("Segoe UI", 8), anchor="w", wraplength=340)
        test_status.pack(fill="x", pady=(4, 0))
        self._autowrap(test_status)

        meter_cv = tk.Canvas(mic_card, height=6, bg=C["input_bg"], highlightthickness=0)
        meter_fill_id = meter_cv.create_rectangle(0, 0, 0, 6, fill=C["success"], outline="")

        mic_test_active = [False]
        mic_test_opening = [False]
        mic_test_job = [None]
        mic_test_stamp = [0]
        scan_active = [False]

        scan_btn.bind("<Enter>", lambda _e: scan_btn.configure(bg=C["accent"], fg=C["bg"]) if not scan_active[0] else None)
        scan_btn.bind("<Leave>", lambda _e: scan_btn.configure(bg=C["surface_hover"], fg=C["text"]) if not scan_active[0] else None)

        def _poll_meter():
            if not mic_test_active[0] or not self._recorder:
                return
            rms, _ = self._recorder.get_live_levels()
            meter_cv.update_idletasks()
            total_w = max(meter_cv.winfo_width(), 1)
            bar_w = int(min(rms / 0.25, 1.0) * total_w)
            color = C["success"] if rms > 0.06 else (C["accent"] if rms > 0.015 else C["subtext"])
            meter_cv.coords(meter_fill_id, 0, 0, bar_w, 6)
            meter_cv.itemconfigure(meter_fill_id, fill=color)
            mic_test_job[0] = self._root.after(80, _poll_meter)

        def _stop_test():
            mic_test_active[0] = False
            mic_test_opening[0] = False
            mic_test_stamp[0] += 1
            if mic_test_job[0]:
                self._root.after_cancel(mic_test_job[0])
                mic_test_job[0] = None
            if self._recorder:
                threading.Thread(
                    target=self._recorder.stop_monitor, daemon=True,
                    name="mic-monitor-stop").start()
            meter_cv.pack_forget()
            meter_cv.coords(meter_fill_id, 0, 0, 0, 6)
            test_btn.configure(text="Test Mic")
            test_status.configure(text="")

        def _start_test():
            if not self._recorder:
                test_status.configure(text="Recorder unavailable", fg=C["error"])
                return
            if self._recorder.is_recording:
                test_status.configure(text="Stop recording first", fg=C["error"])
                return
            selected = mic_var.get()
            device_name = "" if selected == "Default" else selected
            mic_test_stamp[0] += 1
            _stamp = mic_test_stamp[0]
            mic_test_opening[0] = True
            test_btn.configure(text="Opening…")
            test_status.configure(text="Opening microphone…", fg=C["subtext"])

            def _opened(ok: bool):
                if _stamp != mic_test_stamp[0] or not mic_test_opening[0]:
                    if ok:
                        threading.Thread(
                            target=self._recorder.stop_monitor, daemon=True,
                            name="mic-monitor-stale-stop").start()
                    return
                mic_test_opening[0] = False
                if not ok:
                    test_btn.configure(text="Test Mic")
                    test_status.configure(text="Could not open mic", fg=C["error"])
                    return
                mic_test_active[0] = True
                meter_cv.pack(fill="x", pady=(6, 0))
                test_btn.configure(text="Stop")
                test_status.configure(text="Say something…", fg=C["subtext"])
                self._root.after(
                    5000,
                    lambda: _stop_test()
                    if mic_test_active[0] and mic_test_stamp[0] == _stamp
                    else None,
                )
                _poll_meter()

            def _open_worker():
                try:
                    self._recorder.start_monitor(device_name)
                    ok = True
                except Exception:
                    ok = False
                self._ui_after(0, _opened, ok)

            threading.Thread(
                target=_open_worker, daemon=True, name="mic-monitor-open").start()

        def _toggle_test():
            if mic_test_active[0]:
                _stop_test()
            elif mic_test_opening[0]:
                _stop_test()
            else:
                _start_test()

        test_btn.bind("<Button-1>", lambda _e: _toggle_test())

        # ── Find Best Mic scan ────────────────────────────────────────────────
        def _scan_done(best_name, best_level):
            scan_active[0] = False
            scan_btn.configure(text="Find Best Mic", fg=C["text"],
                               bg=C["surface_hover"], cursor="hand2")
            scan_btn.bind("<Button-1>", lambda _e: _start_scan())
            if best_name and best_level > 0.005:
                short = best_name if len(best_name) <= 30 else best_name[:27] + "…"
                test_status.configure(text=f"Best mic: {short} — applied ✓", fg=C["success"])
                mic_var.set(best_name)
                if self._on_settings_change:
                    self._on_settings_change("input_device", best_name)
            else:
                test_status.configure(
                    text="No signal detected — try speaking louder", fg=C["error"])

        def _run_scan():
            import sounddevice as _sd
            import numpy as _np

            mics_to_test = [d for d in unique_devs if _mic_rank(d) < 2]
            if not mics_to_test:
                self._ui_after(0, lambda: _scan_done(None, 0.0))
                return

            SR = 16000
            chunks = {d["name"]: [] for d in mics_to_test}
            streams = {}

            for d in mics_to_test:
                name = d["name"]
                try:
                    def _cb(indata, frames, t, status, _n=name):
                        chunks[_n].append(indata.copy())
                    stream = _sd.InputStream(
                        samplerate=SR, channels=1, dtype="float32",
                        device=d["index"], callback=_cb, blocksize=1024,
                    )
                    streams[name] = stream
                    stream.start()
                except Exception as e:
                    print(f"[MicScan] Could not open {d['name']}: {e}")

            if not streams:
                self._ui_after(0, lambda: _scan_done(None, 0.0))
                return

            for remaining in range(3, 0, -1):
                self._ui_after(0, lambda r=remaining: test_status.configure(
                    text=f"Recording all mics… {r}s", fg=C["accent"]))
                time.sleep(1.0)

            for stream in streams.values():
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

            results = {}
            for d in mics_to_test:
                name = d["name"]
                data = chunks[name]
                if data:
                    audio = _np.concatenate(data, axis=0).flatten()
                    rms = float(_np.sqrt(_np.mean(audio ** 2)))
                    results[name] = rms
                else:
                    results[name] = 0.0

            if results:
                best = max(results, key=results.get)
                self._ui_after(0, lambda b=best, v=results[best]: _scan_done(b, v))
            else:
                self._ui_after(0, lambda: _scan_done(None, 0.0))

        def _start_scan():
            if not self._recorder or scan_active[0]:
                return
            if self._recorder.is_recording:
                test_status.configure(text="Stop recording first", fg=C["error"])
                return
            if mic_test_active[0]:
                _stop_test()
            mics_to_test = [d for d in unique_devs if _mic_rank(d) < 2]
            if not mics_to_test:
                test_status.configure(text="No microphones found", fg=C["error"])
                return
            scan_active[0] = True
            scan_btn.configure(text="Scanning…", fg=C["subtext"],
                               bg=C["surface"], cursor="")
            scan_btn.unbind("<Button-1>")

            countdown = [3]

            def _tick():
                countdown[0] -= 1
                if countdown[0] > 0:
                    test_status.configure(
                        text=f"Get ready to speak — starting in {countdown[0]}s…",
                        fg=C["accent"])
                    self._root.after(1000, _tick)
                else:
                    test_status.configure(
                        text=f"Speak now! Recording {len(mics_to_test)} mics at once…",
                        fg=C["accent"])
                    threading.Thread(target=_run_scan, daemon=True).start()

            test_status.configure(
                text=f"Get ready to speak — starting in 3s…",
                fg=C["accent"])
            self._root.after(1000, _tick)

        scan_btn.bind("<Button-1>", lambda _e: _start_scan())


        # ── Custom Vocabulary ─────────────────────────────────────────────────
        vocab_card = self._card(parent, margin=(0, 8))
        tk.Label(vocab_card, text="Custom Vocabulary",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")
        _vocab_desc = tk.Label(vocab_card,
                 text="Comma-separated names, acronyms, or terms to boost (e.g. FTC, Salesforce, CRM)",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=320)
        _vocab_desc.pack(fill="x")
        self._autowrap(_vocab_desc)
        current_vocab = (cfg.custom_vocabulary if cfg else "") or ""
        vocab_var = tk.StringVar(value=current_vocab)
        vocab_entry = tk.Entry(vocab_card, textvariable=vocab_var,
                               bg=C["input_bg"], fg=C["text"], insertbackground=C["text"],
                               relief="flat", font=("Segoe UI", 9), bd=6)
        vocab_entry.pack(fill="x", pady=(4, 0))

        # Auto-save on blur/Enter (parity with the API-key fields) so the value
        # isn't silently lost if the user edits it then closes the panel without
        # clicking Save.
        def _save_vocab(_e=None):
            if self._on_settings_change:
                self._on_settings_change("custom_vocabulary", vocab_var.get().strip())
        vocab_entry.bind("<FocusOut>", _save_vocab)
        vocab_entry.bind("<Return>", _save_vocab)

        # ── Sound feedback ────────────────────────────────────────────────────
        sound_card = self._card(parent, margin=(0, 4))
        sound_row = tk.Frame(sound_card, bg=C["surface"])
        sound_row.pack(fill="x")

        current_sound = bool(cfg.sound_feedback if cfg else True)
        sound_var = tk.BooleanVar(value=current_sound)

        # Pack pill first so expand=True on label_col doesn't consume all space
        def _on_sound_toggle(v: bool):
            sound_var.set(v)
            if self._on_settings_change:
                self._on_settings_change("sound_feedback", v)

        sound_pill = TogglePill(
            sound_row, value=current_sound, bg=C["surface"],
            command=_on_sound_toggle,
        )
        sound_pill.pack(side="right")

        label_col = tk.Frame(sound_row, bg=C["surface"])
        label_col.pack(side="left", fill="x", expand=True)
        tk.Label(label_col, text="Sound Feedback",
                 fg=C["text"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(anchor="w")
        _sound_desc = tk.Label(label_col, text="Beeps when recording starts, stops, and transcription finishes",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=260)
        _sound_desc.pack(fill="x")
        self._autowrap(_sound_desc)

        # ── Live captions ─────────────────────────────────────────────────────
        cap_card = self._card(parent, margin=(0, 4))
        cap_row = tk.Frame(cap_card, bg=C["surface"])
        cap_row.pack(fill="x")

        current_caps = bool(getattr(cfg, "live_captions", False) if cfg else False)
        caps_var = tk.BooleanVar(value=current_caps)

        def _on_caps_toggle(v: bool):
            caps_var.set(v)
            if self._on_settings_change:
                self._on_settings_change("live_captions", v)

        caps_pill = TogglePill(
            cap_row, value=current_caps, bg=C["surface"],
            command=_on_caps_toggle,
        )
        caps_pill.pack(side="right")

        caps_col = tk.Frame(cap_row, bg=C["surface"])
        caps_col.pack(side="left", fill="x", expand=True)
        tk.Label(caps_col, text="Live Captions",
                 fg=C["text"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(anchor="w")
        _caps_desc = tk.Label(caps_col, text="Show the words you're saying in real time (replaces the waveform bar while recording)",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=260)
        _caps_desc.pack(fill="x")
        self._autowrap(_caps_desc)

        # ── Behaviour toggles (auto_punctuate, trailing_space, auto_enter) ─────
        def _toggle_card(key: str, title: str, subtext: str, default: bool):
            card = self._card(parent, margin=(0, 4))
            row = tk.Frame(card, bg=C["surface"]); row.pack(fill="x")
            cur = bool(getattr(cfg, key, default) if cfg else default)
            var = tk.BooleanVar(value=cur)
            def _toggle(v: bool, _k=key, _v=var):
                _v.set(v)
                if self._on_settings_change:
                    self._on_settings_change(_k, v)
            TogglePill(row, value=cur, bg=C["surface"], command=_toggle).pack(side="right")
            col = tk.Frame(row, bg=C["surface"]); col.pack(side="left", fill="x", expand=True)
            tk.Label(col, text=title, fg=C["text"], bg=C["surface"],
                     font=("Segoe UI", 9), anchor="w").pack(anchor="w")
            desc = tk.Label(col, text=subtext, fg=C["subtext"], bg=C["surface"],
                     font=("Segoe UI", 8), anchor="w", justify="left",
                     wraplength=260)
            desc.pack(fill="x")
            self._autowrap(desc)

        _toggle_card("auto_punctuate", "Auto Punctuation",
                     "Add a trailing period when speech ends without ending punctuation", True)
        _toggle_card("trailing_space", "Add Trailing Space",
                     "Append a space after each injection (useful for mid-sentence dictation)", False)
        _toggle_card("auto_enter", "Press Enter After Insert",
                     "Send Enter after injecting (useful for chat / search boxes)", False)
        _toggle_card("live_inject", "Live Typing (Beta)",
                     "Type each word into the app as you speak instead of all at once. "
                     "Self-corrects when you finish. Available for English dictation.", False)
        _toggle_card("warm_mic", "Instant Mic Start",
                     "Keep the microphone warm so recording starts instantly and the "
                     "first word is never clipped (mic indicator stays on; audio is "
                     "only kept for 1.5s and never stored)", True)


        # ── Account card ──────────────────────────────────────────────────────
        acct_card = self._card(parent, margin=(0, 4))
        tk.Label(acct_card, text="Account",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")

        acct_row = tk.Frame(acct_card, bg=C["surface"])
        acct_row.pack(fill="x", pady=(6, 0))

        self._settings_email_lbl = tk.Label(
            acct_row,
            text=self._auth.user_email or "Not signed in",
            fg=C["text"], bg=C["surface"],
            font=("Segoe UI", 10), anchor="w",
        )
        self._settings_email_lbl.pack(side="left", fill="x", expand=True)

        self._settings_auth_btn = tk.Label(
            acct_row,
            text="Sign Out",
            fg=C["error"], bg=C["surface"],
            font=("Segoe UI", 9), cursor="hand2", anchor="e",
        )
        self._settings_auth_btn.pack(side="right")
        self._settings_auth_btn.bind("<Button-1>", lambda _e: self._do_sign_action())
        self._settings_auth_btn.bind("<Enter>",
            lambda _e: self._settings_auth_btn.configure(
                fg="#ff8888" if self._auth.user_email else C["accent_hover"]))
        self._settings_auth_btn.bind("<Leave>",
            lambda _e: self._settings_auth_btn.configure(
                fg=C["error"] if self._auth.user_email else C["accent"]))

        # ── Save button ───────────────────────────────────────────────────────
        save_wrap = tk.Frame(parent, bg=C["bg"])
        save_wrap.pack(fill="x", padx=20, pady=(12, 8))

        self._settings_status = tk.Label(save_wrap, text="",
                                         fg=C["success"], bg=C["bg"],
                                         font=("Segoe UI", 9))
        self._settings_status.pack(side="left")

        def _save(_e=None):
            if self._on_settings_change:
                mic_val = mic_var.get()
                self._on_settings_change("input_device",
                                         "" if mic_val == "Default" else mic_val)
                self._on_settings_change("custom_vocabulary", vocab_var.get().strip())
                self._on_settings_change("sound_feedback", sound_var.get())
            self._settings_status.configure(text="Saved ✓", fg=C["success"])
            if self._root:
                self._root.after(4000, lambda: self._settings_status.configure(text=""))

        save_btn = self._surface_btn(save_wrap, "Save Settings", _save)
        save_btn.pack(side="right")

    # ── Update banner / toast ─────────────────────────────────────────────────

    def show_toast(self, message: str, duration_ms: int = 5000) -> None:
        """Thread-safe transient notification (bottom-right, auto-dismisses)."""
        if not self._root:
            return
        self._ui_after(0, lambda: show_toast(self._root, message, duration_ms))

    def set_update_status(self, text: str) -> None:
        """Thread-safe update of the status label in the Settings version card."""
        def _apply():
            lbl = getattr(self, "_update_status_lbl", None)
            if lbl is not None:
                try:
                    lbl.configure(text=text, fg=C["accent"])
                except tk.TclError:
                    pass
        self._ui_after(0, _apply)

    def show_update_banner(self, version: str, download_url: str,
                           auto: bool = False) -> None:
        """Show update banner at top of Settings and an inline button in the version card.
        With auto=True the wording reflects that the update installs itself; the
        button stays as a manual "restart now" override."""
        if not self._root:
            return


        # Instance-level, not per-banner: re-opening the banner (a second
        # "Check for Updates") must not mint a fresh guard while a download
        # thread from the previous banner is still running.
        if not hasattr(self, "_update_in_flight"):
            self._update_in_flight = False

        def _set_downloading():
            self._update_in_flight = True
            for btn in _btns:
                try:
                    btn.configure(text="Downloading…", bg=C["subtext"], cursor="")
                    btn.unbind("<Button-1>")
                    btn.unbind("<Enter>")
                    btn.unbind("<Leave>")
                except Exception:
                    pass

        def _reset_btns():
            # Re-enable the button(s) after a failed attempt so the user can retry.
            self._update_in_flight = False
            for btn in _btns:
                try:
                    if not btn.winfo_exists():
                        continue
                    btn.configure(text="Update Now", bg=C["accent"], cursor="hand2")
                    btn.bind("<Button-1>", _do_update)
                    btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=C["accent_hover"]))
                    btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=C["accent"]))
                except Exception:
                    pass

        def _do_update(_e=None):
            if self._update_in_flight:
                return
            from updater import run_auto_update, current_exe_path
            import threading
            exe_path = current_exe_path()
            if not exe_path:
                # Running from source — no frozen exe to swap; open the release page.
                import webbrowser
                webbrowser.open(download_url)
                return
            # Set synchronously — the after(0) UI update alone left a gap where
            # a second click (from a re-opened banner) started a duplicate run.
            self._update_in_flight = True
            self._root.after(0, _set_downloading)

            def _status(msg):
                self._ui_after(0, lambda m=msg: [
                    b.configure(text=(m or "Downloading…"))
                    for b in _btns if b.winfo_exists()
                ])

            def _log_event(stage, ok, detail):
                if self._db:
                    self._db.log_update_event(
                        stage, from_version=self._version or "",
                        to_version=version, ok=ok, detail=detail)

            def _worker():
                # Drive the exact same proven flow as the automatic updater
                # (reliable LOCALAPPDATA download + retries + verified swap script)
                # instead of a separate hand-rolled download. is_idle=True +
                # idle_samples=1 makes it apply immediately, since the user asked
                # to update NOW rather than waiting for the next idle window.
                run_auto_update(
                    version, download_url, exe_path,
                    is_idle=lambda: True,
                    on_status=_status,
                    poll_interval=0.0,
                    idle_samples=1,
                    on_event=_log_event,
                )
                # run_auto_update only returns when the download failed after all
                # retries (on success apply_update replaces the exe and exits the
                # process). Fall back to opening the release page in the browser —
                # a genuine last resort so the user is never fully stuck.
                _log_event("manual_fallback_browser", False, "in-place download failed")
                from error_reporter import report_error
                report_error(
                    f"Manual 'Update Now' failed to download v{version}",
                    context={"version": version, "url": download_url},
                    user_email=getattr(self._auth, "user_email", None),
                )
                import webbrowser
                webbrowser.open(download_url)
                self._ui_after(0, _reset_btns)

            threading.Thread(target=_worker, daemon=True, name="in-app-update").start()

        _btns = []

        # ── Top banner ────────────────────────────────────────────────────────
        if hasattr(self, "_update_banner_frame"):
            for w in self._update_banner_frame.winfo_children():
                w.destroy()

            card = self._card(self._update_banner_frame, margin=(0, 4))

            banner_text = (
                f"Update {version} is ready and will install automatically "
                "when FTC Whisper is idle."
                if auto else f"Update {version} is ready to install."
            )
            _banner_lbl = tk.Label(
                card,
                text=banner_text,
                fg=C["accent"], bg=C["surface"],
                font=("Segoe UI", 10, "bold"), anchor="w", justify="left",
                wraplength=320,
            )
            _banner_lbl.pack(fill="x")
            self._autowrap(_banner_lbl)

            banner_btn = RoundedButton(
                card,
                text="Update Now",
                fg=C["bg"], fill=C["accent"],
                font=("Segoe UI", 9, "bold"),
                padx=14, pady=5,
            )
            banner_btn.pack(anchor="e", pady=(8, 0))
            banner_btn.bind("<Button-1>", _do_update)
            banner_btn.bind("<Enter>", lambda _e: banner_btn.configure(bg=C["accent_hover"]))
            banner_btn.bind("<Leave>", lambda _e: banner_btn.configure(bg=C["accent"]))
            _btns.append(banner_btn)

        # ── Inline button in version card ─────────────────────────────────────
        if hasattr(self, "_ver_update_row"):
            for w in self._ver_update_row.winfo_children():
                w.destroy()

            ver_btn = RoundedButton(
                self._ver_update_row,
                text="Update Now",
                fg=C["bg"], fill=C["accent"],
                font=("Segoe UI", 9, "bold"),
                padx=14, pady=5,
            )
            ver_btn.pack(side="right")
            ver_btn.bind("<Button-1>", _do_update)
            ver_btn.bind("<Enter>", lambda _e: ver_btn.configure(bg=C["accent_hover"]))
            ver_btn.bind("<Leave>", lambda _e: ver_btn.configure(bg=C["accent"]))
            _btns.append(ver_btn)

            self._ver_update_row.pack(fill="x", pady=(8, 0))

        # The banner just grew the settings page. Re-measure the scroll range
        # once layout settles — with the stale region the page bottom stayed
        # cut off by exactly the banner's height.
        cv = getattr(self, "_settings_cv", None)
        if cv is not None:
            self._queue_scrollregion_sync(cv)
            self._repaint(cv)

    # ── Auth callbacks ────────────────────────────────────────────────────────

    def _fire_authenticated(self) -> None:
        threading.Thread(
            target=self._on_authenticated, args=(self._auth,), daemon=True
        ).start()

    def _start_session_restore_retry(self) -> None:
        """Retry restoring a saved session in the background (see run())."""
        threading.Thread(
            target=self._session_restore_retry_loop,
            daemon=True, name="session-restore-retry",
        ).start()

    def _session_restore_retry_loop(self) -> None:
        import time
        # This loop IS the startup session restore (main() no longer blocks on
        # it — the network token refresh used to delay first paint by seconds).
        # First attempt fires immediately; retries continue for ~5 min to cover
        # a slow Wi-Fi reconnect after a cold boot, but bounded so we don't spin
        # forever. Stops early if the session becomes valid, gets cleared
        # (definitive auth failure), or the user signs in manually meanwhile.
        for attempt in range(30):
            if attempt:
                time.sleep(10)
            if self._auth.is_authenticated:
                return
            if not self._auth.has_saved_session():
                return  # file cleared → invalid session, leave login up
            try:
                if self._auth.try_restore_session():
                    if self._root:
                        self._root.after(0, self._promote_restored_session)
                    return
            except Exception:
                pass

    def _promote_restored_session(self) -> None:
        """Main-thread: a background retry restored the session — switch to the
        dashboard and boot services, exactly as a startup restore would have."""
        if not self._auth.is_authenticated:
            return
        self._switch_to_dashboard()
        self._fire_authenticated()

    def _do_sign_action(self) -> None:
        if self._auth.is_authenticated and self._auth.user_email:
            self._do_sign_out()
        else:
            self._do_sign_in()

    def _do_sign_out(self) -> None:
        if not (self._auth.is_authenticated and self._auth.user_email):
            return
        import tkinter.messagebox as mb
        if not mb.askyesno("Sign Out", "Are you sure you want to sign out?",
                           parent=self._root):
            return
        self._on_sign_out()
        self._switch_to_login()

    def _show_login_screen(self, after_login=None, after_cancel=None) -> None:
        """Hide main window, show login as the primary screen, restore on success."""
        from login_window import LoginWindow, WINDOW_W as LW, WINDOW_H as LH
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        # Set root geometry to login size at screen center before withdrawing so
        # LoginWindow can position its Toplevel correctly relative to the parent.
        self._root.geometry(f"{LW}x{LH}+{(sw - LW) // 2}+{(sh - LH) // 2}")
        self._root.withdraw()

        def _on_success(auth):
            self._root.deiconify()
            self._show_dashboard()
            self._apply_auth_ui()
            self._fire_authenticated()
            if after_login:
                after_login(auth)

        def _on_cancel():
            if after_cancel:
                after_cancel()
            else:
                self._do_quit()

        LoginWindow(self._auth, on_success=_on_success, on_cancel=_on_cancel).run(parent=self._root)

    def _apply_auth_ui(self) -> None:
        """Synchronise every account control with the live auth state."""
        email = self._auth.user_email or ""
        signed_in = bool(self._auth.is_authenticated and email)
        account_text = email if signed_in else "Not signed in"
        action_text = "Sign Out" if signed_in else "Sign In"
        if hasattr(self, "_email_display"):
            self._email_display.configure(text=account_text)
        if hasattr(self, "_settings_email_lbl"):
            self._settings_email_lbl.configure(text=account_text)
        if hasattr(self, "_sign_btn"):
            self._sign_btn.configure(text=action_text)
        if hasattr(self, "_settings_auth_btn"):
            self._settings_auth_btn.configure(
                text=action_text, fg=C["error"] if signed_in else C["accent"])

    def _do_sign_in(self) -> None:
        from login_window import LoginWindow

        def _on_success(auth):
            email = auth.user_email or ""
            self._email_display.configure(text=email if email else "Not signed in")
            self._sign_btn.configure(text="Sign Out" if email else "Sign In")
            if self._on_sign_in:
                threading.Thread(target=self._on_sign_in, args=(auth,), daemon=True).start()

        LoginWindow(self._auth, on_success=_on_success).run(parent=self._root)

    def _do_quit(self) -> None:
        self._on_quit()
        if self._root:
            self._root.destroy()

    def _hide(self) -> None:
        self._root.withdraw()

    def _resize(self, w: int, h: int) -> None:
        # Record the programmatic size FIRST: the geometry call fires
        # <Configure> synchronously and _on_root_configure must not read it
        # as a user drag.
        self._applied_size = (w, h)
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x  = (sw - w) // 2
        y  = (sh - h) // 2
        self._root.geometry(f"{w}x{h}+{x}+{y}")

    # ── Rounded card ─────────────────────────────────────────────────────────

    def _card(self, parent: tk.Frame, inner_pad=(18, 14),
              radius: int = 10, margin=(0, 8)) -> tk.Frame:
        """Return an inner Frame sitting inside a rounded-corner Canvas card.

        Geometry sync is coalesced to ONE after_idle pass per layout change.
        The old handler called update_idletasks() inside <Configure>, which
        re-entered the layout engine mid-blit and was the direct cause of the
        torn/ghosted settings page (duplicated rows, stray floating labels).
        """
        cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0)
        cv.pack(fill="x", padx=20, pady=margin)
        px, py = inner_pad
        inner = tk.Frame(cv, bg=C["surface"])
        wid = cv.create_window(px, py, window=inner, anchor="nw")
        state = {"job": None, "last": (0, 0)}

        def _sync_now():
            state["job"] = None
            try:
                cw = cv.winfo_width()
                fh = inner.winfo_reqheight()
            except tk.TclError:
                return
            if cw < 2 or fh < 2:
                return
            if (cw, fh) == state["last"]:
                return  # settled — skip the redraw and stop the Configure echo
            state["last"] = (cw, fh)
            ch = fh + 2 * py
            cv.configure(height=ch)
            cv.coords(wid, px, py)
            cv.itemconfigure(wid, width=max(1, cw - 2 * px))
            cv.delete("bg")
            _rr(cv, 0, 0, cw - 1, ch - 1, radius,
                fill=C["surface"], outline=C["border"], tags="bg")
            cv.tag_lower("bg")

        def sync(_=None):
            if state["job"] is None:
                try:
                    state["job"] = cv.after_idle(_sync_now)
                except tk.TclError:
                    pass

        cv.bind("<Configure>", sync)
        inner.bind("<Configure>", sync)
        return inner

    # ── Widget helpers ────────────────────────────────────────────────────────

    def _surface_btn(self, parent, text, cmd) -> RoundedButton:
        btn = RoundedButton(
            parent, text=text, command=cmd,
            fg=C["text"], fill=C["surface_hover"],
            font=("Segoe UI", 10), padx=16, pady=8,
        )
        btn.bind("<Enter>", lambda _e: btn.configure(bg=C["accent"], fg=C["bg"]))
        btn.bind("<Leave>", lambda _e: btn.configure(bg=C["surface_hover"], fg=C["text"]))
        return btn

    @staticmethod
    def _autowrap(lbl: tk.Label, pad: int = 4) -> tk.Label:
        """Keep a Label's wraplength tracking its real width so text reflows
        when the window is resized (fixed wraplengths either clipped at narrow
        widths or wasted space at wide ones). The label must be packed with
        fill='x' so its width follows the card."""
        state = {"w": 0}

        def _on_cfg(e):
            w = max(e.width - pad, 60)
            if abs(w - state["w"]) > 2:
                state["w"] = w
                try:
                    lbl.configure(wraplength=w)
                except tk.TclError:
                    pass

        lbl.bind("<Configure>", _on_cfg)
        return lbl

    def _ghost_btn(self, parent, text, cmd) -> tk.Label:
        btn = tk.Label(
            parent, text=text,
            fg=C["subtext"], bg=C["bg"],
            font=("Segoe UI", 9), cursor="hand2",
        )
        btn.bind("<Button-1>", lambda _e: cmd())
        btn.bind("<Enter>",    lambda _e: btn.configure(fg=C["text"]))
        btn.bind("<Leave>",    lambda _e: btn.configure(fg=C["subtext"]))
        return btn
