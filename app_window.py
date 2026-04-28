"""
FTC Whisper — Main application window.

Dashboard: Home / Hotkey / History tabs.
Dark theme with rounded-corner cards via Canvas.
"""

import threading
import time
import tkinter as tk
from datetime import datetime
from typing import Callable, Optional
import ctypes

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
    ):
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
        self._root = tk.Tk()
        self._root.title("FTC Whisper")
        self._root.configure(bg=C["bg"])
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._hide)

        self._apply_dark_titlebar()

        self._build_header()

        self._dash_frame = tk.Frame(self._root, bg=C["bg"])
        self._build_dashboard(self._dash_frame)
        self._show_dashboard()
        self._root.after(50, self._fire_authenticated)

        self._root.mainloop()
        # Destroy after mainloop exits (quit() was called on sign-out)
        try:
            self._root.destroy()
        except Exception:
            pass
        self._root = None

    def show(self) -> None:
        if self._root:
            self._root.after(0, self._do_show)

    def _do_show(self) -> None:
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()

    def update_status(self, state: str) -> None:
        if self._root and hasattr(self, "_status_lbl"):
            text, color = self._STATUS.get(state, ("● Ready", C["success"]))
            self._root.after(0, lambda: self._status_lbl.configure(text=text, fg=color))

    # ── Windows dark title bar ────────────────────────────────────────────────

    def _apply_dark_titlebar(self) -> None:
        try:
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
            if not hwnd:
                hwnd = self._root.winfo_id()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        header = tk.Frame(self._root, bg=C["bg"], pady=20)
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

        # Hairline divider
        tk.Frame(self._root, bg=C["divider"], height=1).pack(fill="x")

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
        sign_label = "Sign Out" if email else "Sign In"
        self._sign_btn = self._ghost_btn(footer, sign_label, self._do_sign_action)
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
            self._load_history()
            if self._root:
                self._root.bind_all("<MouseWheel>", self._hist_scroll)
        elif name == "settings":
            if self._root and hasattr(self, "_settings_cv"):
                self._root.bind_all("<MouseWheel>", lambda e: self._settings_cv.yview_scroll(
                    int(-1 * (e.delta / 40)), "units"))
        else:
            if self._root:
                try:
                    self._root.unbind_all("<MouseWheel>")
                except Exception:
                    pass

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

        tk.Label(
            hint_row, text=" hold to dictate",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10),
        ).pack(side="left")

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
        tk.Label(
            ic, text="Hold the hotkey and speak.\nRelease to transcribe into your cursor.",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10), justify="left", anchor="w",
        ).pack(fill="x")

    # ── Hotkey tab ────────────────────────────────────────────────────────────

    def _build_hotkey_tab(self, parent: tk.Frame) -> None:
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

        self._save_btn = tk.Label(
            btn_row, text="Save",
            fg=C["subtext"], bg=C["border"],
            font=("Segoe UI", 10, "bold"), padx=14, pady=8,
        )
        self._save_btn.pack(side="left")

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

        self._refine_save_btn = tk.Label(
            btn_row2, text="Save",
            fg=C["subtext"], bg=C["border"],
            font=("Segoe UI", 10, "bold"), padx=14, pady=8,
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

        # Rounded card background canvas
        card_cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0)
        card_cv.pack(fill="both", expand=True, padx=20, pady=(0, 8))

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

        # Scrollable canvas + scrollbar inside card_inner
        self._hist_sb = tk.Scrollbar(card_inner, orient="vertical",
                                     troughcolor=C["surface"], bg=C["scrollbar"],
                                     width=14)
        self._hist_cv = tk.Canvas(card_inner, bg=C["surface"],
                                  highlightthickness=0, bd=0,
                                  yscrollcommand=self._hist_sb.set)
        self._hist_sb.config(command=self._hist_cv.yview)
        self._hist_sb.pack(side="right", fill="y")
        self._hist_cv.pack(side="left", fill="both", expand=True)

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
        self._hist_set_placeholder("Loading…")
        def _fetch():
            items = self._db.fetch_history() if self._db else []
            self._root.after(0, self._populate_history, items)
        threading.Thread(target=_fetch, daemon=True).start()

    def _hist_set_placeholder(self, msg: str) -> None:
        for w in self._hist_items.winfo_children():
            w.destroy()
        tk.Label(self._hist_items, text=msg,
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 10, "italic"),
                 padx=12, pady=16).pack(fill="x")

    def _populate_history(self, items: list) -> None:
        for w in self._hist_items.winfo_children():
            w.destroy()
        if not items:
            self._hist_set_placeholder("No transcriptions yet.")
            return
        for i, item in enumerate(items):
            self._make_history_row(i, item)

    def _make_history_row(self, index: int, item: dict) -> None:
        text = item.get("refined_text") or item.get("transcribed_text", "")
        raw_ts = item.get("created_at", "")
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            ts_str = dt.astimezone().strftime("%d %b %Y  %H:%M")
        except Exception:
            ts_str = raw_ts[:16]

        if index > 0:
            tk.Frame(self._hist_items, bg=C["border"], height=1).pack(fill="x")

        row = tk.Frame(self._hist_items, bg=C["surface"])
        row.pack(fill="x")

        # Header: [▶ toggle]  [timestamp + preview]  [⎘ copy]
        header = tk.Frame(row, bg=C["surface"])
        header.pack(fill="x", padx=8, pady=(8, 6))

        # Pack copy button FIRST (side=right) so mid's expand never pushes it off
        copy_btn = tk.Label(header, text="⎘", fg=C["subtext"], bg=C["surface"],
                            font=("Segoe UI", 13), cursor="hand2")
        copy_btn.pack(side="right")
        copy_btn.bind("<Button-1>",
                      lambda _e, t=text, b=copy_btn: self._copy_to_clipboard(t, b))
        copy_btn.bind("<Enter>", lambda _e: copy_btn.configure(fg=C["accent"]))
        copy_btn.bind("<Leave>", lambda _e: copy_btn.configure(fg=C["subtext"]))

        toggle = tk.Label(header, text="▶", fg=C["subtext"], bg=C["surface"],
                          font=("Segoe UI", 8), cursor="hand2", width=2, anchor="w")
        toggle.pack(side="left")

        mid = tk.Frame(header, bg=C["surface"])
        mid.pack(side="left", fill="x", expand=True, padx=(4, 4))

        tk.Label(mid, text=ts_str, fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w").pack(fill="x")

        preview = (text[:72] + "…") if len(text) > 72 else text
        prev_lbl = tk.Label(mid, text=preview, fg=C["text"], bg=C["surface"],
                            font=("Segoe UI", 9), anchor="w", justify="left")
        prev_lbl.pack(fill="x")

        # Expanded detail (hidden until toggled; replaces preview to avoid duplication)
        detail = tk.Frame(row, bg=C["surface"])
        detail_lbl = tk.Label(detail, text=text, fg=C["text"], bg=C["surface"],
                              font=("Segoe UI", 9), anchor="w", justify="left",
                              wraplength=300)
        detail_lbl.pack(fill="x", padx=(22, 8), pady=(0, 8))

        expanded = [False]

        def _toggle(_e=None, _detail=detail, _toggle_lbl=toggle, _prev=prev_lbl):
            if expanded[0]:
                _detail.pack_forget()
                _prev.pack(fill="x")
                _toggle_lbl.configure(text="▶", fg=C["subtext"])
                expanded[0] = False
            else:
                _prev.pack_forget()
                _detail.pack(fill="x", after=header)
                _toggle_lbl.configure(text="▼", fg=C["accent"])
                expanded[0] = True

        toggle.bind("<Button-1>", _toggle)
        prev_lbl.bind("<Button-1>", _toggle)
        mid.bind("<Button-1>", _toggle)


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
        btn_row = tk.Frame(frame, bg=C["surface"])
        btn_row.pack(anchor="w", pady=(8, 0))
        yes = tk.Label(btn_row, text=" Yes, delete all ", fg=C["bg"], bg=C["error"],
                       font=("Segoe UI", 9, "bold"), cursor="hand2")
        yes.pack(side="left", padx=(0, 8))
        yes.bind("<Button-1>",
                 lambda _e: threading.Thread(target=self._clear_history, daemon=True).start())
        no = tk.Label(btn_row, text=" Cancel ", fg=C["subtext"], bg=C["surface_hover"],
                      font=("Segoe UI", 9), cursor="hand2")
        no.pack(side="left")
        no.bind("<Button-1>", lambda _e: self._load_history())

    def _clear_history(self) -> None:
        if self._db:
            self._db.clear_history()
        self._root.after(0, self._load_history)

    # ── Settings tab ─────────────────────────────────────────────────────────

    def _build_settings_tab(self, parent: tk.Frame) -> None:
        # Scrollable container
        self._settings_sb = tk.Scrollbar(parent, orient="vertical",
                                         troughcolor=C["surface"], bg=C["scrollbar"],
                                         width=14)
        self._settings_cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0,
                                      bd=0, yscrollcommand=self._settings_sb.set)
        self._settings_sb.config(command=self._settings_cv.yview)
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

        # ── Microphone ────────────────────────────────────────────────────────
        mic_card = self._card(parent, margin=(0, 8))
        tk.Label(mic_card, text="Microphone",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")

        try:
            devs = self._get_input_devices() if self._get_input_devices else []
        except Exception:
            devs = []

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

        seen_names: set = set()
        unique_devs = []
        for d in devs:
            if d["name"] not in seen_names:
                seen_names.add(d["name"])
                unique_devs.append(d)
        unique_devs.sort(key=_mic_rank)

        mic_options = ["Default"] + [d["name"] for d in unique_devs]
        current_mic = (cfg.input_device or "") if cfg else ""
        mic_var = tk.StringVar(value=current_mic if current_mic in mic_options else "Default")

        mic_menu = tk.OptionMenu(mic_card, mic_var, *mic_options)
        mic_menu.configure(bg=C["surface_hover"], fg=C["text"], relief="flat",
                           font=("Segoe UI", 9), anchor="w", highlightthickness=0,
                           activebackground=C["accent"], activeforeground=C["bg"])
        mic_menu["menu"].configure(bg=C["surface"], fg=C["text"],
                                   activebackground=C["accent"], activeforeground=C["bg"],
                                   font=("Segoe UI", 9))
        mic_menu.pack(fill="x", pady=(4, 0))

        # ── Mic test button + level meter ─────────────────────────────────────
        btn_row = tk.Frame(mic_card, bg=C["surface"])
        btn_row.pack(fill="x", pady=(8, 0))

        test_btn = tk.Label(btn_row, text="Test Mic",
                            fg=C["text"], bg=C["surface_hover"],
                            font=("Segoe UI", 9), padx=10, pady=6, cursor="hand2")
        test_btn.pack(side="left", padx=(0, 6))
        test_btn.bind("<Enter>", lambda _e: test_btn.configure(bg=C["accent"], fg=C["bg"]))
        test_btn.bind("<Leave>", lambda _e: test_btn.configure(bg=C["surface_hover"], fg=C["text"]))

        scan_btn = tk.Label(btn_row, text="Find Best Mic",
                            fg=C["text"], bg=C["surface_hover"],
                            font=("Segoe UI", 9), padx=10, pady=6, cursor="hand2")
        scan_btn.pack(side="left")

        test_status = tk.Label(mic_card, text="", fg=C["subtext"], bg=C["surface"],
                               font=("Segoe UI", 8), anchor="w", wraplength=340)
        test_status.pack(fill="x", pady=(4, 0))

        meter_cv = tk.Canvas(mic_card, height=6, bg=C["input_bg"], highlightthickness=0)
        meter_fill_id = meter_cv.create_rectangle(0, 0, 0, 6, fill=C["success"], outline="")

        mic_test_active = [False]
        mic_test_job = [None]
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
            self._root.after(5000, lambda: _stop_test() if mic_test_active[0] else None)
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
                test_status.configure(text=f"Best mic: {short} ✓", fg=C["success"])
                mic_var.set(best_name)
            else:
                test_status.configure(
                    text="No signal detected — try speaking louder", fg=C["error"])

        def _run_scan():
            mics_to_test = [d for d in unique_devs if _mic_rank(d) < 2]
            results = {}
            total = len(mics_to_test)
            for i, d in enumerate(mics_to_test):
                name = d["name"]
                short = name if len(name) <= 24 else name[:21] + "…"
                msg = f"Testing {i + 1}/{total}: {short}"
                self._root.after(0, lambda m=msg: test_status.configure(
                    text=m, fg=C["subtext"]))
                try:
                    self._recorder.start_monitor(name)
                except Exception:
                    results[name] = 0.0
                    continue
                samples = []
                for _ in range(15):
                    time.sleep(0.1)
                    rms, _ = self._recorder.get_live_levels()
                    samples.append(rms)
                self._recorder.stop_monitor()
                results[name] = max(samples) if samples else 0.0
                time.sleep(0.05)
            if results:
                best = max(results, key=results.get)
                self._root.after(0, lambda b=best, v=results[best]: _scan_done(b, v))
            else:
                self._root.after(0, lambda: _scan_done(None, 0.0))

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
            test_status.configure(
                text=f"Speak continuously! Testing {len(mics_to_test)} mics…",
                fg=C["accent"])
            threading.Thread(target=_run_scan, daemon=True).start()

        scan_btn.bind("<Button-1>", lambda _e: _start_scan())

        # ── Whisper model ─────────────────────────────────────────────────────
        model_card = self._card(parent, margin=(0, 0))
        tk.Label(model_card, text="Transcription Model",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")
        tk.Label(model_card, text="Larger = more accurate, slower to load",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w").pack(fill="x")

        model_options = ["tiny.en", "base.en", "small.en", "medium.en",
                         "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
        current_model = (cfg.whisper_model if cfg else "base.en") or "base.en"
        model_var = tk.StringVar(value=current_model)

        model_menu = tk.OptionMenu(model_card, model_var, *model_options)
        model_menu.configure(bg=C["surface_hover"], fg=C["text"], relief="flat",
                             font=("Segoe UI", 9), anchor="w", highlightthickness=0,
                             activebackground=C["accent"], activeforeground=C["bg"])
        model_menu["menu"].configure(bg=C["surface"], fg=C["text"],
                                     activebackground=C["accent"], activeforeground=C["bg"],
                                     font=("Segoe UI", 9))
        model_menu.pack(fill="x", pady=(4, 0))

        # ── Model benchmark ───────────────────────────────────────────────────
        bench_btn_row = tk.Frame(model_card, bg=C["surface"])
        bench_btn_row.pack(fill="x", pady=(8, 0))

        bench_btn = tk.Label(bench_btn_row, text="Benchmark Models",
                             fg=C["text"], bg=C["surface_hover"],
                             font=("Segoe UI", 9), padx=10, pady=6, cursor="hand2")
        bench_btn.pack(side="left")
        bench_btn.bind("<Enter>", lambda _e: bench_btn.configure(bg=C["accent"], fg=C["bg"]) if not bench_active[0] else None)
        bench_btn.bind("<Leave>", lambda _e: bench_btn.configure(bg=C["surface_hover"], fg=C["text"]) if not bench_active[0] else None)

        bench_status = tk.Label(model_card, text="", fg=C["subtext"], bg=C["surface"],
                                font=("Segoe UI", 8), anchor="w", wraplength=340)

        bench_results_frame = tk.Frame(model_card, bg=C["surface"])
        bench_active = [False]

        def _show_results(results):
            bench_active[0] = False
            bench_btn.configure(text="Benchmark Models", fg=C["text"],
                                bg=C["surface_hover"], cursor="hand2")
            bench_btn.bind("<Button-1>", lambda _e: _start_benchmark())
            bench_status.configure(text="")
            bench_status.pack_forget()

            for w in bench_results_frame.winfo_children():
                w.destroy()

            # Determine recommended model: largest that ran in <5s
            fast = [(n, t) for n, t, _tx, _e in results if t is not None and t < 5.0]
            slow = [(n, t) for n, t, _tx, _e in results if t is not None and t >= 5.0]
            if fast:
                recommended = fast[-1][0]
            elif slow:
                recommended = min(slow, key=lambda x: x[1])[0]
            else:
                recommended = None

            tk.Frame(bench_results_frame, bg=C["border"], height=1).pack(fill="x", pady=(0, 4))

            for name, elapsed, _text, err in results:
                row = tk.Frame(bench_results_frame, bg=C["surface"])
                row.pack(fill="x", pady=1)
                is_rec = (name == recommended)
                fg_name = C["accent"] if is_rec else C["text"]
                font_name = ("Segoe UI", 8, "bold") if is_rec else ("Segoe UI", 8)
                tk.Label(row, text=name, fg=fg_name, bg=C["surface"],
                         font=font_name, width=17, anchor="w").pack(side="left")
                if elapsed is not None:
                    col = C["success"] if elapsed < 5 else (C["accent"] if elapsed < 15 else C["error"])
                    tk.Label(row, text=f"{elapsed:.1f}s", fg=col, bg=C["surface"],
                             font=("Segoe UI", 8), width=6, anchor="w").pack(side="left")
                else:
                    tk.Label(row, text="skipped", fg=C["subtext"], bg=C["surface"],
                             font=("Segoe UI", 8), width=6, anchor="w").pack(side="left")
                if is_rec:
                    tk.Label(row, text="← Best", fg=C["success"], bg=C["surface"],
                             font=("Segoe UI", 8, "bold")).pack(side="left")

            if recommended:
                tk.Frame(bench_results_frame, bg=C["border"], height=1).pack(fill="x", pady=(4, 4))
                use_row = tk.Frame(bench_results_frame, bg=C["surface"])
                use_row.pack(fill="x")
                tk.Label(use_row, text=f"Recommended: {recommended}",
                         fg=C["success"], bg=C["surface"],
                         font=("Segoe UI", 8)).pack(side="left")
                use_btn = tk.Label(use_row, text="Use this",
                                   fg=C["bg"], bg=C["accent"],
                                   font=("Segoe UI", 8, "bold"), padx=8, pady=3, cursor="hand2")
                use_btn.pack(side="right")
                use_btn.bind("<Button-1>", lambda _e: model_var.set(recommended))
                use_btn.bind("<Enter>", lambda _e: use_btn.configure(bg=C["accent_hover"]))
                use_btn.bind("<Leave>", lambda _e: use_btn.configure(bg=C["accent"]))

            bench_results_frame.pack(fill="x", pady=(4, 0))

        def _run_tests(audio):
            import gc
            from faster_whisper import WhisperModel as _WM
            device = getattr(self._transcriber, "_device", "cpu")
            compute = getattr(self._transcriber, "_compute_type", "int8")
            threads = getattr(self._transcriber, "_cpu_threads", 4)

            models_to_test = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3-turbo"]
            results = []

            for i, model_name in enumerate(models_to_test):
                msg = f"Testing {model_name} ({i + 1}/{len(models_to_test)})…"
                self._root.after(0, lambda m=msg: bench_status.configure(text=m, fg=C["subtext"]))
                try:
                    import time as _t
                    t0 = _t.time()
                    wm = _WM(model_name, device=device, compute_type=compute,
                             cpu_threads=threads, num_workers=1)
                    if audio is not None and len(audio) > 0:
                        is_en = model_name.endswith(".en")
                        segs, _ = wm.transcribe(
                            audio,
                            language=None if is_en else "en",
                            beam_size=1, vad_filter=True,
                        )
                        text = "".join(s.text for s in segs).strip()
                    else:
                        text = ""
                    elapsed = _t.time() - t0
                    del wm
                    gc.collect()
                    results.append((model_name, elapsed, text, None))
                except Exception as e:
                    results.append((model_name, None, "", str(e)))

                last_elapsed = results[-1][1]
                if last_elapsed is not None and last_elapsed > 15.0:
                    for rem in models_to_test[i + 1:]:
                        results.append((rem, None, "", "skipped — previous too slow"))
                    break

            self._root.after(0, lambda: _show_results(results))

        def _start_benchmark():
            if bench_active[0]:
                return
            if not self._recorder:
                return
            if self._recorder.is_recording:
                bench_status.configure(text="Stop recording first", fg=C["error"])
                bench_status.pack(fill="x", pady=(4, 0))
                return

            for w in bench_results_frame.winfo_children():
                w.destroy()
            bench_results_frame.pack_forget()

            bench_active[0] = True
            bench_btn.configure(text="Recording…", fg=C["subtext"],
                                bg=C["surface"], cursor="")
            bench_btn.unbind("<Button-1>")
            bench_status.pack(fill="x", pady=(4, 0))

            selected_mic = mic_var.get()
            device_name = "" if selected_mic == "Default" else selected_mic
            dev_index = None
            if device_name:
                try:
                    for d in self._recorder.get_input_devices():
                        if d["name"] == device_name:
                            dev_index = d["index"]
                            break
                except Exception:
                    pass

            chunks = []
            try:
                import sounddevice as _sd
                stream = _sd.InputStream(
                    samplerate=16000, channels=1, dtype="float32",
                    callback=lambda indata, *_: chunks.append(indata.copy()),
                    blocksize=1024, device=dev_index,
                )
                stream.start()
            except Exception as e:
                bench_active[0] = False
                bench_btn.configure(text="Benchmark Models", fg=C["text"],
                                    bg=C["surface_hover"], cursor="hand2")
                bench_btn.bind("<Button-1>", lambda _e: _start_benchmark())
                bench_status.configure(text=f"Could not open mic: {e}", fg=C["error"])
                return

            countdown = [5]
            bench_status.configure(
                text=f"Speak naturally — recording {countdown[0]}s…", fg=C["accent"])

            def _tick():
                countdown[0] -= 1
                if countdown[0] > 0:
                    bench_status.configure(
                        text=f"Speak naturally — recording {countdown[0]}s…", fg=C["accent"])
                    self._root.after(1000, _tick)
                else:
                    stream.stop()
                    stream.close()
                    import numpy as _np
                    audio = _np.concatenate(chunks, axis=0).flatten() if chunks else None
                    bench_btn.configure(text="Testing…")
                    bench_status.configure(
                        text="Testing models — this may take a few minutes…",
                        fg=C["subtext"])
                    threading.Thread(target=_run_tests, args=(audio,), daemon=True).start()

            self._root.after(1000, _tick)

        bench_btn.bind("<Button-1>", lambda _e: _start_benchmark())

        # ── Anthropic API Key ─────────────────────────────────────────────────
        api_card = self._card(parent, margin=(0, 8))
        tk.Label(api_card, text="Anthropic API Key",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")
        tk.Label(api_card, text="Optional — enables AI text refinement (filler word removal, cleanup)",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w").pack(fill="x")

        api_row = tk.Frame(api_card, bg=C["surface"])
        api_row.pack(fill="x", pady=(4, 0))

        current_key = (cfg.anthropic_api_key if cfg else "") or ""
        api_var = tk.StringVar(value=current_key)
        api_entry = tk.Entry(api_row, textvariable=api_var, show="●",
                             bg=C["input_bg"], fg=C["text"], insertbackground=C["text"],
                             relief="flat", font=("Segoe UI", 9), bd=6)
        api_entry.pack(side="left", fill="x", expand=True)

        show_var = [False]
        show_btn = tk.Label(api_row, text="Show", fg=C["subtext"], bg=C["surface"],
                            font=("Segoe UI", 8), cursor="hand2", padx=6)
        show_btn.pack(side="right")

        def _toggle_show(_e=None):
            show_var[0] = not show_var[0]
            api_entry.configure(show="" if show_var[0] else "●")
            show_btn.configure(text="Hide" if show_var[0] else "Show")
        show_btn.bind("<Button-1>", _toggle_show)

        # ── Sound feedback ────────────────────────────────────────────────────
        sound_card = self._card(parent, margin=(0, 0))
        sound_row = tk.Frame(sound_card, bg=C["surface"])
        sound_row.pack(fill="x")

        current_sound = bool(cfg.sound_feedback if cfg else True)
        sound_var = tk.BooleanVar(value=current_sound)

        # Pack toggle first so expand=True on label_col doesn't consume all space
        sound_toggle = tk.Label(sound_row, text="", fg=C["subtext"], bg=C["surface"],
                                font=("Segoe UI", 9, "bold"), cursor="hand2")
        sound_toggle.pack(side="right")

        def _make_toggle_btn():
            txt = "On" if sound_var.get() else "Off"
            col = C["success"] if sound_var.get() else C["subtext"]
            sound_toggle.configure(text=txt, fg=col)

        label_col = tk.Frame(sound_row, bg=C["surface"])
        label_col.pack(side="left", fill="x", expand=True)
        tk.Label(label_col, text="Sound Feedback",
                 fg=C["text"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(anchor="w")
        tk.Label(label_col, text="Beeps when recording starts, stops, and transcription finishes",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 8), anchor="w").pack(anchor="w")

        def _toggle_sound(_e=None):
            sound_var.set(not sound_var.get())
            _make_toggle_btn()
        sound_toggle.bind("<Button-1>", _toggle_sound)
        _make_toggle_btn()

        # ── Save button ───────────────────────────────────────────────────────
        save_wrap = tk.Frame(parent, bg=C["bg"])
        save_wrap.pack(fill="x", padx=20, pady=(12, 4))

        self._settings_status = tk.Label(save_wrap, text="",
                                         fg=C["success"], bg=C["bg"],
                                         font=("Segoe UI", 9))
        self._settings_status.pack(side="left")

        def _save(_e=None):
            if self._on_settings_change:
                mic_val = mic_var.get()
                self._on_settings_change("input_device",
                                         "" if mic_val == "Default" else mic_val)
                self._on_settings_change("whisper_model", model_var.get())
                self._on_settings_change("anthropic_api_key", api_var.get().strip())
                self._on_settings_change("sound_feedback", sound_var.get())
            self._settings_status.configure(text="Saved ✓", fg=C["success"])
            if self._root:
                self._root.after(2500, lambda: self._settings_status.configure(text=""))

        save_btn = self._surface_btn(save_wrap, "Save Settings", _save)
        save_btn.pack(side="right")

        # ── Sign out / Sign in (settings tab) ────────────────────────────────
        signout_wrap = tk.Frame(parent, bg=C["bg"])
        signout_wrap.pack(fill="x", padx=20, pady=(0, 8))
        email = self._auth.user_email or ""
        self._settings_auth_btn = tk.Label(
            signout_wrap,
            text="Sign Out" if email else "Sign In",
            fg=C["error"] if email else C["subtext"],
            bg=C["bg"],
            font=("Segoe UI", 9), cursor="hand2", anchor="e",
        )
        self._settings_auth_btn.pack(side="right")
        self._settings_auth_btn.bind("<Button-1>", lambda _e: self._do_sign_action())
        self._settings_auth_btn.bind(
            "<Enter>",
            lambda _e: self._settings_auth_btn.configure(
                fg="#ff8888" if self._auth.user_email else C["text"]))
        self._settings_auth_btn.bind(
            "<Leave>",
            lambda _e: self._settings_auth_btn.configure(
                fg=C["error"] if self._auth.user_email else C["subtext"]))

    # ── Auth callbacks ────────────────────────────────────────────────────────

    def _fire_authenticated(self) -> None:
        threading.Thread(
            target=self._on_authenticated, args=(self._auth,), daemon=True
        ).start()

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
        self._dash_frame.pack_forget()

        def _after_relogin(auth):
            if self._on_sign_in:
                threading.Thread(target=self._on_sign_in, args=(auth,), daemon=True).start()

        def _after_cancel():
            # Closed login without signing in — show dashboard in offline mode
            self._root.deiconify()
            self._show_dashboard()
            self._apply_auth_ui()

        self._show_login_screen(after_login=_after_relogin, after_cancel=_after_cancel)

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
        """Update footer and settings tab to reflect current auth state. Safe to call via after()."""
        email = self._auth.user_email or ""
        if hasattr(self, "_email_display"):
            self._email_display.configure(text=email if email else "Not signed in")
        if hasattr(self, "_sign_btn"):
            self._sign_btn.configure(text="Sign Out" if email else "Sign In")
        if hasattr(self, "_settings_auth_btn"):
            self._settings_auth_btn.configure(
                text="Sign Out" if email else "Sign In",
                fg=C["error"] if email else C["subtext"],
            )

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

    def _surface_btn(self, parent, text, cmd) -> tk.Label:
        btn = tk.Label(
            parent, text=text,
            fg=C["text"], bg=C["surface_hover"],
            font=("Segoe UI", 10), padx=12, pady=8, cursor="hand2",
        )
        btn.bind("<Button-1>", lambda _e: cmd())
        btn.bind("<Enter>",    lambda _e: btn.configure(bg=C["accent"], fg=C["bg"]))
        btn.bind("<Leave>",    lambda _e: btn.configure(bg=C["surface_hover"], fg=C["text"]))
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
