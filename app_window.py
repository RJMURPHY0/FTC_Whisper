"""
FTC Whisper — Main application window.

Dashboard: Home / Hotkey / History tabs.
Dark theme with rounded-corner cards via Canvas.
"""

import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
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
DASH_H   = 560


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
        self.delete("all")
        # Fully visible content → no thumb (nothing to scroll)
        if self._first <= 0.0 and self._last >= 1.0:
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
        self._round_rect(pad, y0, w - pad, y1, r, fill=color)

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
        self._config                  = config
        self._get_input_devices       = get_input_devices
        self._recorder                = recorder
        self._transcriber             = transcriber
        self._hotkey                  = hotkey.upper()
        self._refine_hotkey           = refine_hotkey.upper()
        self._root: Optional[tk.Tk] = None

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
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._hide)

        self._apply_dark_titlebar()

        try:
            self._build_header()

            self._dash_frame = tk.Frame(self._root, bg=C["bg"])
            self._build_dashboard(self._dash_frame)

            self._login_frame = tk.Frame(self._root, bg=C["bg"])
            self._build_embedded_login()

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
        self._sign_btn = self._ghost_btn(footer, "Sign Out", self._do_sign_out)
        self._sign_btn.pack(side="right")

        tk.Frame(parent, bg=C["divider"], height=1).pack(fill="x", before=footer)

        self._switch_dash_tab("home")

    def _switch_dash_tab(self, name: str) -> None:
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

        # Bind scroll to the appropriate scrollable area
        if name == "history":
            # Always refetch — a cached list goes stale as soon as the next
            # dictation lands, which reads as "history not working".
            self._load_history()
            if self._root:
                self._root.bind_all("<MouseWheel>", self._hist_scroll)
        elif name == "settings":
            if self._root and hasattr(self, "_settings_cv"):
                self._root.bind_all("<MouseWheel>", lambda e: self._settings_cv.yview_scroll(
                    int(-1 * (e.delta / 40)), "units"))
            if hasattr(self, "_update_check_btn") and hasattr(self, "_do_update_check"):
                # Don't clobber an in-flight check — resetting the label here
                # both swallowed the pending result and re-armed the button for
                # overlapping checks.
                if self._update_check_btn.cget("text") != "Checking...":
                    self._update_check_btn.configure(
                        text="Check for Updates", fg=C["accent"], cursor="hand2")
                    self._update_check_btn.bind("<Button-1>", self._do_update_check)
        elif name == "hotkey":
            if self._root and hasattr(self, "_hk_cv"):
                self._root.bind_all("<MouseWheel>", lambda e: self._hk_cv.yview_scroll(
                    int(-1 * (e.delta / 40)), "units"))
        else:
            if self._root:
                try:
                    self._root.unbind_all("<MouseWheel>")
                except Exception:
                    pass

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
        self._resize(WINDOW_W, DASH_H)
        if hasattr(self, "_email_display"):
            self._email_display.configure(text=self._auth.user_email or "")

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

        # Instructions card
        ic = self._card(parent, margin=(0, 0))
        _ic_mode = getattr(self._config, "mode", "hold") if self._config else "hold"
        _ic_text = (
            "Hold the hotkey and speak.\nRelease to transcribe into your cursor."
            if _ic_mode != "toggle" else
            "Press the hotkey to start recording.\nPress again to stop and transcribe."
        )
        self._home_instructions_lbl = tk.Label(
            ic, text=_ic_text,
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10), justify="left", anchor="w",
        )
        self._home_instructions_lbl.pack(fill="x")

    # ── Hotkey tab ────────────────────────────────────────────────────────────

    def _build_hotkey_tab(self, parent: tk.Frame) -> None:
        # Scrollable container
        self._hk_cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0)
        _hk_cv = self._hk_cv
        _hk_sb = ModernScrollbar(parent, command=_hk_cv.yview)
        _hk_cv.configure(yscrollcommand=_hk_sb.set)
        _hk_sb.pack(side="right", fill="y")
        _hk_cv.pack(side="left", fill="both", expand=True)
        _hk_inner = tk.Frame(_hk_cv, bg=C["bg"])
        _hk_win = _hk_cv.create_window(0, 0, window=_hk_inner, anchor="nw")
        _hk_inner.bind("<Configure>", lambda _e: _hk_cv.configure(
            scrollregion=_hk_cv.bbox("all")))
        _hk_cv.bind("<Configure>", lambda e: _hk_cv.itemconfigure(
            _hk_win, width=e.width))
        _hk_cv.bind("<MouseWheel>", lambda e: _hk_cv.yview_scroll(
            int(-1 * (e.delta / 40)), "units"))
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

        self._hotkey_record_msg = tk.Label(
            card1,
            text="Press  Change Shortcut  then press any key combo (e.g. F9, Alt+V).",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340,
        )
        self._hotkey_record_msg.pack(fill="x", pady=(0, 8))

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

        # Compact toggle switch — inline with the buttons
        _mode_right = tk.Frame(btn_row, bg=C["surface"])
        _mode_right.pack(side="right")

        _init_mode = getattr(self._config, "mode", "hold") if self._config else "hold"
        self._mode_toggle_on = (_init_mode == "toggle")

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
            if hasattr(self, "_home_mode_hint_lbl"):
                self._home_mode_hint_lbl.configure(
                    text=" press to start/stop" if new_val else " hold to dictate"
                )
            if hasattr(self, "_home_instructions_lbl"):
                self._home_instructions_lbl.configure(
                    text=(
                        "Press the hotkey to start recording.\nPress again to stop and transcribe."
                        if new_val else
                        "Hold the hotkey and speak.\nRelease to transcribe into your cursor."
                    )
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

    def _toggle_hotkey_recording(self) -> None:
        if self._recording_hotkey:
            self._stop_hotkey_recording(cancelled=True)
        else:
            self._start_hotkey_recording()

    def _start_hotkey_recording(self) -> None:
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
                text="Press  Change Shortcut  then press any key combo (e.g. F9, Alt+V).",
                fg=C["subtext"],
            )
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

        self._ghost_btn(top, "↻ Refresh", self._load_history).pack(side="right")
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
            self._render_history()

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
            card_cv.update_idletasks()
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
            card_cv.update_idletasks()
            cw, ch = card_cv.winfo_width(), card_cv.winfo_height()
            if cw > 2 and ch > 2:
                card_cv.itemconfigure(card_win, width=cw - 2, height=ch - 2)
            _redraw_card()

        card_cv.bind("<Configure>", _sync_card)

        # Scrollable list canvas — fills the whole card (no scrollbar inside now)
        self._hist_cv = tk.Canvas(card_inner, bg=C["surface"],
                                  highlightthickness=0, bd=0)
        self._hist_cv.pack(fill="both", expand=True)

        # External scrollbar on the window background, to the RIGHT of the card.
        # Packed (side=right) before the card is packed (side=left, expand) so it
        # claims the right edge; the card then fills the remaining width.
        self._hist_sb = ModernScrollbar(mid, command=self._hist_cv.yview)
        self._hist_cv.configure(yscrollcommand=self._hist_sb.set)
        self._hist_sb.pack(side="right", fill="y")
        # Right pad 8 so the card's right edge lines up with the search bar (W-20),
        # while the 12px scrollbar sits flush at the window edge just beyond it.
        card_cv.pack(side="left", fill="both", expand=True, padx=(0, 8))

        # Inner frame that holds one Frame per history row
        self._hist_items = tk.Frame(self._hist_cv, bg=C["surface"])
        self._hist_items_win = self._hist_cv.create_window(
            0, 0, window=self._hist_items, anchor="nw")

        self._hist_items.bind("<Configure>", lambda _e: self._hist_cv.configure(
            scrollregion=self._hist_cv.bbox("all")))
        self._hist_cv.bind("<Configure>", lambda e: self._hist_cv.itemconfigure(
            self._hist_items_win, width=e.width))


    def _hist_scroll(self, event) -> None:
        if hasattr(self, "_hist_cv"):
            self._hist_cv.yview_scroll(int(-1 * (event.delta / 40)), "units")

    def _load_history(self) -> None:
        if getattr(self, "_history_loading", False):
            return  # a fetch is already in flight (rapid tab switching)
        self._history_loading = True
        self._hist_set_placeholder("Loading…")
        def _fetch():
            try:
                items = self._db.fetch_history(limit=100) if self._db else []
            except Exception as e:
                print(f"[AppWindow] History fetch failed: {e}")
                items = []
            self._ui_after(0, self._populate_history, items)
        threading.Thread(target=_fetch, daemon=True).start()

    def _hist_set_placeholder(self, msg: str) -> None:
        for w in self._hist_items.winfo_children():
            w.destroy()
        tk.Label(self._hist_items, text=msg,
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 10, "italic"),
                 padx=12, pady=16).pack(fill="x")

    def _populate_history(self, items: list) -> None:
        self._history_loading = False
        self._hist_all = items or []
        self._render_history()

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
        for w in self._hist_items.winfo_children():
            w.destroy()
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
            return

        last_group = None
        first_in_group = True
        for item in items:
            raw_ts = item.get("created_at") or ""
            try:
                dt = datetime.fromisoformat(
                    raw_ts.replace("Z", "+00:00")).astimezone()
                group = self._hist_date_label(dt)
            except Exception:
                dt, group = None, "Earlier"
            if group != last_group:
                tk.Label(self._hist_items, text=group.upper(),
                         fg=C["subtext"], bg=C["surface"],
                         font=("Segoe UI", 8, "bold"), anchor="w",
                         ).pack(fill="x", padx=14,
                                pady=((12 if last_group is None else 14), 4))
                last_group = group
                first_in_group = True
            self._make_history_row(item, dt, first_in_group)
            first_in_group = False

    def _make_history_row(self, item: dict, dt, first_in_group: bool) -> None:
        text = item.get("refined_text") or item.get("transcribed_text") or ""
        time_str = dt.strftime("%H:%M") if dt else ""
        app_name = item.get("app_name") or ""
        app_exe = item.get("app_exe") or ""

        if not first_in_group:
            tk.Frame(self._hist_items, bg=C["divider"], height=1).pack(
                fill="x", padx=10)

        row = tk.Frame(self._hist_items, bg=C["surface"])
        row.pack(fill="x")

        header = tk.Frame(row, bg=C["surface"])
        # pady 4 (was 8): the 36px icon now spans the row's vertical space that
        # the old padding used to occupy, so the row height is unchanged (44px)
        # and the two-line text block keeps the same distance to the row edge.
        header.pack(fill="x", padx=10, pady=4)

        # App icon (real exe icon; generic tile for pre-capture rows).
        # Two variants per exe — normal and hover row background.
        # Fidelity order: real bundled brand logo (only way to get the correct
        # icon for a browser-hosted web app) → real exe icon (native apps) →
        # coloured monogram → generic tile.
        icon_n = (get_brand_icon(app_name, C["surface"])
                  or get_app_icon(app_exe, C["surface"])
                  or get_monogram_icon(app_name, C["surface"])
                  or get_fallback_icon(C["surface"]))
        icon_h = (get_brand_icon(app_name, C["surface_hover"])
                  or get_app_icon(app_exe, C["surface_hover"])
                  or get_monogram_icon(app_name, C["surface_hover"])
                  or get_fallback_icon(C["surface_hover"]))
        icon_lbl = tk.Label(header, bg=C["surface"])
        if icon_n is not None:
            icon_lbl.configure(image=icon_n)
            icon_lbl._image_refs = (icon_n, icon_h)  # keep tk references alive
        icon_lbl.pack(side="left", padx=(0, 10))

        # Right side packed first so the middle column can never push it off.
        copy_cv = tk.Canvas(header, width=22, height=22, bg=C["surface"],
                            highlightthickness=0, bd=0, cursor="hand2")
        copy_cv.pack(side="right", padx=(6, 0))

        copy_state = {"bg": C["surface"], "fg": C["subtext"], "busy": False}

        def _draw_copy():
            if copy_state["busy"]:
                return
            copy_cv.delete("all")
            copy_cv.configure(bg=copy_state["bg"])
            _rr(copy_cv, 8, 3, 18, 13, 3, fill="", outline=copy_state["fg"])
            _rr(copy_cv, 4, 7, 14, 17, 3, fill=copy_state["bg"],
                outline=copy_state["fg"])
        _draw_copy()

        def _copied_feedback():
            copy_state["busy"] = True
            copy_cv.delete("all")
            copy_cv.create_line(5, 12, 9, 16, 17, 6, fill=C["success"],
                                width=2, capstyle="round", joinstyle="round")
            def _restore():
                copy_state["busy"] = False
                _draw_copy()
            copy_cv.after(1400, _restore)

        copy_cv.bind("<Button-1>",
                     lambda _e, t=text: (self._copy_to_clipboard(t),
                                         _copied_feedback()))
        copy_cv.bind("<Enter>", lambda _e: (copy_state.update(fg=C["accent"]),
                                            _draw_copy()))
        copy_cv.bind("<Leave>", lambda _e: (copy_state.update(fg=C["subtext"]),
                                            _draw_copy()))

        # Timestamp (display only).
        time_lbl = tk.Label(header, text=time_str, fg=C["subtext"],
                            bg=C["surface"], font=("Segoe UI", 8), width=5)
        time_lbl.pack(side="right", padx=(6, 0))
        confirming = [False]

        # Delete control — a clean line-art trash-can (drawn, not an emoji). It
        # is HIDDEN by default and appears (left of the timestamp) only while the
        # row is hovered. Clicking it expands the row so the full text is visible
        # and drops a clear "Delete this transcription?  Delete / Cancel" bar
        # underneath. Delete removes the row from the UI immediately (the
        # Supabase row follows 30 days later).
        del_cv = tk.Canvas(header, width=22, height=22, bg=C["surface"],
                           highlightthickness=0, bd=0, cursor="hand2")
        # NOT packed here — on hover it REPLACES the timestamp in its slot.
        del_state = {"bg": C["surface"], "fg": C["subtext"]}

        def _draw_trash():
            del_cv.delete("all")
            del_cv.configure(bg=del_state["bg"])
            c = del_state["fg"]
            # handle + lid
            del_cv.create_line(8, 5, 14, 5, fill=c, width=2, capstyle="round")
            del_cv.create_line(4, 7, 18, 7, fill=c, width=2, capstyle="round")
            # bucket body (rounded bottom)
            del_cv.create_line(6, 8, 7, 17, 15, 17, 16, 8, fill=c, width=2,
                               capstyle="round", joinstyle="round")
            # inner stripes
            for x in (9, 11, 13):
                del_cv.create_line(x, 9, x, 15, fill=c, width=1,
                                   capstyle="round")
        _draw_trash()

        def _show_bin():
            # Bin REPLACES the timestamp in its slot (packed right after the
            # copy icon so it keeps the timestamp's priority over the text
            # column — this is why it shows even on long-text rows, where a
            # last-packed widget would be clipped out of the full header).
            if confirming[0] or del_cv.winfo_ismapped():
                return
            time_lbl.pack_forget()
            del_cv.pack(side="right", padx=(6, 0), after=copy_cv)

        def _hide_bin():
            if del_cv.winfo_ismapped():
                del_cv.pack_forget()
            if not confirming[0] and not time_lbl.winfo_ismapped():
                time_lbl.pack(side="right", padx=(6, 0), after=copy_cv)

        mid = tk.Frame(header, bg=C["surface"])
        mid.pack(side="left", fill="x", expand=True)

        preview = (text[:64] + "…") if len(text) > 64 else text
        prev_lbl = tk.Label(mid, text=preview, fg=C["text"], bg=C["surface"],
                            font=("Segoe UI", 9), anchor="w", justify="left")
        prev_lbl.pack(fill="x")

        app_lbl = None
        if app_name:
            app_lbl = tk.Label(mid, text=app_name, fg=C["subtext"],
                               bg=C["surface"], font=("Segoe UI", 8),
                               anchor="w", justify="left")
            app_lbl.pack(fill="x")

        # Expanded detail — full text below the header, toggled by clicking the row
        detail = tk.Frame(row, bg=C["surface"])
        detail_lbl = tk.Label(detail, text=text, fg=C["text"], bg=C["surface"],
                              font=("Segoe UI", 9), anchor="w", justify="left",
                              wraplength=310)
        detail_lbl.pack(fill="x", padx=(46, 10), pady=(0, 10))
        expanded = [False]

        def _expand():
            if expanded[0]:
                return
            prev_lbl.pack_forget()      # avoid showing the text twice
            detail.pack(fill="x", after=header)
            expanded[0] = True

        def _collapse():
            if not expanded[0]:
                return
            detail.pack_forget()
            if app_lbl is not None:
                prev_lbl.pack(fill="x", before=app_lbl)
            else:
                prev_lbl.pack(fill="x")
            expanded[0] = False

        def _toggle(_e=None):
            if confirming[0]:
                return  # ignore row clicks while the confirm bar is open
            _collapse() if expanded[0] else _expand()

        # Inline confirm controls — clicking the bin shows "Cancel  Delete" in
        # the header, just LEFT of the timestamp. They're packed with the
        # timestamp's layout priority so they never clip on long-text rows; the
        # row still expands so the full text being deleted stays visible.
        cancel_lbl = tk.Label(header, text="Cancel", fg=C["subtext"],
                              bg=C["surface"], font=("Segoe UI", 9),
                              cursor="hand2")

        def _do_delete(_e=None):
            def _worker():
                try:
                    if self._db:
                        self._db.delete_transcription(item)
                except Exception as e:
                    print(f"[History] Delete failed: {e}")
            threading.Thread(target=_worker, daemon=True).start()
            try:
                self._hist_all.remove(item)
            except ValueError:
                pass
            self._render_history()

        del_btn = RoundedButton(header, text="Delete", fg=C["bg"],
                                fill=C["error"], font=("Segoe UI", 9, "bold"),
                                padx=12, pady=4, command=_do_delete)

        def _cancel(_e=None):
            confirming[0] = False
            del_btn.pack_forget()
            cancel_lbl.pack_forget()
            _collapse()
            _set_bg(C["surface"])

        cancel_lbl.bind("<Button-1>", _cancel)

        def _start_confirm(_e=None):
            if confirming[0]:
                return
            _hide_bin()                    # bin away, timestamp back in its slot
            confirming[0] = True
            _expand()                      # show all the text being deleted
            # Order left→right: Cancel, Delete, timestamp. Packed after the
            # timestamp so they land immediately to its left with priority.
            del_btn.pack(side="right", after=time_lbl, padx=(0, 6))
            cancel_lbl.pack(side="right", after=del_btn, padx=(0, 8))
            _set_bg(C["surface"])          # drop the hover highlight

        del_cv.bind("<Button-1>", _start_confirm)

        hover_widgets = [row, header, icon_lbl, mid, prev_lbl, time_lbl,
                         detail, detail_lbl, cancel_lbl] \
            + ([app_lbl] if app_lbl else [])

        def _set_bg(bg: str):
            if confirming[0]:
                bg = C["surface"]          # no hover highlight during confirm
            hover = (bg == C["surface_hover"])
            for w in hover_widgets:
                try:
                    w.configure(bg=bg)
                except tk.TclError:
                    return  # row destroyed mid-hover (refresh/search)
            if icon_n is not None:
                icon_lbl.configure(image=icon_h if hover else icon_n)
            copy_state["bg"] = bg
            _draw_copy()
            del_state["bg"] = bg
            _draw_trash()
            # The bin replaces the timestamp only while the row is hovered
            # (and not mid-confirm).
            if not confirming[0]:
                _show_bin() if hover else _hide_bin()

        def _on_enter(_e=None):
            _set_bg(C["surface_hover"])

        def _on_leave(_e=None):
            # Enter/Leave also fire when crossing into child widgets — only
            # un-hover once the pointer has genuinely left the row.
            def _check():
                try:
                    x, y = row.winfo_pointerxy()
                    rx, ry = row.winfo_rootx(), row.winfo_rooty()
                    inside = (rx <= x < rx + row.winfo_width()
                              and ry <= y < ry + row.winfo_height())
                except tk.TclError:
                    return
                if not inside:
                    _set_bg(C["surface"])
            row.after(1, _check)

        for w in [row, header, icon_lbl, mid, prev_lbl] + \
                 ([app_lbl] if app_lbl else []):
            w.bind("<Enter>", _on_enter)
            w.bind("<Leave>", _on_leave)
            w.configure(cursor="hand2")
            w.bind("<Button-1>", _toggle)
        # time_lbl and the trash canvas keep row-hover alongside their own
        # handlers. The bin turns red on direct hover as a delete affordance.
        time_lbl.bind("<Enter>", _on_enter, add="+")
        time_lbl.bind("<Leave>", _on_leave, add="+")
        del_cv.bind("<Enter>", lambda _e: (del_state.update(fg=C["error"]),
                                           _draw_trash(), _on_enter()))
        del_cv.bind("<Leave>", lambda _e: (del_state.update(fg=C["subtext"]),
                                           _draw_trash(), _on_leave()))
        detail_lbl.bind("<Enter>", _on_enter)
        detail_lbl.bind("<Leave>", _on_leave)
        detail_lbl.bind("<Button-1>", _toggle)

    def _copy_to_clipboard(self, text: str, btn=None) -> None:
        if self._root:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
        if btn:
            btn.configure(text="✓", fg=C["success"])
            self._root.after(1500, lambda: btn.configure(text="⎘", fg=C["subtext"]))

    def _confirm_clear_history(self) -> None:
        for w in self._hist_items.winfo_children():
            w.destroy()
        frame = tk.Frame(self._hist_items, bg=C["surface"])
        frame.pack(fill="x", padx=12, pady=12)
        tk.Label(frame, text="Delete all history?", fg=C["text"], bg=C["surface"],
                 font=("Segoe UI", 10)).pack(anchor="w")
        tk.Label(frame,
                 text="Disappears from the app now; removed from the\n"
                      "cloud after 30 days.",
                 fg=C["subtext"], bg=C["surface"], justify="left",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 0))
        btn_row = tk.Frame(frame, bg=C["surface"])
        btn_row.pack(anchor="w", pady=(8, 0))
        yes = RoundedButton(btn_row, text="Yes, delete all", fg=C["bg"], fill=C["error"],
                            font=("Segoe UI", 9, "bold"), padx=14, pady=6,
                            command=lambda: threading.Thread(
                                target=self._clear_history, daemon=True).start())
        yes.pack(side="left", padx=(0, 8))
        no = RoundedButton(btn_row, text="Cancel", fg=C["subtext"], fill=C["surface_hover"],
                           font=("Segoe UI", 9), padx=14, pady=6,
                           command=lambda: self._load_history())
        no.pack(side="left")

    def _clear_history(self) -> None:
        if self._db:
            self._db.clear_history()
        self._ui_after(0, self._load_history)

    # ── Settings tab ─────────────────────────────────────────────────────────

    def _build_settings_tab(self, parent: tk.Frame) -> None:
        # Scrollable container
        self._settings_cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0)
        self._settings_sb = ModernScrollbar(parent, command=self._settings_cv.yview)
        self._settings_cv.configure(yscrollcommand=self._settings_sb.set)
        self._settings_sb.pack(side="right", fill="y")
        self._settings_cv.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(self._settings_cv, bg=C["bg"])
        _win = self._settings_cv.create_window(0, 0, window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: self._settings_cv.configure(
            scrollregion=self._settings_cv.bbox("all")))
        self._settings_cv.bind("<Configure>", lambda e: self._settings_cv.itemconfigure(
            _win, width=e.width))

        # Shadow parent so all existing code below writes into the scrollable frame
        parent = inner
        cfg = self._config

        # Placeholder shown when an update is available (populated by show_update_banner)
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

        meter_cv = tk.Canvas(mic_card, height=6, bg=C["input_bg"], highlightthickness=0)
        meter_fill_id = meter_cv.create_rectangle(0, 0, 0, 6, fill=C["success"], outline="")

        mic_test_active = [False]
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
            if mic_test_job[0]:
                self._root.after_cancel(mic_test_job[0])
                mic_test_job[0] = None
            if self._recorder:
                self._recorder.stop_monitor()
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
            try:
                self._recorder.start_monitor(device_name)
            except Exception:
                test_status.configure(text="Could not open mic", fg=C["error"])
                return
            mic_test_active[0] = True
            meter_cv.pack(fill="x", pady=(6, 0))
            test_btn.configure(text="Stop")
            test_status.configure(text="Say something…", fg=C["subtext"])
            # Stamp the auto-stop timer: a stale timer from a previous test
            # must not kill a newly started test early.
            mic_test_stamp[0] += 1
            _stamp = mic_test_stamp[0]
            self._root.after(
                5000,
                lambda: _stop_test()
                if mic_test_active[0] and mic_test_stamp[0] == _stamp
                else None,
            )
            _poll_meter()

        def _toggle_test():
            if mic_test_active[0]:
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
        tk.Label(vocab_card,
                 text="Comma-separated names, acronyms, or terms to boost (e.g. FTC, Salesforce, CRM)",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=320).pack(fill="x")
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
        tk.Label(label_col, text="Beeps when recording starts, stops, and transcription finishes",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=260).pack(anchor="w")

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
        tk.Label(caps_col, text="Show the words you're saying in real time (replaces the waveform bar while recording)",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=260).pack(anchor="w")

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
            tk.Label(col, text=subtext, fg=C["subtext"], bg=C["surface"],
                     font=("Segoe UI", 8), anchor="w", justify="left",
                     wraplength=260).pack(anchor="w")

        _toggle_card("auto_punctuate", "Auto Punctuation",
                     "Add a trailing period when speech ends without ending punctuation", True)
        _toggle_card("trailing_space", "Add Trailing Space",
                     "Append a space after each injection (useful for mid-sentence dictation)", False)
        _toggle_card("auto_enter", "Press Enter After Insert",
                     "Send Enter after injecting (useful for chat / search boxes)", False)
        _toggle_card("live_inject", "Live Typing (Beta)",
                     "Type each word into the app as you speak instead of all at once. "
                     "Self-corrects when you finish. Parakeet engine only.", False)
        _toggle_card("warm_mic", "Instant Mic Start",
                     "Keep the microphone warm so recording starts instantly and the "
                     "first word is never clipped (mic indicator stays on; audio is "
                     "only kept for 1.5s and never stored)", True)


        # ── Version / update card ─────────────────────────────────────────────
        ver_card = self._card(parent, margin=(0, 4))
        ver_row = tk.Frame(ver_card, bg=C["surface"])
        ver_row.pack(fill="x")

        ver_lbl_text = f"Version {self._version}" if self._version else "FTC Whisper"
        tk.Label(ver_row, text=ver_lbl_text,
                 fg=C["text"], bg=C["surface"],
                 font=("Segoe UI", 10), anchor="w").pack(side="left")

        # "Check for Updates" button — always visible and clickable
        self._update_check_btn = tk.Label(
            ver_row, text="Check for Updates",
            fg=C["accent"], bg=C["surface"],
            font=("Segoe UI", 9), cursor="hand2", anchor="e",
        )
        self._update_check_btn.pack(side="right")

        # Status label — shows "Up to date ✓" or "Update available: X.X.X"
        self._update_status_lbl = tk.Label(
            ver_row, text="",
            fg=C["success"], bg=C["surface"],
            font=("Segoe UI", 9), anchor="e",
        )
        self._update_status_lbl.pack(side="right", padx=(0, 8))

        def _restore_check_btn():
            self._update_check_btn.configure(
                text="Check for Updates", fg=C["accent"], cursor="hand2")
            self._update_check_btn.bind("<Button-1>", _check_now)

        def _check_now(_e=None):
            self._update_check_btn.configure(text="Checking...", fg=C["subtext"], cursor="")
            self._update_check_btn.unbind("<Button-1>")
            self._update_status_lbl.configure(text="")

            # Three honest outcomes: update / up-to-date / CHECK FAILED. The old
            # flow only had a "found update" callback plus a blind 6s timer that
            # showed "Up to date ✓" — so being offline reported "Up to date ✓"
            # and a real pending update was silently missed.
            def _check_worker():
                from updater import get_latest_release, is_newer
                info = get_latest_release()

                def _apply():
                    if info is None:
                        self._update_status_lbl.configure(
                            text="Check failed — no connection? Retrying works.",
                            fg=C["subtext"])
                        _restore_check_btn()
                    elif is_newer(info["version"], self._version):
                        self._update_status_lbl.configure(
                            text=f"Update available: {info['version']}", fg=C["accent"])
                        _restore_check_btn()
                        # show_update_banner creates tk widgets — it must run on
                        # the UI thread, not the updater worker thread.
                        self.show_update_banner(info["version"], info["download_url"])
                    else:
                        self._update_status_lbl.configure(
                            text="Up to date ✓", fg=C["success"])
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

        # Inline update-action row — shown by show_update_banner when update found
        self._ver_update_row = tk.Frame(ver_card, bg=C["surface"])

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
        self._settings_auth_btn.bind("<Button-1>", lambda _e: self._do_sign_out())
        self._settings_auth_btn.bind("<Enter>",
            lambda _e: self._settings_auth_btn.configure(fg="#ff8888"))
        self._settings_auth_btn.bind("<Leave>",
            lambda _e: self._settings_auth_btn.configure(fg=C["error"]))

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

            top_row = tk.Frame(card, bg=C["surface"])
            top_row.pack(fill="x")

            banner_text = (f"Update {version} installing automatically…" if auto
                           else f"Update available → {version}")
            tk.Label(
                top_row,
                text=banner_text,
                fg=C["accent"], bg=C["surface"],
                font=("Segoe UI", 10, "bold"), anchor="w",
            ).pack(side="left")

            banner_btn = RoundedButton(
                top_row,
                text="Update Now",
                fg=C["bg"], fill=C["accent"],
                font=("Segoe UI", 9, "bold"),
                padx=14, pady=5,
            )
            banner_btn.pack(side="right")
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
        if self._auth.user_email:
            self._do_sign_out()
        else:
            self._do_sign_in()

    def _do_sign_out(self) -> None:
        if not self._auth.user_email:
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
        """Update footer email display after login. Safe to call via after()."""
        email = self._auth.user_email or ""
        if hasattr(self, "_email_display"):
            self._email_display.configure(text=email if email else "")
        if hasattr(self, "_settings_email_lbl"):
            self._settings_email_lbl.configure(text=email)
        if hasattr(self, "_sign_btn"):
            self._sign_btn.configure(text="Sign Out")
        if hasattr(self, "_settings_auth_btn"):
            self._settings_auth_btn.configure(text="Sign Out", fg=C["error"])

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
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x  = (sw - w) // 2
        y  = (sh - h) // 2
        self._root.geometry(f"{w}x{h}+{x}+{y}")

    # ── Rounded card ─────────────────────────────────────────────────────────

    def _card(self, parent: tk.Frame, inner_pad=(18, 14),
              radius: int = 10, margin=(0, 8)) -> tk.Frame:
        """Return an inner Frame sitting inside a rounded-corner Canvas card."""
        cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0)
        cv.pack(fill="x", padx=20, pady=margin)
        px, py = inner_pad
        inner = tk.Frame(cv, bg=C["surface"])
        wid = cv.create_window(px, py, window=inner, anchor="nw")

        def sync(_=None):
            cv.update_idletasks()
            cw = cv.winfo_width()
            fh = inner.winfo_reqheight()
            if cw < 2:
                return
            ch = fh + 2 * py
            cv.configure(height=ch)
            cv.coords(wid, px, py)
            cv.itemconfigure(wid, width=max(1, cw - 2 * px))
            cv.delete("bg")
            _rr(cv, 0, 0, cw - 1, ch - 1, radius,
                fill=C["surface"], outline=C["border"], tags="bg")
            cv.tag_lower("bg")

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
